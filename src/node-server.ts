// Node adapter: the same API and WebSocket protocol as the Cloudflare Worker,
// for hosts like Railway. Rooms live in memory (and are lost on restart).
//   PORT             port to listen on (default 8787)
//   ALLOWED_ORIGINS  comma-separated frontend origins, or "*" (default)

import { createServer, IncomingMessage, ServerResponse } from "node:http";
import { randomInt } from "node:crypto";
import { WebSocket, WebSocketServer } from "ws";
import { Conn, RoomCore, UserInfo } from "./room-core";
import { InputError, User, makeStore, parseAvatar } from "./store";

const PORT = Number(process.env.PORT ?? 8787);
const ALLOWED = (process.env.ALLOWED_ORIGINS ?? "*").split(",").map((s) => s.trim()).filter(Boolean);
const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const CODE_RE = /^[A-HJ-NP-Z2-9]{5}$/;

interface RoomEntry {
  core: RoomCore;
  sockets: Set<WebSocket>;
  timer?: NodeJS.Timeout;
  /** Serialises work per room, like a Durable Object does. */
  queue: Promise<void>;
}

const rooms = new Map<string, RoomEntry>();
const store = makeStore();
/** Public URL prefix for avatars (absolute so other origins can load them). */
const PUBLIC_URL = (process.env.PUBLIC_URL ?? "").replace(/\/$/, "");

function avatarUrl(u: User): string {
  return `${PUBLIC_URL}/api/users/${u.id}/avatar?v=${u.avatarVersion}`;
}

function userInfo(u: User): UserInfo {
  return { id: u.id, name: u.nickname, avatarUrl: avatarUrl(u), rating: u.rating };
}

function publicUser(u: User) {
  return {
    id: u.id,
    nickname: u.nickname,
    firstName: u.firstName,
    lastName: u.lastName,
    rating: u.rating,
    games: u.games,
    wins: u.wins,
    peak: u.peak,
    avatarUrl: avatarUrl(u),
  };
}
const playerIds = new WeakMap<WebSocket, string | null>();

function wrap(ws: WebSocket): Conn {
  return {
    send: (t) => ws.readyState === WebSocket.OPEN && ws.send(t),
    close: (c, r) => ws.close(c, r),
    getPlayerId: () => playerIds.get(ws) ?? null,
    setPlayerId: (id) => playerIds.set(ws, id),
  };
}

function run(entry: RoomEntry, fn: () => Promise<void>): void {
  entry.queue = entry.queue.then(fn).catch((e) => console.error(e));
}

function makeRoom(code: string): RoomEntry {
  const entry: RoomEntry = { sockets: new Set(), queue: Promise.resolve(), core: null as unknown as RoomCore };
  entry.core = new RoomCore(
    {
      connections: () => [...entry.sockets].map(wrap),
      persist: async (d) => {
        if (!d) {
          clearTimeout(entry.timer);
          rooms.delete(code);
        }
      },
      resolveUser: async (token) => {
        const u = await store.userByToken(token);
        return u ? userInfo(u) : null;
      },
      rateGame: (game) => store.rateGame(game),
      penalize: (userId, amount, reason, code) => store.penalize(userId, amount, reason, code),
      setTimer: async (at) => {
        clearTimeout(entry.timer);
        entry.timer = setTimeout(() => run(entry, () => entry.core.onTimer()), Math.max(0, at - Date.now()));
      },
    },
    null,
  );
  return entry;
}

function originOk(origin: string | undefined): boolean {
  return !origin || ALLOWED.includes("*") || ALLOWED.includes(origin);
}

function send(req: IncomingMessage, res: ServerResponse, status: number, body: unknown): void {
  const origin = req.headers.origin;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (origin && originOk(origin)) {
    headers["Access-Control-Allow-Origin"] = origin;
    headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,OPTIONS";
    headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization";
    headers["Vary"] = "Origin";
  }
  res.writeHead(status, headers);
  res.end(body === null ? undefined : JSON.stringify(body));
}

async function readJson(req: IncomingMessage, limit = 600 * 1024): Promise<Record<string, unknown>> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const c of req) {
    size += (c as Buffer).length;
    if (size > limit) throw new InputError("Request too large");
    chunks.push(c as Buffer);
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  } catch {
    throw new InputError("Bad JSON");
  }
}

