"""Environment-driven configuration.

Safety: live trading is enabled ONLY when KALSHI_LIVE_TRADING is exactly the
string "true" AND KALSHI_ENV is "prod". Every other combination is paper-only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv

REST_BASE_URLS = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "prod": "https://external-api.kalshi.com/trade-api/v2",
}
WS_URLS = {
    "demo": "wss://external-api-margin-ws.demo.kalshi.co/trade-api/ws/v2/margin",
    "prod": "wss://external-api-margin-ws.kalshi.com/trade-api/ws/v2/margin",
}
TICKERS = {"demo": "KXBTCPERP1", "prod": "KXBTCPERP"}


@dataclass(frozen=True)
class Settings:
    env: str
    api_key_id: str | None
    private_key_path: Path | None
    live_trading_flag: str
    max_notional_per_trade: Decimal
    daily_loss_limit: Decimal

    @property
    def rest_base_url(self) -> str:
        return REST_BASE_URLS[self.env]

    @property
    def ws_url(self) -> str:
        return WS_URLS[self.env]

    @property
    def ticker(self) -> str:
        return TICKERS[self.env]

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id) and self.private_key_path is not None and self.private_key_path.is_file()

    @property
    def live_trading_enabled(self) -> bool:
        # Exact string compare on purpose: "True", "1", "yes" do NOT enable live trading.
        return self.live_trading_flag == "true" and self.env == "prod"


def load_settings(env_file: str | os.PathLike | None = None, environ: dict | None = None) -> Settings:
    """Build Settings from the process environment (after loading .env if present).

    Pass `environ` to bypass os.environ entirely (used by tests).
    """
    if environ is None:
        load_dotenv(env_file, override=False)
        environ = dict(os.environ)

    env = (environ.get("KALSHI_ENV") or "demo").strip().lower()
    if env not in REST_BASE_URLS:
        raise ValueError(f"KALSHI_ENV must be 'demo' or 'prod', got {env!r}")

    key_path_raw = (environ.get("KALSHI_PRIVATE_KEY_PATH") or "").strip()
    key_path = Path(key_path_raw).expanduser() if key_path_raw else None

    return Settings(
        env=env,
        api_key_id=(environ.get("KALSHI_API_KEY_ID") or "").strip() or None,
        private_key_path=key_path,
        live_trading_flag=environ.get("KALSHI_LIVE_TRADING", ""),
        max_notional_per_trade=Decimal(environ.get("RISK_MAX_NOTIONAL_PER_TRADE") or "500"),
        daily_loss_limit=Decimal(environ.get("RISK_DAILY_LOSS_LIMIT") or "100"),
    )
