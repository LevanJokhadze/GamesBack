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
"""

DEFAULT_SETTINGS = {
    "auto_approve": False,
    "workers_paused": False,
    # Posting slots in US Eastern time (HH:MM). Each channel gets at most one post per slot.
    "slots_et": ["12:00", "19:00", "21:00"],
    "max_posts_per_day": 3,
    # Buffer refuses more than 2 YouTube posts a day on this channel
    "max_per_day_by_service": {"youtube": 2},
    # timezone of the Buffer account: its daily limits count days in this zone
    "buffer_timezone": "Asia/Tbilisi",
    # Buffer's free plan keeps at most 10 scheduled posts per channel at a time
    "max_queued_per_channel": 10,
    "stagger_minutes": 15,
    "services": ["tiktok", "instagram", "youtube"],
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
