"""Write the on-screen hook, the title and the caption line for a clip, and pick its payoff moment.

With ANTHROPIC_API_KEY set, Claude reads the timed transcript and the loudest moments (hook.candidates), picks the
payoff the clip should open on, and writes a short on-screen hook for the first 1-2 seconds, under the campaign's
rules (accurate, no clickbait). Without it, the loudest moment is used, the viewer's Twitch clip title becomes the
title, and the clip waits for review in the dashboard.
"""
import json
import re
import urllib.request

from .. import config

PROMPT = """You edit short vertical clips of a streamer's GTA 6 stream for TikTok, Reels and Shorts.
Viewers decide in the first 1-2 seconds whether to keep watching, so each clip opens with a 2-second teaser of
its payoff (the funniest or most intense moment) and a big text hook on screen, then plays from the setup.
Campaign rules: everything must be accurate to what happens in the clip; misleading titles, fake context and
clickbait that oversells are rejected. English only. No hashtags, no links.

Streamer: {streamer}
Title a viewer gave this Twitch clip: {clip_title!r}
Timed transcript of the streamer (seconds, may be noisy):
{timed}

Loudest / most hyped moments found in the audio:
{cands}

Return only JSON: {{"payoff": "A", "screen": "...", "title": "...", "hook": "..."}}
- payoff: the letter of the moment that is the payoff (the reaction, the punchline, the crash), or "" if none fits.
- screen: the on-screen hook shown from 0:00, 3-7 words, at most 34 characters, readable in one glance.
  It makes the viewer need to see what happens, without spoiling it. Plain words, no emoji, no quotes.
  Good: "HE TRUSTED THE GPS ONE TIME", "COPS DID NOT EXPECT THIS", "THIS GRANNY IS UNSTOPPABLE".
- title: post title, 5-12 words: who + what is at stake + the open question. Name the streamer or GTA 6.
  Never reveal the ending. No emoji.
- hook: one caption line (max 80 chars) that invites comments, ideally a short question to the viewer, may end
  with one fitting emoji.
- If the transcript and clip title don't make clear what happens, return {{"payoff": "", "screen": "", "title": "", "hook": ""}}."""


def timed_transcript(words: list[dict], every: float = 2.5, limit: int = 1800) -> str:
    lines, cur, t0 = [], [], None
    for w in words:
        s = float(w.get("s", 0))
        if t0 is None:
            t0 = s
        if s - t0 >= every and cur:
            lines.append(f"[{t0:.1f}s] " + " ".join(cur))
            cur, t0 = [], s
        cur.append(str(w.get("w", "")))
    if cur:
        lines.append(f"[{t0:.1f}s] " + " ".join(cur))
    return "\n".join(lines)[:limit] or "(no speech)"


def _screen_from(title: str) -> str:
    words = re.findall(r"[A-Za-z0-9']+", title or "")
    return " ".join(words[:6]).upper() if len(words) >= 3 else ""


def _fallback(clip_title: str, streamer: str) -> dict:
    t = re.sub(r"\s+", " ", clip_title or "").strip()
    words = re.findall(r"[A-Za-z']+", t)
    good = len(words) >= 3 and not (t.isupper() and len(words) < 5)
    if not good:
        return {"title": "", "hook": "", "screen": "WAIT FOR IT…", "payoff": None, "needs_review": True}
    title = t if streamer.lower() in t.lower() else f"{t} ({streamer}, GTA 6)"
    return {"title": title[:80], "hook": t[:80], "screen": _screen_from(t), "payoff": None, "needs_review": True}


def make(clip_title: str, transcript: str, streamer: str, words: list[dict] | None = None,
         cands: list[dict] | None = None) -> dict:
    """Returns title, hook (caption line), screen (on-screen hook), payoff (index into cands or None), needs_review."""
    if not config.ANTHROPIC_API_KEY:
        return _fallback(clip_title, streamer)
    cands = cands or []
    letters = "ABCDE"
    cand_txt = "\n".join(f"{letters[i]}: at {c['t']:.1f}s, around it: {c['text']!r}" for i, c in enumerate(cands[:5])) or "(none)"
    timed = timed_transcript(words) if words else (transcript or "")[:1500]
    body = {"model": config.TITLE_MODEL, "max_tokens": 2000,
            "messages": [{"role": "user", "content": PROMPT.format(streamer=streamer, clip_title=clip_title,
                                                                   timed=timed, cands=cand_txt)}]}
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
        letter = str(out.get("payoff") or "").strip().upper()[:1]
        payoff = letters.index(letter) if letter and letter in letters[:len(cands)] else None
        screen = re.sub(r"[\"“”]", "", str(out.get("screen") or "")).strip()[:40] or _screen_from(out["title"])
        return {"title": out["title"][:90], "hook": (out.get("hook") or out["title"])[:90], "screen": screen,
                "payoff": payoff, "needs_review": False}
    except Exception:
        return _fallback(clip_title, streamer)
