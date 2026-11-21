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


# --- 21 Nov 2026: KreekCraft cut his rate to $0.50 per 1K mid-campaign; switch to Bobby VanRiper's GTA VI campaign ---
SWITCH_KEY = "campaign_switch_2026_11_21"

BOBBY_RULES = """Content Rewards / Clipping Nation brief (read 21 Nov 2026):
- "Get paid to clip Bobby VanRiper's GTA VI content." Source: twitch.tv/bobby_vanriper (also youtube.com/@bobbyvanriper).
- $1.50 per 1K views, min payout $2.25 (1,500 views), max $150 per clip. Budget $1,000 ($35 used on 21 Nov).
- TikTok, Instagram and YouTube. Content Rewards rules: post from a connected account, submit within 30 min.
- The brief's reference files (2) open only after joining: check them for a required overlay or caption line and
  set overlay_url / caption_template here if they ask for one."""

BOESHI_RULES = """Content Rewards / ClipLaunch brief (read 21 Nov 2026). Backup campaign, off until turned on:
- Boeshi's funniest GTA 6 Twitch moments (reactions, police chases, chaos); drive viewers back to his live streams.
- Source: twitch.tv/darealboeshi. $1 per 1K views, min payout $3, max $300. Budget $1,750 ($47 used on 21 Nov).
- TikTok, Instagram and YouTube. Reference file (1) opens after joining."""


def switch_campaign():
    """Once: mark KreekCraft ended (its data and posts stay; live posts are still submitted and counted at the new
    rate) and add Bobby VanRiper as the active campaign, with Boeshi as an inactive backup."""
    if db.settings().get(SWITCH_KEY):
        return
    db.q("""UPDATE campaigns SET cpm=0.5, min_payout=2.5, max_payout=500, active=FALSE, ended_at=now(),
              streamer_name='KreekCraft',
              end_reason='Rate cut from $1 to $0.50 per 1K views on 20 Nov 2026, mid-campaign. Stopped cutting new clips.'
            WHERE 'kreekcraft' = ANY(channels) AND ended_at IS NULL""")
    add = [("BOBBY VAN RIPER GTA VI Clipping", "https://contentrewards.com/c/campaigns/f583b069-ced7-49e5-9ed3-2d3a13d6df0c",
            ["bobby_vanriper"], "Bobby VanRiper", 1.5, 2.25, 150,
            "{hook} Watch Bobby VanRiper live on Twitch! #GTA6 #GTAVI #BobbyVanRiper", BOBBY_RULES, True),
           ("Boeshi GTA 6 Clipping", "https://contentrewards.com/c/campaigns/5c01ecdb-a148-4489-ba26-5aadbbb5bf69",
            ["darealboeshi"], "Boeshi", 1, 3, 300,
            "{hook} Catch Boeshi live on Twitch! #GTA6 #GTAVI #Boeshi", BOESHI_RULES, False)]
    for name, url, channels, streamer, cpm, mn, mx, caption, rules, active in add:
        if db.one("SELECT 1 AS x FROM campaigns WHERE url=%s", (url,)):
            continue
        # no overlay and no fixed webcam box (found per clip); word captions; 3 clips a day to match posting slots
        db.q("""INSERT INTO campaigns(name, url, cpm, min_payout, max_payout, channels, streamer_name, game,
                  caption_template, overlay_url, captions, cam_box, title_secs, daily_quota, min_clip_views, lookback_hours,
                  rules, active, submit_window_minutes)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'Grand Theft Auto VI',%s,NULL,'word',NULL,5,3,50,72,%s,%s,30)""",
             (name, url, cpm, mn, mx, channels, streamer, caption, rules, active))
    # accounts limited to KreekCraft clips also take the new streamer's
    db.q("""UPDATE accounts SET streamers = array_append(streamers, 'bobby_vanriper')
            WHERE EXISTS (SELECT 1 FROM unnest(streamers) s WHERE lower(s) = 'kreekcraft')
              AND NOT EXISTS (SELECT 1 FROM unnest(streamers) s WHERE lower(s) = 'bobby_vanriper')""")
    db.set_setting(SWITCH_KEY, True)
    db.log("seed", "KreekCraft campaign ended (rate cut to $0.50/1K); Bobby VanRiper GTA VI is now the active campaign, "
                   "Boeshi added as an inactive backup")


