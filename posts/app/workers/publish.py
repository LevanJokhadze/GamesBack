"""Publish: schedule approved clips in Buffer at the next free US-Eastern slot on every channel."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db
from ..integrations import buffer

ET = ZoneInfo("America/New_York")


def next_slot(channel_id: str, s: dict, after: datetime) -> datetime:
    slots = sorted(s.get("slots_et") or ["19:00"])
    taken = {r["due_at"] for r in db.q("SELECT due_at FROM posts WHERE channel_id=%s AND due_at > now() - interval '2 days'",
                                       (channel_id,))}
    day = after.astimezone(ET).date()
    for _ in range(30):
        per_day = 0
        for hhmm in slots:
            h, mi = map(int, hhmm.split(":"))
            t = datetime(day.year, day.month, day.day, h, mi, tzinfo=ET).astimezone(timezone.utc)
            day_posts = [x for x in taken if x.astimezone(ET).date() == day]
            if len(day_posts) >= int(s.get("max_posts_per_day") or 3):
                break
            if t <= after or any(abs((x - t).total_seconds()) < 3600 for x in taken):
                continue
            return t
        day += timedelta(days=1)
    raise RuntimeError("no free slot in the next 30 days")


def run():
    s = db.settings()
    clips = db.q("""SELECT * FROM clips WHERE status='approved'
                    AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.clip_id = clips.id) ORDER BY approved_at LIMIT 5""")
    if not clips:
        return 0
    wanted = s.get("services") or ["tiktok", "instagram", "youtube"]
    chans = [ch for ch in buffer.channels() if ch["service"] in wanted and not ch.get("isQueuePaused")]
    order = {svc: i for i, svc in enumerate(wanted)}
    chans.sort(key=lambda ch: order.get(ch["service"], 99))
    n = 0
    for clip in clips:
        earliest = datetime.now(timezone.utc) + timedelta(minutes=20)
        base = max(next_slot(ch["id"], s, earliest) for ch in chans) if chans else None
        for i, ch in enumerate(chans):
            due = base + timedelta(minutes=int(s.get("stagger_minutes") or 15) * i)
            try:
                p = buffer.create_video_post(ch, clip["video_url"], clip["title"] or "", clip["caption"] or "",
                                             due.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
                db.q("""INSERT INTO posts(clip_id, campaign_id, channel_id, channel_name, service, buffer_post_id, due_at, status)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                     (clip["id"], clip["campaign_id"], ch["id"], ch["name"], ch["service"], p["id"], p["dueAt"], p["status"]))
                n += 1
            except Exception as e:
                db.log("publish", f"clip #{clip['id']} on {ch['service']}: {e}", "error")
        db.q("UPDATE clips SET status='scheduled' WHERE id=%s", (clip["id"],))
        db.log("publish", f"clip #{clip['id']} scheduled for {base.astimezone(ET):%a %b %d %H:%M} ET on {len(chans)} channels")
    return n
