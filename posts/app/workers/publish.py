"""Publish: schedule approved clips in Buffer at the next free US-Eastern slot on every channel.

Channels linked to an account in manual mode are skipped: those get upload emails from workers/manual.py.

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


def min_gap(s: dict) -> int:
    """Seconds that must separate two posts on one account (spaces out same-streamer posts)."""
    return int(float(s.get("min_gap_minutes") or 60) * 60)


def next_slot(channel_id: str, service: str, s: dict, after: datetime) -> datetime:
    """Next posting time from the US-Eastern slots that is at least `min_gap_minutes` from any other post on this
    channel and whose day still has room. Days are counted in Buffer's own timezone, the one its daily limits use."""
    slots = sorted(s.get("slots_et") or ["19:00"])
    taken = [r["due_at"] for r in db.q("""SELECT due_at FROM posts WHERE channel_id=%s AND status <> 'deleted'
                                          AND due_at > now() - interval '2 days'""", (channel_id,))]
    cap = day_cap(service, s)
    gap = min_gap(s)
    btz = ZoneInfo(s.get("buffer_timezone") or "Asia/Tbilisi")
    day = after.astimezone(ET).date()
    for _ in range(30):
        for hhmm in slots:
            h, mi = map(int, hhmm.split(":"))
            t = datetime(day.year, day.month, day.day, h, mi, tzinfo=ET).astimezone(timezone.utc)
            if t <= after or any(abs((x - t).total_seconds()) < gap for x in taken):
                continue
            if sum(1 for x in taken if x.astimezone(btz).date() == t.astimezone(btz).date()) >= cap:
                continue
            return t
        day += timedelta(days=1)
    raise RuntimeError("no free slot in the next 30 days")


RETRY_AFTER = 6 * 3600  # a channel that refused a clip is not asked again for this long


def run():
    import time
    if buffer.paused_until() > time.time():
        return 0
    s = db.settings()
    refused = {k: v for k, v in (s.get("publish_refused") or {}).items() if time.time() - v < RETRY_AFTER}
    wanted = s.get("services") or ["tiktok", "instagram", "youtube"]
    # only Buffer posts on the enabled platforms count here: a clip uploaded by hand somewhere still goes out on the rest
    # only clips of active campaigns: an ended campaign's leftover clips would use up the day's few posting slots
    clips = db.q("""SELECT c.* FROM clips c JOIN campaigns ca ON ca.id = c.campaign_id AND ca.active
                    WHERE c.status IN ('approved','scheduled') AND c.approved_at > now() - interval '4 days'
                    AND (SELECT count(*) FROM posts p WHERE p.clip_id = c.id AND p.status <> 'deleted'
                         AND p.buffer_post_id IS NOT NULL AND p.service = ANY(%s)) < %s
                    ORDER BY c.approved_at LIMIT 20""", (wanted, len(wanted)))
    if not clips:
        return 0  # every clip already has a post on every platform: no Buffer call needed
    order = {svc: i for i, svc in enumerate(wanted)}
    # channels of accounts in manual mode get upload emails instead (workers/manual.py), never Buffer posts
    manual = {r["buffer_channel_id"] for r in db.q("""SELECT buffer_channel_id FROM accounts
                                                     WHERE upload_mode='manual' AND buffer_channel_id IS NOT NULL""")}
    chans = sorted([ch for ch in buffer.channels() if ch["service"] in wanted and not ch.get("isQueuePaused")
                    and ch["id"] not in manual],
                   key=lambda ch: order.get(ch["service"], 99))
    stagger = int(s.get("stagger_minutes") or 15)
    n = 0
    # Buffer's free plan holds at most 10 queued posts per channel at a time
    cap_q = int(s.get("max_queued_per_channel") or 10)
    queued = {r["channel_id"]: r["n"] for r in db.q("""SELECT channel_id, count(*) AS n FROM posts
                                                     WHERE status NOT IN ('sent','error','deleted','draft') GROUP BY channel_id""")}
    for clip in clips:
        have = {r["channel_id"] for r in db.q("SELECT channel_id FROM posts WHERE clip_id=%s AND status <> 'deleted'",
                                              (clip["id"],))}
        # a clip already handed to an account by email is never posted to that account's channel again
        have |= {r["buffer_channel_id"] for r in db.q("""SELECT a.buffer_channel_id FROM manual_uploads mu
                                                         JOIN accounts a ON a.id = mu.account_id
                                                         WHERE mu.clip_id=%s AND a.buffer_channel_id IS NOT NULL""",
                                                      (clip["id"],))}
        missing = [ch for ch in chans if ch["id"] not in have and f"{clip['id']}:{ch['id']}" not in refused
                   and queued.get(ch["id"], 0) < cap_q]
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
            except buffer.RateLimited:
                raise
            except Exception as e:
                refused[f"{clip['id']}:{ch['id']}"] = time.time()
                db.set_setting("publish_refused", refused)
                db.log("publish", f"clip #{clip['id']} on {ch['service']}: {e} (will try again in 6 h)", "warn")
                continue
            db.q("""INSERT INTO posts(clip_id, campaign_id, channel_id, channel_name, service, buffer_post_id, due_at, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                 (clip["id"], clip["campaign_id"], ch["id"], ch["name"], ch["service"], p["id"], p["dueAt"], p["status"]))
            queued[ch["id"]] = queued.get(ch["id"], 0) + 1
            planned.append(f"{ch['service']} {due.astimezone(ET):%a %H:%M}")
            n += 1
        if planned:
            db.q("UPDATE clips SET status='scheduled' WHERE id=%s AND status='approved'", (clip["id"],))
            db.log("publish", f"clip #{clip['id']} scheduled (ET): " + ", ".join(planned))
    return n