# --- 21 Nov 2026: viewers swiped away in the first 1-2 s, and 3 same-streamer Shorts a day got 0 impressions ---
PLAN_KEY = "posting_plan_2026_11_21"


def posting_plan():
    """Once: 2 slots a day (12:00 and 15:00 ET = 21:00 and 00:00 Tbilisi), at most 2 posts a day per account and 1 on
    YouTube, 2.5 h between posts on an account, 2 clips cut a day per active campaign (no more than can be posted),
    and the hashtags of clips not posted yet cleaned (captions.clean). Hook-first editing is on for every campaign
    through the column defaults (target_secs 18, teaser, punch_zoom)."""
    from . import captions
    if db.settings().get(PLAN_KEY):
        return
    db.set_setting("slots_et", ["12:00", "15:00"])
    db.set_setting("max_posts_per_day", 2)
    caps = dict(db.settings().get("max_per_day_by_service") or {})
    caps["youtube"] = 1
    db.set_setting("max_per_day_by_service", caps)
    db.set_setting("min_gap_minutes", 150)
    db.q("UPDATE campaigns SET daily_quota = LEAST(COALESCE(daily_quota, 2), 2) WHERE active")
    for c in db.q("SELECT id, caption FROM clips WHERE status IN ('review','approved','scheduled') AND caption IS NOT NULL"):
        fixed = captions.clean(c["caption"])
        if fixed != c["caption"]:
            db.q("UPDATE clips SET caption=%s WHERE id=%s", (fixed, c["id"]))
    db.set_setting(PLAN_KEY, True)
    db.log("seed", "posting plan: 2 slots a day (21:00 and 00:00 Tbilisi), max 2 posts a day per account and 1 on "
                   "YouTube, 2.5 h apart; 2 clips cut a day; clips open on their payoff with a text hook")


# --- 21 Nov 2026: no more KreekCraft clips at all; take every KreekCraft post off the schedule and the dashboard ---
DROP_KREEK_KEY = "drop_kreekcraft_2026_11_21"


def drop_kreekcraft():
    """Once (retried on the next start while a Buffer post couldn't be cancelled): the KreekCraft campaign goes with
    its clips and posts; queued posts are cancelled in Buffer; posts already live are listed in the event log with
    their links, to delete on the app by hand. Bobby VanRiper stays."""
    from . import manage
    if db.settings().get(DROP_KREEK_KEY):
        return
    done = True
    for ca in db.q("SELECT id FROM campaigns WHERE 'kreekcraft' = ANY(channels) OR name ILIKE '%%kreekcraft%%'"):
        try:
            r = manage.remove_campaign(ca["id"], who="seed")
        except Exception as e:  # Buffer down or rate-limited: try again on the next start
            db.log("seed", f"KreekCraft removal failed, will retry: {e}", "warn")
            done = False
            continue
        done = done and r["campaign_removed"]
        print("[seed] KreekCraft removed:", {k: v for k, v in r.items()}, flush=True)
    db.q("""UPDATE accounts SET streamers = ARRAY(SELECT s FROM unnest(streamers) s WHERE lower(s) <> 'kreekcraft')
            WHERE EXISTS (SELECT 1 FROM unnest(streamers) s WHERE lower(s) = 'kreekcraft')""")
    if done:
        db.set_setting(DROP_KREEK_KEY, True)


LAYOUT_KEY = "layout_whole_frame_2026_11_21"


