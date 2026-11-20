"""Buffer GraphQL API (personal key). Posts video by public URL; reads status, link and metrics."""
import json
import urllib.error
import urllib.request

from .. import config

API = "https://api.buffer.com"


class BufferError(RuntimeError):
    pass


def gql(query: str, variables: dict | None = None) -> dict:
    if not config.BUFFER_API_KEY:
        raise BufferError("BUFFER_API_KEY is not set")
    req = urllib.request.Request(API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {config.BUFFER_API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        raise BufferError(f"HTTP {e.code}: {e.read().decode()[:500]}")


def org_id() -> str:
    r = gql("{ account { organizations { id } } }")
    return r["data"]["account"]["organizations"][0]["id"]


def channels() -> list[dict]:
    r = gql("query($o: OrganizationId!){ channels(input:{organizationId:$o}){ id name displayName service isQueuePaused } }",
            {"o": org_id()})
    if r.get("errors"):
        raise BufferError(json.dumps(r["errors"])[:500])
    return r["data"]["channels"]


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
    r = gql(CREATE, {"input": inp})
    res = (r.get("data") or {}).get("createPost") or {}
    if r.get("errors") or "post" not in res:
        raise BufferError(res.get("message") or json.dumps(r.get("errors"))[:500])
    return res["post"]


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
