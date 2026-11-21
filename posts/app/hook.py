"""Hook-first editing: find the payoff of a clip, trim around it, and open on it.

Viewers decide in the first 1-2 seconds. So instead of playing the Twitch clip from its first frame, every clip:

1. finds its payoff (the loudest reaction / hype words, or the moment the title model picks from the candidates),
2. is trimmed to about `target_secs` (15-20 s) around it, with the setup before and the reaction after,
3. opens with a ~2 s teaser of that payoff, then a quick flash and the clip from the setup,
4. shows a big text hook from 0:00, placed in the safe zone and never over the campaign's overlay,
5. punches in (zooms) on the gameplay at the payoff and at the second-best moment, so it is our own edit,
   not the same raw clip every other clipper posts.

Pure planning functions (candidates, plan, hook_y) are separate from ffmpeg (finish) so they can be tested alone.
"""
import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np

W, H = 1080, 1920
SEAM = 730            # react layout (vendor/gtavi/cut.py TOP): webcam above, gameplay below
FPS = 30
# The final, uploaded file. TikTok / Reels / Shorts re-encode whatever they get, so start from a high bitrate:
# CRF 17 on the slow preset (~10-15 Mbps for 1080x1920), capped at 20 Mbps; aq-mode 3 keeps dark scenes
# (night driving, interiors) from turning into blocks.
ENCODE = ["-c:v", "libx264", "-preset", "slow", "-crf", "17", "-profile:v", "high", "-level", "4.2",
          "-maxrate", "20M", "-bufsize", "40M", "-x264-params", "aq-mode=3"]
HYPE = re.compile(r"\b(no+|no way|oh my god|omg|what|wait|bro|let'?s go+|holy|insane|crazy|yo+|wtf|dude|look|nah|run|"
                  r"go+|dead|help|stop|why|how|hey|oh+|ah+|yes+|wow)\b", re.I)
STEP = 0.1            # analysis resolution in seconds
TEASER_BEFORE, TEASER_AFTER = 0.7, 1.3   # teaser = payoff - 0.7 s .. payoff + 1.3 s (2 s)
TOP_SAFE, BOTTOM_SAFE = 230, 1380        # keep the hook out of the platforms' top bar and caption / button area


# ---------- analysis ----------

def loudness(wav: Path) -> np.ndarray:
    """Loudness in dB per STEP seconds of a 16 kHz mono wav (moments.py writes jobs/<id>/audio.wav)."""
    with wave.open(str(wav)) as w:
        rate = w.getframerate()
        x = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(1)
    k = int(rate * STEP)
    n = len(x) // k
    if n == 0:
        return np.zeros(1)
    rms = np.sqrt((x[: n * k].reshape(n, k) ** 2).mean(1) + 1e-9)
    return 20 * np.log10(rms)


def _smooth(a: np.ndarray, k: int) -> np.ndarray:
    k = max(1, min(int(k), len(a)))
    return np.convolve(a, np.ones(k) / k, mode="same")


def score(db: np.ndarray, words: list[dict]) -> np.ndarray:
    """Per-STEP excitement: loudness above the local level, plus hype words, plus fast/shouted speech."""
    n = len(db)
    local = _smooth(db, int(6 / STEP))
    jump = np.clip(db - local, 0, None)
    jump = _smooth(jump, int(0.6 / STEP))
    hype = np.zeros(n)
    rate = np.zeros(n)
    for w in words:
        i = int(float(w.get("s", 0)) / STEP)
        if 0 <= i < n:
            rate[i] += 1
            if HYPE.search(str(w.get("w", ""))):
                hype[i] += 1
            if str(w.get("w", "")).strip("!?.,").isupper() and len(str(w.get("w", ""))) > 2:
                hype[i] += 0.5
    hype = _smooth(hype, int(1.5 / STEP)) * (1.5 / STEP)
    rate = _smooth(rate, int(2 / STEP)) * (2 / STEP)  # words in the surrounding 2 s
    z = lambda a: (a - np.median(a)) / (a.std() + 1e-6) if a.std() > 1e-6 else np.zeros_like(a)
    return 1.0 * z(jump).clip(0) + 0.7 * z(hype).clip(0) + 0.3 * z(rate).clip(0) + 0.4 * z(_smooth(db, 5)).clip(0)


def candidates(db: np.ndarray, words: list[dict], dur: float, top: int = 3, gap: float = 4.0) -> list[dict]:
    """Up to `top` peak moments at least `gap` s apart, best first, each with the words said around it."""
    s = score(db, words)
    out: list[dict] = []
    for i in np.argsort(-s):
        t = round(float(i) * STEP, 1)
        if len(out) >= top:
            break
        # a peak in the first 1.5 s can't be teased (it is already the opening); one in the last 0.8 s has no reaction
        if t < 1.5 or t > dur - 0.8 or any(abs(t - c["t"]) < gap for c in out):
            continue
        text = " ".join(str(w.get("w", "")) for w in words if t - 3 <= float(w.get("s", 0)) <= t + 2)
        out.append({"t": t, "score": round(float(s[i]), 2), "text": text[:200]})
    return out


