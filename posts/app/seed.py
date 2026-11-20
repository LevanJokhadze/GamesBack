"""First-start data: the KreekCraft campaign (rules from its Content Rewards brief) and the clip already
scheduled by hand on 20 Nov 2026, so the workers never post it twice. Runs only when no campaign exists."""
from . import db

KREEK_RULES = """Content Rewards / ClipLaunch brief (read 20 Nov 2026):
- Official KreekCraft logo overlay visible for the whole clip.
- Only KreekCraft's official GTA 6 livestreams and replays (Twitch/YouTube).
- Caption must include "Watch KreekCraft on YouTube and Twitch" and #GTA6 #KreekCraft #GrandTheftAutoVI. No links.
- 40%+ Tier 1 audience (US, UK, CA, AU). Engagement 0.8%+, likes visible. Public for 30+ days.
- Strong hook in 1-2 s; no misleading titles, fake reactions, reuploads or other clippers' edits.
- Same or near-identical clip at most 5 times. TikTok, Reels and Shorts only.
- $1 per 1K views, min payout $3, max $300 per clip."""

OVERLAY = "https://2gxk4zs5dqpdbvwd.public.blob.vercel-storage.com/overlays/kreekcraft-wh79GcMOXBgWTMbeIep4UU53JHfejg.png"
CROC_VIDEO = "https://2gxk4zs5dqpdbvwd.public.blob.vercel-storage.com/clips/kreek_crocodile_bet-wqL1Bqeh0IirVdZMhGmaeuq0NiqPhS.mp4"
CROC_POSTS = [("6aff97430f5eda1a57abfaeb", "6aff8ebdece6dfde5f28a405", "buildclips12", "tiktok", "2026-11-21T00:00:00Z"),
              ("6aff9744ca85ebefb0e314ea", "6aff8e90ece6dfde5f289c8f", "clipbuilds12", "instagram", "2026-11-21T00:15:00Z"),
              ("6aff97457b0a126ceb958570", "6aff8ed9ece6dfde5f28a605", "buildclips", "youtube", "2026-11-21T00:30:00Z")]


def run():
    if db.one("SELECT 1 AS x FROM campaigns LIMIT 1"):
        return
    c = db.one("""INSERT INTO campaigns(name, url, cpm, min_payout, max_payout, channels, game, caption_template, overlay_url,
                    overlay_width, overlay_y, captions, cam_box, title_secs, daily_quota, min_clip_views, lookback_hours, rules)
                  VALUES (%s,%s,1,3,300,%s,'Grand Theft Auto VI',%s,%s,600,895,'none','0.0:0.11:0.21:0.29',5,4,150,72,%s)
                  RETURNING id""",
               ("KreekCraft GTA 6 Clipping", "https://contentrewards.com/c/campaigns/2eaa14fb-6927-4136-897e-b096e0d907d6",
                ["kreekcraft"], "{hook} Watch KreekCraft on YouTube and Twitch! #GTA6 #KreekCraft #GrandTheftAutoVI",
                OVERLAY, KREEK_RULES))
    m = db.one("""INSERT INTO moments(campaign_id, channel, external_id, title, views, duration, game, url, status)
                  VALUES (%s,'kreekcraft','TangentialFreezingTeaKlappa-3haneQVakbOUV_GJ','Crocodile',717,30,'Grand Theft Auto VI',
                  'https://clips.twitch.tv/TangentialFreezingTeaKlappa-3haneQVakbOUV_GJ','rendered') RETURNING id""", (c["id"],))
    cl = db.one("""INSERT INTO clips(moment_id, campaign_id, title, caption, video_url, duration, status, approved_at)
                   VALUES (%s,%s,%s,%s,%s,30,'scheduled',now()) RETURNING id""",
                (m["id"], c["id"], "He bet a free copy of GTA 6 he would NOT get eaten by a crocodile",
                 "He said there's NO way a crocodile gets him 🐊 Watch KreekCraft on YouTube and Twitch! #GTA6 #KreekCraft #GrandTheftAutoVI",
                 CROC_VIDEO))
    for bid, ch, name, svc, due in CROC_POSTS:
        db.q("""INSERT INTO posts(clip_id, campaign_id, channel_id, channel_name, service, buffer_post_id, due_at, status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'scheduled') ON CONFLICT (buffer_post_id) DO NOTHING""",
             (cl["id"], c["id"], ch, name, svc, bid, due))
    db.log("seed", "KreekCraft campaign created; crocodile clip imported with its 3 scheduled posts")
