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


def top_clips(login: str, period: str = "LAST_WEEK") -> list[dict]:
    """period: LAST_DAY | LAST_WEEK | LAST_MONTH | ALL_TIME."""
    data = gql(CLIPS, {"login": login, "period": period})
    user = (data.get("data") or {}).get("user")
    if not user:
        raise RuntimeError(f"twitch user {login!r} not found: {data.get('errors')}")
    out = []
    for e in user["clips"]["edges"]:
        n = e["node"]
        out.append({
            "slug": n["slug"], "title": (n.get("title") or "").strip(), "views": n.get("viewCount") or 0,
            "duration": n.get("durationSeconds") or 0, "created_at": n.get("createdAt"),
            "game": (n.get("game") or {}).get("name"), "vod_id": (n.get("video") or {}).get("id"),
            "vod_offset": n.get("videoOffsetSeconds"), "url": f"https://clips.twitch.tv/{n['slug']}",
        })
    return out
