# NIFTY ML research plan and current implementation

The ML laboratory is **read-only research**. It does not start a strategy,
submit orders, change any stop in the live controller, or authorize live
trading. The aim is to test whether a model adds repeatable value to a hedged
ATM short straddle before exposing any paper strategy to it.

## Data audit and collection

The supplied data/option_chain/nifty_oc_*.csv folder contains seven
one-minute NIFTY chain days (19–27 August 2026), not one-second history.
Four days (19, 20, 21, 24 August) have constant ATM call IV and open interest
throughout the day. Their origin cannot be established from the CSV, so they
are retained in the audit and excluded from fitting and validation. The three
variable-field days (25–27 August) remain **unverified historical quotes**,
not proof of actual executable depth. Sibling trade_log*.csv files are
indexed as reference records; their simulated/legacy fills are never labels
or evidence of model profit.

During the NIFTY session, the dashboard's existing read-only broker stream
is sampled once per clock second into data/strategy_lab/ml.sqlite3,
independently of whether a strategy is running. Each row preserves the
option-book age. Stale or absent quotes are recorded but excluded from
learning. The stream watches near-ATM strikes plus the nearest strikes about
1,000 points on either side, rather than requesting an entire chain every
second. The collector cannot invent ticks between exchange updates. Missing
market data remains missing. Live payloads are compacted and compressed;
the collector retains 90 days of raw live snapshots to bound server disk use.
Historical imports and aggregate reports are retained.

## Decision model

The first model is a robustly scaled, regularized three-class softmax model
for the NIFTY's next five minutes: down, flat, or up. A separate regularized
model estimates each ATM short leg's next-five-minute adverse ask excursion,
which supplies an experimental adaptive stop/trail width. Inputs at a
decision contain only already-observed snapshots and completed preceding
bars:

1. One-minute spot return
2. EMA(8)–EMA(21) distance
3. KAMA(10,2,30) slope
4. Fourteen-bar DMI trend strength
5. RSI(14)
6. ATR(14) divided by spot
7. Ten-return realized volatility
8. Ten-return path efficiency
9. Position in the previous 20-observation range
10. ATM straddle premium divided by spot
11. ATM bid/ask spread fraction
12. Put–call open-interest imbalance
13. Put–call IV skew
14. Days to expiry
15. Fraction of the NIFTY session elapsed

Training uses the most recent 60 calendar days **relative to the test day**.
Each day gets near-equal total weight, with a maximum 1.5:1 recency factor;
minute count cannot make one day dominate. The newest eligible day is held
out completely. The model fitted on earlier days is scored and replayed on
that newest day. Only after this evaluation is a new research candidate
fitted through the newest day for the *next* session. The collector tries
this once after 15:42 IST on a day with fresh observed data. It never
re-fits intraday against the day being measured.

The dashboard reports ordinary and balanced classification accuracy,
the majority-class baseline from **training only**, label counts, and a
quoted-price replay. The replay enters one ATM short CE/PE basket with
1,000-point protective wings, marks short legs at ask and wings at bid, and
executes model decisions on the **next** observed minute. A trend signal can
close the adverse short leg; the remaining short has model-informed stop and
trail widths, while a reversal can re-enter the missing short at the original
strike only when close enough to ATM. It closes on an observed quote no later
than 15:25. Missing held-leg quotes make a replay incomplete; no P&L is
reported for that session. The chart uses premium **points per one option
unit**, before brokerage, taxes, impact, and partial fills. It is not rupee
profit, a capital return, or an actual fill record.

## Initial result and promotion gate

Initial imported data provided two training days (25–26 August) and one
held-out day (27 August). The held-out day has 299 five-minute labels.
The model scored 38.8% ordinary and 34.9% balanced accuracy, versus a
36.5% training-majority baseline. This is weak evidence and **does not
qualify as a trading edge**. One-day replay results are displayed to help
audit behavior, not to establish expected returns.

No ML policy can control broker orders. Before even considering a dedicated
paper strategy, require at least 20 independent training sessions and five
rolling out-of-sample sessions, consistent improvement over fixed-rule and
hold-to-close baselines after complete fees and conservative fills, acceptable
worst-day loss/drawdown, and no material data gaps. Model updates remain
versioned candidates; a newer fit is not automatically considered better.
Deep temporal models or offline reinforcement learning are later experiments
only after this data/replay layer is credible. Offline RL can overvalue
actions absent from prior experience, so it must not learn by exploring with
real capital. See the original [Conservative Q-Learning paper](https://arxiv.org/abs/2006.04779)
and [Deep Hedging](https://arxiv.org/abs/1802.03042).

## Operation

Import the supplied files on a machine that can read their directory:

    python3 -m strategy_lab.ml_research import --root . --option-chain '/path/to/data/option_chain'
    python3 -m strategy_lab.ml_research train --root .

The importer also finds sibling backtest/spot_1m and logs/trade_book
folders by default. Reimporting the same day is idempotent. The separate
SQLite store is ignored by Git; deploy it privately to the server's
data/strategy_lab/ directory. The authenticated /api/ml endpoint serves
only the aggregate report, data counts, and simulated events to the ML tab.
