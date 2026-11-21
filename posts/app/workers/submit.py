"""Submit: send every new live post link to its Content Rewards campaign within the campaign's window
(30 minutes after it goes live), without anyone awake.

Methods, tried in the order of the `submit_methods` setting until one works:
- api:     Whop's official API (POST /bounty_submissions), when the campaign has a Whop bounty id and Whop is
           connected on the Settings page.
- browser: headless Chromium on this server, signed in with the saved Content Rewards session, fills in the
           campaign page's submit form (integrations/browser_submit.py).
- agent:   hands the job to an outside helper (a laptop or server running Claude with a browser) that polls
           /api/agent/submissions and reports back.

A failed attempt is retried after 1, 2, 4... minutes until the deadline. The user gets one email as soon as it is
clear the automatic route is struggling (second failure, or less than 12 minutes left), with the link and the
deadline in Tbilisi time, so it can still be submitted by hand.
"""
import time
from datetime import datetime, timedelta, timezone

from .. import config, db, notify
from ..integrations import browser_submit, whop

MAX_PER_RUN = 6
KEEPALIVE_EVERY = 6 * 3600
LATE_GRACE = timedelta(minutes=30)  # keep trying a little past the deadline: a late submission beats none


def _enqueue(s: dict) -> int:
    since = s.get("auto_submit_since")
    if not since:
        return 0
    rows = db.q("""UPDATE posts p SET submit_status='pending', submit_attempts=0, submit_next_at=now(),
                     submit_deadline = COALESCE(p.sent_at, now())
                                       + make_interval(mins => COALESCE(ca.submit_window_minutes, 30))
                   FROM campaigns ca
                   WHERE ca.id = p.campaign_id AND p.status='sent' AND p.external_link IS NOT NULL
                     AND NOT p.submitted AND p.submit_status IS NULL
                     AND COALESCE(p.sent_at, p.created_at) >= to_timestamp(%s)
                   RETURNING p.id""", (float(since),))
    return len(rows)


def _methods_for(post: dict, s: dict) -> list[str]:
    out = []
    for m in s.get("submit_methods") or ["api", "browser", "agent"]:
        if m == "api" and post.get("whop_bounty_id") and whop.configured() and whop.connected():
            out.append(m)
        elif m == "browser" and (post.get("submit_url") or post.get("campaign_url")) and browser_submit.available() \
                and browser_submit.has_session():
            out.append(m)
        elif m == "agent" and config.SUBMIT_AGENT_TOKEN:
            out.append(m)
    return out


def _done(post: dict, method: str, ref: str | None):
    db.q("""UPDATE posts SET submit_status='submitted', submitted=TRUE, submitted_at=now(), submit_method=%s,
              submit_ref=%s, submit_error=NULL, submit_next_at=NULL, updated_at=now() WHERE id=%s""",
         (method, ref, post["id"]))
    db.log("submit", f"{post['service']} post of clip #{post['clip_id']} submitted on Content Rewards ({method})")


def attempt(post: dict, s: dict) -> bool:
    methods = _methods_for(post, s)
    errors = []
    shot = None
    for m in methods:
        try:
            if m == "api":
                r = whop.submit(post["whop_bounty_id"], post["external_link"], None, f"posts-{post['id']}")
                _done(post, "api", r.get("id"))
                return True
            if m == "browser":
                r = browser_submit.run(post.get("submit_url") or post["campaign_url"], post["external_link"], post["service"])
                _done(post, "browser", r.get("screenshot"))  # the confirmation screenshot, as proof
                return True
            if m == "agent":
                db.q("""UPDATE posts SET submit_status='agent', submit_method='agent', submit_next_at=NULL, updated_at=now()
                        WHERE id=%s""", (post["id"],))
                db.log("submit", f"post {post['id']} handed to the outside submit helper")
                return True
        except browser_submit.BrowserError as e:
            errors.append(f"browser: {e}")
            shot = e.screenshot or shot
            if e.signed_out:
                notify.session_expired(str(e))
            if e.maybe_submitted:
                # Submit was pressed: never press it again on its own (no duplicate submissions); the user checks
                _failed(post, "; ".join(errors), shot, final=True)
                return False
        except Exception as e:
            errors.append(f"{m}: {e}")
    if not methods:
        errors.append("no submit method is set up yet (Settings → Auto-submit)")
    _failed(post, "; ".join(errors), shot)
    return False


