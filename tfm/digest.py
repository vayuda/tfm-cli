"""Compact text summary of a player's view of the game."""
import time

from . import data
from .text import card_line, message

RES = [("megacredits", "MC"), ("steel", "St"), ("titanium", "Ti"), ("plants", "Pl"), ("energy", "En"), ("heat", "He")]
PROD = {"megacredits": "megacreditProduction", "steel": "steelProduction", "titanium": "titaniumProduction",
        "plants": "plantProduction", "energy": "energyProduction", "heat": "heatProduction"}


def resources(p):
    return "  ".join(f"{label} {p[k]}({p[PROD[k]]:+d})" for k, label in RES)


def tags(p):
    return ", ".join(f"{k} {v}" for k, v in sorted(p["tags"].items(), key=lambda kv: -kv[1]) if v) or "none"


def corporation(p):
    for card in p["tableau"]:
        info = data.card(card["name"])
        if info and info["type"] == "corporation":
            return card["name"]
    return "?"


def fmt_minutes(minutes):
    sign = "-" if minutes < 0 else ""
    total = int(abs(minutes) * 60)
    return f"{sign}{total // 60}:{total % 60:02d}"


def clock(p, options):
    """Elapsed time and Escape Velocity penalty projection, matching the server's formula."""
    timer = p["timer"]
    # The app does not time a player's first action of the game.
    running = timer["running"] and timer.get("afterFirstAction", True)
    elapsed_ms = timer["sumElapsed"] + (time.time() * 1000 - timer["startedAt"] if running else 0)
    elapsed = elapsed_ms / 60000
    ev = options.get("escapeVelocity")
    if not ev:
        return f"clock {fmt_minutes(elapsed)}"
    allowance = ev["thresholdMinutes"] + p["actionsTakenThisGame"] * ev["bonusSectionsPerAction"] / 60
    over = elapsed - allowance
    if over <= 0:
        return f"clock {fmt_minutes(elapsed)} of {fmt_minutes(allowance)} allowance ({fmt_minutes(-over)} left before the time penalty)"
    period = ev["penaltyPeriodMinutes"]
    penalty = ev["penaltyVPPerPeriod"] * int(over // period)
    to_next = period - (over % period)
    return f"clock {fmt_minutes(elapsed)}, {fmt_minutes(over)} over allowance: penalty now -{penalty} VP (next -{ev['penaltyVPPerPeriod']} in {fmt_minutes(to_next)})"


def globals_line(g):
    parts = [
        f"temp {g['temperature']}°C/+8 ({max(0, (8 - g['temperature']) // 2)} steps left)",
        f"O2 {g['oxygenLevel']}%/14 ({max(0, 14 - g['oxygenLevel'])} left)",
        f"oceans {g['oceans']}/9 ({max(0, 9 - g['oceans'])} left)",
    ]
    if g["gameOptions"]["expansions"].get("venus"):
        parts.append(f"Venus {g['venusScaleLevel']}%/30")
    return " · ".join(parts)


def milestones(g, me, names, ctx):
    ms = g["milestones"]
    claimed = [m for m in ms if m.get("playerName")]
    lines = [f"MILESTONES ({len(claimed)}/3 claimed, cost 8 MC):"]
    for m in ms:
        scores = {s["color"]: s for s in m["scores"]}
        mine = scores.get(me["color"], {})
        rivals = sorted((s for c, s in scores.items() if c != me["color"]), key=lambda s: -s["score"])
        best = rivals[0] if rivals else None
        desc = data.milestone_text(m["name"]) if ctx.show_text("ms:" + m["name"]) else None
        line = f"  {m['name']}"
        if desc:
            line += f" ({desc})"
        if m.get("playerName"):
            line += f": CLAIMED by {m['playerName']}"
        else:
            line += f": you {mine.get('score', '?')}"
            if best:
                line += f", best rival {names.get(best['color'], best['color'])} {best['score']}"
            claimable = [names.get(c, c) for c, s in scores.items() if s.get("claimable")]
            if mine.get("claimable"):
                line += "  <<< YOU CAN CLAIM"
            elif claimable:
                line += f"  (claimable by {', '.join(claimable)})"
        lines.append(line)
    return lines


def awards(g, me, names, ctx):
    aw = g["awards"]
    funded = [a for a in aw if a.get("playerName")]
    cost = [8, 14, 20][len(funded)] if len(funded) < 3 else None
    head = f"AWARDS ({len(funded)}/3 funded" + (f", next costs {cost} MC):" if cost else "):")
    lines = [head]
    for a in aw:
        if len(funded) == 3 and not a.get("playerName"):
            continue
        ranked = sorted(a["scores"], key=lambda s: -s["score"])
        desc = data.award_text(a["name"]) if ctx.show_text("aw:" + a["name"]) else None
        line = f"  {a['name']}" + (f" ({desc})" if desc else "")
        line += f" [funded by {a['playerName']}]" if a.get("playerName") else " [not funded]"
        line += ": " + ", ".join(
            ("YOU" if s["color"] == me["color"] else names.get(s["color"], s["color"])) + f" {s['score']}" for s in ranked)
        lines.append(line)
    return lines


def colonies(g, view, names, ctx=None):
    active = [c for c in g["colonies"] if c.get("isActive", True)]
    if not active:
        return []
    me = view["thisPlayer"]
    lines = [f"COLONIES (your fleets {me['fleetSize']}, trades used {me['tradesThisGeneration']}; you own {me['coloniesCount']}):"]
    for c in active:
        info = data.colony(c["name"]) or {}
        qty = info.get("tradeQty")
        pos = c.get("trackPosition", 0)
        now = qty[pos] if isinstance(qty, list) and pos < len(qty) else "?"
        owners = ", ".join(names.get(x, x) for x in c["colonies"]) or "-"
        trade = data.colony_trade(c["name"]) if ctx is None or ctx.show_text("col:" + c["name"]) else "…"
        line = f"  {c['name']}: trade {trade} (n={now} now) · colonies {owners} ({len(c['colonies'])}/3)"
        if c.get("visitor"):
            line += f" · fleet: {names.get(c['visitor'], c['visitor'])}"
        lines.append(line)
    return lines


def tableau(p):
    out = []
    for card in p["tableau"]:
        info = data.card(card["name"]) or {}
        if info.get("type") == "corporation":
            continue
        s = card["name"]
        if card.get("resources"):
            s += f"[{card['resources']}]"
        out.append(s)
    return ", ".join(out) or "none"


def sections(view, ctx, new_logs):
    """The summary as ordered chunks (key, always, lines).

    Chunks sharing a key form one section. Sections with always=True are printed on every turn;
    the others can be left out of a turn when unchanged since the seat's last turn (see changes_only).
    """
    g = view["game"]
    me = view["thisPlayer"]
    names = ctx.names
    opts = g["gameOptions"]
    out = []

    def add(key, always, *lines):
        out.append((key, always, list(lines)))

    head = [f"GAME {g['name']} · generation {g['generation']} · phase {g['phase']} · map {opts['boardName']}"
            f" · {len(view['players'])} players", "GLOBALS: " + globals_line(g)]
    if g.get("pathfinders"):
        tracks = ", ".join(f"{k} {v}" for k, v in g["pathfinders"].items() if v is not None and v >= 0)
        head.append(f"PLANETARY TRACKS: {tracks}")
    add("game", True, *head)
    vp = me["victoryPointsBreakdown"]
    add("", True, "")
    add("you", True,
        f"YOU: {me['name']} ({me['color']}) · {corporation(me)} · TR {me['terraformRating']} · "
        f"VP if the game ended now {vp['total']} (TR {vp['terraformRating']}, milestones {vp['milestones']}, "
        f"awards {vp['awards']} by current standings, "
        f"greenery {vp['greenery']}, city {vp['city']}, cards {vp['victoryPoints']})",
        f"  {resources(me)}  · steel={me['steelValue']} ti={me['titaniumValue']}")
    add("you.tags", False, f"  tags: {tags(me)}")
    add("you.tiles", False, f"  tiles: {me['citiesCount']} cities · colonies {me['coloniesCount']} · "
                            f"corruption {me.get('underworldData', {}).get('corruption', 0)}")
    add("you.played", False, f"  played: {tableau(me)}")
    if me["actionsThisGeneration"]:
        add("you.actions", False, f"  card actions used this generation: {', '.join(me['actionsThisGeneration'])}")
    add("you.clock", True, f"  {clock(me, opts)} · actions taken {me['actionsTakenThisGame']}")
    hand = view.get("cardsInHand", [])
    add("hand", False, f"  HAND ({len(hand)}):",
        *("    " + card_line(c, with_text=ctx.show_text(c["name"])) for c in hand))
    for label, key in (("PRELUDES IN HAND", "preludeCardsInHand"), ("DRAFTED", "draftedCards")):
        cards = view.get(key) or []
        if cards:
            add(key, False, f"  {label}: " + "; ".join(card_line(c, with_text=ctx.show_text(c["name"])) for c in cards))

    add("", True, "")
    add("opps", True, "OPPONENTS:")
    passed = set(g.get("passedPlayers", []))
    for p in view["players"]:
        if p["color"] == me["color"]:
            continue
        flag = " PASSED" if p["color"] in passed else (" (acting)" if p.get("isActive") else "")
        add(f"opp.{p['name']}", True,
            f"  {p['name']} ({p['color']}) · {corporation(p)} · TR {p['terraformRating']} · hand {p['cardsInHandNbr']}"
            f" · cities {p['citiesCount']} · colonies {p['coloniesCount']}{flag}",
            f"    {resources(p)}")
        add(f"opp.{p['name']}.tags", False, f"    tags: {tags(p)}")
        add(f"opp.{p['name']}.played", False, f"    played: {tableau(p)}")

    add("", True, "")
    add("milestones", False, *milestones(g, me, names, ctx))
    add("awards", False, *awards(g, me, names, ctx))
    col = colonies(g, view, names, ctx if ONCE_COLONY_TEXT else None)
    if col:
        add("colonies", False, *col)

    if new_logs:
        add("log", True, "", "SINCE YOUR LAST LOOK:", *("  " + message(entry, names) for entry in new_logs[-60:]))
    return out


SECTION_NAMES = {"you.tags": "your tags", "you.tiles": "your tiles", "you.played": "your played cards",
                 "you.actions": "your card actions used", "hand": "your hand",
                 "preludeCardsInHand": "your preludes", "draftedCards": "your drafted cards"}


ONCE_COLONY_TEXT = False  # print each colony's trade text only the first time, like card text


def section_name(key):
    if key.startswith("opp."):
        _, rest = key.split(".", 1)
        player, _, part = rest.rpartition(".")
        return f"{player}'s {part}"
    return SECTION_NAMES.get(key, key)


def summarize(view, ctx, new_logs):
    return [line for _key, _always, lines in sections(view, ctx, new_logs) for line in lines]


def section_hashes(chunks):
    import hashlib
    texts = {}
    for key, always, lines in chunks:
        if not always:
            texts[key] = texts.get(key, "") + "\n".join(lines) + "\n"
    return {k: hashlib.sha1(v.encode()).hexdigest()[:10] for k, v in texts.items()}


LINE_LEVEL = ("milestones", "awards", "colonies")


def _line_key(line):
    """The name a milestone/award/colony line is about (text before its description or colon)."""
    return line.strip().split(" (")[0].split(":")[0].split(" [")[0]


def changes_only(chunks, shown, by_line=False):
    """Lines for a turn that repeats only what changed since `shown` (from the seat's last turn).

    Sections are compared whole; with by_line, milestones, awards and colonies are compared line by line,
    printing their header plus the changed lines.
    """
    import hashlib

    def h(text):
        return hashlib.sha1(text.encode()).hexdigest()[:10]

    now = section_hashes(chunks)
    if by_line:
        for key, _always, lines in chunks:
            if key in LINE_LEVEL:
                for line in lines[1:]:
                    now[f"{key}/{_line_key(line)}"] = h(line)
    keep = {k for k, v in now.items() if shown.get(k) != v}
    lines, skipped = [], []
    for key, always, chunk in chunks:
        if always or (key in keep and not (by_line and key in LINE_LEVEL)):
            lines.extend(chunk)
        elif by_line and key in LINE_LEVEL and key in keep:
            changed = [ln for ln in chunk[1:] if f"{key}/{_line_key(ln)}" in keep]
            omitted = len(chunk) - 1 - len(changed)
            lines.append(chunk[0] + (f" (only changed lines; {omitted} unchanged not repeated)" if omitted else ""))
            lines.extend(changed)
        elif key not in skipped:
            skipped.append(key)
    if skipped:
        note = "UNCHANGED since your last turn, so not repeated: " + ", ".join(section_name(k) for k in skipped)
        idx = next((i for i, ln in enumerate(lines) if ln == "SINCE YOUR LAST LOOK:"), len(lines))
        if idx > 0 and lines[idx - 1] == "":
            idx -= 1
        lines[idx:idx] = ["", note]
    return lines, now


def final_scores(view):
    lines = ["GAME OVER. Final scores:"]
    for p in sorted(view["players"], key=lambda p: -p["victoryPointsBreakdown"]["total"]):
        vp = p["victoryPointsBreakdown"]
        lines.append(f"  {p['name']}: {vp['total']} (TR {vp['terraformRating']}, milestones {vp['milestones']}, awards {vp['awards']},"
                     f" greenery {vp['greenery']}, city {vp['city']}, cards {vp['victoryPoints']}, time {vp['escapeVelocity']})")
    return lines
