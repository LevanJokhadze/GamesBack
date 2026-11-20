"""Monitor: follow every Buffer post until it is sent (link) or fails, and record views/likes over time."""
from datetime import datetime, timezone

from .. import db, notify
from ..integrations import buffer

FIELDS = {"views": ("view", "views", "plays", "video_views", "impressions"),
          "likes": ("like", "likes", "reactions"),
          "comments": ("comment", "comments"),
          "shares": ("share", "shares", "reposts"),
          "saves": ("save", "saves", "favorites")}

_scope_warned = False


def _pick(metrics: list[dict]) -> dict:
    out = {}
    for m in metrics:
        key = f"{m.get('type') or ''} {m.get('name') or ''}".lower()
        for f, names in FIELDS.items():
            if f not in out and any(n in key.split() or key.endswith(n) for n in names):
                out[f] = int(m.get("value") or 0)
    return out


def run():
    global _scope_warned
    changed = 0
    for p in db.q("""SELECT * FROM posts WHERE status NOT IN ('sent','error') OR (status='sent' AND external_link IS NULL)
                     ORDER BY due_at LIMIT 100"""):
        try:
            st = buffer.post_status(p["buffer_post_id"])
        except Exception as e:
            db.log("monitor", f"post {p['id']}: {e}", "warn")
            continue
        if not st:
            db.q("UPDATE posts SET status='deleted', updated_at=now() WHERE id=%s", (p["id"],))
            continue
        err = (st.get("error") or {}).get("message")
        if st["status"] != p["status"] or st.get("externalLink") != p["external_link"] or err != p["error"]:
            db.q("""UPDATE posts SET status=%s, external_link=%s, error=%s, sent_at=%s, due_at=%s, updated_at=now() WHERE id=%s""",
                 (st["status"], st.get("externalLink"), err, st.get("sentAt"), st.get("dueAt") or p["due_at"], p["id"]))
            changed += 1
            if st["status"] == "error":
                db.log("monitor", f"{p['service']} post for clip #{p['clip_id']} failed: {err}", "error")
            elif st["status"] == "sent":
                db.log("monitor", f"{p['service']} post for clip #{p['clip_id']} is live: {st.get('externalLink')}")
    notify.live_posts()
    notify.failed_posts()
    db.q("""UPDATE clips SET status='posted' WHERE status='scheduled' AND NOT EXISTS (
              SELECT 1 FROM posts p WHERE p.clip_id = clips.id AND p.status NOT IN ('sent','error','deleted'))""")

    # metrics for posts sent in the last 30 days, at most every 2 hours each
    for p in db.q("""SELECT * FROM posts WHERE status='sent' AND sent_at > now() - interval '30 days'
                     AND (metrics_at IS NULL OR metrics_at < now() - interval '2 hours') LIMIT 50"""):
        try:
            vals = _pick(buffer.post_metrics(p["buffer_post_id"]))
        except Exception as e:
            if "insights:read" in str(e):
                if not _scope_warned:
                    db.log("monitor", "Buffer key lacks insights:read, so views are not tracked. Make a key with it.", "warn")
                    _scope_warned = True
                break
            db.log("monitor", f"metrics for post {p['id']}: {e}", "warn")
            continue
        db.q("""UPDATE posts SET views=%s, likes=%s, comments=%s, shares=%s, saves=%s, metrics_at=%s WHERE id=%s""",
             (vals.get("views", p["views"]), vals.get("likes", p["likes"]), vals.get("comments", p["comments"]),
              vals.get("shares", p["shares"]), vals.get("saves", p["saves"]), datetime.now(timezone.utc), p["id"]))
        db.q("INSERT INTO metrics(post_id, views, likes, comments, shares, saves) VALUES (%s,%s,%s,%s,%s,%s)",
             (p["id"], vals.get("views"), vals.get("likes"), vals.get("comments"), vals.get("shares"), vals.get("saves")))
    return changed
