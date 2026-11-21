"""Buffer GraphQL API (personal key). Posts video by public URL; reads status and links.

Buffer's free plan allows 150 requests / 15 min, 600 / day and 3,000 / 30 days, shared by all keys. So:
- the organization and channel list are cached for a day (in the settings table, so restarts don't refetch),
- every response's RateLimit headers are stored, and calls stop when a window is nearly used up,
- a 429 pauses all Buffer calls until the window resets.
"""
import json
import re
import time
import urllib.error
import urllib.request

from .. import config, db

API = "https://api.buffer.com"
RESERVE = {900: 10, 86400: 40, 2592000: 150}  # keep this many requests in each window for posting


class BufferError(RuntimeError):
    pass


class RateLimited(BufferError):
    pass


def _save_limits(headers):
    """RateLimit: "600-in-1day";r=598;t=86397  (one header per window)."""
    windows = {}
    span = {"15min": 900, "1day": 86400, "30days": 2592000}
    for h in headers.get_all("RateLimit") or []:
        for part in h.split(","):
            m = re.search(r'"(\d+)-in-([a-z0-9]+)";\s*r=(\d+);\s*t=(\d+)', part)
            if m:
                q, unit, r, t = int(m[1]), m[2], int(m[3]), int(m[4])
                w = span.get(unit) or (900 if t <= 900 else 86400 if t <= 86400 else 2592000)
                windows[str(w)] = {"quota": q, "remaining": r, "reset_at": int(time.time()) + t}
    if windows:
        db.set_setting("buffer_limits", windows)
        low = [w for w, v in windows.items() if v["remaining"] <= RESERVE.get(int(w), 10)]
        if low:
            until = max(windows[w]["reset_at"] for w in low)
            db.set_setting("buffer_paused_until", until)


def paused_until() -> int:
    return int(db.settings().get("buffer_paused_until") or 0)


def gql(query: str, variables: dict | None = None, essential: bool = False) -> dict:
    """essential=True (creating a post) may use the reserve; everything else stops when a window runs low."""
    if not config.BUFFER_API_KEY:
        raise BufferError("BUFFER_API_KEY is not set")
    until = paused_until()
    if until > time.time() and not essential:
        raise RateLimited(f"Buffer calls paused until {time.strftime('%H:%M UTC', time.gmtime(until))} to stay under the limit")
    req = urllib.request.Request(API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {config.BUFFER_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            _save_limits(r.headers)
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        if e.code == 429:
            _save_limits(e.headers)
            retry = int(e.headers.get("Retry-After") or 0) or 3600
            db.set_setting("buffer_paused_until", max(paused_until(), int(time.time()) + retry))
            raise RateLimited(f"Buffer rate limit reached; paused for {retry // 60} min")
        raise BufferError(f"HTTP {e.code}: {body}")


def _cached(key: str, max_age: int, fetch):
    c = db.settings().get(key)
    if c and time.time() - c.get("at", 0) < max_age:
        return c["value"]
    value = fetch()
    db.set_setting(key, {"at": int(time.time()), "value": value})
    return value


def org_id() -> str:
    def fetch():
        r = gql("{ account { organizations { id } } }")
        return r["data"]["account"]["organizations"][0]["id"]
    return _cached("buffer_org", 7 * 86400, fetch)


def channels() -> list[dict]:
    def fetch():
        r = gql("query($o: OrganizationId!){ channels(input:{organizationId:$o}){ id name displayName service isQueuePaused } }",
                {"o": org_id()})
        if r.get("errors"):
            raise BufferError(json.dumps(r["errors"])[:500])
        return r["data"]["channels"]
    return _cached("buffer_channels", 86400, fetch)


CREATE = """mutation($input: CreatePostInput!) { createPost(input: $input) {
  ... on PostActionSuccess { post { id dueAt status } }
  ... on MutationError { message } } }"""


def create_video_post(channel: dict, video_url: str, title: str, caption: str, due_iso: str) -> dict:
    inp = {"channelId": channel["id"], "text": caption, "schedulingType": "automatic", "mode": "customScheduled",
           "dueAt": due_iso, "needsApproval": False, "source": "posts.lane.ge",
           "assets": [{"video": {"url": video_url}}]}
    s = channel["service"]
    if s == "instagram":
        inp["metadata"] = {"instagram": {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": False}}
        inp["assets"][0]["video"]["metadata"] = {"thumbnailOffset": 1500}
    elif s == "tiktok":
        inp["metadata"] = {"tiktok": {"title": title[:90], "isAiGenerated": False}}
        inp["assets"][0]["video"]["metadata"] = {"thumbnailOffset": 1500}
    elif s == "youtube":
        inp["metadata"] = {"youtube": {"title": (title[:90] + " #shorts")[:100], "categoryId": "20", "privacy": "public",
                                       "madeForKids": False, "notifySubscribers": True, "isAiGenerated": False}}
    r = gql(CREATE, {"input": inp}, essential=True)
    res = (r.get("data") or {}).get("createPost") or {}
    if r.get("errors") or "post" not in res:
        raise BufferError(res.get("message") or json.dumps(r.get("errors"))[:500])
    return res["post"]


def recent_posts(channel_ids: list[str], statuses: list[str], metrics: bool = False) -> list[dict]:
    """Up to 50 most recent posts of these channels in these states: one request instead of one per post.
    metrics=True also returns each post's stats (needs the insights:read scope)."""
    ids = ", ".join(json.dumps(c) for c in channel_ids if re.fullmatch(r"[A-Za-z0-9]+", c))
    sts = ", ".join(x for x in statuses if re.fullmatch(r"[a-z_]+", x))
    q = (f'query {{ posts(first: 50, input: {{organizationId: {json.dumps(org_id())}, '
         f'sort: [{{field: dueAt, direction: desc}}], filter: {{status: [{sts}], channelIds: [{ids}]}}}}) '
         '{ edges { node { id status dueAt sentAt externalLink error { message }'
         + (' metrics { type name value unit } metricsUpdatedAt' if metrics else '') + ' } } } }')
    r = gql(q)
    if r.get("errors"):
        raise BufferError(json.dumps(r["errors"])[:500])
    return [e["node"] for e in r["data"]["posts"]["edges"]]


def post_status(post_id: str) -> dict:
    r = gql("query($id: PostId!){ post(input:{id:$id}){ id status dueAt sentAt externalLink error { message } } }", {"id": post_id})
    if r.get("errors"):
        raise BufferError(json.dumps(r["errors"])[:500])
    return r["data"]["post"]


def post_metrics(post_id: str) -> list[dict]:
    """Needs the insights:read scope on the key."""
    r = gql("query($id: PostId!){ post(input:{id:$id}){ metrics { type name value unit } metricsUpdatedAt } }", {"id": post_id})
    if r.get("errors"):
        raise BufferError(json.dumps(r["errors"])[:500])
    return r["data"]["post"].get("metrics") or []
