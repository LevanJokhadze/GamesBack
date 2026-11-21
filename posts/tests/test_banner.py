"""Split-screen layout, satisfying loops and the sponsor banner insert. Needs numpy, opencv and ffmpeg.
Run: cd posts && python -m pytest tests/test_banner.py"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import banner, hook, loops  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
HAS_FFMPEG = shutil.which("ffmpeg") is not None


def test_banner_times_follow_the_rules():
    assert banner.times(8, 5, 4) == [4.0]                 # under 10 s: the middle
    assert banner.times(24, 16, 4) == [15.0]              # 1 s before the payoff
    assert banner.times(24, 6, 4) == [10.0]               # never before 0:10
    assert banner.times(24, None, 4) == [10.0]
    assert banner.times(140, 30, 4) == [29.0, 89.0]       # again a minute later, not when under a minute is left
    assert banner.times(12, 11.8, 4) == [10.8]
    assert banner.times(11, 11.0, 4) == [10.0]            # payoff at the very end: as late as the rules allow


def test_has_alpha():
    assert banner.has_alpha("yuva444p10le") and banner.has_alpha("argb") and not banner.has_alpha("yuv420p")


def _ff(*a):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *a], check=True)


def test_split_layout_and_banner_end_to_end():
    if not HAS_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        src, bn, loop = d / "src.mp4", d / "banner.mp4", d / "loop.mp4"
        _ff("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=60:d=14", "-f", "lavfi", "-i", "sine=f=300:d=14",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src))
        _ff("-f", "lavfi", "-i", "color=c=0x00FF00:s=1280x720:r=30:d=2", "-f", "lavfi", "-i", "sine=f=900:d=2",
            "-vf", "drawbox=x=240:y=160:w=800:h=400:color=0xFF6A00:t=fill", "-c:v", "libx264", "-preset", "ultrafast",
            "-c:a", "aac", str(bn))
        loops.make("plinko", loop, secs=4)
        assert hook.resolution(loop) == (loops.W, loops.H)
        job = d / "job"
        job.mkdir()
        (job / "candidates.json").write_text(json.dumps({"source": str(src), "candidates": [{"n": 1, "start": 0, "end": 13}]}))
        (job / "transcript.json").write_text(json.dumps([{"w": "hello", "s": 1.0, "e": 1.4}]))
        subprocess.run([sys.executable, str(ROOT / "vendor/gtavi/cut.py"), str(job), "--start", "1", "--end", "13",
                        "--layout", "split", "--bottom", str(loop), "--cam", "0.70:0.05:0.28:0.30"], check=True)
        cut = job / "edits" / "clip_1.mp4"
        assert hook.resolution(cut) == (1080, 1920)
        assert abs(hook.duration(cut) - 12) < 0.3   # the looped bottom never makes the clip longer
        p = hook.plan(hook.duration(cut), 9.0, [], target=12, teaser=True)
        fin = d / "final.mp4"
        hook.finish(cut, fin, p, "test hook", layout="split")
        payoff = p["main_at"] + p["peak"]
        at = banner.times(hook.duration(fin), payoff, 2)
        assert at and at[0] >= 10
        out = d / "with_banner.mp4"
        r = banner.insert(fin, bn, out, at, width=1000)
        assert r["key"].startswith("0x00F")          # the green corners were found
        assert abs(hook.duration(out) - (hook.duration(fin) + 2 * len(at))) < 0.3
        # mid-banner, the centre of the frame is the banner's orange, the green is keyed out around it
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at[0] + 1:.2f}", "-i", str(out), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        px = lambda x, y: raw[(y * 1080 + x) * 3:(y * 1080 + x) * 3 + 3]
        r_, g_, b_ = px(540, 960)
        assert r_ > 200 and 60 < g_ < 150 and b_ < 60
        r2, g2, b2 = px(30, 960)  # left of the banner: not green screen
        assert not (g2 > 200 and r2 < 80 and b2 < 80)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
