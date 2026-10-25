"""Estimate every player's VP from public information, as the app would score it if the game ended now.

The app hides other players' VP breakdowns unless the game shows them, so this recomputes them from what
everyone can see: TR, claimed milestones, funded awards' standings, tiles on the board and played cards.
Card VP the bundled data can't compute (custom card rules, VP from neighbouring tiles) is listed by name.
"""
import re

from . import data

CITY_TILES = {"city", "Capital", "Ocean City", "Red City", "New Holland"}
GREENERY_TILES = {"greenery", "Wetlands"}
OCEAN_TILES = {"ocean", "Ocean City", "Ocean Farm", "Ocean Sanctuary", "Wetlands", "New Holland"}


def _tile(space):
    return data.tile_name(space["tileType"]) if space.get("tileType") is not None else None


def award_points(game, color):
    """5 for first (shared on a tie), 2 for second when first is not tied; as the app scores funded awards."""
    total = 0
    for award in game["awards"]:
        if not award.get("playerName"):
            continue
        scores = sorted((s["score"] for s in award["scores"]), reverse=True)
        mine = next((s["score"] for s in award["scores"] if s["color"] == color), None)
        if mine is None or not scores:
            continue
        if mine == scores[0]:
            total += 5
        elif scores.count(scores[0]) == 1 and len(scores) > 2 and mine == scores[1]:
            total += 2
    return total


def board_points(game, board_name, color):
    """(greenery, city) points from the tiles a player owns."""
    spaces = {s["id"]: s for s in game["spaces"]}
    greenery = city = 0
    for s in game["spaces"]:
        if s.get("color") != color:
            continue
        tile = _tile(s)
        if tile in GREENERY_TILES:
            greenery += 1
        elif tile in CITY_TILES:
            city += sum(1 for a in data.adjacency(board_name, s["id"]) if _tile(spaces.get(a, {})) in GREENERY_TILES)
    return greenery, city


def _next_to_own_tile(game, color, card_name, kinds):
    """Tiles of `kinds` next to the player's tile named after the card (e.g. Commercial District), or None."""
    tile_name = card_name.split(":")[0]
    spaces = {s["id"]: s for s in game["spaces"]}
    board_name = game["gameOptions"]["boardName"]
    for s in game["spaces"]:
        if s.get("color") == color and _tile(s) == tile_name:
            return sum(1 for a in data.adjacency(board_name, s["id"]) if _tile(spaces.get(a, {})) in kinds)
    return None


def card_points(player, game):
    """(points, cards whose VP could not be computed) for a player's played cards."""
    points, unknown, negative = 0, [], 0
    cities_in_play = sum(1 for s in game["spaces"] if _tile(s) in CITY_TILES)
    colonies_in_play = sum(len(c.get("colonies", [])) for c in game.get("colonies") or [])
    for card in player["tableau"]:
        vp = (data.behavior(card["name"]) or {}).get("vp")
        if vp is None:
            continue
        if isinstance(vp, (int, float)):
            got = vp
        elif isinstance(vp, dict) and "resourcesHere" in vp:
            n = card.get("resources") or 0
            got = n * vp["each"] if "each" in vp else n // vp.get("per", 1)
        elif isinstance(vp, dict) and "tag" in vp and len(vp) <= 2:
            got = player["tags"].get(vp["tag"], 0) // vp.get("per", 1)
        elif isinstance(vp, dict) and "cities" in vp and vp.get("all"):
            got = cities_in_play // vp.get("per", 1)
        elif isinstance(vp, dict) and "colonies" in vp and vp.get("all"):
            got = colonies_in_play // vp.get("per", 1)
        elif isinstance(vp, dict) and "nextToThis" in vp and ("cities" in vp or "oceans" in vp):
            got = _next_to_own_tile(game, player["color"], card["name"], CITY_TILES if "cities" in vp else OCEAN_TILES)
            if got is None:
                unknown.append(card["name"])
                continue
        else:
            unknown.append(card["name"])
            continue
        points += got
        negative += min(got, 0)
    return points, unknown, negative


def colony_build_points(game, color):
    """VP from building colonies on tiles whose build reward is VP by order placed (Titania: 5, 3, 2)."""
    total = 0
    for col in game.get("colonies") or []:
        m = re.match(r"Gain ([\d, or]+) VP", (data.colony(col["name"]) or {}).get("build", ""))
        if not m:
            continue
        rewards = [int(x) for x in re.findall(r"\d+", m.group(1))]
        total += sum(rewards[i] for i, owner in enumerate(col.get("colonies", [])) if owner == color and i < len(rewards))
    return total


