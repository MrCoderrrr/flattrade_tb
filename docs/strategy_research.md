# Strategy candidates and validation

These are research candidates, not profitable strategies established by historical evidence. A monthly 4–5% target compounds to approximately 60–80% annually before withdrawals. It must not be treated as a promised or routine outcome.

## Rules

**NIFTY:** sell an out-of-the-money iron condor only while price remains inside the opening range, near its session mean, with low directional efficiency and limited EMA separation/slope. Buy same-expiry wings first. The full 09:15–09:45 range and 12 completed five-minute bars are required; the earliest signal is 10:15. Stop new entries at 14:00; flatten at 15:30 IST.

**MCX Natural Gas:** require a directional trend, EMA confirmation and two completed breakout bars. Sell a hedged put spread in an uptrend or a hedged call spread in a downtrend. Earliest signal is 17:00 after 12 bars from 16:00. Stop entries at 22:30; flatten at 23:15 IST. The implementation excludes Thursdays as a coarse inventory-release precaution. This does not cover holiday-shifted releases: a verified event calendar remains necessary before live commissioning.

The naked ATM straddle requested initially is deliberately not the low-drawdown candidate: a strong trend can increase the losing short option's liability faster than the other option decays. Standard Natural Gas lots may be too large for this budget; the strategy can return no trade. Mini contracts are supported by the pure strategy rules, but the default feed currently selects standard NATURALGAS only. Mini deployment requires explicit matching of the correct underlying future and liquid option book.

Both strategies use one complete exchange lot per leg per multiplier, dynamically from contract metadata. ₹200,000 per multiplier is a capital allocation rule, **not** a claim about actual broker margin. The same account capital is reused sequentially; open positions in the other session block entry.

Candidate payoff risk is capped at ₹2,000 per multiplier, including an assumed cost reserve. The paper engine recomputes the risk using simulated executable fills. Portfolio stop triggers are at most ₹1,000 per multiplier; the daily session loss trigger is ₹1,000. Shared daily loss and persistent drawdown triggers are 1% and 5% of declared account capital. These are triggers, not guaranteed loss caps: jumps, unavailable depth, devolution and inability to exit can exceed them. No same-day or next-weekday-expiry entries; all protective legs must share the expiry and lot size. At most two entries per session with a 30-minute cooldown.

## Execution and data assumptions

Paper fills cross bid/ask with an additional adverse tick and require sufficient displayed quantity. The provisional fee model is ₹20 plus 0.2% of premium turnover per leg fill; it is a research allowance, not a verified brokerage/tax tariff. Synthetic baskets do not simulate real queue priority, partial fills, exchange rejections or legging latency. Live selection is therefore locked in this implementation.

Quotes must include an exchange timestamp; receipt time and LTP are never substituted for executable depth. A missing/stale quote leaves an exit pending, retains the position and labels valuation stale. Quotes, bars, state and simulated trades are recorded locally. Process restart expires authorization and attempts recovery of persisted paper exposure rather than opening a fresh session.

Exchange schedules are not inferred from old comments. The current [NSE market timings](https://www.nseindia.com/static/market-data/market-timings) page lists equity derivatives 09:15–15:40; 15:30 provides a ten-minute buffer. MCX's exact contract schedule and special sessions must be confirmed for each deployed instrument; 23:15 is the conservative candidate cutoff, not a statement of exchange close. [EIA's release schedule](https://ir.eia.gov/ngs/schedule.html) specifies the usual Thursday 10:30 Eastern release and exceptions. [Flattrade's official API documentation](https://github.com/flattrade/pythonAPI) describes quotes, intraday bars, chain metadata and order status. These sources support operational mechanics, not the profitability of the candidate thresholds.

## Offline replay

Run `python3 -m strategy_lab.replay DATA.jsonl --capital 200000 --multiplier 1 --output data/research/report.json`.

Each JSONL record has `timestamp` (ISO with timezone), `market` (`NIFTY` or `MCX`), `bars`, and `quotes`. Each bar has `timestamp` (five-minute OPEN), `open`, `high`, `low`, `close`, `volume`. Each quote has `contract`, `timestamp`, `bid`, `ask`, `last`, `bid_size`, `ask_size`. Contract fields are `symbol`, `token`, `exchange`, `expiry` (YYYY-MM-DD), `strike`, `option_type`, `lot_size`, `tick_size`. See the typed objects in `strategy_lab/models.py`. Runtime recordings already use this schema.

Merge snapshots chronologically, preserve original timestamps and retain both markets. The replay runs the same controller and risk rules using an isolated temporary ledger, never broker APIs. It rejects malformed or duplicate records. Missing prices are not interpolated and final open positions are not liquidated at an invented price. Incomplete sessions and gaps are disclosed; calendar-month returns remain unset without verified calendar coverage. Returns on allocation use ₹200,000 × multiplier once, not twice for two sessions; if declared account capital exceeds allocation, the full-account return is lower.

The report includes costs, entries, unresolved exposure, observed equity drawdown, daily/monthly observed-sample summaries and a chronological 70/30 split by complete dates. Parameters are fixed, not fitted on either partition. The split does not itself prove unseen data or sufficient sample size. With fewer than two dates, no held-out test is claimed.

Before any live implementation, obtain timestamped historical option depth across expiry cycles and both trend/chop regimes; verify broker charges, instrument metadata, holiday/event calendars and margin; evaluate untouched data and adverse cost assumptions; then collect a substantial forward-paper record. Commission real order acknowledgement, partial-fill reconciliation, hedge-first entry, short-first exit and recovery separately. Unit tests prove software behavior, not expected returns. The server's legacy closed-trade logs cannot substitute for this validation.
