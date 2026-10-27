"""
Shared Kalshi authentication helpers.

Loads credentials from environment variables:
    KALSHI_KEY_ID           your API key ID
    KALSHI_PRIVATE_KEY_PATH path to your PEM private key file
"""

import base64
import os
import time

from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric import padding


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"Missing environment variable: {name}\n"
            f"Set it before running: export {name}=<value>"
        )
    return val


KEY_ID = _require_env("KALSHI_KEY_ID")
PRIVATE_KEY_PATH = _require_env("KALSHI_PRIVATE_KEY_PATH")


def load_private_key():
    with open(PRIVATE_KEY_PATH, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign_text(private_key, text: str) -> str:
    message = text.encode("utf-8")
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(message)
    else:
        signature = private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
    return base64.b64encode(signature).decode("utf-8")


def make_auth_headers(private_key, method: str, path: str) -> dict:
    timestamp = str(int(time.time() * 1000))
    msg_string = timestamp + method + path.split("?")[0]
    sig = sign_text(private_key, msg_string)
    return {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }
