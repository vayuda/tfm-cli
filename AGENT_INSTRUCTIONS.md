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
   If it prints `STILL WAITING`, run it again straight away and say nothing else (unless voice chat
   is on and someone spoke to you; see "Voice chat"). Waiting costs no thinking.
2. Decide.
3. `tfm -s <seat> play <id> [value ...]`
   This submits the choice. If more input is needed in the same turn (a placement, a second action),
   the new choices are printed right away: keep going with `play`. When it says
   `Your turn is done`, go back to step 1.

Set your shell tool's timeout to at least 120 seconds for `tfm turn`. If your tool cannot wait that long,
use `tfm -s <seat> turn --wait 50`.

## Reading the output

- `YOU`: your TR, current VP breakdown, resources as `stock(+production)`, tags, played cards, hand,
  and the **clock**. Escape Velocity is usually on: time past the allowance costs VP, so don't
  over-deliberate routine moves.
- `OPPONENTS`: public resources, tags and played cards.
- `MILESTONES`: your score against the best rival's. `<<< YOU CAN CLAIM` means you qualify right now.
- `AWARDS`: everyone's current score. While a slot is open, each unfunded award also gets a `→` line:
  projected final scores (from how fast each count has grown and rivals' cards that raise it by
  themselves), your chance of 1st and 2nd, **fund EV** (your expected award VP minus the fee and the VP
  it hands rivals) and **wait** (your value if a rival likely to win funds it instead), with a verdict.
  It's an estimate that assumes `tfm`'s own guess of the generations left: if yours differs, adjust,
  and check rivals' hands and played cards before acting on a close call.
- `SINCE YOUR LAST LOOK`: what happened since your previous `turn`.
- `YOUR DECISION`: the legal choices. Only these ids are accepted.

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
colony or space id (text from the option's label, or for a choice that takes values, like the opening pick,
from one of your values). For example: `tfm -s me play 2.3 --expect "Solar Farm"` or
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
- `tfm -s <seat> vp`: every player's VP if the game ended now. Yours comes from the app; rivals' are
  recomputed from public information (TR, milestones, award standings, tiles, played cards), because the app
  may hide them. Cards it can't compute are named with `+?`. Use it before ending the game or funding an award.
- `tfm card "<name>" ...`: card text.
- `tfm -s <seat> value [card ...]`: what each card in your pending decision and hand (or the cards you
  name) is worth in M€ right now, against what it costs, plus context flags: bonus thresholds, the last
  step of a parameter, planetary-track rewards, milestones you could claim, award placings it changes,
  requirements, rivals' payouts and attack blockers. `tfm -s <seat> turn --value` appends it to the
  turn. Use it for drafting, buying and close choices; it is an estimate, so read any `?` lines yourself.

## Voice chat

If your human says voice chat is on, you are also sitting in their Discord voice channel. A daemon
transcribes what people say and speaks what you tell it to.

- If `tfm -s <seat> voice status` says voice is off, run `tfm -s <seat> voice start` once.
- `tfm turn`, `state` and `play` print a `VOICE CHAT` block with what was said since you last looked.
- **Table talk** (when `voice status` shows it on): a fast auto-responder answers people for you
  within seconds. Its replies show as `YOU (table-talk auto-reply)`; they are your words, so stay
  consistent with them. It hands you anything that needs a real decision (a move, a deal, an undo),
  shown as `asked you (needs YOUR answer)`.
- Keep its notes current, so it can explain you: add `--note "..."` to a `play` when your thinking
  changes (once a generation is plenty), or run `tfm -s <seat> voice notes "..."`. One to three
  sentences about why you made recent moves and how you see the table. **Never put cards in your hand or
  future plans in the notes** unless your human said voice is in open mode: the notes can be quoted aloud.
- While you wait, `tfm turn` **returns early** when something needs your answer, printing
  `YOU WERE ADDRESSED` and `NOT YOUR TURN YET`. Answer, then run `tfm turn` again straight away.
- Speak with `tfm -s <seat> voice say "..."`. It returns immediately and speaks in the background,
  so carry on playing. Never put `voice say` and `play` in the same shell call.

How to talk:

- **Always answer what's handed to you**, promptly, before your next game command if you can.
- Keep it short and spoken: one or two sentences, no lists, no markdown, no card ids or numbers
  nobody needs. Say "megacredits", not "M€".
- Talk like a friendly player at the table. With table talk on, it handles the chatter; speak up
  yourself only for handed-over questions and the odd big moment (a milestone, the last ocean).
- Keep your hand and plans to yourself, as any player would. Don't lie about public game state.
- Transcription can mishear names and cards; if a question is unclear, ask them to repeat it.
- In confirm mode, propose moves out loud ("I'd like to play Birds, okay?") and treat a clear spoken
  yes from your human as approval.

## Confirm mode

If `join` reported `mode confirm`, `play` only proposes a move. Show the proposal to your human and
ask them. Rerun the same command with `--yes` **only after they agree**.

## Fair play

Use only your own player link. Never fetch another player's link or read their hidden hand.

## Strategy

Read [docs/STRATEGY.md](docs/STRATEGY.md) once at the start of the game: it covers strategic
direction, rules that commonly trip players up, the expansions and the endgame. The short version:

- Count remaining generations from how fast **all** players can finish the global parameters
  (temperature +8°C, oxygen 14%, 9 oceans). Engines that pay back after the game ends are worth nothing.
- Recheck every milestone after each card or tile, and keep 8 M€ for a claim you can win.
- Before funding an award, project rivals' final scores, not just today's: start from its `→` line in `AWARDS`.
- Value heat at zero once temperature is maxed, unless a card gives it another use.
- Before passing, look for leftover greeneries, milestones, awards, card actions and trades.
- If your human gave you their own strategy notes, they take precedence over docs/STRATEGY.md.
