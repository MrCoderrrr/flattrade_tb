# Trading Pipeline Report

## Execution path

1. `TradingRuntime.heartbeat()` obtains authenticated underlying quotes from
   `FlattradeMarketData`.
2. `IndicatorRegistry` maintains independent streaming indicators for NIFTY
   and MCX Natural Gas. Warm-up, volatility-ratio persistence, EMA direction,
   and data freshness gates run before any entry.
3. Strategies resolve each option through the broker contract search. The
   resolver selects only an exact strike and the nearest non-expired contract.
   In paper mode, the deterministic quote simulator is used only when the
   broker option quote is unavailable.
4. `PaperExecution` applies bid/ask-aware fills to typed positions. Every fill
   is written to the CSV audit trail by `TradeLogger`.
5. NIFTY opens the configured hedged short strangle. MCX opens the two-leg
   short strangle and manages the surviving leg.
6. Management runs on every heartbeat:
   - NIFTY initial stop: option ask at 135% of entry.
   - NIFTY trailing stop: after a 5% favorable move, the lowest option ask
     plus 5% (DTE <= 1) or 7% (later expiry).
   - MCX initial stop: option ask at the configured 15% adverse threshold.
   - MCX trailing stop: the best favorable option ask plus `k * ATR`.
   - expiry/session flattening and the combined account loss guardian close
     positions only with fresh quotes.
7. State is atomically persisted so indicator warm-up and open paper positions
   survive a process restart on the same IST trading date.

## Dashboard

The terminal dashboard is a read-only projection of the runtime state. It
shows feed age, indicator values, option-data status, active legs, bid/ask-aware
mark-to-market P&L, initial stop, trailing stop, realized P&L, and the next
action. It does not invent prices when the feed is unavailable.

## Production readiness boundary

- Broker authentication is opt-in through the configured Flattrade token.
- The runtime deliberately injects `PaperExecution`; it cannot place live
  orders accidentally.
- A live deployment still requires an explicitly approved
  `FlattradeExecutionAdapter` implementation with broker-side order/modify
  acknowledgement, reconciliation, idempotency, and a kill switch. The
  current repository does not provide those guarantees, so claiming live
  production execution would be unsafe.
- No quote, option contract, fill, or stop trigger is silently fabricated in
  authenticated mode. Missing or stale data pauses entries and prevents
  forced exits until a fresh quote is available.

## Verification

`python3 -m compileall -q .` passes. The repository does not currently declare
or include a runnable test dependency in this environment, so the existing
pytest suite could not be executed here.
