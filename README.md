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
pytest                             # unit tests
```

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

## Layout

| Path | What |
| --- | --- |
| `kalshi_perps/` | Config, request signing (RSA-PSS, also Ed25519), REST client for `/margin/*`, position accounting, paper broker |
| `risk/` | Risk engine: notional cap, daily loss limit, kill switch |
| `scripts/` | `market_check.py` (backfill script coming) |
| `dashboard/` | Local dashboard server + static page |
| `tests/` | pytest suite |

## Kalshi API notes

- Prices are decimal-dollar strings **per contract**. BTC price ≈ contract price × 10,000.
- The order book endpoint does not reliably return levels best-first; the client sorts them.
- Candles for minutes with an empty book carry sentinel values (ask high = int64 max, bid low = 0); ignore them when computing spreads.
- Positive funding rate: longs pay shorts. Clamped to ±2% per 8h interval.
- `/margin/balance` can return 403 on demo; the client reports it as unavailable instead of raising.
