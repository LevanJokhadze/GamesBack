# posts — clip pipeline API and workers (posts.lane.ge)

Separate app from the Joker server in this repo: its own folder, Docker image and Railway service.
Dashboard: [LevanJokhadze/Games](https://github.com/LevanJokhadze/Games) `posts/`.

It turns a streamer's best Twitch moments into vertical clips for paid view campaigns
(Content Rewards / Whop) and posts them to TikTok, Instagram Reels and YouTube Shorts through Buffer.

## Workers (background threads in the API process, running 24/7)

| Worker | Every | What it does |
| --- | --- | --- |
| `discover` | 20 min | For each active campaign, reads the most-viewed Twitch clips of its channels (public GQL, no key), keeps those in the campaign's game, newer than `lookback_hours`, with at least `min_clip_views` views. |
| `render` | 3 min | Takes the most-viewed new moment (up to `daily_quota` clips per campaign per US-Eastern day): downloads it, transcribes it (faster-whisper), writes the title, cuts a 1080x1920 react clip (webcam on top, game below), burns in the campaign's logo overlay, uploads video + thumbnail to Vercel Blob. The clip then waits for review (or is approved at once with `auto_approve` and a written title). |
| `publish` | 5 min | Schedules every approved clip in Buffer on each connected channel at the next free US-Eastern slot (`slots_et`, max `max_posts_per_day` per channel, `stagger_minutes` between platforms). |
| `monitor` | 10 min | Follows each Buffer post until it is sent (stores the public link) or fails, and stores views/likes/comments/shares every 2 hours (needs a Buffer key with `insights:read`). |

Heartbeats, errors and a log are stored in the database and shown on the dashboard. `workers_paused` stops
the timers; "Run now" in the dashboard still runs a worker once.

Clip rendering uses scripts vendored from [gtavi-claudecode](https://github.com/Bodila51/gtavi-claudecode)
(MIT, reviewed commit in `vendor/gtavi/README.md`).

## Variables

| Variable | Meaning |
| --- | --- |
| `DATABASE_URL` | Postgres (Railway: `${{Postgres.DATABASE_URL}}`). Tables are created on start. |
| `ADMIN_PASSWORD` | Password for the dashboard login. |
| `SECRET_KEY` | Signs login tokens (any long random string). |
| `ALLOWED_ORIGINS` | Dashboard origin(s), e.g. `https://posts.lane.ge`. |
| `BUFFER_API_KEY` | Buffer personal key with posts:read, posts:write, insights:read. |
| `BLOB_READ_WRITE_TOKEN` | Vercel Blob store token (public store) for hosting the videos Buffer fetches. |
| `ANTHROPIC_API_KEY` | Optional. Claude writes titles and caption hooks from the transcript. Without it the viewer's Twitch clip title is used and every clip waits for review. |
| `TITLE_MODEL` | Optional, default `claude-haiku-5-5`. |
| `RUN_WORKERS` | `1` (default) runs the workers in this process; `0` serves the API only. |

## Run locally

```bash
cd posts
pip install -r requirements.txt   # needs ffmpeg on PATH
DATABASE_URL=postgresql://... ADMIN_PASSWORD=... RUN_WORKERS=0 uvicorn app.main:app --port 8099
```

## Deploy (Railway)

Service with root directory `posts` (it builds the `Dockerfile`), a Postgres service, and the variables above.
The image bakes the whisper and face models so a new deploy renders right away. Give it 2+ GB of memory.
