"""Trends: find the most-viewed recent Shorts (and Reels) of a campaign's streamers that nobody has turned into a
split-screen clip yet, and queue them as moments, so render cuts our own split + banner version into Review.

For each active campaign with trend_tags (e.g. evelone, buster, strogo), every TREND_EVERY (4 h):
  1. YouTube Shorts search for each tag (#tag, most viewed first, within the campaign's lookback), plus Instagram
     Reels for the tag when APIFY_TOKEN is set.
  2. The best new ones (at least trend_min_views, 6-60 s, not one of our own accounts) are checked, CHECKS a run:
     the video's thumbnail and the thumbnails of other Shorts of the same streamer posted around it (from the same
     searches and one more YouTube search on the video's title) go to Claude, which says whether the video itself is
     already a split screen (a streamer over a satisfying/gameplay filler video), and whether any of the others shows
     the same moment as a split screen. Either: the video is skipped. Otherwise it becomes a moment (source
     yt_short / ig_reel) and render makes our split-screen version, which always waits in Review.
Each checked video is kept in trend_videos with its verdict, so it is never checked (or paid for) twice.
"""
import base64
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import requests

from .. import config, db
from ..integrations import instagram, youtube

TREND_EVERY = 4 * 3600   # per campaign; up to 9 YouTube searches (900 units) a run: ~5,400 of the free 10,000 a day
CHECKS = 3               # videos judged per campaign per run
MAX_TAGS = 6
MAX_OTHERS = 12


def due(c: dict) -> bool:
    return time.time() - float(db.settings().get(f"trends_at_{c['id']}") or 0) >= TREND_EVERY


def run():
    if not config.YOUTUBE_API_KEY and not config.APIFY_TOKEN:
        return 0
    added = 0
    for c in db.q("SELECT * FROM campaigns WHERE active AND cardinality(trend_tags) > 0 ORDER BY id"):
        if not due(c):
            continue
        db.set_setting(f"trends_at_{c['id']}", time.time())
        try:
            added += scan(c)
        except Exception as e:
            db.log("trends", f"{c['name']}: {e}", "error")
    return added


def tags(c: dict) -> list[str]:
    return [t.strip().lstrip("#").lower() for t in (c.get("trend_tags") or []) if t and t.strip()][:MAX_TAGS]


def collect(c: dict) -> list[dict]:
    """Every Short/Reel the campaign's tags turn up, each with the tag that found it."""
    since = datetime.now(timezone.utc) - timedelta(hours=c.get("lookback_hours") or 72)
    lang = c.get("language") or ""
    seen, pool = set(), []
    for tag in tags(c):
        found = []
        if config.YOUTUBE_API_KEY:
            try:
                found += youtube.videos(youtube.search(f"#{tag}", since, language=lang))
            except Exception as e:
                db.log("trends", f"#{tag} on YouTube: {e}", "warn")
        if config.APIFY_TOKEN:
            try:
                found += [r for r in instagram.reels(tag) if (instagram.parse_time(r["published_at"]) or since) >= since]
            except Exception as e:
                db.log("trends", f"#{tag} on Instagram: {e}", "warn")
        for v in found:
            if (v["platform"], v["id"]) not in seen:
                seen.add((v["platform"], v["id"]))
                pool.append({**v, "tag": tag})
    return pool


def ours() -> set[str]:
    return {re.sub(r"[^a-z0-9_.]", "", (a["handle"] or "").lower()) for a in db.q("SELECT handle FROM accounts")}


def eligible(v: dict, c: dict, own: set[str]) -> bool:
    if v["views"] < (c.get("trend_min_views") or 0):
        return False
    if not (6 <= (v["duration"] or 0) <= 60):
        return False
    return re.sub(r"[^a-z0-9_.]", "", (v["channel"] or "").lower()) not in own


def keywords(title: str, n: int = 5) -> str:
    words = [w for w in re.findall(r"\w+", re.sub(r"#\w+", " ", title or ""), re.U) if len(w) > 2]
    return " ".join(words[:n])


def scan(c: dict) -> int:
    pool = collect(c)
    if not pool:
        return 0
    done = {(r["platform"], r["video_id"]) for r in db.q(
        "SELECT platform, video_id FROM trend_videos WHERE campaign_id=%s", (c["id"],))}
    own = ours()
    fresh = sorted((v for v in pool if (v["platform"], v["id"]) not in done and eligible(v, c, own)),
                   key=lambda v: -v["views"])[:CHECKS]
    added = 0
    for v in fresh:
        verdict, why = judge(v, pool, c)
        moment_id = None
        if verdict in ("new", "unchecked"):
            moment_id = queue(c, v)
            added += 1 if moment_id else 0
        db.q("""INSERT INTO trend_videos(campaign_id, platform, video_id, url, title, channel, tag, views, duration,
                  published_at, verdict, reason, moment_id)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (campaign_id, platform, video_id) DO NOTHING""",
             (c["id"], v["platform"], v["id"], v["url"], v["title"][:300], v["channel"], v["tag"], v["views"],
              v["duration"], instagram.parse_time(v["published_at"]), verdict, why[:500], moment_id))
        db.log("trends", f"{v['platform']} {v['url']} ({v['views']:,} views, #{v['tag']}): {verdict}"
                         + (f" ({why})" if why else "") + (f", queued as moment #{moment_id}" if moment_id else ""))
    return added


