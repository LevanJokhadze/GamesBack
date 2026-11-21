"""Monitor: learn when Buffer has published our posts (public link) or failed them, and record their stats.

Kept cheap because Buffer's free plan allows ~100 requests a day on average (3,000 / 30 days):
- no request at all while nothing is due,
- one list request for all channels once posts are due,
- stats in one list request at most every 12 hours.
"""
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .. import config, db, notify
from ..integrations import buffer

FIELDS = {"views": ("view", "views", "plays", "video_views", "impressions"),
          "likes": ("like", "likes", "reactions"),
          "comments": ("comment", "comments"),
          "shares": ("share", "shares", "reposts"),
          "saves": ("save", "saves", "favorites")}

STATS_EVERY = 12 * 3600
YT_EVERY = 3600
SYNC_EVERY = 2 * 3600  # also look every 2 h while posts are queued, to catch "Publish Now", edits and deletions in Buffer


def _pick(metrics: list[dict]) -> dict:
    out = {}
    for m in metrics or []:
        key = f"{m.get('type') or ''} {m.get('name') or ''}".lower()
        for f, names in FIELDS.items():
            if f not in out and any(n in key.split() or key.endswith(n) for n in names):
                out[f] = int(float(m.get("value") or 0))
    return out


_YT_ID = re.compile(r"(?:shorts/|watch\?v=|youtu\.be/|embed/)([A-Za-z0-9_-]{11})")


def youtube_stats() -> int:
    """Public counts of our YouTube Shorts from the YouTube Data API (needs YOUTUBE_API_KEY). Returns posts updated."""
    if not config.YOUTUBE_API_KEY:
        return 0
    ids = {}
    for p in db.q("""SELECT id, external_link FROM posts WHERE service='youtube' AND status='sent'
                     AND external_link IS NOT NULL AND sent_at > now() - interval '30 days'"""):
        m = _YT_ID.search(p["external_link"])
        if m:
            ids.setdefault(m.group(1), []).append(p["id"])
    done, now = 0, datetime.now(timezone.utc)
    vids = list(ids)
    for i in range(0, len(vids), 50):
        url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(
            {"part": "statistics", "id": ",".join(vids[i:i + 50]), "key": config.YOUTUBE_API_KEY})
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                items = json.load(r).get("items") or []
        except Exception as e:  # noqa: BLE001  (a bad key must not stop the monitor)
            db.log("monitor", f"YouTube stats failed: {str(e)[:200]}", "warn")
            return done
        for it in items:
            st = it.get("statistics") or {}
            views, likes, comments = (int(st[k]) if st.get(k) is not None else None
                                      for k in ("viewCount", "likeCount", "commentCount"))
            for pid in ids.get(it["id"], []):
                db.q("""UPDATE posts SET views=COALESCE(%s, views), likes=COALESCE(%s, likes),
                        comments=COALESCE(%s, comments), metrics_at=%s WHERE id=%s""", (views, likes, comments, now, pid))
                db.q("INSERT INTO metrics(post_id, views, likes, comments) VALUES (%s,%s,%s,%s)", (pid, views, likes, comments))
                done += 1
    return done


def _apply(p: dict, st: dict) -> bool:
    err = (st.get("error") or {}).get("message")
    if st["status"] == p["status"] and st.get("externalLink") == p["external_link"] and err == p["error"]:
        return False
    db.q("""UPDATE posts SET status=%s, external_link=%s, error=%s, sent_at=%s, updated_at=now() WHERE id=%s""",
         (st["status"], st.get("externalLink"), err, st.get("sentAt"), p["id"]))
    if st["status"] == "error":
        db.log("monitor", f"{p['service']} post for clip #{p['clip_id']} failed: {err}", "error")
    elif st["status"] == "sent":
        db.log("monitor", f"{p['service']} post for clip #{p['clip_id']} is live: {st.get('externalLink')}")
    return True


def seconds_to_next_due() -> float:
    """Seconds until 4 minutes after the next scheduled post's time (when Buffer has usually published it)."""
    r = db.one("""SELECT EXTRACT(EPOCH FROM (min(due_at) + interval '4 minutes' - now())) AS s FROM posts
                  WHERE status NOT IN ('sent','error','deleted','draft') AND due_at > now() - interval '3 minutes'""")
    return float(r["s"]) if r and r["s"] is not None else 10 * 60


