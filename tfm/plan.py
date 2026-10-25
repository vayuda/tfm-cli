"""Multi-generation projection of your own economy: `tfm plan`.

`tfm value` prices one card at a time against the current tableau. This module plays the rest of the game
forward for you alone, generation by generation, so timing shows up: production bought early pays for
later cards, VP cards can wait, and saving one generation for an expensive card can beat two weak buys.

Each simulated generation:
1. Free conversions: 8 plants → greenery (1 VP, +1 oxygen step while open), 8 heat → temperature step.
2. Plays, greedily by value per M€ out of pocket. Each hand card is priced with `value.value_card` against
   an Outlook built for that generation (gens_left, global parameters, tags and tableau as simulated), and
   only once its requirement (and any production it gives up) is met. Also offered, after the current
   generation: the 4 cards drawn each generation ("avg cards"), one from each quarter of the deck ranked by
   what `value.value_card` says they return per M€ at that point of the game (`stream_profile`; their
   value shrinks near the end, when production stops paying), and standard projects. Nothing is
   played below break-even (PLAY_RATIO) except with money next generation does not need, and in the last
   generation, when leftover M€ is worth nothing.
   Before spending, the plan takes the hand card worth most net of its cost and checks whether playing it
   first (or holding money so next generation's income covers it) projects better than plain greedy order.
3. Production phase (M€ = TR + M€ production; energy becomes heat), then the other players raise the
   global parameters at the table's pace (`Outlook.pace`).

The effects the simulation tracks step by step are production, stock resources, TR, global parameters,
oceans, greeneries, cities, printed VP and VP per tag. Everything else `value_card` prices (actions,
triggers, card draws, bonus steps, ...) is banked at its M€ estimate for the generation the card is played
in and converted at 5 M€ per VP. Tag discounts are applied to the hand cards they discount, and to the
stream of future cards at the deck's rate of matching cards per generation (`deck.matches_per_gen`).

Known limits (read the numbers as a plan, not a forecast):
- Rivals are mostly ignored: they only raise the global parameters at the table pace. No attacks, no
  races for milestones or awards (milestones and awards are left out entirely), no shared VP.
- No board placement: tiles score flat (a city is CITY_VP, an ocean pays OCEAN_ADJACENCY M€), no
  placement bonuses, no adjacency planning. Pathfinders tracks, colonies trading, Turmoil are not simulated.
- Greedy play order with a one-step lookahead (each generation compares "best card first / save for it" with
  greedy for the rest of the game, not every ordering); the game length is fixed at `Outlook.gens_left` and
  does not react to the simulation finishing the parameters early.
- Card actions, draws and triggers are a lump sum at play time, not simulated per generation.
"""
import functools
import itertools
import math
import re

from . import data, deck, value

VP = value.VP
_PROFILES = {}  # stream_profile cache, shared by every simulation of the same game state
_STREAM_PROD = {}  # production part of each stream_profile pick (same keys)
_DECK_VALUES = {}  # value_card of deck cards at a later generation

# Assumptions, each derived from the rules or from value.py's own conventions (not fitted to games).
STREAM_CARDS_SEEN = deck.CARDS_SEEN_PER_GEN  # research shows 4 new cards per generation
PLAY_RATIO = 1.0           # play a card only if it returns at least the M€ it costs, until the last generation
CITY_VP = value.CITY_TILE / VP  # a city's greenery adjacency, flat (value.py prices a city tile at 5 M€)
OCEAN_ADJACENCY = value.OCEAN_ADJACENCY  # M€ from ocean adjacency on top of the ocean's TR
PRELUDES_KEPT = 2          # standard Prelude rule: keep 2 of the 4 dealt
RIVAL_VENUS_STEPS = 1.0    # Venus steps per generation from rivals when Venus does not count for game end
MAX = {"temperature": 8, "oxygen": 14, "oceans": 9, "venus": 30}  # standard maps; see _map_max
STEP = {"temperature": 2, "oxygen": 1, "oceans": 1, "venus": 2}
MIN = {"temperature": -30, "oxygen": 0, "oceans": 0, "venus": 0}
STANDARD_PROJECTS = {"Power Plant:SP": 11, "Asteroid:SP": 14, "Aquifer": 18, "Greenery": 23, "City": 25,
                     "Air Scrapping": 15}
SP_LABEL = {"Power Plant:SP": "SP power plant", "Asteroid:SP": "SP asteroid", "Aquifer": "SP aquifer",
            "Greenery": "SP greenery", "City": "SP city", "Air Scrapping": "SP air scrapping"}
STREAM = "avg cards"
# Effects the simulation applies step by step; the rest of a card's value is banked as M€.
MODELLED = ("production", "stock", "tr", "global", "ocean", "greenery", "city", "or")
# Corporations whose effect on later plays the simulation applies itself (in apply and buy), so value.py's estimate
# of it is not banked: Manutech gains the resources of each production step.
SIMULATED_TRIGGERS = ("Manutech",)
CARD_BUY_COST = {"Polyphemos": 5, "TerraLabs Research": 1}  # M€ to buy a card (value.BUY_COST otherwise)
FREE_HAND = ("Beginner Corporation",)        # its starting cards are free
# Preludes that play a card from hand (value.SETUP_BEHAVIORS' playFromHand): the simulation plays the best fitting
# kept card right after the prelude, with its discount and floaters, ignoring global requirements when it says so.
PLAY_FROM_HAND = {n: b["behavior"]["playFromHand"] for n, b in value.SETUP_BEHAVIORS.items()
                  if isinstance(b, dict) and "playFromHand" in (b.get("behavior") or {})}
RESOURCES = ("megacredits", "steel", "titanium", "plants", "energy", "heat")
PROJECT_TYPES = ("automated", "active", "event")


# --------------------------------------------------------------------------------------------- state

