"""Manual uploads: accounts in 'manual' mode get an email at each posting slot instead of a Buffer post.

New accounts start manual so they can warm up with hand uploads. At each US-Eastern slot (the same slots and daily
limits as the publish worker) every manual account that is warming or active gets one email: the clip's download
link, its caption with the campaign hashtags, the platform and the username. A clip is never sent twice to the same
account. After 7 days in manual mode one more email suggests switching the account to automatic on the dashboard;
the switch itself is always the user's.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .. import db, notify
from .publish import ET, day_cap, slot_plan, zone_label

LEAD = timedelta(minutes=10)     # email this long before the slot, so there's time to download
WINDOW = timedelta(minutes=50)   # a slot missed by more than this (worker paused, deploy) is skipped
WEEK = timedelta(days=7)
LIVE_STATUSES = ("warming", "active")


def geo_of(account: dict) -> str | None:
    """The account's geo code (RU, US, ...): on the row when the query joined geos, else looked up."""
    code = account.get("geo") or account.get("geo_code")
    if not code and account.get("geo_id"):
        row = db.one("SELECT code FROM geos WHERE id=%s", (account["geo_id"],))
        code = row["code"] if row else None
    return code.upper() if code else None


def slot_times(s: dict, around: datetime, geo: str | None = None) -> list[datetime]:
    """Slot times (UTC) from yesterday to the day after tomorrow, in the geo's own timezone (default US Eastern)."""
    tz, slots = slot_plan(s, geo)
    out = []
    d0 = around.astimezone(tz).date()
    for dd in (-1, 0, 1, 2):
        d = d0 + timedelta(days=dd)
        for hhmm in slots:
            h, mi = map(int, hhmm.split(":"))
            out.append(datetime(d.year, d.month, d.day, h, mi, tzinfo=tz).astimezone(timezone.utc))
    return sorted(out)


def _day_count(account_id: int, t: datetime, btz: ZoneInfo) -> int:
    rows = db.q("SELECT slot_at FROM manual_uploads WHERE account_id=%s AND slot_at > %s - interval '2 days'",
                (account_id, t))
    return sum(1 for r in rows if r["slot_at"].astimezone(btz).date() == t.astimezone(btz).date())


def next_slot(account: dict, s: dict, now: datetime | None = None) -> datetime | None:
    """When this account's next reminder is due (shown on the dashboard)."""
    now = now or datetime.now(timezone.utc)
    btz = ZoneInfo(s.get("buffer_timezone") or "Asia/Tbilisi")
    done = {r["slot_at"] for r in db.q("SELECT slot_at FROM manual_uploads WHERE account_id=%s AND slot_at > now() - interval '2 days'",
                                       (account["id"],))}
    for t in slot_times(s, now, geo_of(account)):
        if t + WINDOW < now or t in done:
            continue
        if _day_count(account["id"], t, btz) >= day_cap(account["platform"], s, geo_of(account)):
            continue
        return t
    return None


