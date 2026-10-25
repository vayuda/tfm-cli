"""Rough card valuation for decision making: `tfm value`.

Two layers:

1. A **baseline number**. Every effect a card declares (production, resources, TR, global steps, tiles,
   cards, VP) is priced in M€ (1 VP = 5 M€; 1 TR = 5 M€ + 1 M€ per remaining spendable production
   phase; see docs/STRATEGY.md §6), with production counted only for the production phases the game is likely to
   have left. Compare it with what you pay (play cost, plus 3 M€ to buy a card you don't own yet).
2. **Context checks** on the live game. The card's steps, tiles and tags run through one event queue
   (_Sim), so chains are followed: global-parameter bonus thresholds (oxygen 8% → temperature → 0°C ocean),
   Pathfinders planetary-track rewards (for you and for everyone, including rewards that raise a
   parameter), the last step of a parameter, and what your own cards' triggers pay (OWN_TRIGGERS). Also:
   milestones it would let you claim, award positions it changes for you and for rivals, requirements it
   meets for cards in your hand, rivals' cards that pay out when you do this, and attacks a rival can block.
3. **Combos with the hand** (planned tableau). Hand cards count as future plays, each with a chance of being
   played before the end. Effects that depend on future plays (this card's OWN_TRIGGERS effect, discounts,
   VP per tag) are priced from those chances, hand effect cards this card would fire get partial credit,
   and requirements it meets for hand cards are priced by how much earlier they can be played. All of it
   is the card's marginal value to the planned set, printed on one `combo` line.

The numbers are estimates for comparing options, not exact values. Effects the app implements with
custom code are flagged so you read the card text yourself.
"""
import copy
import math
import random
import re
from collections import deque

from . import data, deck

VP, CARD, BUY_COST = 5.0, 3.5, 3
CITY_TILE, SPECIAL_TILE, COLONY = 5.0, 2.0, 10.0
OCEAN_ADJACENCY = 2.0   # typical ocean-adjacency income on top of the ocean's TR
PLANETARY = ("venus", "earth", "mars", "jovian", "moon")
RESOURCES = ("megacredits", "steel", "titanium", "plants", "energy", "heat")
_PROD_KEY = {"megacredits": "megacreditProduction", "steel": "steelProduction", "titanium": "titaniumProduction",
             "plants": "plantProduction", "energy": "energyProduction", "heat": "heatProduction"}
_STOCK_KEY = {r: r for r in RESOURCES}  # the app's PlayerModel: megacredits, steel, ...

# Underworld token bag (normal game, from the app's UnderworldExpansion.TOKEN_COUNTS).
TOKEN_COUNTS = {
    "nothing": 5, "corruption1": 13, "corruption2": 2, "card1": 4, "card2": 1, "mcprod1pertemp": 2,
    "steel2plant": 2, "steel1production": 4, "steel2pertemp": 3, "titaniumandplant": 3, "titanium2": 3,
    "titanium1production": 1, "titanium1pertemp": 3, "plant2": 4, "plant3": 1, "plant1production": 4,
    "plant2pertemp": 3, "energy2": 2, "energy1production": 5, "heat2production": 3, "microbe2": 4,
    "microbe2pertemp": 1, "tr": 2, "ocean": 2, "sciencetag": 2, "planttag": 2, "anyresource1": 2, "place6mc": 2,
    "oceanrequirementmod": 2, "oxygenrequirementmod": 2, "temprequirementmod": 2,
}

# Standard projects and actions are implemented in code in the app, so their effects are spelled out here.
STANDARD_BEHAVIORS = {
    "Aquifer": {"ocean": {}},
    "City": {"city": {}, "production": {"megacredits": 1}},
    "Power Plant:SP": {"production": {"energy": 1}},
    "Greenery": {"greenery": {}},
    "Asteroid:SP": {"global": {"temperature": 1}},
    "Air Scrapping": {"global": {"venus": 1}},
    "Air Scrapping (Var)": {"global": {"venus": 1}},
    "Build Colony": {"colonies": {"buildColony": {}}},
    "Buffer Gas": {"tr": 1},
    "Excavate:SP": {"underworld": {"excavate": 1}},
    # Conversions spend plants/heat that have no better use, so only the gain is priced.
    "Convert Plants": {"greenery": {}},
    "Convert Heat": {"global": {"temperature": 1}},
}

# Fixed-cost standard projects, cheapest first, for the "a standard project does this for less" check.
SP_COSTS = {"Power Plant:SP": 11, "Asteroid:SP": 14, "Aquifer": 18, "Greenery": 23, "City": 25}

# Cards whose TR depends on your tags are custom code in the app, so their count is spelled out here.
TR_PER_TAG = {
    "Terraforming Ganymede": {"tag": "jovian"},
    "Social Events": {"tag": "mars", "per": 2},
}

# Underground tokens that depict steel or titanium (Deepmining may only excavate these).
STEEL_TOKENS = ("steel2plant", "steel1production", "steel2pertemp")
TITANIUM_TOKENS = ("titaniumandplant", "titanium2", "titanium1production", "titanium1pertemp")


def _raise_to_one(ctx):
    """Industrial Complex: every production below 1 goes up to 1, from the player's production now."""
    if ctx is None:
        return {}
    out = {}
    for res in ("megacredits", "steel", "titanium", "plants", "energy", "heat"):
        have = ctx.me.get(_PROD_KEY[res], 0) or 0
        if have < 1:
            out[res] = 1 - have
    return out


def _best_building_box(ctx):
    """Robotic Workforce: the production box of your best building card (one whose result keeps every
    production legal)."""
    if ctx is None:
        return {}
    best, best_value = {}, 0.0
    for name in ctx.tableau:
        if name == "Robotic Workforce" or "building" not in (data.card(name) or {}).get("tags", []):
            continue
        box = (card_behavior(name, ctx).get("behavior") or {}).get("production") or {}
        box = {r: n for r, n in box.items() if isinstance(n, (int, float))}
        if not box or any((ctx.me.get(_PROD_KEY.get(r, ""), 0) or 0) + n < (-5 if r == "megacredits" else 0)
                          for r, n in box.items()):
            continue
        worth = sum(n * _unit(r, ctx) * (ctx.plant_phases if r == "plants" else ctx.phases) for r, n in box.items())
        if worth > best_value:
            best, best_value = box, worth
    return best


def _oumuamua(ctx):
    """Oumuamua Type Object Survey: 2 data anywhere; reveal 2 cards. The first science or microbe card is
    played free; failing that, a space card gives 3 energy production. Cards not played are kept."""
    cards = deck.deck((ctx.game["gameOptions"].get("expansions") if ctx else None) or {})
    n = len(cards) or 1
    sci = sum(1 for c in cards if {"science", "microbe"} & set(c.get("tags") or [])) / n
    space = sum(1 for c in cards if "space" in (c.get("tags") or []) and
                not {"science", "microbe"} & set(c.get("tags") or [])) / n
    other = 1 - sci - space
    free = sci + other * sci
    energy = space + other * space
    return {"behavior": {"addResourcesToAnyCard": {"type": "Data", "count": 2}, "drawCard": round(2 - free, 2),
                         "freePlay": round(free, 2), "production": {"energy": round(3 * energy, 2)}}}


# Effects the app implements in custom code (bespokePlay / bespokeAction), spelled out from the card text.
# Each entry is merged over the exported behavior (a key here replaces the same key there); a callable gets
# the Outlook (or None when there is none) and returns the entry.
CUSTOM_BEHAVIORS = {
    "Noctis City": {"behavior": {"production": {"energy": -1, "megacredits": 3}, "city": {}}},
    "Lava Tube Settlement": {"behavior": {"city": {}}},
    "Small Open Pit Mine": {"behavior": {"or": {"behaviors": [{"production": {"steel": 2}},
                                                              {"production": {"titanium": 1}}]}}},
    "Deepmining": {"behavior": {"or": {"behaviors": [
        {"production": {"steel": 1}, "underworld": {"excavate": {"count": 1, "tokens": STEEL_TOKENS}}},
        {"production": {"titanium": 1}, "underworld": {"excavate": {"count": 1, "tokens": TITANIUM_TOKENS}}}]}}},
    # +5 TR now; skipping the first TR of every later action phase is priced by the skipFirstTR key.
    "Preservation Program": {"behavior": {"tr": 5, "skipFirstTR": True}},
    # Its own 2 microbe tags add 2 diseases at the start: lose 4 M€ each.
    "Pharmacy Union": {"behavior": {"drawCard": {"count": 1, "tag": "science"}, "stock": {"megacredits": -8}}},
    "Shipment to Earth": {"behavior": {"tr": 3, "stock": {"plants": -3, "steel": -3,
                                                          "megacredits": {"tag": "earth", "each": 2}}}},
    "Imported Hydrogen": {"behavior": {"ocean": {}, "or": {"behaviors": [
        {"stock": {"plants": 3}, "title": "3 plants"},
        {"addResourcesToAnyCard": {"type": "Microbe", "count": 3, "excludeThis": True}, "title": "3 microbes"},
        {"addResourcesToAnyCard": {"type": "Animal", "count": 2, "excludeThis": True}, "title": "2 animals"}]}}},
    "Potatoes": {"behavior": {"production": {"megacredits": 2}, "stock": {"plants": -2}}},
    "Industrial Complex": lambda ctx: {"behavior": {"production": _raise_to_one(ctx)}},
    "Robotic Workforce": lambda ctx: {"behavior": {"production": _best_building_box(ctx)}},
    # One planet tag of your choice (not modelled) and 3 steps on that planetary track.
    "Kickstarter": {"behavior": {"planetaryTrack": 3}},
    "Oumuamua Type Object Survey": _oumuamua,
    # Returning a played event is about as good as drawing a card you chose.
    "Astra Mechanica": lambda ctx: {"behavior": {"drawCard": min(2, (ctx.tags.get("event", 0) if ctx else 0))}},
    # Each action puts 2 resources on a held card or doubles them: about 2 M€ off a later card per use.
    "Self-replicating Robots": {"action": {"stock": {"megacredits": 2}}},
    "Vital Colony": {"behavior": {"colonies": {"buildColony": {"doubleBonus": True}}}},
    "Double Down": lambda ctx: _double_down(ctx),
    "Atmoscoop": {"behavior": {"or": {"behaviors": [{"global": {"temperature": 2}, "title": "temperature +2"},
                                                     {"global": {"venus": 2}, "title": "Venus +2"}]}}},
    "Museum of Early Colonisation": {"behavior": {"production": {"energy": -1, "steel": 1, "titanium": 1, "plants": 1}}},
    # Discarding a card you will not play pays 2 M€ (Sell Patents pays 1): spread over the generations left.
    "Aridor": {},  # its effect is priced in _plan (_aridor) and _own_triggers
    "Ceres Tech Market": lambda ctx: {"action": {"stock": {"megacredits": round(2 * _unplayed_per_gen(ctx), 2)}}},
}


def _unplayed_per_gen(ctx):
    """Hand cards per generation left that are unlikely to be played (expected count, at least 1 per game)."""
    if ctx is None:
        return 0.0
    unplayed = sum(1 - pc["p"] for pc in planned_tableau(ctx))
    return max(1.0, unplayed) / max(1, ctx.gens_left)


def _double_down(ctx):
    """Double Down copies your other prelude's direct effect (its play effect and M€, not ongoing effects)."""
    best, best_value = {}, None
    for other in (ctx.preludes if ctx else []):
        name = data.card_name(other) or other
        if name == "Double Down" or (data.card(name) or {}).get("type") != "prelude":
            continue
        beh = dict(card_behavior(name, ctx).get("behavior") or {})
        mc = (data.card(name) or {}).get("startingMC") or 0
        if mc and "megacredits" not in (beh.get("stock") or {}):
            beh["stock"] = dict(beh.get("stock") or {}, megacredits=mc)
        trial = Valuation(name)
        _apply(beh, trial, ctx, [], name)
        if best_value is None or trial.value > best_value:
            best, best_value = dict(beh, copied=name), trial.value
    return {"behavior": best}


def starting_mc(name, beh=None):
    """A corporation's starting M€, or a prelude's M€ when its behavior does not already grant it as stock."""
    info = data.card(name) or {}
    if info.get("type") not in ("corporation", "prelude") or not info.get("startingMC"):
        return 0
    beh = beh if beh is not None else card_behavior(name)
    if info["type"] == "prelude" and "megacredits" in ((beh.get("behavior") or {}).get("stock") or {}):
        return 0
    return info["startingMC"]


def card_behavior(name, ctx=None):
    """Behavior data for a card (as in behaviors.json) with CUSTOM_BEHAVIORS merged over it, standard
    projects included. A merged entry sets "custom": the card text is modelled, so no manual note."""
    beh = data.behavior(name) or ({"behavior": STANDARD_BEHAVIORS[name]} if name in STANDARD_BEHAVIORS else {})
    custom = CUSTOM_BEHAVIORS.get(data.card_name(name) or name)
    if custom is None:
        return beh
    custom = custom(ctx) if callable(custom) else custom
    out = dict(beh, custom=True)
    for key, part in custom.items():
        out[key] = dict(beh.get(key) or {}, **part) if isinstance(part, dict) and key in ("behavior", "firstAction") \
            else part
    return out


# Well-known cards that pay their owner (or the acting player) when ANY player does something.
ANY_PLAYER_TRIGGERS = {
    "Aphrodite": ("venus", "its owner gains 2 M€ per Venus step"),
    "Exploitation Of Venus": ("venus", "the player raising Venus gains 2 M€ per step"),
    "Poseidon": ("colony", "its owner gains 1 M€ production per colony"),
    "Botanical Experience": ("greenery", "its owner gains 1 data per greenery (3 data = +1 plant production)"),
    "Tharsis Republic": ("city", "its owner gains 1 M€ production per city on Mars"),
    "Rover Construction": ("city", "its owner gains 2 M€ per city"),
    "Pets": ("city", "its owner adds an animal (1/2 VP) per city"),
    "Immigrant City": ("city", "its owner gains 1 M€ production per city"),
    "Arctic Algae": ("ocean", "its owner gains 2 plants per ocean"),
    "Saturn Systems": ("jovian", "its owner gains 1 M€ production per Jovian tag"),
    "Splice": ("microbe", "its owner gains 2 M€ per microbe tag"),
}


# --------------------------------------------------------------------------------------------- outlook

def offered_cards(view):
    """Corporations, preludes and project cards on offer in the opening decision."""
    found = {}

    def walk(node):
        if isinstance(node, dict):
            for c in node.get("cards") or []:
                if isinstance(c, dict) and c.get("name"):
                    found.setdefault(c["name"], None)
            for key in ("options", "andOptions"):
                for child in node.get(key) or []:
                    walk(child)
    walk(view.get("waitingFor"))
    for key in ("dealtCorporationCards", "dealtPreludeCards", "dealtProjectCards"):
        for c in view.get(key) or []:
            found.setdefault(c["name"], None)
    out = {"corporation": [], "prelude": [], "project": []}
    for name in found:
        t = (data.card(name) or {}).get("type")
        out["corporation" if t == "corporation" else "prelude" if t == "prelude" else "project"].append(name)
    return out


class Outlook:
    """What the current game state says about time left and what resources are worth."""

    def __init__(self, view, gens_left=None):
        g = view["game"]
        self.view = view
        self.game = g
        self.me = view["thisPlayer"]
        self.players = view["players"]
        self.rivals = [p for p in self.players if p["color"] != self.me["color"]]
        self.names = {p["color"]: p["name"] for p in self.players}
        opts = g["gameOptions"]
        self.expansions = opts.get("expansions", {})
        self.alt_venus = bool(opts.get("altVenusBoard"))
        self.venus_counts = bool(self.expansions.get("venus")) and bool(opts.get("requiresVenusTrackCompletion"))
        self.generation = g["generation"]
        self.temp, self.oxygen, self.oceans = g["temperature"], g["oxygenLevel"], g["oceans"]
        self.venus = g.get("venusScaleLevel", 0) or 0
        # Maximums and track bonuses depend on the map (Amazonis Planitia's tracks are longer).
        self.board = opts.get("boardName", "")
        self.globals = data.map_globals(self.board)
        self.left = {
            "temperature": data.steps_left("temperature", self.temp, self.board),
            "oxygen": data.steps_left("oxygen", self.oxygen, self.board),
            "oceans": data.steps_left("oceans", self.oceans, self.board),
            "venus": data.steps_left("venus", self.venus, self.board) if self.expansions.get("venus") else 0,
        }
        remaining = self.left["temperature"] + self.left["oxygen"] + self.left["oceans"] + \
            (self.left["venus"] if self.venus_counts else 0)
        self.pace = self._pace(g.get("globalsPerGeneration") or [])
        by_pace = 1 if remaining == 0 else max(1, math.ceil(remaining / self.pace))
        # Early pace understates how fast tables finish (terraforming accelerates), so also cap the
        # estimate at a typical game length for this player count and setup. Our 4-player games with
        # Prelude ended in generation 8 and our 5-player ones in generation 7.
        typical = {1: 14, 2: 12, 3: 11, 4: 9}.get(len(self.players), 8) - (1 if self.expansions.get("prelude") else 0)
        # Longer tracks mean a longer game: scale by this map's terraforming steps against the standard 42.
        steps = sum(data.total_steps(p, self.board) for p in ("temperature", "oxygen", "oceans"))
        typical = round(typical * steps / 42)
        by_length = max(1, typical - self.generation + 1)
        self.gens_left = gens_left if gens_left is not None else min(by_pace, by_length)
        self.by_pace, self.by_length = by_pace, by_length
        # Production at the end of each remaining generation except the last can still be spent.
        self.phases = max(0, self.gens_left - 1)
        # A TR is 1 VP at the end plus 1 M€ of income at every remaining spendable production phase.
        self.tr = VP + self.phases
        self.plant_phases = self.gens_left  # final plants still become a greenery
        self.remaining_steps = remaining
        # 8 heat = 1 temperature TR; 8 plants = a greenery (1 VP, plus 1 oxygen TR while oxygen is open).
        # Ambient keeps turning 8 heat into 1 TR after temperature is maxed.
        ambient = self.ambient = any(c["name"] == "Ambient" for c in self.me.get("tableau") or [])
        self.heat = self.tr / 8 if self.left["temperature"] or ambient else 0.0
        self.plant = (VP + self.tr) / 8 if self.left["oxygen"] else VP / 8
        self.steel = float(self.me.get("steelValue", 2))
        self.titanium = float(self.me.get("titaniumValue", 3))
        self.energy = 1.0
        self.tags = dict(self.me.get("tags") or {})
        self.hand = view.get("cardsInHand") or []
        self.tableau = {c["name"]: c for c in self.me.get("tableau", [])}
        # Helion spends heat as M€, so heat is worth at least 1 M€ to it.
        self.helion = "Helion" in self.tableau
        if self.helion:
            self.heat = max(self.heat, 1.0)
        # The opening decision: the offered corporations, preludes and project cards form the package.
        self.offered = offered_cards(view) if not self.tableau and view.get("dealtCorporationCards") else None
        # Project cards the player holds or keeps (the opening package's kept cards in tfm plan), for effects that
        # play a card from hand (Eccentric Sponsor, Valuable Gases).
        self.package_hand = list(view.get("_package_hand") or [c["name"] for c in self.hand] or
                                 (self.offered or {}).get("project", []))
        self.preludes = list(view.get("_package_preludes") or
                             [c["name"] for c in view.get("preludeCardsInHand") or []] or
                             (self.offered or {}).get("prelude", []))
        self.my_greeneries = view["_greeneries"] if "_greeneries" in view else self.greeneries(self.me["color"])

    def greeneries(self, color=None):
        return sum(1 for s in self.game.get("spaces", []) if s.get("tileType") is not None
                   and data.tile_name(s["tileType"]) == "greenery" and (color is None or s.get("color") == color))

    def _pace(self, history):
        def steps(e):
            s = data.param_steps("temperature", e.get("temperature", -30)) + e.get("oxygen", 0) + e.get("oceans", 0)
            if self.venus_counts:
                s += data.param_steps("venus", e.get("venus", 0))
            return s
        if len(history) >= 2:
            recent = history[-3:]
            per_gen = (steps(recent[-1]) - steps(recent[0])) / (len(recent) - 1)
            if per_gen > 0:
                return per_gen
        return 5.0  # typical 4-player pace when there is no history yet

    def summary(self):
        return (f"OUTLOOK: generation {self.generation}, about {self.gens_left} generation(s) left including this one "
                f"({self.remaining_steps} terraforming steps remain at ~{self.pace:.1f}/generation → {self.by_pace}; "
                f"typical game length → {self.by_length}; override with --gens) → "
                f"{self.phases} spendable production phase(s). Rates: VP {VP:g}, TR {self.tr:g}, heat {self.heat:g}, "
                f"plant {self.plant:g}, steel {self.steel:g}, Ti {self.titanium:g}, card {CARD:g} M€.")


