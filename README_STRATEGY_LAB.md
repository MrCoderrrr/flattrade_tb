# NIFTY / MCX strategy lab

Local replacement for research and paper trading. **No live order submission is enabled.** The two candidates have not demonstrated the requested 4–5% monthly return.

## Start the dashboard

From this repository, run:

```sh
python3 control_dashboard.py --port 8080
```

Open `http://127.0.0.1:8080` and enter the **dashboard control token** printed in that terminal. This token only unlocks local controls; it is not your Flattrade token. The server starts idle. Each session's Start dialog asks for mode, multiplier and declared shared account capital. One multiplier requires ₹200,000. Stop requests flattening; emergency stop locks new entries for the rest of the day. A restart requires new session authorization. Live mode is visibly locked pending strategy and execution validation.

The dashboard controls only the new local controller. It does not control or stop the old server bots. No server deployment was performed. Do not run the old launchers expecting the new risk controls.

## Connect market data for paper trading

Use your existing Flattrade login flow to obtain a current token. Keep it in an owner-readable token file and set these environment variables before launching:

```sh
export FLATTRADE_USER_ID='your-user-id'
export FLATTRADE_TOKEN_FILE='/absolute/path/to/token.txt'
python3 control_dashboard.py
```

Provide the current dated `NFO_symbols_YYYY-MM-DD.csv` and `MCX_symbols_YYYY-MM-DD.csv` in the repository using the existing CSV schema (`Exchange,Token,LotSize,Symbol,TradingSymbol,Expiry,Instrument,OptionType,StrikePrice,TickSize`). Old token maps are rejected; quotes also verify broker symbol and lot size. Do not merely rename an old file as today's master. The local snapshot's older masters and an expired token are insufficient for a fresh session. The adapter uses only read-only Flattrade endpoints; it has no order-placement endpoint.

The new interface does not collect your broker password or automatically renew broker login. Without current credentials, valid masters and a fresh liquid book it reports the reason and makes no trades. No external package installation is needed for the new controller; Python 3.10+ with timezone data is sufficient.

## Records and tests

SQLite state, journal and simulated fills: `data/strategy_lab/ledger.sqlite3`. Bid/ask recordings: `data/strategy_lab/quotes_YYYY-MM-DD.jsonl`. They remain under Git-ignored `data/`. Stop and flatten before closing the dashboard. If an exit cannot be priced, the position remains recorded for recovery; closing the program does not erase it.

```sh
python3 -m unittest discover -s tests -v
python3 -m strategy_lab.replay data/strategy_lab/quotes_YYYY-MM-DD.jsonl --output data/research/report.json
```

The HTTP tests need permission to bind a loopback socket. All tests use synthetic data; none places broker orders. A replay report from synthetic fixtures is not a trading-performance result.

See `docs/strategy_research.md` for exact assumptions, `docs/legacy_strategy_audit.md` for verified defects and server-result discrepancies, and `docs/server_sync.md` for the preserved server copy. Existing laptop edits were preserved; nothing was pushed to GitHub.

## TradeDesk website

The responsive TradeDesk interface adds pause/resume of new entries (positions remain managed), per-market filtering and instrument search, fills CSV export, recorded daily P&L, signal details, execution health and manual refresh. Visible tabs poll every two seconds without overlapping requests; hidden tabs stop polling until visible. Unchanged tables retain their DOM. No frontend framework, external fonts or chart library is downloaded.

For a server deployment, `--token-file` persists a private dashboard password and `--public-origin-file` trusts the exact HTTPS Cloudflare tunnel origin written by `deploy/tunnel.py`. The backend remains loopback-only. The tunnel rewrites Host to localhost; only that local Host and the current explicit HTTPS Origin are accepted. Cloudflare quick-tunnel URLs can change on restart and carry no uptime guarantee; a permanent domain/named tunnel can replace this when available. The dashboard password is never embedded in the webpage or URL.
