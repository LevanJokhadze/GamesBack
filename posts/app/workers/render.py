"""Render: turn the best new moment of each campaign into a finished 9:16 clip and upload it.

download (yt-dlp) -> transcript (vendored moments.py) -> title -> cut.py react layout -> logo overlay -> upload.
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

from .. import config, db
from ..integrations import blob, titles

ET = ZoneInfo("America/New_York")


def sh(cmd, cwd=None, env=None, timeout=1800):
    r = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"{Path(cmd[1] if cmd[0] == sys.executable else cmd[0]).name} failed: {(r.stderr or r.stdout)[-800:]}")
    return r.stdout


def rendered_today(campaign_id: int) -> int:
    start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    return db.one("SELECT count(*) AS n FROM clips WHERE campaign_id=%s AND created_at >= %s",
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


class Skip(Exception):
    pass


def render_one(c: dict, m: dict) -> int:
    work = config.WORK_DIR / f"m{m['id']}"
    shutil.rmtree(work, ignore_errors=True)
    (work / "src").mkdir(parents=True)
    env = dict(os.environ, CLIP_DESK_HOME=str(work))
    try:
        src = work / "src" / "clip.mp4"
        sh([sys.executable, "-m", "yt_dlp", "--no-warnings", "-q", "-f", "best[height<=1080]", "-o", str(src), m["url"]],
           timeout=600)
        sh([sys.executable, str(config.VENDOR / "moments.py"), str(src), "--top", "1", "--min", "5", "--max", "60"],
           cwd=work, env=env)
        job = next((work / "jobs").iterdir())
        words = json.loads((job / "transcript.json").read_text())
        transcript = " ".join(str(w.get("w", "")) for w in words if isinstance(w, dict))
        # display name as written in the campaign name ("KreekCraft GTA 6 Clipping" -> "KreekCraft")
        streamer = next((w for w in (c["name"] or "").split() if w.lower() == (m["channel"] or "").lower()),
                        (m["channel"] or "").capitalize())
        t = titles.make(m["title"] or "", transcript, streamer)
        title = t["title"] or (m["title"] or "")
        dur = min(float(m["duration"] or 30), 59.0)
        cmd = [sys.executable, str(config.VENDOR / "cut.py"), str(job), "--start", "0", "--end", f"{dur:.2f}",
               "--layout", "react", "--captions", c["captions"] or "word", "--title", title,
               "--title-secs", str(c["title_secs"] or 5)]
        if c["cam_box"]:
            cmd += ["--cam", c["cam_box"]]
        r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=1800)
        out = (r.stdout or "") + (r.stderr or "")
        if "skipped:" in out:
            raise Skip(re.search(r"skipped:.*", out).group(0)[:300])
        if r.returncode != 0:
            raise RuntimeError(out[-800:])
        cut = next((job / "edits").glob("*.mp4"))
        final = work / "final.mp4"
        if c["overlay_url"]:
            ov = work / "overlay.png"
            sh([sys.executable, "-c", "import sys,urllib.request;urllib.request.urlretrieve(sys.argv[1],sys.argv[2])",
                c["overlay_url"], str(ov)])
            sh(["ffmpeg", "-v", "error", "-y", "-i", str(cut), "-i", str(ov), "-filter_complex",
                f"[1:v]scale={c['overlay_width'] or 600}:-1[o];[0:v][o]overlay=x=(W-w)/2:y={c['overlay_y'] or 895}:format=auto",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", str(final)])
        else:
            sh(["ffmpeg", "-v", "error", "-y", "-i", str(cut), "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(final)])
        thumb = work / "thumb.jpg"
        sh(["ffmpeg", "-v", "error", "-y", "-ss", "1.5", "-i", str(final), "-frames:v", "1", "-vf", "scale=540:-2", str(thumb)])
        slug = re.sub(r"[^a-z0-9]+", "-", (m["channel"] or "clip").lower())
        video_url = blob.upload(final, f"clips/{slug}-{m['id']}.mp4", "video/mp4")
        thumb_url = blob.upload(thumb, f"thumbs/{slug}-{m['id']}.jpg", "image/jpeg")
        caption = c["caption_template"].replace("{hook}", t["hook"] or title).strip()
        auto = db.settings().get("auto_approve") and not t.get("needs_review")
        row = db.one("""INSERT INTO clips(moment_id, campaign_id, title, caption, video_url, thumb_url, duration, status, approved_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                     (m["id"], c["id"], title, caption, video_url, thumb_url, dur, "approved" if auto else "review",
                      datetime.now(timezone.utc) if auto else None))
        return row["id"]
    finally:
        shutil.rmtree(work, ignore_errors=True)
