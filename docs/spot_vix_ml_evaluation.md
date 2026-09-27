# Spot/VIX research evaluation, 28 September 2026

This is a **read-only research model**. It cannot place orders, change stops,
or control a strategy. The input is the year of Flattrade NIFTY 50 and India
VIX one-minute OHLC, joined only on exact broker timestamps. Index volume is
zero and is not used. The labels are (1) NIFTY direction over the next five
completed minutes, with a volatility-scaled flat band, and (2) the maximum
spot excursion during those five minutes in basis points. Features are built
from the current completed bar and the preceding 30 minutes only.

## Fixed chronological split

| Block | Dates | Sessions | Purpose |
| --- | --- | ---: | --- |
| Training | 26 Sep 2025–30 Jun 2026 | 181 | Initial fit, 61,537 minute labels |
| Validation | 1–31 Jul 2026 | 22 | Check the fixed model; no test-based tuning |
| Held-out test | 3 Aug–25 Sep 2026 | 39 | Final evaluation, 13,260 labels |

After validation, the evaluation checkpoint was refitted through **31 July**
and frozen before scoring August–September. The three option-chain sessions,
25–27 August, were never part of either fit. Overlapping five-minute labels
stay within one session, so they cannot cross a split boundary. A missing
minute invalidates its surrounding feature/label window. Training gives each
session near-equal weight; the last 60 days receive up to twice the weight
of an old day, with no single day allowed to dominate.

The directional model is a robust-scaled, regularized multinomial logistic
regression. The range model is regularized regression of log one-plus future
spot excursion. Both use 15 completed-bar features: 1- and 5-minute returns,
EMA(8/21) gap, EMA slope, KAMA slope, RSI(14), ATR(14), DMI strength,
10-minute realized volatility, 20-minute path efficiency and channel
position, VIX level/change/EMA gap, and session time.

## Observed results

| Metric | Model | Baseline | Interpretation |
| --- | ---: | ---: | --- |
| July direction accuracy | 35.31% | 35.13% majority | No useful improvement |
| Aug–Sep direction accuracy | 33.16% | 30.83% majority | Below a practical signal threshold |
| Aug–Sep high-confidence calls | 1 / 13,260 | — | Effectively no trade opportunities |
| July range MAE | 2.1584 bps | 2.1881 bps ATR | Small gain |
| Aug–Sep range MAE | 2.0218 bps | 1.9843 bps ATR | Worse than ATR |

The directional model is **not promoted** to a strategy, and the range model
is **not promoted** to stop control. The classifier almost never predicts
FLAT and is poorly calibrated for actionable direction. Range prediction
beats a constant median but not the stronger ATR baseline on the untouched
test block. These findings are kept rather than retuning on the test set.

## How the three option-chain days are tested

1. Freeze the model trained through 31 July. Calculate each August minute's
   spot/VIX features using only completed bars. A hypothetical order must use
   a **later** option quote; the matching timestamp in the audit is for data
   availability, not a claimed executable fill.
2. On 25, 26, and 27 August, align those predictions to historical chain
   minutes with ATM CE/PE and both 1,000-point hedge strikes present with
   non-crossed positive bid/ask quotes. The aligned forecast counts are
   315, 311, and 322. Direction accuracies are 32.06%, 31.51%, and 30.43%.
   There were **zero high-confidence directional calls** on all three days.
3. To test a *trading policy*, replay the same three days in order without
   refitting within or between them. Enter/exit on the next available quote;
   sell at bid, buy at ask, include hedges, stop and trail logic, costs,
   expiry, margin, and missing-quote rejection. Compare with an unchanged
   hedged-straddle baseline. Three days are too few to establish expected
   returns or drawdown; repeat the frozen policy on many later paper sessions.

These CSVs have varying IV/OI on the three days, but their provenance and
historical executability are unverified. The spot model's forecast accuracy
does **not** measure option premium decay or option-strategy P&L. No trade
replay P&L is claimed from these forecasts.

Run the research again from the repository root:

```sh
OPENBLAS_NUM_THREADS=1 python3 -m strategy_lab.spot_ml \
  --data-dir data/market_history/nifty_1y \
  --option-dir '/path/to/data/option_chain'
```

Outputs are `spot_ml_model.json`, `spot_range_model.json`, and
`spot_ml_report.json` in the historical data directory. They are research
artifacts, not broker configuration.
