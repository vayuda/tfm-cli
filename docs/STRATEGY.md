# Terraforming Mars: strategy and rules context for agents

Context for an AI agent playing a seat with `tfm` (see [AGENT_INSTRUCTIONS.md](../AGENT_INSTRUCTIONS.md)):
a strategic framework, rules that commonly trip players up and expansion notes. Read it once at the
start of a game, not every turn.

It is **not** a card database or proof of optimal play. When a card interaction is unusual, the exact
card text (`tfm card "<name>"`), the live game settings and the choices `tfm` offers decide. Never carry
resources, coordinates or plans over from a previous game.

---

## Priorities

1. Read the actual setup from your first `tfm turn`: player count, map, expansions, milestones,
   awards, colonies, and whether the clock penalty is on.
2. **Leave no weak VP category.** At the start of every generation, compare your TR, card VP, board VP
   (greeneries + cities), milestones and awards with the leader's, and spend that generation on your
   weakest one (see §1).
3. Track stock and production separately. Note which card actions you've used and which fleets are free.
4. Estimate how many generations are left from **every** player's ability to finish the global
   parameters (§8), and switch from buying production to buying points about two generations before the
   end. Pass your estimate to `tfm value --gens N`.
5. After every card or tile, recheck **all** unclaimed milestones, not just the ones you were aiming for.
6. Keep cash in reserve for a milestone claim, an award fee or a contested opportunity before spending
   on optional development.
7. Drafting, buying and playing are three separate decisions. A good draft pick doesn't have to be worth
   buying, and every pick also decides what the next player sees (§5).
8. Only place tiles on the spaces `tfm` lists, and read each space's bonus, costs and neighbours first.
9. Value a resource by its remaining uses. Heat after temperature is maxed is not automatic TR.
10. Before passing, look for unused scoring actions, trades, plant thresholds, milestones, awards and
    hand sales.
11. Keep routine turns fast. Save long thinking for the opening, award funding and the endgame.

---

## 1. Strategic model

Terraforming Mars is a competition over **time, capital, scoring opportunities and access**. Your
opponents help decide when your investments stop paying: an engine that needs three more generations
is weak if the table can end the game this generation.

Keep two projections:

- **Economic:** what can I afford and activate before the next production phase?
- **Scoring:** where will my final points come from if the game ends this generation, the next, or the
  one after? Break it into TR, milestones, awards, greeneries, cities, card and resource points,
  expansion points and penalties. `tfm` prints your VP breakdown every turn.

The highest TR at the table doesn't necessarily win, and neither does the most card VP. In theory a
weak category is fine if another route makes up for it; in practice it rarely does. Against a strong
player who scores in every category, a single neglected category has decided game after game. Over a
long series of four-player games against rm0331, the agents lost almost every time in the one category
each had neglected, often by 2–9 VP:

- High TR but 4–16 card VP, or 30–49 card VP but low TR.
- 6–9 board VP against the winner's 19–34: a first city on Mars only in generation 7–8, or 2–3 plant
  production against the winner's 15.
- 0 milestone and award VP after every slot was taken by generation 4–6.

The agents who won were competitive in all five categories, not dominant in one; the
start-of-generation check in §11 is how to stay there. For any candidate action, ask:

> What does this add to my likely final score, what does it enable, what does it give or deny the
> strongest rival, and what better action does its cost rule out?

Hidden hands, unknown draws and uncertain game length call for ranges and judgement, not fake precision.

### Main strategic directions

| Direction | When it is attractive | Main failure mode |
|---|---|---|
| Fast terraforming | Efficient heat, plants and events, and a lead that benefits from an early finish | Helping close the game while a rival scores more efficiently elsewhere |
| Economy and card engine | Early discounts or card draw, reliable things to spend on, enough generations left | Investing after the payback window has closed |
| Ground game | Plant supply, good placements, viable city sites, map-related scoring | Expensive empty cities, or greeneries that feed opponents' cities |
| Space/Jovian | Titanium supply, strong space cards, multipliers you can support | Collecting tags or metal with no affordable way to score them |
| Resources on cards | Repeatable animals, floaters, microbes or data with a good conversion | Buying a slow action too late, or resources stolen before they score |
| Colonies and trade | Profitable reachable colonies, a cheap way to pay for trades, useful owner bonuses | Assuming an attractive trade will still be there on your turn |

Most winning positions mix directions. Pick an initial emphasis, then adapt to the cards and opponents
you actually face.

