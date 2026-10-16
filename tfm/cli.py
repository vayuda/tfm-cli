"""tfm: play Terraforming Mars (terraforming-mars.herokuapp.com or a local server) from a terminal.

Built so that AI agents with shell access can play a seat with few tokens:
    tfm join <player-link>    remember the seat
    tfm turn                  wait for your turn, then print the state and numbered choices
    tfm play <id> [value...]  submit a choice
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

from . import data, digest, options, seats, value
from .client import DEFAULT_SERVER, Client, TfmError, create_game, parse_link
from .text import card_line, message

POLL_SECONDS = 3


def _client(seat):
    return Client(seat["server"], seat["player_id"])


def _new_logs(client, seat, view):
    """Log entries this seat has not printed yet (current and any skipped generations)."""
    gen = view["game"]["generation"]
    last_gen = seat.get("last_gen") or gen
    entries = []
    for g in range(max(1, min(last_gen, gen), gen - 2), gen + 1):
        entries.extend(client.logs(g) or [])
    last_ts = seat.get("last_log_ts", 0)
    fresh = [e for e in entries if e.get("timestamp", 0) > last_ts]
    if entries:
        seat["last_log_ts"] = max(last_ts, max(e.get("timestamp", 0) for e in entries))
    seat["last_gen"] = gen
    return fresh


def _print_options(view, ctx, seat):
    wf = view.get("waitingFor")
    if wf is None:
        print("No decision pending for you right now. Run `tfm turn` to wait for one.")
        seat.pop("fp", None)
        return
    leaves = options.leaves(wf, ctx)
    title = options._title(wf, ctx)
    print(f"YOUR DECISION: {title}")
    for leaf in leaves:
        label = leaf.label[len(title) + 3:] if leaf.label.startswith(title + " › ") else leaf.label
        print(f"  [{leaf.id}] {label}" + (f"  ⟨{leaf.hint}⟩" if leaf.hint else ""))
    _print_unplayable(view, title)
    print("Reply with: tfm play <id> [value ...]   (quote values containing spaces)")
    seat["fp"] = options.fingerprint(wf)



def _turn_done(view):
    g = view["game"]
    if g["phase"] == "action":
        if view["thisPlayer"]["color"] in g.get("passedPlayers", []):
            return ("You have passed: no more actions for you this generation. "
                    "Run `tfm turn` to wait for your next decision.")
        return ("Your turn is done, but you have NOT passed: you act again this generation after the other "
                "players. Run `tfm turn` to wait for it.")
    return f"Done for now (phase: {g['phase']}). Run `tfm turn` to wait for your next decision."


def _playable_names(node, out):
    if isinstance(node, dict):
        if node.get("type") == "projectCard":
            out.update(c["name"] for c in node.get("cards") or [] if isinstance(c, dict) and not c.get("isDisabled"))
        for key in ("options", "andOptions"):
            for child in node.get(key) or []:
                _playable_names(child, out)
    return out


def _unplayable_reason(model, outlook):
    """Why a hand card is not offered as a play: the first reason tfm can find from its data."""
    me = outlook.me
    info = data.card(model["name"]) or {}
    for req in info.get("req", []):
        met, text = value.requirement_status(req, outlook)
        if met is False:
            return f"requirement not met: {text}"
    production = ((data.behavior(model["name"]) or {}).get("behavior") or {}).get("production") or {}
    for res, delta in production.items():
        if not isinstance(delta, (int, float)) or delta >= 0:
            continue
        key = {"megacredits": "megacreditProduction", "steel": "steelProduction", "titanium": "titaniumProduction",
               "plants": "plantProduction", "energy": "energyProduction", "heat": "heatProduction"}.get(res)
        if key is None:
            continue
        floor = -5 if res == "megacredits" else 0
        if me[key] + delta < floor:
            return f"needs {res} production to drop {-delta} (you have {me[key]:+d})"
    cost = model.get("calculatedCost", info.get("cost") or 0)
    tags = info.get("tags", [])
    most = me["megacredits"] + (me["steel"] * me["steelValue"] if "building" in tags else 0) \
        + (me["titanium"] * me["titaniumValue"] if "space" in tags else 0)
    if cost > most:
        return f"costs {cost} M€; you can pay about {most}"
    return "another condition (a legal space, a resource on a card, or a card rule): see `tfm card`"


def _print_unplayable(view, title):
    """In an action menu, list hand cards that aren't offered as plays, with a likely reason."""
    if not re.match(r"Take your (first|next) action", title):
        return
    playable = _playable_names(view.get("waitingFor"), set())
    blocked = [c for c in view.get("cardsInHand", []) if c["name"] not in playable]
    if not blocked:
        return
    outlook = value.Outlook(view)
    print("NOT PLAYABLE NOW from your hand: " + "; ".join(
        f"{c['name']} ({_unplayable_reason(c, outlook)})" for c in blocked))


