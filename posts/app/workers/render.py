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
import time
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import banner, captions, config, db, hook, loops
from ..integrations import blob, safety, titles

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


SKIPS_PER_RUN = 8   # moments one run may pass over (nudity, no speech, ...) before it gives the other workers a turn


_paused: dict[int, float] = {}


DEAD_STORE = "%blob.vercel-storage.com%"  # the Vercel Blob store blocked on 22 Nov 2026; its files can't be read back


def restore_one() -> bool:
    """Re-render one waiting clip (Review or Approved) whose video sits on the blocked Vercel store, newest first,
    so it plays again from the Railway bucket. Runs before new clips; a clip that fails is marked and left alone."""
    if not blob.bucket_on():
        return False
    for clip in db.q("""SELECT cl.id, cl.campaign_id FROM clips cl WHERE cl.status IN ('review','approved')
                          AND cl.video_url LIKE %s AND cl.moment_id IS NOT NULL
                          AND (cl.error IS NULL OR cl.error NOT LIKE 'restore failed%%')
                        ORDER BY cl.created_at DESC LIMIT 20""", (DEAD_STORE,)):
        c = db.one("SELECT * FROM campaigns WHERE id=%s", (clip["campaign_id"],))
        if not c or not assets_ready(c):
            continue
        try:
            rerender(clip["id"])
        except Exception as e:
            db.q("UPDATE clips SET error=%s WHERE id=%s", (f"restore failed: {str(e)[:400]}", clip["id"]))
        return True
    return False


def run():
    if restore_one():
        return True
    for c in db.q("SELECT * FROM campaigns WHERE active ORDER BY id"):
        if rendered_today(c["id"]) >= (c["daily_quota"] or 0):
            continue
        if not assets_ready(c) or _paused.get(c["id"], 0) > time.time():
            continue
        for _ in range(SKIPS_PER_RUN):
            m = db.one("""UPDATE moments SET status='rendering', updated_at=now() WHERE id = (
                            SELECT id FROM moments WHERE campaign_id=%s AND status='new' ORDER BY views DESC LIMIT 1
                            FOR UPDATE SKIP LOCKED) RETURNING *""", (c["id"],))
            if not m:
                break
            try:
                clip_id = render_one(c, m)
                db.q("UPDATE moments SET status='rendered', updated_at=now() WHERE id=%s", (m["id"],))
                db.log("render", f"clip #{clip_id} ready: {m['title']!r} ({m['views']} views on Twitch)")
            except SourceBlocked as e:
                # back in the queue untouched, and the campaign waits an hour (new cookies end the wait on restart)
                db.q("UPDATE moments SET status='new', updated_at=now() WHERE id=%s", (m["id"],))
                _paused[c["id"]] = time.time() + 3600
                db.log("render", f"{c['name']}: paused for an hour, YouTube won't let us download Shorts: the "
                                 f"YTDLP_COOKIES sign-in expired; paste fresh cookies ({str(e)[-200:]})", "warn")
                break
            except Skip as e:
                db.q("UPDATE moments SET status='skipped', reason=%s, updated_at=now() WHERE id=%s", (str(e)[:500], m["id"]))
                db.log("render", f"skipped {m['external_id']}: {e}", "warn")
                continue  # the next best moment of this campaign
            except Exception as e:
                db.q("UPDATE moments SET status='failed', reason=%s, updated_at=now() WHERE id=%s", (str(e)[:500], m["id"]))
                db.log("render", f"failed {m['external_id']}: {e}", "error")
            return True  # one clip per run
        else:
            return True  # only skips so far: go again right away
    return False


SOURCE_FORMAT = "b[format_id!^=portrait][height<=1080]/b[height<=1080]/b"
TREND_SOURCES = ("yt_short", "ig_reel")
TREND_FORMAT = "bv*[height<=1920]+ba/b"


