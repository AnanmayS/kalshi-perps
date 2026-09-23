import pytest

from kalshi_perps.config import load_settings


def s(**env):
    return load_settings(environ=env)


def test_defaults_to_demo_and_paper():
    st = s()
    assert st.env == "demo"
    assert st.ticker == "KXBTCPERP1"
    assert st.rest_base_url.startswith("https://external-api.demo.kalshi.co")
    assert st.live_trading_enabled is False
    assert str(st.daily_loss_limit) == "100"


@pytest.mark.parametrize("flag", ["True", "TRUE", "1", "yes", " true", "true ", "", "false"])
def test_live_requires_exact_true_string(flag):
    assert s(KALSHI_ENV="prod", KALSHI_LIVE_TRADING=flag).live_trading_enabled is False


def test_live_true_on_demo_is_still_paper():
    assert s(KALSHI_ENV="demo", KALSHI_LIVE_TRADING="true").live_trading_enabled is False


def test_live_only_prod_and_exact_true():
    st = s(KALSHI_ENV="prod", KALSHI_LIVE_TRADING="true")
    assert st.live_trading_enabled is True
    assert st.ticker == "KXBTCPERP"


def test_bad_env_rejected():
    with pytest.raises(ValueError):
        s(KALSHI_ENV="staging")


def test_missing_key_file_means_no_credentials(tmp_path):
    st = s(KALSHI_API_KEY_ID="abc", KALSHI_PRIVATE_KEY_PATH=str(tmp_path / "nope.pem"))
    assert st.has_credentials is False
