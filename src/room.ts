// Cloudflare adapter: one Durable Object per game room, using the WebSocket
// Hibernation API. All game logic lives in RoomCore (room-core.ts).

import { DurableObject } from "cloudflare:workers";
import { Conn, RoomCore, RoomData, parseGame } from "./room-core";

export interface Env {
  ROOMS: DurableObjectNamespace<Room>;
  ALLOWED_ORIGINS?: string;
}

interface Attachment {
  playerId: string | null;
}

function wrap(ws: WebSocket): Conn {
  return {
    send: (t) => ws.send(t),
    close: (c, r) => ws.close(c, r),
    getPlayerId: () => (ws.deserializeAttachment() as Attachment | null)?.playerId ?? null,
    setPlayerId: (id) => ws.serializeAttachment({ playerId: id } satisfies Attachment),
  };
}

export class Room extends DurableObject<Env> {
  private core!: RoomCore;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      const data = (await ctx.storage.get<RoomData>("room")) ?? null;
      this.core = new RoomCore(
        {
          connections: () => ctx.getWebSockets().map(wrap),
          persist: async (d) => {
            if (d) await ctx.storage.put("room", d);
            else await ctx.storage.deleteAll();
          },
          setTimer: async (at) => ctx.storage.setAlarm(at),
        },
        data,
      );
    });
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/init" && request.method === "POST") {
      const { code, game } = (await request.json()) as { code: string; game?: string };
      if (!(await this.core.init(code, parseGame(game) ?? "joker"))) return new Response("exists", { status: 409 });
      return Response.json({ code });
    }
    if (url.pathname === "/info") {
      const info = this.core.info();
      return info ? Response.json(info) : new Response("not found", { status: 404 });
    }
    if (url.pathname === "/ws") {
      if (!this.core.data) return new Response("room not found", { status: 404 });
      if (request.headers.get("Upgrade") !== "websocket") {
        return new Response("expected websocket", { status: 426 });
      }
      const pair = new WebSocketPair();
      const [client, server] = [pair[0], pair[1]];
      this.ctx.acceptWebSocket(server);
      server.serializeAttachment({ playerId: null } satisfies Attachment);
      return new Response(null, { status: 101, webSocket: client });
    }
    return new Response("not found", { status: 404 });
  }

  async webSocketMessage(ws: WebSocket, raw: string | ArrayBuffer): Promise<void> {
    await this.core.onMessage(wrap(ws), typeof raw === "string" ? raw : new TextDecoder().decode(raw));
  }

  async webSocketClose(ws: WebSocket): Promise<void> {
    await this.core.onClose(wrap(ws));
  }

  async webSocketError(ws: WebSocket): Promise<void> {
    await this.core.onClose(wrap(ws));
  }

  async alarm(): Promise<void> {
    await this.core.onTimer();
  }
}
