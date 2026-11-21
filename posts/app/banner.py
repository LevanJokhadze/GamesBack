"""Sponsor banner insert (e.g. BigBalls Partners), done after hook.finish on the finished clip.

The sponsor's rules (Telegram bot @Bigballs_tiktok_bot, 22 Nov 2026): the banner sits in the centre at 30%+ of the
screen with its voiceover clearly audible, never before 0:10 (a clip under 10 s: in the middle), again every minute
in longer clips, the main video paused while it plays, at most 1.2x speed. Their tip: about 1 s before the payoff.
So at each banner time the clip freezes on its current frame, its sound stops, the banner (green screen keyed out,
or a MOV with alpha) plays over the frozen frame with its own sound, and the clip goes on where it stopped.
Every part is its own ffmpeg input (seeked), so nothing is buffered in memory across the cut.
"""
import json
import subprocess
from pathlib import Path

W, H, FPS = 1080, 1920, 30
EARLIEST = 10.0      # the rules: not before the 10th second
REPEAT = 60.0        # and again every minute
LEAD = 1.0           # this long before the payoff


def _sh(cmd: list[str], timeout: int = 1800) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed (exit {r.returncode}): {(r.stderr or r.stdout)[-800:]}")
    return r.stdout


def probe(path: Path) -> dict:
    out = json.loads(_sh(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,pix_fmt,width,height:format=duration",
                          "-of", "json", str(path)]))
    v = next((s for s in out["streams"] if s.get("codec_type") == "video"), {})
    return {"duration": float(out["format"]["duration"]), "pix_fmt": v.get("pix_fmt") or "",
            "width": int(v.get("width") or 0), "height": int(v.get("height") or 0),
            "audio": any(s.get("codec_type") == "audio" for s in out["streams"])}


def has_alpha(pix_fmt: str) -> bool:
    p = pix_fmt.lower()
    return p.startswith(("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya")) or p in ("pal8",)


def key_colour(path: Path, at: float = 0.5) -> str:
    """The banner's background colour as 0xRRGGBB: the average of its four corners at `at` seconds."""
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path), "-frames:v", "1",
                          "-vf", "scale=64:64", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, timeout=120).stdout
    if len(raw) < 64 * 64 * 3:
        return "0x00FF00"
    px = lambda x, y: raw[(y * 64 + x) * 3:(y * 64 + x) * 3 + 3]
    corners = [px(1, 1), px(62, 1), px(1, 62), px(62, 62)]
    r, g, b = (sum(c[i] for c in corners) // 4 for i in range(3))
    return f"0x{r:02X}{g:02X}{b:02X}"


def content_box(path: Path, key: str = "", alpha: bool = False, dur: float = 0) -> tuple[float, float, float, float]:
    """Where the banner's artwork is, as fractions (x0, y0, x1, y1) of its frame: the union over a few frames of the
    pixels that are not the key colour (or, for a file with alpha, not transparent). The whole frame if none."""
    sw, sh = 480, 270
    x0, y0, x1, y1 = 1.0, 1.0, 0.0, 0.0
    kc = int(key[2:], 16) if key.startswith("0x") else 0x00FF00
    kr, kg, kb = kc >> 16, (kc >> 8) & 255, kc & 255
    for t in [dur * f for f in (0.15, 0.35, 0.55, 0.75, 0.92)] if dur else [0.5]:
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(path), "-frames:v", "1",
                              "-vf", f"scale={sw}:{sh}", "-f", "rawvideo", "-pix_fmt", "rgba", "-"],
                             capture_output=True, timeout=120).stdout
        if len(raw) < sw * sh * 4:
            continue
        import numpy as np
        a = np.frombuffer(raw, np.uint8).reshape(sh, sw, 4).astype(np.int16)
        if alpha:
            on = a[:, :, 3] > 40
        else:
            on = (abs(a[:, :, 0] - kr) + abs(a[:, :, 1] - kg) + abs(a[:, :, 2] - kb)) > 70
        ys, xs = np.nonzero(on)
        if len(xs) < 50:
            continue
        x0, x1 = min(x0, xs.min() / sw), max(x1, (xs.max() + 1) / sw)
        y0, y1 = min(y0, ys.min() / sh), max(y1, (ys.max() + 1) / sh)
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else (0.0, 0.0, 1.0, 1.0)


def fit_width(box: tuple[float, float, float, float], bw: int, bh: int) -> int:
    """The banner width (px, may be wider than the clip) at which its artwork is 96 % of the clip's width, but at
    most 60 % of its height: big enough for the sponsor's 30 %-of-the-screen rule."""
    cw, ch = (box[2] - box[0]) * bw, (box[3] - box[1]) * bh
    scale = min(W * 0.96 / max(cw, 1), H * 0.6 / max(ch, 1))
    return int(bw * scale) // 2 * 2