async function authUser(req: IncomingMessage): Promise<User | null> {
  const m = (req.headers.authorization ?? "").match(/^Bearer (\S{20,128})$/);
  return m ? store.userByToken(m[1]) : null;
}

// Failed logins per IP: at most 10 in 10 minutes.
const loginFails = new Map<string, number[]>();
function clientIp(req: IncomingMessage): string {
  return String(req.headers["x-forwarded-for"] ?? req.socket.remoteAddress ?? "").split(",")[0].trim();
}
function loginAllowed(ip: string): boolean {
  const now = Date.now();
  const list = (loginFails.get(ip) ?? []).filter((t) => now - t < 10 * 60_000);
  loginFails.set(ip, list);
  if (loginFails.size > 10_000) loginFails.clear();
  return list.length < 10;
}
function loginFailed(ip: string): void {
  loginFails.set(ip, [...(loginFails.get(ip) ?? []), Date.now()]);
}

const server = createServer((req, res) => {
  handle(req, res).catch((e) => {
    if (e instanceof InputError) return send(req, res, 400, { error: e.message });
    console.error(e);
    send(req, res, 500, { error: "server error" });
  });
});

async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = new URL(req.url ?? "/", "http://x");
  if (req.method === "OPTIONS") return send(req, res, 204, null);
  if (url.pathname === "/" || url.pathname === "/api/health") {
    return send(req, res, 200, { ok: true, service: "joker-backend", rooms: rooms.size });
  }
  if (url.pathname === "/api/rooms" && req.method === "POST") {
    if (!originOk(req.headers.origin)) return send(req, res, 403, { error: "origin not allowed" });
    const body = await readJson(req, 4096).catch(() => ({}) as Record<string, unknown>);
    const game = body.game === "bura" ? "bura" : "joker";
    let code = "";
    do {
      code = Array.from({ length: 5 }, () => CODE_ALPHABET[randomInt(CODE_ALPHABET.length)]).join("");
    } while (rooms.has(code));
    const entry = makeRoom(code);
    rooms.set(code, entry);
    await entry.core.init(code, game);
    return send(req, res, 201, { code });
  }
  if (url.pathname === "/api/register" && req.method === "POST") {
    if (!originOk(req.headers.origin)) return send(req, res, 403, { error: "origin not allowed" });
    const body = await readJson(req);
    const { token, user } = await store.register({
      nickname: String(body.nickname ?? ""),
      firstName: String(body.firstName ?? ""),
      lastName: String(body.lastName ?? ""),
      avatar: parseAvatar(body.avatar),
      password: String(body.password ?? ""),
    });
    return send(req, res, 201, { token, user: { ...publicUser(user), hasPassword: user.hasPassword } });
  }
  if (url.pathname === "/api/login" && req.method === "POST") {
    if (!originOk(req.headers.origin)) return send(req, res, 403, { error: "origin not allowed" });
    const ip = clientIp(req);
    if (!loginAllowed(ip)) return send(req, res, 429, { error: "Too many attempts. Try again in a few minutes." });
    const body = await readJson(req, 4096);
    try {
      const { token, user } = await store.login(String(body.nickname ?? ""), String(body.password ?? ""));
      return send(req, res, 200, { token, user: { ...publicUser(user), hasPassword: user.hasPassword } });
    } catch (e) {
      if (e instanceof InputError) loginFailed(ip);
      throw e;
    }
  }
  if (url.pathname === "/api/logout" && req.method === "POST") {
    const m = (req.headers.authorization ?? "").match(/^Bearer (\S{20,128})$/);
    if (m) await store.logout(m[1]);
    return send(req, res, 200, { ok: true });
  }
  if (url.pathname === "/api/me") {
    const me = await authUser(req);
    if (!me) return send(req, res, 401, { error: "not registered" });
    if (req.method === "PUT") {
      if (!originOk(req.headers.origin)) return send(req, res, 403, { error: "origin not allowed" });
      const body = await readJson(req);
      const updated = await store.updateProfile(me.id, {
        nickname: body.nickname === undefined ? undefined : String(body.nickname),
        firstName: body.firstName === undefined ? undefined : String(body.firstName),
        lastName: body.lastName === undefined ? undefined : String(body.lastName),
        avatar: body.avatar ? parseAvatar(body.avatar) : undefined,
        password: body.password ? String(body.password) : undefined,
      });
      return send(req, res, 200, { user: { ...publicUser(updated), hasPassword: updated.hasPassword } });
    }
    return send(req, res, 200, { user: { ...publicUser(me), hasPassword: me.hasPassword } });
  }
  if (url.pathname === "/api/leaderboard") {
    const limit = Math.min(200, Math.max(1, Number(url.searchParams.get("limit") ?? 100) || 100));
    const rows = await store.leaderboard(limit);
    return send(req, res, 200, { players: rows.map((u) => ({ ...publicUser(u), rank: u.rank })) });
  }
  const av = url.pathname.match(/^\/api\/users\/([0-9a-f-]{36})\/avatar$/i);
  if (av) {
    const a = await store.avatar(av[1]);
    if (!a) return send(req, res, 404, { error: "not found" });
    res.writeHead(200, {
      "Content-Type": a.type,
      "Content-Length": a.data.length,
      "Cache-Control": "public, max-age=31536000, immutable",
      "Access-Control-Allow-Origin": "*",
      "X-Content-Type-Options": "nosniff",
    });
    res.end(a.data);
    return;
  }
  const up = url.pathname.match(/^\/api\/users\/([0-9a-f-]{36})$/i);
  if (up) {
    const u = await store.userById(up[1]);
    if (!u) return send(req, res, 404, { error: "not found" });
    return send(req, res, 200, { user: publicUser(u), games: await store.recentGames(u.id, 20) });
  }
  const ug = url.pathname.match(/^\/api\/users\/([0-9a-f-]{36})\/games$/i);
  if (ug) {
    const limit = Math.min(50, Math.max(1, Number(url.searchParams.get("limit") ?? 20) || 20));
    return send(req, res, 200, { games: await store.recentGames(ug[1], limit, url.searchParams.get("before") ?? undefined) });
  }
  const gm = url.pathname.match(/^\/api\/games\/([0-9a-f-]{36})$/i);
  if (gm) {
    const m = await store.match(gm[1]);
    return m ? send(req, res, 200, { match: m }) : send(req, res, 404, { error: "not found" });
  }
  const m = url.pathname.match(/^\/api\/rooms\/([A-Za-z0-9]+)$/);
  if (m) {
    const info = rooms.get(m[1].toUpperCase())?.core.info();
    return info ? send(req, res, 200, info) : send(req, res, 404, { error: "room not found" });
  }
  send(req, res, 404, { error: "not found" });
}