def _finish(seat, ctx):
    seat["seen"] = sorted(ctx.seen)
    seats.save(seat)


def _decision_cards(node, out):
    """Card models offered in the pending decision (draft picks, buys, cards to play)."""
    if isinstance(node, dict):
        for c in node.get("cards") or []:
            if isinstance(c, dict) and c.get("name"):
                out.setdefault(c["name"], c)
        for key in ("options", "andOptions"):
            for child in node.get(key) or []:
                _decision_cards(child, out)
    return out


def _print_values(view, names=None, gens=None):
    ctx = value.Outlook(view, gens_left=gens)
    print(ctx.summary())
    buying_phase = view["game"]["phase"] in ("research", "drafting", "initial_drafting", "prelude_draft", "ceo_draft")
    rows = []
    if names:
        hand = {c["name"]: c for c in view.get("cardsInHand", [])}
        for n in names:
            key = data.card_name(n)
            if key is None:
                print(f"{n}: not found")
                continue
            rows.append((key, hand.get(key), key not in hand, "named"))
    else:
        offered = _decision_cards(view.get("waitingFor"), {})
        for name, model in offered.items():
            rows.append((name, model, buying_phase, "buy" if buying_phase else "choice"))
        for model in view.get("cardsInHand", []):
            if model["name"] not in offered:
                rows.append((model["name"], model, False, "hand"))
        for model in (view.get("preludeCardsInHand") or []) + (view.get("draftedCards") or []):
            if model["name"] not in offered:
                rows.append((model["name"], model, model in (view.get("draftedCards") or []), "prelude" if model in (view.get("preludeCardsInHand") or []) else "drafted"))
    if not rows:
        print("No cards to value: name some, e.g. tfm value \"Kelp Farming\".")
        return
    results = [(value.value_card(name, ctx, buying=buying, model=model), where) for name, model, buying, where in rows]
    for v, where in sorted(results, key=lambda r: -r[0].net):
        print("\n".join(value.describe(v, where)))


def _show(view, seat, client, full, with_state=True, with_values=False, snapshot=False):
    ctx = options.Context(view, set(seat.get("seen", [])), full)
    if view["game"]["phase"] == "end":
        print("\n".join(digest.final_scores(view)))
        _finish(seat, ctx)
        return
    logs = _new_logs(client, seat, view)
    if with_state and seat.get("output") == "changes":
        # Repeat only sections that changed since this seat's last turn; a full snapshot on the first
        # turn of each generation, or on request.
        gen = view["game"]["generation"]
        shown = {} if snapshot or full or seat.get("shown_gen") != gen else seat.get("shown", {})
        lines, seat["shown"] = digest.changes_only(digest.sections(view, ctx, logs), shown)
        seat["shown_gen"] = gen
        if not shown:
            lines.insert(0, "FULL SNAPSHOT (later turns this generation repeat only what changed)")
        print("\n".join(lines))
        print()
    elif with_state:
        print("\n".join(digest.summarize(view, ctx, logs)))
        print()
    _print_options(view, ctx, seat)
    if with_values:
        print()
        _print_values(view)
    _finish(seat, ctx)