# --------------------------------------------------------------------------------------------- valuation

class Valuation:
    def __init__(self, name):
        self.name = name
        self.value = 0.0
        self.parts = []      # (label, M€)
        self.flags = []      # context notes
        self.manual = []     # things the numbers don't cover
        self.raises = {"temperature": 0, "oxygen": 0, "venus": 0, "oceans": 0}
        self.tiles = {"city": 0, "greenery": 0, "ocean": 0, "other": 0}
        self.prod = {}       # production change by resource
        self.stock = {}
        self.tr = 0.0
        self.colonies = 0
        self.draws = 0
        self.card_resources = 0
        self.tokens = 0      # underground tokens claimed (Tunneler, Excavator)
        self.attack = False
        self.combo = []      # (label, M€) priced from the planned tableau; printed on one line
        self.simulated = 0.0  # M€ of effects tfm plan applies step by step itself (not banked as a lump)

    def add(self, label, amount):
        if amount:
            self.value += amount
            self.parts.append((label, amount))

    def add_combo(self, label, amount, keep=False):
        if amount or keep:
            self.value += amount
            self.combo.append((label, amount))


def _resolve(count, card_tags, ctx):
    """Evaluate a Countable (a number, or a dict such as {"tag": "jovian", "per": 2})."""
    if count is None:
        return 0
    if isinstance(count, (int, float)):
        return count
    total = 0
    if "tag" in count:
        tags = count["tag"] if isinstance(count["tag"], list) else [count["tag"]]
        if count.get("all") or count.get("others"):
            players = ctx.rivals if count.get("others") else ctx.players
            total += sum((p.get("tags") or {}).get(t, 0) for p in players for t in tags)
        else:
            total += sum(ctx.tags.get(t, 0) for t in tags) + ctx.tags.get("wild", 0) + sum(card_tags.count(t) for t in tags)
    # As in the app, cities and greeneries count every player's tiles unless `all` is false.
    if "cities" in count:
        total += ctx.me["citiesCount"] if count.get("all") is False else sum(p["citiesCount"] for p in ctx.players)
    if "greeneries" in count:
        total += ctx.greeneries(None if count.get("all") is not False else ctx.me["color"])
    if "oceans" in count:
        total += ctx.oceans
    if "colonies" in count:
        total += sum(p["coloniesCount"] for p in ctx.players) if count.get("all") else ctx.me["coloniesCount"]
    total *= count.get("each", 1)
    if count.get("per"):
        total //= count["per"]
    return total


def _unit(res, ctx):
    return {"megacredits": 1.0, "steel": ctx.steel, "titanium": ctx.titanium, "plants": ctx.plant,
            "energy": ctx.energy, "heat": ctx.heat}.get(res, 1.0)


def _resource_vp_value(card_name, ctx, resource_type=None):
    """M€ value of one resource placed on a card, from that card's VP rule."""
    beh = data.behavior(card_name) or {}
    vp = beh.get("vp")
    if isinstance(vp, dict) and "resourcesHere" in vp:
        per = vp.get("per") or (vp["resourcesHere"] if isinstance(vp["resourcesHere"], int) else 1) or 1
        return VP / per
    return 0.5


def _best_target(ctx, resource_type, exclude=None, this_card=None):
    best, best_name = 0.5, None
    candidates = list(ctx.tableau) + ([this_card] if this_card and this_card not in ctx.tableau else [])
    for name in candidates:
        if name == exclude:
            continue
        info = data.card(name) or {}
        if resource_type and info.get("resource") != resource_type:
            continue
        if not resource_type and not info.get("resource"):
            continue
        v = _resource_vp_value(name, ctx)
        if v > best:
            best, best_name = v, name
    return best, best_name


def best_option(or_beh, ctx, card_tags, card_name):
    """The option of an "or" behavior worth most now (None if it has none)."""
    best, best_value = None, None
    for option in or_beh.get("behaviors", []):
        trial = Valuation(card_name)
        _apply(option, trial, ctx, card_tags, card_name)
        if best_value is None or trial.value > best_value:
            best, best_value = option, trial.value
    return best


def _best_track(ctx, steps):
    """(track, M€) for the planetary track whose next `steps` rewards are worth most to you."""
    best, best_value = None, 0.0
    for track, pos in (ctx.game.get("pathfinders") or {}).items():
        layout = data.planetary_tracks().get(track, [])
        if pos is None or pos < 0 or not layout:
            continue
        worth = sum(_reward_value(r, ctx) for space in layout[pos + 1:pos + 1 + steps]
                    for r in space.get("risingPlayer", []) + space.get("everyone", []))
        if worth > best_value:
            best, best_value = track, worth
    return best, best_value


def _apply(beh, v, ctx, card_tags, card_name, scale=1.0, label=""):
    """Price one Behavior dict into v. `scale` > 1 for repeated actions."""
    if not beh:
        return
    pre = f"{label}" if label else ""
    if "or" in beh:
        option = best_option(beh["or"], ctx, card_tags, card_name)
        if option is not None:
            best = Valuation(card_name)
            _apply(option, best, ctx, card_tags, card_name)
            v.add(pre + "best option" + (f" ({option['title']})" if option.get("title") else ""), best.value * scale)
            v.raises = {k: v.raises[k] + best.raises[k] * scale for k in v.raises}
            v.tiles = {k: v.tiles[k] + best.tiles[k] * scale for k in v.tiles}
            for res, n in best.prod.items():
                v.prod[res] = v.prod.get(res, 0) + n * scale
            for res, n in best.stock.items():
                v.stock[res] = v.stock.get(res, 0) + n * scale
            v.tr += best.tr * scale
            v.tokens += best.tokens * scale
            v.draws += best.draws * scale
            v.card_resources += best.card_resources * scale
    if beh.get("copied"):
        v.flags.append(f"copies {beh['copied']}'s effect")
    if beh.get("skipFirstTR"):
        # The first TR of each action phase is lost, this generation's included: a TR lost g generations from
        # now is worth 1 VP plus the income phases after it.
        g = ctx.gens_left
        v.add(f"{pre}skips your first TR each generation ×{g}", -(g * VP + g * (g - 1) / 2) * scale)
    if beh.get("planetaryTrack") and ctx.game.get("pathfinders"):
        track, worth = _best_track(ctx, beh["planetaryTrack"])
        if track:
            v.add(f"{pre}{track} track +{beh['planetaryTrack']}", worth * scale)
    if beh.get("freePlay"):
        p = beh["freePlay"]
        # A card played free is worth about its printed cost (the break-even reference).
        v.add(f"{pre}free play of a revealed card (p≈{p:.2f})", p * deck.mean_cost(ctx.expansions) * scale)
    for key in ("spend", "lose"):
        if key in beh:
            spend = beh[key]
            if key == "lose":
                spend = spend.get("stock") or spend.get("production") or {}
            for res, amt in spend.items():
                if res in ("canUseSteel", "canUseTitanium"):
                    continue
                n = _resolve(amt, card_tags, ctx)
                if res == "resourcesHere":
                    continue
                cost = n * (_unit(res, ctx) if res in ("megacredits", "steel", "titanium", "plants", "energy", "heat") else 1.0)
                v.add(f"{pre}spend {n:g} {res}", -cost * scale)
    if "production" in beh:
        for res, amt in beh["production"].items():
            n = _resolve(amt, card_tags, ctx)
            phases = ctx.plant_phases if res == "plants" else ctx.phases
            v.prod[res] = v.prod.get(res, 0) + n
            v.add(f"{pre}{n:+g} {res} prod × {phases}", n * _unit(res, ctx) * phases * scale)
    if "stock" in beh:
        for res, amt in beh["stock"].items():
            n = _resolve(amt, card_tags, ctx)
            v.stock[res] = v.stock.get(res, 0) + n
            v.add(f"{pre}{n:g} {res}", n * _unit(res, ctx) * scale)
    if "standardResource" in beh:
        sr = beh["standardResource"]
        n = sr if isinstance(sr, (int, float)) else sr.get("count", 1)
        v.add(f"{pre}{n:g} standard resource", 2.0 * n * scale)
    if "drawCard" in beh:
        dc = beh["drawCard"]
        n = dc if isinstance(dc, (int, float)) else _resolve(dc.get("count", 1), card_tags, ctx)
        keep = n if isinstance(dc, (int, float)) else dc.get("keep", n)
        v.draws += keep * scale
        v.add(f"{pre}draw {keep:g}" + (f" of {n:g}" if keep != n else ""), (keep * CARD + (n - keep) * 0.5) * scale)
    if "tr" in beh:
        n = _resolve(beh["tr"], card_tags, ctx)
        v.tr += n * scale
        v.add(f"{pre}{n:g} TR", n * ctx.tr * scale)
    if "global" in beh:
        for param, steps in beh["global"].items():
            left = ctx.left.get(param, 0)
            gained = max(0, min(steps, left)) if steps > 0 else 0
            v.raises[param] += steps * scale
            v.tr += gained * scale
            v.add(f"{pre}{param} +{steps}" + (" (maxed)" if gained == 0 and steps > 0 else ""), gained * ctx.tr * scale)
    if "ocean" in beh:
        n = beh["ocean"].get("count", 1) if isinstance(beh["ocean"], dict) else 1
        gained = min(n, ctx.left["oceans"])
        v.raises["oceans"] += n * scale
        v.tiles["ocean"] += n * scale
        v.tr += gained * scale
        v.add(f"{pre}{n} ocean" + (" (oceans full)" if gained == 0 else ""), gained * (ctx.tr + OCEAN_ADJACENCY) * scale)
    if "greenery" in beh:
        o2 = 1 if ctx.left["oxygen"] else 0
        v.raises["oxygen"] += 1 * scale
        v.tiles["greenery"] += scale
        v.tr += o2 * scale
        v.add(f"{pre}greenery", (VP + o2 * ctx.tr) * scale)
    if "city" in beh:
        v.tiles["city"] += scale
        v.add(f"{pre}city tile (placement-dependent)", CITY_TILE * scale)
        if beh["city"].get("space"):
            v.flags.append("city goes on a reserved off-map area: it scores no greenery adjacency")
    if "tile" in beh:
        v.tiles["other"] += scale
        v.add(f"{pre}special tile", SPECIAL_TILE * scale)
    if "addResources" in beh:
        n = _resolve(beh["addResources"], card_tags, ctx)
        v.card_resources += n * scale
        v.add(f"{pre}{n:g} resource here", n * _resource_vp_value(card_name, ctx) * scale)
    if "addResourcesToAnyCard" in beh:
        adds = beh["addResourcesToAnyCard"]
        for add in adds if isinstance(adds, list) else [adds]:
            n = _resolve(add.get("count", 1), card_tags, ctx)
            each, target = _best_target(ctx, add.get("type"), exclude=card_name if add.get("excludeThis") else None,
                                        this_card=card_name)
            v.card_resources += n * scale
            v.add(f"{pre}{n:g} {add.get('type', 'resource')} to " + (target or "a card (no scoring target)"), n * each * scale)
    if "removeAnyPlants" in beh or "decreaseAnyProduction" in beh or "removeResourcesFromAnyCard" in beh:
        v.attack = True
    if "colonies" in beh:
        col = beh["colonies"]
        if "buildColony" in col:
            opts = col["buildColony"] if isinstance(col["buildColony"], dict) else {}
            n = opts.get("count", 1)
            builds = colony_builds(ctx, duplicates=bool(opts.get("allowDuplicates")), double=bool(opts.get("doubleBonus")))
            v.colonies += n * scale
            if not builds:
                worth = COLONY * (1.5 if opts.get("doubleBonus") else 1) if not _colony_models(ctx) else 0.0
                v.add(f"{pre}colony" + (" (placement bonus + owner bonuses)" if worth else " (no colony tile has room)"),
                      n * worth * scale)
            else:
                picked = builds[:n] if not opts.get("allowDuplicates") else [builds[0]] * n
                v.add(f"{pre}colon{'ies' if n > 1 else 'y'} (" + "; ".join(t for _, t, _ in picked) + ")",
                      sum(w for w, _, _ in picked) * scale)
                if len(builds) > 1:
                    v.flags.append("colony tiles compared: " + ", ".join(f"{name} ≈{w:.0f}" for w, _, name in builds[:5]))
        if col.get("addTradeFleet"):
            worth, why = fleet_value(ctx, col["addTradeFleet"])
            v.add(f"{pre}+{col['addTradeFleet']} trade fleet ({why})", worth * scale)
    if "underworld" in beh:
        uw = beh["underworld"]
        if "excavate" in uw:
            ex = uw["excavate"]
            n = int(ex if isinstance(ex, (int, float)) else _resolve(ex.get("count", 1), card_tags, ctx))
            only = None if isinstance(ex, (int, float)) else ex.get("tokens")
            # Identified tokens already claimed by earlier uses of the same action are gone (ctx.dug).
            best = [h for h in reachable_tokens(ctx) if only is None or h[0] in only][int(getattr(ctx, "dug", 0)):]
            blind = blind_dig_value(ctx, only)
            known = [t for t, _ in best[:n] if token_value(t, ctx) > blind]
            amount = sum(token_value(t, ctx) for t in known) + (n - len(known)) * blind
            v.tokens += n * scale
            label = f"{pre}excavate {n}" + (f" (best known: {', '.join(known)})" if known else f" (blind ≈{blind:.1f} each)")
            v.add(label, amount * scale)
            if best:
                v.flags.append("identified tokens you can dig next to your tiles: " +
                               ", ".join(f"{t} at {sid} (≈{token_value(t, ctx):.0f})" for t, sid in best[:4]))
        if "identify" in uw:
            ident = uw["identify"]
            count = ident if isinstance(ident, (int, float)) else ident.get("count", 1)
            claim = 0 if isinstance(ident, (int, float)) else ident.get("claim", 0)
            if claim:
                v.tokens += claim * scale
                v.add(f"{pre}identify {count:g}, claim {claim:g} (best of {count:g} ≈{best_of_value(ctx, count):.1f})",
                      claim * best_of_value(ctx, count) * scale)
            else:
                v.add(f"{pre}identify {count:g}", 0.5 * scale)
        if "corruption" in uw:
            n = _resolve(uw["corruption"], card_tags, ctx)
            v.add(f"{pre}{n:g} corruption", n * corruption_value(ctx) * scale)
    if beh.get("titaniumValue"):
        v.manual.append(("raises" if beh["titaniumValue"] > 0 else "lowers") +
                        " your titanium value: count your remaining space cards")
    if beh.get("steelValue"):
        v.manual.append(("raises" if beh["steelValue"] > 0 else "lowers") +
                        " your steel value: count your remaining building cards")
    if beh.get("greeneryDiscount"):
        v.add(f"{pre}greeneries cost 1 plant less", ctx.plant * ctx.plant_phases * 0.5 * scale)
        v.simulated += ctx.plant * ctx.plant_phases * 0.5 * scale  # tfm plan converts at 7 plants itself
    _apply_setup_effects(beh, v, ctx, card_tags, card_name, scale, pre)


def value_card(name, ctx, cost=None, buying=False, model=None, nested=False):
    """Value a card for this player in this game. Returns a Valuation.

    A project card whose global-parameter requirement is likely met only some generations from now is valued as
    played then (production pays for fewer phases), with the requirement flags of now.

    `nested` is for valuations made inside another one (a hand card it unlocks): it skips unlock pricing,
    which would value cards again."""
    info = data.card(name) or {}
    params = [r for r in info.get("req") or [] if re.match(r"^(max )?(temperature|oxygen|oceans|venus) ", r)]
    if not nested and info.get("type") in PROJECT and name not in ctx.tableau and params and ctx.gens_left > 1:
        # Only global parameters: tag requirements depend on your own plays (priced in _unlocks).
        wait = max(_req_timing(req, ctx)[1] for req in params)
        g = math.ceil(wait - 1e-9) if wait < ctx.gens_left else None
        if g is None or g >= 1:
            later = _later(ctx, g if g is not None else ctx.gens_left - 1)
            v = _value_card(name, later, cost, buying, model, nested=True)
            now = Valuation(name)
            now.info = info
            _requirements(now, ctx)
            v.flags = [f for f in v.flags if not f.startswith("NOT PLAYABLE")] + now.flags + \
                [f"valued as played in generation {ctx.generation + g} (when its requirement is likely met)"
                 if g is not None else "requirement unlikely to be met before the last generation"]
            return v
    return _value_card(name, ctx, cost, buying, model, nested)


