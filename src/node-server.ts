// Node adapter: the same API and WebSocket protocol as the Cloudflare Worker,
// for hosts like Railway. Rooms live in memory (and are lost on restart).
//   PORT             port to listen on (default 8787)
//   ALLOWED_ORIGINS  comma-separated frontend origins, or "*" (default)

import { createServer, IncomingMessage, ServerResponse } from "node:http";
import { randomInt } from "node:crypto";
import { WebSocket, WebSocketServer } from "ws";
import { Conn, RoomCore } from "./room-core";

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
    headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS";
    headers["Access-Control-Allow-Headers"] = "Content-Type";
    headers["Vary"] = "Origin";
  }
  res.writeHead(status, headers);
  res.end(body === null ? undefined : JSON.stringify(body));
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url ?? "/", "http://x");
  if (req.method === "OPTIONS") return send(req, res, 204, null);
  if (url.pathname === "/" || url.pathname === "/api/health") {
    return send(req, res, 200, { ok: true, service: "joker-backend", rooms: rooms.size });
  }
  if (url.pathname === "/api/rooms" && req.method === "POST") {
    if (!originOk(req.headers.origin)) return send(req, res, 403, { error: "origin not allowed" });
    let code = "";
    do {
      code = Array.from({ length: 5 }, () => CODE_ALPHABET[randomInt(CODE_ALPHABET.length)]).join("");
    } while (rooms.has(code));
    const entry = makeRoom(code);
    rooms.set(code, entry);
    await entry.core.init(code);
    return send(req, res, 201, { code });
  }
  const m = url.pathname.match(/^\/api\/rooms\/([A-Za-z0-9]+)$/);
  if (m) {
    const info = rooms.get(m[1].toUpperCase())?.core.info();
    return info ? send(req, res, 200, info) : send(req, res, 404, { error: "room not found" });
  }
  send(req, res, 404, { error: "not found" });
});

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

server.listen(PORT, () => console.log(`joker-backend listening on :${PORT}`));
