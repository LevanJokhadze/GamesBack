"""Email alerts over SMTP (Gmail app password): live posts to submit on Content Rewards, failures, and upload
reminders for accounts in manual mode."""
import html
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from . import config, db


def enabled() -> bool:
    return bool(config.SMTP_HOST and config.SMTP_USER and config.SMTP_PASSWORD and config.ALERT_EMAIL)


def send(subject: str, text: str, html_body: str | None = None) -> None:
    m = EmailMessage()
    m["From"] = f"{config.MAIL_FROM_NAME} <{config.MAIL_FROM or config.SMTP_USER}>"
    m["To"] = config.ALERT_EMAIL
    m["Subject"] = subject
    m.set_content(text)
    if html_body:
        m.add_alternative(html_body, subtype="html")
    ctx = ssl.create_default_context()
    if int(config.SMTP_PORT) == 465:
        with smtplib.SMTP_SSL(config.SMTP_HOST, 465, context=ctx, timeout=30) as s:
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(m)
    else:
        with smtplib.SMTP(config.SMTP_HOST, int(config.SMTP_PORT), timeout=30) as s:
            s.starttls(context=ctx)
            s.login(config.SMTP_USER, config.SMTP_PASSWORD)
            s.send_message(m)


LABEL = {"tiktok": "TikTok", "instagram": "Instagram Reels", "youtube": "YouTube Shorts"}


def live_posts():
    """One email per clip once its posts are live: the links to submit on Content Rewards."""
    if not enabled():
        return 0
    rows = db.q("""SELECT p.*, c.title, c.thumb_url, ca.name AS campaign, ca.url AS campaign_url FROM posts p
                   JOIN clips c ON c.id = p.clip_id LEFT JOIN campaigns ca ON ca.id = p.campaign_id
                   WHERE p.status = 'sent' AND p.external_link IS NOT NULL AND p.notified_at IS NULL
                   ORDER BY p.clip_id, p.service""")
    by_clip: dict[int, list[dict]] = {}
    for r in rows:
        by_clip.setdefault(r["clip_id"], []).append(r)
    sent = 0
    for clip_id, posts in by_clip.items():
        first = posts[0]
        dash = f"{config.DASHBOARD_URL}/#posts"
        lines = [f"Your clip is live: {first['title']}", "",
                 f"Submit each link on Content Rewards ({first['campaign']}), one submission per link:",
                 first["campaign_url"] or "", ""]
        lines += [f"- {LABEL.get(p['service'], p['service'])}: {p['external_link']}" for p in posts]
        auto = db.settings().get("auto_submit")
        lines += ["", "Auto-submit is on: the pipeline submits these now. You'll get another email only if it fails."
                  if auto else "Some campaigns want the link within 30 minutes of posting.",
                  f"Status per post: {dash}" if auto else f"Then tick 'Submitted' in the dashboard: {dash}"]
        items = "".join(f'<li><b>{LABEL.get(p["service"], p["service"])}</b>: <a href="{html.escape(p["external_link"])}">'
                        f'{html.escape(p["external_link"])}</a></li>' for p in posts)
        body = (f'<p>Your clip is live: <b>{html.escape(first["title"] or "")}</b></p>'
                + (f'<p><img src="{html.escape(first["thumb_url"])}" width="160" style="border-radius:8px"></p>' if first["thumb_url"] else "")
                + f'<p>Submit each link on <a href="{html.escape(first["campaign_url"] or "")}">{html.escape(first["campaign"] or "the campaign")}</a> '
                  f'on Content Rewards, one submission per link:</p><ul>{items}</ul>'
                + (f'<p><b>Auto-submit is on</b>: the pipeline submits these now. You’ll get another email only if it '
                   f'fails. Status per post on <a href="{dash}">the dashboard</a>.</p>' if auto else
                   f'<p>Some campaigns want the link within 30 minutes of posting. Then tick “Submitted” on '
                   f'<a href="{dash}">the dashboard</a>.</p>'))
        if auto:
            lines[2] = f"Being submitted automatically to {first['campaign']}:"
        try:
            send(f"Live now{' (auto-submitting)' if auto else ', submit on Content Rewards'}: {first['title']}", "\n".join(lines), body)
        except Exception as e:
            db.log("notify", f"email for clip #{clip_id} failed: {e}", "error")
            return sent
        db.q("UPDATE posts SET notified_at = now() WHERE id = ANY(%s)", ([p["id"] for p in posts],))
        db.log("notify", f"emailed live links for clip #{clip_id} ({len(posts)} posts)")
        sent += 1
    return sent


