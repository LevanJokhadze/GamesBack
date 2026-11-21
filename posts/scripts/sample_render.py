"""Render sample clips with the current editing, without the database, Blob or Buffer.

    cd posts && python scripts/sample_render.py --channel bobby_vanriper --n 2 --out /tmp/samples
    python scripts/sample_render.py --url https://clips.twitch.tv/<slug> --out /tmp/samples

Picks the channel's most-viewed GTA clips of the week (6-60 s), runs the same steps as the render worker
(workers/render.py build) and writes <out>/<slug>.mp4, a contact sheet <slug>.jpg (0.3 s, the flash, the payoff,
the end) and <slug>.json (texts and edit plan). With ANTHROPIC_API_KEY set the model writes the hook; without it,
--hook (or the Twitch clip title) is used.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.integrations import titles, twitch  # noqa: E402
from app.workers import render  # noqa: E402

BOBBY = {"id": 0, "name": "BOBBY VAN RIPER GTA VI Clipping", "streamer_name": "Bobby VanRiper", "captions": "word",
         "cam_box": None, "overlay_url": None, "overlay_width": 600, "overlay_y": 895, "title_secs": 5,
         "target_secs": 18, "teaser": True, "punch_zoom": True,
         "caption_template": "{hook} Watch Bobby VanRiper live on Twitch! #GTA6 #GTAVI #BobbyVanRiper"}


def sheet(video: Path, plan: dict, out: Path):
    ts = [0.3, plan["main_at"] + 0.05 if plan.get("teaser") else 2.0, plan["zooms"][-1][0] + 0.5 if plan["zooms"] else 6,
          max(0.5, plan["length"] - 1)]
    ts = sorted({round(t, 2) for t in ts})
    files = []
    for i, t in enumerate(ts):
        f = out.parent / f".{out.stem}_{i}.jpg"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(t), "-i", str(video), "-frames:v", "1",
                        "-vf", "scale=360:-2", str(f)], check=True)
        files.append(f)
    args = sum([["-i", str(f)] for f in files], [])
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args, "-filter_complex",
                    "".join(f"[{i}:v]" for i in range(len(files))) + f"hstack=inputs={len(files)}", "-q:v", "4", str(out)],
                   check=True)
    for f in files:
        f.unlink()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="bobby_vanriper")
    ap.add_argument("--url", action="append", default=[])
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--hook", action="append", default=[], help="on-screen hook per clip when there is no API key")
    ap.add_argument("--out", default="samples")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if a.url:
        clips = [{"slug": u.rstrip("/").split("/")[-1], "url": u, "title": "", "views": 0, "duration": 0} for u in a.url]
    else:
        clips = [k for k in twitch.top_clips(a.channel, "LAST_WEEK")
                 if (k["game"] or "").startswith("Grand Theft Auto") and 6 <= (k["duration"] or 0) <= 60][: a.n]
    for i, k in enumerate(clips):
        if i < len(a.hook):  # stand-in for the model's hook when there is no key
            real = titles.make
            titles.make = lambda *x, _h=a.hook[i], **y: {**real(*x, **y), "screen": _h}
        m = {"id": i, "url": k["url"], "title": k["title"], "channel": a.channel}
        work = out / f"work-{k['slug'][:40]}"
        shutil.rmtree(work, ignore_errors=True)
        try:
            b = render.build(BOBBY, m, work)
        except render.Skip as e:
            print(json.dumps({"slug": k["slug"], "skipped": str(e)}))
            continue
        name = k["slug"][:40]
        shutil.copy(b["final"], out / f"{name}.mp4")
        sheet(out / f"{name}.mp4", b["plan"], out / f"{name}.jpg")
        info = {"slug": k["slug"], "twitch_title": k["title"], "views": k["views"], "source_secs": k["duration"],
                "final_secs": b["duration"], "screen_hook": b["texts"].get("screen"), "title": b["title"],
                "caption_hook": b["texts"].get("hook"), "plan": b["plan"], "candidates": b["candidates"]}
        (out / f"{name}.json").write_text(json.dumps(info, indent=2))
        print(json.dumps(info))
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
