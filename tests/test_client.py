import json
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_perps.client import KalshiAPIError, KalshiPerpsClient, LiveTradingDisabled
from kalshi_perps.config import load_settings


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = b"" if body is None else json.dumps(body).encode()
        self.text = self.content.decode()

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, routes):
        self.routes = routes  # {(method, path_suffix): (status, body)}
        self.calls = []

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": headers})
        for (m, suffix), (status, body) in self.routes.items():
            if m == method and url.endswith(suffix):
                return FakeResp(status, body)
        return FakeResp(404, {"error": "not found"})


@pytest.fixture
def keyfile(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "demo-key.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return p


def make(routes, **env):
    sess = FakeSession(routes)
    return KalshiPerpsClient(load_settings(environ=env), session=sess), sess


def test_orderbook_is_sorted_best_first_regardless_of_api_order():
    # Real demo response shape: best levels came back LAST.
    body = {"orderbook": {"asks": [["8.5008", "1.00"], ["8.5003", "58.00"], ["8.4997", "54.00"]],
                          "bids": [["8.4748", "590.00"], ["8.4754", "589.00"], ["8.4760", "355.00"]]}}
    c, _ = make({("GET", "/margin/markets/KXBTCPERP1/orderbook"): (200, body)})
    ob = c.orderbook()
    assert ob.best_bid == Decimal("8.4760")
    assert ob.best_ask == Decimal("8.4997")
    assert ob.spread == Decimal("0.0237")
    assert [p for p, _ in ob.asks] == sorted(p for p, _ in ob.asks)


def test_public_calls_are_unsigned():
    c, sess = make({("GET", "/margin/exchange/status"): (200, {"exchange_active": True, "trading_active": True})})
    c.exchange_status()
    assert "KALSHI-ACCESS-SIGNATURE" not in sess.calls[0]["headers"]


def test_private_calls_are_signed(keyfile):
    c, sess = make({("GET", "/margin/positions"): (200, {"positions": []})},
                   KALSHI_API_KEY_ID="kid", KALSHI_PRIVATE_KEY_PATH=str(keyfile))
    assert c.positions() == []
    h = sess.calls[0]["headers"]
    assert h["KALSHI-ACCESS-KEY"] == "kid" and h["KALSHI-ACCESS-SIGNATURE"]


@pytest.mark.parametrize("status", [401, 403])
def test_balance_auth_errors_do_not_crash(keyfile, status):
    c, _ = make({("GET", "/margin/balance"): (status, {"error": {"code": "forbidden"}})},
                KALSHI_API_KEY_ID="kid", KALSHI_PRIVATE_KEY_PATH=str(keyfile))
    r = c.balance()
    assert r.available is False and r.status == status


def test_balance_without_credentials_does_not_crash():
    c, sess = make({})
    r = c.balance()
    assert r.available is False and sess.calls == []


def test_balance_other_errors_still_raise(keyfile):
    c, _ = make({("GET", "/margin/balance"): (500, {"error": "boom"})},
                KALSHI_API_KEY_ID="kid", KALSHI_PRIVATE_KEY_PATH=str(keyfile))
    with pytest.raises(KalshiAPIError):
        c.balance()


@pytest.mark.parametrize("env", [
    {},
    {"KALSHI_ENV": "demo", "KALSHI_LIVE_TRADING": "true"},
    {"KALSHI_ENV": "prod", "KALSHI_LIVE_TRADING": "True"},
    {"KALSHI_ENV": "prod"},
])
def test_orders_blocked_unless_live(keyfile, env):
    c, sess = make({("POST", "/margin/orders"): (201, {"order": {}})},
                   KALSHI_API_KEY_ID="kid", KALSHI_PRIVATE_KEY_PATH=str(keyfile), **env)
    with pytest.raises(LiveTradingDisabled):
        c.create_order("bid", "1", "8.40")
    with pytest.raises(LiveTradingDisabled):
        c.cancel_all_orders()
    assert sess.calls == []  # nothing ever left the process


def test_order_body_when_live(keyfile):
    c, sess = make({("POST", "/margin/orders"): (201, {"order": {"order_id": "x"}})},
                   KALSHI_ENV="prod", KALSHI_LIVE_TRADING="true",
                   KALSHI_API_KEY_ID="kid", KALSHI_PRIVATE_KEY_PATH=str(keyfile))
    c.create_order("ask", Decimal("2"), Decimal("8.4500"), time_in_force="immediate_or_cancel")
    body = sess.calls[0]["json"]
    assert body["ticker"] == "KXBTCPERP" and body["side"] == "ask"
    assert body["count"] == "2" and body["price"] == "8.4500"
    assert body["self_trade_prevention_type"] == "taker_at_cross"
    assert body["client_order_id"]