def estimate(view, player, colony_vp=None):
    """VP breakdown for one player, computed from public information only. `colony_vp` (color -> VP, from the
    log) replaces the build-only count of colony VP when given: it includes Titania trades."""
    g = view["game"]
    color = player["color"]
    greenery, city = board_points(g, g["gameOptions"]["boardName"], color)
    cards, unknown, negative = card_points(player, g)
    cards += colony_vp.get(color, 0) if colony_vp is not None else colony_build_points(g, color)
    cards += track_points(g, player, view["players"])
    if g["gameOptions"]["expansions"].get("underworld"):
        cards += min(-negative, (player.get("underworldData") or {}).get("corruption", 0))
    parts = {
        "terraformRating": player["terraformRating"],
        "milestones": 5 * sum(1 for m in g["milestones"] if m.get("playerName") == player["name"]),
        "awards": award_points(g, color),
        "greenery": greenery,
        "city": city,
        "victoryPoints": cards,
    }
    parts["total"] = sum(parts.values())
    return parts, unknown


def vp_trade_colonies(game):
    """Names of colonies in play whose trade reward is VP (Titania)."""
    return {c["name"] for c in game.get("colonies") or [] if "VP" in (data.colony(c["name"]) or {}).get("trade", "")}


def colony_vp_from_logs(logs):
    """color -> VP from colony builds, trades and bonuses, from the app's '${0} gained ${1} VP' log lines."""
    out = {}
    for entry in logs:
        if entry.get("message") != "${0} gained ${1} VP":
            continue
        values = [d.get("value") for d in entry.get("data", [])]
        if len(values) >= 2:
            try:
                out[values[0]] = out.get(values[0], 0) + int(values[1])
            except (TypeError, ValueError):
                pass
    return out


def track_points(game, player, players):
    """VP from Pathfinders planetary tracks that reached their end: most tags of that planet scores it (ties
    share it)."""
    total = 0
    for track, pos in (game.get("pathfinders") or {}).items():
        layout = data.planetary_tracks().get(track, [])
        if pos is None or not layout or pos < len(layout) - 1:
            continue
        vp = sum(int(r[:-2]) for r in layout[-1].get("mostTags", []) if r.endswith("vp"))
        counts = {p["color"]: (p.get("tags") or {}).get(track, 0) for p in players}
        top = max(counts.values(), default=0)
        if top and counts.get(player["color"]) == top:
            total += vp
    return total


# Standard projects that turn M€ into points: (cost, global parameter it needs open or None, VP when it is open,
# VP when it is not). Rule costs; ocean adjacency M€ and city adjacency for greeneries are left out.
SP_POINTS = (("Asteroid", 14, "temperature", 1, 0), ("Aquifer", 18, "oceans", 1, 0), ("Greenery", 23, "oxygen", 2, 1))


def _steps_left(game):
    board = (game.get("gameOptions") or {}).get("boardName")
    return {"temperature": data.steps_left("temperature", game["temperature"], board),
            "oxygen": data.steps_left("oxygen", game["oxygenLevel"], board),
            "oceans": data.steps_left("oceans", game["oceans"], board)}


def _sp_rate(left):
    """M€ per VP of the cheapest standard project that still scores."""
    rates = [cost / (vp if left.get(param) else vp_closed) for _, cost, param, vp, vp_closed in SP_POINTS
             if (vp if left.get(param) else vp_closed)]
    return min(rates) if rates else None