# ---------- planning ----------

def _snap_start(s: float, words: list[dict]) -> float:
    for w in words:  # never start in the middle of a word
        if float(w["s"]) < s < float(w["e"]):
            return max(0.0, float(w["s"]) - 0.15)
    return s


def _snap_end(e: float, words: list[dict], dur: float) -> float:
    for w in words:
        if float(w["s"]) < e < float(w["e"]):
            return min(dur, float(w["e"]) + 0.3)
    return e


def plan(dur: float, peak: float | None, words: list[dict], target: float = 18, teaser: bool = True,
         second: float | None = None) -> dict:
    """Where to cut the clip and what to tease.

    Returns start/end in the source clip and, in the finished clip's own timeline, the teaser (start, end in the
    *cut* clip, which plays first), the zoom moments and where the main part starts."""
    target = max(8.0, float(target or 18))
    longest = target + 4
    if peak is None:
        peak = min(dur / 2, 6.0)
    if dur <= longest:
        s, e = 0.0, dur
    else:
        s = max(0.0, peak - 0.62 * target)        # setup before the payoff ...
        e = min(dur, s + target)
        e = min(dur, max(e, peak + 4.0))         # ... and at least 4 s of reaction after it
        if e - s < target:
            s = max(0.0, e - target)
        s = _snap_start(s, words)
        e = _snap_end(e, words, dur)
        if e - s > longest:
            e = s + longest
    p = peak - s  # payoff, in the cut clip
    tz = None
    if teaser and p >= 3.0:
        ta, tb = max(0.0, p - TEASER_BEFORE), min(e - s, p + TEASER_AFTER)
        if tb - ta >= 1.2:
            tz = (round(ta, 2), round(tb, 2))
    off = (tz[1] - tz[0]) if tz else 0.0
    zooms = [(round(off + p - 0.15, 2), round(off + p + 1.6, 2), 1.16)]
    if second is not None and s + 1 < second < e - 1 and abs(second - peak) >= 3:
        q = second - s
        zooms.append((round(off + q - 0.1, 2), round(off + q + 1.1, 2), 1.10))
    if tz:
        zooms.append((0.0, round(off, 2), 1.08))  # the teaser itself is slightly zoomed, so the replay reads as new
    return {"start": round(s, 2), "end": round(e, 2), "peak": round(p, 2), "teaser": tz, "main_at": round(off, 2),
            "zooms": sorted(zooms), "length": round(off + e - s, 2)}


def wrap(text: str, width: int = 18) -> list[str]:
    lines, cur = [], ""
    for wd in text.split():
        if cur and len(cur) + 1 + len(wd) > width:
            lines.append(cur)
            cur = wd
        else:
            cur = (cur + " " + wd).strip()
    return lines + ([cur] if cur else [])


def hook_lines(text: str) -> list[str]:
    t = re.sub(r"\s+", " ", (text or "").replace("\n", " ")).strip().upper()
    lines = wrap(t, 18)
    if len(lines) > 3:  # too long for a glance: keep the first 3 lines
        lines = lines[:3]
        lines[-1] = lines[-1].rstrip(".,!?") + "…"
    return lines


LINE_H = 96


def hook_y(lines: int, layout: str, overlay: tuple[int, int] | None) -> int:
    """Centre y of the hook: on the webcam/gameplay seam (react) or upper third, moved off the overlay."""
    band = lines * LINE_H + 40
    # react: on the webcam/gameplay seam; blur: in the blurred band just above the full stream frame
    # (cut.py puts the 1080x608 frame at y 576-1184); other layouts: upper third
    y = SEAM if layout in ("react", "split") else (556 - band / 2) if layout == "blur" else 560
    if overlay:
        oy, oh = overlay
        lo, hi = y - band / 2, y + band / 2
        if hi > oy - 20 and lo < oy + oh + 20:  # would cover the campaign's overlay
            above = oy - 30 - band / 2
            below = oy + oh + 30 + band / 2
            y = above if above - band / 2 >= TOP_SAFE else below
    return int(min(max(y, TOP_SAFE + band / 2), BOTTOM_SAFE - band / 2))


