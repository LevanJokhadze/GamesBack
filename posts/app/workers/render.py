"""Render: turn the best new moment of each campaign into a finished 9:16 clip and upload it.

download (yt-dlp) -> transcript + audio (vendored moments.py) -> payoff candidates (hook.py) -> title, on-screen hook
and payoff pick (titles.py) -> cut.py react layout over the 15-20 s around the payoff -> hook.finish: payoff teaser
first, punch-in zooms, logo overlay, text hook from 0:00 -> upload.
One clip per run, so a slow render never blocks the other workers for long.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import captions, config, db, hook
from ..integrations import blob, titles

ET = ZoneInfo("America/New_York")


def sh(cmd, cwd=None, env=None, timeout=1800):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{Path(cmd[1] if cmd[0] == sys.executable else cmd[0]).name} failed: {(r.stderr or r.stdout)[-800:]}")
    return r.stdout


def rendered_today(campaign_id: int) -> int:
    start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.one("SELECT count(*) AS n FROM clips WHERE campaign_id=%s AND created_at >= %s AND status <> 'rejected'",
                  (campaign_id, start))["n"]


def run():
    for c in db.q("SELECT * FROM campaigns WHERE active ORDER BY id"):
        if rendered_today(c["id"]) >= (c["daily_quota"] or 0):
            continue
        m = db.one("""UPDATE moments SET status='rendering', updated_at=now() WHERE id = (
                        SELECT id FROM moments WHERE campaign_id=%s AND status='new' ORDER BY views DESC LIMIT 1
                        FOR UPDATE SKIP LOCKED) RETURNING *""", (c["id"],))
        if not m:
            continue
        try:
            clip_id = render_one(c, m)
            db.q("UPDATE moments SET status='rendered', updated_at=now() WHERE id=%s", (m["id"],))
            db.log("render", f"clip #{clip_id} ready: {m['title']!r} ({m['views']} views on Twitch)")
        except Skip as e:
            db.q("UPDATE moments SET status='skipped', reason=%s, updated_at=now() WHERE id=%s", (str(e)[:500], m["id"]))
            db.log("render", f"skipped {m['external_id']}: {e}", "warn")
        except Exception as e:
            db.q("UPDATE moments SET status='failed', reason=%s, updated_at=now() WHERE id=%s", (str(e)[:500], m["id"]))
            db.log("render", f"failed {m['external_id']}: {e}", "error")
        return True  # one clip per run
    return False


SOURCE_FORMAT = "b[format_id!^=portrait][height<=1080]/b[height<=1080]/b"


class Skip(Exception):
    pass


def build(c: dict, m: dict, work: Path, screen: str | None = None) -> dict:
    """Download, transcribe, plan and edit one moment into work/final.mp4. No database or upload, so
    scripts/sample_render.py can run it on its own. Returns the final file, the texts and the edit plan."""
    (work / "src").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CLIP_DESK_HOME=str(work))
    src = work / "src" / "clip.mp4"
    # the landscape 1080p rendition (Twitch also lists "portrait-*" crops, which the react layout must not get),
    # highest resolution and frame rate first
    sh([sys.executable, "-m", "yt_dlp", "--no-warnings", "-q", "-S", "res:1080,fps", "-f", SOURCE_FORMAT,
        "-o", str(src), m["url"]], timeout=600)
    src_res = hook.resolution(src)
    sh([sys.executable, str(config.VENDOR / "moments.py"), str(src), "--top", "1", "--min", "5", "--max", "60"],
       cwd=work, env=env)
    job = next((work / "jobs").iterdir())
    words = json.loads((job / "transcript.json").read_text())
    words = [w for w in words if isinstance(w, dict) and "s" in w and "e" in w]
    transcript = " ".join(str(w.get("w", "")) for w in words)
    dur_src = hook.duration(src)
    # the campaign's streamer name, else as written in the campaign name ("KreekCraft GTA 6 Clipping" -> "KreekCraft")
    streamer = c.get("streamer_name") or next((w for w in (c["name"] or "").split() if w.lower() == (m["channel"] or "").lower()),
                    (m["channel"] or "").capitalize())
    cands = hook.candidates(hook.loudness(job / "audio.wav"), words, dur_src)
    t = titles.make(m["title"] or "", transcript, streamer, words=words, cands=cands)
    if screen:  # re-render: keep the on-screen hook the clip already has
        t["screen"] = screen
    title = t["title"] or (m["title"] or "")
    # the payoff the clip opens on: the model's pick from the candidates, else the loudest moment
    pick = cands[t["payoff"]] if t.get("payoff") is not None else (cands[0] if cands else None)
    second = next((x["t"] for x in cands if pick and x is not pick), None)
    use_teaser = c.get("teaser") is not False
    plan = hook.plan(dur_src, pick["t"] if pick else None, words, target=c.get("target_secs") or 18,
                     teaser=use_teaser, second=second)
    cmd = [sys.executable, str(config.VENDOR / "cut.py"), str(job), "--start", f"{plan['start']:.2f}",
           "--end", f"{plan['end']:.2f}", "--layout", c.get("layout") or "react", "--captions", c["captions"] or "word"]
    if c["cam_box"]:
        cmd += ["--cam", c["cam_box"]]
    r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    if "skipped:" in out:
        raise Skip(re.search(r"skipped:.*", out).group(0)[:300])
    if r.returncode != 0:
        raise RuntimeError(out[-800:])
    cut = next((job / "edits").glob("*.mp4"))
    layout = json.loads(next((job / "edits").glob("*.json")).read_text()).get("layout", "react")
    final = work / "final.mp4"
    ov = None
    if c["overlay_url"]:
        ov = work / "overlay.png"
        sh([sys.executable, "-c", "import sys,urllib.request;urllib.request.urlretrieve(sys.argv[1],sys.argv[2])",
            c["overlay_url"], str(ov)])
    hook.finish(cut, final, plan, t.get("screen") or "", layout=layout, hook_secs=c["title_secs"] or 5,
                overlay=ov, overlay_width=c["overlay_width"] or 600, overlay_y=c["overlay_y"] or 895,
                punch=c.get("punch_zoom") is not False)
    dur = round(hook.duration(final), 2)
    plan = {**plan, "source": f"{src_res[0]}x{src_res[1]}"}
    return {"final": final, "duration": dur, "title": title, "texts": t, "plan": plan, "candidates": cands}


def render_one(c: dict, m: dict) -> int:
    work = config.WORK_DIR / f"m{m['id']}"
    shutil.rmtree(work, ignore_errors=True)
    try:
        b = build(c, m, work)
        final, dur, title, t, plan, cands = b["final"], b["duration"], b["title"], b["texts"], b["plan"], b["candidates"]
        if int(plan["source"].split("x")[1]) < 720:
            db.log("render", f"{m['external_id']}: Twitch only gave a {plan['source']} source, the clip will look soft", "warn")
        thumb = work / "thumb.jpg"
        sh(["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(final), "-frames:v", "1", "-vf", "scale=540:-2", str(thumb)])
        slug = re.sub(r"[^a-z0-9]+", "-", (m["channel"] or "clip").lower())
        video_url = blob.upload(final, f"clips/{slug}-{m['id']}.mp4", "video/mp4")
        thumb_url = blob.upload(thumb, f"thumbs/{slug}-{m['id']}.jpg", "image/jpeg")
        caption = captions.clean(c["caption_template"].replace("{hook}", t["hook"] or title))
        # a new campaign's first clip always waits for review, so its layout, overlay and caption are checked once
        first = not db.one("SELECT 1 AS x FROM clips WHERE campaign_id=%s AND approved_at IS NOT NULL LIMIT 1", (c["id"],))
        auto = db.settings().get("auto_approve") and not t.get("needs_review") and not first
        row = db.one("""INSERT INTO clips(moment_id, campaign_id, title, caption, video_url, thumb_url, duration, status,
                          approved_at, hook_text, edit_plan)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                     (m["id"], c["id"], title, caption, video_url, thumb_url, dur, "approved" if auto else "review",
                      datetime.now(timezone.utc) if auto else None, t.get("screen") or None,
                      json.dumps({**plan, "candidates": cands, "payoff_by": "model" if t.get("payoff") is not None else "audio"})))
        return row["id"]
    finally:
        shutil.rmtree(work, ignore_errors=True)


