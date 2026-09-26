# NIFTY / MCX strategy lab

Research and paper trading dashboard. **No live order submission is enabled.** None of the versions has demonstrated or can guarantee the requested 4–5% monthly return.

The dashboard lists all six version names. `nfv1` and `mcxv1` are aliases for the original engines and appear as archived because their execution and risk state cannot safely be managed by this controller. `nfv2` and `mcxv2` remain the selective, five-minute paper candidates. `nfv3` uses one-minute EMA 9/21, RSI 14, ATR 14 and the NIFTY session price mean, beginning at 09:20 and flattening at 15:34 IST. It sells an at-the-money put in a bullish regime, call in a bearish regime, or both when balanced, with farther-strike protective options. `mcxv3` uses the same indicator votes and a volume-weighted session price, beginning at 16:05 and flattening at 23:24 IST. Its short options have **no hedge**; a stop trigger cannot cap the realized loss. V3 permits more entries with a 60-second cooldown, but data, depth, stops and session limits can leave it flat. Prior-session one-minute bars are needed to warm up indicators for the earliest entry.

## Start the dashboard

From this repository, run:

```sh
python3 control_dashboard.py --port 8080
```

Open `http://127.0.0.1:8080` and enter the **dashboard control token** printed in that terminal. This token only unlocks local controls; it is not your Flattrade token. The server starts idle. Each session's Start dialog asks for mode, multiplier and declared shared account capital. One multiplier requires ₹200,000. Stop requests flattening; emergency stop locks new entries for the rest of the day. A restart requires new session authorization. Live mode is visibly locked pending strategy and execution validation.

The dashboard controls the v2/v3 paper controller. The original v1 launchers have separate execution and state, so their dashboard rows are informational.

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

See `docs/strategy_research.md` for research assumptions. Saved broker snapshots remain necessary for an out-of-sample evaluation of v3.

## TradeDesk website

The responsive TradeDesk interface adds pause/resume of new entries (positions remain managed), per-market filtering and instrument search, fills CSV export, recorded daily P&L, signal details, execution health and manual refresh. Visible tabs poll every two seconds without overlapping requests; hidden tabs stop polling until visible. Unchanged tables retain their DOM. No frontend framework, external fonts or chart library is downloaded.

On the requested server, `strategy-control.service` serves port 8080 directly and restarts on failure. The private dashboard token is stored outside Git. Stop and restart the service with systemd; running a second `nohup` copy would conflict with its state lock and port. The browser uses plain HTTP on this IP, so its control token is not transport-encrypted.