class _State:
    """Your economy at the start of one simulated generation's action phase."""

    def __init__(self):
        self.stock = dict.fromkeys(RESOURCES, 0.0)
        self.prod = dict.fromkeys(RESOURCES, 0.0)
        self.tr = 20
        self.tags = {}
        self.tableau = []          # dicts as in the view (name, resources)
        self.hand = []             # card names
        self.params = {}           # temperature °C, oxygen %, oceans, venus % (floats while rivals advance)
        self.max = dict(MAX)       # this map's parameter maximums
        self.cities = 0
        self.greeneries = 0
        self.card_vp = 0.0         # printed VP of cards played in the simulation
        self.banked = 0.0          # M€ estimate of effects not simulated step by step
        self.stream_vp = 0.0
        self.steel_value, self.ti_value = 2.0, 3.0
        self.plays = []            # (generation, label)
        self.reserve = 0.0         # M€ held back this generation (saving)
        self.commit = None         # (generation index, card) to play first
        self.start_cities = 0
        self.trace = []            # (generation, global parameters at the start of its action phase)
        self.stream = {}           # generation → average cards bought (fractions allowed: it is an expectation)
        self.stream_q = {}         # (generation, quarter of the deck) → share of that drawn card bought
        self.tokens = 0            # underground tokens claimed (for requirements such as Geothermal Network)
        self.worth_buying = set()  # hand cards played at break-even or better (their 3 M€ purchase included)
        self.metal_used = {}       # (generation, steel/titanium) → units spent on drawn cards

    def copy(self):
        s = _State.__new__(_State)
        s.__dict__.update(self.__dict__)
        for k in ("stock", "prod", "tags", "params", "stream", "stream_q"):
            setattr(s, k, dict(getattr(self, k)))
        s.worth_buying = set(self.worth_buying)
        s.metal_used = dict(self.metal_used)
        for k in ("tableau", "hand", "plays", "trace"):
            setattr(s, k, list(getattr(self, k)))
        return s


class Projection:
    """Result of a simulation: projected final VP, its parts and the play order."""

    def __init__(self, state, start_vp, start_tr, tag_vp, first_gen, gens):
        self.state = state
        self.tr = state.tr
        self.tag_vp = tag_vp
        self.parts = {
            "now": start_vp, "TR gained": state.tr - start_tr, "card VP": state.card_vp + tag_vp,
            "greeneries": state.greeneries, "cities": (state.cities - state.start_cities) * CITY_VP,
            "other effects": state.banked / VP, "average cards": state.stream_vp,
        }
        self.vp = sum(self.parts.values())
        self.first_gen, self.gens = first_gen, gens
        self.plays = state.plays
        self.unplayed = list(state.hand)

    def by_generation(self):
        out = {}
        for gen, label in self.plays:
            out.setdefault(gen, []).append(label)
        return out


def _map_max(game):
    """This map's parameter maximums (Amazonis Planitia's tracks are longer). Venus is simulated in 2% steps,
    so it stops at 30% here even where the map's track goes further."""
    top = dict(data.map_globals((game.get("gameOptions") or {}).get("boardName"))["max"])
    top["venus"] = min(top["venus"], 30)
    return top


def _whole(param, x):
    """A simulated (fractional) parameter as the game shows it: only completed steps count."""
    return MIN[param] + int((x - MIN[param]) / STEP[param] + 1e-9) * STEP[param]


def _modelled_part(b, ctx, tags, name):
    """The keys of one behavior this simulation applies itself, with an "or" replaced by its best option's."""
    out = {k: x for k, x in b.items() if k in MODELLED and k != "or"}
    if "or" in b:
        option = value.best_option(b["or"], ctx, tags, name) or {}
        for k, x in option.items():
            if k in MODELLED and k != "or":
                if isinstance(x, dict) and isinstance(out.get(k), dict):
                    x = {r: out[k].get(r, 0) + n for r, n in x.items()} | {r: n for r, n in out[k].items() if r not in x}
                out[k] = x
    return out


def _param_reqs_met(name, ctx):
    """False when a global-parameter requirement (minimum or maximum) is not met at ctx's parameters. Tag and
    other requirements are left out: tags come from the cards still to be played."""
    for req in (data.card(name) or {}).get("req") or []:
        words = req.split()
        if (words[1] if words[0] == "max" else words[0]) in ("temperature", "oxygen", "oceans", "venus"):
            met, _ = value.requirement_status(req, ctx)
            if met is False:
                return False
    return True


def _rival_req(req):
    """True for a requirement that depends on other players or Turmoil (not simulated here)."""
    return req.endswith("(any player)") or any(w in req for w in ("ruling", "chairman", "partyLeader"))


def _digs(tableau):
    """Underground tokens your played cards' actions claim per generation (one use each)."""
    return sum(1 for c in tableau if ((value.card_behavior(c["name"]).get("action") or {}).get("underworld") or {})
               .get("excavate"))


# --------------------------------------------------------------------------------------------- deck shares

def _modules(expansions):
    return frozenset({m for m, on in expansions.items() if on} | {"base", "corpera", "promo"})


@functools.lru_cache(maxsize=None)
def _project_cards(modules):
    return tuple(c for c in data.cards().values() if c.get("type") in PROJECT_TYPES and c.get("module") in modules)


def mean_cost(expansions):
    """Average printed cost of a project card in this game's deck (from the card data)."""
    cards = _project_cards(_modules(expansions))
    return sum(c.get("cost") or 0 for c in cards) / len(cards) if cards else 12.0


def _tag_share(expansions, tag, events=False):
    """Share of the deck's project cards with a tag (events count only for payment, not for tag effects)."""
    return _share(_modules(expansions), tag, events)


@functools.lru_cache(maxsize=None)
def _share(modules, tag, events):
    cards = _project_cards(modules)
    hits = sum(1 for c in cards if tag in (c.get("tags") or []) and (events or c.get("type") != "event"))
    return hits / len(cards) if cards else 0.0


# Draft: you keep the best card of the 4 dealt, then of the 3, 2 and 1 passed to you. If rivals valued cards as
# you do, each passed hand would hold only what they left (so your picks are just the 4 random cards in order);
# if their tastes were unrelated to yours, each passed hand is a fresh random one. Assumed halfway: rivals take
# the card you want half of the time.
DRAFT_OVERLAP = 0.5


def _pick_weights(n, draft, seen=4):
    """For each card you keep in a generation (best first), the chance it is each card of a deck of n sorted
    best first: the m-th best of `seen` random cards, or with a draft a mix of that and the best of seen-m+1."""
    out = []
    for m in range(1, seen + 1):
        k = seen - m + 1
        order = [math.comb(seen, m - 1) * (seen - m + 1) / n * (j / n) ** (m - 1) * ((n - j - 1) / n) ** (seen - m)
                 for j in range(n)]
        best = [((n - j) / n) ** k - ((n - j - 1) / n) ** k for j in range(n)]
        mix = DRAFT_OVERLAP if draft else 1.0
        weights = [mix * a + (1 - mix) * b for a, b in zip(order, best)]
        total = sum(weights) or 1.0
        out.append([w / total for w in weights])
    return out


