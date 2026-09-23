import base64

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kalshi_perps.auth import KalshiSigner, load_private_key

URL = "https://external-api.demo.kalshi.co/trade-api/v2/margin/orders?limit=5&status=resting"


def write_pem(tmp_path, key, name):
    p = tmp_path / name
    p.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    return p


def test_rsa_pss_signature_verifies_over_path_without_query(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signer = KalshiSigner.from_file("key-id-123", write_pem(tmp_path, key, "k.pem"))
    h = signer.headers("get", URL, timestamp_ms=1703123456789)

    assert h["KALSHI-ACCESS-KEY"] == "key-id-123"
    assert h["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"
    expected_msg = b"1703123456789GET/trade-api/v2/margin/orders"
    sig = base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(sig, expected_msg,
                            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                            hashes.SHA256())
    # Query string must not be part of the signed message.
    with pytest.raises(InvalidSignature):
        key.public_key().verify(sig, expected_msg + b"?limit=5&status=resting",
                                padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
                                hashes.SHA256())


def test_rsa_pss_is_randomized():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    s = KalshiSigner("k", key)
    a = s.headers("GET", URL, 1)["KALSHI-ACCESS-SIGNATURE"]
    b = s.headers("GET", URL, 1)["KALSHI-ACCESS-SIGNATURE"]
    assert a != b  # PSS uses a random salt


def test_ed25519_keys_also_supported(tmp_path):
    key = ed25519.Ed25519PrivateKey.generate()
    s = KalshiSigner("k", load_private_key(write_pem(tmp_path, key, "e.pem")))
    h = s.headers("POST", "/trade-api/v2/margin/orders", 42)
    key.public_key().verify(base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"42POST/trade-api/v2/margin/orders")
