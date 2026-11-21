"""Instagram Reels for a hashtag, through Apify's Instagram Hashtag Scraper (needs APIFY_TOKEN; Instagram has no
public search API). Optional: without the token the trends worker only reads YouTube Shorts.

Apify bills per result (a few cents per run at our sizes; the free plan's monthly credit covers light use).
"""
from datetime import datetime, timezone

import requests

from .. import config

ACTOR = "apify~instagram-hashtag-scraper"


def reels(tag: str, limit: int = 30) -> list[dict]:
    if not config.APIFY_TOKEN:
        return []
    r = requests.post(f"https://api.apify.com/v2/acts/{ACTOR}/run-sync-get-dataset-items",
                      params={"token": config.APIFY_TOKEN, "timeout": 240},
                      json={"hashtags": [tag.lstrip("#")], "resultsType": "reels", "resultsLimit": limit}, timeout=300)
    if r.status_code >= 300:
        raise RuntimeError(f"Apify {r.status_code}: {r.text[:300]}")
    out = []
    for it in r.json() or []:
        if not isinstance(it, dict) or not it.get("videoUrl"):
            continue
        views = int(it.get("videoPlayCount") or it.get("videoViewCount") or it.get("playsCount") or 0)
        ts = it.get("timestamp")
        out.append({"platform": "instagram", "id": str(it.get("shortCode") or it.get("id")),
                    "title": (it.get("caption") or "")[:200], "channel": it.get("ownerUsername") or "",
                    "channel_id": str(it.get("ownerId") or ""), "tags": it.get("hashtags") or [], "description": "",
                    "views": views, "likes": int(it.get("likesCount") or 0),
                    "duration": float(it.get("videoDuration") or 0), "published_at": ts,
                    "thumb": it.get("displayUrl"), "url": it.get("url") or f"https://www.instagram.com/reel/{it.get('shortCode')}/",
                    "video_url": it.get("videoUrl")})
    return out


def parse_time(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None