def ytdlp_trend_cmd(work: Path, *args: str, clients: str = "") -> list[str]:
    """yt-dlp for someone else's Short/Reel, signed in with YTDLP_COOKIES when set (YouTube bot-checks cloud IPs)."""
    cmd = [sys.executable, "-m", "yt_dlp"]
    if config.YTDLP_COOKIES:
        cmd += ["--cookies", str(cookie_file())]
    if clients:
        cmd += ["--extractor-args", f"youtube:player_client={clients}"]
    return cmd + list(args)


def cookie_file() -> Path:
    """One cookies.txt per container, kept between downloads: yt-dlp saves the cookies YouTube refreshes back into
    it, so the sign-in lasts longer than a fresh copy of YTDLP_COOKIES each time. A new YTDLP_COOKIES replaces it."""
    import hashlib
    f = config.WORK_DIR / "yt-cookies.txt"
    tag = f.with_suffix(".src")
    h = hashlib.sha1(config.YTDLP_COOKIES.encode()).hexdigest()
    if not f.exists() or not tag.exists() or tag.read_text() != h:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(config.YTDLP_COOKIES)
        tag.write_text(h)
    return f


class SourceBlocked(RuntimeError):
    """YouTube refuses every download until someone acts (cookies expired, sign-in asked): the moment is not at fault."""


BLOCKED_SIGNS = ("cookies are no longer valid", "Sign in to confirm", "account cookies")


# YouTube player clients tried in turn for a trending Short: signed in from a cloud IP, a client may list no
# video formats at all ("Requested format is not available") while another one works
TREND_CLIENTS = ("", "tv", "web_safari,mweb", "web_creator", "tv_simply")


def download_trend(work: Path, url: str, src: Path):
    errors = []
    for clients in TREND_CLIENTS if "youtube.com" in url or "youtu.be" in url else ("",):
        try:
            # warnings stay on: a failure then says why (a missing JS runtime, expired cookies, ...)
            sh(ytdlp_trend_cmd(work, "-q", "-S", "res:1920,fps", "-f", TREND_FORMAT, "--merge-output-format", "mp4",
                               "-o", str(src), url, clients=clients), timeout=600)
            if clients:
                print(f"[render] {url}: downloaded with YouTube player_client={clients}", flush=True)
            return
        except RuntimeError as e:
            errors.append(f"[{clients or 'default'}] {str(e)[-500:]}")
            print(f"[render] {url}: yt-dlp ({clients or 'default'} clients) failed: {str(e)[-1500:]}", flush=True)
    msg = " | ".join(errors)[-1800:]
    if errors and all(any(sign in e for sign in BLOCKED_SIGNS) for e in errors):
        raise SourceBlocked(msg)
    raise RuntimeError(msg)


class Skip(Exception):
    pass


def apply_title_template(c: dict, title: str) -> str:
    """The campaign's title wrapper, e.g. 'twitch: evelone2004 {title} #evelone' when a brief wants the Twitch
    channel in the Shorts title. YouTube cuts titles over 100 characters, so the model's part is shortened to fit."""
    tpl = c.get("title_template") or "{title}"
    if "{title}" not in tpl or not title:
        return title
    room = 100 - len(tpl.replace("{title}", ""))
    if len(title) > room:
        title = title[:max(room - 1, 10)].rstrip() + "…"
    return re.sub(r"\s+", " ", tpl.replace("{title}", title)).strip()