Whatever the emphasis, every strategy needs a minimum in each category: an on-Mars city by
generation 3–4 with real plant production behind it, even on a Venus, space or Jovian plan (§8); a VP
card added most generations from generation 2 (§5); and TR rising every generation. An engine that
raised no parameters until the last two generations left an agent 15 TR behind. Games are short (§8),
so prefer plays that score or raise TR now over production engines that pay back late.

---

## 2. Rules baseline

Normal multiplayer defaults; cards, maps and variants can override them.

- Each turn you take one or two actions; passing ends your participation for the generation.
- Research cards cost 3 M€ each to keep, separately from their play cost.
- Each blue-card action can normally be used once per generation.
- M€ income includes your TR. Leftover energy becomes heat before new energy is produced.
- 8 plants → a greenery; 8 heat → one temperature step.
- Temperature ends at +8°C, oxygen at 14%, oceans at nine. The game ends at the end of the generation in
  which all three are complete, after production and a final greenery conversion.
- Each greenery scores 1; each city scores 1 per adjacent greenery, whoever owns it.
- Milestones cost 8 M€ and score 5; normally three can be claimed in total.
- Awards cost 8 / 14 / 20 M€ to fund; normally three can be funded. First scores 5, second 2. Funding
  doesn't guarantee winning.
- Steel pays 2 M€ towards building cards; titanium pays 3 towards space cards. Neither is general cash.
- An ordinary greenery must go next to one of your own tiles when possible. Ordinary cities can't go
  next to other cities.

### Rules that commonly cause bad decisions

- Event tags count when the event is played, then stop counting as persistent tags (milestones, awards
  and requirements ignore them).
- The app may let you raise a parameter after it is maxed, but that part gives no increase and no TR.
  This includes a parameter a rival maxes earlier in the same generation: agents have paid for Air
  Scrapping and Asteroid after Venus or temperature had just closed. Check the parameter's current level
  in `tfm state` immediately before any play whose value is a raise.
- A greenery needs a legal space. Late in a game with a large plant engine at the table, every space
  can fill up, and then neither the action phase nor the final conversion after production offers a
  greenery; leftover plants score nothing. Once open spaces get scarce, count them and convert plants
  while spaces remain, instead of counting on the final conversion.
- The game log doesn't record every triggered resource change. Trust the resource display.
- A card's own tag doesn't count toward its own requirement. A production decrease that looks optional
  often isn't.
- An off-map city (Stanford Torus, Lunar Embassy, Maxwell Base and other reserved areas) has no
  neighbours, so it scores no greenery points and doesn't replace a city on Mars. It still counts as a
  city for cards and awards that count cities.
- If the server rejects a move, read the exact reason instead of repeating the move.
- A **wild tag** can supply a tag for an action or milestone claim, but doesn't count for awards or
  end-game multipliers. A **clone tag** (Pathfinders) permanently becomes the tag you choose when played.
  A **wild resource** is a choice of resource, not a tag.

---

## 3. Settings to check at the start of every game

Read these from the game itself. Don't assume them.

| Setting | Why it matters |
|---|---|
| Player count | Game length, award competition, how contested milestones are |
| Map | Special spaces and placement rules; `tfm` lists each space's bonuses |
| Expansions | Venus, Colonies, Prelude, Turmoil, Ares, Pathfinders, Underworld, etc. change what's valuable |
| Draft variant | Whether you pass cards between players during research |
| Milestones and awards | Often random, sometimes fan-made. `tfm` prints each requirement the first time |
| Colonies in play | Only the tiles shown can be traded with or settled |
| Banned or replaced cards | A card name can appear as its **Ares replacement version**, with its own text: read it |
| Venus | Whether Venus must be completed to end the game, and whether the alternative Venus board is on |
| Clock penalty (Escape Velocity) | Thinking time can cost VP |

**Clock penalty:** the allowance is `threshold + seconds per action × actions taken`; past it you lose
the stated VP for each full penalty period. `tfm` prints clock, allowance and penalty every turn. Your
first action isn't timed; thinking on your turn is.

---

## 4. The opening: choose a package

Judge corporation, preludes and starting cards together. For each serious option, sketch a
generation-1 sequence and a generation-2 goal, and check the cash left after buying cards, prelude
costs, required first actions, available metal and placement legality. A combination you can't pay for
is not an opening.

