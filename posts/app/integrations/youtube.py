"""YouTube Data API v3 (needs YOUTUBE_API_KEY): find the most-viewed recent Shorts for a hashtag.

Quota: a search costs 100 units, a videos.list call 1 unit; the free daily quota is 10,000 units, so the trends
worker (app/workers/trends.py) keeps to a few dozen searches a day.
"""
import re
from datetime import datetime

import requests

from .. import config

API = "https://www.googleapis.com/youtube/v3"


def _get(path: str, params: dict) -> dict:
    if not config.YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY is not set")
    r = requests.get(f"{API}/{path}", params={**params, "key": config.YOUTUBE_API_KEY}, timeout=30)
    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:300]
        raise RuntimeError(f"YouTube {path}: {r.status_code} {msg}")
    return r.json()


def seconds(iso: str) -> float:
    """ISO 8601 duration (PT1M5S) -> seconds."""
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", iso or "")
    if not m:
        return 0.0
    d, h, mi, s = (float(x) if x else 0.0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def search(q: str, after: datetime, language: str = "", region: str = "", order: str = "viewCount",
           limit: int = 25) -> list[str]:
    """Video ids of short (< 4 min) videos matching q published after `after`, best first."""
    p = {"part": "id", "type": "video", "q": q, "order": order, "maxResults": min(limit, 50), "videoDuration": "short",
         "publishedAfter": after.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if language:
        p["relevanceLanguage"] = language
    if region:
        p["regionCode"] = region
    return [i["id"]["videoId"] for i in _get("search", p).get("items", []) if i.get("id", {}).get("videoId")]


def videos(ids: list[str]) -> list[dict]:
    """Title, channel, views, duration, publish time and thumbnail for each id (50 per call)."""
    out = []
    for k in range(0, len(ids), 50):
        data = _get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids[k:k + 50])})
        for v in data.get("items", []):
            sn, st = v.get("snippet", {}), v.get("statistics", {})
            th = sn.get("thumbnails", {})
            thumb = (th.get("maxres") or th.get("high") or th.get("medium") or th.get("default") or {}).get("url")
            out.append({"platform": "youtube", "id": v["id"], "title": sn.get("title") or "",
                        "channel": sn.get("channelTitle") or "", "channel_id": sn.get("channelId") or "",
                        "tags": sn.get("tags") or [], "description": (sn.get("description") or "")[:500],
                        "views": int(st.get("viewCount") or 0), "likes": int(st.get("likeCount") or 0),
                        "duration": seconds(v.get("contentDetails", {}).get("duration", "")),
                        "published_at": sn.get("publishedAt"), "thumb": thumb,
                        "url": f"https://www.youtube.com/shorts/{v['id']}"})
    return out
