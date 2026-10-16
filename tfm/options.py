"""Turn the app's `waitingFor` input tree into numbered choices, and choices back into responses.

Every choice gets a dotted id ("3", "2.4"). Nested `or` menus become dotted prefixes, and inputs
that pick exactly one thing (a card, space, player, colony, resource) are expanded so each pick
is its own id. Inputs that need more (amounts, multi-card picks, payments) take values on the
command line.
"""
import hashlib
import json
import re

from . import data
from .client import TfmError
from .text import card_line, message

SPENDABLE = [
    "megacredits", "heat", "steel", "titanium", "plants", "microbes", "floaters",
    "lunaArchivesScience", "spireScience", "seeds", "auroraiData", "graphene", "kuiperAsteroids",
]
UNIT_KEYS = ["megacredits", "steel", "titanium", "plants", "energy", "heat"]
ALIASES = {
    "mc": "megacredits", "m": "megacredits", "megacredit": "megacredits", "st": "steel",
    "ti": "titanium", "pl": "plants", "plant": "plants", "en": "energy", "he": "heat",
    "data": "auroraiData", "science": "spireScience", "asteroids": "kuiperAsteroids", "seed": "seeds",
    "microbe": "microbes", "floater": "floaters",
}
CARD_RESOURCE_VALUE = {"microbes": 2, "floaters": 3, "seeds": 5, "auroraiData": 3, "graphene": 4,
                       "spireScience": 2, "lunaArchivesScience": 1, "kuiperAsteroids": 1}
EXPAND_LIMIT = 60


class Leaf:
    def __init__(self, id, label, path, node, fixed=None, hint=None):
        self.id, self.label, self.path, self.node, self.fixed, self.hint = id, label, path, node, fixed, hint


def fingerprint(waiting_for):
    return hashlib.sha1(json.dumps(waiting_for, sort_keys=True).encode()).hexdigest()[:12]


class Context:
    """What option rendering needs to know about the game."""

    def __init__(self, view, seen=None, full=False):
        self.view = view
        self.me = view["thisPlayer"]
        self.names = {p["color"]: p["name"] for p in view["players"]}
        self.seen = seen if seen is not None else set()
        self.full = full
        game = view["game"]
        self.board_name = game["gameOptions"]["boardName"]
        self.spaces = {s["id"]: s for s in game["spaces"]}

    def show_text(self, key):
        """True when this card/milestone text has not been shown to this seat yet."""
        if self.full or key not in self.seen:
            self.seen.add(key)
            return True
        return False


# ---------------------------------------------------------------- listing

def leaves(waiting_for, ctx):
    if waiting_for is None:
        return []
    out = []
    _expand(waiting_for, ctx, [], [], "", out, top=True)
    return out


def _title(node, ctx):
    return message(node.get("title"), ctx.names).strip() or {"initialCards": "Choose corporation, preludes and cards"}.get(node["type"], node["type"])


def _join(prefix, label):
    return f"{prefix} › {label}" if prefix else label


def _expand(node, ctx, ids, path, prefix, out, top=False):
    t = node["type"]
    title = _title(node, ctx)

    def add(label, fixed=None, sub_ids=None, hint=None):
        out.append(Leaf(".".join(map(str, sub_ids if sub_ids is not None else ids)) or "1", label, path, node, fixed, hint))

    if t == "or":
        child_prefix = prefix if top else _join(prefix, title)
        for i, child in enumerate(node["options"]):
            _expand(child, ctx, ids + [i + 1], path + [i], child_prefix, out)
        return
    label_prefix = _join(prefix, title)

    if t == "projectCard":
        n = 0
        for card in node["cards"]:
            if card.get("isDisabled"):
                continue
            n += 1
            pay, short = default_payment(card, node, ctx)
            cost = card.get("calculatedCost", 0)
            note = f"pay {_pay_str(pay)}" if short == 0 else f"CAN'T AFFORD ({short} MC short)"
            text = card_line(card, with_text=ctx.show_text(card["name"]))
            add(f"{_join(label_prefix, text)} [{cost} MC → {note}]", card["name"], ids + [n])
        return
    if t == "card" and node.get("min") == 1 and node.get("max") == 1 and len(node["cards"]) <= EXPAND_LIMIT:
        for j, card in enumerate(node["cards"]):
            if card.get("isDisabled"):
                continue
            text = card_line(card, with_text=ctx.show_text(card["name"]))
            add(_join(label_prefix, text), card["name"], ids + [j + 1])
        return
    if t == "space" and len(node["spaces"]) <= EXPAND_LIMIT:
        for j, sid in enumerate(node["spaces"]):
            add(_join(label_prefix, describe_space(sid, ctx)), sid, ids + [j + 1])
        return
    if t == "player":
        for j, color in enumerate(node["players"]):
            add(_join(label_prefix, ctx.names.get(color, color)), color, ids + [j + 1])
        return
    if t == "colony":
        for j, col in enumerate(node["coloniesModel"]):
            add(_join(label_prefix, describe_colony(col, ctx)), col["name"], ids + [j + 1])
        return
    if t == "resource":
        for j, res in enumerate(node["include"]):
            add(_join(label_prefix, res), res, ids + [j + 1])
        return
    if t == "option":
        add(label_prefix)
        return
    if t == "and":
        combos = _and_combos(node, ctx)
        if combos is not None:
            for j, (label, responses) in enumerate(combos):
                add(_join(label_prefix, label), responses, ids + [j + 1])
            return
    add(label_prefix, hint=needs(node, ctx))