Compare packages on immediate usable economy, synergy with the cards actually offered, milestone timing
and cash to claim, access to trades and placements, flexibility if a rival takes your target, and a
credible path to points at the table's pace.

A corporation whose effect fires on **any** player's action is often stronger than one that only
rewards your own plays, and it grows with the table. Check the opposite case too: a corporation action
needing a resource you won't produce may never fire.

Look for milestones the opening package alone can reach: a corporation, prelude and starting card can
sometimes qualify on the first action turn.

Add up the package's M€ production before choosing. A package that starts near +0 M€ stays poor for a
long time, and TR from preludes doesn't replace income. A corporation that pays in a resource is only as
good as your use for that resource, and one that raises the price of buying cards (Polyphemos, 5 M€
per card) taxes every research phase, not just the opening: pick it only with the income to keep
buying VP cards. A corporation paid per step of one global parameter depends on how fast the
table moves that parameter; prelude and early standard-project steps on it are what make it pay.

Preludes that give early resources can unlock a sequence production alone can't pay for, but cash that
buys nothing useful can be worse than lasting production. Look for prelude and colony combinations whose
placement bonuses chain into extra steps.

Keep starting cards you can realistically play. Don't leave your corporation unable to act in
generation 1. Cards whose value grows with the generation number (e.g. Flat Mars Theory) are worth
holding for a couple of generations.

---

## 5. Drafting and buying

**Draft** for cards that fix a current bottleneck, enable an affordable high-value sequence, secure a
scoring race, or give efficient points within the remaining game.

**Denial drafting (hate-drafting).** Every draft pick does two things: it keeps a card for you, and it
decides which cards the next player sees. Score each option on both. Taking a card in the draft costs
nothing; only buying it costs 3 M€. A denial pick you never buy still works, because the rival never sees
the card.

- **Work out who receives your pass.** The draft direction alternates each generation, so the receiver
  is one neighbour in odd generations and the other in even ones. Check seat order and the generation.
  If you can't tell, weigh the card against both neighbours.
- **Read the receiver's position.** Look at their corporation, tags, production, colonies, milestone
  and award races, and the cities and tiles they have on the board. A card's value to them is what it
  adds to *their* engine, not its general rating.
- **Take the card that helps the receiver most** when nothing in the pack helps you much, and even over
  a decent card for yourself unless your own pick is clearly better. Feed no one's engine: heat
  production passed to Helion or Ambient, plant production (Botanical Experience, which combos with an
  Iapetus II colony) passed to a plant engine. Passing Capital and Botanical Experience to the leader
  cost a game by 2 VP.
- **Check the receiver's requirements, not yours.** A card you can't play can still be the right pick.
  Capital costs 26 M€ and needs 2 energy production to lower; the player who passed it had none, so it
  wasn't worth playing, but the receiver could play it for a large endgame VP spike.
- **Don't buy a pure denial pick** unless it is also worth 3 M€ to you; the denial was already done
  when you drafted it.

**Buying test**, for each card:

- When will I play it, and what pays for it?
- How many useful activations or production phases are left?
- What resource, tag, requirement or board condition does it need? For placement restrictions
  (volcanic spaces, spaces next to two oceans), check that a legal space exists and will stay free.
  A card that lowers your energy production can't be played with 0 energy production.
- What other purchase or action does it displace?
- Is its main effect still available? A card whose parameter is already maxed is worth only its
  printed VP and tags.
- Have I read its actual text (`tfm card "<name>"`)? An agent bought a card for an action it turned out
  not to have; another bought VP cards (Cloud Tourism, Titan Shuttles) whose resources never arrived in
  time to score.

**Keep VP cards flowing.** From generation 2, each research phase should usually add at least one card
that scores VP: resource-VP cards (animals, microbes, floaters), Jovian cards or flat-VP cards. A single
animal card such as Stratospheric Birds or Pets has scored 10+ VP for the winner, and agents who scored
only 5–11 card VP lost to players with 20–34.

**Among cards for your own hand, prefer ones you'll buy.** Take the card you can afford and play in the
time left over a better card you can't, but weigh that against a denial pick, as above. Cards that sit
unplayed in hand are 3 M€ each thrown away.

Judge the **whole purchase** against your action-phase budget: three reasonable cards can together block
a milestone claim or an award fee. A card in hand is a sunk cost; selling it can be right when the
proceeds complete a better scoring action.

---

## 6. Valuing resources

### Baseline exchange rates

