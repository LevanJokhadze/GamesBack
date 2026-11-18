# GamesBack — Joker game server

Backend for **Joker** (ჯოკერი), the Georgian four-player trick-taking card game.
The server is authoritative; clients only send intents. All room logic lives in
`src/room-core.ts` and runs on two hosts with the same API:

- **Cloudflare Workers** (`src/index.ts`, `src/room.ts`): one Durable Object per game room.
- **Node** (`src/node-server.ts`, `npm start`): for Railway or any Node host. Rooms are kept in
  memory, so a restart or redeploy ends games in progress.

Frontend: [LevanJokhadze/Games](https://github.com/LevanJokhadze/Games).

## Features

- Full rules engine (`src/engine`): deck, dealing, trump, bidding with the dealer restriction,
  joker play (high/low, leading with a named suit), trick resolution, scoring, khishti and premium.
- Online rooms with 5-letter codes, reconnect by player id, host controls.
- **Spectators**: anyone who opens a full, started table watches it without seeing any cards,
  shows up in the people list and can chat.
- Bots fill empty seats.
- **Players who stop responding**: 30 s to play, then a warning and one more minute. Still
  nothing: −5 rating and a bot plays the seat. Not back within 2 minutes (a move or `back`):
  removed from the match, −25 more (30 in all), and a bot finishes the game. Every step is
  posted in the chat as a notice, and penalties are stored in `rating_penalties`.
  `TURN_MS` scales all three timers (warning = 2×, removal = 4×) for testing.
- **Chat and voice**: text chat for players and spectators; push-to-talk voice over WebRTC
  between people who joined voice, with the room WebSocket as the signalling relay.
- Rooms delete themselves after 12 hours without activity.
- WebSocket Hibernation API, so idle rooms cost nothing.
- **Accounts** (Node server): nickname (unique), first name, last name and photo are all required.
  A password (6+ characters, scrypt) lets the player log in on any device; each login is its own
  session token. The server stores only SHA-256 hashes of tokens. Failed logins are limited to
  10 per IP per 10 minutes.
- **Chess Elo**: every finished game with at least one account is rated (see below). Global
  leaderboard, per-player match history and a full summary of every match.
- **Play in pairs**: partners sit opposite (seats 1 & 3 against 2 & 4), team points are added up
  and the team with more points wins.

## Rules implemented

| Topic | Rule |
| --- | --- |
| Deck | 36 cards: 6–A in ♥ ♦, 7–A in ♠ ♣, plus two jokers (they replace the black sixes). |
| Game | 4 pulkas. **Standard**: 1→8 cards, 4 × 9, 8→1, 4 × 9 (24 deals). **Nines**: 4 pulkas of 4 × 9 (16 deals). |
| Trump | Deals under 9 cards: the next deck card is turned up; a joker means no trump. 9-card deals: the player left of the dealer sees 3 cards and names trump or no trump. |
| Bidding | From the dealer's left; the dealer bids last and cannot make the bids add up to the hand size. |
| Play | Follow suit; if void, play a trump if you have one; otherwise anything. A joker may always be played. |
| Joker | Played **high** it wins the trick (a later high joker beats an earlier one). Played **low** it loses. Leading a joker you name a suit: *high* = everyone must give their **highest** card of that suit (or a trump if void) and the joker wins; *low* = everyone must play that suit (or trump) and the joker is the lowest card. |
| Scoring | Exact bid: 50 × bid + 50 (bid 0 made = 50). All tricks of a deal with 2+ cards: 100 × cards. Missed: 10 per trick taken. Bid ≥ 1 and took 0: **khishti** (host chooses −200, −500 or −900). |
| Premium | Hit every bid in a pulka and your best deal of that pulka is added again. |

| Pairs | Optional. Seats 0 & 2 against 1 & 3. Everyone still bids and scores for themselves; the team total decides the winner and both partners share the place. |

House rules vary a lot between families and apps; these are easy to change in `src/engine/game.ts`.

## Rating (Elo)

Exactly the chess (FIDE) formula: expected score `E = 1 / (1 + 10^((Rb − Ra)/400))`, change
`K × (result − E)`, K = 40 for the first 30 games, 10 once a player has reached 2400, otherwise 20.
Everyone starts at 1500.

- **Solo**: each pair of players at the table is one chess game; the higher final score wins
  (equal = draw). K is shared over the three opponents (K/3 each).
- **Pairs**: one team game. Each team is rated at its two players' average; each player gets
  their own `K × (result − E)`, so partners win or lose together.
- Bots count as 1500 and are never rated.

Code: `src/engine/elo.ts`, applied in `src/store.ts` inside one transaction per game.

## Development

```bash
npm install
npm run dev        # wrangler dev on http://localhost:8787
npm run dev:node   # the Node server on http://localhost:8787
npm test           # engine, Elo and store tests (vitest)
# Postgres tests too: TEST_DATABASE_URL=postgres://... npm test (use an empty database)
npm run typecheck
npm run simulate   # play 200 bot-only games and print bot accuracy
```

## API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/health` | Health check |
| POST | `/api/rooms` | Create a room → `{ "code": "ABCDE" }` |
| GET | `/api/rooms/:code` | Room info (404 if it doesn't exist) |
| GET (WebSocket) | `/api/rooms/:code/ws` | Join a room |
| POST | `/api/register` | `{nickname, firstName, lastName, avatar, password}` (avatar = JPEG/PNG/WebP data URL ≤ 300 KB) → `{token, user}` |
| POST | `/api/login` | `{nickname, password}` (nickname in any case) → `{token, user}` for this device |
| POST | `/api/logout` | Ends this device's session |
| GET / PUT | `/api/me` | Your profile / change it, including `password` (`Authorization: Bearer <token>`) |
| GET | `/api/leaderboard?limit=100` | Rated players by rating |
| GET | `/api/users/:id` | Profile and the last 20 games |
| GET | `/api/users/:id/games?limit=20&before=<ISO time>` | Match history, newest first, paged |
| GET | `/api/users/:id/avatar` | Profile photo |
| GET | `/api/games/:id` | Match summary: players, places, rating changes, every deal |
| GET | `/api/ice` | `{iceServers}`: voice relay (TURN) servers for the browser, empty when none is set |

WebSocket messages from the client (JSON): `hello {token}` (send first; without accounts
`hello {playerId, name}`), `start`, `config {config: {mode, khishti, pairs}}`, `seat {seat}`, `addBot {seat}`, `removeSeat {seat}`,
`trump {suit|null}`, `bid {bid}`, `play {cardId, joker?: {type: "high"|"low", suit?}}`,
`chat {text}`, `rematch`, `back` (end a bot takeover), `ping`.
Voice: `voice {on}` (joined/left voice), `talk {on}` (push-to-talk, broadcast as `talk {peer, on}`),
`rtc {to: peer, data}` (WebRTC signalling, delivered as `rtc {from: peer, data}`).
The server answers with `state {room}` (a per-player view, own cards only) or `error {message}`.

## Deploy to Railway (Node)

The service runs `npm start` (Railpack detects it). Settings:

- Variables: `ALLOWED_ORIGINS` = your frontend origin(s), comma-separated (or `*`). Railway sets `PORT`.
- Add a Postgres service and set `DATABASE_URL=${{Postgres.DATABASE_URL}}` on this service.
  Tables are created on start. Without `DATABASE_URL` accounts live in memory (lost on restart).
- Healthcheck path: `/api/health`.
- Generate a domain (Settings → Networking) or attach your subdomain, and use that URL as the
  frontend's `VITE_API_URL`.
- Keep one replica: rooms are in memory, so several replicas would not share games.
- Voice relay (optional, for players on mobile data or strict Wi-Fi): set `CF_TURN_KEY_ID` and
  `CF_TURN_API_TOKEN` from a Cloudflare Realtime TURN key (short-lived credentials are fetched and
  cached), or `TURN_URLS` (comma-separated), `TURN_USERNAME` and `TURN_CREDENTIAL` for any other
  TURN server. Without them voice connects directly, which some networks block.

## Deploy to Cloudflare

1. `npm install` and `npx wrangler login` (or set `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`).
2. In `wrangler.jsonc` set `ALLOWED_ORIGINS` to your frontend origin(s), e.g.
   `"https://joker.example.com"` (comma-separated for several; `*` allows all).
3. Optional custom subdomain for the API: uncomment `routes` in `wrangler.jsonc`, e.g.
   `{ "pattern": "joker-api.example.com", "custom_domain": true }` (the zone must be on Cloudflare).
4. `npm run deploy`. The first deploy creates the `Room` Durable Object class (SQLite-backed,
   works on the Workers Free plan).
5. Give the frontend the URL you get (`https://joker-backend.<account>.workers.dev` or your
   custom domain) as `VITE_API_URL`.

Alternatively, route the API under the same hostname as the site (for example
`joker.example.com/api/*`) and leave `VITE_API_URL` empty: the frontend then calls its own origin.
