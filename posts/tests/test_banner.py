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


def test_race_progress_is_a_close_race_won_just_before_the_end():
    import random
    import numpy as np
    secs = 20
    p = loops._progress(secs, random.Random(3))
    assert p.shape == (secs * loops.FPS, loops.RACERS)
    assert (np.diff(p, axis=0) >= -1e-9).all()                 # nobody goes backwards
    kf = int((secs - loops.FINISH_LEAD) * loops.FPS)
    assert abs(p[kf].max() - 1.0) < 1e-6                       # the winner is on the line at secs - FINISH_LEAD
    assert p[kf - loops.FPS].max() < 1.0                       # and not a second earlier
    assert p[0].max() < 0.01
    lead = np.argmax(p[loops.FPS * 2:kf], axis=1)
    assert np.count_nonzero(np.diff(lead)) >= 2                # the lead changes hands


def test_track_stays_inside_the_panel():
    path = loops.track_path()
    m = loops.TRACK_W / 2 + 6
    assert path[:, 0].min() >= m and path[:, 0].max() <= loops.W - m
    assert path[:, 1].min() >= m and path[:, 1].max() <= loops.H - m


def test_fit_width():
    # artwork 1200 x 820 px in a 1920 x 1080 banner (the BigBalls one): ~1040 px wide on the clip
    w = banner.fit_width((352 / 1920, 72 / 1080, 1552 / 1920, 892 / 1080), 1920, 1080)
    assert 1640 <= w <= 1680
    art_w, art_h = 1200 * w / 1920, 820 * w / 1920
    assert art_w * art_h / (1080 * 1920) >= 0.30               # the sponsor's 30 %-of-the-screen rule
    assert banner.fit_width((0, 0, 1, 1), 1080, 1920) <= 1080  # tall artwork: capped by the height


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
        # width 0: the artwork (the orange box, 800 of the banner's 1280 px) is fitted to ~96 % of the clip width
        box = banner.content_box(bn, "0x00FF00", dur=2)
        assert abs(box[0] - 240 / 1280) < 0.02 and abs(box[2] - 1040 / 1280) < 0.02
        out2 = d / "with_banner_fit.mp4"
        r = banner.insert(fin, bn, out2, at, width=0)
        assert 1600 <= r["width"] <= 1680
        raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{at[0] + 1:.2f}", "-i", str(out2), "-frames:v", "1",
                              "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
        r3, g3, b3 = px(40, 960)  # the artwork now reaches near the frame's edge
        assert r3 > 200 and 60 < g3 < 150 and b3 < 60


def test_portrait_source_in_split_layout_with_a_race():
    """A vertical Short (the trends worker's sources) cut into the split layout over a ball race."""
    if not HAS_FFMPEG:
        return
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        src = d / "short.mp4"
        _ff("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=10", "-f", "lavfi", "-i", "sine=f=300:d=10",
            "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(src))
        race = loops.pick({"bottom_kind": "race"}, 4, secs=8, work=d)
        assert race.name == "race-4.mp4" and abs(hook.duration(race) - 8.5) < 0.2
        job = d / "job"
        job.mkdir()
        (job / "candidates.json").write_text(json.dumps({"source": str(src), "candidates": [{"n": 1, "start": 0, "end": 9}]}))
        (job / "transcript.json").write_text(json.dumps([{"w": "hello", "s": 1.0, "e": 1.4}]))
        r = subprocess.run([sys.executable, str(ROOT / "vendor/gtavi/cut.py"), str(job), "--start", "1", "--end", "9",
                            "--layout", "split", "--bottom", str(race), "--bottom-ss", "0"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr[-800:]
        assert "vertical source" in r.stdout
        cut = job / "edits" / "clip_1.mp4"
        assert hook.resolution(cut) == (1080, 1920)
        assert abs(hook.duration(cut) - 8) < 0.3


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