| Unit | Worth | Where the number comes from |
|---|---:|---|
| 1 VP | 5 M€ | Dust Seals: 2 M€ to play + 3 M€ to buy, for 1 VP |
| 1 TR | 5 M€ + 1 M€ per remaining production phase | 1 VP at the end, plus 1 M€ of income at every production phase you can still spend |
| Heat | TR ÷ 8 | 8 heat = 1 temperature TR. 0 once temperature is maxed |
| Plant | (VP + TR) ÷ 8 | 8 plants = greenery (1 VP + 1 oxygen TR). VP ÷ 8 ≈ 0.6 M€ once oxygen is maxed |
| Steel / titanium | 2 / 3 M€ | Only when you have a matching card to spend it on |
| Card in hand | 3–4 M€ | Roughly its purchase price; less late when it can't be played |
| M€ production | ~1 M€ per remaining usable production phase | The final phase can't fund a normal action |

"Remaining production phases" means the ones whose income you can still spend: about the number of
generations left minus one, since the final phase's cash can't fund a normal action. So a TR is worth
about 13 M€ in generation 1 of a 9-generation game, 8 M€ with four generations left, and exactly 5 M€
(one VP) in the last generation. In that last generation, 2 TR for 17 M€ is a loss, not a fair trade.
Heat and plants follow the TR rate. Release of Inert Gases (17 M€ including the purchase, for 2 TR)
breaks even with about 3–4 phases left: better earlier, worse later.

**Card test:** compare total value (VP × 5 + TR × that rate + resources + production) with `cost + 3`.
Preludes and corporations have no purchase price. `tfm value` applies these rates for its estimate of
the generations left (shown on its OUTLOOK line; override with `--gens`) and flags many hidden effects.
When a card's value has a `? custom code` line, work out the real effect yourself: the estimate can be
far off.

Standard projects are usually worse than these rates (Asteroid: 14 M€ for 1 TR, which only breaks even
with 9 phases left; Greenery: 23 M€ for 1 VP + 1 TR, i.e. 10 M€ + 1 M€ per remaining phase).
They are a ceiling on what to pay when no card does better, and a use for spare final-generation cash.

### Value depends on time

| Resource | Main question |
|---|---|
| M€ | Which specific action does the next unit enable? |
| Steel / titanium | How much can be spent on suitable cards before the game ends? |
| Plants | Will these reach a greenery before attacks or the final conversion? |
| Energy | Is it needed for a trade, a card action or a production cost? Budget each unit once |
| Heat | Are temperature steps, or another explicit heat use, still available? |
| Cards | Is there affordable play value, a scoring count, or only sale value? |
| Tags | Do they unlock a requirement, milestone, award, trigger or multiplier? |
| Card resources | Can a rival's action (Ants, Predators and similar) steal them before they score? |
| Corruption (Underworld) | Will it block an attack, or cancel negative card VP at the end? |

Titanium with nothing left to spend it on is worth much less than cash; the one plant that completes 8
can matter more than several that stay stranded. Count only **spendable** production payouts, and only
the discounts you will actually use. For repeatable actions, count the affordable, useful uses left.

When a rival has a stealing action, spend card resources as soon as they reach a threshold, or block
the attack with corruption.

### Hidden value: what a card triggers, enables, counts toward and denies

Before buying or ranking a card, ask:

- **Who will trigger it?** An effect fed by every player grows with the table.
- **What does it unlock?** A cheap card that completes a threshold, meets a requirement or finds a key
  card can beat an expensive one.
- **Does it add an action?** A free trade, a fleet reuse, or returning events to hand is an extra action.
- **What does it feed?** Cards that put animals, microbes or data onto other cards score through their
  targets.
- **Which tags does it bring?** Count every requirement, milestone, award, planetary track and
  per-tag payout it moves. A per-tag payout is worth more after cheap cards with that tag are down, and
  a "VP per tag" card makes every later card with that tag worth more.
- **Does it count for an award?** Cheap cards can decide a count-based award late in the game.
- **What does it deny?** A cheap card that removes points from the leader can swing a close game.
- **Does it cross a bonus threshold?** Global parameters and Pathfinders tracks pay extra at fixed steps
  (oxygen 8% raises temperature; 0°C places an ocean). Follow the chain: one step can set off several.
- **Is cash now worth more than cash later?** Money for a contested action this turn (an ocean while
  oceans remain) beats the same money next generation.
