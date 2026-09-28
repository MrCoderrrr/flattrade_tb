# TradeDesk paper strategy controller

The dashboard at port 8000 controls five paper strategies: NIFTY v1, v3 and v5; MCX v1 and v3. Each uses a separate runtime ledger. The shared dashboard shows current positions, entry/current/best premiums, per-leg premium stops where the strategy defines one, variables, daily history, and paper P&L.

NIFTY v1 uses KAMA-led ATM option selling with protective wings. NIFTY v3 uses EMA, RSI, ATR and session anchor with long wings. NIFTY v5 uses one-second flow and adaptive premium stops with long wings. MCX v1 and v3 use unhedged ATM option selling; their stop triggers do not cap gap loss. No strategy is validated to produce a fixed return.

New NIFTY entries use the nearest listed nonexpired option expiry. Existing legs stay on their entry expiry until closed. The page requires a dashboard PIN and paper is the default. A live request cannot place real orders because this controller has no live execution adapter.

Server state is kept under `data/strategy_lab/`. No raw option-chain snapshots or ML data are recorded. Historical paper P&L and fills use the compact strategy and analytics ledgers.