def stream_matches(ctx, tag):
    """Expected plays with a tag among one generation's drawn cards (not yet in hand), from the deck's
    composition (tfm/deck.py). Simulated generations start with an empty hand, so this generation's draws count
    too, the last one included: the per-generation rate, not value.future_matches (which covers only the
    generations after this one)."""
    return deck.matches_per_gen(ctx, f"tag:{tag}")


# --------------------------------------------------------------------------------------------- planner

class _Planner:
    def __init__(self, view, gens=None):
        self.view = view
        base = value.Outlook(view, gens_left=gens)
        self.base = base
        self.gens = base.gens_left
        self.first_gen = base.generation
        self.n_players = len(base.players)
        # Rivals' share of the table's terraforming pace (steps per generation). The game ends when the parameters
        # are done, so the table must finish the steps left within the projected generations: at least
        # remaining / gens a generation, even when the pace so far (or the no-history default) is slower.
        self.table_pace = max(base.pace, base.remaining_steps / max(1, self.gens))
        self.rival_pace = 0.0 if self.n_players == 1 else self.table_pace * (self.n_players - 1) / self.n_players
        self.venus_on = bool(base.expansions.get("venus"))
        self.venus_counts = base.venus_counts
        # Drawn cards are priced against your tableau as it is now, without the hand's combos or awards.
        self.neutral = value.Outlook(dict(view, cardsInHand=[], game=dict(view["game"], awards=[], milestones=[])),
                                     gens_left=gens if gens is not None else base.gens_left)
        self.draft = bool(view["game"]["gameOptions"].get("draftVariant")) and self.n_players > 1
        self.building_share = _tag_share(base.expansions, "building", events=True)
        self.space_share = _tag_share(base.expansions, "space", events=True)
        self._cache = {}
        self.unbought = set()  # hand cards whose 3 M€ purchase is paid when played (best_openings' trial)
        self.preludes = [c["name"] for c in view.get("preludeCardsInHand") or []]  # what Double Down can copy

    # ---- contexts

    def ctx(self, s, i):
        """An Outlook for simulated generation i (0 = the current one)."""
        g0 = self.view["game"]
        p = s.params
        whole = {k: _whole(k, x) for k, x in p.items()}
        game = dict(g0, generation=self.first_gen + i, temperature=whole["temperature"],
                    oxygenLevel=whole["oxygen"], oceans=whole["oceans"], venusScaleLevel=whole["venus"],
                    milestones=[], awards=[], pathfinders=None)
        me0 = self.view["thisPlayer"]
        uw = dict(me0.get("underworldData") or {}, tokens=[{"token": "?"}] * int(s.tokens))
        me = dict(me0, tags=s.tags, tableau=s.tableau, terraformRating=s.tr, citiesCount=s.cities,
                  steelValue=s.steel_value, titaniumValue=s.ti_value, underworldData=uw,
                  **{value._PROD_KEY[r]: s.prod[r] for r in RESOURCES},
                  **{value._STOCK_KEY[r]: s.stock[r] for r in RESOURCES})
        players = [me if q["color"] == me0["color"] else q for q in self.view["players"]]
        return value.Outlook({"thisPlayer": me, "players": players, "game": game, "cardsInHand": [],
                              "_package_preludes": self.preludes, "_package_hand": list(s.hand),
                              "_greeneries": s.greeneries},
                             gens_left=self.gens - i)

    def valuation(self, name, s, i, ctx):
        key = (name, i, tuple(_whole(k, x) for k, x in s.params.items()), s.tr, s.cities,
               tuple(sorted(s.tags.items())), tuple(c["name"] for c in s.tableau), tuple(sorted(s.prod.items())),
               int(s.tokens), tuple(s.hand) if name in PLAY_FROM_HAND else ())
        hit = self._cache.get(key)
        if hit is None:
            v = value.value_card(name, ctx)
            hit = self._cache[key] = (v.value, v.value - self._modelled(name, v, ctx))
        return hit

    @staticmethod
    def _modelled(name, v, ctx):
        """M€ value.py gives the effects this simulation applies itself (subtracted to leave the residual)."""
        beh = value.card_behavior(name, ctx)
        tags = v.persistent_tags
        trial = value.Valuation(name)
        for key in ("behavior", "firstAction"):
            value._apply(_modelled_part(beh.get(key) or {}, ctx, tags, name), trial, ctx, tags, name)
        out = trial.value
        out += value.starting_mc(name, beh)
        vp = beh.get("vp")
        if isinstance(vp, (int, float)):
            out += vp * VP
        elif isinstance(vp, dict) and "tag" in vp:
            out += (value._resolve({"tag": vp["tag"]}, tags, ctx) // (vp.get("per") or 1)) * VP
        # Discounts are applied by the simulation (hand cards and the future-card stream), so value.py's
        # discount on future draws (in v.combo) is not banked as well.
        out += sum(amt for label, amt in v.parts + v.combo if label.startswith("discount "))
        # Aridor's production for a new tag type is applied by the simulation (apply), and so are the effects in
        # SIMULATED_TRIGGERS and the ones value.py marks as simulated (cards played from hand, buy costs, greenery
        # discounts).
        out += sum(amt for label, amt in v.parts if label.startswith("your Aridor"))
        out += sum(amt for label, amt in v.parts if any(label.startswith(f"your {c}:") for c in SIMULATED_TRIGGERS))
        if name in SIMULATED_TRIGGERS:
            out += sum(amt for label, amt in v.combo if label.startswith("effect"))
        out += v.simulated
        return out

    # ---- costs and requirements

    def discount(self, s, tags):
        total = 0
        for c in s.tableau:
            d = (data.behavior(c["name"]) or {}).get("cardDiscount")
            for one in (d if isinstance(d, list) else [d]) if d else []:
                if not one.get("tag") or one["tag"] in tags:
                    total += one.get("amount", 0)
        return total

    def card_cost(self, name, s, less=0):
        """(M€ out of pocket, steel used, titanium used) to play a hand card now (`less` M€ off, as Eccentric
        Sponsor gives)."""
        info = data.card(name) or {}
        tags = info.get("tags") or []
        cost = max(0, (info.get("cost") or 0) - self.discount(s, tags) - less) + \
            (self.buy_cost(s) if name in self.unbought else 0)
        steel = ti = 0
        if "space" in tags and s.ti_value:
            ti = min(int(s.stock["titanium"]), int(cost // s.ti_value))
            cost -= ti * s.ti_value
        if "building" in tags and s.steel_value:
            steel = min(int(s.stock["steel"]), int(cost // s.steel_value))
            cost -= steel * s.steel_value
        return cost, steel, ti

    def stream_profile(self, i):
        """Generation i's drawn cards: [(value M€, printed cost)] for each quarter of the deck ranked by value per
        M€ paid (3 M€ to buy included), priced with `value.value_card` at that point of the game. With 4 cards
        seen per generation, each quarter stands for one card a generation."""
        ctx = self.neutral
        key = (deck.names(ctx.expansions), tuple(sorted(ctx.me.get("tags", {}).items())), tuple(ctx.tableau),
               ctx.generation, ctx.temp, ctx.oxygen, ctx.oceans, ctx.venus, self.gens, i)
        hit = _PROFILES.get(key)
        if hit is None:
            later = value._later(ctx, i)
            priced = []
            for n in deck.names(ctx.expansions):
                # A card whose global requirement is met only later is bought now and played then (at that
                # generation's value), as players hold such cards.
                v = None
                for j in range(i, self.gens):
                    at = later if j == i else value._later(ctx, j)
                    if _param_reqs_met(n, at):
                        v = self._deck_value(n, j, at)
                        break
                ok = v is not None and v.value > 0
                prod = {r: x for r, x in v.prod.items() if r in RESOURCES and isinstance(x, (int, float))} if ok else {}
                priced.append((v.value if ok else 0.0, v.cost if v else (data.card(n) or {}).get("cost") or 0, prod))
            priced.sort(key=lambda vc: -vc[0] / (vc[1] + value.BUY_COST))
            hit, _STREAM_PROD[key] = [], []
            for weights in _pick_weights(len(priced), self.draft, STREAM_CARDS_SEEN):
                worth = sum(w * x for w, (x, _, _) in zip(weights, priced))
                cost = sum(w * c for w, (_, c, _) in zip(weights, priced))
                # Average production change of the pick, and the M€ value_card gave it: production is applied to
                # the simulated economy (so it pays for later cards) instead of being banked as VP.
                prod = {r: sum(w * p.get(r, 0) for w, (_, _, p) in zip(weights, priced)) for r in RESOURCES}
                prod_worth = sum(x * value._unit(r, later) * (later.plant_phases if r == "plants" else later.phases)
                                 for r, x in prod.items())
                hit.append((worth, cost))
                _STREAM_PROD[key].append((prod, prod_worth))
            _PROFILES[key] = hit
        return hit

    def _deck_value(self, name, j, ctx):
        key = (name, j, ctx.generation, ctx.temp, ctx.oxygen, ctx.oceans, ctx.venus, ctx.gens_left, tuple(ctx.tableau))
        hit = _DECK_VALUES.get(key)
        if hit is None:
            hit = _DECK_VALUES[key] = value.value_card(name, ctx, buying=True, nested=True)
        return hit

    def stream_production(self, i, q):
        """(average production change, its M€ value as priced) of the drawn card of quarter q in generation i."""
        self.stream_profile(i)
        ctx = self.neutral
        key = (deck.names(ctx.expansions), tuple(sorted(ctx.me.get("tags", {}).items())), tuple(ctx.tableau),
               ctx.generation, ctx.temp, ctx.oxygen, ctx.oceans, ctx.venus, self.gens, i)
        return _STREAM_PROD[key][q]

    def stream_cost(self, s, i, q):
        """(M€ out of pocket, steel, titanium) for the drawn card of pick q. Of the 4 cards seen, a player holding
        titanium (steel) buys the space (building) ones first, so metal pays up to the deck's share of those tags
        in the cost of all 4 seen cards each generation, spread over the cards bought."""
        profile = self.stream_profile(i)
        cost = max(0.0, profile[q][1] - self.discount(s, ()))
        gen = self.first_gen + i
        seen = sum(c for _, c in profile)
        out = []
        for res, share, unit in (("titanium", self.space_share, s.ti_value), ("steel", self.building_share, s.steel_value)):
            if not unit:
                out.append(0.0)
                continue
            budget = max(0.0, share * seen / unit - s.metal_used.get((gen, res), 0.0))
            n = min(s.stock[res], budget, cost / unit)
            cost -= n * unit
            out.append(n)
        ti, steel = out
        return cost + self.buy_cost(s), steel, ti

    @staticmethod
    def buy_cost(s):
        """M€ to buy a card into hand, as your corporation sets it."""
        return next((CARD_BUY_COST[c["name"]] for c in s.tableau if c["name"] in CARD_BUY_COST), value.BUY_COST)

    def stream_options(self, s, i):
        out = []
        for q, (worth, _) in enumerate(self.stream_profile(i) if i > 0 else []):
            cost = self.stream_cost(s, i, q)[0]
            if worth > 0 and s.stream_q.get((self.first_gen + i, q), 0.0) < 1 - 1e-6:
                out.append((worth / max(1.0, cost), "stream", q, worth, 0.0, cost))
        return out

    def playable(self, name, s, ctx, ignore_globals=False):
        info = data.card(name) or {}
        for req in info.get("req", []):
            if ignore_globals and re.match(r"^(max )?(temperature|oxygen|oceans|venus) ", req):
                continue
            met, _ = value.requirement_status(req, ctx)
            if not met and _rival_req(req) and not req.startswith("max "):
                # Rivals' tiles, tags and Turmoil are not simulated: assume they meet it by mid-game.
                met = ctx.gens_left <= self.gens - self.gens // 2
            if not met:
                return False  # unmet, or not checkable here: never assume it is met
        for check in (value.custom_requirement(name, ctx), value.tile_space_status(name, ctx)):
            if check and not check[0]:
                return False
        b = _modelled_part(value.card_behavior(name, ctx).get("behavior") or {}, ctx, info.get("tags") or [], name)
        for res, amt in (b.get("production") or {}).items():
            n = value._resolve(amt, info.get("tags") or [], ctx)
            floor = -5 if res == "megacredits" else 0
            if s.prod.get(res, 0) + n < floor:
                return False
        for res, amt in (b.get("stock") or {}).items():
            n = value._resolve(amt, info.get("tags") or [], ctx)
            if n < 0 and s.stock.get(res, 0) + n < 0:
                return False
        return True

    # ---- effects

    def play_from_hand(self, prelude, s, i):
        """A prelude's card from hand (PLAY_FROM_HAND): the fitting kept card worth most net of what it costs now
        is played at once, with the prelude's discount and floaters. Nothing happens without one (the app's fizzle)."""
        opts = PLAY_FROM_HAND[prelude]
        ctx = self.ctx(s, i)
        best = None
        for name in s.hand:
            info = data.card(name) or {}
            if (opts.get("tag") and opts["tag"] not in (info.get("tags") or [])) or \
                    (opts.get("floaterActive") and not value._floater_card(name, active=True)) or \
                    not self.playable(name, s, ctx, ignore_globals=bool(opts.get("ignoreReqs"))):
                continue
            cost, steel, ti = self.card_cost(name, s, less=opts.get("discount", 0))
            if cost > s.stock["megacredits"] + 1e-9:
                continue
            total, residual = self.valuation(name, s, i, ctx)
            floaters = opts.get("floaters", 0) * value._resource_vp_value(name, ctx) if value._floater_card(name) else 0.0
            if best is None or total + floaters - cost > best[0]:
                best = (total + floaters - cost, name, cost, steel, ti, residual + floaters)
        if best is None:
            return
        _, name, cost, steel, ti, residual = best
        s.stock["megacredits"] -= cost
        s.stock["steel"] -= steel
        s.stock["titanium"] -= ti
        s.hand.remove(name)
        s.worth_buying.add(name)
        self.apply(name, s, ctx, residual)
        s.plays.append((self.first_gen + i, f"{name} (via {prelude})"))

    def raise_param(self, s, param, steps):
        """Raise a global parameter by whole steps for you; returns the TR gained."""
        if param not in s.params or (param == "venus" and not self.venus_on):
            return 0
        room = int(round((s.max[param] - _whole(param, s.params[param])) / STEP[param]))
        done = max(0, min(int(steps), room))
        s.params[param] = min(s.max[param], s.params[param] + done * STEP[param])
        s.tr += done
        return done

    def gain_production(self, s, res, n):
        """Raise a production; Manutech also gains that many of the resource."""
        s.prod[res] += n
        if n > 0 and any(c["name"] == "Manutech" for c in s.tableau):
            s.stock[res] += n

    def apply(self, name, s, ctx, residual):
        info = data.card(name) or {}
        beh = value.card_behavior(name, ctx)
        ctype = info.get("type")
        persistent = [] if ctype == "event" else (info.get("tags") or [])
        if name in SIMULATED_TRIGGERS and ctype == "corporation":
            s.tableau.append({"name": name, "resources": 0})  # "including this": its own production counts
        for key in ("behavior", "firstAction"):
            b = _modelled_part(beh.get(key) or {}, ctx, persistent, name)
            uw = (beh.get(key) or {}).get("underworld") or {}
            ex = uw.get("excavate")
            s.tokens += (ex if isinstance(ex, (int, float)) else (ex or {}).get("count", 1) if ex else 0) + \
                ((uw.get("identify") or {}).get("claim", 0) if isinstance(uw.get("identify"), dict) else 0)
            for res, amt in (b.get("production") or {}).items():
                if res in s.prod:
                    self.gain_production(s, res, value._resolve(amt, persistent, ctx))
            for res, amt in (b.get("stock") or {}).items():
                if res in s.stock:
                    s.stock[res] += value._resolve(amt, persistent, ctx)
            if "tr" in b:
                s.tr += value._resolve(b["tr"], persistent, ctx)
            for param, steps in (b.get("global") or {}).items():
                if steps > 0:
                    self.raise_param(s, param, steps)
            if "ocean" in b:
                n = b["ocean"].get("count", 1) if isinstance(b["ocean"], dict) else 1
                placed = self.raise_param(s, "oceans", n)
                s.stock["megacredits"] += placed * OCEAN_ADJACENCY
            if "greenery" in b:
                s.greeneries += 1
                self.raise_param(s, "oxygen", 1)
            if "city" in b:
                s.cities += 1
        s.stock["megacredits"] += value.starting_mc(name, beh)
        vp = beh.get("vp")
        if isinstance(vp, (int, float)):
            s.card_vp += vp
        if ctype != "event" and name not in value.STANDARD_BEHAVIORS:
            if any(c["name"] == "Aridor" for c in s.tableau):
                self.gain_production(s, "megacredits", len(value._new_tag_types(persistent, ctx)))
            for t in persistent:
                s.tags[t] = s.tags.get(t, 0) + 1
            if not any(c["name"] == name for c in s.tableau):
                s.tableau.append({"name": name, "resources": 0})
        if beh.get("behavior", {}).get("steelValue"):
            s.steel_value += beh["behavior"]["steelValue"]
        if beh.get("behavior", {}).get("titaniumValue"):
            s.ti_value += beh["behavior"]["titaniumValue"]
        s.banked += residual

    @staticmethod
    def greenery_plants(s):
        """Plants per greenery: 8, less your played cards' greenery discount (Ecoline: 7)."""
        return 8 - sum((value.card_behavior(c["name"]).get("behavior") or {}).get("greeneryDiscount", 0) for c in s.tableau)

    def convert(self, s, gen, ctx=None):
        plants = self.greenery_plants(s)
        while s.stock["plants"] >= plants:
            s.stock["plants"] -= plants
            s.greeneries += 1
            self.raise_param(s, "oxygen", 1)
            s.plays.append((gen, "greenery (plants)"))
        helion = any(c["name"] == "Helion" for c in s.tableau)
        # Helion spends heat as M€: 8 heat go to a temperature step only while a TR is worth more than 8 M€.
        while s.stock["heat"] >= 8 and s.params["temperature"] < s.max["temperature"] and \
                not (helion and ctx is not None and ctx.tr <= 8):
            s.stock["heat"] -= 8
            self.raise_param(s, "temperature", 1)
            s.plays.append((gen, "temperature (heat)"))
        if helion:
            s.stock["megacredits"] += s.stock["heat"]
            s.stock["heat"] = 0.0

    def production(self, s):
        s.stock["heat"] += s.stock["energy"]
        s.stock["energy"] = 0
        s.stock["megacredits"] += s.tr
        for res in RESOURCES:
            s.stock[res] += s.prod[res]

    def rivals(self, s, i):
        """Other players raise the global parameters at their share of the table pace, and at least enough that
        the table (you included) keeps to the schedule that finishes terraforming in the projected generations:
        your own steps from drawn cards are banked as VP, not simulated, so rivals stand in for them."""
        params = ["temperature", "oxygen", "oceans"] + (["venus"] if self.venus_counts else [])
        left = {p: max(0.0, (s.max[p] - s.params[p]) / STEP[p]) for p in params}
        total = sum(left.values())
        if total and self.n_players > 1:
            due = total - max(0.0, self.base.remaining_steps - (i + 1) * self.table_pace)
            steps = max(self.rival_pace, due)
            for p in params:
                s.params[p] = min(s.max[p], s.params[p] + min(left[p], steps * left[p] / total) * STEP[p])
        if self.venus_on and not self.venus_counts:
            s.params["venus"] = min(s.max["venus"], s.params["venus"] + RIVAL_VENUS_STEPS * STEP["venus"])

    # ---- one generation

    def tag_blocked(self, s, ctx):
        """Hand cards held back only by your own tag requirements: {name: [(tag, count needed)]}."""
        out = {}
        for name in s.hand:
            need, other = [], False
            for req in (data.card(name) or {}).get("req", []):
                met, _ = value.requirement_status(req, ctx)
                m = value._REQ.match(req)
                if met is False and m and not m.group(1) and not req.endswith("(any player)"):
                    need.append((m.group(3), int(m.group(2))))
                elif not met:
                    other = True
            if need and not other:
                out[name] = need
        return out

    def unlock_bonus(self, name, s, i, ctx, blocked):
        """Net M€ of the hand cards that playing `name` makes playable by adding its tags (for play order and
        keep decisions only: the unlocked card's value is banked when it is played)."""
        info = data.card(name) or {}
        tags = [] if info.get("type") == "event" else info.get("tags") or []
        bonus = 0.0
        for other, need in blocked.items():
            if other != name and all(s.tags.get(t, 0) + s.tags.get("wild", 0) + tags.count(t) >= n for t, n in need):
                total, _ = self.valuation(other, s, i, ctx)
                bonus += max(0.0, total - self.card_cost(other, s)[0])
        return bonus

    def options(self, s, i, ctx):
        """Hand cards, standard projects and stream cards on offer: (ratio, kind, name, value, residual, cost)."""
        out = []
        blocked = self.tag_blocked(s, ctx)
        for name in s.hand:
            if not self.playable(name, s, ctx):
                continue
            total, residual = self.valuation(name, s, i, ctx)
            cost = self.card_cost(name, s)[0]
            unlock = self.unlock_bonus(name, s, i, ctx, blocked) if blocked else 0.0
            out.append(((total + unlock) / max(1.0, cost), "card", name, total, residual, cost))
        out += self.sp_options(s, i, ctx)
        out += self.stream_options(s, i)
        out.sort(key=lambda o: -o[0])
        return out

    def focus(self, s, i, opts):
        """The hand card worth most net of its cost, as (M€ to hold back now, (generation index, card)) when
        greedy-by-ratio might not play it first: play it first if affordable now, else save for it if next
        generation's income covers it."""
        cards = [o for o in opts if o[1] == "card" and o[0] >= PLAY_RATIO]
        if not cards:
            return None
        ratio, _, name, total, _, cost = max(cards, key=lambda o: o[3] - o[5])
        if cost <= s.stock["megacredits"]:
            return None if cards[0][2] == name else (0.0, (i, name))
        income = s.tr + s.prod["megacredits"]
        if i + 1 < self.gens and cost <= s.stock["megacredits"] + income:
            return max(0.0, cost - income), (i + 1, name)
        return None

    def generation(self, s, i, branch):
        gen = self.first_gen + i
        last = i == self.gens - 1
        ctx = self.ctx(s, i)
        s.trace.append((gen, dict(s.params)))
        self.convert(s, gen, ctx)
        if i > 0:
            # Tag discounts also apply to the cards you will draw (general ones are in the average card's cost).
            for c in s.tableau:
                d = (data.behavior(c["name"]) or {}).get("cardDiscount")
                for one in (d if isinstance(d, list) else [d]) if d else []:
                    if one.get("tag"):
                        s.stock["megacredits"] += one.get("amount", 0) * stream_matches(ctx, one["tag"])
        opts = self.options(s, i, ctx)
        if branch and not last:
            target = self.focus(s, i, opts)
            if target:
                reserve, commit = target
                saver = s.copy()
                saver.reserve, saver.commit = reserve, commit
                if self.finish(saver, i, opts).vp > self.finish(s.copy(), i, opts).vp:
                    s.reserve, s.commit = reserve, commit
        self.spend(s, i, ctx, opts, last)
        s.reserve = 0.0
        s.tokens += _digs(s.tableau)
        self.production(s)
        self.rivals(s, i)

    def spend(self, s, i, ctx, opts, last):
        if s.commit and s.commit[0] == i:
            name = s.commit[1]
            s.commit = None
            hit = [o for o in opts if o[1] == "card" and o[2] == name]
            if hit:
                opts = hit + [o for o in opts if o is not hit[0]]
        ctx = self.buy(s, i, ctx, opts, 0.0 if last else PLAY_RATIO, s.reserve)
        if not last:
            # M€ that next generation's income does not need for the drawn cards worth playing would otherwise
            # sit until the last generation, when a standard project is worth least: spend it now on the best
            # of this generation's remaining drawn cards and standard projects.
            # Hand cards worth playing that are still in hand count too: their money is saved, not spent.
            need = sum(self.stream_cost(s, i + 1, q)[0] for q, (worth, _) in enumerate(self.stream_profile(i + 1))
                       if worth >= PLAY_RATIO * self.stream_cost(s, i + 1, q)[0]) + \
                sum(o[5] for o in opts if o[1] == "card" and o[2] in s.hand and o[0] >= PLAY_RATIO) - \
                (s.tr + s.prod["megacredits"])
            # Hand cards below break-even compete here too: holding them for the last generation while the money
            # goes to a standard project that returns even less only delays their TR and income.
            cards = [o for o in opts if o[1] == "card" and o[2] in s.hand and o[0] < PLAY_RATIO]
            rest = sorted(self.sp_options(s, i, ctx) + self.stream_options(s, i) + cards, key=lambda o: -o[0])
            self.buy(s, i, ctx, rest, 0.0, s.reserve + max(0.0, need))

    def sp_options(self, s, i, ctx):
        out = []
        for name, cost in STANDARD_PROJECTS.items():
            if name == "Air Scrapping" and not self.venus_on:
                continue
            total, residual = self.valuation(name, s, i, ctx)
            if total > 0:
                out.append((total / cost, "sp", name, total, residual, cost))
        return sorted(out, key=lambda o: -o[0])

    def buy(self, s, i, ctx, opts, floor, keep):
        """Play options in order while each returns at least `floor` per M€ and leaves `keep` M€."""
        gen = self.first_gen + i
        for ratio, kind, name, total, residual, _ in opts:
            if ratio < floor or ratio <= 0:
                continue
            while True:
                if kind == "card":
                    if name not in s.hand or not self.playable(name, s, ctx):
                        break
                    cost, steel, ti = self.card_cost(name, s)
                elif kind == "sp":
                    cost, steel, ti = STANDARD_PROJECTS[name], 0, 0
                else:
                    cost, steel, ti = self.stream_cost(s, i, name)
                money = s.stock["megacredits"] - keep
                if kind == "stream":
                    room = 1.0 - s.stream_q.get((gen, name), 0.0)
                    share = min(room, money / cost) if cost > 0 else room
                    if share <= 1e-6:
                        break
                    s.stock["megacredits"] -= share * cost
                    s.stock["steel"] -= share * steel
                    s.stock["titanium"] -= share * ti
                    for res, n in (("steel", steel), ("titanium", ti)):
                        s.metal_used[(gen, res)] = s.metal_used.get((gen, res), 0.0) + share * n
                    prod, prod_worth = self.stream_production(i, name)
                    for r, x in prod.items():
                        self.gain_production(s, r, share * x)
                    s.stream_vp += share * (total - prod_worth) / VP
                    if gen not in s.stream:
                        s.plays.append((gen, STREAM))
                    s.stream[gen] = s.stream.get(gen, 0.0) + share
                    s.stream_q[(gen, name)] = s.stream_q.get((gen, name), 0.0) + share
                    break
                if cost > money + 1e-9:
                    break
                s.stock["megacredits"] -= cost
                s.stock["steel"] -= steel
                s.stock["titanium"] -= ti
                if kind == "card":
                    s.hand.remove(name)
                    if ratio >= PLAY_RATIO:
                        s.worth_buying.add(name)
                self.apply(name, s, ctx, residual)
                s.plays.append((gen, SP_LABEL.get(name, name)))
                ctx = self.ctx(s, i)
                if kind != "sp":
                    break
                # Re-price a repeated standard project once its parameter moves.
                total, residual = self.valuation(name, s, i, ctx)
                if total <= 0 or total / STANDARD_PROJECTS[name] < floor:
                    break
        return ctx

    def finish(self, s, i, opts=None):
        """Play out generation i (already priced as opts) and the rest greedily, without further saving branches."""
        ctx = self.ctx(s, i)
        last = i == self.gens - 1
        self.spend(s, i, ctx, opts if opts is not None else self.options(s, i, ctx), last)
        s.reserve = 0.0
        s.tokens += _digs(s.tableau)
        self.production(s)
        self.rivals(s, i)
        for j in range(i + 1, self.gens):
            self.generation(s, j, branch=False)
        return self.result(s)

    def run(self, s, branch=True):
        for i in range(self.gens):
            self.generation(s, i, branch)
        return self.result(s)

    def result(self, s):
        # Plants left after the final production phase still become greeneries.
        s = s.copy()
        plants = self.greenery_plants(s)
        while s.stock["plants"] >= plants:
            s.stock["plants"] -= plants
            s.greeneries += 1
            self.raise_param(s, "oxygen", 1)
        tag_vp = 0.0
        for c in s.tableau:
            vp = (data.behavior(c["name"]) or {}).get("vp")
            if isinstance(vp, dict) and "tag" in vp:
                tags = vp["tag"] if isinstance(vp["tag"], list) else [vp["tag"]]
                per = vp.get("per") or 1
                final = sum(s.tags.get(t, 0) for t in tags) + s.tags.get("wild", 0)
                start = self.start_tag_counts.get(c["name"])
                tag_vp += final // per - (start // per if start is not None else 0)
        return Projection(s, self.start_vp, self.start_tr, tag_vp, self.first_gen, self.gens)

    # ---- starting states

    def state_from_view(self, extra=(), without=()):
        me = self.view["thisPlayer"]
        g = self.view["game"]
        s = _State()
        for res in RESOURCES:
            s.stock[res] = float(me.get(value._STOCK_KEY[res], 0) or 0)
            s.prod[res] = float(me.get(value._PROD_KEY[res], 0) or 0)
        s.tr = me.get("terraformRating", 20)
        s.tags = dict(me.get("tags") or {})
        s.tableau = list(me.get("tableau") or [])
        s.cities = s.start_cities = me.get("citiesCount", 0) or 0
        s.tokens = len((me.get("underworldData") or {}).get("tokens") or [])
        s.steel_value = float(me.get("steelValue", 2))
        s.ti_value = float(me.get("titaniumValue", 3))
        s.max = _map_max(g)
        s.params = {"temperature": float(g["temperature"]), "oxygen": float(g["oxygenLevel"]),
                    "oceans": float(g["oceans"]), "venus": float(g.get("venusScaleLevel", 0) or 0)}
        hand = [c["name"] for c in (self.view.get("cardsInHand") or []) + (self.view.get("preludeCardsInHand") or [])]
        drop = set(without or ())
        s.hand = [n for n in hand if n not in drop] + [data.card_name(n) or n for n in extra or ()]
        self.start_tr = s.tr
        self.start_vp = (me.get("victoryPointsBreakdown") or {}).get("total", s.tr)
        self._remember_tags(s)
        return s

    def _remember_tags(self, s):
        self.start_tag_counts = {}
        for c in s.tableau:
            vp = (data.behavior(c["name"]) or {}).get("vp")
            if isinstance(vp, dict) and "tag" in vp:
                tags = vp["tag"] if isinstance(vp["tag"], list) else [vp["tag"]]
                self.start_tag_counts[c["name"]] = sum(s.tags.get(t, 0) for t in tags) + s.tags.get("wild", 0)


# --------------------------------------------------------------------------------------------- public API

def project(view, extra=None, without=None, gens=None, mc=0):
    """Projected final VP and play order for your own economy. `extra` adds cards to the hand, `without`
    removes hand cards, `mc` adjusts starting M€ (e.g. -3 to buy a card)."""
    planner = _Planner(view, gens)
    s = planner.state_from_view(extra, without)
    s.stock["megacredits"] += mc
    return planner.run(s)


def decision_value(view, name, buying=False, gens=None):
    """VP a card adds to the projection: projected VP with it minus without it."""
    key = data.card_name(name) or name
    hand = {c["name"] for c in view.get("cardsInHand") or []}
    if key in hand:
        return project(view, gens=gens).vp - project(view, without=[key], gens=gens).vp
    with_it = project(view, extra=[key], gens=gens, mc=-value.BUY_COST if buying else 0)
    return with_it.vp - project(view, gens=gens).vp


def opening(view, corporation, preludes=(), keep=(), gens=None, free=False, buy_on_play=False):
    """Score an opening bundle (corporation + preludes + kept project cards) by projected final VP.

    Starts from generation 1 with the game's starting parameters and 20 TR (14 solo), the corporation's
    starting M€ minus 3 M€ per kept card (none if `free`; with `buy_on_play` each card's 3 M€ is paid
    only when it is played), plays the corporation's and preludes' effects, then simulates the game."""
    planner = _Planner(view, gens)
    planner.preludes = [data.card_name(n) or n for n in preludes]
    g = view["game"]
    s = _State()
    s.tr = 14 if planner.n_players == 1 else 20
    s.max = _map_max(g)
    s.params = {"temperature": float(g["temperature"]), "oxygen": float(g["oxygenLevel"]),
                "oceans": float(g["oceans"]), "venus": float(g.get("venusScaleLevel", 0) or 0)}
    planner.start_tr, planner.start_vp = s.tr, s.tr
    planner._remember_tags(s)
    s.hand = [data.card_name(n) or n for n in keep]
    # Preludes whose effect reads your production (Industrial Complex) go last, as a player would play them.
    preludes = sorted((data.card_name(n) or n for n in preludes), key=lambda n: callable(value.CUSTOM_BEHAVIORS.get(n)))
    corporation = data.card_name(corporation) or corporation
    free = free or corporation in FREE_HAND
    if buy_on_play and not free:
        planner.unbought = set(s.hand)
    for name in [corporation] + preludes:
        name = data.card_name(name) or name
        ctx = planner.ctx(s, 0)
        _, residual = planner.valuation(name, s, 0, ctx)
        planner.apply(name, s, ctx, residual)
        s.plays.append((planner.first_gen, name))
        if name == corporation and not buy_on_play and not free:
            # The starting hand is bought with the corporation's M€, at its price, before any prelude.
            s.stock["megacredits"] -= planner.buy_cost(s) * len(s.hand)
        if name in PLAY_FROM_HAND:
            planner.play_from_hand(name, s, 0)
    return planner.run(s)


def opening_cards(view):
    """Corporations, preludes and project cards on offer in the opening decision."""
    return value.offered_cards(view)


def best_openings(view, cards=None, gens=None, top=3):
    """Rank corporation × prelude-pair bundles: [(Projection, corporation, preludes, kept cards)].

    The kept hand for each bundle is the cards its plan plays at break-even or better when every offered project
    card costs its 3 M€ only when played (so a card is kept only if it is worth buying, whenever its requirement
    lets it be played; cards played only with last-generation leftovers are not); the bundle is then rescored
    paying 3 M€ per kept card up front."""
    cards = cards or opening_cards(view)
    pres = cards["prelude"]
    pairs = list(itertools.combinations(pres, min(PRELUDES_KEPT, len(pres)))) or [()]
    ranked = []
    for corp in cards["corporation"]:
        for pair in pairs:
            trial = opening(view, corp, pair, cards["project"], gens, buy_on_play=True)
            keep = [n for n in cards["project"] if n in trial.state.worth_buying]
            ranked.append((opening(view, corp, pair, keep, gens), corp, pair, keep))
    ranked.sort(key=lambda r: -r[0].vp)
    return ranked[:top] if top else ranked


def rank_line(vp, ranked):
    """Where a bundle projected at `vp` ranks among best_openings' bundles (all of them), and their spread."""
    rank = 1 + sum(1 for r in ranked if r[0].vp > vp + 1e-9)
    best, worst = ranked[0][0].vp, ranked[-1][0].vp
    where = f"rank {rank} of {len(ranked)}" if rank <= len(ranked) else f"below all {len(ranked)}"
    return (f"{where} offered bundles (each with the kept cards its plan plays); spread ~{best:.0f} best to "
            f"~{worst:.0f} worst, this one {vp - best:+.0f} VP from the best")


# --------------------------------------------------------------------------------------------- output

def describe(p, limit=None):
    """Compact lines: projected VP with its main parts, then the play order by generation."""
    parts = " · ".join(f"{k} {v:+.0f}" if k != "now" else f"now {v:.0f}" for k, v in p.parts.items() if round(v))
    last = p.first_gen + p.gens - 1
    lines = [f"PLAN: ~{p.vp:.0f} VP at the end of generation {last} ({parts}); rivals ignored, greedy order"]
    for gen, labels in sorted(p.by_generation().items()):
        counted = []
        for label in labels:
            if label == STREAM:
                label = f"{STREAM} ×{p.state.stream[gen]:.1f}"
            if counted and counted[-1][0] == label:
                counted[-1][1] += 1
            else:
                counted.append([label, 1])
        lines.append(f"  gen {gen}: " + ", ".join(f"{label}" + (f" ×{n}" if n > 1 else "") for label, n in counted))
    if p.unplayed:
        lines.append("  never played: " + ", ".join(p.unplayed))
    return lines[:limit] if limit else lines