def times(dur: float, payoff: float | None, banner_secs: float) -> list[float]:
    """Where the banners go, in the clip's own time (before any banner is added)."""
    if dur < EARLIEST:
        return [round(dur / 2, 2)]
    first = max(EARLIEST, (payoff - LEAD) if payoff is not None else EARLIEST)
    if first > dur - 0.5:  # the payoff is right at the end: banner as late as the rules allow
        first = max(EARLIEST, dur - 1.5) if dur - 1.5 >= EARLIEST else dur / 2
    out, t = [], first
    while t < dur - 0.3:
        out.append(round(t, 2))
        if dur - t < REPEAT + 1:  # under a minute left after this one: no repeat
            break
        t += REPEAT
    return out


def insert(video: Path, banner: Path, out: Path, at: list[float], width: int = 1000, key: str = "") -> dict:
    """Write `video` with `banner` inserted (freeze + banner) at each time in `at`. key: '' = auto (alpha if the file
    has it, else the corner colour), 'none' = no keying, or a colour like 0x00FF00. width: the banner's width in px
    (centred, may overflow the frame), 0 = fit its artwork to the clip's width. Returns what was done."""
    work = out.parent
    v, b = probe(video), probe(banner)
    bd = round(b["duration"], 3)
    if b["pix_fmt"] and has_alpha(b["pix_fmt"]) and key in ("", "auto"):
        keyf, how = "format=yuva420p", "alpha"
    elif key == "none":
        keyf, how = "format=yuva420p", "none"
    else:
        colour = key if key and key not in ("auto",) else key_colour(banner)
        keyf, how = f"chromakey=color={colour}:similarity=0.16:blend=0.08,despill=type=green,format=yuva420p", colour
    if not width or int(width) <= 0:
        box = content_box(banner, how, alpha=how == "alpha", dur=bd) if how != "none" else (0.0, 0.0, 1.0, 1.0)
        width = fit_width(box, b["width"] or W, b["height"] or W * 9 // 16)
    width = max(int(W * 0.5), min(int(width), W * 3)) // 2 * 2
    at = sorted(t for t in at if 0 < t < v["duration"])
    inputs, f, parts = [], [], []
    edges = [0.0] + at + [v["duration"]]
    n = 0
    for k in range(len(edges) - 1):
        a, e = edges[k], edges[k + 1]
        if e - a > 0.04:
            inputs += ["-ss", f"{a:.3f}", "-t", f"{e - a:.3f}", "-i", str(video)]
            f += [f"[{n}:v]setpts=PTS-STARTPTS,fps={FPS},scale={W}:{H},setsar=1,format=yuv420p[v{n}]",
                  f"[{n}:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo[a{n}]"]
            parts.append(n)
            n += 1
        if k < len(at):
            t = at[k]
            frame = work / f"freeze_{k}.png"
            _sh(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t - 0.04):.3f}", "-i", str(video), "-frames:v", "1",
                 str(frame)])
            fi, bi = n, n + 1
            inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{bd:.3f}", "-i", str(frame), "-i", str(banner)]
            n += 2
            f += [f"[{bi}:v]setpts=PTS-STARTPTS,fps={FPS},{keyf},scale={width}:-2:flags=lanczos[bk{k}]",
                  f"[{fi}:v]scale={W}:{H},setsar=1,format=yuv420p[fz{k}]",
                  f"[fz{k}][bk{k}]overlay=x=(W-w)/2:y=(H-h)/2:eof_action=pass:format=auto,"
                  f"trim=duration={bd:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{fi}]"]
            if b["audio"]:
                f += [f"[{bi}:a]asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,"
                      f"apad,atrim=duration={bd:.3f}[a{fi}]"]
            else:
                f += [f"anullsrc=r=48000:cl=stereo,atrim=duration={bd:.3f}[a{fi}]"]
            parts.append(fi)
    f += ["".join(f"[v{p}][a{p}]" for p in parts) + f"concat=n={len(parts)}:v=1:a=1[vo][ao]"]
    from .hook import ENCODE
    _sh(["ffmpeg", "-v", "error", "-y", *inputs, "-filter_complex", ";".join(f), "-map", "[vo]", "-map", "[ao]",
         "-r", str(FPS), *ENCODE, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
         "-movflags", "+faststart", str(out)])
    return {"at": at, "secs": bd, "width": width, "key": how}
