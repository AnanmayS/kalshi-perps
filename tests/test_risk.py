from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from risk import RiskConfig, RiskEngine

T0 = datetime(2026, 9, 23, 15, 0, tzinfo=timezone.utc)


def eng():
    return RiskEngine(RiskConfig(max_notional_per_trade=D("500"), daily_loss_limit=D("100")), D("10000"), T0)


def test_default_daily_loss_limit_is_100():
    assert RiskConfig().daily_loss_limit == D("100")


def test_notional_cap():
    r = eng()
    assert r.check_order(D("500"), False, T0).allowed
    d = r.check_order(D("500.01"), False, T0)
    assert not d.allowed and "exceeds max" in d.reason
    assert r.check_order(D("900"), True, T0).allowed   # closing is never blocked by the cap


def test_kill_switch_trips_at_limit_and_blocks_new_risk():
    r = eng()
    r.update_equity(D("9900.01"), T0)
    assert not r.killed
    r.update_equity(D("9900"), T0)                      # exactly -100
    assert r.killed and "daily loss" in r.kill_reason
    assert not r.check_order(D("10"), False, T0).allowed
    assert r.check_order(D("10"), True, T0).allowed     # can still flatten


def test_manual_reset_retrips_while_still_over_limit():
    r = eng()
    r.update_equity(D("9850"), T0)
    r.reset(T0)
    assert r.killed


def test_new_utc_day_rebases_and_clears_loss_kill():
    r = eng()
    r.update_equity(D("9850"), T0)
    assert r.killed
    tomorrow = T0 + timedelta(days=1)
    assert r.check_order(D("10"), False, tomorrow).allowed
    assert r.day_start_equity == D("9850") and r.daily_pnl == 0


def test_manual_kill_persists_across_day_until_reset():
    r = eng()
    r.kill("manual", T0)
    assert not r.check_order(D("10"), False, T0 + timedelta(days=1)).allowed
    r.reset(T0 + timedelta(days=1))
    assert not r.killed
