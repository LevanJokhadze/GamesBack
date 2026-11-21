"""Nudity check: look at the source clip before it is cut.

TikTok removes videos that show nudity (game characters included) and gives the account a strike; YouTube
age-restricts them, which keeps a Short out of the Shorts feed. With ANTHROPIC_API_KEY set, one frame a second of
the source clip goes to Claude, which answers with one of three verdicts:

  nudity     exposed genitals, bare buttocks, female nipples or a sex act: the moment is skipped, no clip is cut
  revealing  bikini, underwear, lingerie, a strip club without nudity: the clip is cut but waits in Review
  clean      nothing of the kind

When the check cannot run (no key, an error, a refusal) the verdict is "unsure" and the clip waits in Review too,
so a failed check never lets a clip through unseen. NUDITY_CHECK=0 turns the check off.
"""
import base64
import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .. import config

MAX_FRAMES = 60   # clips are 6-60 s: one frame a second covers every scene
WIDTH = 640

PROMPT = """These are frames from a Twitch stream clip (a streamer playing a video game, with their webcam in a
corner), one per second, each labelled with its time. The clip will be posted on TikTok and YouTube Shorts, which
do not allow nudity, also not of video game or animated characters.

Look at every frame, including small or background figures and the game's cutscenes, and answer with only JSON:
{"verdict": "nudity" | "revealing" | "clean", "at": [seconds], "what": "one short sentence"}

- "nudity": any frame shows exposed genitals, a bare buttocks, exposed female nipples or breasts, a fully naked
  body (even from behind or partly covered by an object or the frame edge), or a sex act.
- "revealing": no nudity, but a bikini, underwear, lingerie, a thong, a strip club or pole dancing, or a pose or
  camera angle that is clearly sexual.
- "clean": none of that. A shirtless man, normal swimwear on a beach seen from afar, violence and swearing are clean.
- "at": the times of the frames that decided it (empty for clean). When unsure between two, pick the stricter."""


def frames(src: Path, limit: int = MAX_FRAMES, width: int = WIDTH) -> list[tuple[float, bytes]]:
    """(time in seconds, JPEG bytes) for about one frame a second across the whole clip."""
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(src)],
                               capture_output=True, text=True, timeout=60).stdout.strip() or 0)
    step = max(1.0, dur / limit) if dur else 1.0
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-vf", f"fps=1/{step:.3f},scale={width}:-2",
                        "-frames:v", str(limit), "-q:v", "6", str(Path(d) / "f%03d.jpg")],
                       capture_output=True, timeout=300, check=True)
        return [(round(i * step, 1), p.read_bytes()) for i, p in enumerate(sorted(Path(d).glob("f*.jpg")))]


def _ask(shots: list[tuple[float, bytes]]) -> dict:
    content: list[dict] = []
    for t, jpg in shots:
        content.append({"type": "text", "text": f"Frame at {t:.0f}s:"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(jpg).decode()}})
    content.append({"type": "text", "text": PROMPT})
    body = {"model": config.TITLE_MODEL, "max_tokens": 2000, "messages": [{"role": "user", "content": content}]}
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
                                 headers={"x-api-key": config.ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} {e.read()[:160].decode('utf-8', 'replace')}") from None
    if data.get("stop_reason") == "refusal":
        raise RuntimeError("the model declined to look at the frames")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise RuntimeError(f"no JSON in the answer (stop: {data.get('stop_reason')})")
    out = json.loads(m.group(0))
    return out if isinstance(out, dict) else {}


def check(src: Path) -> dict:
    """{"verdict": "nudity" | "revealing" | "clean" | "unsure" | "off", "what": str, "at": [seconds]}"""
    if not config.NUDITY_CHECK:
        return {"verdict": "off", "what": "", "at": []}
    if not config.ANTHROPIC_API_KEY:
        return {"verdict": "unsure", "what": "ANTHROPIC_API_KEY is not set", "at": []}
    why = ""
    for _ in range(2):  # one more try after an error
        try:
            shots = frames(src)
            if not shots:
                raise RuntimeError("no frames could be read")
            out = _ask(shots)
            verdict = str(out.get("verdict") or "").strip().lower()
            if verdict not in ("nudity", "revealing", "clean"):
                raise RuntimeError(f"unexpected verdict {verdict!r}")
            at = [x for x in (out.get("at") or []) if isinstance(x, (int, float))][:10]
            return {"verdict": verdict, "what": str(out.get("what") or "")[:200], "at": at}
        except Exception as e:
            why = f"{type(e).__name__}: {e}"[:200]
    return {"verdict": "unsure", "what": why, "at": []}
