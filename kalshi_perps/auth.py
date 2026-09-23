"""Kalshi request signing.

Message = timestamp_ms + METHOD + path, where path is the full URL path from the
API root (e.g. /trade-api/v2/margin/balance) with the query string stripped.
RSA keys sign with RSA-PSS / SHA-256 (salt length = digest length); Ed25519 keys
(Kalshi's current portal default) sign the message directly. Signature is base64.
"""

from __future__ import annotations

import base64
import time
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def load_private_key(path: str | Path):
    with open(Path(path).expanduser(), "rb") as f:
        key = serialization.load_pem_private_key(f.read(), password=None)
    if not isinstance(key, (rsa.RSAPrivateKey, Ed25519PrivateKey)):
        raise ValueError(f"Unsupported key type {type(key).__name__}; expected RSA or Ed25519")
    return key


def sign_message(private_key, message: str) -> str:
    data = message.encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        sig = private_key.sign(data)
    else:
        sig = private_key.sign(
            data,
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256(),
        )
    return base64.b64encode(sig).decode("ascii")


class KalshiSigner:
    def __init__(self, api_key_id: str, private_key):
        self.api_key_id = api_key_id
        self.private_key = private_key

    @classmethod
    def from_file(cls, api_key_id: str, key_path: str | Path) -> "KalshiSigner":
        return cls(api_key_id, load_private_key(key_path))

    def headers(self, method: str, url_or_path: str, timestamp_ms: int | None = None) -> dict[str, str]:
        ts = str(timestamp_ms if timestamp_ms is not None else int(time.time() * 1000))
        path = urlparse(url_or_path).path  # drops scheme/host/query
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sign_message(self.private_key, ts + method.upper() + path),
        }
