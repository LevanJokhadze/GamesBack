"""Whop public API (api.whop.com/api/v1): submit a post link to a campaign as the signed-in clipper.

Whop campaigns for clippers are "bounties" (business goal `clipping`). Content Rewards now runs its own site
(contentrewards.com, independent of Whop), so a Content Rewards campaign may not be reachable here: the Settings
page lists the bounties this account can see, and a campaign only uses this method once its bounty id is set.
A submission is
POST /bounty_submissions {bounty_id, deliverable: {urls: [link]}}. Authoring a submission needs a *user*
credential (Whop refuses account API keys for it), so the dashboard connects the user's Whop account once through
OAuth ("Sign in with Whop" for our own small Whop app). The access and refresh tokens are kept encrypted in the
database (ACCOUNTS_KEY) and refreshed automatically; nothing is ever committed or logged.
"""
import base64
import hashlib
import json
import secrets as pysecrets
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import config, db, vault

API = "https://api.whop.com/api/v1"
OAUTH_AUTHORIZE = "https://api.whop.com/oauth/authorize"
OAUTH_TOKEN = "https://api.whop.com/oauth/token"
API_VERSION = "2026-11-13-1"  # the dated version the official SDK (whop-sdk 2.8.1) pins
SCOPES = ["bounty:basic:read", "bounty:submission:create"]


class WhopError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def configured() -> bool:
    return bool(config.WHOP_CLIENT_ID and config.WHOP_CLIENT_SECRET)


def redirect_uri() -> str:
    return f"{config.PUBLIC_API_URL}/api/whop/callback"


# --- OAuth ---------------------------------------------------------------------------------------------------------

def authorize_url() -> str:
    """Authorization code flow with PKCE (S256), as Whop's OAuth guide describes."""
    state = pysecrets.token_urlsafe(24)
    verifier = pysecrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    vault.put("whop_oauth_pending", {"state": state, "verifier": verifier, "at": time.time()})
    return OAUTH_AUTHORIZE + "?" + urllib.parse.urlencode({
        "client_id": config.WHOP_CLIENT_ID, "redirect_uri": redirect_uri(), "response_type": "code",
        "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def check_state(state: str) -> str | None:
    """The PKCE verifier for this sign-in, if the state matches one started in the last 30 minutes."""
    saved = vault.get("whop_oauth_pending") or {}
    vault.delete("whop_oauth_pending")
    if state and saved.get("state") == state and time.time() - float(saved.get("at") or 0) < 1800:
        return saved["verifier"]
    return None


def _token_request(data: dict) -> dict:
    body = urllib.parse.urlencode({**data, "client_id": config.WHOP_CLIENT_ID,
                                   "client_secret": config.WHOP_CLIENT_SECRET}).encode()
    req = urllib.request.Request(OAUTH_TOKEN, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tok = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise WhopError(f"Whop sign-in failed ({e.code}): {e.read()[:300].decode(errors='replace')}", e.code)
    if not tok.get("access_token"):
        raise WhopError(f"Whop sign-in returned no token: {str(tok)[:200]}")
    tok["expires_at"] = time.time() + float(tok.get("expires_in") or 3600) - 120
    return tok


def exchange_code(code: str, verifier: str) -> dict:
    tok = _token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(),
                          "code_verifier": verifier})
    vault.put("whop_oauth", tok)
    return tok


def _access_token() -> str:
    tok = vault.get("whop_oauth")
    if not tok:
        raise WhopError("Whop is not connected: press “Connect Whop” on the Settings page", 401)
    if time.time() > float(tok.get("expires_at") or 0):
        if not tok.get("refresh_token"):
            raise WhopError("The Whop sign-in expired: press “Connect Whop” on the Settings page again", 401)
        new = _token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"]})
        new.setdefault("refresh_token", tok["refresh_token"])  # Whop rotates it on each use; keep the newest
        vault.put("whop_oauth", new)
        tok = new
    return tok["access_token"]


def connected() -> bool:
    return bool(vault.get("whop_oauth"))


def disconnect():
    vault.delete("whop_oauth")


# --- API -----------------------------------------------------------------------------------------------------------

def _call(method: str, path: str, params: dict | None = None, body: dict | None = None,
          idempotency_key: str | None = None) -> dict:
    url = f"{API}/{path}" + ("?" + urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
                             if params else "")
    headers = {"Authorization": f"Bearer {_access_token()}", "Api-Version-Date": API_VERSION,
               "Accept": "application/json", "Content-Type": "application/json", "User-Agent": "posts-pipeline/1.0"}
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = urllib.request.Request(url, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        text = e.read()[:600].decode(errors="replace")
        try:
            j = json.loads(text)
            text = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("message") or text
        except Exception:
            pass
        raise WhopError(f"Whop said {e.code}: {text}", e.code)
    except urllib.error.URLError as e:
        raise WhopError(f"Whop could not be reached: {e.reason}")


def me() -> dict:
    return _call("GET", "users/me")


def bounties(query: str | None = None) -> list[dict]:
    """Campaigns this user can see and work, newest first (business goal: clipping, then anything matching)."""
    out, seen = [], set()
    for goal in ("clipping", None):
        r = _call("GET", "bounties", {"business_goal_type": goal, "query": query or None, "first": 50,
                                      "order": "created_at", "direction": "desc"})
        for b in r.get("data") or []:
            if b["id"] not in seen:
                seen.add(b["id"])
                out.append({"id": b["id"], "title": b.get("title"), "status": b.get("status"),
                            "goal": b.get("business_goal_type"), "experience_id": b.get("experience_id"),
                            "host": (b.get("hosting_account") or {}).get("title") or (b.get("hosting_account") or {}).get("name"),
                            "accepted_deliverable_types": b.get("accepted_deliverable_types")})
    return out


def submit(bounty_id: str, link: str, caption: str | None, key: str) -> dict:
    """One submission per post link. `key` makes a retry after a timeout return the first submission, not a second."""
    deliverable = {"urls": [link]}
    if caption:
        deliverable["caption"] = caption[:1000]
    r = _call("POST", "bounty_submissions", body={"bounty_id": bounty_id, "deliverable": deliverable},
              idempotency_key=key)
    return {"id": r.get("id"), "status": r.get("status")}


def submission(sub_id: str) -> dict:
    return _call("GET", f"bounty_submissions/{urllib.parse.quote(sub_id)}")