_rerendering: set[int] = set()


def rerendering() -> list[int]:
    return sorted(_rerendering)


def rerender(clip_id: int) -> None:
    """Render an existing clip again with the current editing (e.g. after a quality fix) and swap its video in place.
    The clip keeps its id, title, caption, on-screen hook, status and posts. Posts Buffer has already queued keep the
    video Buffer fetched when they were scheduled. Runs in the caller's thread; the API starts it in a background one."""
    clip = db.one("SELECT * FROM clips WHERE id=%s", (clip_id,))
    m = db.one("SELECT * FROM moments WHERE id=%s", (clip["moment_id"],))
    c = db.one("SELECT * FROM campaigns WHERE id=%s", (clip["campaign_id"],))
    work = config.WORK_DIR / f"re{clip_id}"
    shutil.rmtree(work, ignore_errors=True)
    _rerendering.add(clip_id)
    db.log("render", f"clip #{clip_id}: re-rendering with the current editing")
    try:
        b = build(c, m, work, screen=clip.get("hook_text"))
        thumb = work / "thumb.jpg"
        sh(["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(b["final"]), "-frames:v", "1", "-vf", "scale=540:-2", str(thumb)])
        slug = re.sub(r"[^a-z0-9]+", "-", (m["channel"] or "clip").lower())
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")  # new URL, so no browser or CDN serves the old file
        video_url = blob.upload(b["final"], f"clips/{slug}-{m['id']}-r{stamp}.mp4", "video/mp4")
        thumb_url = blob.upload(thumb, f"thumbs/{slug}-{m['id']}-r{stamp}.jpg", "image/jpeg")
        plan = b["plan"]
        db.q("UPDATE clips SET video_url=%s, thumb_url=%s, duration=%s, edit_plan=%s WHERE id=%s",
             (video_url, thumb_url, b["duration"],
              json.dumps({**plan, "candidates": b["candidates"], "rerendered_at": stamp}), clip_id))
        db.log("render", f"clip #{clip_id}: re-rendered ({plan.get('source', '?')} source), new video is on the dashboard")
    except Exception as e:
        db.log("render", f"clip #{clip_id}: re-render failed: {e}", "error")
        raise
    finally:
        _rerendering.discard(clip_id)
        shutil.rmtree(work, ignore_errors=True)
