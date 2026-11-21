"""Small encrypted key/value store in the database for sign-ins the workers need (Whop tokens, browser cookies).

Values are JSON, encrypted with Fernet under the same ACCOUNTS_KEY variable as account passwords, and never
returned by the settings or overview endpoints."""
import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken

from . import config, db


class VaultError(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not config.ACCOUNTS_KEY:
        raise VaultError("ACCOUNTS_KEY is not set on the server, so sign-ins can't be stored")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(config.ACCOUNTS_KEY.encode()).digest()))


def put(key: str, value) -> None:
    enc = _fernet().encrypt(json.dumps(value).encode()).decode()
    db.q("""INSERT INTO vault(key, value_enc, updated_at) VALUES (%s,%s,now())
            ON CONFLICT (key) DO UPDATE SET value_enc=EXCLUDED.value_enc, updated_at=now()""", (key, enc))


def get(key: str):
    r = db.one("SELECT value_enc FROM vault WHERE key=%s", (key,))
    if not r:
        return None
    try:
        return json.loads(_fernet().decrypt(r["value_enc"].encode()))
    except (InvalidToken, VaultError):
        return None


def updated_at(key: str):
    r = db.one("SELECT updated_at FROM vault WHERE key=%s", (key,))
    return r["updated_at"] if r else None


def delete(key: str) -> None:
    db.q("DELETE FROM vault WHERE key=%s", (key,))