def queue(c: dict, v: dict) -> int | None:
    src = "yt_short" if v["platform"] == "youtube" else "ig_reel"
    row = db.one("""INSERT INTO moments(campaign_id, source, channel, external_id, title, views, duration, url,
                      clip_created_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (campaign_id, external_id) DO NOTHING RETURNING id""",
                 (c["id"], src, v["tag"], f"{v['platform']}:{v['id']}", v["title"][:300], v["views"], v["duration"],
                  v.get("video_url") or v["url"], instagram.parse_time(v["published_at"])))
    return row["id"] if row else None


# ---- the split-screen check ----------------------------------------------------------------------------------

PROMPT = """Image 0 is the thumbnail of a vertical short video (YouTube Short or Instagram Reel) cut from a Twitch
streamer's stream. Its title: {title!r}. Images 1-{n} are thumbnails of other short videos of the same streamer
posted around the same time.

We want to re-cut image 0's moment in the split-screen format: the streamer clip in the top part of the frame and a
"satisfying" filler video in the bottom part (a ball race, a maze, soap or wood cutting, Subway Surfers, Minecraft
parkour, a marble run and the like), stacked with a straight horizontal seam.

Answer with only JSON:
{{"candidate_split": true | false, "split_same_moment": [indexes], "same_moment": [indexes], "why": "one sentence"}}

- "candidate_split": image 0 itself is already that split-screen format (a stacked webcam over the game it plays is
  NOT a split screen; a filler video unrelated to the stream is).
- "same_moment": others that show the same moment as image 0: same streamer, same scene, setting, outfit and
  situation, and a matching title if one is visible. Different frames of the same moment count.
- "split_same_moment": those of "same_moment" that are in the split-screen format.
When unsure whether something is the same moment, leave it out."""


def _jpeg(url: str) -> bytes | None:
    try:
        r = requests.get(url, timeout=20)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/") and len(r.content) < 4_000_000:
            return r.content
    except Exception:
        pass
    return None


def others_for(v: dict, pool: list[dict], c: dict) -> list[dict]:
    near = [o for o in pool if o is not v and o["tag"] == v["tag"]]
    if config.YOUTUBE_API_KEY and v["platform"] == "youtube":
        pub = instagram.parse_time(v["published_at"]) or datetime.now(timezone.utc)
        try:
            ids = youtube.search(f"{v['tag']} {keywords(v['title'])}", pub - timedelta(days=1), order="relevance",
                                 limit=15, language=c.get("language") or "")
            have = {o["id"] for o in near} | {v["id"]}
            near += [{**o, "tag": v["tag"]} for o in youtube.videos([i for i in ids if i not in have])]
        except Exception as e:
            db.log("trends", f"look-alike search for {v['url']}: {e}", "warn")
    near.sort(key=lambda o: -o["views"])
    return near[:MAX_OTHERS]


def judge(v: dict, pool: list[dict], c: dict) -> tuple[str, str]:
    """('new' | 'split' | 'taken' | 'unchecked', why)."""
    if not config.ANTHROPIC_API_KEY:
        return "unchecked", "ANTHROPIC_API_KEY is not set, nothing was compared"
    main = _jpeg(v["thumb"]) if v.get("thumb") else None
    if not main:
        return "unchecked", "no thumbnail to compare"
    others = [(o, img) for o in others_for(v, pool, c) if o.get("thumb") and (img := _jpeg(o["thumb"]))]
    try:
        out = ask(v, main, others)
    except Exception as e:
        return "unchecked", f"the check failed: {e}"[:300]
    why = str(out.get("why") or "")[:300]
    if out.get("candidate_split"):
        return "split", why or "already a split screen"
    hits = [i for i in out.get("split_same_moment") or [] if isinstance(i, int) and 1 <= i <= len(others)]
    if hits:
        urls = ", ".join(others[i - 1][0]["url"] for i in hits[:3])
        return "taken", f"already split-screened: {urls}"
    same = [i for i in out.get("same_moment") or [] if isinstance(i, int) and 1 <= i <= len(others)]
    return "new", (f"{len(same)} other upload(s) of this moment, none split-screened" if same else "")


def ask(v: dict, main: bytes, others: list[tuple[dict, bytes]]) -> dict:
    content: list[dict] = []
    for k, (title, img) in enumerate([(v["title"], main)] + [(o["title"], im) for o, im in others]):
        content.append({"type": "text", "text": f"Image {k}" + (f" (title: {title[:120]!r})" if k else "") + ":"})
        mt = "image/png" if img[:4] == b"\x89PNG" else "image/webp" if img[8:12] == b"WEBP" else "image/jpeg"
        content.append({"type": "image", "source": {"type": "base64", "media_type": mt,
                                                    "data": base64.b64encode(img).decode()}})
    content.append({"type": "text", "text": PROMPT.format(title=v["title"][:200], n=len(others))})
    body = {"model": config.TITLE_MODEL, "max_tokens": 800, "messages": [{"role": "user", "content": content}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.read()[:160].decode('utf-8', 'replace')}") from None
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError("no JSON in the answer")
    return json.loads(m.group(0))
