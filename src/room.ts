// One Durable Object per game room. Holds the seats, the game state and the
// WebSocket connections. The server is authoritative: clients only send intents.

import { DurableObject } from "cloudflare:workers";
import {
  BotAction,
  DEFAULT_CONFIG,
  GameConfig,
  GameState,
  JokerMode,
  RuleError,
  SEATS,
  Suit,
  applyAction,
  botAction,
  forbiddenBid,
  legalBids,
  legalCards,
  newGame,
  visibleHand,
} from "./engine";

export interface Env {
  ROOMS: DurableObjectNamespace<Room>;
  ALLOWED_ORIGINS?: string;
}

interface Seat {
  playerId: string | null; // null = bot
  name: string;
}

interface ChatLine {
  seat: number | null;
  name: string;
  text: string;
  at: number;
}

interface RoomData {
  code: string;
  createdAt: number;
  lastActivity: number;
  hostId: string | null;
  status: "lobby" | "playing" | "finished";
  seats: (Seat | null)[];
  config: GameConfig;
  game: GameState | null;
  /** When the current actor will be auto-played (bots, timeouts). */
  deadline: number | null;
  chat: ChatLine[];
  /** Identifies whose turn the deadline was computed for. */
  turnKey?: string;
}

type ClientMsg =
  | { t: "hello"; playerId: string; name: string }
  | { t: "start" }
  | { t: "config"; config: Partial<GameConfig> }
  | { t: "seat"; seat: number }
  | { t: "addBot"; seat: number }
  | { t: "removeSeat"; seat: number }
  | { t: "trump"; suit: Suit | null }
  | { t: "bid"; bid: number }
  | { t: "play"; cardId: string; joker?: JokerMode }
  | { t: "chat"; text: string }
  | { t: "rematch" }
  | { t: "ping" };

interface Attachment {
  playerId: string | null;
}

const BOT_NAMES = ["Bot Nino", "Bot Gio", "Bot Tamo", "Bot Luka"];
const BOT_DELAY = 750;
const AFTER_TRICK_DELAY = 1600;
const HUMAN_TURN_MS = 60_000;
const DISCONNECTED_TURN_MS = 12_000;
const IDLE_CLEANUP_MS = 12 * 60 * 60 * 1000;
const KHISHTI_OPTIONS = [-200, -500, -900];

function cleanName(name: unknown): string {
  const n = String(name ?? "").replace(/[\u0000-\u001f]/g, "").trim().slice(0, 18);
  return n || "Player";
}