def _value_card(name, ctx, cost=None, buying=False, model=None, nested=False):
    info = data.card(name) or {}
    beh = card_behavior(name, ctx)
    own = beh.get("behavior") or {}
    if (name == "Helion" and not ctx.helion) or own.get("titaniumValue") or own.get("steelValue"):
        # The card's own effects are priced at the rates it sets: Helion spends heat as M€, Boom Town lowers the
        # titanium it produces to 2 M€.
        ctx = copy.copy(ctx)
        if name == "Helion":
            ctx.helion, ctx.heat = True, max(ctx.heat, 1.0)
        ctx.titanium += own.get("titaniumValue", 0)
        ctx.steel += own.get("steelValue", 0)
    v = Valuation(name)
    v.info = info
    ctype = info.get("type")
    tags = info.get("tags", [])
    persistent = [] if ctype == "event" else tags
    v.persistent_tags = persistent
    v.event_tags = tags if ctype == "event" else []

    if starting_mc(name, beh):
        v.add("starting M€" if ctype == "corporation" else "M€", info["startingMC"])
    if beh.get("behavior"):
        _apply(beh["behavior"], v, ctx, persistent, name)
    if beh.get("firstAction"):
        fa = dict(beh["firstAction"])
        fa.pop("text", None)
        _apply(fa, v, ctx, persistent, name, label="first action: ")

    vp = beh.get("vp")
    if isinstance(vp, (int, float)):
        v.add(f"{vp:g} VP", vp * VP)
    elif isinstance(vp, dict):
        if "resourcesHere" in vp:
            per = vp.get("per") or (vp["resourcesHere"] if isinstance(vp["resourcesHere"], int) else 1) or 1
            # Resources already here score now; future ones are valued where they come from (its action).
            current = (model or ctx.tableau.get(name) or {}).get("resources") or 0
            if current:
                v.add(f"{current} resources here ÷ {per} → VP", (current // per) * VP)
            if not (beh.get("action") or {}).get("addResources"):
                v.manual.append("VP depends on resources other cards or effects put here")
        elif "tag" in vp:
            n = _resolve({"tag": vp["tag"]}, persistent, ctx) // (vp.get("per") or 1)
            v.add(f"{n:g} VP from {vp['tag']} tags now", n * VP)
        elif "cities" in vp:
            n = (ctx.me["citiesCount"] + v.tiles["city"]) if vp.get("all") is False else \
                sum(p["citiesCount"] for p in ctx.players) + v.tiles["city"]
            per = vp.get("per", 1)
            v.add(f"{n // per:g} VP from {n} cities in play now", (n // per) * VP)
            v.flags.append("VP grows as anyone places cities")
        else:
            v.manual.append("VP rule not modelled: read the card")
    elif vp == "special":
        v.manual.append("special VP rule: read the card")

    action = beh.get("action")
    if action:
        _price_action(action, v, ctx, persistent, name, model)
    if beh.get("tr") and not beh.get("behavior", {}).get("tr") and beh.get("bespokePlay"):
        tr = beh["tr"]
        steps = sum(min(tr.get(k, 0), ctx.left.get({"oceans": "oceans"}.get(k, k), 99)) for k in ("temperature", "oxygen", "venus", "oceans")) + tr.get("tr", 0)
        if steps:
            v.add(f"~{steps:g} TR (from the card's TR hint)", steps * ctx.tr)
            for k in ("temperature", "oxygen", "venus", "oceans"):
                v.raises[k] += tr.get(k, 0)
            v.tr += steps
    tr_count = TR_PER_TAG.get(name)
    if tr_count:
        # "Including this" counts the printed tags, even on an event.
        steps = _resolve(tr_count, tags, ctx)
        per = f" ÷ {tr_count['per']}" if tr_count.get("per") else ""
        v.add(f"{steps:g} TR from {tr_count['tag']} tags{per}, including this", steps * ctx.tr)
        v.tr += steps
    elif beh.get("bespokePlay") and not beh.get("custom"):
        v.manual.append("part of its play effect is custom code: read the card text")
    if beh.get("bespokeAction") and not beh.get("custom"):
        v.manual.append("its action is custom code: read the card text")
    if name in EXCAVATION_PAYOUTS:
        rate, why = table_dig_rate(ctx)
        n = rate * ctx.gens_left
        v.add(f"{EXCAVATION_PAYOUTS[name]:g} M€ per excavation by anyone ×{n:.1f} ({why})", EXCAVATION_PAYOUTS[name] * n)
    if beh.get("triggers") and not (name in OWN_TRIGGERS and name not in ctx.tableau) and name not in PRICED_EFFECTS \
            and not beh.get("custom"):
        v.manual.append("ongoing effect (" + ", ".join(beh["triggers"]) + "): value depends on future plays")
    if name in ("Convert Plants", "Convert Heat"):
        v.flags.append("uses 8 " + ("plants" if name == "Convert Plants" else "heat") + " (priced at 0: no other use)")
    if not beh and ctype not in ("corporation",):
        v.manual.append("no structured effect data for this card")

    if cost is None:
        cost = (model or {}).get("calculatedCost", info.get("cost") or 0)
    if ctype in ("prelude", "corporation", "ceo"):
        cost = 0
    v.cost = cost or 0
    bought = ctype in ("automated", "active", "event")  # only project cards carry the 3 M€ purchase price
    v.paid = v.cost + (BUY_COST if buying and bought else 0)
    _context(v, ctx)
    _plan(v, ctx, nested)
    v.net = v.value - v.paid
    v.ratio = (v.value / v.paid) if v.paid else None
    return v


# Effects priced in value_card / _plan although the app runs them in custom code.
PRICED_EFFECTS = ("Aridor", "Excavator Leasing")
EXCAVATION_PAYOUTS = {"Excavator Leasing": 1}


def _digs_on_play(card):
    beh = data.behavior(card["name"] if isinstance(card, dict) else card) or {}
    return bool(((beh.get("behavior") or {}).get("underworld") or {}).get("excavate"))


def table_dig_rate(ctx):
    """(excavations per generation by all players, why): the faster of the table's rate so far (excavated spaces
    per finished generation), its excavation actions (one use each per generation) and what the deck's
    excavating cards give at the usual rate of cards played."""
    spaces = ctx.game.get("spaces") or []
    done = sum(1 for sp in spaces if sp.get("excavator"))
    past = done / max(1, ctx.generation - 1)
    actions = sum(1 for p in ctx.players for c in p.get("tableau") or []
                  if ((card_behavior(c["name"]).get("action") or {}).get("underworld") or {}).get("excavate"))
    cards = deck.names(ctx.expansions)
    share = sum(1 for c in cards if _digs_on_play(c)) / len(cards) if cards else 0.0
    drawn = share * deck.CARDS_PLAYED_PER_GEN * len(ctx.players)
    rate = max(past, actions + drawn)
    why = f"{done} dug so far" if past >= actions + drawn else f"{actions} excavation actions + cards"
    return rate, why


SP_KIND = {"Power Plant:SP": "energy production", "Asteroid:SP": "temperature", "Aquifer": "ocean",
           "Greenery": "greenery", "City": "city"}


def main_effect(v, ctx):
    """The kind of effect (as in SP_KIND) that makes up at least half of a card's value, or None."""
    amounts = {
        "energy production": v.prod.get("energy", 0) * ctx.phases * ctx.energy,
        "temperature": v.raises["temperature"] * ctx.tr,
        "ocean": v.tiles["ocean"] * (ctx.tr + OCEAN_ADJACENCY),
        "greenery": v.tiles["greenery"] * (VP + ctx.tr),
        "city": v.tiles["city"] * CITY_TILE,
    }
    kind, amount = max(amounts.items(), key=lambda kv: kv[1])
    # Milestones and awards the effect reaches come with it (the standard project reaches them too).
    shared = sum(a for label, a in v.parts if label.startswith("lets you claim") or " award " in label)
    return kind if amount > 0 and amount >= 0.5 * (v.value - shared) else None


def flag_cheaper_standard_projects(valuations, ctx):
    """Flag project cards that a standard project with the same main effect matches in value for less: Energy
    Saving for +1 energy production (and the milestone it reaches) is a dearer Power Plant. Each standard
    project is priced once."""
    sps = None
    for v in valuations:
        if v.info.get("type") not in ("automated", "active", "event") or v.value <= 0:
            continue
        kind = main_effect(v, ctx)
        if kind is None:
            continue
        if sps is None:
            sps = [value_card(n, ctx, cost=c, nested=True) for n, c in SP_COSTS.items()]
        sp = next((sp for sp in sps if SP_KIND[sp.name] == kind and sp.paid < v.paid and sp.value >= v.value - 0.5),
                  None)
        if sp:
            v.flags.append(f"STANDARD PROJECT does as much for less: {sp.name} ≈{sp.value:.0f} for {sp.paid:g} M€ "
                           f"(this ≈{v.value:.0f} for {v.paid:g} M€)")


def _later(ctx, g):
    """A copy of ctx as seen g generations from now: fewer generations left, and the steps left on each global
    parameter reduced at the table's pace (rounded to whole steps)."""
    c = copy.copy(ctx)
    c.gens_left = max(1, ctx.gens_left - g)
    c.phases = c.gens_left - 1
    c.tr = VP + c.phases
    c.plant_phases = c.gens_left
    c.left = {p: max(0, round(n - _param_rate(p, ctx) * g)) for p, n in ctx.left.items()}
    def level(param):
        return data.param_level(param, data.total_steps(param, ctx.board) - c.left[param])
    c.temp = max(ctx.temp, level("temperature"))
    c.oxygen = max(ctx.oxygen, level("oxygen"))
    c.oceans = max(ctx.oceans, level("oceans"))
    if ctx.expansions.get("venus"):
        c.venus = max(ctx.venus, level("venus"))
    c.heat = c.tr / 8 if c.left["temperature"] > 0 or ctx.ambient else 0.0
    c.plant = (VP + c.tr) / 8 if c.left["oxygen"] > 0 else VP / 8
    return c


_ACTION_INPUTS = ("steel", "titanium", "plants", "energy", "heat")


def _energy_demand(ctx, exclude):
    """Energy your other played cards' actions spend each generation (they already have first call on it)."""
    total = 0
    for name in ctx.tableau:
        if name == exclude:
            continue
        action = card_behavior(name).get("action") or {}
        options = action["or"].get("behaviors", []) if "or" in action else [action]
        spends = [o.get("spend", {}).get("energy", 0) for o in options]
        total += max([n for n in spends if isinstance(n, (int, float))] or [0])
    return total


def _price_action(action, v, ctx, tags, name, model):
    """Price a repeatable action as one use per generation left, played out generation by generation:
    each use needs its inputs (resources here, and steel/titanium/plants/heat stock and production, or this
    generation's energy, since unspent energy turns into heat), resources added here only pay through a
    later conversion or the card's VP, and each use is priced at the rates of the generation it happens in."""
    G = ctx.gens_left
    owned = name in ctx.tableau
    options = action["or"].get("behaviors", []) if "or" in action else [action]
    vp = card_behavior(name).get("vp")
    per_res = 0.0
    if isinstance(vp, dict) and "resourcesHere" in vp:
        per_res = VP / (vp.get("per") or (vp["resourcesHere"] if isinstance(vp["resourcesHere"], int) else 1) or 1)
    here = ((model or ctx.tableau.get(name) or {}).get("resources") or 0) + (0 if owned else v.card_resources)
    inputs = sorted({r for o in options for r in (o.get("spend") or {}) if r in _ACTION_INPUTS})
    stock = {r: float(ctx.me.get(_STOCK_KEY[r], 0) or 0) for r in inputs}
    income = {r: float(ctx.me.get(_PROD_KEY[r], 0) or 0) + (0 if owned else v.prod.get(r, 0)) for r in inputs}
    demand = {r: (_energy_demand(ctx, name) if r == "energy" else 0) for r in inputs}
    stock = {r: max(0.0, stock[r] - demand[r]) for r in inputs}
    converts = any((o.get("spend") or {}).get("resourcesHere") for o in options)
    own_type = (data.card(name) or {}).get("resource")
    # A card not played yet can only act once its requirements are likely met.
    start = 0
    if not owned:
        waits = [_req_timing(req, ctx)[1] for req in (data.card(name) or {}).get("req", [])]
        wait = max(waits, default=0.0)
        start = G if wait >= G else math.ceil(wait - 1e-9)
        if start >= G:
            v.flags.append("its action never runs: the requirement is unlikely to be met in time")
            return
    total, uses, dug, short = 0.0, 0, 0, False
    for g in range(start, G):
        cg = _later(ctx, g)
        cg.dug = dug
        picks, lacking = [], False
        for opt in options:
            spend = opt.get("spend") or {}
            need = _resolve(spend.get("resourcesHere", 0), tags, cg)
            if any(_resolve(spend[r], tags, cg) > stock[r] + 1e-9 for r in inputs if r in spend):
                lacking = True
                continue
            if need > here:
                continue
            trial = Valuation(name)
            # Resources this card's own later conversion spends count as added here, even when the action may
            # put them on any card (Comet Aiming's asteroids).
            to_any = opt.get("addResourcesToAnyCard")
            self_fuel = converts and isinstance(to_any, dict) and to_any.get("type") == own_type
            skip = ("addResources", "title") + (("addResourcesToAnyCard",) if self_fuel else ())
            _apply({k: x for k, x in opt.items() if k not in skip}, trial, cg, tags, name)
            add = _resolve(opt.get("addResources", 0), tags, cg) + \
                (_resolve(to_any.get("count", 1), tags, cg) if self_fuel else 0)
            worth = trial.value + (add - need) * per_res
            # Adding fuel for a later conversion is worth nothing by itself but keeps the action going.
            if worth > 1e-9 or (add and converts and not need):
                picks.append((bool(need), worth, opt, trial, add, need))
        short = short or (lacking and not picks)
        if picks:
            conversions = [p for p in picks if p[0]]
            _, worth, opt, trial, add, need = max(conversions or picks, key=lambda p: p[1])
            total += worth
            uses += 1
            here += add - need
            dug += trial.tokens
            v.tokens += trial.tokens
            v.card_resources += add
            v.draws += trial.draws
            v.tr += trial.tr
            for k in v.raises:
                v.raises[k] += trial.raises[k]
            for r in inputs:
                stock[r] -= _resolve((opt.get("spend") or {}).get(r, 0), tags, cg)
        for r in inputs:
            # Production phase: energy left over becomes heat, other resources carry over.
            stock[r] = max(0.0, income[r] - demand[r]) if r == "energy" else stock[r] + income[r]
    if total > 0:
        limited = f" of {G - start}, short of {'/'.join(inputs)}" if short else ""
        limited += f" from generation {ctx.generation + start} (requirement)" if start else ""
        v.add(f"action ×{uses}{limited} (≈{total / uses:.1f} each)", total)
    else:
        v.flags.append("its action pays nothing at current rates and resources")


# --------------------------------------------------------------------------------------------- colonies

TRADE_COST = 9               # M€ per trade (or 3 energy or 3 titanium), from the rules
# Assumption: about half of the players' trade fleets trade each generation (a trade costs 9 M€ for a return that
# beats it only once a track has moved up). Those trades go to the tiles in proportion to what a trade there pays.
TRADES_PER_PLAYER = 0.5
_COLONY_RES = {"microbe": "Microbe", "animal": "Animal", "floater": "Floater", "asteroid": "Asteroid", "data": "Data"}


def colony_reward_value(text, n, ctx):
    """M€ value of one colony reward text (build, trade or colony bonus) with n units."""
    t = (text or "").lower()
    left = ctx.left
    if "production" in t:
        res = next((r for r in ("steel", "titanium", "plant", "energy", "heat") if r in t), "megacredits")
        res = "plants" if res == "plant" else res
        return n * _unit(res, ctx) * (ctx.plant_phases if res == "plants" else ctx.phases)
    if "vp" in t:
        return n * VP
    if re.search(r"\btr\b", t):
        return n * ctx.tr
    if "draw" in t:
        return CARD + (n - 1) * 0.5 if "keep 1" in t else n * CARD
    if "add" in t and "to any" in t:
        kind = next((k for k in _COLONY_RES if k in t), None)
        return n * (_best_target(ctx, _COLONY_RES[kind])[0] if kind else 1.0)
    if "ocean" in t:
        return (ctx.tr + OCEAN_ADJACENCY) if left["oceans"] else 0.0
    if "venus" in t and "increase" in t:
        return n * ctx.tr if left["venus"] else 0.0
    if "lose" in t and "m€" in t:
        return -n
    for word, res in (("steel", "steel"), ("titanium", "titanium"), ("plant", "plants"), ("energy", "energy"),
                      ("heat", "heat"), ("m€", "megacredits")):
        if word in t:
            return n * _unit(res, ctx)
    return 1.0


def _first_number(text, default=1):
    m = re.search(r"\d+", text or "")
    return int(m.group(0)) if m else default


def _colony_models(ctx):
    return [c for c in ctx.game.get("colonies") or [] if c.get("isActive", True) and data.colony(c["name"])]


def _trade_qty(col, bump=0):
    qty = (data.colony(col["name"]) or {}).get("tradeQty") or [1]
    return qty[min(max(0, col.get("trackPosition", 1) + bump), len(qty) - 1)]


def tile_trade_rates(ctx):
    """{tile: expected trades there per generation by all players}: TRADES_PER_PLAYER per player, split over the
    tiles in proportion to what a trade pays at the track position now (at least a quarter of the trade cost, so
    every tile sees some trades)."""
    models = _colony_models(ctx)
    weights = {c["name"]: max(TRADE_COST / 4, colony_reward_value(data.colony(c["name"]).get("trade"), _trade_qty(c), ctx))
               for c in models}
    total = sum(weights.values())
    return {n: TRADES_PER_PLAYER * len(ctx.players) * w / total for n, w in weights.items()} if total else {}


def colony_build_value(col, ctx, double=False, rates=None):
    """(M€, text) for building a colony on one tile: its build bonus (twice with Vital Colony) and the colony
    bonus it pays you each time anyone trades there (Titania's bonus is a loss: 3 M€ per trade)."""
    info = data.colony(col["name"])
    build = info.get("build", "")
    nth = len(col.get("colonies") or [])
    m = re.match(r"Gain ([\d, or]+) VP", build)
    n = [int(x) for x in re.findall(r"\d+", m.group(1))][min(nth, 2)] if m else _first_number(build)
    bonus = colony_reward_value(build, n, ctx) * (2 if double else 1)
    rate = (rates if rates is not None else tile_trade_rates(ctx)).get(col["name"], 0.0)
    per_trade = colony_reward_value(info.get("colonyBonus"), _first_number(info.get("colonyBonus")), ctx)
    income = per_trade * rate * ctx.gens_left
    shown = f"Gain {n} VP" if m else build
    text = f"{col['name']}: {shown}" + (" ×2" if double else "") + \
        f", {info.get('colonyBonus')} ×{rate * ctx.gens_left:.1f} trades"
    return bonus + income, text


def colony_builds(ctx, duplicates=False, double=False):
    """[(M€, text, tile)] for every colony tile you can build on now, best first."""
    me = ctx.me["color"]
    rates = tile_trade_rates(ctx)
    out = [colony_build_value(c, ctx, double, rates) + (c["name"],) for c in _colony_models(ctx)
           if len(c.get("colonies") or []) < 3 and (duplicates or me not in (c.get("colonies") or []))]
    return sorted(out, key=lambda o: -o[0])


def best_colony_build(ctx, duplicates=False, double=False):
    """(M€, text) for the best colony you can build now (a flat COLONY estimate without colony data)."""
    builds = colony_builds(ctx, duplicates, double)
    if not builds:
        return (COLONY * (1.5 if double else 1), None) if not _colony_models(ctx) else (0.0, "no colony tile has room")
    return builds[0][:2]


def trade_value(col, ctx, bump=0):
    """M€ from one trade with a colony at its track position now (moved `bump` steps up first), your colony
    bonuses there included."""
    info = data.colony(col["name"])
    mine = (col.get("colonies") or []).count(ctx.me["color"])
    bonus = colony_reward_value(info.get("colonyBonus"), _first_number(info.get("colonyBonus")), ctx)
    return colony_reward_value(info.get("trade"), _trade_qty(col, bump), ctx) + mine * bonus


def fleet_value(ctx, extra=1):
    """(M€, text) for `extra` more trade fleets: your fleets already take the best trades, each new one trades
    once a generation with the next best colony, net of the trade's cost."""
    models = _colony_models(ctx)
    if not models:
        return 0.0, "no colony tiles in play"
    trades = sorted(((trade_value(c, ctx) - TRADE_COST, c["name"]) for c in models), reverse=True)
    have = ctx.me.get("fleetSize", 1) or 1
    picked = trades[have:have + extra]
    worth = sum(max(0.0, t) for t, _ in picked)
    text = ", ".join(f"{n} {t + TRADE_COST:.0f}" for t, n in picked) or "no colony left"
    return worth * ctx.gens_left, f"next best trade {text} (−{TRADE_COST} per trade)"


# --------------------------------------------------------------------------------------------- corporations, preludes
# Corporation and prelude effects the app runs in custom code, priced from the card text and the rules (not fitted to
# game results). Similar effects share one helper: an extra prelude or corporation drawn (best of k from this game's
# pool), colony-track moves and trades, a card played from hand, effects that pay each generation. Effects fired by
# later plays (per tag, per tile, per TR) are OWN_TRIGGERS / RIVAL_TRIGGERS entries further down.

# A requirement card played earlier than its requirement allows (Ecology Experts, Inventrix's ±2 steps): about one
# generation of a typical card's income. Assumption: the deck's average card returns ~4 M€ a generation.
EARLY_PLAY = 4.0
# Cards whose effect draws more preludes or corporations: left out of the pools they draw from (no recursion).
_DRAWS_SETUP = ("New Partner", "Board of Directors", "Merger", "Double Down", "Co-leadership", "Valley Trust")
_POOLS = {}


def _neutral(ctx):
    """ctx without your tableau, hand or opening package: what a drawn prelude or corporation is worth by itself."""
    c = copy.copy(ctx)
    c.tableau, c.hand, c.package_hand, c.preludes, c.offered = {}, [], [], [], None
    c._planned, c._afford = [], 1.0
    c.__dict__.pop("notes", None)
    return c


def setup_pool(ctx, kind):
    """Sorted values of the preludes (or corporations) this game's modules put in its deck, priced against ctx's
    game state without your tableau. Cached per game state."""
    modules = deck._enabled(ctx.expansions)
    key = (kind, modules, ctx.gens_left, ctx.generation, len(ctx.players), ctx.left["oceans"], ctx.left["oxygen"],
           ctx.left["temperature"], tuple(sorted(c["name"] for c in _colony_models(ctx))))
    if key not in _POOLS:
        base = _neutral(ctx)
        _POOLS[key] = sorted(value_card(name, base, nested=True).value for name, c in data.cards().items()
                             if c.get("type") == kind and c.get("module") in modules and name not in _DRAWS_SETUP)
    return _POOLS[key]


def best_of(values, k, cost=0.0, optional=False):
    """Expected value of the best of k drawn from `values` (with replacement), minus `cost`; with `optional` the
    draw may be discarded, so a draw worth less than its cost counts as 0."""
    n = len(values)
    if not n or k <= 0:
        return 0.0
    out, prev = 0.0, 0.0
    for i, x in enumerate(sorted(values)):
        cdf = ((i + 1) / n) ** k
        out += (max(0.0, x - cost) if optional else x - cost) * (cdf - prev)
        prev = cdf
    return out


def extra_setup_card(ctx, kind, draw, cost=0.0, optional=False):
    """(M€, text) for drawing `draw` preludes (or corporations) and playing the best: Valley Trust, New Partner,
    Merger, Board of Directors."""
    pool = setup_pool(ctx, kind)
    worth = best_of(pool, draw, cost, optional)
    return worth, f"best of {draw} {kind}s drawn (pool of {len(pool)}, mean ≈{sum(pool) / max(1, len(pool)):.0f})" + \
        (f" − {cost:g} M€" if cost else "")


def free_trade_value(ctx, bump=0):
    """M€ of one trade without paying for it, at your best colony tile (0 without colony tiles)."""
    return max((trade_value(c, ctx, bump) for c in _colony_models(ctx)), default=0.0)


def trade_all_value(ctx, bump=1):
    """(M€, text) for trading with every active colony tile at once, each track first moved `bump` steps up
    (Trade Advance)."""
    models = _colony_models(ctx)
    if not models:
        return 0.0, "no colony tiles in play"
    parts = [(trade_value(c, ctx, bump), c["name"]) for c in models]
    return sum(w for w, _ in parts), ", ".join(f"{n} {w:.0f}" for w, n in parts)


def colony_track_value(ctx, steps):
    """(M€, text) for moving every colony track `steps` up (Early Colonization): your next trade pays more at
    the tile it gains most on. Rivals' trades gain too, but rival gains are not priced here."""
    models = _colony_models(ctx)
    if not models:
        return 0.0, "no colony tiles in play"
    gain, name = max((trade_value(c, ctx, steps) - trade_value(c, ctx), c["name"]) for c in models)
    return gain, f"your next trade: {name} +{gain:.0f}"


def _hand_cards(ctx):
    return [n for n in ctx.package_hand if (data.card(n) or {}).get("type") in PROJECT]


def _floater_card(name, active=False):
    info = data.card(name) or {}
    return info.get("resource") == "Floater" and (not active or info.get("type") == "active")


def play_from_hand(ctx, opts):
    """(M€, text, card) for a prelude that plays a card from your hand: the M€ it saves (Eccentric Sponsor's
    discount), floaters it adds and a requirement it skips. Without a known hand, the chance a 5-card hand holds a
    fitting card comes from the deck."""
    def fits(name):
        info = data.card(name) or {}
        return (not opts.get("tag") or opts["tag"] in (info.get("tags") or [])) and \
            (not opts.get("floaterActive") or _floater_card(name, active=True))

    def gain(name):
        info = data.card(name) or {}
        out = min(opts.get("discount", 0), info.get("cost") or 0)
        if opts.get("floaters") and _floater_card(name):
            out += opts["floaters"] * _resource_vp_value(name, ctx)
        if opts.get("ignoreReqs") and any(re.match(r"^(max )?(temperature|oxygen|oceans|venus) ", r)
                                          for r in info.get("req") or []):
            out += EARLY_PLAY
        return out
    hand = [n for n in _hand_cards(ctx) if fits(n)]
    if hand:
        best = max(hand, key=gain)
        return gain(best), f"plays {best} from hand", best
    cards = [c for c in deck.names(ctx.expansions) if fits(c)]
    if not cards:
        return 0.0, "no fitting card in this deck", None
    p = 1 - (1 - len(cards) / max(1, len(deck.names(ctx.expansions)))) ** 5
    mean = sum(gain(c) for c in cards) / len(cards)
    return p * mean, f"a fitting card in a 5-card hand p≈{p:.2f}", None


def _cards_bought(ctx):
    """Cards you will still buy: CARDS_PLAYED_PER_GEN in each research phase to come."""
    return deck.CARDS_PLAYED_PER_GEN * deck.draw_gens(ctx)


def _standard_project_net(ctx):
    """Best standard project's value minus its cost now (Established Methods must perform two)."""
    return max(value_card(n, ctx, cost=c, nested=True).net for n, c in SP_COSTS.items())


def _agricola(ctx):
    """Agricola Inc: at game end −2/0/1/2 VP per tag type for 0/1-2/3-4/5+ tags. Tags kept now plus the expected
    kept tags of the cards you will play (deck rates), rounded to whole tags."""
    types = {t for c in deck.deck(ctx.expansions) for t in c.get("tags") or []} - set(_TAG_TYPES_SKIP)
    points = 0
    for t in types:
        n = round(ctx.tags.get(t, 0) + deck.future_matches(ctx, f"tag:{t}"))
        points += -2 if n == 0 else 0 if n <= 2 else 1 if n <= 4 else 2
    return points * VP


def _orbital_value(ctx):
    """Adhai High Orbit Constructions: each 2 orbitals take 1 M€ off later space cards without a planet tag,
    standard colony projects and trades. An orbital gained now pays ½ M€ on each of those still to come (about
    half the game's on average)."""
    later = deck.future_matches(ctx, "orbital") + (ctx.gens_left if _colony_models(ctx) else 0) * TRADES_PER_PLAYER
    return 0.5 * later / 2


def _darkside(ctx):
    """The Darkside of The Moon Syndicate: 1 titanium adds a fleet; a fleet steals 2 M€ from each opponent."""
    return {"action": {"or": {"behaviors": [
        {"spend": {"titanium": 1}, "addResources": 1, "title": "add a syndicate fleet"},
        {"spend": {"resourcesHere": 1}, "stock": {"megacredits": 2 * len(ctx.rivals)}, "title": "steal 2 M€ each"}]}}} \
        if ctx else {}


SETUP_BEHAVIORS = {
    # Extra preludes and corporations.
    "Valley Trust": {"firstAction": {"extraSetup": {"kind": "prelude", "draw": 3}}},
    "New Partner": {"behavior": {"extraSetup": {"kind": "prelude", "draw": 2}}},
    "Merger": {"behavior": {"extraSetup": {"kind": "corporation", "draw": 4, "cost": 42}}},
    # One prelude a generation while directors last (4): drawn, then played for 12 M€ or discarded.
    "Board of Directors": {"action": {"spend": {"resourcesHere": 1},
                                      "extraSetup": {"kind": "prelude", "draw": 1, "cost": 12, "optional": True}}},
    # Colonies and trades.
    "Trade Advance": {"behavior": {"stock": {"megacredits": 2}, "tradeAll": 1}},
    "Early Colonization": {"behavior": {"colonyTracks": 2}},
    "Aerospace Mission": {"behavior": {"colonies": {"buildColony": {"count": 2}}}},
    "Prospecting": {"behavior": {"colonies": {"buildColony": {}}}},
    "Huygens Observatory": {"behavior": {"colonies": {"buildColony": {"allowDuplicates": True}}, "tradeFree": 1}},
    # A card played from hand (tfm plan plays it; see plan.PLAY_FROM_HAND).
    "Eccentric Sponsor": {"behavior": {"playFromHand": {"discount": 25}}},
    "Ecology Experts": {"behavior": {"playFromHand": {"ignoreReqs": True}}},
    "Valuable Gases": {"behavior": {"playFromHand": {"tag": "venus", "floaters": 4}}},
    "Valuable Gases:Pathfinders": {"behavior": {"stock": {"megacredits": 10},
                                                "playFromHand": {"floaterActive": True, "ignoreReqs": True, "floaters": 5}}},
    # Cards drawn by tag (the generic draw prices any card the same).
    "Atmospheric Enhancers": {"behavior": {"drawCard": {"count": 2, "tag": "floater"}}},
    "Nobel Prize": {"behavior": {"drawCard": 2}},
    "Personal Agenda": {"behavior": {"drawCard": 3}},
    "Planetary Alliance": {"behavior": {"drawCard": 2}},
    "High Circles": {"behavior": {"drawCard": 1}},
    "Political Uprising": {"behavior": {"drawCard": 1}},
    "Celestic": {"firstAction": {"drawCard": 2}},
    "Morning Star Inc.": {"firstAction": {"drawCard": 3}, "behavior": {"earlyPlay": 1}},
    "Spire": {"firstAction": {"drawCard": {"count": 4, "keep": 1}}},
    "Eris": {"firstAction": {"drawCard": 1}, "action": {"tr": 0.5}},  # a hazard to remove about every other generation
    # Tiles and underground tokens.
    "Underground Settlement": {"behavior": {"city": {}, "underworld": {"identify": {"count": 4, "claim": 1}}}},
    "Central Reservoir": {"behavior": {"ocean": {}, "underworld": {"identify": {"count": 4, "claim": 2}}}},
    "Tunneling Operation": {"behavior": {"underworld": {"identify": 1, "excavate": 2}}},
    "Survey Mission": {"behavior": {"standardResource": 3}},  # three placement bonuses collected
    "Boom Town": {"behavior": {"standardResource": 2}},  # the steel or titanium placement bonus under the city
    "Mining Rights": {"behavior": {"tile": {}, "or": {"behaviors": [
        {"production": {"steel": 1}, "stock": {"steel": 1}, "title": "steel"},
        {"production": {"titanium": 1}, "stock": {"titanium": 1}, "title": "titanium"}]}}},
    "Mining Rights:ares": {"behavior": {"tile": {}, "standardResource": 1, "or": {"behaviors": [
        {"production": {"steel": 1}, "stock": {"steel": 1}, "title": "steel"},
        {"production": {"titanium": 1}, "stock": {"titanium": 1}, "title": "titanium"}]}}},
    "Landfill": lambda ctx: {"behavior": {"production": {"megacredits": sum(
        1 for r in RESOURCES if (ctx.me.get(_PROD_KEY[r], 0) or 0) > 0) if ctx else 0}}},
    "Project Eden": {"behavior": {"ocean": {}, "city": {}, "greenery": {}, "worth": {"discard 3 cards": -1.5}}},
    "Old Mining Colony": {"behavior": {"worth": {"discard 1 card": -0.5}}},
    "Mining Complex": {"behavior": {"tr": 2, "worth": {"mine and road tiles on The Moon": 2 * SPECIAL_TILE}}},
    "Crew Training": {"behavior": {"planetaryTrack": 2}},
    "Established Methods": lambda ctx: {"behavior": {"worth": {
        "2 standard projects (best value − cost)": 2 * _standard_project_net(ctx) if ctx else 0.0}}},
    "Executive Order": {"behavior": {"stock": {"megacredits": 10}}},
    # Effects paying each generation (or each action) priced per generation.
    "Aerotech": {"behavior": {"perGeneration": {"text": "titanium for the 2 cards not bought",
                                                "behavior": {"stock": {"titanium": 4 - deck.CARDS_PLAYED_PER_GEN}}}}},
    "Pristar": {"behavior": {"perGeneration": {"text": "no TR this generation (assumed 1 in 3): preservation + 6 M€",
                                               "share": 1 / 3, "phase": True,
                                               "behavior": {"stock": {"megacredits": 6}, "addResources": 1}}}},
    "SolBank": {"behavior": {"perGeneration": {"text": "data for each payment (cards played + 1), 1 M€ each",
                                               "phase": True,
                                               "behavior": {"stock": {"megacredits": deck.CARDS_PLAYED_PER_GEN + 1}}}}},
    "Mars Maths": {"behavior": {"perGeneration": {"text": "a 5th research card (half a card kept)",
                                                  "behavior": {"drawCard": 0.5}}}},
    # Actions the app runs in custom code.
    "AstroDrill": {"action": {"or": {"behaviors": [
        {"spend": {"resourcesHere": 1}, "stock": {"titanium": 3}, "title": "3 titanium"},
        {"standardResource": 1, "title": "a standard resource"}]}}},
    "Aeron Genomics": {"action": {"addResourcesToAnyCard": {"type": "Animal", "count": 1}}},  # a claimed token a gen
    "Arcadian Communities": {"action": {"stock": {"megacredits": 2}}},  # 3 M€ for most community spaces you build on
    # The energy production step once (it needs no energy left), then 3 M€ for a building card.
    "Factorum": {"behavior": {"production": {"steel": 1, "energy": 1}}, "action": {"spend": {"megacredits": 3}, "drawCard": 1}},
    "Gagarin Mobile Base": {"firstAction": {"standardResource": 1}, "action": {"standardResource": 1}},
    "Junk Ventures": {"action": {"drawCard": {"count": 3, "keep": 1}}},
    "Keplertec": {"action": {"spend": {"titanium": 1}, "underworld": {"identify": {"count": 4, "claim": 1}}}},
    "Nanotech Industries": {"action": {"addResources": 1}},
    "Odyssey": {"action": {"stock": {"megacredits": 2}}},     # replays a cheap event: its value over its cost
    "Playwrights": {"action": {"stock": {"megacredits": 2}}},  # the same, from any player's events
    "Project Workshop": {"action": {"spend": {"megacredits": 3}, "drawCard": 1}},
    # Each use raises a different lowest production (the one worth most first, then the next): priced as an even
    # mix of the five non-M€ productions.
    "Robinson Industries": {"action": {"spend": {"megacredits": 4}, "production": {
        r: 0.2 for r in ("steel", "titanium", "plants", "energy", "heat")}}},
    "Tycho Magnetics": {"action": {"spend": {"energy": 1}, "drawCard": 1}},
    "United Nations Mars Initiative": {"action": {"spend": {"megacredits": 3}, "tr": 1}},
    "Viron": {"action": {"stock": {"megacredits": 2}}},  # a second use of your best action, once you have one
    "Septem Tribus": {"action": {"stock": {"megacredits": 4}}},  # delegates in about 2 parties
    "Main Belt Asteroids": {"action": {"addResources": 1, "stock": {"titanium": 1}}},
    # An asteroid per space tag, spent as 1 M€ on the aquifer and asteroid standard projects.
    "Kuiper Cooperative": {"action": {"stock": {"megacredits": {"tag": "space"}}}},
    # A floater here is 2 heat.
    "Stormcraft Incorporated": {"action": {"or": {"behaviors": [
        {"addResourcesToAnyCard": {"type": "Floater", "count": 1}, "title": "a floater on another card"},
        {"stock": {"heat": 2}, "title": "a floater here (2 heat)"}]}}},
    "Floating Trade Hub": {"action": {"standardResource": 1}},  # 2 floaters here one generation, spent the next
    # A weak card swapped for a new one, about every other generation (when you hold one you will not play).
    "Focused Organization": {"action": {"worth": {"swap a weak card for a new one": (CARD - 0.5) / 2}}},
    "The Darkside of The Moon Syndicate": _darkside,
    # One-off firsts and passive effects.
    "Anubis Securities": {"firstAction": {"earlyPlay": 1}},
    "Inventrix": {"behavior": {"earlyPlay": 2}},
    "Vitor": {"firstAction": {"worth": {"an award funded free": 8}}},
    "Intragen Sanctuary Headquarters": {"firstAction": {"tr": 1, "tile": {}}},
    "Nirgal Enterprises": {"behavior": {"worth": {"a milestone and an award at no cost": 8 + 8}}},
    "Soylent Seedling Systems": {"behavior": {"stock": {"megacredits": 10}}},  # 2 seeds at 5 M€ on a greenery
    "Ecoline": {},  # greeneries for 7 plants: its exported greeneryDiscount
    "Polyphemos": {"behavior": {"cardBuyCost": 5}},
    "TerraLabs Research": {"behavior": {"cardBuyCost": 1}},
    "Beginner Corporation": {"behavior": {"freeHand": 10}},
    "Agricola Inc": lambda ctx: {"behavior": {"worth": {"VP per tag type at game end": _agricola(ctx) if ctx else 0}}},
    # The custom code only collects the printed payment (startingMC, already priced) or checks it can be paid.
    "Business Empire": {}, "Galilean Mining": {}, "Huge Asteroid": {}, "Aquifer Turbines": {},
    "Strategic Base Planning": {},
    # Priced elsewhere: Helion's heat as M€ (_value_card), wild tags (counted as tags), and the rest by triggers.
    "Helion": {}, "Chimera": {}, "Research Network": {},
    # Their actions spend what their triggers (OWN_TRIGGERS) collect, priced there; Ambient's 8 heat for 1 TR is the
    # heat rate after temperature is maxed (Outlook.ambient).
    "Collegium Copernicus": {}, "Mind Set Mars": {}, "The Archaic Foundation Institute": {}, "Ambient": {},
    "Lakefront Resorts": {}, "Sagitta Frontier Services": {"behavior": {"drawCard": 1}},
    "Tharsis Republic": {},  # its custom play only adds solo mode's neutral cities
}
CUSTOM_BEHAVIORS.update(SETUP_BEHAVIORS)

# Corporations and preludes left unpriced on purpose, with why. tests/test_setup_coverage.py fails when a card with
# custom code is in neither a model nor this list.
NOT_MODELLED = {
    "Kingdom of Tauraro": "its custom part gives every opponent 2 M€ production: rival gains are not priced",
    "Mons Insurance": "its custom part cuts opponents' production and pays victims of attacks: rival effects are not priced",
    "Recession": "its custom part takes M€ and production from opponents: rival losses are not priced",
    "Luna First Incorporated": "pays per Moon rate step: Moon rates are not simulated",
    "Luna Trade Federation": "titanium production per Moon mine and titanium as 2 M€: Moon tiles are not simulated",
    "The Grand Luna Capital Group": "pays per adjacent Moon habitat: Moon placement is not simulated",
    "Tempest Consultancy": "delegates and chairman TR: Turmoil is not simulated",
    "By-Election": "sets the ruling party and gives influence: Turmoil is not simulated",
    "The New Space Race": "first player and the first ruling policy: Turmoil and turn order are not simulated",
    "Incite": "influence and cheaper lobbying: Turmoil is not simulated",
    "Corridors of Power": "draws a card when you become party leader: Turmoil is not simulated",
    "Election Sponsorship": "influence: Turmoil is not simulated",
    "Co-leadership": "draws CEO cards: CEO cards are not in the card data",
    "Delta Project": "the Delta Project track is not in the card data",
    "Athena": "hazard immunity and hazard tiles: board placement is not simulated",
    "Habitat Marte": "Mars tags count as science tags: tag counts are not remapped",
    "World Government Advisor": "its action raises a parameter with no TR or bonus: it only changes the game's pace",
}


def _apply_setup_effects(beh, v, ctx, card_tags, card_name, scale, pre):
    """Price the SETUP_BEHAVIORS keys of one Behavior dict into v (see _apply)."""
    if "perGeneration" in beh:
        pg = beh["perGeneration"]
        times = (ctx.phases if pg.get("phase") else ctx.gens_left) * pg.get("share", 1.0)
        trial = Valuation(card_name)
        _apply(pg["behavior"], trial, ctx, card_tags, card_name)
        v.add(f"{pre}{pg.get('text', 'each generation')} ×{times:.1f}", trial.value * times * scale)
    for label, amount in (beh.get("worth") or {}).items():
        v.add(pre + label, amount * scale)
    if beh.get("earlyPlay"):
        v.add(f"{pre}plays {beh['earlyPlay']:g} requirement card(s) a generation early", beh["earlyPlay"] * EARLY_PLAY * scale)
    if beh.get("extraSetup"):
        ex = beh["extraSetup"]
        worth, text = extra_setup_card(ctx, ex["kind"], ex["draw"], ex.get("cost", 0), ex.get("optional", False))
        v.add(pre + text, worth * scale)
    if beh.get("tradeAll"):
        worth, text = trade_all_value(ctx, beh["tradeAll"])
        v.add(f"{pre}trades with every colony, tracks +{beh['tradeAll']} ({text})", worth * scale)
    if beh.get("colonyTracks"):
        worth, text = colony_track_value(ctx, beh["colonyTracks"])
        v.add(f"{pre}colony tracks +{beh['colonyTracks']} ({text})", worth * scale)
    if beh.get("tradeFree"):
        worth = free_trade_value(ctx) * beh["tradeFree"]
        v.add(f"{pre}a free trade" + ("" if worth else " (no colony tiles)"), worth * scale)
    if beh.get("playFromHand"):
        worth, text, _ = play_from_hand(ctx, beh["playFromHand"])
        v.add(f"{pre}{text}", worth * scale)
        v.simulated += worth * scale
    if beh.get("cardBuyCost"):
        n = _cards_bought(ctx)
        worth = (BUY_COST - beh["cardBuyCost"]) * n
        v.add(f"{pre}cards cost {beh['cardBuyCost']} M€ to buy ×{n:.0f}", worth * scale)
        v.simulated += worth * scale
    if beh.get("freeHand"):
        # The opening hand you would buy (about half the cards dealt) is free, and the rest come along.
        n = beh["freeHand"]
        worth = n / 2 * BUY_COST + n / 2 * 0.5
        v.add(f"{pre}{n} starting cards free", worth * scale)
        v.simulated += worth * scale


# --------------------------------------------------------------------------------------------- underworld

def corruption_value(ctx):
    """Corruption cancels negative card VP at game end (1 VP each); otherwise it mainly blocks attacks."""
    vp = ctx.me.get("victoryPointsBreakdown") or {}
    have = (ctx.me.get("underworldData") or {}).get("corruption", 0)
    return VP if abs(vp.get("negativeVP", 0) or 0) > have else 1.0


def token_value(token, ctx):
    """M€ value of claiming one underground token now."""
    microbe, _ = _best_target(ctx, "Microbe")
    return {
        "nothing": 0, "card1": CARD, "card2": 2 * CARD,
        "corruption1": corruption_value(ctx), "corruption2": 2 * corruption_value(ctx),
        "steel2plant": 2 * ctx.steel + ctx.plant, "steel1production": ctx.steel * ctx.phases,
        "titanium2": 2 * ctx.titanium, "titanium1production": ctx.titanium * ctx.phases,
        "titaniumandplant": ctx.titanium + ctx.plant, "plant2": 2 * ctx.plant, "plant3": 3 * ctx.plant,
        "plant1production": ctx.plant * ctx.plant_phases, "energy2": 2 * ctx.energy,
        "energy1production": ctx.energy * ctx.phases, "heat2production": 2 * ctx.heat * ctx.phases,
        "microbe2": 2 * microbe, "tr": ctx.tr,
        "ocean": (ctx.tr + OCEAN_ADJACENCY - 4) if ctx.left["oceans"] else 0,
        "sciencetag": 2.0, "planttag": 2.0, "anyresource1": 2.0, "place6mc": 0,
        # Temporary bonuses pay per 2 °C raised this generation; assume one step.
        "plant2pertemp": 2 * ctx.plant if ctx.left["temperature"] else 0,
        "steel2pertemp": 2 * ctx.steel if ctx.left["temperature"] else 0,
        "titanium1pertemp": ctx.titanium if ctx.left["temperature"] else 0,
        "microbe2pertemp": 2 * microbe if ctx.left["temperature"] else 0,
        "mcprod1pertemp": ctx.phases if ctx.left["temperature"] else 0,
        "oceanrequirementmod": 1.0, "oxygenrequirementmod": 1.0, "temprequirementmod": 1.0,
    }.get(token, 1.0)


def _bag(ctx):
    """Tokens still in the bag: the full set minus identified and claimed ones visible to us."""
    bag = dict(TOKEN_COUNTS)
    seen = [s["undergroundResource"] for s in ctx.game.get("spaces", []) if s.get("undergroundResource")]
    for p in ctx.players:
        seen += [t["token"] for t in (p.get("underworldData") or {}).get("tokens", [])]
    for t in seen:
        if bag.get(t, 0) > 0:
            bag[t] -= 1
    return bag


def blind_dig_value(ctx, only=None):
    bag = _bag(ctx)
    if only is not None:
        bag = {t: n for t, n in bag.items() if t in only}
    total = sum(bag.values())
    return sum(n * token_value(t, ctx) for t, n in bag.items()) / total if total else 0.0


def best_of_value(ctx, k):
    """Expected value of the best of k random tokens (drawn with replacement; close enough)."""
    bag = _bag(ctx)
    total = sum(bag.values())
    if not total:
        return 0.0
    values = sorted(token_value(t, ctx) for t, n in bag.items() for _ in range(n))
    n = len(values)
    # P(max <= values[i]) = ((i+1)/n)^k
    expected, prev = 0.0, 0.0
    for i, val in enumerate(values):
        cdf = ((i + 1) / n) ** k
        expected += val * (cdf - prev)
        prev = cdf
    return expected


def reachable_tokens(ctx):
    """Identified, unexcavated tokens on spaces you may excavate (next to your tiles or excavations)."""
    spaces = {s["id"]: s for s in ctx.game.get("spaces", [])}
    board = ctx.game["gameOptions"].get("boardName", "")
    mine = [s["id"] for s in spaces.values()
            if (s.get("color") == ctx.me["color"] and s.get("tileType") is not None) or s.get("excavator") == ctx.me["color"]]
    near = {a for sid in mine for a in data.adjacency(board, sid)} if mine else set(spaces)
    hits = [(s["undergroundResource"], sid) for sid, s in spaces.items()
            if sid in near and s.get("undergroundResource") and not s.get("excavator")]
    return sorted(hits, key=lambda h: -token_value(h[0], ctx))


# --------------------------------------------------------------------------------------------- context

def _context(v, ctx):
    _requirements(v, ctx)
    _simulate(v, ctx)
    _milestones(v, ctx)
    _awards(v, ctx)
    _rival_triggers(v, ctx)
    if v.attack:
        _attack(v, ctx)
    if v.event_tags:
        v.flags.append("event: its tags count for planetary tracks and triggers now, but not for milestones or awards")


_REQ = re.compile(r"^(max )?(\d+) (\w+) tag")


def _card_resources(ctx, rtype=None):
    """Resources on your played cards: of one type, or {type: count} for all."""
    out = {}
    for name, c in ctx.tableau.items():
        n = c.get("resources") or 0
        t = (data.card(name) or {}).get("resource")
        if n and t:
            out[t] = out.get(t, 0) + n
    return out.get(rtype, 0) if rtype else out


def requirement_status(req, ctx, extra_tags=()):
    """(met, text) for one requirement string from cards.json, or (None, text) if not checkable."""
    anyone = req.endswith("(any player)")
    m = _REQ.match(req)
    if m:
        n, tag = int(m.group(2)), m.group(3)
        if anyone:
            have = sum((p.get("tags") or {}).get(tag, 0) for p in ctx.players) + list(extra_tags).count(tag)
        else:
            have = ctx.tags.get(tag, 0) + ctx.tags.get("wild", 0) + list(extra_tags).count(tag)
        return (have <= n if m.group(1) else have >= n), f"{tag} tags {have}/{n}" + (" (any player)" if anyone else "")
    m = re.match(r"^(max )?(temperature|oxygen|oceans|venus|tr|cities|colonies|greeneries) (-?\d+)", req)
    if m:
        n, what = int(m.group(3)), m.group(2)
        if anyone:
            have = {"cities": sum(p.get("citiesCount", 0) for p in ctx.players),
                    "colonies": sum(p.get("coloniesCount", 0) for p in ctx.players),
                    "greeneries": ctx.greeneries()}.get(what)
        else:
            have = {"temperature": ctx.temp, "oxygen": ctx.oxygen, "oceans": ctx.oceans, "venus": ctx.venus,
                    "tr": ctx.me["terraformRating"], "cities": ctx.me["citiesCount"],
                    "colonies": ctx.me["coloniesCount"], "greeneries": ctx.my_greeneries}[what]
        if have is None:
            return None, req
        return (have <= n if m.group(1) else have >= n), \
            f"{what} {have} (needs {'≤' if m.group(1) else '≥'}{n}{', any player' if anyone else ''})"
    m = re.match(r"^(max )?(undergroundTokens|corruption) (\d+)$", req)
    if m:
        n, uw = int(m.group(3)), ctx.me.get("underworldData") or {}
        have = len(uw.get("tokens") or []) if m.group(2) == "undergroundTokens" else uw.get("corruption", 0) or 0
        label = "underground tokens" if m.group(2) == "undergroundTokens" else "corruption"
        return (have <= n if m.group(1) else have >= n), f"{label} {have}/{n}"
    m = re.match(r"^(\d+) (megacredits|steel|titanium|plants|energy|heat) production$", req)
    if m:
        n, have = int(m.group(1)), ctx.me.get(_PROD_KEY[m.group(2)], 0) or 0
        return have >= n, f"{m.group(2)} production {have}/{n}"
    m = re.match(r"^floaters (\d+)$", req)
    if m:
        n, have = int(m.group(1)), _card_resources(ctx, "Floater")
        return have >= n, f"floaters {have}/{n}"
    m = re.match(r"^resourceTypes (\d+)$", req)
    if m:
        n = int(m.group(1))
        have = sum(1 for r in RESOURCES if (ctx.me.get(_STOCK_KEY[r], 0) or 0) > 0) + len(_card_resources(ctx))
        return have >= n, f"resource types {have}/{n}"
    return None, req


def custom_requirement(name, ctx):
    """(met, text) for a play condition the app checks in custom code (not in the card's requirement list), or
    None for a card without one."""
    base = data.card_name(name) or name
    corruption = {p["color"]: (p.get("underworldData") or {}).get("corruption", 0) or 0 for p in ctx.players}
    mine = corruption.get(ctx.me["color"], 0)
    if base == "Private Investigator":
        more = [ctx.names[c] for c, n in corruption.items() if n > mine]
        return bool(more), "needs a player with more corruption than you" + (f" ({', '.join(more)})" if more else ": none")
    if base == "Class-action Lawsuit":
        top = max(corruption.values()) if corruption else 0
        leaders = [c for c, n in corruption.items() if n == top]
        if len(leaders) != 1:
            return False, f"needs one player with strictly the most corruption (tied at {top})"
        who = "you" if leaders[0] == ctx.me["color"] else ctx.names[leaders[0]]
        return True, f"targets {who} ({top} corruption)" + (": it hits YOU" if who == "you" else "")
    if base == "Shipment to Earth":
        short = [f"{n} {r}" for r, n in (("plants", 3), ("steel", 3)) if (ctx.me.get(r, 0) or 0) < n]
        return not short, "needs 3 plants and 3 steel to lose" + (f" (short: {', '.join(short)})" if short else "")
    return None


def tile_space_status(name, ctx):
    """(met, text) when the card's tile must go on a kind of space the board may lack, else None. A board without
    volcanic spaces lets volcanic tiles go on any land space (as in the app)."""
    tile = ((card_behavior(name, ctx).get("behavior") or {}).get("tile") or {})
    if tile.get("on") != "volcanic":
        return None
    spaces = ctx.game.get("spaces") or []
    volcanic = [s for s in spaces if s.get("highlight") == "volcanic"]
    if not spaces:
        return None
    if not volcanic:
        return True, "this board has no volcanic spaces: the tile may go on any land space"
    free = [s["id"] for s in volcanic if s.get("tileType") is None or _is_hazard(s)]
    return bool(free), f"volcanic spaces free: {', '.join(free) if free else 'none'}"


def _is_hazard(space):
    name = data.tile_name(space["tileType"]) if space.get("tileType") is not None else ""
    return not space.get("color") and name.endswith(("Dust Storm", "Erosion"))


def stock_shortfall(stock, ctx):
    """Text for the first resource this card spends from your stock that you do not have (None if none)."""
    for res, n in stock.items():
        have = ctx.me.get(_STOCK_KEY.get(res, ""), 0) or 0
        if isinstance(n, (int, float)) and n < 0 and have + n < 0:
            return f"needs {-n:g} {res} to lose (have {have:g})"
    return None


def production_shortfall(prod, ctx):
    """Text for the first production this card lowers below what you have (None if all are legal)."""
    for res, n in prod.items():
        have = ctx.me.get(_PROD_KEY.get(res, ""), 0) or 0
        if n < 0 and have + n < (-5 if res == "megacredits" else 0):
            return f"needs {-n:g} {res} production to give up (have {have:g})"
    return None


def _package_providers(name, tag, ctx):
    """Offered opening cards other than `name` that would add a tag (their own copies of it, events left out)."""
    out = []
    for kind in ("corporation", "prelude", "project"):
        for other in (ctx.offered or {}).get(kind, []):
            info = data.card(other) or {}
            if other != name and info.get("type") != "event":
                out += [other] * (info.get("tags") or []).count(tag)
    return out


def _requirements(v, ctx):
    for req in v.info.get("req", []):
        met, text = requirement_status(req, ctx)
        m = _REQ.match(req)
        if met is False and ctx.offered and m and not m.group(1) and not req.endswith("(any player)"):
            # Opening: the corporation, preludes and cards you keep count too.
            need = int(m.group(2)) - (ctx.tags.get(m.group(3), 0) + ctx.tags.get("wild", 0))
            providers = _package_providers(v.name, m.group(3), ctx)
            if len(providers) >= need:
                v.flags.append(f"requirement {text}: met if your package includes {need} of {', '.join(providers)}")
                continue
        if met is False:
            v.flags.append(f"NOT PLAYABLE YET: {text}")
        elif met is None and "any player" not in req:
            v.manual.append(f"check requirement: {req}")
    custom = custom_requirement(v.name, ctx)
    if custom:
        v.flags.append(("NOT PLAYABLE YET: " if not custom[0] else "") + custom[1])
    space = tile_space_status(v.name, ctx)
    if space:
        v.flags.append(("NOT PLAYABLE: " if not space[0] else "") + space[1])
    short = production_shortfall(v.prod, ctx) or (None if custom else stock_shortfall(v.stock, ctx))
    if short and v.info.get("type") in PROJECT:
        v.flags.append(f"NOT PLAYABLE YET: {short}")


def _reward_value(reward, ctx):
    return {
        "1vp": VP, "2vp": 2 * VP, "3mc": 3.0, "6mc": 6.0, "any_resource": 2.0, "card": CARD, "city": CITY_TILE,
        "delegate": 1.0 if ctx.expansions.get("turmoil") else 0.0, "energy": ctx.energy, "energy_production": ctx.energy * ctx.phases, "floater": 1.0,
        "greenery": VP + (ctx.tr if ctx.left["oxygen"] else 0), "heat": ctx.heat, "heat_production": ctx.heat * ctx.phases,
        "ocean": (ctx.tr + OCEAN_ADJACENCY) if ctx.left["oceans"] else 0, "plant": ctx.plant,
        "plant_production": ctx.plant * ctx.plant_phases, "resource": 1.0, "steel": ctx.steel,
        "steel_production": ctx.steel * ctx.phases, "titanium": ctx.titanium,
        "titanium_production": ctx.titanium * ctx.phases, "tr": ctx.tr, "venus_scale": ctx.tr if ctx.left["venus"] else 0,
        "moon_road": SPECIAL_TILE, "moon_mine": SPECIAL_TILE,
    }.get(reward, 1.0)


def _simulate(v, ctx):
    """Run the card's effects through one event queue against a simulated game state.

    The card's own steps, tiles and tags are queued first (their TR is priced in _apply). Each handler
    updates the state and may queue further events: oxygen 8% queues a temperature step, 0 °C queues an
    ocean, a planetary-track reward can raise Venus or place an ocean, greenery or city. Bonus events are priced
    here, labelled with what set them off. When the queue is empty, your own cards' triggers are priced
    from everything that happened (see OWN_TRIGGERS)."""
    sim = _Sim(v, ctx)
    sim.run()
    sim.finish()


class _Sim:
    def __init__(self, v, ctx):
        self.v, self.ctx = v, ctx
        self.temp, self.oxygen, self.venus = ctx.temp, ctx.oxygen, ctx.venus
        self.left = dict(ctx.left)
        self.tracks = dict(ctx.game.get("pathfinders") or {})
        self.queue = deque()
        self.last = []
        # What actually happened, own effects and bonuses together, for triggers and rival checks.
        self.steps = {"temperature": 0, "oxygen": 0, "venus": 0}
        self.tiles = {"ocean": 0, "greenery": v.tiles["greenery"], "city": v.tiles["city"]}
        self.bonus_tr = 0.0

    @staticmethod
    def _whole(x):
        return max(0, int(x + 1e-9))

    def run(self):
        r, v = self.v.raises, self.v
        # The card's own effects go first, oceans before temperature (so its ocean fills the track before
        # a 0 °C bonus ocean is counted) and temperature before oxygen (so an oxygen bonus step only gets
        # the temperature room the card leaves).
        self.queue.append(("ocean", self._whole(r["oceans"]), None, ""))
        self.queue.append(("temperature", self._whole(r["temperature"]), None, ""))
        self.queue.append(("oxygen", self._whole(r["oxygen"]), None, ""))
        self.queue.append(("venus", self._whole(r["venus"]), None, ""))
        for track in PLANETARY:
            n = (v.persistent_tags + v.event_tags).count(track)
            if n:
                self.queue.append(("track", (track, n), None, ""))
        guard = 0
        while self.queue and guard < 200:
            kind, n, label, via = self.queue.popleft()
            getattr(self, "_" + kind)(n, label, via)
            guard += 1

    def _price(self, label, amount, tr=0):
        """Price a bonus event; label is None for the card's own effects, which _apply already priced."""
        if label:
            self.v.add(label, amount)
            self.bonus_tr += tr
            self.v.tr += tr

    def _temperature(self, n, label, via):
        ctx, v = self.ctx, self.v
        for _ in range(min(n, self.left["temperature"])):
            before, self.temp = self.temp, self.temp + 2
            self.left["temperature"] -= 1
            self.steps["temperature"] += 1
            self._price(label, ctx.tr, tr=1)
            label = label and f"bonus temperature step{via}"
            self._track_bonuses("temperature", before, self.temp, via)
            if not self.left["temperature"]:
                self.last.append("temperature")

    def _oxygen(self, n, label, via):
        ctx, v = self.ctx, self.v
        for _ in range(min(n, self.left["oxygen"])):
            before, self.oxygen = self.oxygen, self.oxygen + 1
            self.left["oxygen"] -= 1
            self.steps["oxygen"] += 1
            self._price(label, ctx.tr, tr=1)
            label = label and f"bonus oxygen step{via}"
            self._track_bonuses("oxygen", before, self.oxygen, via)
            if not self.left["oxygen"]:
                self.last.append("oxygen")

    def _ocean(self, n, label, via):
        placed = min(n, self.left["oceans"])
        self.left["oceans"] -= placed
        self.tiles["ocean"] += placed
        if placed:
            self._price(label, placed * (self.ctx.tr + OCEAN_ADJACENCY), tr=placed)
            if not self.left["oceans"]:
                self.last.append("oceans")

    def _greenery(self, n, label, via):
        # Only bonus greeneries come through here; the card's own are already in v.raises["oxygen"].
        self.tiles["greenery"] += n
        self._price(label, n * VP)
        self.queue.append(("oxygen", n, f"oxygen step from that greenery{via}", via))

    def _city(self, n, label, via):
        self.tiles["city"] += n
        self._price(label, n * CITY_TILE)

    def _venus(self, n, label, via):
        ctx, v = self.ctx, self.v
        for _ in range(min(n, self.left["venus"])):
            before = self.venus
            self.venus = data.param_level("venus", data.param_steps("venus", before) + 1)
            self.left["venus"] -= 1
            self.steps["venus"] += 1
            self._price(label, ctx.tr, tr=1)
            label = label and f"bonus Venus step{via}"
            if ctx.alt_venus:
                if self.venus > 16:
                    v.add(f"alternative Venus board: resource reward{via}", 2.0)
            else:
                self._track_bonuses("venus", before, self.venus, via)
            if not self.left["venus"]:
                self.last.append("venus")

    def _track_bonuses(self, param, before, after, via):
        """The bonuses printed on this map's `param` track that a step from `before` to `after` crosses."""
        ctx, v = self.ctx, self.v
        unit = {"temperature": "°C", "oxygen": "%", "venus": "%"}[param]
        name = {"temperature": "temperature", "oxygen": "oxygen", "venus": "Venus"}[param]
        for threshold, bonus in ctx.globals["tracks"][param]:
            if not before < threshold <= after:
                continue
            at = f"{name} reaches {threshold}{unit}"
            if bonus == "heatproduction":
                v.add(f"{at}: +1 heat production{via}", ctx.heat * ctx.phases)
            elif bonus == "plant":
                v.add(f"{at}: +1 plant production{via}", ctx.plant * ctx.plant_phases)
            elif bonus == "drawcard":
                v.add(f"{at}: draw a card{via}", CARD)
            elif bonus == "colony":
                v.add(f"{at}: build a colony{via}", COLONY)
            elif bonus == "tr":
                v.add(f"{at}: +1 TR{via}", ctx.tr)
                self.bonus_tr += 1
                v.tr += 1
            elif bonus == "ocean":
                if self.left["oceans"]:
                    self.queue.append(("ocean", 1, f"{at}: bonus ocean{via}", via))
                else:
                    v.flags.append(f"{at}{via} but oceans are full: no bonus ocean")
            elif bonus == "temperature":
                if self.left["temperature"]:
                    self.queue.append(("temperature", 1, f"{at}: bonus temperature step", f" (via {name} {threshold}{unit})"))
                else:
                    v.flags.append(f"{at} but temperature is maxed: no bonus")

    # Planetary-track rewards that change the board go back into the queue instead of a flat price.
    _CHAIN_REWARDS = {"venus_scale": "venus", "ocean": "ocean", "greenery": "greenery", "city": "city"}

    def _track(self, payload, label, via):
        track, n = payload
        ctx, v = self.ctx, self.v
        pos = self.tracks.get(track)
        if pos is None or pos < 0:
            return
        layout = data.planetary_tracks().get(track, [])
        for step in range(pos + 1, min(pos + n, len(layout) - 1) + 1):
            space = layout[step]
            mine, everyone = space.get("risingPlayer", []), space.get("everyone", [])
            if mine:
                flat = [r for r in mine if r not in self._CHAIN_REWARDS]
                label = f"{track} track {step}: you get {', '.join(mine)}"
                v.add(label, sum(_reward_value(r, ctx) for r in flat))
                for r in mine:
                    if r in self._CHAIN_REWARDS:
                        cause = f" (via {track} track {step})"
                        self.queue.append((self._CHAIN_REWARDS[r], 1, f"{track} track {step}: you get {r}", cause))
                if "tr" in flat:
                    self.bonus_tr += flat.count("tr")
                    v.tr += flat.count("tr")
            if everyone:
                each = sum(_reward_value(r, ctx) for r in everyone)
                v.add(f"{track} track {step}: everyone gets {', '.join(everyone)}", each)
                v.flags.append(f"{track} track {step} also gives each rival {', '.join(everyone)} (≈{each:.0f} M€ each)")
            if space.get("mostTags"):
                v.flags.append(f"{track} track reaches its end: most {track} tags scores {', '.join(space['mostTags'])}")
        self.tracks[track] = pos + n

    def finish(self):
        ctx, v = self.ctx, self.v
        v.sim = {**self.steps, **self.tiles}
        # Milestone and award checks run after the simulation and must include bonus tiles.
        # These counts already include the card's own tiles, so replace rather than add.
        v.tiles.update(self.tiles)
        if "temperature" in self.last:
            holders = [f"{p['name']} ({p['heat']} heat)" for p in ctx.rivals if p["heat"] >= 8]
            v.flags.append("takes the LAST temperature step" + (f": strands {', '.join(holders)}" if holders else ""))
        if "oxygen" in self.last:
            holders = [f"{p['name']} ({p['plants']} plants)" for p in ctx.rivals if p["plants"] >= 8]
            v.flags.append("takes the LAST oxygen step" + (f": later greeneries give no TR ({', '.join(holders)} holding plants)" if holders else ""))
        if "venus" in self.last:
            v.flags.append("takes the LAST Venus step")
        if "oceans" in self.last:
            v.flags.append("places the LAST ocean: ends every ocean action and ocean card for the table")
        if ctx.remaining_steps and not self.left["temperature"] and not self.left["oxygen"] and not self.left["oceans"] and \
                (not ctx.venus_counts or not self.left["venus"]):
            v.flags.append("COMPLETES TERRAFORMING: the game ends after this generation")
        # Exploitation Of Venus pays whoever raises Venus, whoever owns it.
        if self.steps["venus"]:
            for p in ctx.players:
                if any(c["name"] == "Exploitation Of Venus" for c in p.get("tableau", [])):
                    who = "your" if p["color"] == ctx.me["color"] else f"{p['name']}'s"
                    v.add(f"{who} Exploitation Of Venus pays you per Venus step", 2.0 * self.steps["venus"])
        _own_triggers(v, ctx, self)


# Your own cards that pay you when this card's effects happen. The app implements these payouts in
# custom code, so behaviors.json only names the hook; the amounts here come from the card text.
# event → what one occurrence pays: ("mc", n), a stock resource, "prod:<resource>", "card",
# "here" (a resource on the trigger card itself) or "res:<Type>" (a resource on your best card of that type).
OWN_TRIGGERS = {
    "Homeostasis Bureau": [("temperature", [("mc", 3)], "+3 M€ per temperature step")],
    "Venus Contract": [("venus", [("mc", 3)], "+3 M€ per Venus step")],
    "Aphrodite": [("venus", [("mc", 2)], "+2 M€ per Venus step")],
    "Terraforming Deal": [("tr", [("mc", 2)], "+2 M€ per TR")],
    "United Nations Mission One": [("tr", [("mc", 1)], "+1 M€ per TR")],
    "Arctic Algae": [("ocean", [("plants", 2)], "+2 plants per ocean")],
    "Lakefront Resorts": [("ocean", [("prod:megacredits", 1)], "+1 M€ production per ocean"),
                          ("tile", [("mc", 0.7)], "+1 M€ ocean adjacency per tile (about 0.7 oceans next to it)")],
    "Polaris": [("ocean", [("prod:megacredits", 1), ("mc", 4)], "+1 M€ production and 4 M€ per ocean")],
    "PolderTECH Dutch": [("ocean", [("energy", 1)], "+1 energy per ocean"), ("greenery", [("plants", 1)], "+1 plant per greenery")],
    "Herbivores": [("greenery", [("here", 1)], "+1 animal per greenery")],
    "Gordon": [("greenery", [("mc", 2)], "+2 M€ per greenery"), ("city", [("mc", 2)], "+2 M€ per city")],
    "Rover Construction": [("city", [("mc", 2)], "+2 M€ per city")],
    "Tharsis Republic": [("city", [("prod:megacredits", 1), ("mc", 3)], "+1 M€ production and 3 M€ per city")],
    "Immigrant City": [("city", [("prod:megacredits", 1)], "+1 M€ production per city")],
    "Pets": [("city", [("here", 1)], "+1 animal per city")],
    "Steelaris": [("city", [("steel", 1), ("plants", 1)], "+1 steel and 1 plant per city")],
    "Interplanetary Cinematics": [("event", [("mc", 2)], "+2 M€ per event")],
    "Media Group": [("event", [("mc", 3)], "+3 M€ per event")],
    "Martian Insurance Group": [("event", [("prod:megacredits", 1)], "+1 M€ production per event")],
    "Optimal Aerobraking": [("space_event", [("mc", 3), ("heat", 3)], "+3 M€ and 3 heat per space event")],
    "Palladin Shipping": [("space_event", [("titanium", 1)], "+1 titanium per space event")],
    "Point Luna": [("tag:earth", [("card", 1)], "draws a card per Earth tag")],
    "Space Relay": [("card:jovian", [("card", 1)], "draws a card per Jovian card")],
    "Ambient": [("card:venus", [("prod:heat", 1)], "+1 heat production per Venus card")],
    "Robin Haulings": [("card:venus", [("res:Floater", 1)], "+1 floater per Venus card")],
    "Microbiology Patents": [("card:microbe", [("prod:megacredits", 1)], "+1 M€ production per microbe card")],
    "Advertising": [("cost20", [("prod:megacredits", 1)], "+1 M€ production for a 20+ M€ card")],
    "CrediCor": [("cost20", [("mc", 4)], "+4 M€ for a 20+ M€ card or standard project")],
    "Spin-off Department": [("cost20", [("card", 1)], "draws a card for a 20+ M€ card")],
    "Sagitta Frontier Services": [("notags", [("mc", 4)], "+4 M€ for a card with no tags"),
                                  ("onetag", [("mc", 1)], "+1 M€ for a card with one tag")],
    "Standard Technology": [("standard_project", [("mc", 3)], "+3 M€ per standard project")],
    "Venusian Animals": [("tag:science", [("here", 1)], "+1 animal per science tag")],
    "Terraforming Robots": [("tag:mars", [("here", 1)], "+1 robot per Mars tag")],
    "Martian Repository": [("tag:science", [("here", 1)], "+1 data per science tag"), ("tag:mars", [("here", 1)], "+1 data per Mars tag")],
    "Martian Zoo": [("tag:earth", [("here", 1)], "+1 animal per Earth tag")],
    "Decomposers": [("tag:bio", [("here", 1)], "+1 microbe per plant, animal or microbe tag")],
    "Arklight": [("tag:plant", [("here", 1)], "+1 animal per plant tag"), ("tag:animal", [("here", 1)], "+1 animal per animal tag")],
    "Ecological Zone": [("tag:plant", [("here", 1)], "+1 animal per plant tag"), ("tag:animal", [("here", 1)], "+1 animal per animal tag")],
    "Viral Enhancers": [("tag:bio", [("plants", 1)], "+1 plant per plant, animal or microbe tag")],
    "EcoTec": [("tag:bio", [("plants", 1)], "+1 plant per plant, animal or microbe tag")],
    "GMO Contract": [("tag:bio", [("mc", 2)], "+2 M€ per plant, animal or microbe tag")],
    "Soil Bacteria": [("tag:plant", [("plants", 1)], "+1 plant per plant tag"), ("tag:microbe", [("plants", 1)], "+1 plant per microbe tag")],
    "Albedo Plants": [("tag:plant", [("heat", 3)], "+3 heat per plant tag")],
    "Saturn Systems": [("tag:jovian", [("prod:megacredits", 1)], "+1 M€ production per Jovian tag")],
}
# Effects that also pay when a rival does it (rivals draw from the same deck: each plays about as many matching
# cards as you do).


def _per_trade(n):
    """An amount worth 1/n of a free trade at your best colony tile (a resource that trades when n are spent)."""
    return lambda ctx: free_trade_value(ctx) / n


_CITY_ON_MARS = CITY_TILE + 2.0  # Mind Set Mars: a city tile with its placement bonus (about a standard resource)
_PLANET_TAGS = ("earth", "mars", "venus", "moon", "jovian")
# Corporations and preludes whose custom code is an effect fired by later plays. An amount may be a function of the
# Outlook. The assumptions are spelled out in each text.
OWN_TRIGGERS.update({
    "Splice": [("tag:microbe", [("mc", 4)], "+2 M€ to you and 2 M€ to the player per microbe tag")],
    # Each microbe tag adds a disease (−4 M€); each of your science tags removes one for 1 TR.
    "Pharmacy Union": [("tag:science", [("tr", 1)], "+1 TR per science tag (a disease removed)"),
                       ("tag:microbe", [("mc", -4)], "−4 M€ per microbe tag (a disease added)")],
    "Ringcom": [("card:jovian", [("titanium", 1)], "+1 titanium per Jovian card")],
    "Recyclon": [("tag:building", [("prod:plants", 0.5)], "+1 microbe per building tag (2 = 1 plant production)")],
    "Hecate Speditions": [(f"tag:{t}", [("mc", _per_trade(2))], f"+1 supply chain per {t} tag (2 = a trade)")
                          for t in _PLANET_TAGS],
    "Collegium Copernicus": [("tag:science", [("mc", _per_trade(3))], "+1 data per science tag (3 = a trade)")],
    "Demetron Labs": [("tag:science", [("mc", lambda ctx: 2 / 3 * best_of_value(ctx, 3))],
                       "+2 data per science tag (3 = claim the best of 3 tokens)")],
    "Vitor": [("vpcard", [("mc", 3)], "+3 M€ per card with a VP icon")],
    "Spire": [("multitag", [("mc", 2)], "+1 science (2 M€ on standard projects) per card with 2+ tags")],
    "Adhai High Orbit Constructions": [("orbital", [("mc", _orbital_value)],
                                        "+1 orbital per space card without a planet tag (2 = 1 M€ off)")],
    "Arborist Collective": [("cheapevent", [("prod:plants", 0.5), ("plants", 1)],
                             "+1 activist per event of 14 M€ or less (2 = 1 plant production and 2 plants)")],
    "The Archaic Foundation Institute": [("tag:moon", [("tr", 1 / 3)], "+1 cube per Moon tag (3 = 1 TR)")],
    "Intragen Sanctuary Headquarters": [("tag:animal", [("here", 1)], "+1 animal per animal tag")],
    "Mind Set Mars": [("tag:building", [("mc", _CITY_ON_MARS / 5)], "+1 agenda per building tag (5 = a city)")],
    "Curiosity II": [("tile", [("mc", (CARD - 2) / 2)], "pay 2 M€ for a card on about half your tiles")],
    "Mining Guild": [("tile", [("prod:steel", 0.25)], "+1 steel production on steel/titanium bonus spaces (~1 in 4)")],
    "Philares": [("tile", [("mc", 1.0)], "~½ new adjacency with a rival tile per tile: a standard resource")],
    "Soylent Seedling Systems": [("greenery", [("mc", 5)], "+1 seed (5 M€ on a greenery or plant card) per greenery")],
    "Poseidon": [("colony", [("prod:megacredits", 1)], "+1 M€ production per colony")],
    "Colony Trade Hub": [("colony", [("mc", 2)], "+2 M€ per colony")],
    "Aurorai": [("tr", [("mc", 2)], "+1 data per TR (3 M€ on standard projects; about 2 spent)")],
    "Anubis Securities": [("tr", [("mc", 1)], "+2 M€ per TR while you have the least corruption (about half the time)")],
    "Voltagon": [("token", [("energy", 2)], "+2 energy per underground token claimed")],
    "Deepwater Dome": [("token", [("mc", 1)], "+2 M€ per excavation next to an ocean (about half of them)")],
    "Manutech": [("prodmc", [("mc", 1)], "gains the resources of each production step")],
    "Suitable Infrastructure": [("prodcard", [("mc", 2)], "+2 M€ per action that raises production")],
    "Crescent Research Association": [("tag:moon", [("mc", 2)], "1 M€ off per Moon tag you have, per Moon tag")],
    "Mars Direct": [("tag:mars", [("mc", lambda ctx: 1 + deck.future_matches(ctx, "tag:mars") / 2)],
                     "1 M€ off per Mars tag you have, per Mars tag")],
})
# Effects that also pay when a rival does it: {card: [(event, payouts)]}. Rivals draw from the same deck, so each
# plays about as many matching cards as you do; global parameters are shared: rivals raise their share of the steps
# left.
RIVAL_TRIGGERS = {
    "Saturn Systems": [("tag:jovian", [("prod:megacredits", 1)])],
    "Splice": [("tag:microbe", [("mc", 2)])],
    "Pharmacy Union": [("tag:microbe", [("mc", -4)])],
    "Ringcom": [("card:jovian", [("titanium", 1)])],
    "Intragen Sanctuary Headquarters": [("tag:animal", [("here", 1)])],
    "Philares": [("tile", [("mc", 0.5)])],
    "Poseidon": [("colony", [("prod:megacredits", 1)])],
    "Colony Trade Hub": [("colony", [("mc", 2)])],
    "Lakefront Resorts": [("ocean", [("prod:megacredits", 1)])],
    "Polaris": [("ocean", [("prod:megacredits", 1)])],
    "Tharsis Republic": [("city", [("prod:megacredits", 1)])],
    "Steelaris": [("city", [("steel", 1), ("plants", 1)])],
    "Aphrodite": [("venus", [("mc", 2)])],
}
_TAG_TYPES_SKIP = ("event", "wild", "clone")
OWN_TRIGGERS["Ecological Zone:ares"] = OWN_TRIGGERS["Ecological Zone"]


def _amount(amt, ctx):
    return amt(ctx) if callable(amt) else amt


def rival_matches(ctx, event):
    """Expected times rivals fire `event` over the rest of the game: their share of the global steps left, or what
    their draws bring (as many as yours each)."""
    rivals = len(ctx.players) - 1
    param = {"ocean": "oceans", "temperature": "temperature", "oxygen": "oxygen", "venus": "venus"}.get(event)
    if param:
        left = ctx.left[param] if param != "venus" or ctx.venus_counts else \
            min(ctx.left["venus"], UNCOUNTED_VENUS_RATE * ctx.gens_left)
        return left * rivals / len(ctx.players)
    return rivals * future_matches(ctx, event)

_NO_CARD_TRIGGERS = ("Convert Plants", "Convert Heat")


def _trigger_counts(v, ctx, sim):
    """How many times each OWN_TRIGGERS event happens when this card or project is used."""
    info = v.info or {}
    tags = v.persistent_tags + v.event_tags
    project = info.get("type") in ("automated", "active", "event")
    standard = v.name in STANDARD_BEHAVIORS and v.name not in _NO_CARD_TRIGGERS
    base = info.get("cost") if project else (v.cost if standard else 0)
    counts = {
        "temperature": sim.steps["temperature"], "oxygen": sim.steps["oxygen"], "venus": sim.steps["venus"],
        "ocean": sim.tiles["ocean"], "greenery": sim.tiles["greenery"], "city": sim.tiles["city"],
        "tr": max(0.0, v.tr), "standard_project": 1 if standard else 0,
        "cost20": 1 if (project or standard) and (base or 0) >= 20 else 0,
        "colony": v.colonies, "token": v.tokens,
        "tile": sim.tiles["ocean"] + sim.tiles["greenery"] + sim.tiles["city"] + v.tiles["other"],
        "prodmc": sum(n * deck.PRODUCTION_UNIT.get(r, 1.0) for r, n in v.prod.items()
                      if isinstance(n, (int, float)) and n > 0),
    }
    counts["prodcard"] = 1 if counts["prodmc"] else 0
    if project or info.get("type") == "prelude":
        # A prelude's tags count when it is played, as a project card's do.
        vp = (data.behavior(v.name) or {}).get("vp")
        counts.update({
            "vpcard": 1 if vp is not None and not (isinstance(vp, (int, float)) and vp < 0) else 0,
            "multitag": 1 if len(tags) + (info.get("type") == "event") >= 2 else 0,
            "orbital": 1 if "space" in tags and not set(tags) & deck.PLANETARY_TAGS else 0,
            "cheapevent": 1 if info.get("type") == "event" and (info.get("cost") or 0) <= 14 else 0,
            "event": 1 if info.get("type") == "event" else 0,
            "space_event": 1 if info.get("type") == "event" and "space" in tags else 0,
            "notags": 1 if not tags else 0, "onetag": 1 if len(tags) == 1 else 0,
            "tag:bio": sum(tags.count(t) for t in ("plant", "animal", "microbe")),
        })
        for t in set(tags):
            counts[f"tag:{t}"] = tags.count(t)
            counts[f"card:{t}"] = 1
    return counts


def _payout(kind, n, owner, ctx):
    if kind == "mc":
        return n
    if kind in ("plants", "heat", "steel", "titanium", "energy"):
        return n * _unit(kind, ctx)
    if kind.startswith("prod:"):
        res = kind[5:]
        return n * _unit(res, ctx) * (ctx.plant_phases if res == "plants" else ctx.phases)
    if kind == "card":
        return n * CARD
    if kind == "tr":
        return n * ctx.tr
    if kind == "here":
        return n * _resource_vp_value(owner, ctx)
    if kind.startswith("res:"):
        return n * _best_target(ctx, kind[4:])[0]
    return 0.0


def _new_tag_types(tags, ctx):
    return sorted({t for t in tags if t not in _TAG_TYPES_SKIP and not ctx.tags.get(t)})


def _own_triggers(v, ctx, sim):
    counts = _trigger_counts(v, ctx, sim)
    v.trigger_counts = counts
    if "Aridor" in ctx.tableau and v.name != "Aridor":
        new = _new_tag_types(v.persistent_tags, ctx)
        if new:
            v.add(f"your Aridor: +1 M€ production per new tag type ({', '.join(new)})", len(new) * ctx.phases)
    for owner in ctx.tableau:
        if owner == v.name:
            continue
        for event, pays, text in OWN_TRIGGERS.get(owner, []):
            n = counts.get(event, 0)
            if n:
                v.add(f"your {owner}: {text} (×{n:g})",
                      sum(_payout(k, _amount(amt, ctx) * n, owner, ctx) for k, amt in pays))


_MILESTONE_METRICS = {
    "Terraformer": "tr", "Terraformer29": "tr", "Mayor": "cities", "Gardener": "greeneries", "Builder": "tag:building",
    "Builder7": "tag:building", "Ecologist": "tags:plant,microbe,animal", "Tycoon": "projects", "Tycoon10": "blue_green",
    "Legend": "events", "Legend4": "events", "Diversifier": "distinct", "Tactician": "reqcards", "Tactician4": "reqcards",
    "Energizer": "prod:energy", "Rim Settler": "tag:jovian", "Terran": "tag:earth", "Terran5": "tag:earth",
    "Economizer": "prod:heat", "Martian": "tag:mars", "T. Spacefarer": "tag:space", "V. Spacefarer": "tag:space",
    "Spacefarer4": "tag:space", "Architect": "tag:city", "C. Forester": "prod:plants", "Forester": "prod:plants",
    "V. Electrician": "tag:power", "Smith": "prod:steel,titanium", "Metallurgist": "prod:steel,titanium",
    "Pioneer": "colonies", "Agronomist": "tag:plant", "Engineer": "prod:energy,heat", "Researcher": "tag:science",
    "Fundraiser": "prod:megacredits", "Sponsor": "cost20", "One Giant Step": "tag:moon", "Hoverlord": "res:Floater",
    "Farmer": "res:Animal,Microbe", "Planner": "hand", "Producer": "prod:all", "Tunneler": "tokens",
}

_AWARD_METRICS = {
    "Landlord": "tiles", "Scientist": "tag:science", "Banker": "prod:megacredits", "Celebrity": "cost20",
    "Benefactor": "tr", "Contractor": "tag:building", "Cultivator": "greeneries", "Magnate": "automated",
    "Space Baron": "tag:space", "Venuphile": "tag:venus", "Full Moon": "tag:moon", "Botanist": "prod:plants",
    "Promoter": "events", "A. Manufacturer": "active", "Biologist": "tags:plant,microbe,animal", "Forecaster": "reqcards",
    "Naturalist": "prod:plants,heat", "Voyager": "tag:jovian", "Traveller": "tags:jovian,earth",
    "Manufacturer": "prod:steel,heat", "Blacksmith": "prod:steel,titanium", "Kingpin": "crime", "Administrator": "notags",
    "Constructor": "colonies+cities", "Electrician": "tag:power", "Incorporator": "cost10", "Innovator": "projects_events",
    "Investor": "tag:earth", "Metropolist": "cities", "Mogul": "prod:nonmc", "Physicist": "tags:science,space",
    "Visionary": "hand", "Miner": "stock:steel,titanium", "Thermalist": "stock:heat", "Industrialist": "stock:steel,energy",
    "Excentric": "card_resources", "Zoologist": "res:Animal", "A. Zoologist": "res:Animal,Microbe",
    "Excavator": "tokens", "A. Engineer": "prodcards",
}


def _metric_delta(metric, v, ctx):
    """How much playing this card moves one milestone/award metric for you (None = not modelled)."""
    info = v.info
    ctype = info.get("type")
    project = ctype in ("automated", "active", "event")
    if metric.startswith("tag:"):
        return v.persistent_tags.count(metric[4:])
    if metric.startswith("tags:"):
        return sum(v.persistent_tags.count(t) for t in metric[5:].split(","))
    if metric.startswith("prod:"):
        keys = metric[5:]
        if keys == "all":
            return sum(v.prod.values())
        if keys == "nonmc":
            return sum(n for r, n in v.prod.items() if r != "megacredits")
        return sum(v.prod.get(r, 0) for r in keys.split(","))
    if metric.startswith("stock:"):
        return sum(v.stock.get(r, 0) for r in metric[6:].split(","))
    if metric.startswith("res:"):
        rtype = metric[4:].split(",")
        return v.card_resources if (info.get("resource") in rtype) else None
    return {
        "tr": v.tr, "cities": v.tiles["city"], "greeneries": v.tiles["greenery"],
        "tiles": v.tiles["city"] + v.tiles["greenery"] + v.tiles["other"],
        "projects": 1 if ctype in ("automated", "active") else 0,
        "blue_green": 1 if ctype in ("automated", "active") else 0,
        "projects_events": 1 if project else 0,
        "events": 1 if ctype == "event" else 0,
        "automated": 1 if ctype == "automated" else 0,
        "active": 1 if ctype == "active" else 0,
        "reqcards": 1 if info.get("req") and ctype in ("automated", "active") else 0,
        "cost20": 1 if (info.get("cost") or 0) >= 20 and ctype in ("automated", "active") else 0,
        "cost10": 1 if (info.get("cost") or 99) <= 10 and ctype in ("automated", "active") else 0,
        "colonies": v.colonies, "colonies+cities": v.colonies + v.tiles["city"],
        "crime": (v.persistent_tags + v.event_tags).count("crime"),
        "notags": 1 if not info.get("tags") and ctype in ("automated", "active") else 0,
        "prodcards": 1 if ctype in ("automated", "active") and any(v.prod.values()) else 0,
        "distinct": len({t for t in v.persistent_tags if t not in ("event", "wild", "clone")}
                        - {t for t, n in ctx.tags.items() if n and t not in ("event",)}),
        # Every play empties a hand slot whichever card it is, so only cards drawn move the count.
        "hand": v.draws, "card_resources": v.card_resources, "tokens": v.tokens,
    }.get(metric)


def _milestones(v, ctx):
    ms = ctx.game.get("milestones") or []
    claimed = sum(1 for m in ms if m.get("playerName"))
    if claimed >= 3:
        return
    for m in ms:
        if m.get("playerName"):
            continue
        metric = _MILESTONE_METRICS.get(m["name"])
        if not metric:
            continue
        delta = _metric_delta(metric, v, ctx)
        if not delta:
            continue
        scores = {s["color"]: s for s in m.get("scores", [])}
        mine = scores.get(ctx.me["color"], {}).get("score")
        target = re.search(r"(\d+)", data.milestone_text(m["name"]) or "")
        if mine is None or not target:
            continue
        need = int(target.group(1))
        if mine < need <= mine + delta:
            v.add(f"lets you claim {m['name']} (5 VP for 8 M€)", 5 * VP - 8)
            v.flags.append(f"MILESTONE: {m['name']} {mine}→{mine + delta}/{need}: claimable after this")
        elif mine < need and mine + delta >= need - 2:
            v.flags.append(f"milestone {m['name']}: {mine}→{mine + delta} of {need}")


def _award_points(scores, n_players):
    """VP each color gets from an award, using first 5 / second 2 and the tie rules."""
    if not scores:
        return {}
    top = max(scores.values())
    firsts = [c for c, s in scores.items() if s == top]
    out = {c: 0 for c in scores}
    for c in firsts:
        out[c] = 5
    if len(firsts) == 1 and n_players > 2:
        rest = [s for c, s in scores.items() if c not in firsts]
        if rest:
            second = max(rest)
            for c, s in scores.items():
                if s == second and c not in firsts:
                    out[c] = 2
    return out


def _awards(v, ctx):
    """Funded awards this card moves you in. The value is the change in the VP you can expect from the award at
    game end (award_outlook with your count moved), not today's standings: a lead now can still change hands.
    Unfunded awards are left to the funding advice (describe_award)."""
    for a in ctx.game.get("awards") or []:
        metric = _AWARD_METRICS.get(a["name"])
        if not a.get("playerName") or not metric:
            continue
        delta = _metric_delta(metric, v, ctx)
        scores = {s["color"]: s["score"] for s in a.get("scores", [])}
        if not delta or ctx.me["color"] not in scores:
            continue
        me = ctx.me["color"]
        before = _award_points(scores, len(ctx.players))
        after_scores = dict(scores, **{me: scores[me] + delta})
        after = _award_points(after_scores, len(ctx.players))
        mine = after[me] - before[me]
        expected = _award_shift(a, delta, ctx)
        if expected is None:
            expected = mine
        if abs(expected) >= 0.05:
            v.add(f"{a['name']} award (expected at game end)", expected * VP)
        rival_changes = [f"{ctx.names[c]} {after[c] - before[c]:+d}" for c in scores if c != me and after[c] != before[c]]
        if mine or rival_changes or abs(expected) >= 0.5:
            v.flags.append(f"AWARD {a['name']} (funded): you {scores[me]}→{after_scores[me]}: you {mine:+d} VP" +
                           (f", {', '.join(rival_changes)} VP" if rival_changes else "") +
                           f" now; ≈{expected:+.1f} VP expected at game end")


def _award_shift(award, delta, ctx):
    """Change in your expected award VP at game end when your count moves by delta (None if not projectable)."""
    cache = ctx.__dict__.setdefault("_award_base", {})
    if award["name"] not in cache:
        cache[award["name"]] = award_outlook(award, ctx)
    base = cache[award["name"]]
    me = ctx.me["color"]
    moved = dict(award, scores=[dict(s, score=s["score"] + delta) if s["color"] == me else s
                                for s in award.get("scores", [])])
    shifted = award_outlook(moved, ctx)
    if base is None or shifted is None:
        return None
    return shifted["points"][me] - base["points"][me]


# --------------------------------------------------------------------------------------------- planned tableau
# The hand counts as future plays. Each project card in hand gets a chance of being played before the game
# ends (affordable with the hand's share of your money; requirement met in time at the table's pace), and
# effects that depend on future plays are priced from those chances. Every number below is an assumption
# taken from the rules or plain reasoning, not fitted to game results.

PROJECT = ("automated", "active", "event")
# Share of the money you will still have that goes to cards already in hand. The rest buys new cards
# (3 M€ each), standard projects, milestones and awards.
HAND_BUDGET_SHARE = 0.5
# Generations a hand card typically waits for money and an action before it is played.
HAND_PLAY_DELAY = 1.0
# Chance that a requirement met only by other hand cards' tags is met in time (they must be played first).
HAND_TAG_REQ_P = 0.6
# Chance for a requirement this model cannot check (greeneries, any player's cities, colonies...).
UNCHECKED_REQ_P = 0.7
# A hand effect card only fires on this card if it is played first: assume it is, half of the time.
ORDER_SHARE = 0.5
# Venus steps per generation when Venus does not count towards the game end (nobody needs to finish it).
UNCOUNTED_VENUS_RATE = 1.0
_PARAM_UNIT = {"temperature": 2, "oxygen": 1, "oceans": 1, "venus": 2}
_SELF_TRIGGER = re.compile(r"(including|incl\.) th(is|ese)", re.I)


def future_matches(ctx, event):
    """Expected number of times `event` happens from cards not drawn yet, over the rest of the game.

    Events use the _play_counts keys ("tag:earth", "card:jovian", "card" for any project card, "event",
    "space_event", "cost20", "notags", "onetag", "tag:bio") and the OWN_TRIGGERS ones ("ocean", "city", ...).
    Drawn cards are counted from the deck's composition (tfm/deck.py); tile and parameter events are 0."""
    return deck.future_matches(ctx, event)


def _kept_tags(name):
    """Tags that stay on the tableau when the card is played (an event's tags do not)."""
    info = data.card(name) or {}
    return [] if info.get("type") == "event" else list(info.get("tags", []))


def _play_counts(name):
    """How often each trigger/discount event happens when this project card is played, from the card data
    alone (bonus events from thresholds are left out)."""
    info = data.card(name) or {}
    ctype = info.get("type")
    if ctype not in PROJECT:
        return {}
    tags = info.get("tags", [])
    counts = {
        "card": 1, "event": 1 if ctype == "event" else 0,
        "space_event": 1 if ctype == "event" and "space" in tags else 0,
        "notags": 1 if not tags else 0, "onetag": 1 if len(tags) == 1 else 0,
        "cost20": 1 if (info.get("cost") or 0) >= 20 else 0,
        "tag:bio": sum(tags.count(t) for t in ("plant", "animal", "microbe")),
    }
    for t in set(tags):
        counts[f"tag:{t}"] = tags.count(t)
        counts[f"card:{t}"] = 1
    beh = card_behavior(name).get("behavior") or {}
    steps = dict(beh.get("global") or {})
    oceans = (beh["ocean"].get("count", 1) if isinstance(beh["ocean"], dict) else 1) if "ocean" in beh else 0
    counts.update({"ocean": oceans, "city": 1 if "city" in beh else 0, "greenery": 1 if "greenery" in beh else 0,
                   "temperature": steps.get("temperature", 0), "oxygen": steps.get("oxygen", 0) + ("greenery" in beh),
                   "venus": steps.get("venus", 0)})
    tr = beh.get("tr") if isinstance(beh.get("tr"), (int, float)) else 0
    counts["tr"] = tr + oceans + counts["temperature"] + counts["oxygen"] + counts["venus"]
    for event in ("multitag", "orbital", "cheapevent", "tile", "colony", "prodmc", "prodcard", "vpcard", "token"):
        counts[event] = deck._per_card(info, event, name)
    return {k: n for k, n in counts.items() if n}


def _param_rate(param, ctx):
    """Steps per generation of one parameter: the table's pace, split by the steps each parameter has left."""
    if param == "venus" and not ctx.venus_counts:
        return UNCOUNTED_VENUS_RATE if ctx.left["venus"] else 0.0
    return ctx.pace * ctx.left[param] / ctx.remaining_steps if ctx.remaining_steps else 0.0


def _within(wait, ctx):
    """Chance a play `wait` generations from now still happens: 1 before the last generation, fading over it."""
    return max(0.0, min(1.0, ctx.gens_left - wait))


def _req_timing(req, ctx, extra_tags=(), hand_tags=(), raises=None):
    """(chance, generations to wait) for one requirement string from cards.json."""
    raises = raises or {}
    m = _REQ.match(req)
    if m:
        n, tag = int(m.group(2)), m.group(3)
        have = ctx.tags.get(tag, 0) + ctx.tags.get("wild", 0) + list(extra_tags).count(tag)
        if m.group(1):
            return (1.0, 0.0) if have <= n else (0.0, math.inf)
        if have >= n:
            return 1.0, 0.0
        hand = list(hand_tags).count(tag)
        if have + hand >= n:
            return HAND_TAG_REQ_P, HAND_PLAY_DELAY
        future = future_matches(ctx, f"tag:{tag}")
        if future <= 0:
            return 0.0, math.inf
        wait = (n - have - hand) / (future / ctx.gens_left)
        return _within(wait, ctx), wait
    m = re.match(r"^(max )?(temperature|oxygen|oceans|venus) (-?\d+)", req)
    if m:
        param, n, unit = m.group(2), int(m.group(3)), _PARAM_UNIT[m.group(2)]
        have = {"temperature": ctx.temp, "oxygen": ctx.oxygen, "oceans": ctx.oceans, "venus": ctx.venus}[param] + \
            raises.get(param, 0) * unit
        rate = _param_rate(param, ctx)
        if m.group(1):
            if have > n:
                return 0.0, math.inf
            # Open until the parameter passes n; a hand card needs about HAND_PLAY_DELAY to get played.
            window = ((n - have) // unit + 1) / rate if rate else math.inf
            return min(1.0, window / HAND_PLAY_DELAY), 0.0
        if have >= n:
            return 1.0, 0.0
        wait = math.ceil((n - have) / unit) / rate if rate else math.inf
        return _within(wait, ctx), wait
    m = re.match(r"^tr (\d+)", req)
    if m:
        need = int(m.group(1)) - ctx.me["terraformRating"]
        if need <= 0:
            return 1.0, 0.0
        wait = need / (ctx.pace / len(ctx.players))  # your fair share of the table's terraforming
        return _within(wait, ctx), wait
    met, _ = requirement_status(req, ctx, extra_tags)
    if met is True:
        return 1.0, 0.0
    return UNCHECKED_REQ_P, (HAND_PLAY_DELAY if met is False else 0.0)


def _card_cost(model):
    return model.get("calculatedCost", (data.card(model["name"]) or {}).get("cost") or 0)


def _money(ctx):
    """M€ you can still spend this game: what you hold plus income at each spendable production phase.
    Steel and titanium count at full value, as if every card could use them."""
    me = ctx.me
    income = me.get("terraformRating", 20) + me.get("megacreditProduction", 0) + \
        me.get("steelProduction", 0) * ctx.steel + me.get("titaniumProduction", 0) * ctx.titanium
    held = me.get("megacredits", 0) + me.get("steel", 0) * ctx.steel + me.get("titanium", 0) * ctx.titanium
    return held + income * ctx.phases


def _hand_play(model, ctx, cards, afford, without=None, extra_tags=(), raises=None):
    """(chance, wait) that one hand card gets played, with `without`'s tags left out of the hand."""
    others = [t for c in cards if c is not model and c["name"] != without for t in _kept_tags(c["name"])]
    p, wait = afford, 0.0
    for req in (data.card(model["name"]) or {}).get("req", []):
        rp, rw = _req_timing(req, ctx, extra_tags=extra_tags, hand_tags=others, raises=raises)
        p, wait = p * rp, max(wait, rw)
    return p, wait


def planned_tableau(ctx):
    """Hand project cards as future plays: [{"name", "p", "wait", "model"}], cached on ctx.

    p is the chance the card is played before the game ends: an affordability share (HAND_BUDGET_SHARE of
    your remaining money over the hand's total cost, at most 1) times the chance each requirement is met in
    time. wait is the generations until its requirements are met."""
    if getattr(ctx, "_planned", None) is not None:
        return ctx._planned
    cards = [c for c in ctx.hand if (data.card(c["name"]) or {}).get("type") in PROJECT]
    total = sum(_card_cost(c) for c in cards)
    ctx._afford = 1.0 if total <= 0 else max(0.0, min(1.0, HAND_BUDGET_SHARE * _money(ctx) / total))
    ctx._planned = []
    for c in cards:
        p, wait = _hand_play(c, ctx, cards, ctx._afford)
        ctx._planned.append({"name": c["name"], "p": p, "wait": wait, "model": c})
    return ctx._planned


def _matches(plan, event, ctx, kept=False):
    """[(expected count, generations until it happens)] for an event over the planned plays and future draws.
    `kept` counts only tags that stay on the tableau (for VP per tag)."""
    out = []
    for pc in plan:
        if kept:
            n = _kept_tags(pc["name"]).count(event[4:]) if event.startswith("tag:") else 0
        else:
            n = _play_counts(pc["name"]).get(event, 0)
        if n and pc["p"]:
            out.append((n * pc["p"], pc["wait"] + HAND_PLAY_DELAY))
    future = future_matches(ctx, event)
    if future:
        out.append((future, ctx.gens_left / 2))  # spread evenly over the rest of the game
    return out


def _plan_payout(kind, n, owner, ctx, delay):
    """_payout for an occurrence `delay` generations from now: production pays for fewer phases."""
    if kind.startswith("prod:"):
        res = kind[5:]
        phases = ctx.plant_phases if res == "plants" else ctx.phases
        return n * _unit(res, ctx) * max(0.0, phases - delay)
    return _payout(kind, n, owner, ctx)


def _self_counts(v):
    """Events this card sets off for its own 'including this' effect."""
    counts = dict(getattr(v, "trigger_counts", {}))
    if (v.info or {}).get("type") not in PROJECT:
        # A corporation's printed tags count as played when it enters the game.
        for t in set(v.persistent_tags):
            counts[f"tag:{t}"] = v.persistent_tags.count(t)
        counts["tag:bio"] = sum(v.persistent_tags.count(t) for t in ("plant", "animal", "microbe"))
        counts["multitag"] = 1 if len(v.persistent_tags) >= 2 else 0
        counts["orbital"] = 1 if "space" in v.persistent_tags and not set(v.persistent_tags) & deck.PLANETARY_TAGS else 0
    return counts


def _discount_event(d):
    if not d.get("tag"):
        return "card"
    return f"card:{d['tag']}" if d.get("per") == "card" else f"tag:{d['tag']}"


def _plan(v, ctx, nested=False):
    """Price this card's combos with the planned tableau, as its marginal value: only pairs that involve
    this card, with this card left out of the plan, and nothing _own_triggers already priced."""
    plan = [pc for pc in planned_tableau(ctx) if pc["name"] != v.name]
    beh = data.behavior(v.name) or {}
    owned = v.name in ctx.tableau

    # This card's own effect on later plays.
    if v.name in OWN_TRIGGERS and not owned:
        self_counts = _self_counts(v) if _SELF_TRIGGER.search((v.info or {}).get("text", "")) else {}
        amount, hand, own = 0.0, 0.0, 0.0
        for event, pays, _ in OWN_TRIGGERS[v.name]:
            items = _matches(plan, event, ctx)
            if self_counts.get(event):
                items.append((self_counts[event], 0.0))
                own += self_counts[event]
            hand += sum(n for n, _ in items)
            amount += sum(_plan_payout(k, _amount(amt, ctx) * n, v.name, ctx, delay) for n, delay in items
                          for k, amt in pays)
        label = f"effect ×{hand:.1f}" + (f" (incl. {own:g} own)" if own else "") if hand else "effect: no planned matches"
        v.add_combo(label, amount, keep=True)

    for event, pays in (RIVAL_TRIGGERS.get(v.name, []) if not owned else []):
        n = rival_matches(ctx, event)
        what = f"{event[4:]} tags" if event.startswith("tag:") else f"{event[5:]} cards" if event.startswith("card:") \
            else event + ("s" if event in ("ocean", "city", "tile", "colony") else " steps")
        v.add_combo(f"rivals' {what} ×{n:.1f}",
                    sum(_plan_payout(k, _amount(amt, ctx) * n, v.name, ctx, ctx.gens_left / 2) for k, amt in pays))
    if v.name == "Aridor" and not owned:
        _aridor(v, ctx, plan)

    discount = beh.get("cardDiscount")
    if discount:
        for d in discount if isinstance(discount, list) else [discount]:
            n = sum(n for n, _ in _matches(plan, _discount_event(d), ctx))
            v.add_combo(f"discount {d.get('amount', 0)}" + (f"/{d['tag']}" if d.get("tag") else "") + f" ×{n:.1f}",
                        d.get("amount", 0) * n, keep=True)

    vp = beh.get("vp")
    if isinstance(vp, dict) and "tag" in vp:
        n = sum(n for n, _ in _matches(plan, f"tag:{vp['tag']}", ctx, kept=True))
        v.add_combo(f"{vp['tag']} tags planned ×{n:.1f}", n / (vp.get("per") or 1) * VP)

    # The reverse direction: effect cards in hand (and VP-per-tag cards anywhere) that this card feeds.
    counts = getattr(v, "trigger_counts", None) or _play_counts(v.name)
    fed, amount = [], 0.0
    holders = [{"name": n, "p": 1.0, "wait": 0.0, "tableau": True} for n in ctx.tableau if n != v.name] + plan
    for pc in holders:
        got = 0.0
        if not pc.get("tableau"):
            for event, pays, _ in OWN_TRIGGERS.get(pc["name"], []):
                n = counts.get(event, 0)
                got += sum(_plan_payout(k, _amount(amt, ctx) * n, pc["name"], ctx, pc["wait"]) for k, amt in pays) * \
                    pc["p"] * ORDER_SHARE
            d_beh = (data.behavior(pc["name"]) or {}).get("cardDiscount")
            for d in (d_beh if isinstance(d_beh, list) else [d_beh] if d_beh else []):
                got += d.get("amount", 0) * counts.get(_discount_event(d), 0) * pc["p"] * ORDER_SHARE
        h_vp = (data.behavior(pc["name"]) or {}).get("vp")
        if isinstance(h_vp, dict) and "tag" in h_vp:
            got += v.persistent_tags.count(h_vp["tag"]) / (h_vp.get("per") or 1) * VP * pc["p"]
        if got > 0.05:
            fed.append(pc["name"])
            amount += got
    if fed:
        v.add_combo("feeds " + ", ".join(fed), amount)

    if not nested:
        _unlocks(v, ctx)


def _aridor(v, ctx, plan):
    """Aridor: +1 M€ production per new tag type. Each tag type you lack is counted with the chance that a
    planned or drawn card brings it, at about the time the first one is expected."""
    types = {t for c in deck.deck(ctx.expansions) for t in c.get("tags") or []} - set(_TAG_TYPES_SKIP)
    total, n_types = 0.0, 0.0
    for t in _new_tag_types(types, ctx):
        items = _matches(plan, f"tag:{t}", ctx, kept=True) + [(future_matches(ctx, f"tag:{t}"), 0.0)]
        expected = sum(n for n, _ in items)
        if expected <= 0:
            continue
        p = 1 - math.exp(-expected)
        delay = min(ctx.gens_left, ctx.gens_left / (1 + expected))
        n_types += p
        total += p * max(0.0, ctx.phases - delay)
    v.add_combo(f"new tag types ×{n_types:.1f}", total, keep=True)
    # The extra colony tile: your fleet trades with the best tile not in play when it beats the best one now.
    models = _colony_models(ctx)
    if models:
        in_play = {c["name"] for c in models}
        now = max((trade_value(c, ctx) for c in models), default=0.0)
        new = max((trade_value({"name": n, "trackPosition": 1}, ctx) for n in _all_colonies() if n not in in_play),
                  default=0.0)
        if new > now:
            v.add_combo("extra colony tile (better trade)", (new - max(now, TRADE_COST)) * ctx.gens_left
                        if new > TRADE_COST else 0.0)


def _all_colonies():
    return list(data._load("colonies.json"))


def _unlocks(v, ctx):
    """Hand cards whose requirement this card helps meet, priced by how much earlier they can be played."""
    cards = [pc["model"] for pc in planned_tableau(ctx)]
    sim = getattr(v, "sim", None) or {}
    raises = {"temperature": sim.get("temperature", 0), "oxygen": sim.get("oxygen", 0), "venus": sim.get("venus", 0),
              "oceans": sim.get("ocean", 0)}
    for model in cards:
        name = model["name"]
        if name == v.name or not (data.card(name) or {}).get("req"):
            continue
        p0, w0 = _hand_play(model, ctx, cards, ctx._afford, without=v.name)
        p1, w1 = _hand_play(model, ctx, cards, ctx._afford, without=v.name, extra_tags=v.persistent_tags, raises=raises)
        if p1 <= p0 + 1e-9 and w1 >= w0 - 1e-9:
            continue
        y = value_card(name, ctx, model=model, nested=True)
        per_phase = sum(n * _unit(r, ctx) for r, n in y.prod.items())

        def worth(p, wait):
            return p * max(0.0, y.net - per_phase * min(wait, ctx.phases)) if p else 0.0
        gain = worth(p1, w1) - worth(p0, w0)
        if gain > 0.05:
            sooner = "" if not p0 else f" ~{w0 - w1:.1f} gen sooner"
            v.add_combo(f"unlocks {name}{sooner}", gain)


# --------------------------------------------------------------------------------------------- award funding

AWARD_FEES = (8, 14, 20)
_CARD_COUNT_METRICS = {"automated", "active", "events", "projects_events", "projects", "blue_green", "reqcards",
                       "cost20", "cost10", "notags"}
# Spread of the final score per generation left, by how easily the count moves: production rarely drops,
# counts of cards, tags and tiles grow in bursts, and stock can be spent at any time.
_SPREAD = {"narrow": 0.4, "medium": 0.8, "wide": 1.5}
# Played cards that raise a production count by themselves: (resource, rough steps per generation).
_AUTO_PRODUCTION = {
    "Botanical Experience": ("plants", 1 / 3),
    "Immigrant City": ("megacredits", 0.7),
    "Tharsis Republic": ("megacredits", 0.5),
    "Saturn Systems": ("megacredits", 0.3),
    "Poseidon": ("megacredits", 0.4),
}
_SAMPLES = 1000


def _metric_kind(metric):
    if metric.startswith("stock:") or metric == "hand":
        return "wide"
    if metric.startswith("prod:"):
        return "narrow"
    return "medium"


def _award_rate(name, metric, color, score, ctx, history):
    """Growth per generation: this seat's stored standings (up to 3 generations back) blended with the
    average so far, which counts setup (corporation, preludes) as one generation."""
    gen = ctx.generation
    if metric == "tr":
        # Everyone starts at 20 TR (14 solo); assume at least a fair share of the table's pace.
        base = 14 if len(ctx.players) == 1 else 20
        average = max((score - base) / gen, ctx.pace / len(ctx.players))
    else:
        average = score / (gen + 1)
    past = [(int(g), s[name][color]) for g, s in (history or {}).items()
            if gen - 3 <= int(g) < gen and color in (s.get(name) or {})]
    if not past:
        return average
    g0, s0 = min(past)
    weight = (gen - g0) / 3
    return weight * max(0.0, (score - s0) / (gen - g0)) + (1 - weight) * average


def _auto_growth(metric, player):
    """Steps per generation the player's played cards add to this count without further plays."""
    out = 0.0
    for card in player.get("tableau") or []:
        name = card["name"]
        if metric.startswith("res:") or metric == "card_resources":
            beh = data.behavior(name) or {}
            rtype = beh.get("resourceType")
            if not rtype or (metric.startswith("res:") and rtype not in metric[4:].split(",")):
                continue
            add = (beh.get("action") or {}).get("addResources")
            out += add if isinstance(add, (int, float)) else (0.5 if beh.get("triggers") else 0)
        elif metric.startswith("prod:") and name in _AUTO_PRODUCTION:
            res, rate = _AUTO_PRODUCTION[name]
            keys = metric[5:]
            if keys == "all" or (keys == "nonmc" and res != "megacredits") or res in keys.split(","):
                out += rate
    return out


_METRIC_TYPES = {"automated": ("automated",), "active": ("active",), "events": ("event",),
                 "projects_events": PROJECT, "projects": ("automated", "active"), "blue_green": ("automated", "active")}


def _deck_share(ctx, tags=None, types=None):
    """Share of the deck's project cards with one of `tags` (kept: not events) or of one of `types`."""
    cards = deck.deck(ctx.expansions)
    if not cards:
        return 0.0
    if tags is not None:
        hits = sum(1 for c in cards if c.get("type") != "event" and set(tags) & set(c.get("tags") or []))
    else:
        hits = sum(1 for c in cards if c.get("type") in types)
    return hits / len(cards)


def _hand_bump(metric, player, ctx):
    """(expected rise, variance) from cards in hand that could still raise a card-count or tag award: yours
    exactly, rivals' from their hand size. A rival plays the share of its hand it can pay for in the generations
    left (its M€, steel and titanium plus income, at the deck's mean card cost); each card counts with the
    deck's share of matching cards, blended with the rival's own tag mix for tag awards (hands follow strategy)."""
    tags = metric[4:].split(",") if metric.startswith("tag:") else metric[5:].split(",") if metric.startswith("tags:") else None
    if tags is None and metric not in _CARD_COUNT_METRICS:
        return 0.0, 0.0
    if player["color"] == ctx.me["color"]:
        hits = 0
        for c in ctx.hand:
            info = data.card(c["name"]) or {}
            if tags is not None:
                hits += 0 if info.get("type") == "event" else sum(info.get("tags", []).count(t) for t in tags)
            else:
                hits += 1 if info.get("type") in ("automated", "active", "event") else 0
        return 0.6 * hits, 0.24 * hits
    hand = player.get("cardsInHandNbr") or 0
    if not hand:
        return 0.0, 0.0
    if tags is None:
        share = _deck_share(ctx, types=_METRIC_TYPES.get(metric, PROJECT))
    else:
        have = player.get("tags") or {}
        total = sum(n for t, n in have.items() if t != "event")
        own = sum(have.get(t, 0) for t in tags) / total if total else 0.0
        share = (own + _deck_share(ctx, tags=tags)) / 2
    phases = max(0, ctx.gens_left - 1)
    money = (player.get("megacredits", 0) or 0) + 2 * (player.get("steel", 0) or 0) + \
        3 * (player.get("titanium", 0) or 0) + phases * ((player.get("terraformRating", 20) or 0) +
                                                         (player.get("megacreditProduction", 0) or 0))
    played = min(1.0, money / (hand * max(1.0, deck.mean_cost(ctx.expansions))))
    n = hand * played
    return n * share, n * share * (1 - share)


def award_outlook(award, ctx, history=None, seed=0):
    """Project an award's final standings and what funding it is worth to you, in VP.

    Each player's final score is their current score plus their growth rate (and what their played cards
    add by themselves) over the generations left, plus cards in hand that could still count. A small
    Monte Carlo around that, with the app's tie rules, gives the chance of finishing first or second.
    """
    metric = _AWARD_METRICS.get(award["name"])
    scores = {s["color"]: s["score"] for s in award.get("scores", [])}
    if not metric or ctx.me["color"] not in scores:
        return None
    kind = _metric_kind(metric)
    players = {p["color"]: p for p in ctx.players}
    proj = {}
    for color, score in scores.items():
        p = players.get(color, {})
        rate = _award_rate(award["name"], metric, color, score, ctx, history) + _auto_growth(metric, p)
        # Stock and hands get spent along the way, so only half their past growth is assumed to stick.
        bump, var = _hand_bump(metric, p, ctx)
        mean = score + rate * ctx.gens_left * (0.5 if kind == "wide" else 1) + bump
        sigma = math.hypot(_SPREAD[kind] * math.sqrt(ctx.gens_left) * (1 + rate) + (0.4 * score if kind == "wide" else 0),
                           math.sqrt(var))
        proj[color] = (mean, sigma)
    if max(m for m, _ in proj.values()) < 1:
        return None  # nobody has started on it: nothing to project yet
    rng = random.Random(f"{award['name']}:{seed}")
    points = {c: 0.0 for c in scores}
    first = {c: 0 for c in scores}
    second = {c: 0 for c in scores}
    for _ in range(_SAMPLES):
        final = {c: max(0, round(rng.gauss(m, s))) for c, (m, s) in proj.items()}
        for c, pts in _award_points(final, len(ctx.players)).items():
            points[c] += pts
            first[c] += pts == 5
            second[c] += pts == 2
    points = {c: n / _SAMPLES for c, n in points.items()}
    me = ctx.me["color"]
    rivals = [c for c in scores if c != me]

    def standing(c):
        p = players.get(c, {})
        return (p.get("victoryPointsBreakdown") or {}).get("total", p.get("terraformRating", 0))

    leader = max(rivals, key=standing) if rivals else None
    others = max(1, len(ctx.players) - 1)
    gift = sum(points[c] * (1 if c == leader else 1 / others) for c in rivals)
    funded = sum(1 for a in ctx.game.get("awards") or [] if a.get("playerName"))
    fee = AWARD_FEES[funded] / VP if funded < len(AWARD_FEES) else None
    rival_p1 = max((first[c] / _SAMPLES for c in rivals), default=0.0)
    fund = points[me] - gift - fee if fee is not None else None
    # A rival likely to win it will probably fund it: you then get your share without paying the fee.
    wait = rival_p1 * (points[me] - gift)
    return {
        "name": award["name"], "current": scores, "metric": metric, "kind": kind, "projection": proj, "points": points,
        "p1": first[me] / _SAMPLES, "p2": second[me] / _SAMPLES, "gift": gift, "fee": fee,
        "fund": fund, "wait": wait, "rival_p1": rival_p1,
        "top_rival": max(rivals, key=lambda c: points[c]) if rivals else None,
    }


def describe_award(o, ctx):
    """One line: projected finish for you and the two strongest rivals, then funding vs waiting."""
    me = ctx.me["color"]
    rivals = [c for c in o["current"] if c != me and o["projection"][c][0] >= 0.5]
    order = [me] + sorted(rivals, key=lambda c: -o["projection"][c][0])[:2]
    parts = [("you" if c == me else ctx.names.get(c, c)) + f" {o['current'][c]}→~{o['projection'][c][0]:.0f}"
             for c in order]
    line = ", ".join(parts) + f" · P(1st) {o['p1']:.0%}"
    if o["p2"] >= 0.05:
        line += f", P(2nd) {o['p2']:.0%}"
    if o["fund"] is not None:
        top = o["top_rival"]
        line += f" · fund EV {o['fund']:+.1f} VP"
        if top is not None and o["points"][top] >= 0.05:
            line += f" (gives {ctx.names.get(top, top)} ~{o['points'][top]:.1f})"
        line += f" · wait {o['wait']:+.1f}"
        if o["fund"] > max(o["wait"], 0) + 0.3:
            line += " → fund"
        elif o["wait"] > 0.3 and o["wait"] >= o["fund"]:
            line += " → let a rival fund"
        elif o["fund"] < 0:
            line += " → don't fund"
    if o["kind"] == "wide":
        line += " · counts stock or hand: can be spent"
    return line


def _rival_triggers(v, ctx):
    sim = getattr(v, "sim", None) or {}
    hits = {"venus": sim.get("venus", 0) > 0, "colony": v.colonies > 0, "greenery": sim.get("greenery", 0) > 0,
            "city": sim.get("city", 0) > 0, "ocean": sim.get("ocean", 0) > 0,
            "jovian": "jovian" in v.persistent_tags + v.event_tags,
            "microbe": "microbe" in v.persistent_tags + v.event_tags}
    for p in ctx.players:
        for card in p.get("tableau", []):
            trig = ANY_PLAYER_TRIGGERS.get(card["name"])
            if trig and hits.get(trig[0]):
                who = "your" if p["color"] == ctx.me["color"] else f"{p['name']}'s"
                # Priced in _Sim.finish (Exploitation) and _own_triggers (your own cards).
                if card["name"] == "Exploitation Of Venus" or (who == "your" and card["name"] in OWN_TRIGGERS):
                    continue
                v.flags.append(f"triggers {who} {card['name']}: {trig[1]}")
            elif p["color"] != ctx.me["color"]:
                beh = data.behavior(card["name"]) or {}
                if any(t.endswith("ByAnyPlayer") for t in beh.get("triggers", [])) and card["name"] not in ANY_PLAYER_TRIGGERS:
                    # The same for every card valued: noted once per output (ctx.notes), not per card.
                    note(ctx, f"{p['name']}'s {card['name']} reacts to other players' plays: check what triggers it")


def note(ctx, text):
    """A context note that applies to every card valued against ctx: printed once (see notes())."""
    notes = ctx.__dict__.setdefault("notes", [])
    if text not in notes:
        notes.append(text)


def notes(ctx):
    return [f"⚑ {n}" for n in getattr(ctx, "notes", [])]


def _attack(v, ctx):
    notes = []
    for p in ctx.rivals:
        corruption = (p.get("underworldData") or {}).get("corruption", 0)
        names = {c["name"] for c in p.get("tableau", [])}
        bits = [f"{p['plants']} plants"]
        if corruption:
            bits.append(f"{corruption} corruption (can block)")
        if "Botanical Experience" in names:
            bits.append("loses only half its plants")
        notes.append(f"{p['name']}: " + ", ".join(bits))
    v.flags.append("attack: " + "; ".join(notes))


# --------------------------------------------------------------------------------------------- output

def describe(v, where=""):
    ratio = f"{v.ratio:.2f}" if v.ratio is not None else "n/a"
    paid = f"pay {v.paid:g}" + (f" ({v.cost:g} + {BUY_COST} to buy)" if v.paid != v.cost else "")
    head = f"{v.name}{' [' + where + ']' if where else ''} — {paid} · value ≈{v.value:.0f} · net {v.net:+.0f} · ratio {ratio}"
    lines = [head]
    if v.parts:
        lines.append("    " + "; ".join(f"{label} {amt:+.1f}" for label, amt in v.parts))
    if v.combo:
        lines.append(f"    combo {sum(a for _, a in v.combo):+.1f}: " + "; ".join(f"{label} {amt:+.1f}" for label, amt in v.combo))
    for f in v.flags:
        lines.append(f"    ⚑ {f}")
    for m in v.manual:
        lines.append(f"    ? {m}")
    return lines