def _choices(node, ctx):
    """All (label, response) picks for an input with a small finite set of answers, else None."""
    t = node["type"]
    title = _title(node, ctx)
    if t == "option":
        return [(title, {"type": "option"})]
    if t == "or":
        out = []
        for i, child in enumerate(node["options"]):
            sub = _choices(child, ctx)
            if sub is None:
                return None
            out.extend((label, {"type": "or", "index": i, "response": r}) for label, r in sub)
        return out
    if t == "card" and node.get("min") == 1 and node.get("max") == 1:
        return [(c["name"], {"type": "card", "cards": [c["name"]]}) for c in node["cards"] if not c.get("isDisabled")]
    if t == "space":
        return [(describe_space(sid, ctx), {"type": "space", "spaceId": sid}) for sid in node["spaces"]]
    if t == "player":
        return [(ctx.names.get(c, c), {"type": "player", "player": c}) for c in node["players"]]
    if t == "colony":
        return [(describe_colony(c, ctx), {"type": "colony", "colonyName": c["name"]}) for c in node["coloniesModel"]]
    if t == "resource":
        return [(r, {"type": "resource", "resource": r}) for r in node["include"]]
    return None


def _and_combos(node, ctx):
    parts = [_choices(o, ctx) for o in node["options"]]
    if any(p is None for p in parts):
        return None
    total = 1
    for p in parts:
        total *= len(p)
    if total == 0 or total > EXPAND_LIMIT:
        return None
    combos = [([], [])]
    for p in parts:
        combos = [(labels + [label], resps + [r]) for labels, resps in combos for label, r in p]
    return [(" + ".join(labels), resps) for labels, resps in combos]


