"""Single-admin login: ADMIN_PASSWORD -> signed token (HMAC), valid 30 days."""
import base64
import hashlib
import hmac
import time

from fastapi import Header, HTTPException

from . import config

TTL = 30 * 24 * 3600


def _sign(msg: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()


def issue() -> str:
    exp = str(int(time.time()) + TTL)
    return base64.urlsafe_b64encode(f"admin.{exp}.{_sign('admin.' + exp)}".encode()).decode()


def check_password(pw: str) -> bool:
    return bool(config.ADMIN_PASSWORD) and hmac.compare_digest(pw.encode(), config.ADMIN_PASSWORD.encode())


def require(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    try:
        sub, exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split(".")
        ok = hmac.compare_digest(sig, _sign(f"{sub}.{exp}")) and int(exp) > time.time()
    except Exception:
        ok = False
    if not ok:
        raise HTTPException(401, "login required")
    return sub
