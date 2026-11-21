"""Run: cd posts && python -m pytest tests (or python tests/test_hook.py). Needs numpy, and ffmpeg for the last test."""
import shutil
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import captions, hook  # noqa: E402


def words_every(dur, step=0.5, loud_at=None):
    out, t = [], 0.2
    while t < dur - 0.3:
        w = "NO WAY" if loud_at and abs(t - loud_at) < 0.6 else "word"
        out.append({"w": w, "s": round(t, 2), "e": round(t + 0.35, 2)})
        t += step
    return out


def test_captions():
    assert captions.clean("Lol #KreekCraftCraft #GTA6 #gta6 x") == "Lol #kreekcraft #gta6 x"
    assert captions.clean("{hook} Watch Bobby! #GTA6 #GTAVI #BobbyVanRiper") == "{hook} Watch Bobby! #gta6 #gtavi #bobbyvanriper"
    assert captions.clean("#hahaha #shorts") == "#hahaha #shorts"


def test_candidates_find_loud_moment():
    db = np.full(300, -30.0)  # 30 s at 0.1 s
    db[220:235] = -6  # shout at 22 s
    cands = hook.candidates(db, words_every(30, loud_at=22.5), 30)
    assert cands and abs(cands[0]["t"] - 22.5) < 1.5, cands


def test_plan_long_clip_teaser_and_trim():
    p = hook.plan(45, 30, words_every(45), target=18, second=20)
    assert 14 <= p["end"] - p["start"] <= 22
    assert p["start"] < 30 < p["end"] and p["end"] >= 34
    assert p["teaser"] and abs((p["teaser"][1] - p["teaser"][0]) - 2.0) < 0.05
    assert p["main_at"] == round(p["teaser"][1] - p["teaser"][0], 2)
    assert p["length"] <= 24


def test_plan_short_clip_keeps_all_no_teaser_at_start():
    p = hook.plan(12, 1.0, [], target=18)
    assert (p["start"], p["end"]) == (0.0, 12) and p["teaser"] is None


def test_hook_avoids_overlay():
    y = hook.hook_y(2, "react", (680, 120))
    band = 2 * hook.LINE_H + 40
    assert y + band / 2 <= 680 - 20 or y - band / 2 >= 800 + 20
    assert hook.hook_y(2, "react", None) == hook.SEAM


def test_finish_renders():
    if not shutil.which("ffmpeg"):
        return
    d = Path(tempfile.mkdtemp())
    cut = d / "cut.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=18",
                    "-f", "lavfi", "-i", "sine=f=440:d=18", "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(cut)], check=True)
    p = hook.plan(18, 12, [], target=18, second=5)
    out = hook.finish(cut, d / "final.mp4", p, "He trusted the GPS one time", layout="react")
    assert abs(hook.duration(out) - p["length"]) < 0.3, (hook.duration(out), p)
    shutil.rmtree(d)


if __name__ == "__main__":
    for k, f in list(globals().items()):
        if k.startswith("test_"):
            f()
            print("ok", k)