- **What is the payment really worth?** A card paid mostly with otherwise dead titanium is cheaper than
  its listed cost.
- **Does taking the last step deny someone?** Taking the last parameter step strands rivals' saved heat
  or plants and can end a rival's repeatable action early.
- **What does it cost hidden?** A tag can lock you out of a later effect; raising a parameter may pay
  rivals' triggers; track rewards that go to everyone help rivals.
- **How many payouts are left?** Production bought in the last two generations rarely repays its cost.

### Payments

The cost `tfm` shows in a play choice already includes your discounts; `tfm card` shows the printed
cost. `tfm` suggests a payment (metal first); override it, e.g. `play 1.4 mc=4,steel=3`, to save a
scarce metal you need for an action with no cash alternative.

---

## 7. Milestones and awards

### Milestones: check every turn

`tfm` lists every milestone with your score, the best rival's, and `<<< YOU CAN CLAIM` when you qualify.
Recheck after **every** play that changes tags, production, TR, hand size, colonies or tiles: it's easy
to qualify on the spot and not notice.

Milestones go early: all three have usually been claimed by generation 2–6, and agents who waited ended
with 0 milestone VP. In generation 1, pick the milestone you will race and play the cards that move it
first. In a race, count rivals' tiles and tags every turn and save the cost of the last tile plus the
8 M€ claim before anything optional. Qualifying is not scoring: claim a contested milestone as soon as
you can; an agent has lost Rim Settler by one action.

- **Draft toward the missing piece.** Don't sit one tag short hoping to draw it: an agent stayed at 3 of
  4 plant tags for Agronomist from generation 1 until every milestone was gone.
- **Don't play away your own progress.** An agent leading the Energizer race at 4 energy production
  played cards that lowered it and never claimed.

### Awards: project the finish

Before funding, estimate your final score on the award and each serious rival's reachable score.

**Is the lead safe?**

- **Your engine versus theirs.** Fund counts your own engine keeps raising (production, cards played,
  tiles), not stock you'll spend: a lead built on stock vanishes when it's spent. But rivals' engines
  grow too. On a production or cash award (Banker, Magnate), one big card can swing it: a 12-to-1 Banker
  lead funded in generation 1 and a 10-to-7 lead funded in generation 4 were both overturned (one by
  Interplanetary Trade). Count what each rival could still add.
- **Thin leads and ties in a count anyone can raise cheaply are weak.** Read each rival's played cards
  for ones that raise the count by themselves, and don't fund a tie a rival with more cash can break:
  the fee can buy them first place. On a narrow card-count lead, don't pass first; keep a cheap card to
  play last, or pass after them.
- **Before funding a board award,** count each rival's open city sites next to greeneries or oceans
  that they could fill in the last generation.

**When to fund**

- **Fund a safe lead as soon as you can afford it.** An unfunded lead scores nothing, slots run out as
  fast as milestones, and funding early locks the cheaper fee. An agent leading Constructor waited and
  lost the last slot. Letting a rival fund an award you will win saves the fee only if nobody can take
  the last slot first.
- **Fund an award on something nobody else is collecting,** early while it's cheap, then keep drafting
  for it.
- If you lead nothing you can protect by mid-game, plan your points elsewhere instead of waiting for a
  slot.

**Special cases**

- **An award that counts stock you'd otherwise spend has a running cost.** Holding heat for a heat
  award means not converting it while temperature is still open. Re-project each rival's final stock
  from their production every generation, and count a rival's energy as heat too: it becomes heat at the
  next production phase. A production attack can protect a lead; play it only after the rival has passed.
  Re-check standings before spending resources an award counts.
- **A resource-count award against a rival with a stealing card is weaker than it looks:** each steal
  moves the gap by 2. Count every possible steal before you spend resources the award counts.
- **Tourist counts distinct empty adjacent spaces, not a sum of each tile's empty neighbours.**
  Placing your own tile removes its space from that count and adds only previously uncounted empty
  neighbours. Recalculate the union after rivals' final greeneries and before your own final placement.
  A greenery worth one extra city point can be worse if it turns your sole award lead into a tie and
  gives the strongest rival three additional award points.

---

## 8. The map and game pace

### Judge a placement before choosing it

For each candidate space consider printed bonuses, ocean adjacency (2 M€ each), special-tile bonuses,
adjacency rewards paid to rivals, hazard costs and placement fees, city scoring, award progress, room to
expand, and which spaces you open or deny. `tfm` lists each legal space with its bonuses, token and
neighbours; `tfm board` shows every tile.