def failed_posts():
    if not enabled():
        return 0
    rows = db.q("""SELECT p.*, c.title FROM posts p JOIN clips c ON c.id = p.clip_id
                   WHERE p.status = 'error' AND p.notified_at IS NULL""")
    for p in rows:
        try:
            send(f"Post failed on {LABEL.get(p['service'], p['service'])}: {p['title']}",
                 f"Buffer could not publish clip #{p['clip_id']} on {p['service']} ({p['channel_name']}).\n"
                 f"Reason: {p['error']}\n\nDashboard: {config.DASHBOARD_URL}/#posts")
            db.q("UPDATE posts SET notified_at = now() WHERE id = %s", (p["id"],))
        except Exception as e:
            db.log("notify", f"failure email for post {p['id']} failed: {e}", "error")
            break
    return len(rows)


UPLOAD_PAGE = {"tiktok": "https://www.tiktok.com/upload", "instagram": "https://www.instagram.com/",
               "youtube": "https://studio.youtube.com/"}
TBILISI = "Asia/Tbilisi"


def _download(url: str) -> str:
    # Vercel Blob serves the file as an attachment with ?download=1, so phones save it instead of playing it
    return url + ("&" if "?" in url else "?") + "download=1"


def _when(t, tz: str | None = None) -> str:
    """Tbilisi time plus the audience's own time (the account's geo timezone, default US Eastern)."""
    tz = tz or "America/New_York"
    label = "US Eastern" if tz == "America/New_York" else tz.split("/")[-1].replace("_", " ")
    return (f"{t.astimezone(ZoneInfo(TBILISI)):%a %d %b, %H:%M} Tbilisi "
            f"({t.astimezone(ZoneInfo(tz)):%H:%M} {label})")


def manual_day(account: dict) -> int:
    since = account.get("manual_since") or account.get("created_at")
    return max(1, (datetime.now(timezone.utc) - since).days + 1) if since else 1


def manual_upload(account: dict, clip: dict, slot, tz: str | None = None) -> None:
    """'Time to upload' email for a manual account: download link, caption, platform and username."""
    pf = LABEL.get(account["platform"], account["platform"])
    handle = "@" + account["handle"]
    dl = _download(clip["video_url"])
    caption = clip["caption"] or ""
    title = clip["title"] or ""
    day = manual_day(account)
    dash = f"{config.DASHBOARD_URL}/#accounts"
    lines = [f"It's time to upload a video on {pf}.", "",
             f"Platform: {pf}", f"Account: {handle}" + (f" ({account['profile_url']})" if account.get("profile_url") else ""),
             f"Post at: {_when(slot, tz)}", "",
             f"Download the video: {dl}", ""]
    if account["platform"] in ("youtube", "tiktok") and title:
        lines += ["Title:", title, ""]
    lines += ["Caption (text and hashtags, paste as is):", caption, "",
              f"Upload here: {UPLOAD_PAGE.get(account['platform'], '')}", "",
              f"Day {day} of manual uploads for this account." + (" It's been a week: switch it to automatic on the dashboard when you're ready." if day > 7 else ""),
              f"After posting, add the link on the Accounts page so it counts for Content Rewards: {dash}"]
    e = html.escape
    title_html = (f'<p style="margin:12px 0 4px"><b>Title</b></p><pre style="white-space:pre-wrap;font-family:inherit;'
                  f'background:#f4f4f5;padding:10px;border-radius:8px;margin:0">{e(title)}</pre>'
                  if account["platform"] in ("youtube", "tiktok") and title else "")
    body = (f'<p style="font-size:16px"><b>It’s time to upload a video on {e(pf)}.</b></p>'
            f'<table cellpadding="4" style="border-collapse:collapse">'
            f'<tr><td style="color:#666">Platform</td><td><b>{e(pf)}</b></td></tr>'
            f'<tr><td style="color:#666">Account</td><td><b>{e(handle)}</b>'
            + (f' (<a href="{e(account["profile_url"])}">profile</a>)' if account.get("profile_url") else "") + '</td></tr>'
            f'<tr><td style="color:#666">Post at</td><td>{e(_when(slot, tz))}</td></tr></table>'
            + (f'<p><img src="{e(clip["thumb_url"])}" width="160" style="border-radius:8px"></p>' if clip.get("thumb_url") else "")
            + f'<p><a href="{e(dl)}" style="display:inline-block;background:#111;color:#fff;padding:10px 16px;border-radius:8px;'
              f'text-decoration:none">Download the video</a></p>'
            + title_html
            + f'<p style="margin:12px 0 4px"><b>Caption</b> (text and hashtags, paste as is)</p>'
              f'<pre style="white-space:pre-wrap;font-family:inherit;background:#f4f4f5;padding:10px;border-radius:8px;margin:0">{e(caption)}</pre>'
            + f'<p>Upload here: <a href="{e(UPLOAD_PAGE.get(account["platform"], ""))}">{e(UPLOAD_PAGE.get(account["platform"], ""))}</a></p>'
            + f'<p style="color:#666">Day {day} of manual uploads for this account.'
            + (" It’s been a week: switch it to automatic on the dashboard when you’re ready." if day > 7 else "")
            + f' After posting, add the link on <a href="{dash}">the Accounts page</a> so it counts for Content Rewards.</p>')
    send(f"Time to upload on {pf} {handle}: {title or clip['id']}", "\n".join(lines), body)