def needs(node, ctx):
    """Describe the values `tfm play ID ...` expects for an input."""
    t = node["type"]
    if t == "card":
        names = []
        for c in node["cards"]:
            names.append(card_line(c, with_text=ctx.show_text(c["name"])))
        lo, hi = node.get("min", 0), node.get("max", len(node["cards"]))
        return f"VALUE = comma-separated card names, pick {lo}-{hi}; cards:\n      - " + "\n      - ".join(names)
    if t == "amount":
        return f"VALUE = number {node['min']}-{node['max']}"
    if t == "payment":
        return f"optional VALUE = payment like mc=5,heat=3 for {node['amount']} MC; default auto"
    if t == "and" and _all_amounts(node):
        parts = ", ".join(f"{_title(o, ctx)} {o['min']}-{o['max']}" for o in node["options"])
        return f"VALUE = amounts by name, e.g. {_title(node['options'][0], ctx)}=1 (parts: {parts}; unnamed parts are 0)"
    if t in ("and", "initialCards"):
        parts = [f"VALUE{i + 1}: {_title(o, ctx)} — {needs(o, ctx)}" for i, o in enumerate(node["options"])]
        return "one quoted VALUE per part, in order:\n    " + "\n    ".join(parts)
    if t == "resources":
        return f"VALUE = units totalling {node['count']}, e.g. plants=2,steel=1"
    if t == "productionToLose":
        pp = node["payProduction"]
        me = ctx.me
        prod = {k: me[f] for k, f in (("megacredits", "megacreditProduction"), ("steel", "steelProduction"),
                ("titanium", "titaniumProduction"), ("plants", "plantProduction"), ("energy", "energyProduction"),
                ("heat", "heatProduction"))}
        return (f"VALUE = production steps totalling {pp['cost']}, e.g. megacredits={pp['cost']} "
                f"(your production: {_units_str(prod)}; M€ production may drop to -5)")
    if t == "claimedUndergroundToken":
        toks = ", ".join(f"{i}:{tk}" for i, tk in enumerate(node.get("tokens", [])))
        return f"VALUE = comma-separated token indexes ({node['min']}-{node['max']}): {toks}"
    if t == "aresGlobalParameters":
        return "VALUE = lowOceanDelta=0,highOceanDelta=0,temperatureDelta=0,oxygenDelta=0 (each -1..1)"
    if t == "space":
        return "VALUE = space id: " + ", ".join(node["spaces"])
    if t in ("player", "delegate"):
        return "VALUE = player name: " + ", ".join(ctx.names.get(c, c) for c in node["players"])
    if t == "colony":
        return "VALUE = colony name: " + ", ".join(c["name"] for c in node["coloniesModel"])
    if t == "resource":
        return "VALUE = one of " + ", ".join(node["include"])
    if t == "party":
        return "VALUE = party: " + ", ".join(node["parties"])
    if t == "globalEvent":
        return "VALUE = global event: " + ", ".join(node["globalEventNames"])
    if t == "deltaProject":
        return "VALUE = step: " + ", ".join(map(str, node["validSteps"]))
    if t == "option":
        return "no value"
    if t == "or":
        return "VALUE = number of one of: " + "; ".join(f"{i + 1}={_title(o, ctx)}" for i, o in enumerate(node["options"]))
    if t == "projectCard":
        return "VALUE = card name; optional second VALUE = payment"
    return f"unsupported input type '{t}' — do this step in the web UI"


def describe_space(sid, ctx):
    space = ctx.spaces.get(sid, {})
    parts = [f"space {sid}" + (f" [{space['a9']}]" if space.get("a9") else "")]
    if space.get("spaceType") not in (None, "land"):
        parts.append(space["spaceType"])
    if space.get("bonus"):
        parts.append("bonus " + ",".join(data.bonus_name(b) for b in space["bonus"]))
    if space.get("undergroundResource"):
        parts.append(f"underground {space['undergroundResource']}")
    if space.get("tileType") is not None:
        covered = data.tile_name(space["tileType"])
        steps = data.HAZARD_STEPS.get(covered)
        if steps:
            parts.append(f"COVERS {covered}: costs {8 * steps} M€ extra, gives +{steps} TR")
    adj = []
    empty = 0
    hazard_loss = 0
    for aid in data.adjacency(ctx.board_name, sid):
        a = ctx.spaces.get(aid, {})
        if a.get("tileType") is None:
            empty += 1
            continue
        owner = a.get("color")
        who = "your" if owner == ctx.me["color"] else ctx.names.get(owner, "") if owner else ""
        adj.append(f"{who} {data.tile_name(a['tileType'])}".strip())
        hazard_loss += data.HAZARD_STEPS.get(data.tile_name(a["tileType"]), 0)
    if adj:
        parts.append("adj: " + ", ".join(adj))
    if hazard_loss:
        parts.append(f"NEXT TO HAZARDS: unless this is an ocean, lose {hazard_loss} production of your choice (2 from one type per severe hazard)")
    parts.append(f"{empty} empty adj")
    return " · ".join(parts)


def describe_colony(col, ctx):
    info = data.colony(col["name"]) or {}
    qty = info.get("tradeQty")
    trade = data.colony_trade(col["name"])
    pos = col.get("trackPosition")
    if isinstance(qty, list) and pos is not None and pos < len(qty):
        trade = re.sub(r"\bn\b", str(qty[pos]), trade)
    owners = ", ".join(ctx.names.get(c, c) for c in col.get("colonies", [])) or "none"
    s = f"{col['name']} (trade: {trade}; colonies: {owners}; colony bonus: {info.get('colonyBonus', '?')}"
    if col.get("visitor"):
        s += f"; fleet there: {ctx.names.get(col['visitor'], col['visitor'])}"
    return s + ")"