A city next to several greeneries is efficient points and denial; one surrounded by spaces you can't
fill is weak. Greeneries next to a rival's city can still be worth it, but count the points you give
away. Identify open city sites beside your greeneries before spending your last cash: a rival can
build there after you pass.

A card that turns one of your greeneries into a city (Kaguya Tech) changes your score by: −1 for the
greenery, −1 for each city next to it, +1 for each greenery next to the new city. Count before
converting: a greenery between two cities can lose points.

**Get on-map tiles down early.** A ground game started late leaves few good spaces and few board points.

- **City deadline:** have an on-Mars city by generation 3–4, whatever your strategy. Agents whose first
  city came in generation 7–8 lost 9–15 board VP to the winner.
- **Back it with plant production.** 2–3 plant production hasn't been enough to keep greening around
  your city: a leader with about 15 out-scored every agent on the board by 16–19 VP.
- **Pick city sites with open, hazard-free neighbours.** A city boxed in by erosion hazards or rival
  tiles can't be surrounded by your own greeneries.
- **Surround your own city yourself.** Rivals build cities next to your greeneries to score them, so
  place your greeneries next to your own city, or put your city next to them first.
- **Late greeneries may be forced next to rivals' cities.** Before paying 23 M€ for a greenery standard
  project, check the legal spaces: an agent's last greenery could only go on a space touching three
  rival cities, giving each of them +1 VP. When the board fills completely, there may be no space at
  all for a final greenery (see §2).
- **Off-map cities** don't count toward any of this (§2).

### Ending versus extending

**Game length.** Most 4–5 player games last 7–10 generations, and recent ones have often ended after 7
or 8. Estimate the generations left yourself from the remaining steps on each parameter: `tfm`'s own
estimate has been off by up to three generations.

Track the slowest global parameter, and watch rivals' cash for standard projects as well as their
production. When two parameters are close to finished, a burst of greeneries and oceans can finish them
in one generation, earlier than the pace suggests. Switch to points then, not when an estimate says so.
Forecast each parameter separately from rivals' corporations and production: specialists can close one
parameter early, and cards that raise it lose their value when they do.

Estimate what one more generation is worth to you and to your strongest rival. If they gain more from
repeated scoring actions, speeding up the end can be right; if you gain more, don't close the last
parameter unless the immediate points make up for it.

At 4–5 players, holding back one step rarely extends the game: anyone else with heat or plants closes
it, and the TR you held back is lost. Unless you hold the only realistic way to finish a parameter, take
your step.

**Confirm the end from the server, in both directions.**

- **Don't sell early.** Never sell VP cards or dump resources expecting the end until the server state
  shows every required parameter is actually maxed, Venus included when the game requires it. `tfm` has
  shown "1 generation left" from generation 7 in a game that ran to 10, and one game ran a generation
  past the point where temperature, oxygen and oceans all looked maxed, after two agents had sold cards.
- **Don't hoard.** Once the end is genuinely in sight, turn every M€, plant and heat into points (§10).

---

## 9. Expansions

### Colonies

A standard trade costs 9 M€, 3 energy or 3 titanium; a colony costs 17 M€. Normally each fleet trades
once per generation and each tile can be traded with once per generation. `tfm` lists each trade's
current reward. **You get the trade reward only**: the listed colony bonus goes to each colony owner on
the tile (you included only if you own one there).

Judge a trade by its usable reward minus the real cost of what you paid with; titanium or energy with no
other use is cheap. A colony is worth its placement reward plus owner bonuses that rivals will actually
trigger. Read community colonies' current app text: some give VP for building there, which can beat a
greenery late in the game.

A Mercury trade gives production of the type under its track marker, and that type changes as the
marker moves. Check the current type before trading for a specific production.

Cheap card resources from trades can become TR through cards that convert them (microbes into
temperature or TR, for example). Look for a trade that supplies a converter you already own.

A **Terra** trade raises the global parameter "as World Government": the trader gets **no TR** and
nothing else. It only speeds the game end and pays any colony owners.

A **Titania** colony is good early VP (5 VP for the first colony, about 17 M€), but each colony you own
there costs you 3 M€ every time anyone trades there. When rivals trade there most generations, that
drains a lot of cash; count how often they will.

A **Hygiea** trade steals from rivals: check what it would actually take before paying. An agent paid
3 titanium to steal 1 heat.