def build(c: dict, m: dict, work: Path, screen: str | None = None) -> dict:
    """Download, transcribe, plan and edit one moment into work/final.mp4. No database or upload, so
    scripts/sample_render.py can run it on its own. Returns the final file, the texts and the edit plan."""
    (work / "src").mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, CLIP_DESK_HOME=str(work))
    src = work / "src" / "clip.mp4"
    # the landscape 1080p rendition (Twitch also lists "portrait-*" crops, which the react layout must not get),
    # highest resolution and frame rate first
    trend = (m.get("source") or "") in TREND_SOURCES
    if trend:
        # someone else's Short or Reel (the trends worker): its best vertical video + audio
        download_trend(work, m["url"], src)
    else:
        sh([sys.executable, "-m", "yt_dlp", "--no-warnings", "-q", "-S", "res:1080,fps", "-f", SOURCE_FORMAT,
            "-o", str(src), m["url"]], timeout=600)
    src_res = hook.resolution(src)
    # nudity gets a TikTok strike and a YouTube age restriction: such a moment is never cut
    safe = safety.check(src)
    if safe["verdict"] == "nudity":
        at = ", ".join(f"{x:.0f}s" for x in safe["at"][:4])
        raise Skip(f"nudity{' at ' + at if at else ''}: {safe['what']}".strip(": "))
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
    t = titles.make(m["title"] or "", transcript, streamer, words=words, cands=cands, language=c.get("language") or "en")
    if screen:  # re-render: keep the on-screen hook the clip already has
        t["screen"] = screen
    t["safety"] = safe
    if safe["verdict"] in ("revealing", "unsure"):  # a person decides: never auto-approved
        t["needs_review"] = True
    if trend:  # re-cut from another creator's upload: a person always looks first
        t["needs_review"] = True
    plain = t["title"] or (m["title"] or "")
    t["hook"] = t.get("hook") or plain  # never the templated title: the caption template repeats its own wrapper
    title = apply_title_template(c, plain)
    # the payoff the clip opens on: the model's pick from the candidates, else the loudest moment
    pick = cands[t["payoff"]] if t.get("payoff") is not None else (cands[0] if cands else None)
    second = next((x["t"] for x in cands if pick and x is not pick), None)
    use_teaser = c.get("teaser") is not False
    plan = hook.plan(dur_src, pick["t"] if pick else None, words, target=c.get("target_secs") or 18,
                     teaser=use_teaser, second=second)
    want = "split" if trend else (c.get("layout") or "react")  # a vertical Short/Reel only fits the split layout
    cmd = [sys.executable, str(config.VENDOR / "cut.py"), str(job), "--start", f"{plan['start']:.2f}",
           "--end", f"{plan['end']:.2f}", "--layout", want, "--captions", c["captions"] or "word"]
    if c["cam_box"] and not trend:
        cmd += ["--cam", c["cam_box"]]
    if want == "split":
        # under the streamer: a ball race drawn for this clip (its winner crosses the line just before the end), or
        # a satisfying loop chosen by the moment, so a re-render keeps the same one
        mid = int(m.get("id") or 0)
        bottom = loops.pick(c, mid, secs=plan["end"] - plan["start"], work=work)
        cmd += ["--bottom", str(bottom), "--bottom-ss", "0" if bottom.name.startswith("race-") else f"{(mid * 7) % 30}"]
    r = subprocess.run(cmd, cwd=work, env=env, capture_output=True, text=True, timeout=1800)
    out = (r.stdout or "") + (r.stderr or "")
    if "skipped:" in out:
        raise Skip(re.search(r"skipped:.*", out).group(0)[:300])
    if r.returncode != 0:
        raise RuntimeError(out[-800:])
    cut = next((job / "edits").glob("*.mp4"))
    layout = json.loads(next((job / "edits").glob("*.json")).read_text()).get("layout", "react")
    final = work / "final.mp4"
    ov = asset_file(c["overlay_url"], ".png") if c["overlay_url"] else None
    hook.finish(cut, final, plan, t.get("screen") or "", layout=layout, hook_secs=c["title_secs"] or 5,
                overlay=ov, overlay_width=c["overlay_width"] or 600, overlay_y=c["overlay_y"] or 895,
                punch=c.get("punch_zoom") is not False)
    if c.get("banner_url"):
        # the sponsor banner (e.g. BigBalls): freeze + banner about 1 s before the payoff, never before 0:10
        bn = banner_file(c["banner_url"])
        payoff = (plan["main_at"] + plan["peak"]) if plan.get("teaser") else plan.get("peak")
        at = banner.times(hook.duration(final), payoff, banner.probe(bn)["duration"])
        with_banner = work / "final_banner.mp4"
        plan = {**plan, "banner": banner.insert(final, bn, with_banner, at, width=c.get("banner_width") or 0,
                                                key=c.get("banner_key") or "")}
        final = with_banner
    dur = round(hook.duration(final), 2)
    plan = {**plan, "source": f"{src_res[0]}x{src_res[1]}", "layout": layout}
    if trend:
        plan["from"] = {"source": m.get("source"), "url": m["url"]}
    return {"final": final, "duration": dur, "title": title, "texts": t, "plan": plan, "candidates": cands}


