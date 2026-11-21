"""Write the on-screen hook, the title and the caption line for a clip, and pick its payoff moment.

With ANTHROPIC_API_KEY set, Claude reads the timed transcript and the loudest moments (hook.candidates), picks the
payoff the clip should open on, and writes a short on-screen hook for the first 1-2 seconds, under the campaign's
rules (accurate, no clickbait). Without it, or when the model fails twice, the loudest moment is used, the viewer's
Twitch clip title becomes the title (a neutral line when that title is only a number), and the clip waits for review
in the dashboard. The result's "why" says what went wrong, and render logs it.
"""
import json
import re
import urllib.error
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


PROMPT_RU = """You edit short vertical clips of a Russian-speaking streamer's Twitch stream for YouTube Shorts and
TikTok, for a Russian-speaking audience. Viewers decide in the first 1-2 seconds whether to keep watching, so each
clip opens with a 2-second teaser of its payoff (the funniest or most intense moment) and a big text hook on screen,
then plays from the setup. Everything must be accurate to what happens in the clip; no fake context, no clickbait
that oversells, nothing political or hateful. Write in natural Russian (Cyrillic), the way Russian Shorts titles
are written. No hashtags, no links.

Streamer: {streamer}
Title a viewer gave this Twitch clip: {clip_title!r}
Timed transcript of the streamer (seconds, Russian, may be noisy):
{timed}

Loudest / most hyped moments found in the audio:
{cands}

Return only JSON: {{"payoff": "A", "screen": "...", "title": "...", "hook": "..."}}
- payoff: the letter of the moment that is the payoff (the reaction, the punchline), or "" if none fits.
- screen: the on-screen hook shown from 0:00, in Russian, 2-6 words, at most 30 characters, readable in one glance.
  It makes the viewer need to see what happens, without spoiling it. Plain words, no emoji, no quotes.
  Good: "ОН НЕ ОЖИДАЛ ТАКОГО", "ЧАТ В ШОКЕ", "ЭТО БЫЛО ЗРЯ".
- title: post title in Russian, 4-10 words: who + what happens + the open question. Name the streamer.
  Never reveal the ending. No emoji.
- hook: one caption line in Russian (max 80 chars) that invites comments, ideally a short question to the viewer,
  may end with one fitting emoji.
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
    words = re.findall(r"[^\W_]+(?:'[^\W_]+)?", title or "")
    return " ".join(words[:6]).upper() if len(words) >= 3 else ""


# Used only when the model gave nothing and the viewer's Twitch title is not usable either (a number, one word).
# They say nothing the clip could contradict, and the clip waits in Review so a person can write a better one.
GENERIC = {
    "ru": {"titles": ["{s}: этот момент со стрима надо видеть", "{s}: что это сейчас было на стриме?",
                      "{s}: реакция на стриме, которую надо видеть"],
           "hooks": ["Как вам такой момент? 😅", "А что бы сделали вы? 🤔", "Ваша реакция? 👇"],
           "screen": "ЖДИ КОНЦОВКУ…"},
    "en": {"titles": ["{s}: you need to see this GTA 6 stream moment", "{s}: what just happened on stream?",
                      "{s}: the reaction on this GTA 6 stream"],
           "hooks": ["What would you have done here? 🤔", "Did you see that coming? 😅", "Your reaction? 👇"],
           "screen": "WAIT FOR IT…"},
}

# Added to the prompt for a second try when the model returned nothing and the viewer's title is useless.
THIN = """

The viewer's clip title above is not usable (a number or a random word), so ignore it. Do not return empty strings.
Write the title, the hook and the on-screen hook only from what the transcript supports: who is there, what they
react to, the mood. Where details are unclear, stay general instead of guessing."""


def usable(clip_title: str) -> bool:
    """A viewer's Twitch clip title that can stand as a post title: at least three real words, not a number or an id."""
    t = re.sub(r"\s+", " ", clip_title or "").strip()
    words = re.findall(r"[^\W\d_]+(?:'[^\W\d_]+)?", t)
    return len(words) >= 3 and not (t.isupper() and len(words) < 5)


def _fallback(clip_title: str, streamer: str, language: str = "en", why: str = "") -> dict:
    t = re.sub(r"\s+", " ", clip_title or "").strip()
    if not usable(t):
        g = GENERIC["ru" if language == "ru" else "en"]
        i = sum(map(ord, t)) % len(g["titles"])  # varies from clip to clip, stays the same on a re-render
        return {"title": g["titles"][i].format(s=streamer), "hook": g["hooks"][i], "screen": g["screen"],
                "payoff": None, "needs_review": True, "why": why}
    title = t if streamer.lower() in t.lower() else (f"{t} ({streamer})" if language == "ru" else f"{t} ({streamer}, GTA 6)")
    return {"title": title[:80], "hook": t[:80], "screen": _screen_from(t), "payoff": None, "needs_review": True,
            "why": why}


def _ask(prompt: str) -> dict:
    body = {"model": config.TITLE_MODEL, "max_tokens": 2000, "messages": [{"role": "user", "content": prompt}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.read()[:160].decode('utf-8', 'replace')}") from None
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"no JSON in the answer (stop: {data.get('stop_reason')})")
    out = json.loads(m.group(0))
    return out if isinstance(out, dict) else {}


def make(clip_title: str, transcript: str, streamer: str, words: list[dict] | None = None,
         cands: list[dict] | None = None, language: str = "en") -> dict:
    """Returns title, hook (caption line), screen (on-screen hook), payoff (index into cands or None), needs_review
    and why (empty, or the reason the model's texts were not used). language "ru" writes them in Russian."""
    language = (language or "en").lower()
    if not config.ANTHROPIC_API_KEY:
        return _fallback(clip_title, streamer, language, "ANTHROPIC_API_KEY is not set")
    cands = cands or []
    letters = "ABCDE"
    cand_txt = "\n".join(f"{letters[i]}: at {c['t']:.1f}s, around it: {c['text']!r}" for i, c in enumerate(cands[:5])) or "(none)"
    timed = timed_transcript(words) if words else (transcript or "")[:1500]
    prompt = (PROMPT_RU if language == "ru" else PROMPT).format(streamer=streamer, clip_title=clip_title,
                                                                timed=timed, cands=cand_txt)
    out, why, thin = {}, "", False
    for _ in range(2):  # one more try after an error, or with THIN when the first answer was empty
        try:
            out = _ask(prompt + (THIN if thin else ""))
        except Exception as e:
            out, why = {}, f"{type(e).__name__}: {e}"[:200]
            continue
        if out.get("title"):
            break
        why = "the model found the clip unclear"
        if usable(clip_title):
            break
        thin = True
    if not out.get("title"):
        return _fallback(clip_title, streamer, language, why)
    letter = str(out.get("payoff") or "").strip().upper()[:1]
    payoff = letters.index(letter) if letter and letter in letters[:len(cands)] else None
    title = str(out["title"])
    screen = re.sub(r"[\"“”]", "", str(out.get("screen") or "")).strip()[:40] or _screen_from(title)
    # texts written from a thin transcript alone wait in Review instead of being auto-approved
    return {"title": title[:90], "hook": str(out.get("hook") or title)[:90], "screen": screen,
            "payoff": payoff, "needs_review": thin, "why": ""}
