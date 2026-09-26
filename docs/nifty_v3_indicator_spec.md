# NIFTY v3: one-second indicator strategy specification

Status: decision specification with a local, pure signal prototype and a
read-only WebSocket collector. The dashboard's deployed `nfv3` still uses the
earlier one-minute EMA/RSI vote; this document does not change its trading
behaviour. No live broker orders are authorized.

## Position and clock

One unit starts with one short CE and one short PE at the same nearest ATM
strike, plus a long CE 1,000 NIFTY points above its short and a long PE 1,000
points below its short. This is an ATM iron butterfly in payoff terms; after
the index moves, retaining an older surviving short while adding the missing
short at a new ATM can make the two short strikes different. Enter no earlier
than 09:20 IST, stop new entries before 15:33, and request flatten by 15:34.
Keep every hedge until its paired short is closed; purchase a replacement hedge
before adding or rolling a short. Never infer that four orders fill atomically.

## Feed and cadence

Use Flattrade's authenticated WebSocket touchline subscriptions for NSE|26000
and the selected NFO option tokens. Its `tk` event is a snapshot; later `tf`
events contain *only changed fields*. Merge deltas by exchange/token and
clear the cache on reconnect before accepting a new snapshot. Keep separate
receive time and broker event time; never label receive time as exchange time.
Subscribe to the held strikes, current ATM candidates, and the strikes 1,000
points beyond each candidate. Change subscriptions as ATM changes; do not scan
14 near-ATM strikes and expect to find a 1,000-point hedge.

The engine wakes once per second. It may process multiple feed events within a
second. It evaluates every open short's protective stop, then makes at most one
new-exposure decision from the latest coherent snapshot. When no new event
arrives, it records an idle heartbeat, not a fabricated price. For new entries,
require an index update within three seconds, each relevant short-option book
update within five seconds, and wing book data within ten seconds.
Missing/partial book data blocks entry. An existing
position remains recorded when a stale book prevents an executable exit; the
dashboard reports exit pending. Per-second broker event and decision records
are persisted for deterministic replay.

Completed one-minute bars update EMA, KAMA efficiency, and ATR once each minute;
completed five-minute bars update ADX. A *provisional* current-minute price
may contribute to the one-second impulse and breakout but cannot rewrite a
completed bar. Warm up volatility estimates on prior sessions without counting
the overnight gap as a one-minute true range. Start capturing data at 09:15
even if trading authorization is provided later.

## Inputs and adaptive score

The following are research starting values, not fitted parameters:

* `EMA(8)-EMA(21)` on completed one-minute closes from the **current session**:
  persistent direction, divided by ATR(14) to make it comparable across
  volatility regimes. Both EMAs seed from today's first close; yesterday's
  direction must not bias the 09:20 signal.
* NIFTY movement over the latest 15 seconds and one minute: fast impulse and
  its change over the preceding 15 seconds. Calculated from observed ticks.
* A break beyond the previous five-minute high/low: direction and remaining
  room, normalized by ATR. The current tick is excluded from that prior range.
* Kaufman efficiency ratio on five completed one-minute bars from 09:20,
  extending to ten bars after enough current-session history: high means a
  directional path; low means oscillation.
* ADX(7) on completed five-minute bars only after enough bars from today's
  session exist. It adjusts trend confidence, never independently vetoes a
  signal. Before then, efficiency supplies trend quality.
* ATR(14) and ATR relative to its recent median: volatility scale and shock
  detection. NIFTY index volume is not used as a VWAP proxy.

Clamp each directional component to [-1, 1]. Let `q` in [0, 1] be smoothed
trend quality from efficiency and, once valid, ADX. Let `v` be current ATR
divided by its recent median. Compute once per second:

`Flow = 100 × (0.80 + 0.20q) × shock_damper(v) × tanh(1.5 × sum(weight_i(q,v) × component_i))`

The quality factor is intentionally shallow. Quality already changes the
component weights and decision bands; multiplying by a steep quality penalty
would make a low-efficiency early breakout incapable of reaching any exit
threshold. `shock_damper(v) = 1/(1+0.4 max(0,v-1.3))`.

Weights sum to 1. Interpolate continuously between these starting regimes;
do not switch them abruptly at a boundary:

| Situation | 15s/1m impulse | EMA direction | Five-minute break | Acceleration |
| --- | ---: | ---: | ---: | ---: |
| Choppy (`q < .35`) | .20 | .20 | .45 | .15 |
| Transition (`.35 <= q < .65`) | .35 | .20 | .30 | .15 |
| Established trend (`q >= .65`) | .20 | .45 | .10 | .25 |

The high breakout weight in chop can only matter when the efficiency factor
also rises; a one-tick breakout is not enough. For elevated volatility,
gradually shift 10 percentage points from impulse to EMA and lower confidence.
At `v > 2.2`, block *new* short sales for three completed one-minute bars while
continuing stops and exits. Do not interpret a shock as a reliable direction.

## State transitions

The one scalar `Flow` drives direction. Option quotes, margin, and the stop
ledger are independent safety checks. The score band adapts to the last 60
observed seconds. Let `n` be the median absolute change between consecutive
one-second Flow scores, requiring at least 20 valid changes (otherwise `n=0`).
Let `q` and `v` be the latest quality and volatility ratio. Before any order,
compute these candidate bands using past/current observations only:

| Band | Formula in Flow points |
| --- | --- |
| Exit threatened leg `E` | `clamp(55 + 8 max(0,.35-q)/.35 + 8 max(0,v-1) + min(12,1.5n), 55, 72)` |
| Urgent exit | `min(90,E+20)` |
| Fast-jump exit | `E-5`, if Flow changed >=25 points over two consecutive observed seconds |
| Open both | `max(32,45-0.6(E-55))` |
| Restore missing leg `R` | `max(12,20-0.45(E-55))` |
| Opposite reversal | `max(35,E-20)` |

