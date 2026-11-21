"""Posting calendar: when each upload is due, shown in Tbilisi time.

Manual accounts: every reminder already emailed (manual_uploads) plus a forecast of the coming slots, using the same
slots, daily limits and clip choice as workers/manual.py. The clip of a future slot is only "likely": the worker picks
the freshest clip 10 minutes before the slot, so a newer clip can take its place. Automatic accounts: the Buffer posts
in the range, for context. Slots are read from settings.slots_et, never hard-coded here.
"""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

from . import db, notify
from .workers import manual
from .workers.publish import ET, day_cap

router = APIRouter()

TBILISI = ZoneInfo("Asia/Tbilisi")
FORECAST_CLIPS = timedelta(hours=48)  # past this the clip pool will have changed: show the slot without a clip


def _dl(url: str | None) -> str | None:
    return notify._download(url) if url else None


def _local(t: datetime) -> dict:
    lt = t.astimezone(TBILISI)
    return {"at": t.isoformat(), "day": lt.date().isoformat(), "time": lt.strftime("%H:%M"),
            "time_et": t.astimezone(ET).strftime("%H:%M")}


def _clip(c: dict | None) -> dict:
    if not c:
        return {"clip_id": None, "title": None, "caption": None, "thumb_url": None, "video_url": None, "download_url": None}
    return {"clip_id": c["id"], "title": c.get("title"), "caption": c.get("caption"), "thumb_url": c.get("thumb_url"),
            "video_url": c.get("video_url"), "download_url": _dl(c.get("video_url"))}


def _slots(s: dict, lo: datetime, hi: datetime) -> list[datetime]:
    out = []
    d = lo.astimezone(ET).date() - timedelta(days=1)
    while d <= hi.astimezone(ET).date() + timedelta(days=1):
        for hhmm in sorted(s.get("slots_et") or ["19:00"]):
            h, mi = map(int, hhmm.split(":"))
            t = datetime(d.year, d.month, d.day, h, mi, tzinfo=ET).astimezone(timezone.utc)
            if lo <= t < hi:
                out.append(t)
        d += timedelta(days=1)
    return sorted(out)


def _account(a: dict) -> dict:
    return {"account_id": a["id"], "platform": a["platform"], "handle": a["handle"], "profile_url": a.get("profile_url"),
            "upload_page": notify.UPLOAD_PAGE.get(a["platform"])}


def build(start: date, days: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    s = db.settings()
    btz = ZoneInfo(s.get("buffer_timezone") or "Asia/Tbilisi")
    lo = datetime(start.year, start.month, start.day, tzinfo=TBILISI).astimezone(timezone.utc)
    hi = lo + timedelta(days=days)
    events: list[dict] = []

    # reminders already emailed (or sent with "upload now")
    for r in db.q("""SELECT mu.*, a.platform, a.handle, a.profile_url, c.title, c.caption, c.thumb_url, c.video_url
                     FROM manual_uploads mu JOIN accounts a ON a.id = mu.account_id JOIN clips c ON c.id = mu.clip_id
                     WHERE mu.slot_at >= %s AND mu.slot_at < %s ORDER BY mu.slot_at""", (lo, hi)):
        status = "posted" if r["link"] else "done" if r["done_at"] else "to_upload"
        events.append({"kind": "manual", "status": status, "upload_id": r["id"], "link": r["link"],
                       **_local(r["slot_at"]), **_account({**r, "id": r["account_id"]}),
                       **_clip({**r, "id": r["clip_id"]})})

    # forecast: the coming slots of every live manual account, with the clip it would most likely get
    for a in manual.manual_accounts():
        rows = db.q("SELECT clip_id, slot_at FROM manual_uploads WHERE account_id=%s AND slot_at > %s - interval '2 days'",
                    (a["id"], max(lo, now)))
        taken = {r["slot_at"] for r in rows}
        per_day: dict[date, int] = {}
        for r in rows:
            k = r["slot_at"].astimezone(btz).date()
            per_day[k] = per_day.get(k, 0) + 1
        planned: list[int] = []
        cap = day_cap(a["platform"], s)
        for t in _slots(s, max(lo, now - manual.WINDOW), hi):
            if t in taken or t + manual.WINDOW < now:
                continue
            k = t.astimezone(btz).date()
            if per_day.get(k, 0) >= cap:
                continue
            per_day[k] = per_day.get(k, 0) + 1
            clip = manual.pick_clip(a, planned) if t - now < FORECAST_CLIPS else None
            if clip:
                planned.append(clip["id"])
            events.append({"kind": "manual", "status": "planned", "upload_id": None, "link": None,
                           **_local(t), **_account(a), **_clip(clip)})

    # automatic posts through Buffer (uploads recorded by hand are already listed above)
    for p in db.q("""SELECT p.*, c.title, c.caption, c.thumb_url, c.video_url FROM posts p JOIN clips c ON c.id = p.clip_id
                     WHERE p.buffer_post_id IS NOT NULL AND p.status NOT IN ('deleted','draft')
                       AND COALESCE(p.sent_at, p.due_at) >= %s AND COALESCE(p.sent_at, p.due_at) < %s""", (lo, hi)):
        events.append({"kind": "automatic", "status": p["status"], "post_id": p["id"], "link": p.get("external_link"),
                       "platform": p["service"], "handle": p.get("channel_name"), "account_id": None,
                       "profile_url": None, "upload_page": None, "error": p.get("error"),
                       **_local(p["sent_at"] or p["due_at"]), **_clip({**p, "id": p["clip_id"]})})

    events.sort(key=lambda e: (e["at"], e["kind"] != "manual", e["platform"] or ""))
    today = now.astimezone(TBILISI).date()
    slot_local = sorted({_local(t)["time"] for t in _slots(s, datetime.combine(today, datetime.min.time(), TBILISI),
                                                            datetime.combine(today, datetime.min.time(), TBILISI) + timedelta(days=1))})
    return {"timezone": "Asia/Tbilisi", "today": today.isoformat(), "start": start.isoformat(), "days": days,
            "now": now.isoformat(), "slots_et": sorted(s.get("slots_et") or []), "slots_local": slot_local,
            "email_ready": notify.enabled(), "manual_accounts": len(manual.manual_accounts()), "events": events}


@router.get("/calendar")
def calendar(start: str | None = None, days: int = 7):
    if not 1 <= days <= 42:
        raise HTTPException(400, "days must be 1 to 42")
    try:
        d = date.fromisoformat(start) if start else datetime.now(TBILISI).date()
    except ValueError:
        raise HTTPException(400, "start must be YYYY-MM-DD")
    return build(d, days)