def ass(lines: list[str], y: int, secs: float) -> str:
    """Big black-on-white hook box from 0:00, popping in, fading out at `secs`."""
    def ts(t):
        return f"{int(t // 3600)}:{int(t % 3600 // 60):02d}:{t % 60:05.2f}"
    body = "\\N".join(x.replace("{", "(").replace("}", ")").replace("\\", "") for x in lines)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Hook,DejaVu Sans,80,&H00000000,&H00000000,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,100,100,0,0,3,16,0,5,70,70,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 5,{ts(0)},{ts(secs)},Hook,,0,0,0,,{{\\pos({W // 2},{y})\\fad(0,200)\\fscx85\\fscy85\\t(0,140,\\fscx100\\fscy100)}}{body}
"""


def zoom_expr(zooms: list[tuple[float, float, float]]) -> str:
    """zoompan z(it): eases in over 0.12 s, holds, eases out over 0.25 s at each zoom window."""
    terms = [f"{z - 1:.3f}*clip((it-{a})/0.12,0,1)*clip(({b}-it)/0.25,0,1)" for a, b, z in zooms]
    return "1+" + "+".join(terms) if terms else "1"


# ---------- rendering ----------

def _sh(cmd: list[str], timeout: int = 1800) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed (exit {r.returncode}{', killed: out of memory?' if r.returncode == -9 else ''}): "
                           f"{(r.stderr or r.stdout)[-800:]}")
    return r.stdout


def png_size(path: Path) -> tuple[int, int]:
    out = json.loads(_sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                          "-of", "json", str(path)]))
    st = out["streams"][0]
    return int(st["width"]), int(st["height"])


def finish(cut: Path, out: Path, p: dict, hook_text: str, layout: str = "react", hook_secs: float = 5,
           overlay: Path | None = None, overlay_width: int = 600, overlay_y: int = 895, punch: bool = True) -> Path:
    """Teaser + main part, punch-in zooms on the gameplay, overlay for the whole clip, text hook from 0:00."""
    work = out.parent
    ov_box = None
    if overlay:
        ow, oh = png_size(overlay)
        ov_box = (int(overlay_y), int(round(oh * overlay_width / ow)))
    lines = hook_lines(hook_text)
    tz = p.get("teaser")
    secs = max(float(hook_secs or 5), (p["main_at"] + 1.5) if tz else 0)
    if lines:
        (work / "hook.ass").write_text(ass(lines, hook_y(len(lines), layout, ov_box), secs), encoding="utf-8")
    f = []
    inputs = ["-i", str(cut)]
    if tz:
        # the teaser reads the file a second time (own input) instead of split=2: with split, every raw frame of the
        # main part was held in memory until the teaser ended (~7 GB for an 18 s 1080x1920 clip, OOM-killed on Railway)
        inputs += ["-i", str(cut)]
        f += [f"[1:v]trim={tz[0]}:{tz[1]},setpts=PTS-STARTPTS[tv]", f"[1:a]atrim={tz[0]}:{tz[1]},asetpts=PTS-STARTPTS[ta]",
              "[0:v]setpts=PTS-STARTPTS[mv]", "[0:a]asetpts=PTS-STARTPTS[ma]",
              "[tv][ta][mv][ma]concat=n=2:v=1:a=1[cv][ca]"]
        # a short white flash where the teaser ends and the clip "rewinds" to the setup
        f += [f"[cv]drawbox=x=0:y=0:w=iw:h=ih:color=white@0.55:t=fill:enable='between(t,{p['main_at']},{p['main_at'] + 0.1})'[cf]"]
        v, a = "[cf]", "[ca]"
    else:
        v, a = "[0:v]", "[0:a]"
    # constant 30 fps before zoompan: it emits one frame per input frame at its own fps, so a 60 fps Twitch source
    # came out twice as long (slow motion)
    f += [f"{v}fps={FPS}[vr]"]
    v = "[vr]"
    if punch and p.get("zooms"):
        z = zoom_expr(p["zooms"])
        zp = f"zoompan=z='{z}':x='(iw-iw/zoom)/2':y='(ih-ih/zoom)/2':d=1:fps={FPS}"
        if layout == "react":
            f += [f"{v}split=2[z1][z2]", f"[z1]crop={W}:{SEAM}:0:0[cam]",
                  f"[z2]crop={W}:{H - SEAM}:0:{SEAM},{zp}:s={W}x{H - SEAM},setsar=1[game]", "[cam][game]vstack[vz]"]
        elif layout == "split":  # zoom the streamer, never the satisfying loop below
            f += [f"{v}split=2[z1][z2]", f"[z1]crop={W}:{SEAM}:0:0,{zp}:s={W}x{SEAM},setsar=1[cam]",
                  f"[z2]crop={W}:{H - SEAM}:0:{SEAM}[game]", "[cam][game]vstack[vz]"]
        else:
            f += [f"{v}{zp}:s={W}x{H},setsar=1[vz]"]
        v = "[vz]"
    if overlay:
        oi = len(inputs) // 2
        inputs += ["-i", str(overlay)]
        f += [f"[{oi}:v]scale={overlay_width}:-1[ov]", f"{v}[ov]overlay=x=(W-w)/2:y={overlay_y}:format=auto[vo]"]
        v = "[vo]"
    if lines:
        sub = str(work / "hook.ass").replace("\\", "/").replace(":", "\\:")
        f += [f"{v}subtitles='{sub}'[vh]"]
        v = "[vh]"
    if v == "[0:v]":
        f += ["[0:v]null[vn]"]
        v = "[vn]"
    if a == "[0:a]":
        f += ["[0:a]anull[an]"]
        a = "[an]"
    _sh(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(f), "-map", v, "-map", a,
         "-r", str(FPS), *ENCODE, "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart", str(out)])
    return out


def resolution(path: Path) -> tuple[int, int]:
    return png_size(path)


def duration(path: Path) -> float:
    out = json.loads(_sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)]))
    return float(out["format"]["duration"])