def final_gen_upside(player, game):
    """(VP, text): a rough count of what a player can still add in the last generation from what they hold:
    plants (plus plant production, converted after the final production phase) into greeneries, heat into
    temperature steps, and M€ into the cheapest standard project per VP. Cards in hand are not counted."""
    left = _steps_left(game)
    greeneries = int(player.get("plants", 0) + player.get("plantProduction", 0)) // 8
    green_vp = greeneries + min(greeneries, left["oxygen"])
    heat_vp = min(int(player.get("heat", 0)) // 8, left["temperature"])
    rate = _sp_rate(left)
    mc_vp = int(player.get("megacredits", 0) / rate) if rate else 0
    total = green_vp + heat_vp + mc_vp
    bits = [f"{greeneries} greenery" if greeneries else "", f"{heat_vp} heat TR" if heat_vp else "",
            f"{mc_vp} from {player.get('megacredits', 0)} M€" if mc_vp else ""]
    return total, ", ".join(b for b in bits if b)


def _resource_growth(player, gens):
    """VP a player's resource-scoring cards add over `gens` generations: each action that adds resources here is
    used once a generation; a card fed by triggers gets about one resource every other generation."""
    total = 0
    for card in player.get("tableau") or []:
        beh = data.behavior(card["name"]) or {}
        vp = beh.get("vp")
        if not (isinstance(vp, dict) and "resourcesHere" in vp):
            continue
        action = beh.get("action") or {}
        options = action["or"].get("behaviors", []) if "or" in action else [action]
        adds = [o.get("addResources") for o in options if isinstance(o.get("addResources"), (int, float))]
        rate = max(adds) if adds else (0.5 if beh.get("triggers") else 0)
        have = card.get("resources") or 0
        later = have + int(rate * gens)
        per = vp.get("per") or (vp["resourcesHere"] if isinstance(vp["resourcesHere"], int) else 1) or 1
        total += (later * vp["each"] - have * vp["each"]) if "each" in vp else later // per - have // per
    return total


def rest_of_game(player, game, gens_left, card_rate):
    """(VP, text): what a player is likely to add before the game ends, from public information.

    Income over the production phases left (TR + M€ production, steel and titanium at 2 and 3 M€) plus what they
    hold pays first for cards in hand (card_rate: (VP, M€ cost) of a card worth playing in the last generation),
    then the cheapest standard project per VP. Plants and heat (stock plus production) become greeneries and
    temperature steps while those are open; cards that collect resources keep scoring."""
    left = _steps_left(game)
    phases = max(0, gens_left - 1)
    plants = player.get("plants", 0) + player.get("plantProduction", 0) * gens_left
    greeneries = int(plants) // 8
    green_vp = greeneries + min(greeneries, left["oxygen"])
    heat = player.get("heat", 0) + (player.get("heatProduction", 0) + player.get("energyProduction", 0)) * gens_left
    heat_vp = min(int(heat) // 8, left["temperature"])
    money = player.get("megacredits", 0) + 2 * player.get("steel", 0) + 3 * player.get("titanium", 0) + phases * (
        player.get("terraformRating", 20) + player.get("megacreditProduction", 0) +
        2 * player.get("steelProduction", 0) + 3 * player.get("titaniumProduction", 0))
    card_vp, cost = card_rate
    hand = player.get("cardsInHandNbr", 0) or 0
    played = min(hand, int(money // (cost or 1))) if card_vp > 0 else 0
    money -= played * cost
    rate = _sp_rate(left)
    sp_vp = int(money // rate) if rate else 0
    res_vp = _resource_growth(player, gens_left)
    total = round(green_vp + heat_vp + played * card_vp + sp_vp + res_vp)
    bits = [f"{greeneries} greenery" if greeneries else "", f"{heat_vp} heat TR" if heat_vp else "",
            f"{played} of {hand} hand cards ~{played * card_vp:.0f}" if played else "",
            f"{sp_vp} from standard projects" if sp_vp else "", f"{res_vp} card resources" if res_vp else ""]
    return total, ", ".join(b for b in bits if b)


def report(view, logs=None, gens_left=None, card_rate=None):
    """Lines for `tfm vp`: every player's estimate, best first, with your exact score from the app, and (with
    gens_left) what each is likely to add by the end of the game."""
    me = view["thisPlayer"]
    colony_vp = colony_vp_from_logs(logs) if logs is not None else None
    rows = []
    for p in view["players"]:
        parts, unknown = estimate(view, p, colony_vp)
        if p["color"] == me["color"]:
            exact = me["victoryPointsBreakdown"]
            parts = {k: exact[k] for k in parts}  # the app's own numbers for you
            unknown = []
        rows.append((p, parts, unknown))
    rows.sort(key=lambda r: -r[1]["total"])
    out = ["VP IF THE GAME ENDED NOW (awards by current standings; rivals estimated from public info):"]
    for p, v, unknown in rows:
        who = "YOU" if p["color"] == me["color"] else p["name"]
        approx = "" if p["color"] == me["color"] else "~"
        line = (f"  {who}: {approx}{v['total']} = TR {v['terraformRating']} + milestones {v['milestones']} + "
                f"awards {v['awards']} + greenery {v['greenery']} + city {v['city']} + cards {approx}{v['victoryPoints']}")
        if unknown:
            line += f" (+? from {', '.join(unknown)}: run `tfm card`)"
        if gens_left is not None:
            more, why = rest_of_game(p, view["game"], gens_left, card_rate or (0, 1))
            line += f" · by game end ~{v['total'] + more} (+{more}" + (f": {why})" if why else ")")
        out.append(line)
    if gens_left is not None:
        out.append(f"  By game end: {gens_left} generation(s) left incl. this one; hand cards at ~{(card_rate or (0, 1))[0]:.1f} "
                   f"VP for {(card_rate or (0, 1))[1]:.0f} M€ each while money lasts; award swings not included.")
    out.append("  Not counted for rivals: Moon VP and the time penalty" +
               ("" if logs is not None else ", colony trade VP (no log read)") + ".")
    return out


def held_card_rate(view):
    """(VP, M€) of an average card worth playing in the last generation, from the deck: the cards value.py prices
    above zero with one generation left, as VP (5 M€ each) and printed cost."""
    from . import deck, value
    ctx = value.Outlook(view, gens_left=1)
    vals = [value.value_card(n, ctx, nested=True) for n in deck.names(ctx.expansions)]
    good = [v for v in vals if v.value > 0]
    if not good:
        return 0.0, 1.0
    return sum(v.value for v in good) / len(good) / value.VP, sum(v.cost for v in good) / len(good)
