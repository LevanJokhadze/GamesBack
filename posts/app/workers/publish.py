"""Publish: schedule approved clips in Buffer at the next free US-Eastern slot on every channel.

Each channel is planned on its own (YouTube via Buffer allows fewer posts a day than TikTok/Instagram), so a clip
can go out on TikTok tonight and on Shorts tomorrow. A channel that refuses a post is retried on the next run,
at that channel's next free slot.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db
from ..integrations import buffer

ET = ZoneInfo("America/New_York")


def day_cap(service: str, s: dict) -> int:
    caps = s.get("max_per_day_by_service") or {}
    return int(caps.get(service) or s.get("max_posts_per_day") or 3)


def next_slot(channel_id: str, service: str, s: dict, after: datetime) -> datetime:
    slots = sorted(s.get("slots_et") or ["19:00"])
    taken = [r["due_at"] for r in db.q("""SELECT due_at FROM posts WHERE channel_id=%s AND status <> 'deleted'
                                          AND due_at > now() - interval '2 days'""", (channel_id,))]
    cap = day_cap(service, s)
    day = after.astimezone(ET).date()
    for _ in range(30):
        if sum(1 for x in taken if x.astimezone(ET).date() == day) < cap:
            for hhmm in slots:
                h, mi = map(int, hhmm.split(":"))
                t = datetime(day.year, day.month, day.day, h, mi, tzinfo=ET).astimezone(timezone.utc)
                if t > after and not any(abs((x - t).total_seconds()) < 3600 for x in taken):
                    return t
        day += timedelta(days=1)
    raise RuntimeError("no free slot in the next 30 days")


def run():
    s = db.settings()
    wanted = s.get("services") or ["tiktok", "instagram", "youtube"]
    clips = db.q("""SELECT * FROM clips WHERE status IN ('approved','scheduled') AND approved_at > now() - interval '4 days'
                    ORDER BY approved_at LIMIT 20""")
    if not clips:
        return 0
    order = {svc: i for i, svc in enumerate(wanted)}
    chans = sorted([ch for ch in buffer.channels() if ch["service"] in wanted and not ch.get("isQueuePaused")],
                   key=lambda ch: order.get(ch["service"], 99))
    stagger = int(s.get("stagger_minutes") or 15)
    n = 0
    for clip in clips:
        have = {r["channel_id"] for r in db.q("SELECT channel_id FROM posts WHERE clip_id=%s AND status <> 'deleted'",
                                              (clip["id"],))}
        missing = [ch for ch in chans if ch["id"] not in have]
        if not missing:
            continue
        earliest = datetime.now(timezone.utc) + timedelta(minutes=20)
        planned = []
        for ch in missing:
            i = order.get(ch["service"], 0)
            # same slot as the platforms before it, plus the stagger, so TikTok -> Reels -> Shorts
            due = next_slot(ch["id"], ch["service"], s, earliest) + timedelta(minutes=stagger * i)
            try:
                p = buffer.create_video_post(ch, clip["video_url"], clip["title"] or "", clip["caption"] or "",
                                             due.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
            except Exception as e:
                db.log("publish", f"clip #{clip['id']} on {ch['service']}: {e} (will retry at a later slot)", "warn")
                continue
            db.q("""INSERT INTO posts(clip_id, campaign_id, channel_id, channel_name, service, buffer_post_id, due_at, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                 (clip["id"], clip["campaign_id"], ch["id"], ch["name"], ch["service"], p["id"], p["dueAt"], p["status"]))
            planned.append(f"{ch['service']} {due.astimezone(ET):%a %H:%M}")
            n += 1
        if planned:
            db.q("UPDATE clips SET status='scheduled' WHERE id=%s AND status='approved'", (clip["id"],))
            db.log("publish", f"clip #{clip['id']} scheduled (ET): " + ", ".join(planned))
    return n