def run():
    if buffer.paused_until() > time.time():
        return 0  # waiting for Buffer's rate-limit window to reset
    due = db.q("""SELECT * FROM posts WHERE due_at < now() - interval '3 minutes'
                  AND (status NOT IN ('sent','error','deleted','draft') OR (status='sent' AND external_link IS NULL))""")
    s = db.settings()
    stats_due = time.time() - float(s.get("stats_at") or 0) > STATS_EVERY and \
        db.one("SELECT 1 AS x FROM posts WHERE status='sent' AND sent_at > now() - interval '30 days' LIMIT 1")
    sync_due = time.time() - float(s.get("synced_at") or 0) > SYNC_EVERY and \
        db.one("SELECT 1 AS x FROM posts WHERE status NOT IN ('sent','error','deleted','draft') LIMIT 1")
    changed = 0
    if due or stats_due or sync_due:
        chans = [c["id"] for c in buffer.channels()]
        want_stats = bool(stats_due) and not s.get("stats_scope_missing")
        try:
            nodes = buffer.recent_posts(chans, ["sent", "error"], metrics=want_stats)
        except buffer.BufferError as e:
            if want_stats and any(w in str(e).lower() for w in ("insight", "scope", "forbidden", "metrics")):
                db.set_setting("stats_scope_missing", True)
                db.log("monitor", "Buffer key lacks insightsRead, so Buffer views are not tracked. Make a key with that permission ticked.", "warn")
                nodes = buffer.recent_posts(chans, ["sent", "error"])
                want_stats = False
            else:
                raise
        by_id = {n["id"]: n for n in nodes}
        db.set_setting("synced_at", time.time())
        # every post of ours Buffer reports as sent or failed, due or not (a post published early with "Publish Now")
        for p in db.q("SELECT * FROM posts WHERE buffer_post_id = ANY(%s)", (list(by_id),)):
            changed += _apply(p, by_id[p["buffer_post_id"]])
        # a due post missing from the list (deleted in Buffer, or still sending): ask for it alone, a few at most
        late = [p for p in due if p["buffer_post_id"] not in by_id
                and p["due_at"] < datetime.now(timezone.utc) - timedelta(hours=2)]
        for p in late[:3]:
            st = buffer.post_status(p["buffer_post_id"])
            if not st:
                db.q("UPDATE posts SET status='deleted', updated_at=now() WHERE id=%s", (p["id"],))
            else:
                changed += _apply(p, st)
        if want_stats:
            now = datetime.now(timezone.utc)
            for p in db.q("SELECT * FROM posts WHERE status='sent' AND buffer_post_id = ANY(%s)", (list(by_id),)):
                vals = _pick(by_id[p["buffer_post_id"]].get("metrics"))
                if not vals:
                    continue
                db.q("""UPDATE posts SET views=%s, likes=%s, comments=%s, shares=%s, saves=%s, metrics_at=%s WHERE id=%s""",
                     (vals.get("views", p["views"]), vals.get("likes", p["likes"]), vals.get("comments", p["comments"]),
                      vals.get("shares", p["shares"]), vals.get("saves", p["saves"]), now, p["id"]))
                db.q("INSERT INTO metrics(post_id, views, likes, comments, shares, saves) VALUES (%s,%s,%s,%s,%s,%s)",
                     (p["id"], vals.get("views"), vals.get("likes"), vals.get("comments"), vals.get("shares"), vals.get("saves")))
        if stats_due:
            got = sum(1 for n in nodes if _pick(n.get("metrics"))) if want_stats else 0
            db.log("monitor", f"stats sync: Buffer returned stats for {got} post(s)"
                   + ("" if want_stats else " (off: the key lacks insightsRead)"))
            db.set_setting("stats_at", time.time())

    # YouTube's own API costs no Buffer requests, so its counts refresh hourly
    if config.YOUTUBE_API_KEY and time.time() - float(s.get("yt_stats_at") or 0) > YT_EVERY:
        db.set_setting("yt_stats_at", time.time())
        youtube_stats()

    notify.live_posts()
    notify.failed_posts()
    db.q("""UPDATE clips SET status='posted' WHERE status='scheduled' AND NOT EXISTS (
              SELECT 1 FROM posts p WHERE p.clip_id = clips.id AND p.status NOT IN ('sent','error','deleted','draft'))""")
    return changed
