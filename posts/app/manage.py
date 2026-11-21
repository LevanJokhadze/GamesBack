"""Removing and changing things from the dashboard (and from Jarvis): posts, clips, whole campaigns, schedules.

Removing a post takes it out of Buffer when it hasn't gone out yet, then off the dashboard (calendar, posts, cards,
totals). A post that is already live on TikTok/Instagram/YouTube can't be taken down through Buffer, so it is removed
from the dashboard anyway and its link is returned, for deleting it on the app by hand.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import db
from .integrations import buffer

router = APIRouter()
QUEUED = ("sent", "error", "deleted", "draft")  # statuses that are NOT waiting in Buffer


def _cancel_in_buffer(p: dict) -> str:
    """'cancelled' | 'gone' | 'live' | 'none' | 'failed: ...'"""
    if not p.get("buffer_post_id"):
        return "live" if p.get("external_link") or p.get("status") == "sent" else "none"
    if p.get("status") == "sent":
        return "live"
    try:
        buffer.delete_post(p["buffer_post_id"])
        return "cancelled" if p.get("status") not in QUEUED else "gone"
    except buffer.BufferError as e:
        return f"failed: {e}"[:300]


def _remove_posts(rows: list[dict], who: str) -> dict:
    out = {"removed": 0, "cancelled_in_buffer": 0, "still_live": [], "buffer_failed": []}
    for p in rows:
        res = _cancel_in_buffer(p)
        if res.startswith("failed"):
            # keep it: deleting the row would leave a post going out that the dashboard no longer shows
            out["buffer_failed"].append({"post_id": p["id"], "service": p.get("service"), "error": res})
            continue
        if res == "cancelled":
            out["cancelled_in_buffer"] += 1
        if res == "live":
            out["still_live"].append({"service": p.get("service"), "channel": p.get("channel_name"),
                                      "link": p.get("external_link")})
            db.log(who, f"post #{p['id']} removed from the dashboard; still live on {p.get('service')}: "
                        f"{p.get('external_link') or 'no link'} (delete it on the app)")
        db.q("DELETE FROM posts WHERE id=%s", (p["id"],))
        out["removed"] += 1
    return out


def remove_post(post_id: int, who: str = "dashboard") -> dict:
    p = db.one("SELECT * FROM posts WHERE id=%s", (post_id,))
    if not p:
        raise LookupError("post not found")
    out = _remove_posts([p], who)
    if out["removed"]:
        # so the publisher doesn't put the same clip straight back on that platform
        left = db.one("SELECT count(*) AS n FROM posts WHERE clip_id=%s AND status <> 'deleted'", (p["clip_id"],))["n"]
        db.q("UPDATE clips SET status=%s WHERE id=%s AND status IN ('approved','scheduled')",
             ("posted" if left else "rejected", p["clip_id"]))
        db.log(who, f"post #{post_id} ({p.get('service')}) removed" +
               (" and cancelled in Buffer" if out["cancelled_in_buffer"] else ""))
    return out


def remove_clip(clip_id: int, who: str = "dashboard") -> dict:
    c = db.one("SELECT id, title FROM clips WHERE id=%s", (clip_id,))
    if not c:
        raise LookupError("clip not found")
    out = _remove_posts(db.q("SELECT * FROM posts WHERE clip_id=%s", (clip_id,)), who)
    if out["buffer_failed"]:
        db.q("UPDATE clips SET status='rejected' WHERE id=%s", (clip_id,))
        return {**out, "clip_removed": False}
    db.q("DELETE FROM clips WHERE id=%s", (clip_id,))  # its manual uploads go with it
    db.log(who, f"clip #{clip_id} removed: {c['title']}")
    return {**out, "clip_removed": True}


def remove_campaign(campaign_id: int, who: str = "dashboard") -> dict:
    """The campaign, its moments, clips, posts and upload reminders all go; accounts stop targeting its streamers."""
    ca = db.one("SELECT id, name, channels FROM campaigns WHERE id=%s", (campaign_id,))
    if not ca:
        raise LookupError("campaign not found")
    db.q("UPDATE campaigns SET active=FALSE, ended_at=COALESCE(ended_at, now()) WHERE id=%s", (campaign_id,))
    out = _remove_posts(db.q("SELECT * FROM posts WHERE campaign_id=%s OR clip_id IN "
                             "(SELECT id FROM clips WHERE campaign_id=%s)", (campaign_id, campaign_id)), who)
    if out["buffer_failed"]:
        db.log(who, f"campaign {ca['name']}: {len(out['buffer_failed'])} post(s) couldn't be cancelled in Buffer; "
                    "campaign kept (inactive) until they are", "warn")
        return {**out, "campaign_removed": False}
    chans = [c.lower() for c in (ca["channels"] or [])]
    still = {c.lower() for r in db.q("SELECT channels FROM campaigns WHERE id <> %s", (campaign_id,))
             for c in (r["channels"] or [])}
    for ch in chans:
        if ch not in still:
            db.q("""UPDATE accounts SET streamers = ARRAY(SELECT s FROM unnest(streamers) s WHERE lower(s) <> %s)
                    WHERE EXISTS (SELECT 1 FROM unnest(streamers) s WHERE lower(s) = %s)""", (ch, ch))
    db.q("DELETE FROM campaigns WHERE id=%s", (campaign_id,))
    db.log(who, f"campaign {ca['name']} removed with its clips and posts ({out['removed']} posts, "
                f"{out['cancelled_in_buffer']} cancelled in Buffer, {len(out['still_live'])} still live on the apps)")
    return {**out, "campaign_removed": True}


def reschedule_post(post_id: int, due: datetime, who: str = "dashboard") -> dict:
    p = db.one("SELECT * FROM posts WHERE id=%s", (post_id,))
    if not p:
        raise LookupError("post not found")
    if p["status"] in QUEUED:
        raise ValueError("only a post waiting in the queue can be moved")
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    if due <= datetime.now(timezone.utc):
        raise ValueError("pick a time in the future")
    iso = due.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    if p.get("buffer_post_id"):
        buffer.reschedule_post(p["buffer_post_id"], iso)
    db.q("UPDATE posts SET due_at=%s, updated_at=now() WHERE id=%s", (due, post_id))
    db.log(who, f"post #{post_id} ({p.get('service')}) moved to {iso}")
    return {"ok": True, "due_at": iso}


def _http(fn, *a):
    try:
        return fn(*a)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except (ValueError, buffer.BufferError) as e:
        raise HTTPException(400, str(e))


@router.delete("/posts/{post_id}")
def delete_post(post_id: int):
    return _http(remove_post, post_id)


@router.delete("/clips/{clip_id}")
def delete_clip(clip_id: int):
    return _http(remove_clip, clip_id)


@router.delete("/campaigns/{campaign_id}")
def delete_campaign(campaign_id: int):
    return _http(remove_campaign, campaign_id)


class Move(BaseModel):
    due_at: datetime


@router.post("/posts/{post_id}/reschedule")
def move_post(post_id: int, body: Move):
    return _http(reschedule_post, post_id, body.due_at)
