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
| `submit` | 1 min | **Auto-submit to Content Rewards.** Once `auto_submit` is on, every post that goes live afterwards (Buffer, or a hand upload whose link is added on the Accounts page) is submitted to its campaign within the campaign's window (30 min). Methods are tried in the order of `submit_methods`: `api` (Whop's official `POST /bounty_submissions`, after "Connect Whop", for campaigns with a Whop bounty id), `browser` (headless Chromium here, signed in with the Content Rewards session pasted on the Settings page, fills in the campaign's submit form) and `agent` (an outside helper polls `/api/agent/submissions`). Failures retry after 1, 2, 4, 8 min until the deadline; the user gets one email per post as soon as it struggles, with the link and the deadline in Tbilisi time. Every 6 h the browser session is checked so a sign-out is emailed before a post needs it. |
| `manual` | 5 min | For accounts in **manual** upload mode (new accounts start manual): at each US-Eastern slot, within the same daily limits, emails a reminder with the clip's download link, caption and hashtags, platform and username, instead of posting through Buffer. Never sends the same clip to the same account twice. After 7 days it emails once to suggest switching the account to automatic on the Accounts page. |

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
| `ACCOUNTS_KEY` | Encrypts the social account passwords saved on the Accounts page (Fernet, key derived from this string). Any long random string; never change it afterwards, or saved passwords can't be read. Without it accounts still work, passwords just can't be saved. |
| `PUBLIC_API_URL` | Public https address of this API (Whop sends the sign-in back to `{PUBLIC_API_URL}/api/whop/callback`). Defaults to Railway's `RAILWAY_PUBLIC_DOMAIN`. |
| `WHOP_CLIENT_ID`, `WHOP_CLIENT_SECRET` | Optional. A Whop app (whop.com developer dashboard) with that redirect URI, for the `api` submit method. The sign-in itself is stored encrypted (ACCOUNTS_KEY), never in variables. |
| `SUBMIT_AGENT_TOKEN` | Optional. Long random string for the `agent` submit method: the helper sends `Authorization: Bearer <token>` to `GET /api/agent/submissions` and reports each job with `POST /api/agent/submissions/{id}` `{"ok": true}` or `{"ok": false, "error": "..."}`. |
| `WITH_BROWSER` | Build-time, default `1`: installs headless Chromium (Playwright) for the `browser` submit method. |
| `RUN_WORKERS` | `1` (default) runs the workers in this process; `0` serves the API only. |

## Accounts

`geos` (US and Russia are created on first start) hold `accounts`, one per Instagram / TikTok / YouTube profile:
handle, login, password (encrypted with `ACCOUNTS_KEY`, returned only by `POST /api/accounts/{id}/reveal`, which is logged),
status, the streamers it targets, notes and stats. An account linked to a Buffer channel takes views, posts and likes from
the pipeline's posts on that channel; followers and the stats of unlinked accounts are typed in.

## Run locally

```bash
cd posts
pip install -r requirements.txt   # needs ffmpeg on PATH
DATABASE_URL=postgresql://... ADMIN_PASSWORD=... RUN_WORKERS=0 uvicorn app.main:app --port 8099
```

## Deploy (Railway)

Service with root directory `posts` (it builds the `Dockerfile`), a Postgres service, and the variables above.
The image bakes the whisper and face models so a new deploy renders right away. Give it 2+ GB of memory.

Railway: project "Posts" (auto-deploys from `main`), service `posts-api-src` (root `/posts`, branch `posts`), Postgres in the same project.

## Outside submit helper (last resort)

When neither the API nor the server's browser can submit, `agent` hands the job to a helper with a real browser,
for example Claude in Chrome on a laptop left awake, or a small server running Claude with a browser. Its loop:

1. `GET /api/agent/submissions` with `Authorization: Bearer <SUBMIT_AGENT_TOKEN>` (every few minutes).
2. For each job: open `submit_page` (signed in to Content Rewards), press Submit, paste `link`, accept the rules, submit.
3. `POST /api/agent/submissions/{id}` with `{"ok": true}`, or `{"ok": false, "error": "what went wrong"}`.

The user is emailed if a job is still open 10 minutes before its deadline.
