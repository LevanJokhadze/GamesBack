"""Jarvis: what the dashboard's assistant knows, the cards it opens, and the actions it can take.

snapshot() is the live picture handed to Gemini when a session starts (campaigns and their rules, every account with
its status, streamers and numbers, videos stuck at 0 views and why, the calendar, pending Content Rewards submissions,
the review queue and worker health). attention() turns it into the cards the dashboard opens on its own. TOOLS are
what Gemini can call during a voice session (assistant_live.py runs them): reads, safe actions (run a worker) and
actions that change something (mark submitted, add a post link, approve a clip, change an account's status), which
only run with confirmed=true after the user said yes. UI tools (cards, modals, navigation, opening links) are sent to
the browser instead.
"""
import json
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from . import calendar, config, db, notify
from .workers import manual

router = APIRouter()

TBILISI = ZoneInfo("Asia/Tbilisi")
PAGES = ("overview", "calendar", "review", "posts", "accounts", "analytics", "moments", "campaigns", "settings")
WORKERS = ("discover", "render", "publish", "monitor", "manual", "submit")
TOO_EARLY_H = 3  # a post younger than this with 0 views is not a problem yet

PAY = """CASE WHEN GREATEST(p.views,0) / 1000.0 * ca.cpm < COALESCE(ca.min_payout, 0) THEN 0
              ELSE LEAST(GREATEST(p.views,0) / 1000.0 * ca.cpm, COALESCE(ca.max_payout, 1e12)) END"""


def _plain(v):
    """JSON-safe values: Decimal to number, datetimes to Tbilisi time."""
    if isinstance(v, Decimal):
        return float(v) if v % 1 else int(v)
    if isinstance(v, datetime):
        return v.astimezone(TBILISI).strftime("%Y-%m-%d %H:%M")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_plain(x) for x in v]
    return v


def dumps(v) -> str:
    return json.dumps(_plain(v), ensure_ascii=False, separators=(",", ":"))


def _age_h(t: datetime | None, now: datetime) -> float | None:
    return round((now - t).total_seconds() / 3600, 1) if t else None


FIXES = {
    "tiktok": "On TikTok 0 views after a few hours usually means the video is held for review or the account is "
              "throttled (new account, Georgian signals, wrong region). Open it and check it is public and not "
              "'under review'; warm the account up (20-30 min scrolling and liking US GTA/gaming videos on the US VPN); "
              "don't delete and re-upload the same clip right away.",
    "instagram": "New or quiet Instagram accounts get almost no Reels reach. Check the Reel shows in the profile's Reels "
                 "tab, keep posting at the US slots, share it to the story, and keep the caption short.",
    "youtube": "A Short at 0 after a few hours: check YouTube Studio for 'processing' or a restriction, make sure it was "
               "picked up as a Short (vertical, under 60 s) and the title leads with the hook.",
}
GENERAL_FIX = ("For the next clips: the hook must land in the first second, the campaign overlay must be visible the "
               "whole clip, and the caption must have the campaign's required line and hashtags.")


def diagnose(p: dict, s: dict, now: datetime) -> dict:
    """Why a post shows 0 views, and what to do. kind: early, untracked, unsynced, real."""
    age = _age_h(p.get("sent_at"), now) or 0
    manual_post = (p.get("channel_id") or "").startswith("manual:")
    if age < TOO_EARLY_H:
        return {"kind": "early", "why": f"Live for only {age} h: too early to judge.", "fix": "Check again later."}
    if manual_post:
        return {"kind": "untracked", "why": "Uploaded by hand: the pipeline can't read its views.",
                "fix": "Open the post to see its real views. If it really is at 0: " + FIXES.get(p.get("service"), "")}
    if s.get("stats_scope_missing") and not (p.get("service") == "youtube" and p.get("metrics_at")):
        return {"kind": "untracked", "why": "Views aren't tracked: the Buffer API key has no insights permission, so "
                "every Buffer post shows 0.",
                "fix": "Make a new Buffer key with the insightsRead permission ticked and put it in Railway "
                       "(BUFFER_API_KEY); the redeploy tests it on its own. Meanwhile open the post to see its real views."}
    if not p.get("metrics_at"):
        return {"kind": "unsynced", "why": "Its stats haven't been synced from Buffer yet.",
                "fix": "Run the monitor worker, or wait for the next stats sync (every 12 h)."}
    return {"kind": "real", "why": f"Live for {age} h with 0 views.", "fix": FIXES.get(p.get("service"), "") + " " + GENERAL_FIX}


