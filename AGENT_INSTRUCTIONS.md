# Playing Terraforming Mars with `tfm`

You are a player in a live game of Terraforming Mars on the web app
(terraforming-mars.herokuapp.com, or a local server). Your goal is to **win**: finish with the most
victory points. Other seats may be humans or other AI agents.

You play entirely through the `tfm` command-line tool. Do not open the web page.

## Setup (once)

```bash
tfm join "<your player link>" --as <seat-name>
```

Use the seat name you were given (for example `claude` or `gpt`). After that, **pass `-s <seat-name>`
on every command**, because several agents may share this computer.

If `join` fails with `SSL: CERTIFICATE_VERIFY_FAILED`, Python can't find a CA bundle. This is common
with the python.org installer on macOS. Ask your human to run `/Applications/Python 3.x/Install
Certificates.command` once (use the folder for the installed Python version). As a stopgap, prefix each
command with `SSL_CERT_FILE=/etc/ssl/cert.pem`.

## The loop

Repeat until you see `GAME OVER`:

1. `tfm -s <seat> turn`
   This waits up to 100 seconds for your turn, then prints the game state and your numbered choices.
   If it prints `STILL WAITING`, run it again straight away and say nothing else. Waiting costs no thinking.
2. Decide.
3. `tfm -s <seat> play <id> [value ...]`
   This submits the choice. If more input is needed in the same turn (a placement, a second action),
   the new choices are printed right away: keep going with `play`. When it says
   `Your turn is done`, go back to step 1. That message means the other players act next; unless it says
   you have passed, you get more actions later this generation.

Set your shell tool's timeout to at least 120 seconds for `tfm turn`. If your tool cannot wait that long,
use `tfm -s <seat> turn --wait 50`.

## Reading the output

- `YOU`: your TR, VP breakdown, resources as `stock(+production)`, tags, played cards, hand,
  and the **clock**. The VP line is "if the game ended now": its award points follow the current standings of
  the funded awards, so they move as rivals overtake you or you overtake them. Escape Velocity is usually on: time past the allowance costs VP, so don't
  over-deliberate routine moves.
- `OPPONENTS`: public resources, tags and played cards.
- `MILESTONES`: your score against the best rival's. `<<< YOU CAN CLAIM` means you qualify right now.
- `AWARDS`: everyone's current score.
- `SINCE YOUR LAST LOOK`: what happened since your previous `turn`.
- `YOUR DECISION`: the legal choices. Only these ids are accepted. In an action menu, `NOT PLAYABLE NOW`
  lists the hand cards that aren't offered, with the likely reason (unmet requirement, a production decrease
  you can't make, cost).
- Board spaces in a placement list show what the placement costs: `COVERS <hazard>` (extra M€, extra TR)
  and `NEXT TO HAZARDS` (production you must give up, except for oceans).

Card text is printed the **first time** a card appears for your seat and abbreviated after that. Keep
your own notes, or use `tfm card "<name>"` or `--full` to see it again.

## Choice syntax

| What the choice shows | What to type |
|---|---|
| A plain option, card, space, colony or player | `tfm -s me play 3.2` |
| Playing a card: `[cost → pay 2 St + 8 MC]` | `play 1.4` uses the shown payment. To choose your own: `play 1.4 mc=4,steel=3` |
| `⟨VALUE = comma-separated card names, pick 0-4⟩` | `play 2 "Solar Farm,Birds"` (or `none`) |
| `⟨VALUE = number 0-5⟩` | `play 1 3` |
| `⟨VALUE = amounts by name, e.g. Plants=1⟩` | `play 1 plants=1` |
| `⟨one quoted VALUE per part, in order⟩` (e.g. the opening corporation/preludes/cards pick) | `play 1 "Ecoline" "Allied Bank,Biolab" "Mine,Birds"` |

Payment keys: `mc`, `steel`, `titanium`, `heat`, `plants`, `microbes`, `floaters`, `seeds`, `data`, `graphene`.

**Always add `--expect` with the name of what you're choosing**: the card, project, milestone, award,
colony or space id. It is matched against the option's text and against the values you pass, so for the
opening pick `--expect "Ecoline"` works. For example: `tfm -s me play 2.3 --expect "Solar Farm"` or
`play 1.12 --expect "space 35"`. Choice ids renumber after every action, so an id from an old list
can point at a different option. With `--expect`, a mismatch is refused, the options matching your
text are listed, and nothing is submitted. Also, never chain two `play` commands in one shell call:
read the output of each `play` before the next.

If a move is `REJECTED`, read the reason. The current choices are reprinted; pick again. If it says
the choices changed, use the new ids it printed.

## Other commands

- `tfm -s <seat> state`: state and choices now, without waiting.
- `tfm -s <seat> board`: every tile on the board, by owner.
- `tfm -s <seat> log`: this generation's full log.
- `tfm card "<name>" ...`: card text.
- `tfm -s <seat> value [card ...]`: what each card in your pending decision and hand (or the cards you
  name) is worth in M€ right now, against what it costs, plus context flags: bonus thresholds, the last
  step of a parameter, planetary-track rewards, milestones you could claim, award placings it changes,
  requirements, rivals' payouts and attack blockers. `tfm -s <seat> turn --value` appends it to the
  turn. Use it for drafting, buying and close choices; it is an estimate, so read any `?` lines yourself.

## Confirm mode

If `join` reported `mode confirm`, `play` only proposes a move. Show the proposal to your human and
ask them. Rerun the same command with `--yes` **only after they agree**.

## Fair play

Use only your own player link. Never fetch another player's link or read their hidden hand.

## Strategy

Read [docs/STRATEGY.md](docs/STRATEGY.md) once at the start of the game: it covers strategic
direction, rules that commonly trip players up, the expansions, the endgame, and lessons from past
games. The short version:

- Count remaining generations from how fast **all** players can finish the global parameters
  (temperature +8°C, oxygen 14%, 9 oceans). Engines that pay back after the game ends are worth nothing.
- Recheck every milestone after each card or tile, and keep 8 M€ for a claim you can win.
- Before funding an award, project rivals' final scores, not just today's.
- Value heat at zero once temperature is maxed, unless a card gives it another use.
- Before passing, look for leftover greeneries, milestones, awards, card actions and trades.
- If your human gave you their own strategy notes, they take precedence over docs/STRATEGY.md.