def _pay_str(pay):
    short = {"megacredits": "MC", "steel": "St", "titanium": "Ti", "heat": "heat", "plants": "Pl"}
    s = " + ".join(f"{v} {short.get(k, k)}" for k, v in pay.items() if v)
    return s or "0"


def _units_str(u):
    return ", ".join(f"{k} {v}" for k, v in u.items() if v) or "none"


# ---------------------------------------------------------------- payment

def allowed_payment(card_model, node, ctx):
    """Which spendable resources can pay for this card (mirrors the web client's rules)."""
    info = data.card(card_model["name"]) or {}
    tags = info.get("tags", [])
    opts = node.get("paymentOptions", {})
    allow = {"megacredits": True, "heat": bool(opts.get("heat"))}
    can = card_model.get("standardProjectCanPayWith")
    last_resort = ctx.me.get("lastCardPlayed") == "Last Resort Ingenuity"
    if can is not None:
        allow.update(steel=bool(can.get("steel")), titanium=bool(can.get("titanium") or opts.get("lunaTradeFederationTitanium")),
                     seeds=bool(can.get("seeds")), kuiperAsteroids=bool(can.get("kuiperAsteroids")),
                     auroraiData=True, spireScience=True)
    else:
        allow.update(
            steel="building" in tags or last_resort,
            titanium="space" in tags or last_resort or bool(opts.get("lunaTradeFederationTitanium")),
            plants="building" in tags and bool(opts.get("plants")),
            microbes="plant" in tags, seeds="plant" in tags, floaters="venus" in tags,
            lunaArchivesScience="moon" in tags, graphene="space" in tags or "city" in tags,
        )
    return {k for k, v in allow.items() if v}


def _available(ctx, node, card_model=None):
    me = ctx.me
    reserve = (card_model or {}).get("reserveUnits") or {}
    avail = {
        "megacredits": me["megacredits"],
        "heat": me["heat"] - reserve.get("heat", 0),
        "steel": me["steel"] - reserve.get("steel", 0),
        "titanium": me["titanium"] - reserve.get("titanium", 0),
        "plants": me["plants"] - reserve.get("plants", 0),
    }
    for res in CARD_RESOURCE_VALUE:
        avail[res] = node.get(res, 0) or 0
    return {k: max(v, 0) for k, v in avail.items()}


def _values(ctx, card_model=None):
    titanium = ctx.me["titaniumValue"]
    info = data.card(card_model["name"]) if card_model else None
    if card_model and not card_model.get("standardProjectCanPayWith") and "space" not in (info or {}).get("tags", []) \
            and ctx.me.get("lastCardPlayed") != "Last Resort Ingenuity":
        titanium -= 1  # Luna Trade Federation rate for non-space cards
    return {"megacredits": 1, "heat": 1, "steel": ctx.me["steelValue"], "titanium": titanium, "plants": 3, **CARD_RESOURCE_VALUE}


def default_payment(card_model, node, ctx):
    """Spend metal first without overpaying, then M€, then heat. Returns (payment, shortfall)."""
    cost = card_model.get("calculatedCost", 0) or 0
    allow = allowed_payment(card_model, node, ctx)
    return _greedy(cost, allow, _available(ctx, node, card_model), _values(ctx, card_model))