def pick_clip(account: dict, exclude: list[int] | None = None) -> dict | None:
    """The freshest approved clip this account hasn't been given, preferring clips not yet on its platform.
    `exclude` leaves out clips already planned for earlier slots (the calendar's forecast)."""
    streamers = [x.lower() for x in account.get("streamers") or []]
    return db.one("""
        SELECT c.*, ca.name AS campaign, ca.url AS campaign_url FROM clips c LEFT JOIN campaigns ca ON ca.id = c.campaign_id
        WHERE c.status IN ('approved','scheduled','posted') AND c.video_url IS NOT NULL AND COALESCE(ca.active, TRUE)
          AND c.approved_at > now() - interval '4 days'
          AND NOT (c.id = ANY(%(ex)s::int[]))
          AND NOT EXISTS (SELECT 1 FROM manual_uploads mu WHERE mu.account_id = %(a)s AND mu.clip_id = c.id)
          AND NOT EXISTS (SELECT 1 FROM posts p WHERE p.clip_id = c.id AND p.status <> 'deleted'
                          AND p.channel_id IN (%(ch)s, %(mch)s))
          AND (cardinality(%(st)s::text[]) = 0
               OR EXISTS (SELECT 1 FROM unnest(ca.channels) x WHERE lower(x) = ANY(%(st)s::text[])))
          -- a campaign for a geo (e.g. RU) goes only to that geo's accounts; one without a geo goes to the rest
          AND CASE WHEN NULLIF(ca.geo, '') IS NOT NULL
                   THEN upper(ca.geo) = (SELECT upper(code) FROM geos WHERE id = %(g)s)
                   ELSE NOT EXISTS (SELECT 1 FROM campaigns x JOIN geos g ON upper(g.code) = upper(x.geo)
                                    WHERE g.id = %(g)s) END
        ORDER BY
          (SELECT count(*) FROM manual_uploads mu JOIN accounts a ON a.id = mu.account_id
             WHERE mu.clip_id = c.id AND a.platform = %(pf)s)
          + (SELECT count(*) FROM posts p WHERE p.clip_id = c.id AND p.service = %(pf)s AND p.status <> 'deleted'),
          c.approved_at DESC
        LIMIT 1""", {"a": account["id"], "ch": account.get("buffer_channel_id") or "", "mch": f"manual:{account['id']}",
                     "st": streamers, "pf": account["platform"], "ex": list(exclude or []),
                     "g": account.get("geo_id")})


def send_one(account: dict, slot: datetime) -> dict | None:
    """Give this account its next clip for `slot` and email it. Returns the manual_uploads row, or None."""
    clip = pick_clip(account)
    if not clip:
        return None
    row = db.one("""INSERT INTO manual_uploads(account_id, clip_id, slot_at) VALUES (%s,%s,%s)
                    ON CONFLICT (account_id, clip_id) DO NOTHING RETURNING *""", (account["id"], clip["id"], slot))
    if not row:
        return None
    tz, _ = slot_plan(db.settings(), geo_of(account))
    try:
        notify.manual_upload(account, clip, slot, tz.key)
    except Exception as e:
        db.q("DELETE FROM manual_uploads WHERE id=%s", (row["id"],))  # try again on the next run
        db.log("manual", f"upload reminder for {account['platform']} @{account['handle']} failed: {e}", "error")
        raise
    db.q("UPDATE manual_uploads SET emailed_at=now() WHERE id=%s", (row["id"],))
    db.log("manual", f"emailed: upload clip #{clip['id']} to {account['platform']} @{account['handle']} "
                     f"at {slot.astimezone(tz):%a %H:%M} {zone_label(tz)}")
    return row


def manual_accounts() -> list[dict]:
    return db.q("""SELECT a.*, g.code AS geo_code FROM accounts a LEFT JOIN geos g ON g.id = a.geo_id
                   WHERE a.upload_mode='manual' AND a.status = ANY(%s) ORDER BY a.id""", (list(LIVE_STATUSES),))


def run():
    if not notify.enabled():
        return 0
    s = db.settings()
    btz = ZoneInfo(s.get("buffer_timezone") or "Asia/Tbilisi")
    now = datetime.now(timezone.utc)
    sent = 0
    for a in manual_accounts():
        for t in slot_times(s, now, geo_of(a)):
            if not (t - LEAD <= now < t + WINDOW):
                continue
            if db.one("SELECT 1 AS x FROM manual_uploads WHERE account_id=%s AND slot_at=%s", (a["id"], t)):
                continue
            if _day_count(a["id"], t, btz) >= day_cap(a["platform"], s, geo_of(a)):
                continue
            try:
                if send_one(a, t):
                    sent += 1
            except Exception:
                return sent  # mail is down: stop, retry on the next run
        if a["manual_since"] and now - a["manual_since"] >= WEEK and not a["week_hint_at"]:
            try:
                notify.manual_week(a)
            except Exception as e:
                db.log("manual", f"one-week email for @{a['handle']} failed: {e}", "error")
                continue
            db.q("UPDATE accounts SET week_hint_at=now() WHERE id=%s", (a["id"],))
            db.log("manual", f"{a['platform']} @{a['handle']} has had 1 week of manual uploads: suggested automatic")
    return sent
