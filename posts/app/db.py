"""Postgres access. Small helper functions over psycopg 3; tables are created on start."""
import json
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT,
  cpm NUMERIC DEFAULT 1,
  min_payout NUMERIC DEFAULT 0,
  max_payout NUMERIC,
  channels TEXT[] NOT NULL DEFAULT '{}',
  game TEXT DEFAULT 'Grand Theft Auto VI',
  caption_template TEXT NOT NULL DEFAULT '{hook}',
  overlay_url TEXT,
  overlay_width INT DEFAULT 600,
  overlay_y INT DEFAULT 895,
  captions TEXT DEFAULT 'word',
  cam_box TEXT,
  title_secs REAL DEFAULT 5,
  daily_quota INT DEFAULT 4,
  min_clip_views INT DEFAULT 100,
  lookback_hours INT DEFAULT 48,
  rules TEXT,
  active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS moments (
  id SERIAL PRIMARY KEY,
  campaign_id INT REFERENCES campaigns(id) ON DELETE CASCADE,
  source TEXT DEFAULT 'twitch_clip',
  channel TEXT,
  external_id TEXT NOT NULL,
  title TEXT,
  views INT DEFAULT 0,
  duration REAL,
  game TEXT,
  url TEXT,
  vod_id TEXT,
  vod_offset INT,
  clip_created_at TIMESTAMPTZ,
  status TEXT DEFAULT 'new',
  reason TEXT,
  discovered_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (campaign_id, external_id)
);
CREATE TABLE IF NOT EXISTS clips (
  id SERIAL PRIMARY KEY,
  moment_id INT REFERENCES moments(id) ON DELETE SET NULL,
  campaign_id INT REFERENCES campaigns(id) ON DELETE CASCADE,
  title TEXT,
  caption TEXT,
  video_url TEXT,
  thumb_url TEXT,
  duration REAL,
  status TEXT DEFAULT 'review',
  error TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  approved_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS posts (
  id SERIAL PRIMARY KEY,
  clip_id INT REFERENCES clips(id) ON DELETE CASCADE,
  campaign_id INT REFERENCES campaigns(id) ON DELETE CASCADE,
  channel_id TEXT,
  channel_name TEXT,
  service TEXT,
  buffer_post_id TEXT UNIQUE,
  due_at TIMESTAMPTZ,
  sent_at TIMESTAMPTZ,
  status TEXT,
  external_link TEXT,
  error TEXT,
  views BIGINT DEFAULT 0,
  likes BIGINT DEFAULT 0,
  comments BIGINT DEFAULT 0,
  shares BIGINT DEFAULT 0,
  saves BIGINT DEFAULT 0,
  metrics_at TIMESTAMPTZ,
  submitted BOOLEAN DEFAULT FALSE,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS metrics (
  id BIGSERIAL PRIMARY KEY,
  post_id INT REFERENCES posts(id) ON DELETE CASCADE,
  ts TIMESTAMPTZ DEFAULT now(),
  views BIGINT, likes BIGINT, comments BIGINT, shares BIGINT, saves BIGINT
);
CREATE INDEX IF NOT EXISTS metrics_post_ts ON metrics(post_id, ts);
ALTER TABLE posts ADD COLUMN IF NOT EXISTS notified_at TIMESTAMPTZ;
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value JSONB);
CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY,
  ts TIMESTAMPTZ DEFAULT now(),
  level TEXT,
  worker TEXT,
  message TEXT,
  data JSONB
);
CREATE TABLE IF NOT EXISTS heartbeats (
  worker TEXT PRIMARY KEY,
  last_start TIMESTAMPTZ,
  last_ok TIMESTAMPTZ,
  last_error_at TIMESTAMPTZ,
  last_error TEXT,
  runs INT DEFAULT 0
);
CREATE TABLE IF NOT EXISTS geos (
  id SERIAL PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  sort INT DEFAULT 0,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE TABLE IF NOT EXISTS accounts (
  id SERIAL PRIMARY KEY,
  geo_id INT NOT NULL REFERENCES geos(id) ON DELETE RESTRICT,
  platform TEXT NOT NULL,
  handle TEXT NOT NULL,
  login TEXT,
  password_enc TEXT,
  profile_url TEXT,
  status TEXT DEFAULT 'warming',
  streamers TEXT[] NOT NULL DEFAULT '{}',
  buffer_channel_id TEXT,
  followers BIGINT,
  total_views BIGINT,
  total_posts INT,
  total_likes BIGINT,
  stats_at TIMESTAMPTZ,
  notes TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS accounts_geo ON accounts(geo_id, platform);
-- upload mode: 'manual' = the pipeline emails a reminder to upload by hand at each slot and never posts through Buffer;
-- 'automatic' = posted through its linked Buffer channel. Accounts that existed before this column keep posting:
-- linked ones become automatic, the rest manual. New accounts start manual.
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS upload_mode TEXT;
UPDATE accounts SET upload_mode = CASE WHEN buffer_channel_id IS NOT NULL THEN 'automatic' ELSE 'manual' END
  WHERE upload_mode IS NULL;
ALTER TABLE accounts ALTER COLUMN upload_mode SET DEFAULT 'manual';
ALTER TABLE accounts ALTER COLUMN upload_mode SET NOT NULL;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS manual_since TIMESTAMPTZ;
UPDATE accounts SET manual_since = created_at WHERE manual_since IS NULL;
ALTER TABLE accounts ALTER COLUMN manual_since SET DEFAULT now();
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS week_hint_at TIMESTAMPTZ;
-- one row per clip handed to a manual account: the reminder email, and later the link once uploaded
CREATE TABLE IF NOT EXISTS manual_uploads (
  id SERIAL PRIMARY KEY,
  account_id INT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  clip_id INT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  slot_at TIMESTAMPTZ NOT NULL,
  emailed_at TIMESTAMPTZ,
  done_at TIMESTAMPTZ,
  link TEXT,
  created_at TIMESTAMPTZ DEFAULT now(),
  UNIQUE (account_id, clip_id)
);
CREATE INDEX IF NOT EXISTS manual_uploads_slot ON manual_uploads(account_id, slot_at);
-- encrypted sign-ins the workers use (Whop tokens, browser cookies): see vault.py
CREATE TABLE IF NOT EXISTS vault (key TEXT PRIMARY KEY, value_enc TEXT NOT NULL, updated_at TIMESTAMPTZ DEFAULT now());
-- auto-submit to Content Rewards: the campaign's Whop bounty (or submit page for the browser), and per post its state
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS whop_bounty_id TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS submit_url TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS submit_window_minutes INT DEFAULT 30;
-- campaign switch: the streamer's display name (titles), and when/why a campaign ended (it stays for its history)
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS streamer_name TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS ended_at TIMESTAMPTZ;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS end_reason TEXT;
-- submit_status: NULL (not handled: old post or auto-submit off), pending, submitted, failed, manual (ticked by hand)
ALTER TABLE posts ADD COLUMN IF NOT EXISTS submit_status TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS submit_method TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS submit_ref TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS submit_error TEXT;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS submit_attempts INT DEFAULT 0;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS submit_next_at TIMESTAMPTZ;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMPTZ;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS submit_deadline TIMESTAMPTZ;
ALTER TABLE posts ADD COLUMN IF NOT EXISTS submit_alerted_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS posts_submit ON posts(submit_status) WHERE submit_status IN ('pending', 'failed');
-- hook-first editing (hook.py): clip length around the payoff, teaser of the payoff at 0:00, punch-in zooms
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS target_secs REAL DEFAULT 18;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS teaser BOOLEAN DEFAULT TRUE;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS punch_zoom BOOLEAN DEFAULT TRUE;
-- 'react' (webcam panel on top, gameplay cropped below) or 'blur' (the whole stream frame, unzoomed, on a blurred
-- background: sharper, since Twitch clips are too soft to zoom into)
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS layout TEXT DEFAULT 'react';
-- the on-screen hook burned into the clip, and how it was cut (start/end, payoff, teaser, zooms, candidates)
ALTER TABLE clips ADD COLUMN IF NOT EXISTS hook_text TEXT;
ALTER TABLE clips ADD COLUMN IF NOT EXISTS edit_plan JSONB;
-- Russian-speaking campaigns (titles.py writes the hook, title and caption line in this language) and the geo
-- (geos.code) whose accounts get the campaign's clips. NULL geo = the default (US) accounts and unlinked Buffer
-- channels, so a Russian campaign never lands on the US accounts and the other way round.
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS language TEXT DEFAULT 'en';
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS geo TEXT;
-- post title wrapper: '{title}' or e.g. 'twitch: evelone2004 {title} #evelone' when a brief wants it in the title
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS title_template TEXT DEFAULT '{title}';
"""

# Geos created on first start (names only, no account data)
DEFAULT_GEOS = [("US", "United States", 1), ("RU", "Russia", 2)]

DEFAULT_SETTINGS = {
    "auto_approve": False,
    "workers_paused": False,
    # Posting slots in US Eastern time (HH:MM). Each channel gets at most one post per slot.
    # 12:00 and 15:00 ET = 21:00 and 00:00 Tbilisi: US lunch and after school, both while the user is awake.
    "slots_et": ["12:00", "15:00"],
    # Accounts of other geos post at their own audience's hours: geo code -> timezone and local HH:MM slots.
    # "per_day" (by platform, or "default") replaces the daily limits below for that geo's accounts.
    # Russia: five Moscow-time slots from noon to late evening = 13:00, 16:00, 19:00, 21:30 and 23:30 Tbilisi;
    # two streamers' clips a day there, so TikTok takes 4 and YouTube 2.
    "geo_slots": {"RU": {"tz": "Europe/Moscow", "slots": ["12:00", "15:00", "18:00", "20:30", "22:30"],
                         "per_day": {"tiktok": 4, "youtube": 2}}},
    # 1-2 posts a day per account: more clips of the same streamer on one day get fewer impressions each
    "max_posts_per_day": 2,
    # YouTube showed 0 impressions after 3 same-streamer Shorts in one day: one a day there
    "max_per_day_by_service": {"youtube": 1},
    # never two posts on the same account closer than this
    "min_gap_minutes": 150,
    # timezone of the Buffer account: its daily limits count days in this zone
    "buffer_timezone": "Asia/Tbilisi",
    # Buffer's free plan keeps at most 10 scheduled posts per channel at a time
    "max_queued_per_channel": 10,
    "stagger_minutes": 15,
    "services": ["tiktok", "instagram", "youtube"],
    # Auto-submit live post links to Content Rewards. Methods are tried in order until one works:
    # "api" (Whop's official API, signed in with "Connect Whop"), "browser" (headless Chrome on this server with a
    # saved Whop session), "agent" (an outside helper, e.g. Claude in Chrome on a laptop, takes the job from
    # /api/agent/submissions). Only posts that go live after auto-submit is turned on are handled.
    "auto_submit": False,
    "submit_methods": ["api", "browser", "agent"],
}


@contextmanager
def conn():
    with psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row) as c:
        yield c


def q(sql: str, params=None) -> list[dict]:
    with conn() as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


def one(sql: str, params=None) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def migrate():
    with conn() as c:
        c.execute(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT (key) DO NOTHING", (k, json.dumps(v)))
        if not c.execute("SELECT 1 FROM geos LIMIT 1").fetchone():
            for code, name, sort in DEFAULT_GEOS:
                c.execute("INSERT INTO geos(code, name, sort) VALUES (%s,%s,%s) ON CONFLICT (code) DO NOTHING", (code, name, sort))


def settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    for r in q("SELECT key, value FROM settings"):
        s[r["key"]] = r["value"]
    return s


def set_setting(key: str, value):
    q("INSERT INTO settings(key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
      (key, json.dumps(value)))


def log(worker: str, message: str, level: str = "info", data=None):
    try:
        q("INSERT INTO events(level, worker, message, data) VALUES (%s,%s,%s,%s)",
          (level, worker, message[:2000], json.dumps(data) if data is not None else None))
    except Exception:
        pass
    print(f"[{level}] {worker}: {message}", flush=True)
