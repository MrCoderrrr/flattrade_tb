# NIFTY v5 paper research

NIFTY v5 is a **paper-only** strategy. It starts with one ATM short CE and one
ATM short PE, plus matching long CE/PE wings at least 1000 index points away.
All four legs use the same expiry and lot size. The controller rejects missing,
stale, crossed, or insufficient-depth books and computes the basket's
expiration worst case from simulated executable prices before recording entry.
The strategy can be armed from the dashboard outside market hours and begins
only when its next entry window opens. Its entry window is 09:20–15:33 IST;
the paper flatten deadline is 15:34 IST.

The index WebSocket and completed one-minute bars feed an independent sampler
once per second. Its bounded flow score combines 15-second impulse, EMA
direction, five-bar breakout, and acceleration. Weights depend on directional
efficiency, five-minute ADX when available, and volatility. A volatility shock,
stale index, missing minute, or missing 15/30-second observation blocks new
short sales. The sampler retains actual observations only; it does not invent
missing seconds. The strategy controller may act later if the broker quote
requests are slow, but it rejects stale entry signals and never fabricates a
fill.

Three observed fresh seconds open the balanced basket, including on a trend
day. In a balanced basket,
a sustained bullish score closes the CE short; a bearish score closes the PE
short. An urgent score can close the losing leg immediately. An individual
premium stop can also close either short. The opposite short and both wings
remain. Five seconds of trend cooling, or three seconds of reversal, can
restore the missing short at its **original strike** while its long wing is
still held. The original strike and all held wings remain subscribed in the
one-second option-chain collector after spot moves.
If the held basket uses a later expiry than the nearest listed expiry, the
collector switches to that exact expiry while the position remains open.

Each short has an adaptive premium stop, hard-capped at 30% above its entry
premium, and a trailing stop on a solo or sufficiently profitable leg. Higher
chop and volatility give a wider initial stop/trail; stops normally only
tighten. If a profitable solo leg reaches its trail at the same moment that a
valid re-entry is confirmed, the controller restores the missing short and
resets the held short's trail instead of closing and rebuilding the entire
basket. It otherwise closes the triggered short. If no short remains, the
controller releases both wings. The strategy also has a ₹4,000 per-multiplier
session loss trigger, an account-wide drawdown guard, and a 15:34 flatten
deadline. Stops are **triggers, not guaranteed fill prices**. A one-lot basket
with 1000-point wings can have a theoretical loss much larger than ₹4,000 or
5% of a ₹2 lakh account after a gap or feed failure. The dashboard displays
the computed basket maximum loss; do not mistake the trigger for a cap.

The ML tab continues to train its 15-feature directional/risk model after a
session, using at most the latest 60 calendar days and holding out the latest
usable day. It now also fits a regularized **action-value research model** for
CE/PE hold-versus-close and re-enter-versus-wait outcomes. Targets walk the
original-strike quoted ask path through a five-minute horizon with a premium
stop/trail proxy; input features come strictly from the decision minute.
Training weights are balanced by day with a modest recent-day tilt. The ML tab
shows held-out sign accuracy against a constant baseline. The candidate is
saved after the test for later research, **never connected to v5 decisions or
broker execution**. Historical one-minute CSV data misses intraminute stop
crossings and may lack book depth; future one-second captures include quote
ages and displayed sizes. The current few usable days cannot establish a
reliable edge or justify live orders.

All fills are simulated at the displayed bid/ask plus one tick and an
estimated cost allowance. No broker order endpoint is imported into this
controller. Session paper results and ML quote-path diagnostics are separate
from realized brokerage-account P&L.