### Ares

Hazards (erosions and dust storms) change as parameters advance, and mild ones can turn severe.
You can place a tile next to one or **on top of** it:

| Hazard | Place next to it | Place on top of it |
|---|---|---|
| Mild | Lose 1 production of your choice | Pay 8 M€, gain 1 TR |
| Severe | Lose 2 units of one production of your choice | Pay 16 M€, gain 2 TR |

Covering a hazard is one of the cheapest TR sources in a short game: a greenery or city on a Severe
hazard gives +2 TR on top of the tile's own value, and it has decided close games. Look for hazard
spaces next to your own tiles when planning greeneries and cities.

Bonus tiles pay whoever places next to them and their owner. An animal placement bonus is lost if you
have no animal card in play. Ocean upgrades don't place a new ocean or give ocean TR. Treat each
placement as a complete transaction.

A tile with a placement restriction may have only one legal space, and it may be next to a hazard.
Before buying such a card, check its legal spaces for hazards. When the hazard's own space is legal,
compare covering it (fee, plus TR) with building next to it (production lost). In the final
generation that production loss costs almost nothing. `tfm`'s space list shows a hazard on the space itself (`ON Mild Erosion: costs
8 M€ extra, +1 TR`) and the production lost to hazards next to it (`next to hazard: lose N production`).

### Pathfinders

Planetary tags (including event tags when played) advance the matching tracks; requirement modifiers
don't affect them. Rewards may go to the triggering player, to everyone or to the tag leader. Look at the
next threshold before choosing card order: crossing a reward now can be worth a lot, but a reward that
goes to everyone may help a rival.

### Underworld

Excavating costs 7 M€ (steel allowed) and usually has to be next to your tiles or excavations. An
excavation marker isn't a surface tile: no ocean adjacency, no greenery or city placement. Placing a
tile removes the token beneath it. Compare a blind dig with a concrete alternative; a known token that
completes a threshold is easier to value.

Excavation can be an opening engine: repeated digs on identified tokens can pay for themselves and
move dig-related milestones early. Cards that dig while ignoring placement rules can take a known
production token anywhere. Some tokens last only for the current generation, so use their effect
right away. The Excavate standard project shows its spaces only after you pay: check the identified
tokens next to your tiles (`tfm value` lists them) before paying.

Corruption has two uses. It blocks qualifying attacks (damage or theft) against you, and at the end of
the game each corruption you still hold cancels 1 VP of negative card points (shown as "Underworld
Corruption Bribe" in the breakdown). So value corruption by your negative-VP cards. Corruption beyond
your negative card VP is only worth its blocking use. Before paying for an
attack, check whether the target can block it or halve the loss. Some printed rules aren't implemented
in the app.

Holding corruption, or crime tags, has costs too: some cards punish the player with the most
corruption, or reward players with no crime tags. Other cards spend corruption for points, which makes
corruption from digs worth more. In the app, Chemical Factory's action excavates but gives no
corruption, so don't count on it as a supply.

### Venus

Venus can max out very early with several Venus players and big Venus events, and cards bought for the
steps lose their value at once. With several Venus players, price a Venus-raising action only on the
steps you can take in the next generation or two; Venus *tags* keep their value.

Check whether Venus must be completed to end the game. Air Scrapping gets 1 M€ off per persistent Venus
tag (up to 5); on the alternative board each step beyond 16% also gives a chosen resource, and the 30%
step a **wild resource, not a wild tag**. There is no general heat-to-Venus conversion. When a prompt
asks for a resource, choose the one that completes your best sequence.

### Prelude, Turmoil, Moon and others

Preludes change starting cash, tags, production and immediate options; a Prelude 2 prelude with an
action is not a blue project card. An unplayable prelude gives 15 M€ under the app's rules. A prelude
that makes you discard cards (Project Eden) counts as unplayable with an empty hand: keep cards in hand
when you play it, or you get 15 M€ instead of its city, greenery and ocean. Turmoil and
Moon inputs work generically in `tfm` but are untested: read their rules before planning.

---

## 10. Endgame procedure

When the game may end this generation, stop engine planning and compare concrete scoring bundles.

**`tfm vp` is not a ceiling for rivals.** It estimates them from public information and has
underestimated the leader's final score by about 6–24 VP, since hidden hands and late engine plays don't
show. Leave that margin before ending the game on its projection. Give resource-and-cash engines a
separate final-generation upper bound: Henkei Genetics, Viral Enhancers, Ants, Nobel Labs and Meat
Industry turn resource additions into both points and cash for further plays, so project the public
trigger chain, spendable metals and remaining hand size together. In Massive Mineral Unit, a late
forecast put rm0331 near 82 VP; the actual finish was 104, including 39 card VP.

1. List points available now: TR still gainable, card VP, resource-card thresholds, cities, greeneries,
   colony or track rewards, awards.
2. Project final plants (stock + production + placement rewards). Count complete greeneries.
3. Take contested opportunities first: milestone claims, city sites, trade slots, the last parameter
   steps. Read your own cards' side effects before playing them (convert plants before a card that
   removes your plants).
4. Use worthwhile once-per-generation actions, including free trades for card resources.
5. Sell dead cards when the proceeds enable a better action.
6. Spend leftover cash on standard projects that still score: Air Scrapping while Venus is open, a
   greenery next to your city, a colony with VP.
7. Check the whole payment sequence, including triggered resources, and the projected clock penalty.
8. Pass only after an explicit scan for missed points and useful actions.

Cash shown after final production is mostly final income. Cash left during the action phase that could
have bought points deserves scrutiny: an agent finished a 7-generation game with 66 M€ unspent and four
cards in hand, roughly 8–10 VP wasted.

**Passing also ends your ability to spend later reactive payouts.** For Hospitals, distinguish an
existing disease resource from a hoped-for future city trigger: its income action needs a disease.
If an opponent can still build cities, consider preserving useful actions and spending flexibility
until their city plays are resolved. In Massive Mineral Unit, two diseases arrived after gpt had
passed, so neither could fund further scoring. A zero-value action used as a delay is only useful if
the resulting legal choices actually let the turn return to the opponent without passing.

---

## 11. Turn routine with `tfm`

At the start of each generation, before the draft:

- Estimate the generations left yourself from the board and pass the result to `tfm value --gens N`.
- Run `tfm vp` to see where you stand, and `tfm value` on the cards you're thinking of buying.
- Compare your TR, card VP, board VP, milestones and awards with the leader's. Name your weakest
  category and make fixing it this generation's priority.
- Recheck which milestones and awards are still open and what you need to take one.

On each `tfm turn`:

1. Scan `SINCE YOUR LAST LOOK` for what opponents did, and `MILESTONES` / `AWARDS` for anything you or a
   rival can now claim or fund.
2. Glance at the clock line.
3. Pick the action. If it creates a choice that could change your second action (a draw, a placement),
   decide the second after seeing the result. Choice ids renumber after every action, so never chain two
   `play` commands without reading the list `tfm` prints in between.
4. Keep brief notes between turns: target milestones, reserved cash, planned city sites, estimated
   generations left.

Keep reasoning short on routine turns. A notes template, if you want one or need to hand the game over:

```yaml
generation: ?
estimated_generations_left: ?
plan: ?                     # main scoring routes
weakest_category: ?         # vs the leader: TR / cards / board / milestones / awards
reserved: ?                 # cash/resources held for a specific action
milestone_targets: []
award_view: ?               # which funded awards you expect to win or lose
city_sites: []              # spaces worth a city later
final_greenery_projection: ?
strongest_rival_and_threat: ?
```

Unknown means unknown. Don't fill gaps with plausible numbers.

---

## Sources

When this guide and the live app disagree, the app wins.

- Base rulebook: https://fryxgames.se/wp-content/uploads/2023/04/TMRULESFINAL.pdf
- App FAQ: https://github.com/terraforming-mars/terraforming-mars/wiki/FAQ
- Detailed rules FAQ (unofficial): https://github.com/terraforming-mars/faq/wiki
- Prelude rulebook: https://cdn.1j1ju.com/medias/6e/a5/22-terraforming-mars-prelude-rulebook.pdf
- Alternative Venus board: https://github.com/terraforming-mars/terraforming-mars/wiki/Alternative-Venus-Board
- Pathfinders: https://github.com/terraforming-mars/terraforming-mars/wiki/Pathfinders
- Ares: https://github.com/terraforming-mars/terraforming-mars/wiki/Ares
- Underworld: https://github.com/terraforming-mars/terraforming-mars/wiki/Underworld
- Escape Velocity, variants and options: the `EscapeVelocity`, `Variants` and `GameOptions` pages of the app wiki
- Card list: https://terraforming-mars.herokuapp.com/cards
