# NIFTY v4: closed-candle structure candidate

This is a paper-only hypothesis, not a prediction guarantee or a calibrated
model. Research on automated chart patterns finds some information in past price
shapes, but it does not establish profitable NIFTY option execution after
spreads and costs. Support/resistance evidence is likewise market-dependent.
Testing many named candles on one sample creates selection bias. The first
release therefore freezes a small set of rule families and a fixed heuristic
score instead of fitting thresholds to past P&L.

Primary sources:

- [Lo, Mamaysky and Wang (2000), *Foundations of Technical Analysis*](https://business.columbia.edu/sites/default/files-efs/pubfiles/19268/Lo-Mamaysky_wang_foundations.pdf)
- [Osler (2000), *Support for Resistance*](https://www.newyorkfed.org/medialibrary/media/research/epr/00v06n2/0007osle.pdf)
- [SEBI (2024), individual equity F&O outcome study](https://www.sebi.gov.in/media-and-notifications/press-releases/sep-2024/updated-sebi-study-reveals-93-of-individual-traders-incurred-losses-in-equity-fando-between-fy22-and-fy24-aggregate-losses-exceed-1-8-lakh-crores-over-three-years_86906.html)

## Information boundary

The signal uses only completed one-minute NIFTY candles from the current
session. Candles must start at 09:15, remain contiguous, and contain the
complete first fifteen-minute range. The earliest entry is 09:31 IST. No
current/incomplete candle, tomorrow's price, stale bar, or reconstructed option
quote is used. One-minute ATR(14), EMA(8/21), a ten-change path-efficiency
ratio, and completed five-minute close bias supply context. NIFTY index volume
does not masquerade as traded option volume.

The patterns are separate proposals, not equal votes:

| Family | Trigger on closed candle | Invalidation / target idea |
| --- | --- | --- |
| Opening-range break | Close beyond 09:15–09:30 high/low with a large body | Back inside range / measured move |
| Recent-range break | Close beyond the preceding twenty one-minute highs/lows | Back inside broken level / recent range or ATR projection |
| Compression break | Break after six bars whose full range is at most 2.2 ATR | Back into compression / compression-width or ATR projection |
| Breakout retest | Break in one of prior four bars, then touch and close beyond the old level | Below/above level / opening-width or ATR projection |
| Failed break | Previous close outside opening level, latest close back inside | Failed extreme / opposite opening-range boundary |
| Level rejection / engulfing | Wick rejection or body engulfing at prior support/resistance | Candle extreme / opposite structural level |

The implementation rejects any proposal without at least 1.5 units of
underlying target distance per unit of underlying stop distance. Stop distance
must be between 0.25 and 2.5 ATR. Each valid match receives a fixed base score
plus trend, completed-five-minute bias, candle-shape, path-efficiency, and
target-room adjustments. The top score must be at least 0.70. If an opposing
match is within 0.08, the strategy remains flat. The number is a ranking score,
not a measured chance of success. A volatility shock blocks fresh exposure.
Targets are capped at the nearest already-completed five-minute structural
barrier; that cap can cause a setup to fail the 1.5:1 room check.

## Paper position

For a bullish pattern v4 sells a near-ATM put and buys a put 100–200 points
lower. A bearish pattern mirrors this with calls. It uses the broker's fresh
bid/ask/depth and actual lot size, one tick of adverse slippage, and modeled
fees. It rejects spreads with credit at or below ₹500 per multiplier or a
calculated maximum expiry loss above ₹8,000 per multiplier. This narrower wing
is deliberate: a 1,000-point wing on one NIFTY lot can exceed the risk budget
of a ₹2 lakh account. Only one spread is open at a time.

Open exposure exits on the first observed completed candle that touches its
underlying invalidation or target, or earlier if the option portfolio stop,
profit target, account lock, or 15:34 deadline fires. If stop and target appear
in the same one-minute candle, stop takes precedence. All paper exits require
fresh executable option quotes; a stop trigger is not a guaranteed fill. The
underlying target is a price level, not a promise that the option spread will
show a profit after volatility, spread, and fees. The
strategy has a one-minute cooldown, a twelve-entry daily cap, and account-wide
capital/risk locks. No broker order endpoint is reachable from this controller.

## Validation gate

The server held zero recorded `market_snapshots` when this version was built.
Synthetic unit fixtures show that the code follows its rules; they cannot show
expectancy. Capture complete NIFTY option books and one-minute bars over many
trading days, then replay the server's `data/strategy_lab/ledger.sqlite3` with
`--strategy nfv4`. Fix parameters before a
chronological held-out test, include actual charges/spread/slippage, inspect
missing-data coverage and unresolved exits, and compare with a no-trade
baseline. Do not interpret an incomplete observed month as a monthly return.
