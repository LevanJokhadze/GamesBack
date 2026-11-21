"""Twitch public GQL (no account, no key): a channel's most-viewed clips with game and VOD offset."""
import json
import urllib.request

GQL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # Twitch's public web client id (the one yt-dlp uses)

CLIPS = """query($login:String!, $period:ClipsPeriod!){ user(login:$login){ clips(first:50, criteria:{period:$period, sort:VIEWS_DESC}){
  edges{ node{ id slug title viewCount durationSeconds createdAt game{ name } videoOffsetSeconds video{ id } } } } } }"""


def gql(query: str, variables: dict) -> dict:
    req = urllib.request.Request(GQL, data=json.dumps({"query": query, "variables": variables}).encode(),
                                 headers={"Client-ID": CLIENT_ID, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def top_clips(login: str, period: str = "LAST_WEEK", fallback: bool = True) -> list[dict]:
    """period: LAST_DAY | LAST_WEEK | LAST_MONTH | ALL_TIME. With fallback off, a GQL answer without data gives []
    instead of the slow yt-dlp listing (which only knows the last 7 days)."""
    data = gql(CLIPS, {"login": login, "period": period})
    user = (data.get("data") or {}).get("user")
    clips = (user or {}).get("clips")
    if not clips:
        if not fallback:
            return []
        return ytdlp_clips(login, f"twitch GQL gave no clips ({str(data.get('errors') or data)[:300]})")
    out = []
    for e in clips.get("edges") or []:
        n = e["node"]
        out.append({
            "slug": n["slug"], "title": (n.get("title") or "").strip(), "views": n.get("viewCount") or 0,
            "duration": n.get("durationSeconds") or 0, "created_at": n.get("createdAt"),
            "game": (n.get("game") or {}).get("name"), "vod_id": (n.get("video") or {}).get("id"),
            "vod_offset": n.get("videoOffsetSeconds"), "url": f"https://clips.twitch.tv/{n['slug']}",
        })
    return out


def ytdlp_clips(login: str, why: str) -> list[dict]:
    """Fallback when Twitch's GQL answers this server without data: yt-dlp lists the channel's top clips of the
    week, then reads each one for its game and date (yt-dlp handles Twitch's request signing)."""
    import subprocess
    import sys

    from .. import db

    db.log("discover", f"{login}: {why}; using yt-dlp instead", "warn")
    entries = []
    for _ in range(3):  # Twitch sometimes answers with an empty list; try again
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "--flat-playlist", "--playlist-end", "25", "-J",
                            f"https://www.twitch.tv/{login}/clips?filter=clips&range=7d"],
                           capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"yt-dlp clip list failed: {r.stderr[-400:]}")
        entries = json.loads(r.stdout).get("entries") or []
        if entries:
            break
    out = []
    for e in entries:
        url = e.get("url") or ""
        slug = url.rstrip("/").split("/")[-1]
        info = subprocess.run([sys.executable, "-m", "yt_dlp", "--no-warnings", "-J", "--skip-download", url],
                              capture_output=True, text=True, timeout=120)
        meta = json.loads(info.stdout) if info.returncode == 0 and info.stdout.strip() else {}
        ts = meta.get("timestamp")
        created = None
        if ts:
            from datetime import datetime, timezone
            created = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        game = (meta.get("categories") or [None])[0] or meta.get("game")
        out.append({"slug": slug, "title": (e.get("title") or meta.get("title") or "").strip(),
                    "views": e.get("view_count") or meta.get("view_count") or 0,
                    "duration": e.get("duration") or meta.get("duration") or 0, "created_at": created, "game": game,
                    "vod_id": None, "vod_offset": None, "url": f"https://clips.twitch.tv/{slug}"})
    return out
