"""Satisfying loops for the bottom of the split-screen layout (streamer on top, a calm looping animation below).

We draw our own loops instead of re-using other creators' "satisfying" videos, so no clip is claimed for copyright:
  bounce   balls bouncing inside a ring, leaving colour trails
  plinko   balls dropping through a peg board and piling up in bins
  pendulum a pendulum wave (each pendulum a little faster than the one before, so the line keeps re-forming)
Each loop is drawn once with OpenCV (~20-60 s of CPU), cached in WORK_DIR/loops and re-used for every clip.
A campaign can list its own bottom videos instead (campaigns.bottom_urls: public MP4 links, e.g. stock footage).
"""
import hashlib
import math
import random
import subprocess
import urllib.request
from pathlib import Path

import numpy as np

from . import config

W, H = 1080, 1190      # bottom panel of the split layout (vendor/gtavi/cut.py SPLIT_TOP = 730)
FPS = 30
SECS = 40
VERSION = 1
KINDS = ("bounce", "plinko", "pendulum")
BG = (18, 13, 13)      # BGR, near black with a blue tint


def _hsv(h: float, s: float = 0.85, v: float = 1.0) -> tuple[int, int, int]:
    """Hue 0..1 -> BGR tuple."""
    import cv2
    px = np.uint8([[[int(h % 1 * 179), int(s * 255), int(v * 255)]]])
    b, g, r = cv2.cvtColor(px, cv2.COLOR_HSV2BGR)[0, 0]
    return int(b), int(g), int(r)


def _writer(out: Path) -> subprocess.Popen:
    return subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}",
                             "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                             "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)], stdin=subprocess.PIPE)


def _bounce(frames: int, rng: random.Random):
    import cv2
    cx, cy, R = W / 2, H / 2, 500
    balls = []
    for i in range(7):
        a = rng.uniform(0, 2 * math.pi)
        balls.append({"x": cx + math.cos(a) * 150, "y": cy + math.sin(a) * 150, "vx": rng.uniform(-9, 9),
                      "vy": rng.uniform(-9, 9), "c": _hsv(i / 7), "r": 26})
    canvas = np.zeros((H, W, 3), np.float32)
    g = 0.45
    for f in range(frames):
        canvas *= 0.93
        for b in balls:
            b["vy"] += g
            b["x"] += b["vx"]
            b["y"] += b["vy"]
            dx, dy = b["x"] - cx, b["y"] - cy
            d = math.hypot(dx, dy)
            if d > R - b["r"]:  # reflect off the ring, keep the speed so the motion never dies down
                nx, ny = dx / d, dy / d
                dot = b["vx"] * nx + b["vy"] * ny
                b["vx"] -= 2 * dot * nx
                b["vy"] -= 2 * dot * ny
                b["x"], b["y"] = cx + nx * (R - b["r"]), cy + ny * (R - b["r"])
            cv2.circle(canvas, (int(b["x"]), int(b["y"])), b["r"], b["c"], -1, cv2.LINE_AA)
        img = np.clip(canvas, 0, 255).astype(np.uint8)
        img[np.all(img < 20, axis=2)] = BG
        cv2.circle(img, (int(cx), int(cy)), R + 6, (235, 235, 235), 6, cv2.LINE_AA)
        for b in balls:  # bright cores on top of the trails
            cv2.circle(img, (int(b["x"]), int(b["y"])), b["r"], (255, 255, 255), 3, cv2.LINE_AA)
        yield img


