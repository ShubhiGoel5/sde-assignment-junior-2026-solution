"""Protects PII at rest.

Phones are matched with a keyed hash (HMAC), which gives the same token for the same phone
but can't be brute-forced without the key like a plain hash could. To read a value back,
the PII fields are encrypted with Fernet. Both keys come from one secret: AU_PII_KEY, or a
key file generated next to the database for local runs.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ENV_VAR = "AU_PII_KEY"


def _derive(master: bytes, purpose: bytes) -> bytes:
    return hmac.new(master, purpose, hashlib.sha256).digest()


class PiiVault:
    def __init__(self, master_secret: str):
        master = master_secret.encode("utf-8")
        self._index_key = _derive(master, b"phone-blind-index/v1")
        self._fernet = Fernet(base64.urlsafe_b64encode(_derive(master, b"field-encryption/v1")))
        # saved in the database so a wrong key is caught early
        self.fingerprint = _derive(master, b"fingerprint/v1").hex()[:16]

    def phone_token(self, canonical_phone: str) -> bytes:
        """HMAC-SHA256 of the phone, cut to 16 bytes (enough to avoid collisions)."""
        return hmac.digest(self._index_key, canonical_phone.encode(), "sha256")[:16]

    def encrypt(self, values: dict) -> bytes:
        return self._fernet.encrypt(json.dumps(values, ensure_ascii=False).encode("utf-8"))

    def decrypt(self, token: bytes) -> dict:
        return json.loads(self._fernet.decrypt(token))


def load_vault(db_path: Path) -> PiiVault:
    secret = os.environ.get(ENV_VAR)
    if not secret:
        key_file = Path(str(db_path) + ".key")
        if not key_file.exists():
            key_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            print(f"note: {ENV_VAR} not set; generated a local dev key at {key_file}", file=sys.stderr)
        secret = key_file.read_text(encoding="utf-8").strip()
    return PiiVault(secret)
