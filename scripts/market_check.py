"""Connectivity + auth sanity check.

    python scripts/market_check.py

Public checks always run. Signed checks run only if KALSHI_API_KEY_ID and
KALSHI_PRIVATE_KEY_PATH are set and the key file exists. Places no orders.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kalshi_perps import KalshiAPIError, KalshiPerpsClient, load_settings  # noqa: E402
from kalshi_perps.client import D, parse_ts  # noqa: E402

CONTRACT_BTC = 10_000  # 1 contract = 0.0001 BTC


def ok(msg):
    print(f"  [ OK ] {msg}")


def fail(msg):
    print(f"  [FAIL] {msg}")


def warn(msg):
    print(f"  [WARN] {msg}")


def main() -> int:
    s = load_settings()
    failures = 0
    print(f"env={s.env}  ticker={s.ticker}  base={s.rest_base_url}")
    print(f"live_trading_enabled={s.live_trading_enabled}  (paper only unless prod + KALSHI_LIVE_TRADING=\"true\")")
    client = KalshiPerpsClient(s)

    print("\nPublic endpoints")
    try:
        st = client.exchange_status()
        ok(f"exchange_status: exchange_active={st['exchange_active']} trading_active={st['trading_active']}")

        m = client.market()
        px = D(m.get("price"))
        ok(f"market {m['ticker']} status={m['status']} last=${px} (~BTC ${px * CONTRACT_BTC:,.2f}) "
           f"bid={m.get('bid')} ask={m.get('ask')} tick={m['tick_size']}")

        ob = client.orderbook(depth=5)
        ok(f"orderbook: best_bid={ob.best_bid} best_ask={ob.best_ask} spread={ob.spread} "
           f"levels={len(ob.bids)}/{len(ob.asks)}")
        if ob.best_bid is not None and ob.best_ask is not None and ob.best_bid >= ob.best_ask:
            fail("crossed book")
            failures += 1

        now = int(time.time())
        candles = client.candlesticks(now - 3600, now, period_interval=1)
        ok(f"candlesticks (1m, last hour): {len(candles)} candles")

        tr = client.trades(limit=3)["trades"]
        ok(f"trades: {len(tr)} recent, latest {tr[0]['price']} x {tr[0]['count']} taker={tr[0]['taker_side']}"
           if tr else "trades: none returned")

        fe = client.funding_estimate()
        nft = parse_ts(fe["next_funding_time"])
        mins = (nft - datetime.now(timezone.utc)).total_seconds() / 60
        ok(f"funding estimate: rate={fe.get('funding_rate')} next={fe['next_funding_time']} (in {mins:.0f} min)")
    except KalshiAPIError as e:
        fail(str(e))
        failures += 1
    except Exception as e:  # network errors etc.
        fail(f"{type(e).__name__}: {e}")
        failures += 1

    print("\nSigned endpoints")
    if not s.api_key_id or s.private_key_path is None:
        warn("KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY_PATH not set; skipping (see README to generate demo keys)")
    elif not s.private_key_path.is_file():
        warn(f"key file not found at {s.private_key_path}; skipping")
    else:
        try:
            ok(f"margin/enabled: {client.margin_enabled()}")
        except KalshiAPIError as e:
            fail(f"margin/enabled: {e}  (401 => check key id / key file / clock)")
            failures += 1
        bal = client.balance()
        if bal.available:
            ok(f"balance: settled_funds=${bal.data.get('settled_funds')}")
        else:
            warn(f"balance unavailable (HTTP {bal.status}); common on demo, continuing")
        try:
            pos = client.positions()
            ok(f"positions: {len(pos)} open")
            fees = client.fee_tiers()
            ok(f"fee_tiers for {s.ticker}: maker={fees['maker_fee_rates'].get(s.ticker)} "
               f"taker={fees['taker_fee_rates'].get(s.ticker)}")
        except KalshiAPIError as e:
            warn(str(e))

    print("\nRESULT:", "PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
