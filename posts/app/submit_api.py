"""Auto-submit endpoints: status, Whop sign-in, browser session, per-post retry, and the outside-helper queue."""
import hmac
import time

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from . import auth, config, db, vault
from .integrations import browser_submit, whop
from .workers import runner

router = APIRouter(prefix="/api")
A = Depends(auth.require)


@router.get("/submit/status", dependencies=[A])
def status():
    s = db.settings()
    counts = {r["submit_status"]: r["n"] for r in db.q(
        "SELECT submit_status, count(*) n FROM posts WHERE submit_status IS NOT NULL GROUP BY submit_status")}
    return {
        "auto_submit": bool(s.get("auto_submit")), "methods": s.get("submit_methods") or [],
        "whop": {"configured": whop.configured(), "connected": whop.connected(),
                 "redirect_uri": whop.redirect_uri() if config.PUBLIC_API_URL else None,
                 "connected_at": vault.updated_at("whop_oauth")},
        "browser": {"installed": browser_submit.available(), "session": browser_submit.has_session(),
                    "saved_at": vault.updated_at(browser_submit.SESSION_KEY), "check": s.get("browser_status")},
        "agent": {"configured": bool(config.SUBMIT_AGENT_TOKEN)},
        "vault": bool(config.ACCOUNTS_KEY), "counts": counts,
    }


class AutoIn(BaseModel):
    auto_submit: bool | None = None
    methods: list[str] | None = None


@router.put("/submit/settings", dependencies=[A])
def put_settings(body: AutoIn):
    if body.methods is not None:
        bad = [m for m in body.methods if m not in ("api", "browser", "agent")]
        if bad:
            raise HTTPException(400, f"unknown method: {bad[0]}")
        db.set_setting("submit_methods", body.methods)
    if body.auto_submit is not None:
        was = db.settings().get("auto_submit")
        db.set_setting("auto_submit", body.auto_submit)
        if body.auto_submit and not was:
            # only posts that go live from now on: older ones were handled (or not) by hand
            db.set_setting("auto_submit_since", time.time())
            db.log("submit", "auto-submit turned on")
            runner.trigger("submit")
        elif not body.auto_submit and was:
            db.log("submit", "auto-submit turned off")
    return status()


# --- Whop sign-in --------------------------------------------------------------------------------------------------

@router.get("/whop/connect", dependencies=[A])
def whop_connect():
    if not whop.configured():
        raise HTTPException(400, "Add WHOP_CLIENT_ID and WHOP_CLIENT_SECRET on the server first")
    if not config.PUBLIC_API_URL:
        raise HTTPException(400, "Add PUBLIC_API_URL (this API's public https address) on the server first")
    if not config.ACCOUNTS_KEY:
        raise HTTPException(400, "Add ACCOUNTS_KEY on the server first (it encrypts the Whop sign-in)")
    return {"url": whop.authorize_url()}


@router.get("/whop/callback", include_in_schema=False)
def whop_callback(code: str = "", state: str = "", error: str = ""):
    """Whop sends the browser back here after consent (no dashboard token on this request: the state proves it)."""
    back = f"{config.DASHBOARD_URL}/#settings"
    verifier = whop.check_state(state)
    if error or not code or not verifier:
        db.log("submit", f"Whop sign-in did not complete: {error or 'bad or expired state'}", "warn")
        return HTMLResponse(f'<p>Whop sign-in did not complete ({error or "expired"}). <a href="{back}">Back</a></p>', 400)
    try:
        whop.exchange_code(code, verifier)
    except Exception as e:
        db.log("submit", f"Whop sign-in failed: {e}", "error")
        return HTMLResponse(f'<p>Whop sign-in failed. <a href="{back}">Back</a></p>', 502)
    db.log("submit", "Whop account connected")
    return RedirectResponse(back)


@router.post("/whop/disconnect", dependencies=[A])
def whop_disconnect():
    whop.disconnect()
    db.log("submit", "Whop account disconnected")
    return {"ok": True}


@router.get("/whop/bounties", dependencies=[A])
def whop_bounties(query: str | None = None):
    try:
        return {"me": whop.me(), "bounties": whop.bounties(query)}
    except whop.WhopError as e:
        raise HTTPException(502, str(e))


# --- browser session ----------------------------------------------------------------------------------------------

class SessionIn(BaseModel):
    cookies: list | dict
    local_storage: dict | None = None
    origin: str | None = None


@router.post("/submit/browser-session", dependencies=[A])
def save_browser_session(body: SessionIn):
    raw = body.cookies
    if isinstance(raw, list):
        raw = {"cookies_json": raw, "local_storage": body.local_storage or {}, "origin": body.origin}
    try:
        n = browser_submit.save_session(raw)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except vault.VaultError as e:
        raise HTTPException(400, str(e))
    db.set_setting("browser_checked_at", 0)  # check it on the next run
    db.log("submit", f"browser session saved ({n['cookies']} cookies)")
    return n


@router.delete("/submit/browser-session", dependencies=[A])
def delete_browser_session():
    vault.delete(browser_submit.SESSION_KEY)
    return {"ok": True}