def manual_week(account: dict) -> None:
    pf = LABEL.get(account["platform"], account["platform"])
    dash = f"{config.DASHBOARD_URL}/#accounts"
    send(f"{pf} @{account['handle']}: one week of manual uploads",
         f"@{account['handle']} on {pf} has been on manual uploads for 7 days.\n\n"
         f"When you're ready, switch it to Automatic on the Accounts page ({dash}). It needs its Buffer channel linked; "
         f"then the pipeline posts to it at the usual slots and the upload emails stop.\n\nNothing changes until you switch it.")


def _tbilisi(t) -> str:
    return f"{t.astimezone(ZoneInfo(TBILISI)):%H:%M} Tbilisi ({t.astimezone(ZoneInfo(TBILISI)):%a %d %b})"


def submit_failed(post: dict, error: str, screenshot: str | None) -> None:
    """Auto-submit is struggling with this post: everything needed to submit it by hand before the deadline."""
    if not enabled():
        return
    pf = LABEL.get(post["service"], post["service"])
    deadline = post.get("submit_deadline")
    left = ""
    if deadline:
        mins = int((deadline - datetime.now(timezone.utc)).total_seconds() // 60)
        left = f"{mins} min left" if mins > 0 else "the deadline has passed, submit anyway"
    page = post.get("submit_url") or post.get("campaign_url") or ""
    dash = f"{config.DASHBOARD_URL}/#posts"
    lines = [f"Auto-submit failed for a live {pf} post. Submit it by hand" + (f" by {_tbilisi(deadline)} ({left})." if deadline else "."),
             "", f"Post link: {post['external_link']}", f"Campaign: {post.get('campaign') or ''} {page}", "",
             f"Why: {error}"] + ([f"Screenshot: {screenshot}"] if screenshot else []) +             ["", "The pipeline keeps retrying until the deadline. After submitting by hand, tick it on the dashboard: " + dash]
    e = html.escape
    body = (f'<p style="font-size:16px"><b>Auto-submit failed: submit this {e(pf)} post by hand'
            + (f' by {e(_tbilisi(deadline))}</b> ({e(left)})' if deadline else '</b>') + '.</p>'
            f'<p><a href="{e(page)}" style="display:inline-block;background:#111;color:#fff;padding:10px 16px;border-radius:8px;'
            f'text-decoration:none">Open the campaign</a></p>'
            f'<p>Post link (paste this):</p><pre style="background:#f4f4f5;padding:10px;border-radius:8px">{e(post["external_link"])}</pre>'
            f'<p style="color:#666">Why: {e(error)}' + (f' · <a href="{e(screenshot)}">screenshot</a>' if screenshot else "") + '</p>'
            f'<p style="color:#666">It keeps retrying until the deadline. After submitting by hand, tick it on '
            f'<a href="{dash}">the dashboard</a>.</p>')
    send(f"Submit by hand{' by ' + _tbilisi(deadline) if deadline else ''}: {pf} post of {post.get('title') or 'clip'}",
         "\n".join(lines), body)


def session_expired(error: str) -> None:
    """The saved Content Rewards session stopped working: at most one email a day."""
    import time
    if not enabled() or time.time() - float(db.settings().get("session_mail_at") or 0) < 86400:
        return
    db.set_setting("session_mail_at", time.time())
    dash = f"{config.DASHBOARD_URL}/#settings"
    try:
        send("Auto-submit needs you: sign in to Content Rewards again",
             f"The saved Content Rewards session no longer works ({error}).\n\nOpen contentrewards.com in your browser, "
             f"sign in, export the cookies again and paste them under Auto-submit on the Settings page: {dash}\n\n"
             f"Until then, posts that go live are retried and you get an email for each one to submit by hand.")
    except Exception as e:
        db.log("notify", f"session email failed: {e}", "error")
