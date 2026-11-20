"""Write the on-video title and the caption hook for a clip.

With ANTHROPIC_API_KEY set, Claude writes them from the transcript and the viewer's clip title, under the
campaign's rules (accurate, no clickbait). Without it, the viewer's Twitch clip title is used and the clip
waits for review in the dashboard.
"""
import json
import re
import urllib.request

from .. import config

PROMPT = """You title short vertical clips of a streamer's GTA 6 stream for TikTok, Reels and Shorts.
Campaign rules: titles must be accurate to what happens in the clip; misleading titles, fake context and clickbait
that oversells are rejected. Strong hook in the first second. English only. No hashtags, no links, no emoji in the title.

Streamer: {streamer}
Title a viewer gave this Twitch clip: {clip_title!r}
Transcript of the streamer (may be noisy): {transcript!r}

Return only JSON: {{"title": "...", "hook": "..."}}
- title: on-video headline shown in the first seconds, 5-12 words. Formula: who + what is at stake + the open
  question, so the viewer waits for the payoff (e.g. "He bet a free copy of GTA 6 he would NOT get eaten by a crocodile").
  Never reveal the ending. Name the streamer or GTA 6. Must stay true to the transcript.
- hook: one caption line (max 80 chars) that invites comments, ideally a short question to the viewer
  (e.g. "Would you have run? 🐊"), may end with one fitting emoji.
- If the transcript and clip title don't make clear what happens, return {{"title": "", "hook": ""}}."""


def _fallback(clip_title: str, streamer: str) -> dict:
    t = re.sub(r"\s+", " ", clip_title or "").strip()
    words = re.findall(r"[A-Za-z']+", t)
    good = len(words) >= 3 and not (t.isupper() and len(words) < 5)
    if not good:
        return {"title": "", "hook": "", "needs_review": True}
    title = t if streamer.lower() in t.lower() else f"{t} ({streamer}, GTA 6)"
    return {"title": title[:80], "hook": t[:80], "needs_review": True}


def make(clip_title: str, transcript: str, streamer: str) -> dict:
    if not config.ANTHROPIC_API_KEY:
        return _fallback(clip_title, streamer)
    body = {"model": config.TITLE_MODEL, "max_tokens": 2000,
            "messages": [{"role": "user", "content": PROMPT.format(streamer=streamer, clip_title=clip_title,
                                                                   transcript=(transcript or "")[:1500])}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = "".join(b.get("text", "") for b in json.load(r).get("content", []) if b.get("type") == "text")
        m = re.search(r"\{.*\}", text, re.S)
        out = json.loads(m.group(0)) if m else {}
        if not out.get("title"):
            return _fallback(clip_title, streamer)
        return {"title": out["title"][:90], "hook": (out.get("hook") or out["title"])[:90], "needs_review": False}
    except Exception:
        return _fallback(clip_title, streamer)