def _greedy(cost, allow, avail, values):
    pay = {}
    remaining = cost
    for res in ("titanium", "steel"):
        if res in allow and avail.get(res) and remaining > 0:
            n = min(avail[res], remaining // values[res])
            if n:
                pay[res] = n
                remaining -= n * values[res]
    mc = min(avail["megacredits"], remaining)
    if mc:
        pay["megacredits"] = mc
    remaining -= mc
    if remaining > 0 and "heat" in allow:
        h = min(avail["heat"], remaining)
        if h:
            pay["heat"] = h
            remaining -= h
    if remaining > 0:  # top up with one more metal unit, accepting a small overpay
        for res in ("steel", "titanium"):
            if res in allow and avail.get(res, 0) > pay.get(res, 0):
                extra = min(avail[res] - pay.get(res, 0), -(-remaining // values[res]))
                pay[res] = pay.get(res, 0) + extra
                remaining -= extra * values[res]
                if remaining <= 0:
                    break
    return pay, max(remaining, 0)


def _check_payment(payment, cost, allow, avail, values):
    total = 0
    for res, n in payment.items():
        if not n:
            continue
        if n < 0:
            raise TfmError(f"{res} cannot be negative")
        if res not in allow:
            raise TfmError(f"{res} cannot pay for this; allowed: {', '.join(sorted(allow))}")
        if n > avail.get(res, 0):
            raise TfmError(f"you only have {avail.get(res, 0)} {res} available")
        total += n * values.get(res, 1)
    if total < cost:
        raise TfmError(f"payment is worth {total} MC but the cost is {cost} MC")


def parse_units(value, keys):
    out = {k: 0 for k in keys}
    if not value or value in ("none", "0"):
        return out
    for part in value.replace(" ", "").split(","):
        if "=" not in part:
            raise TfmError(f"expected key=number, got '{part}'")
        k, v = part.split("=", 1)
        k = ALIASES.get(k.lower(), k)
        match = next((key for key in keys if key.lower() == k.lower()), None)
        if match is None:
            raise TfmError(f"unknown resource '{k}'; use {', '.join(keys)}")
        out[match] = int(v)
    return out


def payment_from(value, auto):
    if value in (None, "", "auto"):
        return {k: auto.get(k, 0) for k in SPENDABLE}
    return parse_units(value, SPENDABLE)


# ---------------------------------------------------------------- responses

def _all_amounts(node):
    return bool(node["options"]) and all(o["type"] == "amount" for o in node["options"])


def match_card(name, cards):
    names = [c["name"] for c in cards]
    low = name.strip().lower()
    for pick in (
        [n for n in names if n.lower() == low],
        [n for n in names if n.lower().startswith(low)],
        [n for n in names if low in n.lower()],
    ):
        if len(pick) == 1:
            return pick[0]
        if len(pick) > 1:
            raise TfmError(f"'{name}' matches several cards: {', '.join(pick)}")
    raise TfmError(f"no card '{name}' here; choices: {', '.join(names)}")


def _match_name(value, choices, what):
    low = value.strip().lower()
    hit = [c for c in choices if c.lower() == low] or [c for c in choices if c.lower().startswith(low)]
    if len(hit) == 1:
        return hit[0]
    raise TfmError(f"unknown {what} '{value}'; choices: {', '.join(choices)}")


def respond(node, fixed, values, ctx):
    """Build the InputResponse for one input node."""
    t = node["type"]
    value = values[0] if values else None
    if t == "option":
        return {"type": "option"}
    if t == "card":
        if fixed is not None:
            return {"type": "card", "cards": [fixed]}
        names = [] if value in (None, "", "none") else [match_card(v, node["cards"]) for v in value.split(",") if v.strip()]
        lo, hi = node.get("min", 0), node.get("max", len(node["cards"]))
        if not lo <= len(names) <= hi:
            raise TfmError(f"pick {lo}-{hi} cards, got {len(names)}")
        return {"type": "card", "cards": names}
    if t == "projectCard":
        name = fixed or (match_card(value, node["cards"]) if value else None)
        if name is None:
            raise TfmError("which card?")
        card = next(c for c in node["cards"] if c["name"] == name)
        auto, short = default_payment(card, node, ctx)
        if fixed:
            pay_value = values[0] if values else None
        else:
            pay_value = values[1] if len(values) > 1 else None
        if pay_value in (None, "auto") and short:
            raise TfmError(f"cannot afford {name}: {short} MC short")
        payment = payment_from(pay_value, auto)
        _check_payment(payment, card.get("calculatedCost", 0) or 0, allowed_payment(card, node, ctx),
                       _available(ctx, node, card), _values(ctx, card))
        return {"type": "projectCard", "card": name, "payment": payment}
    if t == "payment":
        allow = {k for k, v in node.get("paymentOptions", {}).items() if v} | {"megacredits"}
        if node.get("paymentOptions", {}).get("lunaTradeFederationTitanium"):
            allow.add("titanium")
        auto, short = _greedy(node["amount"], allow, _available(ctx, node), {**_values(ctx), "titanium": ctx.me["titaniumValue"]})
        if value in (None, "auto") and short:
            raise TfmError(f"cannot afford {node['amount']} MC: {short} short")
        payment = payment_from(value, auto)
        _check_payment(payment, node["amount"], allow, _available(ctx, node), {**_values(ctx), "titanium": ctx.me["titaniumValue"]})
        return {"type": "payment", "payment": payment}
    if t == "space":
        sid = fixed or (value or "").strip()
        if sid not in node["spaces"]:
            raise TfmError(f"space '{sid}' is not legal; legal: {', '.join(node['spaces'])}")
        return {"type": "space", "spaceId": sid}
    if t in ("player", "delegate"):
        color = fixed
        if color is None:
            by_name = {ctx.names.get(c, c): c for c in node["players"]}
            color = by_name[_match_name(value or "", list(by_name), "player")]
        return {"type": t, "player": color}
    if t == "colony":
        name = fixed or _match_name(value or "", [c["name"] for c in node["coloniesModel"]], "colony")
        return {"type": "colony", "colonyName": name}
    if t == "resource":
        return {"type": "resource", "resource": fixed or _match_name(value or "", list(node["include"]), "resource")}
    if t == "amount":
        if value is None:
            raise TfmError(f"give an amount {node['min']}-{node['max']}")
        n = int(value)
        if not node["min"] <= n <= node["max"]:
            raise TfmError(f"amount must be {node['min']}-{node['max']}")
        return {"type": "amount", "amount": n}
    if t == "resources":
        return {"type": "resources", "units": parse_units(value, UNIT_KEYS)}
    if t == "productionToLose":
        return {"type": "productionToLose", "units": parse_units(value, UNIT_KEYS)}
    if t == "claimedUndergroundToken":
        sel = [] if not value else [int(x) for x in value.split(",")]
        return {"type": "claimedUndergroundToken", "selected": sel}
    if t == "aresGlobalParameters":
        keys = ["lowOceanDelta", "highOceanDelta", "temperatureDelta", "oxygenDelta"]
        return {"type": "aresGlobalParameters", "response": parse_units(value, keys)}
    if t == "party":
        return {"type": "party", "partyName": _match_name(value or "", node["parties"], "party")}
    if t == "globalEvent":
        return {"type": "globalEvent", "globalEventName": _match_name(value or "", node["globalEventNames"], "global event")}
    if t == "deltaProject":
        return {"type": "deltaProject", "amount": int(value)}
    if t == "and" and fixed is not None:
        return {"type": "and", "responses": fixed}
    if t == "or" and value is not None and value.isdigit():
        i = int(value) - 1
        if not 0 <= i < len(node["options"]):
            raise TfmError(f"pick 1-{len(node['options'])}")
        return {"type": "or", "index": i, "response": respond(node["options"][i], None, values[1:], ctx)}
    if t == "and" and _all_amounts(node) and len(values) == 1 and "=" in values[0]:
        titles = [_title(o, ctx) for o in node["options"]]
        picked = parse_units(values[0], titles)
        resps = []
        for o, title in zip(node["options"], titles):
            n = picked[title]
            if not o["min"] <= n <= o["max"]:
                raise TfmError(f"{title} must be {o['min']}-{o['max']}")
            resps.append({"type": "amount", "amount": n})
        return {"type": "and", "responses": resps}
    if t in ("and", "initialCards"):
        subs = node["options"]
        if len(values) != len(subs):
            raise TfmError(f"this choice needs {len(subs)} values (one per part), got {len(values)}")
        return {"type": t, "responses": [respond(o, None, [v], ctx) for o, v in zip(subs, values)]}
    if t == "or":
        raise TfmError("pick a more specific option id")
    raise TfmError(f"input type '{t}' is not supported by tfm; use the web UI for this step")


def build_response(leaf, values, ctx):
    resp = respond(leaf.node, leaf.fixed, values, ctx)
    for index in reversed(leaf.path):
        resp = {"type": "or", "index": index, "response": resp}
    return resp


def find_leaf(all_leaves, choice):
    for leaf in all_leaves:
        if leaf.id == choice:
            return leaf
    prefix = [leaf.id for leaf in all_leaves if leaf.id.startswith(choice + ".")]
    if prefix:
        raise TfmError(f"'{choice}' is a menu; pick one of: {', '.join(prefix[:20])}")
    raise TfmError(f"no option '{choice}'. Run `tfm options` to see the current choices")
