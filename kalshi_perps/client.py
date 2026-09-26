"""Signed REST client for Kalshi perps (the /margin/* API).

Units (from Kalshi's perps OpenAPI spec):
  * prices are decimal-dollar strings per contract (1 contract = 0.0001 BTC,
    so a price of "8.4760" means BTC ~ $84,760);
  * counts are fixed-point strings with 2 decimals;
  * position is signed: positive = long, negative = short.
Everything money-like is returned as Decimal.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

import requests

from .auth import KalshiSigner
from .config import Settings


class KalshiAPIError(Exception):
    def __init__(self, status: int, method: str, path: str, body: Any):
        self.status, self.method, self.path, self.body = status, method, path, body
        super().__init__(f"{method} {path} -> HTTP {status}: {body}")


class LiveTradingDisabled(RuntimeError):
    """Raised when something tries to send a real order while live trading is off."""


class AuthRequired(RuntimeError):
    pass


def D(x: Any) -> Decimal | None:
    return None if x is None or x == "" else Decimal(str(x))


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


MARK_MAX_DEVIATION = Decimal("0.05")
MAX_ANCHOR_SPREAD = Decimal("0.05")


def _sane_mid(bid, ask) -> Decimal | None:
    """Mid of a quote only if both sides are real and the spread is plausible.

    When one side of the demo book is empty Kalshi can report a sentinel (e.g. an ask of
    int64-max / 10^4), whose "mid" is ~4.6e14; that must never become a price."""
    bid, ask = D(bid), D(ask)
    if not bid or not ask or bid <= 0 or ask <= bid:
        return None
    if (ask - bid) / bid > MAX_ANCHOR_SPREAD:
        return None
    return (bid + ask) / 2


def sane_mark(market: dict, book_mid: Decimal | None = None) -> Decimal | None:
    """Best available mark price, rejecting values the exchange sometimes reports broken.

    Observed on Kalshi demo: settlement_mark_price "0.0000" while trading normally, and an
    empty ask side reported as a huge sentinel. Preference: settlement mark, liquidation mark,
    then a sane book mid, then the last trade. A mark is used only if it is positive and
    within 5% of the anchor (sane book mid, else last trade). None if nothing is trustworthy.
    """
    anchor = book_mid if book_mid and book_mid > 0 else None
    if anchor is None:
        anchor = _sane_mid(market.get("bid"), market.get("ask"))
    last = D(market.get("price"))
    if anchor is None and last and last > 0:
        anchor = last
    if anchor is None:
        return None
    for key in ("settlement_mark_price", "liquidation_mark_price"):
        v = D((market.get(key) or {}).get("price"))
        if v and v > 0 and abs(v / anchor - 1) <= MARK_MAX_DEVIATION:
            return v
    return anchor


@dataclass
class Orderbook:
    bids: list[tuple[Decimal, Decimal]]  # best (highest) first
    asks: list[tuple[Decimal, Decimal]]  # best (lowest) first

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return _sane_mid(self.best_bid, self.best_ask)

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


@dataclass
class BalanceResult:
    """Balance lookup outcome. `available` is False when the endpoint refused (e.g. 403 on demo)."""

    available: bool
    status: int
    data: dict | None = None
    reason: str | None = None


class KalshiPerpsClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None, timeout: float = 10.0):
        self.settings = settings
        self.base_url = settings.rest_base_url
        self.session = session or requests.Session()
        self.timeout = timeout
        self.retry_backoff = 0.5
        self.signer: KalshiSigner | None = None
        if settings.has_credentials:
            self.signer = KalshiSigner.from_file(settings.api_key_id, settings.private_key_path)

    # ---- transport -------------------------------------------------------

    def _request(self, method: str, path: str, *, params=None, json=None, auth=False) -> Any:
        url = self.base_url + path
        headers = {"Accept": "application/json"}
        if auth:
            if self.signer is None:
                raise AuthRequired(f"{path} needs KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY_PATH")
            headers.update(self.signer.headers(method, url))
        params = {k: v for k, v in (params or {}).items() if v is not None}
        # GETs are safe to retry on rate limiting / transient server errors; writes never are.
        attempts = 4 if method == "GET" else 1
        for attempt in range(attempts):
            if auth and attempt:
                headers.update(self.signer.headers(method, url))  # fresh timestamp
            resp = self.session.request(method, url, params=params, json=json, headers=headers, timeout=self.timeout)
            if resp.status_code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                break
            time.sleep(self.retry_backoff * 2 ** attempt)
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = resp.text[:500]
            raise KalshiAPIError(resp.status_code, method, path, body)
        if resp.status_code == 204 or not resp.content:
            return None
        return resp.json()

    def _get(self, path, params=None, auth=False):
        return self._request("GET", path, params=params, auth=auth)

    # ---- public market data ----------------------------------------------

    def exchange_status(self) -> dict:
        return self._get("/margin/exchange/status")

    def markets(self, status: str | None = None) -> list[dict]:
        return self._get("/margin/markets", {"status": status})["markets"]

    def market(self, ticker: str | None = None) -> dict:
        return self._get(f"/margin/markets/{ticker or self.settings.ticker}")["market"]

    def orderbook(self, ticker: str | None = None, depth: int | None = None) -> Orderbook:
        ob = self._get(f"/margin/markets/{ticker or self.settings.ticker}/orderbook", {"depth": depth})["orderbook"]
        # The live API does not reliably return levels best-first, so always sort.
        bids = sorted(((D(p), D(q)) for p, q in ob.get("bids") or []), key=lambda l: l[0], reverse=True)
        asks = sorted(((D(p), D(q)) for p, q in ob.get("asks") or []), key=lambda l: l[0])
        return Orderbook(bids=bids, asks=asks)

    def candlesticks(self, start_ts: int, end_ts: int, period_interval: int = 1,
                     ticker: str | None = None, include_latest_before_start: bool | None = None) -> list[dict]:
        if period_interval not in (1, 60, 1440):
            raise ValueError("period_interval must be 1, 60 or 1440 minutes")
        return self._get(
            f"/margin/markets/{ticker or self.settings.ticker}/candlesticks",
            {"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval,
             "include_latest_before_start": include_latest_before_start},
        )["candlesticks"]

    def trades(self, ticker: str | None = None, limit: int | None = None, cursor: str | None = None,
               min_ts: int | None = None, max_ts: int | None = None) -> dict:
        return self._get("/margin/trades", {"ticker": ticker or self.settings.ticker, "limit": limit,
                                            "cursor": cursor, "min_ts": min_ts, "max_ts": max_ts})

    def funding_estimate(self, ticker: str | None = None) -> dict:
        return self._get("/margin/funding_rates/estimate", {"ticker": ticker or self.settings.ticker})

    def funding_rates_history(self, ticker: str | None = None, start_ts: int | None = None,
                              end_ts: int | None = None) -> list[dict]:
        return self._get("/margin/funding_rates/historical",
                         {"ticker": ticker or self.settings.ticker, "start_ts": start_ts, "end_ts": end_ts})["funding_rates"]

    # ---- private (signed) -------------------------------------------------

    def margin_enabled(self) -> bool:
        return bool(self._get("/margin/enabled", auth=True)["enabled"])

    def balance(self, compute_available_balance: bool = True) -> BalanceResult:
        """Never raises on 401/403: demo accounts frequently get 403 here."""
        try:
            data = self._get("/margin/balance", {"compute_available_balance": str(compute_available_balance).lower()}, auth=True)
            return BalanceResult(available=True, status=200, data=data)
        except KalshiAPIError as e:
            if e.status in (401, 403):
                return BalanceResult(available=False, status=e.status, reason=str(e.body))
            raise
        except AuthRequired as e:
            return BalanceResult(available=False, status=0, reason=str(e))

    def positions(self, ticker: str | None = None, subaccount: int | None = None) -> list[dict]:
        return self._get("/margin/positions", {"ticker": ticker, "subaccount": subaccount}, auth=True)["positions"]

    def orders(self, **filters) -> dict:
        return self._get("/margin/orders", filters, auth=True)

    def fills(self, **filters) -> dict:
        return self._get("/margin/fills", filters, auth=True)

    def fee_tiers(self) -> dict:
        return self._get("/margin/fee_tiers", auth=True)

    def funding_history(self, **filters) -> dict:
        return self._get("/margin/funding_history", filters, auth=True)

    def risk(self) -> dict:
        return self._get("/margin/risk", auth=True)

    # ---- order entry (hard-gated) -----------------------------------------

    def _require_live(self) -> None:
        if not self.settings.live_trading_enabled:
            raise LiveTradingDisabled(
                "Live trading is OFF (requires KALSHI_ENV=prod and KALSHI_LIVE_TRADING=\"true\"). "
                "Use the paper trading engine instead."
            )

    def create_order(self, side: str, count: str | Decimal, price: str | Decimal, *,
                     time_in_force: str = "good_till_canceled", ticker: str | None = None,
                     post_only: bool = False, reduce_only: bool = False,
                     client_order_id: str | None = None, subaccount: int | None = None) -> dict:
        self._require_live()
        if side not in ("bid", "ask"):
            raise ValueError("side must be 'bid' or 'ask'")
        body = {
            "ticker": ticker or self.settings.ticker,
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "side": side,
            "count": str(count),
            "price": str(price),
            "time_in_force": time_in_force,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": post_only,
            "reduce_only": reduce_only,
            "subaccount": subaccount,
        }
        return self._request("POST", "/margin/orders", json={k: v for k, v in body.items() if v is not None}, auth=True)

    def cancel_order(self, order_id: str) -> dict:
        self._require_live()
        return self._request("DELETE", f"/margin/orders/{order_id}", auth=True)

    def cancel_all_orders(self) -> None:
        self._require_live()
        self._request("DELETE", "/margin/orders", auth=True)