def whole_frame_layout():
    """Once (21 Nov 2026): Twitch clips are too soft to zoom into, so every campaign switches from the react layout
    (webcam panel + gameplay cropped ~2x) to the whole stream frame on a blurred background. Campaigns page can
    switch a campaign back."""
    if db.settings().get(LAYOUT_KEY):
        return
    db.q("UPDATE campaigns SET layout='blur'")
    db.set_setting(LAYOUT_KEY, True)
    db.log("seed", "all campaigns now use the whole-frame layout (sharper than the zoomed react layout)")


# --- 21 Nov 2026: Bobby's campaign is real but tiny ($35 of $1,000 paid to 7 clippers) and our account hit its limit of
# 3 pending clips; switch new clips to DiegoLoveless GTA 6 (ClipLaunch, $820 of $1,400 already paid to 106 clippers) ---
DIEGO_KEY = "campaign_diego_2026_11_21"
DIEGO_URL = "https://contentrewards.com/c/campaigns/5d37d3dd-adf3-401f-9c62-24bb54d5087a"
DIEGO_CAPTION = "{hook} Watch Diego live at twitch.tv/diegoloveless #diegoloveless #gta6 #gtavi #twitchclips"
DIEGO_RULES = """Content Rewards / ClipLaunch brief (Notion "DiegoLoveless GTA 6: Full Rules Brief", read 21 Nov 2026):
- Only Diego's Grand Theft Auto Twitch streams (twitch.tv/diegoloveless); he is speedrunning GTA 6 this week.
  Funny moments, reactions, standout gameplay, speedrun highlights. No low-effort screen recordings.
- Caption: twitch.tv/diegoloveless on screen or in the caption, plus a clear CTA to his Twitch.
- Likes visible; at least 0.8% engagement. Any page, no bio requirement. Overlay optional (link in the brief).
- TikTok, Instagram and YouTube: $1 per 1K views, min payout $2 (2,000 views), max $300 per clip.
- Budget $1,400 ($820 paid on 21 Nov). ClipLaunch Discord for announcements."""


def diego_campaign():
    """Once: DiegoLoveless GTA 6 becomes the active campaign (whole-frame layout, 2 clips a day, his small channel
    gets low-view clips too); Bobby VanRiper is turned off (kept with its clips and posts; his Content Rewards page
    allows only 3 pending clips per clipper and ours are all used)."""
    if db.settings().get(DIEGO_KEY):
        return
    if not db.one("SELECT 1 AS x FROM campaigns WHERE url=%s", (DIEGO_URL,)):
        db.q("""INSERT INTO campaigns(name, url, cpm, min_payout, max_payout, channels, streamer_name, game,
                  caption_template, overlay_url, captions, cam_box, title_secs, daily_quota, min_clip_views, lookback_hours,
                  rules, active, submit_window_minutes, layout)
                VALUES (%s,%s,1,2,300,%s,'DiegoLoveless','Grand Theft Auto VI',%s,NULL,'word',NULL,5,2,10,72,%s,TRUE,30,
                        'blur')""",
             ("DiegoLoveless GTA 6 Clipping", DIEGO_URL, ["diegoloveless"], DIEGO_CAPTION, DIEGO_RULES))
    db.q("UPDATE campaigns SET active=FALSE WHERE 'bobby_vanriper' = ANY(channels)")
    db.q("""UPDATE accounts SET streamers = array_append(streamers, 'diegoloveless')
            WHERE EXISTS (SELECT 1 FROM unnest(streamers) s WHERE lower(s) = 'bobby_vanriper')
              AND NOT EXISTS (SELECT 1 FROM unnest(streamers) s WHERE lower(s) = 'diegoloveless')""")
    db.set_setting(DIEGO_KEY, True)
    db.log("seed", "DiegoLoveless GTA 6 is now the active campaign ($1/1K, min 2,000 views); Bobby VanRiper turned off "
                   "(3-pending-clip limit reached on Content Rewards)")