def _failed(post: dict, error: str, shot: str | None, final: bool = False):
    n = (post.get("submit_attempts") or 0) + 1
    now = datetime.now(timezone.utc)
    deadline = post["submit_deadline"] or now
    give_up = final or now > deadline + LATE_GRACE or "no submit method" in error
    nxt = None if give_up else now + timedelta(minutes=min(2 ** (n - 1), 8))
    db.q("""UPDATE posts SET submit_status=%s, submit_attempts=%s, submit_error=%s, submit_next_at=%s, updated_at=now()
            WHERE id=%s""", ("failed" if give_up else "pending", n, (error + (f" (screenshot: {shot})" if shot else ""))[:1500],
                             nxt, post["id"]))
    db.log("submit", f"post {post['id']} ({post['service']}, clip #{post['clip_id']}) not submitted, try {n}: {error}",
           "error" if give_up else "warn")
    if not post.get("submit_alerted_at") and (n >= 2 or give_up or deadline - now < timedelta(minutes=12)):
        try:
            notify.submit_failed(post, error, shot)
            db.q("UPDATE posts SET submit_alerted_at=now() WHERE id=%s", (post["id"],))
        except Exception as e:
            db.log("notify", f"submit failure email for post {post['id']} failed: {e}", "error")


def _agent_overdue():
    """A job the outside helper hasn't finished 10 minutes before the deadline: tell the user."""
    for p in _rows("p.submit_status='agent' AND p.submit_alerted_at IS NULL AND p.submit_deadline < now() + interval '10 minutes'"):
        try:
            notify.submit_failed(p, "the outside submit helper has not submitted it yet", None)
            db.q("UPDATE posts SET submit_alerted_at=now() WHERE id=%s", (p["id"],))
        except Exception as e:
            db.log("notify", f"submit reminder for post {p['id']} failed: {e}", "error")


def _rows(where: str, params=(), limit: int = 50) -> list[dict]:
    return db.q(f"""SELECT p.*, c.title, c.caption, ca.name AS campaign, ca.url AS campaign_url, ca.submit_url,
                           ca.whop_bounty_id FROM posts p JOIN clips c ON c.id = p.clip_id
                    LEFT JOIN campaigns ca ON ca.id = p.campaign_id WHERE {where} ORDER BY p.submit_deadline LIMIT {int(limit)}""", params)


def _keepalive(s: dict):
    """Open the campaign page with the saved session every few hours, so a sign-out is found before a post needs it."""
    if "browser" not in (s.get("submit_methods") or []) or not browser_submit.available() or not browser_submit.has_session():
        return
    if time.time() - float(s.get("browser_checked_at") or 0) < KEEPALIVE_EVERY:
        return
    camp = db.one("SELECT url, submit_url FROM campaigns WHERE active AND COALESCE(submit_url, url) IS NOT NULL ORDER BY id LIMIT 1")
    if not camp:
        return
    db.set_setting("browser_checked_at", time.time())
    try:
        browser_submit.run(camp["submit_url"] or camp["url"], None, dry_run=True)
        db.set_setting("browser_status", {"ok": True, "at": time.time()})
    except browser_submit.BrowserError as e:
        db.set_setting("browser_status", {"ok": False, "at": time.time(), "error": str(e), "screenshot": e.screenshot})
        db.log("submit", f"browser session check failed: {e}", "warn")
        if e.signed_out:
            notify.session_expired(str(e))
    except Exception as e:
        db.set_setting("browser_status", {"ok": False, "at": time.time(), "error": str(e)})
        db.log("submit", f"browser session check crashed: {e}", "warn")


def run():
    s = db.settings()
    if not s.get("auto_submit"):
        return 0
    _enqueue(s)
    done = 0
    for p in _rows("p.submit_status='pending' AND p.submit_next_at <= now()", limit=MAX_PER_RUN):
        done += attempt(p, s)
    _agent_overdue()
    _keepalive(s)
    return done