def cmd_join(args):
    server, pid = parse_link(args.link, args.server)
    client = Client(server, pid)
    view = client.player()
    name = seats.slug(args.name or view["thisPlayer"]["name"])
    seat = {}
    if name in seats.names():
        seat = seats.load(name)
        if seat.get("player_id") != pid:
            seat = {}  # same seat name, different game: start fresh
    seat.update({"name": name, "server": server, "player_id": pid, "mode": "confirm" if args.confirm else "auto",
                 "color": view["thisPlayer"]["color"], "game": view["game"]["name"]})
    seats.save(seat)
    others = ", ".join(p["name"] for p in view["players"] if p["color"] != view["thisPlayer"]["color"])
    print(f"Joined game '{view['game']['name']}' as {view['thisPlayer']['name']} ({view['thisPlayer']['color']}), "
          f"seat name '{name}', mode {seat['mode']}. Opponents: {others}.")
    if len(seats.names()) > 1:
        print(f"Several seats are saved on this machine; pass --seat {name} (or set TFM_SEAT={name}) on every command.")


def cmd_seats(_args):
    for name in seats.names():
        s = seats.load(name)
        print(f"{name}: game {s.get('game')} · {s['server']} · player {s['player_id']} · mode {s.get('mode', 'auto')}")


def cmd_leave(args):
    seats.remove(args.name)
    print(f"Removed seat {args.name}.")


def cmd_turn(args):
    seat = seats.load(args.seat)
    client = _client(seat)
    deadline = time.time() + args.wait
    view = client.player()
    while True:
        if view["game"]["phase"] == "end":
            break
        wf = view.get("waitingFor")
        if wf is not None and not wf.get("optional"):
            break
        if time.time() >= deadline:
            status = client.waiting_for(view["game"]["gameAge"], view["game"]["undoCount"])
            names = {p["color"]: p["name"] for p in view["players"]}
            who = ", ".join(names.get(c, c) for c in status.get("waitingFor", [])) or "nobody (between phases)"
            print(f"STILL WAITING: not your turn yet (waiting on: {who}). Run `tfm turn` again.")
            return
        status = client.waiting_for(view["game"]["gameAge"], view["game"]["undoCount"])
        if status["result"] in ("GO", "REFRESH"):
            view = client.player()
            if status["result"] == "GO":
                continue
        time.sleep(POLL_SECONDS)
    _show(view, seat, client, args.full, with_values=args.value, snapshot=args.snapshot)


def cmd_state(args):
    seat = seats.load(args.seat)
    client = _client(seat)
    _show(client.player(), seat, client, args.full, with_values=args.value, snapshot=args.snapshot)


def cmd_value(args):
    seat = seats.load(args.seat)
    _print_values(_client(seat).player(), args.names, args.gens)


def cmd_options(args):
    seat = seats.load(args.seat)
    view = _client(seat).player()
    ctx = options.Context(view, set(seat.get("seen", [])), args.full)
    _print_options(view, ctx, seat)
    _finish(seat, ctx)


def _leaf_matches(leaf, text, values=()):
    """--expect check: TEXT is in the option's label, its fixed value, or the values being submitted."""
    text = text.lower()
    return (text in leaf.label.lower() or (isinstance(leaf.fixed, str) and text in leaf.fixed.lower())
            or any(text in v.lower() for v in values))