def _campaigns() -> dict:
    rows = db.q("""SELECT id, name, url, cpm, min_payout, max_payout, channels, streamer_name, game, caption_template,
                          rules, overlay_url IS NOT NULL AS has_overlay, daily_quota, min_clip_views, submit_url,
                          submit_window_minutes, active, ended_at, end_reason
                   FROM campaigns ORDER BY active DESC, id DESC""")
    for c in rows:
        c["views_to_get_paid"] = int(-(-float(c["min_payout"] or 0) * 1000 // float(c["cpm"] or 1))) if c["cpm"] else None
    return {"active": [c for c in rows if c["active"]],
            "inactive": [{k: c[k] for k in ("id", "name", "cpm", "ended_at", "end_reason")} for c in rows if not c["active"]]}


def _posts(now: datetime, s: dict) -> list[dict]:
    rows = db.q(f"""SELECT p.id, p.clip_id, p.service, p.channel_id, p.channel_name, p.status, p.due_at, p.sent_at,
                           p.external_link, p.views, p.likes, p.comments, p.shares, p.metrics_at, p.submitted,
                           p.submit_status, p.submit_error, p.error, c.title AS clip_title, c.thumb_url,
                           ca.name AS campaign, ca.min_payout, ca.cpm, ({PAY}) AS est_pay,
                           CEIL(COALESCE(ca.min_payout,0) / NULLIF(ca.cpm,0) * 1000) AS views_needed
                    FROM posts p JOIN clips c ON c.id = p.clip_id LEFT JOIN campaigns ca ON ca.id = p.campaign_id
                    WHERE p.status NOT IN ('deleted','draft') AND COALESCE(p.sent_at, p.due_at) > now() - interval '21 days'
                    ORDER BY COALESCE(p.sent_at, p.due_at) DESC LIMIT 150""")
    for p in rows:
        p["age_h"] = _age_h(p["sent_at"], now)
        if p["status"] == "sent" and not p["views"]:
            p["zero"] = diagnose(p, s, now)
    return rows


def _accounts(posts: list[dict], now: datetime) -> list[dict]:
    s = db.settings()
    rows = db.q("""SELECT a.id, g.code AS geo, g.name AS geo_name, a.platform, a.handle, a.profile_url, a.status,
                          a.upload_mode, a.streamers, a.buffer_channel_id, a.followers, a.total_views, a.notes,
                          a.manual_since, a.created_at
                   FROM accounts a JOIN geos g ON g.id = a.geo_id ORDER BY g.sort, a.platform, lower(a.handle)""")
    active_streamers = {x.lower() for c in db.q("SELECT channels FROM campaigns WHERE active") for x in (c["channels"] or [])}
    for a in rows:
        key = a["buffer_channel_id"] or f"manual:{a['id']}"
        mine = [p for p in posts if p["channel_id"] == key]
        live = [p for p in mine if p["status"] == "sent"]
        a["videos"] = len(live)
        a["scheduled"] = sum(1 for p in mine if p["status"] == "scheduled")
        a["failed"] = sum(1 for p in mine if p["status"] == "error")
        a["views"] = sum(int(p["views"] or 0) for p in live)
        a["best_views"] = max([int(p["views"] or 0) for p in live], default=0)
        a["zero_view_videos"] = sum(1 for p in live if p.get("zero") and p["zero"]["kind"] != "early")
        a["paying_videos"] = sum(1 for p in live if float(p["est_pay"] or 0) > 0)
        a["last_post_at"] = max([p["sent_at"] for p in live if p["sent_at"]], default=None)
        a["targets_active_campaign"] = not a["streamers"] or any(x.lower() in active_streamers for x in a["streamers"])
        if a["upload_mode"] == "manual" and a["status"] in manual.LIVE_STATUSES:
            a["next_upload_at"] = manual.next_slot(a, s, now)
            a["manual_day"] = notify.manual_day(a)
        a["upload_page"] = notify.UPLOAD_PAGE.get(a["platform"])
    return rows


def _calendar(now: datetime) -> list[dict]:
    today = now.astimezone(TBILISI).date()
    out = []
    for e in calendar.build(today - timedelta(days=1), 4, now)["events"]:
        at = datetime.fromisoformat(e["at"])
        if at < now - timedelta(hours=12) or at > now + timedelta(hours=60):
            continue
        out.append({k: e.get(k) for k in ("kind", "status", "day", "time", "time_et", "platform", "handle", "account_id",
                                           "upload_id", "post_id", "clip_id", "title", "link", "upload_page")})
    return out


def snapshot() -> dict:
    now = datetime.now(timezone.utc)
    s = db.settings()
    posts = _posts(now, s)
    accounts = _accounts(posts, now)
    live = [p for p in posts if p["status"] == "sent"]
    pending = [p for p in live if p["external_link"] and not p["submitted"]]
    est = sum(float(p["est_pay"] or 0) for p in live)
    beats = db.q("SELECT worker, last_ok, last_error_at, last_error FROM heartbeats ORDER BY worker")
    review = db.q("""SELECT c.id, c.title, c.created_at, ca.name AS campaign FROM clips c
                     LEFT JOIN campaigns ca ON ca.id = c.campaign_id WHERE c.status = 'review' ORDER BY c.created_at DESC LIMIT 10""")
    ready = db.q("""SELECT c.id, c.title, c.approved_at, c.status, ca.name AS campaign,
                           (SELECT count(*) FROM posts p WHERE p.clip_id = c.id AND p.status <> 'deleted') AS posts
                    FROM clips c LEFT JOIN campaigns ca ON ca.id = c.campaign_id
                    WHERE c.status IN ('approved','scheduled','posted') AND c.approved_at > now() - interval '4 days'
                    ORDER BY c.approved_at DESC LIMIT 12""")
    buffer_paused = s.get("buffer_paused_until") or 0
    return {
        "now_tbilisi": now.astimezone(TBILISI).strftime("%a %d %b %Y %H:%M"),
        "campaigns": _campaigns(),
        "totals": {"live_videos": len(live), "views": sum(int(p["views"] or 0) for p in live),
                   "estimated_pay_usd": round(est, 2), "paying_videos": sum(1 for p in live if float(p["est_pay"] or 0) > 0)},
        "accounts": accounts,
        "videos": [{k: p.get(k) for k in ("id", "clip_id", "service", "channel_name", "status", "sent_at", "due_at", "age_h",
                                          "views", "likes", "comments", "external_link", "submitted", "submit_status",
                                          "campaign", "est_pay", "views_needed", "clip_title", "error", "zero")}
                   for p in posts[:60]],
        "zero_view_videos": [{"post_id": p["id"], "clip_id": p["clip_id"], "platform": p["service"],
                              "account": p["channel_name"], "title": p["clip_title"], "age_h": p["age_h"],
                              "link": p["external_link"], **p["zero"]}
                             for p in live if p.get("zero") and p["zero"]["kind"] != "early"],
        "pending_submissions": [{"post_id": p["id"], "platform": p["service"], "account": p["channel_name"],
                                 "title": p["clip_title"], "link": p["external_link"], "campaign": p["campaign"],
                                 "age_h": p["age_h"], "submit_status": p["submit_status"], "submit_error": p["submit_error"]}
                                for p in pending],
        "calendar_next_60h": _calendar(now),
        "review_queue": review,
        "clips_ready": ready,
        "pipeline": {
            "workers_paused": bool(s.get("workers_paused")), "auto_submit": bool(s.get("auto_submit")),
            "services": s.get("services"), "slots_et": s.get("slots_et"), "geo_slots": s.get("geo_slots"),
            "buffer_paused_until": datetime.fromtimestamp(buffer_paused, timezone.utc) if buffer_paused > time.time() else None,
            "views_tracked": not s.get("stats_scope_missing"),
            "workers": [b for b in beats if b["last_error_at"] and (not b["last_ok"] or b["last_error_at"] > b["last_ok"])],
        },
    }


# --- cards the dashboard opens on its own -------------------------------------------------------------------------

def _card(cid: str, tone: str, title: str, body: str, actions: list[dict], ref: dict | None = None) -> dict:
    return {"id": cid, "tone": tone, "title": title, "body": body, "actions": actions, "ref": ref}


def attention(snap: dict | None = None) -> list[dict]:
    """What needs the user now, most urgent first. Tones: urgent, warn, info, good."""
    snap = snap or snapshot()
    cards: list[dict] = []
    if not snap["campaigns"]["active"]:
        cards.append(_card("no-campaign", "urgent", "No active campaign",
                           "Nothing is being clipped or posted. Turn a campaign on.", [{"label": "Campaigns", "page": "campaigns"}]))
    for p in snap["pending_submissions"][:4]:
        late = (p["age_h"] or 0) * 60 > 30
        cards.append(_card(f"submit-{p['post_id']}", "urgent" if not late else "warn",
                           f"Submit to Content Rewards: {p['title'] or 'clip'}",
                           f"{p['platform']} {p['account'] or ''}, live {p['age_h']} h. "
                           + ("Content Rewards wants links within 30 minutes." if not late else "Past the 30-minute window, submit it anyway.")
                           + (f" Auto-submit failed: {p['submit_error']}" if p.get("submit_error") else ""),
                           [{"label": "Open post", "url": p["link"]},
                            {"label": "Mark submitted", "api": f"/posts/{p['post_id']}/submitted", "body": {"submitted": True}}],
                           {"type": "post", "id": p["post_id"]}))
    now = datetime.now(TBILISI)
    for e in snap["calendar_next_60h"]:
        if e["kind"] != "manual" or e["status"] not in ("to_upload", "planned"):
            continue
        at = datetime.fromisoformat(f"{e['day']}T{e['time']}").replace(tzinfo=TBILISI)
        mins = (at - now).total_seconds() / 60
        if e["status"] == "to_upload" or -60 < mins < 120:
            due = "now" if mins <= 5 else f"at {e['time']}"
            acts = [{"label": "Upload page", "url": e["upload_page"]}]
            if e.get("clip_id"):
                acts.insert(0, {"label": "Preview clip", "modal": {"kind": "clip", "id": e["clip_id"]}})
            if e.get("upload_id"):
                acts.append({"label": "Add link", "modal": {"kind": "upload", "id": e["upload_id"]}})
            cards.append(_card(f"upload-{e.get('upload_id') or e['account_id']}-{e['time']}", "urgent",
                               f"Upload {due} on {e['platform']} @{e['handle']}",
                               (e.get("title") or "The next clip") + f" ({e['time']} Tbilisi, {e.get('slot_time') or e['time_et']} {e.get('slot_zone') or 'ET'}).", acts,
                               {"type": "account", "id": e["account_id"]}))
    real = [z for z in snap["zero_view_videos"] if z["kind"] == "real"]
    for z in real[:3]:
        cards.append(_card(f"zero-{z['post_id']}", "warn", f"0 views: {z['title'] or 'clip'}",
                           f"{z['platform']} {z['account'] or ''}. {z['why']} {z['fix']}",
                           [a for a in ({"label": "Open post", "url": z["link"]} if z["link"] else None,
                                        {"label": "Preview clip", "modal": {"kind": "clip", "id": z["clip_id"]}}) if a],
                           {"type": "post", "id": z["post_id"]}))
    untracked = [z for z in snap["zero_view_videos"] if z["kind"] != "real"]
    if not snap["pipeline"]["views_tracked"]:
        cards.append(_card("stats-scope", "warn", "Views aren't being tracked",
                           "The Buffer key has no insights permission, so Buffer posts all show 0 views. Make a new key with "
                           "insights/metrics in Buffer and put it in Railway as BUFFER_API_KEY.",
                           [{"label": "Settings", "page": "settings"}]))
    elif untracked:
        cards.append(_card("untracked", "info", f"{len(untracked)} video(s) show 0 but aren't tracked",
                           "Hand uploads and unsynced posts: open them to check their real views.",
                           [{"label": "Posts", "page": "posts"}]))
    if snap["review_queue"]:
        cards.append(_card("review", "info", f"{len(snap['review_queue'])} clip(s) waiting for review",
                           "They won't be posted until you approve them.", [{"label": "Review", "page": "review"}]))
    pl = snap["pipeline"]
    if pl["workers_paused"]:
        cards.append(_card("paused", "warn", "Workers are paused", "Nothing is found, rendered or posted.",
                           [{"label": "Settings", "page": "settings"}]))
    if pl["buffer_paused_until"]:
        cards.append(_card("buffer-paused", "warn", "Buffer is paused",
                           f"Buffer's API allowance ran low; posting waits until {_plain(pl['buffer_paused_until'])}.",
                           [{"label": "Settings", "page": "settings"}]))
    for w in pl["workers"][:2]:
        cards.append(_card(f"worker-{w['worker']}", "warn", f"The {w['worker']} worker is failing",
                           (w["last_error"] or "")[:220], [{"label": "Run again", "worker": w["worker"]},
                                                           {"label": "Event log", "page": "settings"}]))
    for a in snap["accounts"]:
        if a["status"] in ("warming", "active") and not a["targets_active_campaign"]:
            cards.append(_card(f"acct-target-{a['id']}", "warn", f"@{a['handle']} targets an ended campaign",
                               f"Its streamers ({', '.join(a['streamers'])}) aren't in any active campaign, so it gets no new clips.",
                               [{"label": "Open account", "modal": {"kind": "account", "id": a["id"]}}], {"type": "account", "id": a["id"]}))
        elif a["status"] in ("warming", "active") and a["videos"] and a["last_post_at"] and \
                (datetime.now(timezone.utc) - a["last_post_at"]).total_seconds() > 48 * 3600 and not a["scheduled"] \
                and not a.get("next_upload_at"):
            cards.append(_card(f"acct-quiet-{a['id']}", "info", f"@{a['handle']} hasn't posted in 2 days",
                               f"{a['platform']} {a['geo']}: nothing scheduled.",
                               [{"label": "Open account", "modal": {"kind": "account", "id": a["id"]}}], {"type": "account", "id": a["id"]}))
    close = [v for v in snap["videos"] if v["status"] == "sent" and v["views_needed"] and v["views"]
             and 0.5 <= int(v["views"]) / float(v["views_needed"]) < 1]
    for v in close[:2]:
        cards.append(_card(f"close-{v['id']}", "good", f"Close to paying: {v['clip_title'] or 'clip'}",
                           f"{int(v['views']):,} of {int(v['views_needed']):,} views needed on {v['service']}.",
                           [a for a in ({"label": "Open post", "url": v["external_link"]} if v["external_link"] else None,) if a],
                           {"type": "post", "id": v["id"]}))
    best = max((v for v in snap["videos"] if v["status"] == "sent"), key=lambda v: int(v["views"] or 0), default=None)
    if best and int(best["views"] or 0) > 0:
        cards.append(_card(f"best-{best['id']}", "good", f"Best video: {int(best['views']):,} views",
                           f"{best['clip_title'] or 'clip'} on {best['service']} {best['channel_name'] or ''}.",
                           [a for a in ({"label": "Open post", "url": best["external_link"]} if best["external_link"] else None,
                                        {"label": "Preview clip", "modal": {"kind": "clip", "id": best["clip_id"]}}) if a],
                           {"type": "post", "id": best["id"]}))
    return cards


# --- modal data (the browser shows what the server loads) -------------------------------------------------------

def modal_data(kind: str, oid: int) -> dict:
    if kind == "clip":
        c = db.one("""SELECT c.id, c.title, c.caption, c.video_url, c.thumb_url, c.status, c.duration, c.created_at,
                             ca.name AS campaign, ca.caption_template, m.url AS source_url
                      FROM clips c LEFT JOIN campaigns ca ON ca.id = c.campaign_id LEFT JOIN moments m ON m.id = c.moment_id
                      WHERE c.id=%s""", (oid,))
        if not c:
            raise HTTPException(404, f"clip #{oid} not found")
        c["download_url"] = notify._download(c["video_url"]) if c["video_url"] else None
        c["posts"] = db.q("""SELECT id, service, channel_name, status, external_link, views, submitted, sent_at, due_at
                             FROM posts WHERE clip_id=%s AND status <> 'deleted' ORDER BY id""", (oid,))
        return c
    if kind == "account":
        snap_posts = _posts(datetime.now(timezone.utc), db.settings())
        a = next((x for x in _accounts(snap_posts, datetime.now(timezone.utc)) if x["id"] == oid), None)
        if not a:
            raise HTTPException(404, f"account #{oid} not found")
        key = a["buffer_channel_id"] or f"manual:{a['id']}"
        a["recent"] = [{k: p.get(k) for k in ("id", "clip_id", "clip_title", "status", "views", "external_link", "sent_at",
                                              "due_at", "submitted", "zero")} for p in snap_posts if p["channel_id"] == key][:12]
        a["uploads"] = db.q("""SELECT mu.id, mu.clip_id, mu.slot_at, mu.done_at, mu.link, c.title FROM manual_uploads mu
                               JOIN clips c ON c.id = mu.clip_id WHERE mu.account_id=%s ORDER BY mu.slot_at DESC LIMIT 6""", (oid,))
        return a
    if kind == "post":
        p = db.one(f"""SELECT p.*, c.title AS clip_title, c.video_url, c.thumb_url, c.caption, ca.name AS campaign, ({PAY}) AS est_pay,
                              CEIL(COALESCE(ca.min_payout,0) / NULLIF(ca.cpm,0) * 1000) AS views_needed
                       FROM posts p JOIN clips c ON c.id = p.clip_id LEFT JOIN campaigns ca ON ca.id = p.campaign_id
                       WHERE p.id=%s""", (oid,))
        if not p:
            raise HTTPException(404, f"post #{oid} not found")
        if p["status"] == "sent" and not p["views"]:
            p["zero"] = diagnose(p, db.settings(), datetime.now(timezone.utc))
        return p
    if kind == "upload":
        u = db.one("""SELECT mu.*, a.platform, a.handle, c.title, c.caption, c.video_url, c.thumb_url FROM manual_uploads mu
                      JOIN accounts a ON a.id = mu.account_id JOIN clips c ON c.id = mu.clip_id WHERE mu.id=%s""", (oid,))
        if not u:
            raise HTTPException(404, f"upload #{oid} not found")
        u["download_url"] = notify._download(u["video_url"]) if u["video_url"] else None
        u["upload_page"] = notify.UPLOAD_PAGE.get(u["platform"])
        return u
    raise HTTPException(400, f"unknown modal kind {kind}")


# --- REST routes (all behind the admin login) -------------------------------------------------------------------

@router.get("/assistant/context")
def context():
    snap = snapshot()
    return {"cards": _plain(attention(snap)), "snapshot": _plain(snap), "voice": voice_status()}


@router.get("/assistant/modal/{kind}/{oid}")
def modal(kind: str, oid: int):
    return _plain(modal_data(kind, oid))


def voice_status() -> dict:
    backend = (f"Vertex AI ({config.GOOGLE_CLOUD_LOCATION})" if config.GOOGLE_CLOUD_PROJECT
               else "Gemini API" if config.GEMINI_API_KEY else None)
    return {"configured": bool(backend), "backend": backend, "model": config.GEMINI_LIVE_MODEL}


# One-use tickets: a browser WebSocket can't send the Authorization header, so it trades its login for one.
TICKETS: dict[str, float] = {}


@router.post("/assistant/session")
def session():
    if not voice_status()["configured"]:
        raise HTTPException(400, "Jarvis needs Gemini on the server: set GEMINI_API_KEY (or GOOGLE_CLOUD_PROJECT and "
                                 "GOOGLE_CREDENTIALS_JSON) in Railway on the posts API service")
    now = time.time()
    for k, exp in list(TICKETS.items()):
        if exp < now:
            TICKETS.pop(k, None)
    t = secrets.token_urlsafe(24)
    TICKETS[t] = now + 60
    return {"ticket": t, **voice_status()}


def take_ticket(t: str) -> bool:
    exp = TICKETS.pop(t, 0)
    return exp > time.time()