# --- 21 Nov 2026: Diego's budget is nearly spent ($820 of $1,400); Boeshi (ClipLaunch, $48 of $1,750 used) runs
# alongside it. His brief requires his Twitch overlay for the whole clip and twitch.tv/darealboeshi in every caption ---
BOESHI_KEY = "campaign_boeshi_2026_11_21"
BOESHI_URL = "https://contentrewards.com/c/campaigns/5c01ecdb-a148-4489-ba26-5aadbbb5bf69"
BOESHI_OVERLAY_SRC = "https://drive.google.com/uc?export=download&id=1WRa2iLdB9JofEZiK0ckR5UKaJSe1x7nn"
BOESHI_CAPTION = "{hook} Catch Boeshi LIVE at twitch.tv/darealboeshi #boeshi #gta6 #gtavi #twitchclips"
BOESHI_BRIEF = """Content Rewards / ClipLaunch brief (Notion "Boeshi GTA 6 Clipping: Full Rules Brief", read 21 Nov 2026):
- Recent GTA 6 Twitch VODs of twitch.tv/darealboeshi; clip farm whole VODs (every clippable moment).
- Required: his Twitch overlay (purple "@darealboeshi" badge, Google Drive) clearly visible for the whole clip.
- Caption: twitch.tv/darealboeshi in every caption (e.g. "Catch Boeshi LIVE at twitch.tv/darealboeshi").
- 40%+ Tier 1 audience (US, UK, CA, AU); at least 0.8% engagement; each clip stays live 30 days; no bio requirement.
- Favoured: jokes about his voice, his mewing when focused or scared, chad edits, meme captions.
- New accounts: warm-up guide; read the approval process and USA-audience guide in the brief.
- $1 per 1K views, min payout $3 (3,000 views), max $300 per clip. Budget $1,750 ($48 used on 21 Nov)."""