This keeps at least 35 points between the persistent exit and ordinary
re-entry bands. Higher noise/chop/volatility makes opening or restoring shorts
harder while allowing a strong, rapid directional exit. The bands are symmetric
for up- and down-moves. A repeated last tick, missing second, stale feed, or
unqualified quote never counts as confirmation. They are hypotheses, not
backtested optimal values.

| Current state | Trigger from observed Flow | Action |
| --- | --- | --- |
| Flat | `|Flow| < open-both band` for three fresh seconds, valid book, risk room | Open paired ATM shorts and 1,000-point wings. If trend is already strong, wait instead of buying then immediately closing a leg. |
| Two shorts | `Flow >= E` for three seconds, or `>= urgent band` for one second | Buy back the threatened short CE. Keep the PE short and its hedge; enter solo-PE mode. Mirror for negative Flow and PE. |
| Two shorts | Flow jumps by >= 25 points in one observed second and reaches `|Flow| >= E-5` | Early version of the same threatened-leg exit. |
| Either short | Its own hard premium stop is reached on a fresh executable ask | Close that short regardless of Flow. A stop on both shorts leaves the basket flat. |
| Solo PE after up-move | Flow retreats to `<= +R` for five seconds | Re-enter CE near current ATM with a new CE hedge, provided quoted premiums have stabilised and basket risk remains within budget. Mirror for solo CE. |
| Solo PE after up-move | Flow reverses to `<= -opposite-reversal band` for three seconds | Re-enter CE, but also evaluate PE as the newly threatened leg. A strong opposite signal switches solo side; it need not restore both shorts. Mirror for solo CE. |
| Solo short | Soft trailing level reached and missing-side re-entry is *already* qualified | Preserve the profitable surviving short if its strike and paired hedge are still valid, retain its original cost basis, add the missing hedged short, and tighten/re-anchor the combined stops. Otherwise close the survivor. |

After a unilateral stop, wait at least 15 seconds before restoring that side;
after complete flattening, wait 60 seconds before another full basket. Repeated
false breaks (for example three unilateral stops within 15 minutes) trigger a
ten-minute churn pause. There is no trade quota; loss and order-rate limits
remain. Exceptional one-strike OTM re-entry is allowed only when Flow crosses
zero at least three times in ten minutes, median absolute Flow is below 20,
and expected credit remains above spread, slippage and fees. Otherwise use ATM.

## Option stops and risk

For each short, calculate the initial hard stop against its actual sell fill
using its executable buyback ask. Candidate premium allowance is
`clamp(18% + 8% × (1-q) - 6% × |Flow|/100, 12%, 30%)` and must exceed the
entry spread/tick buffer. This allowance is set at entry. Subsequent
recalculation may *tighten* an open hard stop, never widen it after an adverse
move. After the other short exits, track the lowest fresh buyback ask for the
survivor. Its candidate trailing allowance is
`clamp(8% + 7% × (1-q) - 3% × |Flow|/100, 5%, 15%)` above that low. The
actual trailing level ratchets only toward profit. A soft trail is a possible
rebuild trigger; a hard stop and the account/session circuit breakers cannot
be waived by rebuilding.

With 1,000-point wings, the expiry payoff risk of one complete basket is
approximately `1,000 × current lot size − executable net credit`, plus costs.
For example, a 65-unit lot with ₹13,000 net credit leaves about ₹52,000 of
maximum payoff loss before costs. This conflicts with the current `nfv3`
₹10,000-per-unit entry cap and 5%-of-capital theoretical-risk check. The
implementation must resolve that explicitly; silently bypassing the checks
would misrepresent the risk. A proposed paper-only maximum of 35% of declared
capital per one-unit basket (₹70,000 on ₹2 lakh) could admit the example above,
but needs explicit acceptance because a gap can skip the much smaller session
stop. Broker margin, current exchange lot size,
round-trip costs, market depth, and all partial-basket payoff states must be
verified before activation. Stops limit intended exposure but cannot guarantee
an execution price.

Separate the *score decision band* above from the two monetary gates. The
declared sizing capital must be at least ₹2,00,000 × multiplier; this is a
sizing convention, not proof of available broker funds. Before each new hedge
or short, compare current broker `get_limits` with the projected required
margin and a liquidity reserve; if the broker does not provide a dependable
forward estimate, fail closed. Independently calculate worst-case expiry
payoff for the complete basket and each possible partial-fill state, plus
conservative costs, against an explicitly accepted maximum payoff-risk budget.
The executable entry credit must exceed estimated round-trip spread,
brokerage, exchange charges and slippage. No favorable Flow score can waive
either monetary gate. An available-margin reading alone does not establish
that the next multi-leg order will be accepted.

## Acceptance evidence

Before this supersedes the deployed NIFTY paper strategy, test one-second
tick/reconnect/stale-book cases, incomplete bars, long and short trend changes,
single-leg stop/TSL/rebuild, hedge rollover order, and expiry payoff on every
partial state. Replay complete timestamped index and option bid/ask/depth data
with actual costs, then run a forward paper period. Optimize the few candidate
thresholds on earlier dates and judge them on later untouched dates. No ML
or automatic parameter learning is part of this version.

Broker stream reference: https://github.com/flattrade/pythonAPI (WebSocket
`subscribe`, `tk` full snapshot and `tf` changed-field updates). Exchange
contract specification: https://www.nseindia.com/static/products-services/equity-derivatives-contract-specifications
