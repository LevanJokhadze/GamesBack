"""Email alerts over SMTP (Gmail app password): live posts to submit on Content Rewards, and failures."""
import html
import smtplib
import ssl
from email.message import EmailMessage

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
        lines += ["", "Some campaigns want the link within 30 minutes of posting.",
                  f"Then tick 'Submitted' in the dashboard: {dash}"]
        items = "".join(f'<li><b>{LABEL.get(p["service"], p["service"])}</b>: <a href="{html.escape(p["external_link"])}">'
                        f'{html.escape(p["external_link"])}</a></li>' for p in posts)
        body = (f'<p>Your clip is live: <b>{html.escape(first["title"] or "")}</b></p>'
                + (f'<p><img src="{html.escape(first["thumb_url"])}" width="160" style="border-radius:8px"></p>' if first["thumb_url"] else "")
                + f'<p>Submit each link on <a href="{html.escape(first["campaign_url"] or "")}">{html.escape(first["campaign"] or "the campaign")}</a> '
                  f'on Content Rewards, one submission per link:</p><ul>{items}</ul>'
                + f'<p>Some campaigns want the link within 30 minutes of posting. Then tick “Submitted” on '
                  f'<a href="{dash}">the dashboard</a>.</p>')
        try:
            send(f"Live now, submit on Content Rewards: {first['title']}", "\n".join(lines), body)
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
