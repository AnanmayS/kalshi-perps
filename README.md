# kalshi-perps

Kalshi BTC perpetuals trading bot. **Demo-first, paper trading by default.**

- Demo ticker `KXBTCPERP1`, production ticker `KXBTCPERP` (1 contract = 0.0001 BTC; funding 3x daily; CF Benchmarks BRTI index)
- Real orders are sent **only** when `KALSHI_ENV=prod` **and** `KALSHI_LIVE_TRADING` is exactly `"true"`. Anything else is paper-only.

## Setup

```bash
git clone https://github.com/AnanmayS/kalshi-perps.git
cd kalshi-perps
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

### Generate demo API keys

1. Sign in at <https://demo.kalshi.co> (a separate account from kalshi.com).
2. **Account & security → API Keys → Create Key**, choose **RSA**.
3. Save the downloaded PEM to `~/.kalshi/demo-key.pem` (`chmod 600` it) and put the key ID in `.env` as `KALSHI_API_KEY_ID`.

`.env` and `*.pem` are git-ignored. Confirm with `git check-ignore .env`.

## Run

```bash
python scripts/market_check.py     # connectivity + auth sanity check (never places orders)
python dashboard/server.py         # http://localhost:8765
python scripts/backfill_candles.py # 7 days of 1m candles + funding history -> data/market.sqlite
python scripts/collect_ws.py       # live trades/quotes/book via authenticated WebSocket (Ctrl-C to stop)
python -m backtest.run             # backtest the momentum strategy on stored candles
python scripts/paper_run.py --strategy carry   # run a strategy live on paper (Ctrl-C to stop)
pytest                             # unit tests
```

### Market data (`data/market.sqlite`, git-ignored)

- `backfill_candles.py [--days N] [--interval 1|60|1440] [--resume]`: public REST candles, chunked under Kalshi's 5,000-candle cap, upserted so re-runs are safe. Each 1m candle keeps trade OHLC **and** bid/ask OHLC (observed spreads for the backtester); empty-book sentinels are stored as NULL. Also stores funding history.
- `collect_ws.py [--duration S]`: signs the WebSocket handshake with your key, subscribes to `orderbook_delta`, `ticker` and `trade`, and maintains a local book. Records every trade, every top-of-book change, ticker updates (mark, index, funding) and a top-25 book snapshot every 5s. If a sequence number is skipped the book is marked invalid and a fresh snapshot is requested; it reconnects with backoff on drops.

The dashboard shows live quotes, a 24h candlestick chart, the order book, the funding countdown, your demo account's fees, and a paper-trading panel. It never sends orders to Kalshi.

### Paper trading

- Orders are immediate-or-cancel and fill against the **live order book**, level by level. Size the book can't fill is cancelled, never assumed filled.
- Every paper fill crosses the spread, so it pays the **taker** fee (your rate from `/margin/fee_tiers`, else a 0.12% default) and is labelled TAKER in the UI.
- Funding is settled on open paper positions at each funding time (positive rate: longs pay shorts).
- Paper state is saved in `data/paper_state.json` (git-ignored). Starting balance: $10,000; max 5x leverage.

### Risk engine (`risk/`)

- `RISK_MAX_NOTIONAL_PER_TRADE` (default $500) caps each order's notional.
- `RISK_DAILY_LOSS_LIMIT` (default $100): when today's P&L (UTC day, marked to market, including fees and funding) reaches −limit, the **kill switch** trips.
- While tripped, only orders that reduce the position are accepted, so you can always close. It clears at the next UTC day or via "Reset kill switch" (which re-trips if the loss is still over the limit). You can also trip it manually.

## Backtesting

```bash
python -m backtest.run                                   # momentum, defaults
python -m backtest.run --lookback 240 --threshold-bps 80 --min-hold 120
python -m backtest.run --strategy hold                   # buy-and-hold baseline
python -m backtest.run --days 3 --slippage-bps 2 --out data/bt   # + trades.csv / equity.csv
```

How fills work (no fantasy fills):

- The strategy sees a 1-minute bar only after it closes and returns a target position.
- The order executes at the **next** bar's open, at the quote actually observed: buys pay `ask_open`, sells get `bid_open`. Never the mid or the signal bar's close.
- If that side of the book was empty, the order doesn't fill (reported as rejected).
- Every fill pays the **taker** fee (your fee tier if keys are set), using the same fee and position code as paper trading.
- Funding is settled on whatever position is held at each funding time; the same risk engine (notional cap, daily loss limit, kill switch) applies.
- Candles don't include depth, so size isn't checked against the book. Keep sizes small, or add `--slippage-bps`.

The report breaks net P&L into price P&L (with the spread you crossed shown separately), taker fees and funding, and compares against buy-and-hold.

### Starter strategy: `strategies/momentum.py`

Time-series momentum on the mid: go long (short) `size` contracts when the `lookback`-minute return is above (below) `threshold_bps`; hold at least `min_hold` minutes; go flat when the signal fades inside `exit_bps`. A round trip costs about 2 × 0.12% taker plus the spread, so thresholds well below ~30 bps lose by construction.

On 7 days of demo data (Sep 16–23, 2026) it **loses money** at every setting tried (−$30 to −$178 on 10 contracts), because demo BTC mean-reverted on these horizons. It's a template for the plumbing, not a strategy to trade.

## Paper trading runner

```bash
python scripts/paper_run.py                                        # momentum, default params
python scripts/paper_run.py --lookback 240 --threshold-bps 80 --min-hold 120 --size 5
python scripts/paper_run.py --duration 3600 --flatten-on-exit      # 1 hour, close position at the end
python scripts/paper_run.py --reset                                # fresh paper account
caffeinate -i python scripts/paper_run.py                          # keep the Mac awake while it runs
```

- Each minute, a few seconds after Kalshi publishes the closed 1m candle, the runner builds the same bar the backtester uses and asks the strategy for a target position.
- Target changes become paper **taker** orders against the live order book, through the same paper broker and risk engine as the dashboard. **Nothing is ever sent to Kalshi**, regardless of `KALSHI_LIVE_TRADING`.
- On start it warms the strategy up on recent candles without trading. If it falls behind (Mac asleep, network down), missed bars update the strategy but never trade; only a bar that just closed can.
- Kill switch: when the daily loss limit trips, the position is flattened and only reducing orders are allowed (the backtester does the same).
- State lives in `data/runner/` (its own paper account, separate from the dashboard's manual ticket), so restarts keep the position and P&L. Funding due while stopped is settled on restart.
- The dashboard's **Strategy runner** card shows its status, signal, position, P&L and log while it runs.

It only runs while the computer is on and awake. For 24/7 paper trading, run it on a small always-on server.

### Strategy research (Aug 14 – Sep 23, 2026 demo data)

Tuned on Aug 14 – Sep 9 only, then scored once on the held-out Sep 9 – 23 (`backtest/sweep.py`). All runs: 10 contracts, 0.12% taker, fills at the next minute's observed bid/ask, funding included.

| Strategy | Tuning period | Held-out period | Notes |
| --- | --- | --- | --- |
| **Funding carry** (`--strategy carry`, avg of last 3 rates, ±20 bps) | +$8.57 (14/15 variants positive) | **+$16.49** (9/9 neighbours positive) | Short while funding is strongly positive; earns the funding |
| Mean reversion (z-score) | best −$0.48, 0/228 variants positive | — | Edge before costs ≈ taker fees; not viable taker-only |
| Momentum (slow) | −$199.86 | −$111.82 | Loses every week |
| Buy & hold | +$8.44 | −$16.78 | |

Funding carry over all 40 days: **+$25.18** (funding +$29.89, price −$4.24, 5 fills, max drawdown −$12.97).

Caveats: demo funding is extreme (~0.66% per 8h, ~700%/yr); production funding is usually far smaller, so this edge may not exist there. Carry holds a short, so a strong BTC rally can outweigh the funding. 40 days is a short sample.

The runner also has a slippage guard (`--max-slippage-bps`, default 25): it never fills further than that from the mark and retries for up to 5 minutes instead. The demo book sometimes empties for a moment right after the minute.

## Layout

| Path | What |
| --- | --- |
| `kalshi_perps/` | Config, request signing (RSA-PSS, also Ed25519), REST client for `/margin/*`, position accounting, paper broker, SQLite store, WebSocket collector |
| `risk/` | Risk engine: notional cap, daily loss limit, kill switch |
| `backtest/` | Data loading, event-driven engine, CLI (`python -m backtest.run`) |
| `strategies/` | Strategy interface; momentum, mean reversion, funding carry, buy-and-hold baseline |
| `runner/` | Paper-trading runner (live data, paper fills) |
| `scripts/` | `market_check.py`, `backfill_candles.py`, `collect_ws.py` |
| `dashboard/` | Local dashboard server + static page |
| `tests/` | pytest suite |

## Kalshi API notes

- Prices are decimal-dollar strings **per contract**. BTC price ≈ contract price × 10,000.
- The order book endpoint does not reliably return levels best-first; the client sorts them.
- Candles for minutes with an empty book carry sentinel values (ask high = int64 max, bid low = 0); ignore them when computing spreads.
- Positive funding rate: longs pay shorts. Clamped to ±2% per 8h interval.
- `/margin/balance` can return 403 on demo; the client reports it as unavailable instead of raising.
- Candlestick requests are capped at 5,000 candles; Kalshi omits some minutes entirely (~6% on demo).
- GET requests retry on 429/5xx with backoff; order requests are never retried.
