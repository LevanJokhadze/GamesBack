"""Discover: pull each campaign streamer's most-viewed Twitch clips (the week's top 50, plus today's top 50 for
campaigns that take low-view clips) and keep them as candidate moments."""
from datetime import datetime, timedelta, timezone

from .. import db
from ..integrations import twitch


def run():
    found = 0
    for c in db.q("SELECT * FROM campaigns WHERE active"):
        since = datetime.now(timezone.utc) - timedelta(hours=c["lookback_hours"] or 48)
        for login in c["channels"] or []:
            try:
                clips = twitch.top_clips(login, "LAST_WEEK")
            except Exception as e:
                db.log("discover", f"{login}: {e}", "error")
                continue
            if (c["min_clip_views"] or 0) < 100:
                # a campaign that takes low-view clips also gets today's clips: they have not had time to reach
                # the week's top 50
                try:
                    seen = {k["slug"] for k in clips}
                    clips += [k for k in twitch.top_clips(login, "LAST_DAY", fallback=False) if k["slug"] not in seen]
                except Exception as e:
                    db.log("discover", f"{login}: today's clips: {e}", "warn")
            for k in clips:
                created = datetime.fromisoformat(k["created_at"].replace("Z", "+00:00")) if k["created_at"] else None
                if c["game"] and k["game"] != c["game"]:
                    continue
                if created and created < since:
                    continue
                if k["views"] < (c["min_clip_views"] or 0) or not (6 <= (k["duration"] or 0) <= 60):
                    continue
                row = db.one("""INSERT INTO moments(campaign_id, channel, external_id, title, views, duration, game, url,
                                  vod_id, vod_offset, clip_created_at)
                                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                                ON CONFLICT (campaign_id, external_id) DO UPDATE SET views = EXCLUDED.views, updated_at = now()
                                RETURNING (xmax = 0) AS inserted""",
                             (c["id"], login, k["slug"], k["title"], k["views"], k["duration"], k["game"], k["url"],
                              k["vod_id"], k["vod_offset"], created))
                if row and row["inserted"]:
                    found += 1
    if found:
        db.log("discover", f"{found} new moments")
    return found