@router.post("/submit/browser-test", dependencies=[A])
def browser_test(campaign_id: int | None = None):
    """Open the campaign page with the saved session and the submit form, without submitting anything."""
    if not browser_submit.available():
        raise HTTPException(400, "The headless browser is not installed in this build")
    camp = db.one("SELECT url, submit_url FROM campaigns WHERE (%s::int IS NULL OR id=%s) AND COALESCE(submit_url, url) IS NOT NULL "
                  "ORDER BY active DESC, id LIMIT 1", (campaign_id, campaign_id))
    if not camp:
        raise HTTPException(404, "no campaign with a page URL")
    try:
        r = browser_submit.run(camp["submit_url"] or camp["url"], None, dry_run=True)
    except browser_submit.BrowserError as e:
        db.set_setting("browser_status", {"ok": False, "at": time.time(), "error": str(e), "screenshot": e.screenshot})
        return {"ok": False, "error": str(e), "screenshot": e.screenshot, "signed_out": e.signed_out}
    except Exception as e:
        return {"ok": False, "error": f"browser crashed: {e}"}
    db.set_setting("browser_status", {"ok": True, "at": time.time(), "screenshot": r.get("screenshot")})
    return {"ok": True, **r}


# --- per post ------------------------------------------------------------------------------------------------------

@router.post("/posts/{post_id}/submit", dependencies=[A])
def submit_now(post_id: int):
    """Submit (or retry) this post now, even if it went live before auto-submit was on."""
    r = db.one("""UPDATE posts p SET submit_status='pending', submit_attempts=0, submit_next_at=now(), submit_error=NULL,
                    submit_alerted_at=NULL,
                    submit_deadline = COALESCE(p.submit_deadline, COALESCE(p.sent_at, now())
                                      + make_interval(mins => COALESCE(ca.submit_window_minutes, 30)))
                  FROM campaigns ca WHERE ca.id = p.campaign_id AND p.id=%s AND p.status='sent'
                    AND p.external_link IS NOT NULL AND NOT p.submitted RETURNING p.id""", (post_id,))
    if not r:
        raise HTTPException(409, "only a live post that isn't submitted yet can be submitted")
    if not db.settings().get("auto_submit"):
        # run this one post now without turning auto-submit on for everything
        from .workers import submit as w
        rows = w._rows("p.id=%s", (post_id,))
        ok = w.attempt(rows[0], db.settings()) if rows else False
        return {"ok": ok}
    runner.trigger("submit")
    return {"ok": True, "queued": True}


# --- outside helper (laptop / server running Claude with a browser) -----------------------------------------------

def _agent(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if not config.SUBMIT_AGENT_TOKEN or not hmac.compare_digest(token.encode(), config.SUBMIT_AGENT_TOKEN.encode()):
        raise HTTPException(401, "bad helper token")


@router.get("/agent/submissions", dependencies=[Depends(_agent)])
def agent_jobs():
    """Posts waiting for the helper, soonest deadline first: open `submit_page`, press Submit, paste `link`."""
    rows = db.q("""SELECT p.id, p.service, p.external_link AS link, p.submit_deadline AS deadline, c.title,
                          ca.name AS campaign, COALESCE(ca.submit_url, ca.url) AS submit_page FROM posts p
                   JOIN clips c ON c.id=p.clip_id LEFT JOIN campaigns ca ON ca.id=p.campaign_id
                   WHERE p.submit_status='agent' AND NOT p.submitted ORDER BY p.submit_deadline LIMIT 20""")
    db.set_setting("agent_seen_at", time.time())
    return rows


class AgentResult(BaseModel):
    ok: bool
    error: str | None = None
    ref: str | None = None


@router.post("/agent/submissions/{post_id}", dependencies=[Depends(_agent)])
def agent_result(post_id: int, body: AgentResult):
    p = db.one("SELECT * FROM posts WHERE id=%s AND submit_status='agent'", (post_id,))
    if not p:
        raise HTTPException(404, "no such job")
    if body.ok:
        db.q("""UPDATE posts SET submit_status='submitted', submitted=TRUE, submitted_at=now(), submit_ref=%s,
                  submit_error=NULL, updated_at=now() WHERE id=%s""", (body.ref, post_id))
        db.log("submit", f"post {post_id} submitted by the outside helper")
    else:
        from . import notify
        from .workers import submit as w
        db.q("UPDATE posts SET submit_status='failed', submit_error=%s, updated_at=now() WHERE id=%s",
             (f"helper: {body.error or 'failed'}"[:1500], post_id))
        db.log("submit", f"outside helper could not submit post {post_id}: {body.error}", "error")
        rows = w._rows("p.id=%s", (post_id,))
        if rows and not rows[0].get("submit_alerted_at"):
            notify.submit_failed(rows[0], f"helper: {body.error or 'failed'}", None)
            db.q("UPDATE posts SET submit_alerted_at=now() WHERE id=%s", (post_id,))
    return {"ok": True}