def _plinko(frames: int, rng: random.Random):
    import cv2
    rows, gap, top = 13, 70, 120
    pegs = []
    for r in range(rows):
        n = r + 3
        y = top + r * gap * 0.82
        x0 = W / 2 - (n - 1) * gap / 2
        pegs += [(x0 + k * gap, y) for k in range(n)]
    floor = H - 20
    bins_top = top + rows * gap * 0.82 + 10
    nbins = rows + 2
    bw = gap
    bx0 = W / 2 - nbins * bw / 2
    piles = [0] * nbins
    settled = []  # (x, y, colour)
    balls = []
    pr, br = 8, 11
    peg_img = np.full((H, W, 3), BG, np.uint8)
    for x, y in pegs:
        cv2.circle(peg_img, (int(x), int(y)), pr, (230, 230, 230), -1, cv2.LINE_AA)
    for k in range(nbins + 1):
        x = int(bx0 + k * bw)
        cv2.line(peg_img, (x, int(bins_top)), (x, floor), (90, 90, 90), 3, cv2.LINE_AA)
    cv2.line(peg_img, (int(bx0), floor), (int(bx0 + nbins * bw), floor), (90, 90, 90), 3, cv2.LINE_AA)
    for f in range(frames):
        if f % 4 == 0:
            balls.append({"x": W / 2 + rng.uniform(-6, 6), "y": 40.0, "vx": rng.uniform(-0.6, 0.6), "vy": 0.0,
                          "c": _hsv(f / frames * 2.5)})
        if sum(piles) > 300 or (f > 0 and f % (FPS * 20) == 0):  # empty the bins now and then so it keeps going
            piles, settled = [0] * nbins, []
        img = peg_img.copy()
        keep = []
        for b in balls:
            b["vy"] += 0.35
            b["x"] += b["vx"]
            b["y"] += b["vy"]
            for px, py in pegs:
                dx, dy = b["x"] - px, b["y"] - py
                d = math.hypot(dx, dy)
                if d < pr + br:
                    nx, ny = dx / (d or 1), dy / (d or 1)
                    dot = b["vx"] * nx + b["vy"] * ny
                    if dot < 0:
                        b["vx"] = (b["vx"] - 1.3 * dot * nx) * 0.45 + rng.choice((-1, 1)) * rng.uniform(0.3, 0.9)
                        b["vx"] = max(-2.6, min(2.6, b["vx"]))
                        b["vy"] = (b["vy"] - 1.3 * dot * ny) * 0.35
                    b["x"], b["y"] = px + nx * (pr + br), py + ny * (pr + br)
            if b["y"] > bins_top:
                k = min(max(int((b["x"] - bx0) // bw), 0), nbins - 1)
                b["x"] = min(max(b["x"], bx0 + k * bw + br + 2), bx0 + (k + 1) * bw - br - 2)
                b["vx"] *= 0.5
                rest = floor - br - 2 - piles[k] * (br * 2 - 4) // 3
                if b["y"] >= rest:
                    col = piles[k] % 3
                    settled.append((int(bx0 + k * bw + bw / 2 + (col - 1) * (br * 2 - 2)), int(rest), b["c"]))
                    piles[k] += 1
                    continue
            keep.append(b)
        balls = keep
        for x, y, c in settled:
            cv2.circle(img, (x, y), br, c, -1, cv2.LINE_AA)
        for b in balls:
            cv2.circle(img, (int(b["x"]), int(b["y"])), br, b["c"], -1, cv2.LINE_AA)
        yield img


def _pendulum(frames: int, rng: random.Random):
    import cv2
    n = 16
    pivot_y, L0 = 60, 1000
    cycle = SECS  # all pendulums line up again after one loop, so the loop is seamless
    canvas = np.zeros((H, W, 3), np.float32)
    for f in range(frames):
        t = f / FPS
        canvas *= 0.86
        img_pts = []
        for k in range(n):
            osc = 18 + k  # oscillations per cycle
            ang = 0.55 * math.cos(2 * math.pi * osc * t / cycle)
            L = L0 * (18 / osc) ** 2 * 0.95
            x = W / 2 + math.sin(ang) * L * 0.9
            y = pivot_y + math.cos(ang) * L * 0.95
            y = min(y, H - 40)
            img_pts.append((int(x), int(y), _hsv(k / n)))
            cv2.circle(canvas, (int(x), int(y)), 22, _hsv(k / n), -1, cv2.LINE_AA)
        img = np.clip(canvas, 0, 255).astype(np.uint8)
        img[np.all(img < 20, axis=2)] = BG
        for x, y, c in img_pts:
            cv2.line(img, (W // 2, pivot_y), (x, y), (120, 120, 120), 1, cv2.LINE_AA)
            cv2.circle(img, (x, y), 22, c, -1, cv2.LINE_AA)
            cv2.circle(img, (x, y), 22, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.circle(img, (W // 2, pivot_y), 8, (235, 235, 235), -1, cv2.LINE_AA)
        yield img


GENERATORS = {"bounce": _bounce, "plinko": _plinko, "pendulum": _pendulum}


def make(kind: str, out: Path, secs: int = SECS, seed: int = 7) -> Path:
    """Draw one loop into out (an MP4, W x H, FPS)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp4")
    p = _writer(tmp)
    try:
        for img in GENERATORS[kind](secs * FPS, random.Random(seed)):
            p.stdin.write(img.tobytes())
    finally:
        p.stdin.close()
        p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg could not write the {kind} loop")
    tmp.replace(out)
    return out


def cache_dir() -> Path:
    return config.WORK_DIR / "loops"


def builtin(kind: str) -> Path:
    f = cache_dir() / f"{kind}-v{VERSION}.mp4"
    if not f.exists():
        make(kind, f)
    return f


def _download(url: str) -> Path:
    f = cache_dir() / f"url-{hashlib.sha1(url.encode()).hexdigest()[:16]}.mp4"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(f)
    return f


def pick(campaign: dict, key: int) -> Path:
    """The bottom video for one clip: one of the campaign's own bottom_urls, else one of our loops, chosen by `key`
    (the moment id) so a re-render keeps the same bottom."""
    urls = [u for u in (campaign.get("bottom_urls") or []) if u]
    if urls:
        try:
            return _download(urls[key % len(urls)])
        except Exception:
            pass  # a dead link must not stop the render: fall back to our own loops
    return builtin(KINDS[key % len(KINDS)])