export class Room extends DurableObject<Env> {
  private data: RoomData | null = null;

  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      this.data = (await ctx.storage.get<RoomData>("room")) ?? null;
    });
  }

  private async save(): Promise<void> {
    if (!this.data) return;
    this.data.lastActivity = Date.now();
    await this.ctx.storage.put("room", this.data);
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (url.pathname === "/init" && request.method === "POST") {
      if (this.data) return new Response("exists", { status: 409 });
      const { code } = (await request.json()) as { code: string };
      this.data = {
        code,
        createdAt: Date.now(),
        lastActivity: Date.now(),
        hostId: null,
        status: "lobby",
        seats: [null, null, null, null],
        config: { ...DEFAULT_CONFIG },
        game: null,
        deadline: null,
        chat: [],
      };
      await this.save();
      await this.ctx.storage.setAlarm(Date.now() + IDLE_CLEANUP_MS);
      return Response.json({ code });
    }
    if (url.pathname === "/info") {
      if (!this.data) return new Response("not found", { status: 404 });
      const d = this.data;
      return Response.json({
        code: d.code,
        status: d.status,
        players: d.seats.filter((s) => s && s.playerId).length,
        open: d.seats.filter((s) => !s).length,
      });
    }
    if (url.pathname === "/ws") {
      if (!this.data) return new Response("room not found", { status: 404 });
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

  // ---------- WebSocket handlers (hibernation API) ----------

  async webSocketMessage(ws: WebSocket, raw: string | ArrayBuffer): Promise<void> {
    if (!this.data) {
      ws.close(4004, "room not found");
      return;
    }
    let msg: ClientMsg;
    try {
      msg = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw));
    } catch {
      return this.sendError(ws, "Bad message");
    }
    const att = (ws.deserializeAttachment() as Attachment | null) ?? { playerId: null };
    try {
      if (msg.t === "ping") {
        ws.send(JSON.stringify({ t: "pong" }));
        return;
      }
      if (msg.t === "hello") {
        await this.onHello(ws, msg.playerId, msg.name);
        return;
      }
      if (!att.playerId) return this.sendError(ws, "Say hello first");
      await this.onAction(att.playerId, msg);
    } catch (e) {
      if (e instanceof RuleError) return this.sendError(ws, e.message);
      console.error(e);
      return this.sendError(ws, "Something went wrong");
    }
  }

  async webSocketClose(ws: WebSocket): Promise<void> {
    ws.serializeAttachment({ playerId: null });
    await this.afterChange();
  }

  async webSocketError(ws: WebSocket): Promise<void> {
    await this.webSocketClose(ws);
  }

  private sendError(ws: WebSocket, message: string): void {
    try {
      ws.send(JSON.stringify({ t: "error", message }));
    } catch {
      /* socket gone */
    }
  }

  private seatOf(playerId: string): number | null {
    const i = this.data!.seats.findIndex((s) => s?.playerId === playerId);
    return i >= 0 ? i : null;
  }

  private connectedIds(): Set<string> {
    const ids = new Set<string>();
    for (const ws of this.ctx.getWebSockets()) {
      const a = ws.deserializeAttachment() as Attachment | null;
      if (a?.playerId) ids.add(a.playerId);
    }
    return ids;
  }

  private async onHello(ws: WebSocket, playerIdRaw: string, nameRaw: string): Promise<void> {
    const d = this.data!;
    const playerId = String(playerIdRaw ?? "").slice(0, 64);
    if (!/^[A-Za-z0-9_-]{8,64}$/.test(playerId)) throw new RuleError("Bad player id");
    const name = cleanName(nameRaw);
    ws.serializeAttachment({ playerId } satisfies Attachment);
    let seat = this.seatOf(playerId);
    if (seat === null && d.status === "lobby") {
      const free = d.seats.findIndex((s) => s === null);
      if (free >= 0) {
        d.seats[free] = { playerId, name };
        seat = free;
      }
    }
    if (seat !== null) d.seats[seat]!.name = name;
    if (!d.hostId || (d.hostId && this.seatOf(d.hostId) === null && seat !== null)) d.hostId = playerId;
    await this.afterChange();
  }

  private requireHost(playerId: string): void {
    if (this.data!.hostId !== playerId) throw new RuleError("Only the host can do that");
  }

  private async onAction(playerId: string, msg: ClientMsg): Promise<void> {
    const d = this.data!;
    const seat = this.seatOf(playerId);
    switch (msg.t) {
      case "config": {
        this.requireHost(playerId);
        if (d.status !== "lobby") throw new RuleError("The game has started");
        const mode = msg.config.mode === "nines" ? "nines" : msg.config.mode === "standard" ? "standard" : d.config.mode;
        const kh = Number(msg.config.khishti);
        const khishti = KHISHTI_OPTIONS.includes(kh) ? kh : d.config.khishti;
        d.config = { mode, khishti };
        break;
      }
      case "seat": {
        if (d.status !== "lobby") throw new RuleError("The game has started");
        const to = Number(msg.seat);
        if (!(to >= 0 && to < SEATS) || d.seats[to]) throw new RuleError("That seat is taken");
        if (seat === null) throw new RuleError("You are watching this game");
        d.seats[to] = d.seats[seat];
        d.seats[seat] = null;
        break;
      }
      case "addBot": {
        this.requireHost(playerId);
        if (d.status !== "lobby") throw new RuleError("The game has started");
        const at = Number(msg.seat);
        if (!(at >= 0 && at < SEATS) || d.seats[at]) throw new RuleError("That seat is taken");
        d.seats[at] = { playerId: null, name: BOT_NAMES[at] };
        break;
      }
      case "removeSeat": {
        this.requireHost(playerId);
        if (d.status !== "lobby") throw new RuleError("The game has started");
        const at = Number(msg.seat);
        if (!(at >= 0 && at < SEATS) || !d.seats[at]) throw new RuleError("Seat is empty");
        if (d.seats[at]!.playerId === playerId) throw new RuleError("You can't remove yourself");
        d.seats[at] = null;
        break;
      }
      case "start": {
        this.requireHost(playerId);
        if (d.status !== "lobby") throw new RuleError("The game has started");
        for (let i = 0; i < SEATS; i++) if (!d.seats[i]) d.seats[i] = { playerId: null, name: BOT_NAMES[i] };
        d.game = newGame(d.config, Math.floor(Math.random() * 2 ** 31));
        d.status = "playing";
        break;
      }
      case "rematch": {
        this.requireHost(playerId);
        if (d.status !== "finished") throw new RuleError("The game is not over");
        d.status = "lobby";
        d.game = null;
        // Free the seats of players who left; keep bots and connected people.
        const online = this.connectedIds();
        d.seats = d.seats.map((s) => (s && (s.playerId === null || online.has(s.playerId)) ? s : null));
        break;
      }
      case "chat": {
        const text = String(msg.text ?? "").replace(/[\u0000-\u001f]/g, " ").trim().slice(0, 200);
        if (!text) return;
        const name = seat !== null ? d.seats[seat]!.name : "Guest";
        d.chat.push({ seat, name, text, at: Date.now() });
        if (d.chat.length > 40) d.chat = d.chat.slice(-40);
        break;
      }
      case "trump":
      case "bid":
      case "play": {
        if (d.status !== "playing" || !d.game) throw new RuleError("No game in progress");
        if (seat === null) throw new RuleError("You are watching this game");
        const action: BotAction =
          msg.t === "trump"
            ? { t: "trump", suit: msg.suit ?? null }
            : msg.t === "bid"
              ? { t: "bid", bid: Number(msg.bid) }
              : { t: "play", cardId: String(msg.cardId), joker: msg.joker };
        d.game = applyAction(d.game, seat, action);
        if (d.game.phase === "gameOver") d.status = "finished";
        break;
      }
      default:
        throw new RuleError("Unknown message");
    }
    await this.afterChange();
  }

  // ---------- Turn timing ----------

  private computeDeadline(): number | null {
    const d = this.data!;
    const g = d.game;
    if (d.status !== "playing" || !g || g.phase === "gameOver") return null;
    const seat = d.seats[g.turn];
    const justFinishedTrick = g.phase === "playing" && g.trick.length === 0 && g.lastTrick !== null;
    const extra = justFinishedTrick ? AFTER_TRICK_DELAY : 0;
    if (!seat || seat.playerId === null) return Date.now() + BOT_DELAY + extra;
    const online = this.connectedIds().has(seat.playerId);
    return Date.now() + (online ? HUMAN_TURN_MS : DISCONNECTED_TURN_MS) + extra;
  }

  private turnKey(): string {
    const g = this.data!.game;
    if (!g) return "";
    return `${g.dealsPlayed}:${g.phase}:${g.turn}:${g.trick.length}:${g.hands[g.turn]?.length ?? 0}`;
  }

  private async afterChange(): Promise<void> {
    const d = this.data!;
    // Reset the turn clock when the actor changes or their connection changes.
    const key = this.turnKey();
    const g = d.game;
    const onlineKey = g && d.seats[g.turn]?.playerId ? String(this.connectedIds().has(d.seats[g.turn]!.playerId!)) : "";
    const fullKey = `${key}|${onlineKey}`;
    if (fullKey !== d.turnKey || d.deadline === null) {
      d.deadline = this.computeDeadline();
      d.turnKey = fullKey;
    }
    if (d.status !== "playing") d.deadline = null;
    await this.save();
    await this.ctx.storage.setAlarm(d.deadline ?? Date.now() + IDLE_CLEANUP_MS);
    this.broadcast();
  }

  async alarm(): Promise<void> {
    const d = this.data;
    if (!d) return;
    if (d.status === "playing" && d.game && d.deadline !== null) {
      if (Date.now() < d.deadline - 50) {
        await this.ctx.storage.setAlarm(d.deadline);
        return;
      }
      const g = d.game;
      const seat = g.turn;
      try {
        d.game = applyAction(g, seat, botAction(g, seat));
      } catch (e) {
        console.error("auto play failed", e);
      }
      if (d.game.phase === "gameOver") d.status = "finished";
      d.deadline = null;
      await this.afterChange();
      return;
    }
    // Nothing pending: clean up rooms nobody has touched for a long time.
    if (this.ctx.getWebSockets().length === 0 && Date.now() - d.lastActivity > IDLE_CLEANUP_MS - 60_000) {
      await this.ctx.storage.deleteAll();
      this.data = null;
      return;
    }
    await this.ctx.storage.setAlarm(Date.now() + IDLE_CLEANUP_MS);
  }

  // ---------- Views ----------

  private broadcast(): void {
    const online = this.connectedIds();
    for (const ws of this.ctx.getWebSockets()) {
      const a = ws.deserializeAttachment() as Attachment | null;
      if (!a?.playerId) continue;
      try {
        ws.send(JSON.stringify({ t: "state", room: this.view(a.playerId, online) }));
      } catch {
        /* closed */
      }
    }
  }

  private view(playerId: string, online: Set<string>) {
    const d = this.data!;
    const you = this.seatOf(playerId);
    const g = d.game;
    return {
      code: d.code,
      status: d.status,
      isHost: d.hostId === playerId,
      you,
      config: d.config,
      deadline: d.deadline,
      serverTime: Date.now(),
      chat: d.chat.slice(-30),
      seats: d.seats.map((s, i) =>
        s
          ? {
              name: s.name,
              bot: s.playerId === null,
              connected: s.playerId === null || online.has(s.playerId),
              host: s.playerId !== null && s.playerId === d.hostId,
              you: i === you,
            }
          : null,
      ),
      game: g
        ? {
            phase: g.phase,
            schedule: g.schedule,
            pulka: g.pulka,
            dealInPulka: g.dealInPulka,
            handSize: g.handSize,
            dealer: g.dealer,
            turn: g.turn,
            trump: g.trump,
            trumpCard: g.trumpCard,
            bids: g.bids,
            took: g.took,
            trick: g.trick,
            lastTrick: g.lastTrick,
            pulkas: g.pulkas,
            totals: g.totals,
            handCounts: g.hands.map((h) => h.length),
            hand: you !== null ? visibleHand(g, you) : [],
            legalCards: you !== null ? legalCards(g, you).map((c) => c.id) : [],
            legalBids: you !== null ? legalBids(g, you) : [],
            forbiddenBid: you !== null && g.phase === "bidding" ? forbiddenBid(g, you) : null,
          }
        : null,
    };
  }
}