def boeshi_campaign():
    """Once the overlay is in Blob: Boeshi's campaign is turned on next to Diego's, with his overlay under the stream
    frame (whole-frame layout puts the frame at y 576-1184 and word captions near 1460). Retried on the next start
    if the overlay can't be fetched; the campaign stays off until then, since the overlay is required."""
    import tempfile
    import urllib.request
    from pathlib import Path
    from .integrations import blob
    if db.settings().get(BOESHI_KEY):
        return
    try:
        tmp = Path(tempfile.mkdtemp()) / "boeshi.png"
        req = urllib.request.Request(BOESHI_OVERLAY_SRC, headers={"user-agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            tmp.write_bytes(r.read())
        if tmp.read_bytes()[:8] != b"\x89PNG\r\n\x1a\n":
            raise RuntimeError("Google Drive did not return a PNG")
        overlay = blob.upload(tmp, "overlays/boeshi.png", "image/png")
    except Exception as e:
        db.log("seed", f"Boeshi overlay could not be fetched, campaign stays off, will retry on next start: {e}", "warn")
        return
    if not db.one("SELECT 1 AS x FROM campaigns WHERE url=%s", (BOESHI_URL,)):
        db.q("""INSERT INTO campaigns(name, url, cpm, min_payout, max_payout, channels, streamer_name, game, active)
                VALUES ('Boeshi GTA 6 Clipping', %s, 1, 3, 300, %s, 'Boeshi', 'Grand Theft Auto VI', FALSE)""",
             (BOESHI_URL, ["darealboeshi"]))
    db.q("""UPDATE campaigns SET active=TRUE, ended_at=NULL, caption_template=%s, rules=%s, overlay_url=%s,
              overlay_width=540, overlay_y=1200, captions='word', cam_box=NULL, layout='blur', daily_quota=2,
              min_clip_views=10, lookback_hours=72, submit_window_minutes=30
            WHERE url=%s""", (BOESHI_CAPTION, BOESHI_BRIEF, overlay, BOESHI_URL))
    db.q("""UPDATE accounts SET streamers = array_append(streamers, 'darealboeshi')
            WHERE EXISTS (SELECT 1 FROM unnest(streamers) s WHERE lower(s) = 'diegoloveless')
              AND NOT EXISTS (SELECT 1 FROM unnest(streamers) s WHERE lower(s) = 'darealboeshi')""")
    db.set_setting(BOESHI_KEY, True)
    db.log("seed", "Boeshi GTA 6 is now active next to DiegoLoveless ($1/1K, min 3,000 views), with his required "
                   "Twitch overlay and twitch.tv/darealboeshi in the caption")


def retry_oom_renders():
    """Once: moments whose render was OOM-killed (empty "ffmpeg failed:" before the 21 Nov fix) go back to the queue."""
    if db.settings().get("retry_oom_renders_2026_11_21"):
        return
    db.q("""UPDATE moments SET status='new', reason=NULL, updated_at=now()
            WHERE status='failed' AND reason LIKE 'ffmpeg failed:%%' AND updated_at > now() - interval '2 days'""")
    db.set_setting("retry_oom_renders_2026_11_21", True)


EVELONE_BRIEF = """Evelone TikTok/Shorts contest (t.me/EveloneTiktok, read 21 Nov 2026):
- TikTok and YouTube Shorts. Evelone must be the main face of the video, or it is rejected.
- #evelone and the Twitch channel in the description (in the title of a Short): "twitch: evelone2004 ...".
- Cover every ad banner in the video. No botted views/likes/comments. No politics, conflicts or bait negativity.
- At most 2 accounts per person. No re-uploads of other clippers' videos. Each video is paid once.
- Pay per video (views not summed): 100K+ $15, 250K+ $25, 500K+ $50, 1M+ $100.
  Videos published 18-25 Nov 2026 are paid x2: $30 / $50 / $100 / $200.
- Submit within 14 days of posting: https://forms.gle/T4fgMUmD8KmwNDdWA . Paid every 14 days."""

STROGO_BRIEF = """Strogo TikTok/Shorts contest (t.me/strogotiktok, read 21 Nov 2026):
- Your Telegram @username must be in the bio of the TikTok/YouTube channel.
- Strogo must be the main face. Stream no older than 30 days, video no older than 7 days when submitted.
- "twitch: strogo" (or twitch.tv/strogo) first in the description. Cover ad banners. No botting, no re-uploads.
- Pay per Short (not summed): 100K+ $15, 500K+ $50 (max). Long cuts (5+ min) 15K+ views $25 (needs an established
  channel of his cuts). Payouts around the 3rd, 13th and 23rd.
- GTA 6 marathon contest (from 17 Nov 2026): most-viewed videos win 100K / 50K / 50K / 25K / 25K RUB.
  The stream timer must be visible, the video must be about the GTA 6 marathon, and the description starts with
  "twitch:StRoGo #GTA6 #марафон #StRoGo". Fill in the form once a video passes 250K views."""

RU_CAMPAIGNS = [
    # name, url, twitch login, streamer name, title template, caption template, cpm, min, max, rules
    ("Evelone TikTok/Shorts contest", "https://t.me/EveloneTiktok", "evelone2004", "Эвелон",
     "twitch: evelone2004 {title} #evelone", "twitch: evelone2004 {hook} #evelone #эвелон #shorts",
     0.15, 15, 100, EVELONE_BRIEF),
    ("Strogo TikTok/Shorts contest", "https://t.me/strogotiktok", "strogo", "Строго",
     "{title} | twitch: strogo", "twitch: strogo {hook} #strogo #строго #shorts",
     0.15, 15, 50, STROGO_BRIEF),
]


def russian_campaigns():
    """Once: Evelone's and Strogo's Telegram clip contests as Russian-language campaigns for the RU geo only (the
    Russian YouTube channel, manual uploads), any game, whole-frame layout. Active from the start (the user joined
    both Telegram channels on 21 Nov 2026); each one's first clip waits in Review."""
    key = "russian_campaigns_2026_11_21"
    if db.settings().get(key):
        return
    for name, url, login, streamer, title_tpl, caption, cpm, mn, mx, rules in RU_CAMPAIGNS:
        if db.one("SELECT 1 AS x FROM campaigns WHERE url=%s", (url,)):
            continue
        db.q("""INSERT INTO campaigns(name, url, cpm, min_payout, max_payout, channels, streamer_name, game, caption_template,
                  title_template, rules, language, geo, layout, captions, daily_quota, min_clip_views, lookback_hours, active)
                VALUES (%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,'ru','RU','blur','word',2,300,48,TRUE)""",
             (name, url, cpm, mn, mx, [login], streamer, caption, title_tpl, rules))
    db.set_setting(key, True)
    db.log("seed", "added the Evelone and Strogo clip contests (Russian, RU geo only, active; first clip of each waits in Review)")


BIGBALLS_BRIEF = """BigBalls Partners (Telegram bot @Bigballs_tiktok_bot, creators channel @csdogTikTok, read 22 Nov 2026):
- Pay per 1,000 views, TikTok / YouTube Shorts: CS2 banner $0.80 / $0.60 (CS2 at least 60% of the video), non-CS2
  banner (streamer clips, IRL) $0.50 / $0.30, banner creatives $0.20, song $0.50 / $0.30, GTA VI case-opening
  site content $2.00 (TikTok only). One video is paid in one format only, and not also by a streamer contest.
- Banner: centred, 30%+ of the screen, its voiceover clearly audible, not before 0:10 (under 10 s: the middle),
  again every minute, main video paused while it plays, at most 1.2x speed. Banner files: @csdogTikTok post 113
  (RU and ENG, MP4 green screen or MOV).
- Not paid: bought views, paid promotion, photo posts/slideshows, voiced Reddit stories, films and series, any other
  ad or project in the same video.
- Submit in the bot once a video has 5,000+ views, within 14 days, with a screen recording of the stats. Only the
  share of viewers who watched past the banner is paid (retention graph). Payout USDT TRC20/TON, min $25 (bot).
- TikTok's rules ban marketing gambling-like sites: banner clips only go to accounts marked "Banner clips"."""


def bigballs_campaign():
    """Once: the BigBalls banner campaign (RU streamer clips, split screen, Russian), off until its banner video is
    uploaded on the Campaigns page; and the Evelone and Strogo contests move to the split-screen layout (streamer on
    top, a satisfying loop below), the format that gets @busterenus 300K-1.3M views on these streamers."""
    key = "bigballs_campaign_2026_11_22"
    if db.settings().get(key):
        return
    url = "https://t.me/Bigballs_tiktok_bot"
    if not db.one("SELECT 1 AS x FROM campaigns WHERE url=%s", (url,)):
        db.q("""INSERT INTO campaigns(name, url, cpm, min_payout, max_payout, channels, streamer_name, game, caption_template,
                  title_template, rules, language, geo, layout, captions, daily_quota, min_clip_views, lookback_hours,
                  target_secs, submit_url, active)
                VALUES ('BigBalls banner (streamer clips)', %s, 0.5, 2.5, NULL, %s, NULL, NULL, '{hook} #нарезки #стрим',
                        '{title}', %s, 'ru', 'RU', 'split', 'word', 2, 300, 72, 25, %s, FALSE)""",
             (url, ["evelone2004", "buster", "strogo"], BIGBALLS_BRIEF, url))
    db.q("UPDATE campaigns SET layout='split' WHERE url IN ('https://t.me/EveloneTiktok', 'https://t.me/strogotiktok')")
    db.set_setting(key, True)
    db.log("seed", "added the BigBalls banner campaign (off until its banner is uploaded) and moved Evelone and Strogo "
                   "to the split-screen layout")
