"""Satisfying loops for the bottom of the split-screen layout (streamer on top, a calm looping animation below).

We draw our own loops instead of re-using other creators' "satisfying" videos, so no clip is claimed for copyright:
  bounce   balls bouncing inside a ring, leaving colour trails
  plinko   balls dropping through a peg board and piling up in bins
  pendulum a pendulum wave (each pendulum a little faster than the one before, so the line keeps re-forming)
  race     8 numbered balls racing down a snake track; drawn for each clip so the winner crosses the line just
           before the clip ends, with lead changes on the way (the reason to watch to the end)
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

# ---- the ball race: drawn for each clip, so the winner crosses the line just before the clip ends --------------

RACERS = 8
RACE_COLOURS = [(60, 60, 235), (235, 140, 40), (60, 200, 60), (40, 210, 240), (220, 80, 200), (240, 240, 240),
                (40, 130, 255), (200, 200, 60)]   # BGR: red, blue, green, yellow, pink, white, orange, teal
LANES = 6
TRACK_W = 118
FINISH_LEAD = 1.3   # the winner crosses this long before the clip ends, then the win is shown


def track_path(lanes: int = LANES) -> np.ndarray:
    """The snake-shaped track, top to bottom: points (x, y) about 4 px apart."""
    top, bottom = 170.0, H - 80.0
    gap = (bottom - top) / (lanes - 1)
    r = gap / 2
    left = r + TRACK_W / 2 + 14   # the U-turns stay inside the frame
    right = W - left
    pts = []
    for k in range(lanes):
        y = top + k * gap
        xs = np.arange(left, right, 4.0) if k % 2 == 0 else np.arange(right, left, -4.0)
        pts += [(x, y) for x in xs]
        if k < lanes - 1:  # a half-circle U-turn down to the next lane
            cx = right if k % 2 == 0 else left
            for a in np.linspace(-math.pi / 2, math.pi / 2, int(math.pi * r / 4)):
                pts.append((cx + math.cos(a) * r * (1 if k % 2 == 0 else -1), y + r + math.sin(a) * r))
    return np.array(pts, np.float32)


def _progress(secs: float, rng: random.Random) -> np.ndarray:
    """Each racer's share of the track at every frame (frames x RACERS), the winner at 1.0 at secs - FINISH_LEAD.
    Speeds wander (a few slow sine waves each), so the lead changes hands; a seed is kept only when the race stays
    close: several lead changes, the final leader not ahead at 75 % of the race, and a narrow finish."""
    n = int(round(secs * FPS))
    t = np.arange(n) / FPS
    tf = max(3.0, secs - FINISH_LEAD)
    best, best_score = None, -1e9
    for _ in range(60):
        sp = np.ones((n, RACERS))
        for i in range(RACERS):
            for _k in range(3):
                f, a, ph = rng.uniform(0.04, 0.22), rng.uniform(0.08, 0.2), rng.uniform(0, 2 * math.pi)
                sp[:, i] += a * np.sin(2 * math.pi * f * t + ph)
        sp = np.clip(sp, 0.35, None)
        sp[: int(FPS * 0.6)] *= np.linspace(0.2, 1, int(FPS * 0.6))[:, None]  # a running start
        dist = np.cumsum(sp, axis=0)
        kf = min(n - 1, int(tf * FPS))
        win = int(np.argmax(dist[kf]))
        p = dist / dist[kf, win]
        lead = np.argmax(p[int(FPS * 2):kf], axis=1)
        changes = int(np.count_nonzero(np.diff(lead)))
        k75 = int(kf * 0.75)
        comeback = int(np.argmax(p[k75])) != win
        margin = 1 - np.sort(p[kf])[-2]
        score = min(changes, 6) + (3 if comeback else 0) - margin * 40
        if score > best_score:
            best, best_score = p, score
        if changes >= 4 and comeback and margin < 0.03:
            break
    return np.clip(best, 0, 1.06)


def _race(frames: int, rng: random.Random):
    import cv2
    secs = frames / FPS
    path = track_path()
    seg = np.r_[0, np.cumsum(np.hypot(*np.diff(path, axis=0).T))]
    total = seg[-1]
    tang = np.gradient(path, axis=0)
    tang /= np.maximum(np.hypot(tang[:, 0], tang[:, 1]), 1e-6)[:, None]
    normal = np.stack([-tang[:, 1], tang[:, 0]], axis=1)
    prog = _progress(secs, rng)
    order0 = list(range(RACERS))
    rng.shuffle(order0)
    offsets = {i: (k - (RACERS - 1) / 2) * (TRACK_W - 44) / (RACERS - 1) for k, i in enumerate(order0)}

    bg = np.full((H, W, 3), BG, np.uint8)
    poly = path.astype(np.int32).reshape(-1, 1, 2)
    cv2.polylines(bg, [poly], False, (70, 64, 64), TRACK_W + 14, cv2.LINE_AA)
    cv2.polylines(bg, [poly], False, (42, 36, 36), TRACK_W, cv2.LINE_AA)
    for k in range(0, len(path), 24):  # dashes down the middle, so the speed reads
        a, b = path[k], path[min(k + 10, len(path) - 1)]
        cv2.line(bg, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), (78, 70, 70), 3, cv2.LINE_AA)
    # start line and a chequered finish line across the track
    for idx, chequer in ((0, False), (len(path) - 1, True)):
        c, nrm = path[idx], normal[idx]
        for s in range(-TRACK_W // 2, TRACK_W // 2, 14):
            for row in range(2 if chequer else 1):
                col = (240, 240, 240) if not chequer or ((s // 14 + row) % 2 == 0) else (20, 20, 20)
                p0 = c + nrm * s + tang[idx] * (row * 14 - (7 if chequer else 0))
                p1 = c + nrm * (s + 14) + tang[idx] * ((row + 1) * 14 - (7 if chequer else 0))
                cv2.rectangle(bg, (int(min(p0[0], p1[0])), int(min(p0[1], p1[1]))),
                              (int(max(p0[0], p1[0])), int(max(p0[1], p1[1]))), col, -1)
    fx, fy = path[-1]
    cv2.putText(bg, "FINISH", (int(min(max(fx - 70, 20), W - 160)), int(fy) - TRACK_W // 2 - 14), cv2.FONT_HERSHEY_DUPLEX, 1.0, (235, 235, 235), 2,
                cv2.LINE_AA)

    trail = np.zeros((H, W, 3), np.uint8)
    confetti: list[list] = []
    done_at: dict[int, int] = {}
    for f in range(frames):
        p = prog[min(f, len(prog) - 1)]
        trail = cv2.convertScaleAbs(trail, alpha=0.84)
        pos = {}
        for i in range(RACERS):
            d = min(p[i], 1.0) * total
            k = int(np.searchsorted(seg, d).clip(1, len(seg) - 1))
            u = (d - seg[k - 1]) / max(seg[k] - seg[k - 1], 1e-6)
            c = path[k - 1] * (1 - u) + path[k] * u + normal[k] * offsets[i]
            pos[i] = (int(c[0]), int(c[1]))
            cv2.circle(trail, pos[i], 15, RACE_COLOURS[i], -1, cv2.LINE_AA)
            if p[i] >= 1.0 and i not in done_at:
                done_at[i] = f
                if len(done_at) == 1:  # the winner: a burst of confetti in its colour
                    for _ in range(140):
                        a, v = rng.uniform(0, 2 * math.pi), rng.uniform(4, 16)
                        confetti.append([fx, fy, math.cos(a) * v, math.sin(a) * v - 6, RACE_COLOURS[i], 50])
        img = cv2.max(bg, cv2.convertScaleAbs(trail, alpha=0.55))  # trails glow over the dark track
        ranking = sorted(range(RACERS), key=lambda i: (done_at.get(i, 10 ** 9), -p[i]))
        for i in sorted(range(RACERS), key=lambda i: p[i]):  # the leader drawn last, on top
            cv2.circle(img, pos[i], 20, RACE_COLOURS[i], -1, cv2.LINE_AA)
            cv2.circle(img, pos[i], 20, (255, 255, 255), 2, cv2.LINE_AA)
            label = str(i + 1)
            cv2.putText(img, label, (pos[i][0] - 7, pos[i][1] + 7), cv2.FONT_HERSHEY_DUPLEX, 0.62, (20, 20, 20), 2,
                        cv2.LINE_AA)
        lead = ranking[0]
        if lead in done_at:  # the winner wears a pulsing gold ring
            r = 30 + int(6 * math.sin((f - done_at[lead]) / 3))
            cv2.circle(img, pos[lead], r, (40, 200, 255), 4, cv2.LINE_AA)
        # HUD: the race's progress (the leader's) and the top three
        bar_w = W - 220
        cv2.rectangle(img, (40, 40), (40 + bar_w, 64), (70, 64, 64), -1)
        cv2.rectangle(img, (40, 40), (40 + int(bar_w * min(p[lead], 1.0)), 64), RACE_COLOURS[lead], -1)
        cv2.putText(img, f"{int(min(p[lead], 1.0) * 100)}%", (52 + bar_w, 62), cv2.FONT_HERSHEY_DUPLEX, 0.9,
                    (235, 235, 235), 2, cv2.LINE_AA)
        for k, i in enumerate(ranking[:3]):
            x = 40 + k * 120
            cv2.circle(img, (x + 18, 100), 16, RACE_COLOURS[i], -1, cv2.LINE_AA)
            cv2.putText(img, str(i + 1), (x + 12, 106), cv2.FONT_HERSHEY_DUPLEX, 0.55, (20, 20, 20), 2, cv2.LINE_AA)
            cv2.putText(img, ("1st", "2nd", "3rd")[k], (x + 42, 109), cv2.FONT_HERSHEY_DUPLEX, 0.8, (220, 220, 220), 2,
                        cv2.LINE_AA)
        keep = []
        for c in confetti:
            c[0] += c[2]
            c[1] += c[3]
            c[3] += 0.5
            c[5] -= 1
            cv2.rectangle(img, (int(c[0]), int(c[1])), (int(c[0]) + 9, int(c[1]) + 5), c[4], -1)
            if c[5] > 0:
                keep.append(c)
        confetti = keep
        yield img


def make_race(out: Path, secs: float, seed: int = 7) -> Path:
    """A ball race exactly `secs` long whose winner crosses the line FINISH_LEAD s before the end."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part.mp4")
    p = _writer(tmp)
    try:
        for img in _race(int(round(secs * FPS)), random.Random(seed)):
            p.stdin.write(img.tobytes())
    finally:
        p.stdin.close()
        p.wait()
    if p.returncode != 0:
        raise RuntimeError("ffmpeg could not write the race")
    tmp.replace(out)
    return out


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


BOTTOM_KINDS = ("race", "mix") + KINDS   # campaigns.bottom_kind; "mix" = bounce/plinko/pendulum in turn


def pick(campaign: dict, key: int, secs: float | None = None, work: Path | None = None) -> Path:
    """The bottom video for one clip: one of the campaign's own bottom_urls, else its bottom_kind: a ball race drawn
    for this clip (`secs` long, into `work`), or one of our loops chosen by `key` (the moment id) so a re-render
    keeps the same bottom."""
    urls = [u for u in (campaign.get("bottom_urls") or []) if u]
    if urls:
        try:
            return _download(urls[key % len(urls)])
        except Exception:
            pass  # a dead link must not stop the render: fall back to our own loops
    kind = campaign.get("bottom_kind") or "race"
    if kind == "race" and secs:
        return make_race((work or cache_dir()) / f"race-{key}.mp4", secs + 0.5, seed=key)
    if kind in KINDS:
        return builtin(kind)
    return builtin(KINDS[key % len(KINDS)])