def cmd_play(args):
    seat = seats.load(args.seat)
    client = _client(seat)
    view = client.player()
    wf = view.get("waitingFor")
    ctx = options.Context(view, set(seat.get("seen", [])), False)
    if wf is None:
        raise TfmError("no decision is pending for you; run `tfm turn`")
    if seat.get("fp") != options.fingerprint(wf):
        print("The choices changed since you last listed them, so nothing was submitted. Current choices:")
        _print_options(view, ctx, seat)
        _finish(seat, ctx)
        sys.exit(2)
    all_leaves = options.leaves(wf, ctx)
    leaf = options.find_leaf(all_leaves, args.choice)
    if args.expect and not _leaf_matches(leaf, args.expect, args.values):
        print(f"Option [{leaf.id}] is now \"{leaf.label}\", which doesn't contain \"{args.expect}\", "
              "so nothing was submitted.")
        matches = [lf.id for lf in all_leaves if _leaf_matches(lf, args.expect)]
        if matches:
            print(f"Options containing \"{args.expect}\": {', '.join(matches)}")
        _print_options(view, ctx, seat)
        _finish(seat, ctx)
        sys.exit(2)
    response = options.build_response(leaf, args.values, ctx)
    if seat.get("mode") == "confirm" and not args.yes:
        print(f"CONFIRM MODE: proposed move [{leaf.id}] {leaf.label}")
        if args.values:
            print(f"  values: {args.values}")
        print("Ask the human to approve; once they say yes, rerun the same command with --yes.")
        sys.exit(3)
    try:
        new_view = client.submit(response, view.get("runId"))
    except TfmError as e:
        print(f"REJECTED: {e}")
        _print_options(client.player(), ctx, seat)
        _finish(seat, ctx)
        sys.exit(1)
    print(f"OK: [{leaf.id}] {leaf.label}" + (f" {args.values}" if args.values else ""))
    if args.debug:
        print(json.dumps(response))
    for entry in _new_logs(client, seat, new_view):
        print("  " + message(entry, ctx.names))
    me = new_view["thisPlayer"]
    print(f"NOW: {digest.resources(me)} · TR {me['terraformRating']} · hand {len(new_view.get('cardsInHand', []))}")
    ctx = options.Context(new_view, ctx.seen, False)
    wf = new_view.get("waitingFor")
    if new_view["game"]["phase"] == "end":
        print("\n".join(digest.final_scores(new_view)))
    elif wf is not None and not wf.get("optional"):
        _print_options(new_view, ctx, seat)
    else:
        seat.pop("fp", None)
        print(_turn_done(new_view))
    _finish(seat, ctx)


def cmd_card(args):
    for name in args.names:
        info = data.card(name)
        if info is None:
            hits = [n for n in data.cards() if name.lower() in n.lower()][:10]
            print(f"{name}: not found" + (f" (did you mean: {', '.join(hits)})" if hits else ""))
            continue
        print(f"{card_line(data.card_name(name))} [{info['type']}, {info.get('module')}]")


def cmd_board(args):
    seat = seats.load(args.seat)
    view = _client(seat).player()
    names = {p["color"]: p["name"] for p in view["players"]}
    me = view["thisPlayer"]["color"]
    rows = {}
    for s in view["game"]["spaces"]:
        if s.get("tileType") is None:
            continue
        owner = "YOU" if s.get("color") == me else names.get(s.get("color"), "neutral" if s.get("color") else "-")
        rows.setdefault(owner, []).append(f"{s['id']}{'[' + s['a9'] + ']' if s.get('a9') else ''} {data.tile_name(s['tileType'])}")
    for owner, tiles in rows.items():
        print(f"{owner}: " + "; ".join(tiles))
    free = [s["id"] for s in view["game"]["spaces"] if s.get("tileType") is None and s["spaceType"] == "land"]
    print(f"empty land spaces: {len(free)}")


def cmd_log(args):
    seat = seats.load(args.seat)
    client = _client(seat)
    view = client.player()
    names = {p["color"]: p["name"] for p in view["players"]}
    for entry in client.logs(args.generation or view["game"]["generation"]):
        print(message(entry, names))