def asset_file(url: str, default_ext: str) -> Path:
    """A campaign's overlay or banner, downloaded once per container (banners are 15-40 MB)."""
    import hashlib
    ext = Path(url.split("?")[0]).suffix.lower()
    ext = ext if ext in (".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".webp") else default_ext
    f = config.WORK_DIR / "assets" / f"{hashlib.sha1(url.encode()).hexdigest()[:16]}{ext}"
    if not f.exists():
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".part")
        try:
            sh([sys.executable, "-c", "import sys,urllib.request;urllib.request.urlretrieve(sys.argv[1],sys.argv[2])",
                url, str(tmp)])
        except Exception as e:
            tmp.unlink(missing_ok=True)
            raise AssetMissing(f"could not download {url}: {str(e)[-300:]}") from e
        tmp.replace(f)
    return f


def banner_file(url: str) -> Path:
    return asset_file(url, ".mp4")


class AssetMissing(RuntimeError):
    """A campaign's overlay or banner can't be downloaded (e.g. the old Vercel Blob store is blocked)."""


_asset_warned: dict[int, float] = {}


def assets_ready(c: dict) -> bool:
    """Download the campaign's overlay and banner before taking a moment, so a dead link pauses the campaign
    (with a warning at most hourly) instead of failing its best moments one by one."""
    try:
        if c.get("overlay_url"):
            asset_file(c["overlay_url"], ".png")
        if c.get("banner_url"):
            banner_file(c["banner_url"])
        _asset_warned.pop(c["id"], None)
        return True
    except AssetMissing as e:
        if time.time() - _asset_warned.get(c["id"], 0) > 3600:
            _asset_warned[c["id"]] = time.time()
            db.log("render", f"{c['name']}: paused, its overlay or banner is unreachable; upload it again on the "
                             f"Campaigns page ({e})", "warn")
        return False


def render_one(c: dict, m: dict) -> int:
    work = config.WORK_DIR / f"m{m['id']}"
    shutil.rmtree(work, ignore_errors=True)
    try:
        b = build(c, m, work)
        final, dur, title, t, plan, cands = b["final"], b["duration"], b["title"], b["texts"], b["plan"], b["candidates"]
        safe = t.get("safety") or {}
        if safe.get("verdict") == "revealing":
            db.log("render", f"{m['external_id']}: revealing but not nudity ({safe.get('what')}); the clip waits in Review", "warn")
        elif safe.get("verdict") == "unsure":
            db.log("render", f"{m['external_id']}: the nudity check could not run ({safe.get('what')}); the clip waits in Review", "warn")
        if t.get("why"):
            db.log("render", f"{m['external_id']}: the title writer's texts were not used ({t['why']}); "
                             f"the clip waits in Review with a stand-in title", "warn")
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
        # the same for a campaign's first split-screen or banner clip: a new look is checked once before it goes out
        if plan.get("layout") == "split" or plan.get("banner"):
            first = first or not db.one("""SELECT 1 AS x FROM clips WHERE campaign_id=%s AND approved_at IS NOT NULL
                                           AND edit_plan->>'layout' = %s AND (edit_plan ? 'banner') = %s LIMIT 1""",
                                        (c["id"], plan.get("layout"), bool(plan.get("banner"))))
        auto = db.settings().get("auto_approve") and not t.get("needs_review") and not first
        row = db.one("""INSERT INTO clips(moment_id, campaign_id, title, caption, video_url, thumb_url, duration, status,
                          approved_at, hook_text, edit_plan)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                     (m["id"], c["id"], title, caption, video_url, thumb_url, dur, "approved" if auto else "review",
                      datetime.now(timezone.utc) if auto else None, t.get("screen") or None,
                      json.dumps({**plan, "candidates": cands, "safety": safe,
                                  "payoff_by": "model" if t.get("payoff") is not None else "audio"})))
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
