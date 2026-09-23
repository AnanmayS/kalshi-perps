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

The dashboard shows live quotes, a 24h candlestick chart, the order book, the funding countdown and your demo account's fees and positions. It is read-only; the paper-trading panel is the next build step.

## Layout

| Path | What |
| --- | --- |
| `kalshi_perps/` | Config, request signing (RSA-PSS, also Ed25519), REST client for `/margin/*` |
| `scripts/` | `market_check.py` (backfill script coming) |
| `dashboard/` | Local dashboard server + static page |
| `tests/` | pytest suite |

## Kalshi API notes

- Prices are decimal-dollar strings **per contract**. BTC price ≈ contract price × 10,000.
- The order book endpoint does not reliably return levels best-first; the client sorts them.
- Candles for minutes with an empty book carry sentinel values (ask high = int64 max, bid low = 0); ignore them when computing spreads.
- Positive funding rate: longs pay shorts. Clamped to ±2% per 8h interval.
- `/margin/balance` can return 403 on demo; the client reports it as unavailable instead of raising.