def cmd_new_game(args):
    settings = json.loads(Path(args.settings).read_text())
    if args.players:
        colors = ["red", "green", "yellow", "blue", "black", "purple", "orange", "pink"]
        settings["players"] = [{"name": n.strip(), "color": colors[i], "beginner": False, "handicap": 0, "first": False}
                               for i, n in enumerate(args.players.split(","))]
    game = create_game(args.server, settings)
    print(f"Created game {game['id']} ({game.get('name')}) on {args.server}")
    for p in game["players"]:
        print(f"  {p['name']}: {args.server.rstrip('/')}/player?id={p['id']}")
    print(f"  spectator: {args.server.rstrip('/')}/spectator?id={game['spectatorId']}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="tfm", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-s", "--seat", help="seat name (default: $TFM_SEAT or the only saved seat)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("join", help="remember a seat from a player link")
    p.add_argument("link")
    p.add_argument("--as", dest="name", help="seat name (default: player name)")
    p.add_argument("--server", help=f"server for a bare player id (default {DEFAULT_SERVER})")
    p.add_argument("--confirm", action="store_true", help="require --yes on every play (human approves each move)")
    p.set_defaults(fn=cmd_join)

    sub.add_parser("seats", help="list saved seats").set_defaults(fn=cmd_seats)
    p = sub.add_parser("leave", help="forget a seat")
    p.add_argument("name")
    p.set_defaults(fn=cmd_leave)

    p = sub.add_parser("turn", help="wait for your turn, then print state and choices")
    p.add_argument("--wait", type=int, default=100, help="seconds to wait before giving up (default 100)")
    p.add_argument("--full", action="store_true", help="repeat card and milestone text already shown")
    p.add_argument("--value", action="store_true", help="also print `tfm value` for the cards in the decision and hand")
    p.add_argument("--snapshot", action="store_true", help="print every section, not only what changed")
    p.set_defaults(fn=cmd_turn)

    p = sub.add_parser("state", help="print state and choices now, without waiting")
    p.add_argument("--full", action="store_true")
    p.add_argument("--value", action="store_true", help="also print `tfm value` for the cards in the decision and hand")
    p.add_argument("--snapshot", action="store_true", help="print every section, not only what changed")
    p.set_defaults(fn=cmd_state)

    p = sub.add_parser("value", help="estimate what cards are worth in M€ now, with context checks")
    p.add_argument("names", nargs="*", help="cards to value (default: cards in the pending decision and your hand)")
    p.add_argument("--gens", type=int, help="override the estimated generations left (including this one)")
    p.set_defaults(fn=cmd_value)

    p = sub.add_parser("options", help="print only the current choices")
    p.add_argument("--full", action="store_true")
    p.set_defaults(fn=cmd_options)

    p = sub.add_parser("play", help="submit a choice: tfm play <id> [value ...]")
    p.add_argument("choice")
    p.add_argument("values", nargs="*")
    p.add_argument("--expect", metavar="TEXT",
                   help="refuse unless the chosen option contains TEXT (e.g. the card name), guarding against renumbered ids")
    p.add_argument("--yes", action="store_true", help="confirm a move in confirm mode")
    p.add_argument("--debug", action="store_true", help="print the JSON sent to the server")
    p.set_defaults(fn=cmd_play)

    p = sub.add_parser("card", help="show card text")
    p.add_argument("names", nargs="+")
    p.set_defaults(fn=cmd_card)

    sub.add_parser("board", help="list tiles on the board").set_defaults(fn=cmd_board)
    p = sub.add_parser("log", help="print the game log for a generation")
    p.add_argument("--generation", type=int)
    p.set_defaults(fn=cmd_log)

    p = sub.add_parser("new-game", help="create a game from a settings JSON and print player links")
    p.add_argument("settings")
    p.add_argument("--server", default=DEFAULT_SERVER)
    p.add_argument("--players", help="comma-separated player names (overrides the settings file)")
    p.set_defaults(fn=cmd_new_game)

    args = parser.parse_args(argv)
    transcript = _transcript_path(args)
    if transcript:
        real, sys.stdout = sys.stdout, _Tee(sys.stdout, transcript, argv if argv is not None else sys.argv[1:])
    try:
        args.fn(args)
    except TfmError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    finally:
        if transcript:
            sys.stdout.close()
            sys.stdout = real


def _transcript_path(args):
    """A seat can record everything tfm prints for it (set "transcript" in its seat file)."""
    if args.cmd in ("join", "seats", "leave", "card", "new-game"):
        return None
    try:
        return seats.load(args.seat).get("transcript")
    except TfmError:
        return None


class _Tee:
    def __init__(self, out, path, argv):
        self.out, self.file = out, open(path, "a")
        self.file.write(f"\n>>> {time.strftime('%H:%M:%S')} tfm {' '.join(argv)}\n")

    def write(self, text):
        self.file.write(text)
        return self.out.write(text)

    def flush(self):
        self.file.flush()
        self.out.flush()

    def close(self):
        self.file.close()


if __name__ == "__main__":
    main()
