# GamesBack — Joker game server

Backend for **Joker** (ჯოკერი), the Georgian four-player trick-taking card game.
It runs on **Cloudflare Workers**: one Worker for the small HTTP API and one
**Durable Object per game room**, which holds the game state and the players'
WebSocket connections. The server is authoritative; clients only send intents.

Frontend: [LevanJokhadze/Games](https://github.com/LevanJokhadze/Games).

## Features

- Full rules engine (`src/engine`): deck, dealing, trump, bidding with the dealer restriction,
  joker play (high/low, leading with a named suit), trick resolution, scoring, khishti and premium.
- Online rooms with 5-letter codes, reconnect by player id, host controls, spectators.
- Bots fill empty seats. A human whose turn lasts 60 s (12 s if disconnected) is auto-played by a bot, so games never stall.
- Rooms delete themselves after 12 hours without activity.
- WebSocket Hibernation API, so idle rooms cost nothing.

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

House rules vary a lot between families and apps; these are easy to change in `src/engine/game.ts`.

## Development

```bash
npm install
npm run dev        # wrangler dev on http://localhost:8787
npm test           # engine tests (vitest)
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

WebSocket messages from the client (JSON): `hello {playerId, name}` (send first), `start`,
`config {config: {mode, khishti}}`, `seat {seat}`, `addBot {seat}`, `removeSeat {seat}`,
`trump {suit|null}`, `bid {bid}`, `play {cardId, joker?: {type: "high"|"low", suit?}}`,
`chat {text}`, `rematch`, `ping`.
The server answers with `state {room}` (a per-player view, own cards only) or `error {message}`.

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