const wss = new WebSocketServer({ noServer: true, maxPayload: 4096 });

server.on("upgrade", (req, socket, head) => {
  const url = new URL(req.url ?? "/", "http://x");
  const m = url.pathname.match(/^\/api\/rooms\/([A-Za-z0-9]+)\/ws$/);
  const code = m?.[1].toUpperCase() ?? "";
  const entry = CODE_RE.test(code) ? rooms.get(code) : undefined;
  if (!entry || !originOk(req.headers.origin)) {
    socket.write(`HTTP/1.1 ${entry ? 403 : 404} ${entry ? "Forbidden" : "Not Found"}\r\n\r\n`);
    socket.destroy();
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    entry.sockets.add(ws);
    playerIds.set(ws, null);
    let alive = true;
    ws.on("pong", () => (alive = true));
    const ping = setInterval(() => {
      if (!alive) return ws.terminate();
      alive = false;
      ws.ping();
    }, 30_000);
    ws.on("message", (data) => run(entry, () => entry.core.onMessage(wrap(ws), data.toString())));
    ws.on("close", () => {
      clearInterval(ping);
      entry.sockets.delete(ws);
      run(entry, () => entry.core.onClose(wrap(ws)));
    });
    ws.on("error", () => ws.close());
  });
});

store
  .init()
  .then(() => server.listen(PORT, () => console.log(`joker-backend listening on :${PORT} (${process.env.DATABASE_URL ? "postgres" : "memory store"})`)))
  .catch((e) => {
    console.error("database init failed", e);
    process.exit(1);
  });
