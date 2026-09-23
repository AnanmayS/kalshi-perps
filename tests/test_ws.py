import base64
from decimal import Decimal as D

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_perps.config import load_settings
from kalshi_perps.store import MarketStore
from kalshi_perps.ws import Collector, LocalBook


@pytest.fixture
def collector(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    p = tmp_path / "k.pem"
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    s = load_settings(environ={"KALSHI_API_KEY_ID": "kid", "KALSHI_PRIVATE_KEY_PATH": str(p)})
    c = Collector(s, MarketStore(":memory:"), snapshot_every=1e9, log=lambda m: None)
    c._pubkey = key.public_key()
    return c


SNAP = {"type": "orderbook_snapshot", "sid": 1, "seq": 1,
        "msg": {"market_ticker": "KXBTCPERP1", "bid": [["8.49", "5.00"], ["8.48", "10.00"]],
                "ask": [["8.52", "7.00"], ["8.51", "3.00"]]}}


def delta(seq, side, price, d, sid=1):
    return {"type": "orderbook_delta", "sid": sid, "seq": seq,
            "msg": {"market_ticker": "KXBTCPERP1", "side": side, "price": price, "delta": d, "ts_ms": 1_790_000_000_000}}


def test_handshake_is_signed_over_ws_path(collector):
    h = collector.handshake_headers()
    msg = (h["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/ws/v2/margin").encode()
    collector._pubkey.verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), msg,
                             padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                             hashes.SHA256())


def test_subscribes_to_book_ticker_trades(collector):
    cmds = collector.subscribe_commands()
    assert [c["params"]["channels"][0] for c in cmds] == ["orderbook_delta", "ticker", "trade"]
    assert all(c["params"]["market_ticker"] == "KXBTCPERP1" for c in cmds)
    assert len({c["id"] for c in cmds}) == 3


def test_local_book_snapshot_and_deltas():
    b = LocalBook()
    b.load_snapshot(SNAP["msg"])
    assert b.best() == (D("8.49"), D("5.00"), D("8.51"), D("3.00"))
    b.apply_delta("ask", "8.51", "-3.00")       # level removed
    b.apply_delta("bid", "8.50", "2.00")        # new better bid
    assert b.best() == (D("8.50"), D("2.00"), D("8.52"), D("7.00"))
    assert not b.crossed


def test_quotes_recorded_only_on_top_change(collector):
    collector.handle(SNAP, now=1.0)
    collector.handle(delta(2, "bid", "8.48", "5.00"), now=2.0)   # below top: no quote
    collector.handle(delta(3, "ask", "8.51", "-1.00"), now=3.0)  # top size change: quote
    assert collector.stats["quotes"] == 2
    rows = collector.store.db.execute("SELECT bid, ask, ask_size FROM quotes ORDER BY rowid").fetchall()
    assert rows[-1] == ("8.49", "8.51", "2.00")


def test_seq_gap_invalidates_book_and_requests_snapshot(collector):
    collector.handle(SNAP, now=1.0)
    collector.handle(delta(3, "bid", "8.50", "1.00"), now=2.0)   # seq 2 missing
    assert not collector.book.valid and collector.stats["gaps"] == 1
    cmd = collector.outbox[-1]
    assert cmd["cmd"] == "update_subscription" and cmd["params"]["action"] == "get_snapshot"
    assert cmd["params"]["sids"] == [1]
    quotes = collector.stats["quotes"]
    collector.handle(delta(4, "bid", "8.50", "1.00"), now=3.0)   # ignored until resnapshot
    assert collector.stats["quotes"] == quotes
    collector.handle({**SNAP, "seq": 5}, now=4.0)
    assert collector.book.valid


def test_trades_and_tickers_stored(collector):
    collector.handle({"type": "trade", "sid": 3, "seq": 1, "msg": {
        "trade_id": "t1", "market_ticker": "KXBTCPERP1", "price": "8.50", "count": "2.00",
        "taker_side": "bid", "ts_ms": 1}})
    collector.handle({"type": "trade", "sid": 3, "seq": 2, "msg": {
        "trade_id": "t1", "market_ticker": "KXBTCPERP1", "price": "8.50", "count": "2.00",
        "taker_side": "bid", "ts_ms": 1}})  # duplicate id ignored
    collector.handle({"type": "ticker", "sid": 2, "msg": {
        "market_ticker": "KXBTCPERP1", "ts_ms": 5, "price": "8.50", "bid": "8.49", "ask": "8.51",
        "settlement_mark_price": {"price": "8.505", "ts_ms": 5},
        "funding_rate": {"rate": 0.001, "next_funding_time_ms": 99, "ts_ms": 5}}})
    c = collector.store.counts()
    assert c["trades"] == 1 and c["tickers"] == 1
    assert collector.store.db.execute("SELECT mark_price, funding_rate FROM tickers").fetchone() == ("8.505", 0.001)
