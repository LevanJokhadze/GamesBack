// Platform-neutral room logic. Used by the Cloudflare Durable Object (room.ts)
// and by the Node server (node-server.ts). The server is authoritative:
// clients only send intents.

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

/** One client connection. */
export interface Conn {
  send(text: string): void;
  close(code: number, reason: string): void;
  getPlayerId(): string | null;
  setPlayerId(id: string | null): void;
}

/** What the platform provides to a room. */
export interface RoomHost {
  connections(): Conn[];
  persist(data: RoomData | null): Promise<void>;
  /** Ask for onTimer() to be called at this time (replaces any earlier request). */
  setTimer(at: number): Promise<void>;
  /**
   * Accounts (optional). When present, players must send a valid account token
   * in "hello", and finished games are rated.
   */
  resolveUser?(token: string): Promise<UserInfo | null>;
  rateGame?(game: FinishedGame): Promise<{ matchId: string; changes: (RatingChange | null)[] }>;
}

export interface UserInfo {
  id: string;
  name: string;
  avatarUrl: string | null;
  rating: number;
}

export interface FinishedGame {
  code: string;
  mode: string;
  khishti: number;
  /** Played in pairs (seats 0 & 2 against 1 & 3). */
  pairs: boolean;
  /** userId per seat, null for bots. */
  userIds: (string | null)[];
  /** Display name per seat at the time of the game. */
  names: string[];
  totals: number[];
  /** Full per-deal results, for the match summary. */
  pulkas: unknown[];
}

export interface RatingChange {
  before: number;
  after: number;
  delta: number;
}

interface Seat {
  playerId: string | null; // null = bot
  name: string;
  userId?: string | null;
  avatarUrl?: string | null;
  rating?: number | null;
}

interface ChatLine {
  seat: number | null;
  name: string;
  text: string;
  at: number;
}

export interface RoomData {
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
  /** Rating changes of the last finished game, per seat. */
  ratingChanges?: (RatingChange | null)[] | null;
  matchId?: string | null;
}

type ClientMsg =
  | { t: "hello"; playerId?: string; name?: string; token?: string }
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

const BOT_NAMES = ["Bot Nino", "Bot Gio", "Bot Tamo", "Bot Luka"];
// BOT_DELAY_MS lets tests run whole games quickly (Node only).
const envDelay = Number((globalThis as { process?: { env?: Record<string, string> } }).process?.env?.BOT_DELAY_MS);
const BOT_DELAY = envDelay >= 0 ? envDelay : 750;
const AFTER_TRICK_DELAY = envDelay >= 0 ? envDelay : 1600;
const HUMAN_TURN_MS = 60_000;
const DISCONNECTED_TURN_MS = 12_000;
const IDLE_CLEANUP_MS = 12 * 60 * 60 * 1000;
const KHISHTI_OPTIONS = [-200, -500, -900];

function cleanName(name: unknown): string {
  const n = String(name ?? "").replace(/[\u0000-\u001f]/g, "").trim().slice(0, 18);
  return n || "Player";
}

export class RoomCore {

  constructor(private host: RoomHost, public data: RoomData | null) {}

  private async save(): Promise<void> {
    if (!this.data) return;
    this.data.lastActivity = Date.now();
    await this.host.persist(this.data);
  }

  async init(code: string): Promise<boolean> {
    if (this.data) return false;
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
    await this.host.setTimer(Date.now() + IDLE_CLEANUP_MS);
    return true;
  }

  info() {
    const d = this.data;
    if (!d) return null;
    return {
      code: d.code,
      status: d.status,
      players: d.seats.filter((s) => s && s.playerId).length,
      open: d.seats.filter((s) => !s).length,
    };
  }

  async onMessage(ws: Conn, raw: string): Promise<void> {
    if (!this.data) {
      ws.close(4004, "room not found");
      return;
    }
    let msg: ClientMsg;
    try {
      msg = JSON.parse(raw);
    } catch {
      return this.sendError(ws, "Bad message");
    }
    const playerId = ws.getPlayerId();
    try {
      if (msg.t === "ping") {
        ws.send(JSON.stringify({ t: "pong" }));
        return;
      }
      if (msg.t === "hello") {
        await this.onHello(ws, msg);
        return;
      }
      if (!playerId) return this.sendError(ws, "Say hello first");
      await this.onAction(playerId, msg);
    } catch (e) {
      if (e instanceof RuleError) return this.sendError(ws, e.message);
      console.error(e);
      return this.sendError(ws, "Something went wrong");
    }
  }

  async onClose(ws: Conn): Promise<void> {
    ws.setPlayerId(null);
    if (this.data) await this.afterChange();
  }

  private sendError(ws: Conn, message: string): void {
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
    for (const ws of this.host.connections()) {
      const id = ws.getPlayerId();
      if (id) ids.add(id);
    }
    return ids;
  }

  private async onHello(ws: Conn, msg: { playerId?: string; name?: string; token?: string }): Promise<void> {
    const d = this.data!;
    let playerId: string;
    let profile: Omit<Seat, "playerId">;
    if (this.host.resolveUser) {
      const user = msg.token ? await this.host.resolveUser(String(msg.token).slice(0, 128)) : null;
      if (!user) {
        ws.send(JSON.stringify({ t: "error", code: "auth", message: "Please register first" }));
        return;
      }
      playerId = `u_${user.id}`;
      profile = { name: user.name, userId: user.id, avatarUrl: user.avatarUrl, rating: user.rating };
    } else {
      playerId = String(msg.playerId ?? "").slice(0, 64);
      if (!/^[A-Za-z0-9_-]{8,64}$/.test(playerId)) throw new RuleError("Bad player id");
      profile = { name: cleanName(msg.name) };
    }
    ws.setPlayerId(playerId);
    let seat = this.seatOf(playerId);
    if (seat === null && d.status === "lobby") {
      const free = d.seats.findIndex((s) => s === null);
      if (free >= 0) {
        d.seats[free] = { playerId, ...profile };
        seat = free;
      }
    }
    if (seat !== null) Object.assign(d.seats[seat]!, profile);
    if (!d.hostId || (d.hostId && this.seatOf(d.hostId) === null && seat !== null)) d.hostId = playerId;
    await this.afterChange();
  }

  /** Called once when a game reaches its end: rate it if accounts are enabled. */
  private async finishGame(): Promise<void> {
    const d = this.data!;
    d.status = "finished";
    d.ratingChanges = null;
    d.matchId = null;
    if (!this.host.rateGame || !d.game) return;
    try {
      const changes = await this.host.rateGame({
        code: d.code,
        mode: d.config.mode,
        khishti: d.config.khishti,
        pairs: Boolean(d.config.pairs),
        userIds: d.seats.map((s) => s?.userId ?? null),
        names: d.seats.map((s, i) => s?.name ?? `#${i + 1}`),
        totals: d.game.totals.slice(),
        pulkas: d.game.pulkas,
      });
      d.ratingChanges = changes.changes;
      d.matchId = changes.matchId;
      changes.changes.forEach((c, i) => {
        if (c && d.seats[i]) d.seats[i]!.rating = c.after;
      });
    } catch (e) {
      console.error("rating failed", e);
    }
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
        const pairs = typeof msg.config.pairs === "boolean" ? msg.config.pairs : d.config.pairs;
        d.config = { mode, khishti, pairs };
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
        d.ratingChanges = null;
        d.matchId = null;
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
        if (d.game.phase === "gameOver") await this.finishGame();
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
    await this.host.setTimer(d.deadline ?? Date.now() + IDLE_CLEANUP_MS);
    this.broadcast();
  }

  async onTimer(): Promise<void> {
    const d = this.data;
    if (!d) return;
    if (d.status === "playing" && d.game && d.deadline !== null) {
      if (Date.now() < d.deadline - 50) {
        await this.host.setTimer(d.deadline);
        return;
      }
      const g = d.game;
      const seat = g.turn;
      try {
        d.game = applyAction(g, seat, botAction(g, seat));
      } catch (e) {
        console.error("auto play failed", e);
      }
      if (d.game.phase === "gameOver") await this.finishGame();
      d.deadline = null;
      await this.afterChange();
      return;
    }
    // Nothing pending: clean up rooms nobody has touched for a long time.
    if (this.host.connections().length === 0 && Date.now() - d.lastActivity > IDLE_CLEANUP_MS - 60_000) {
      this.data = null;
      await this.host.persist(null);
      return;
    }
    await this.host.setTimer(Date.now() + IDLE_CLEANUP_MS);
  }

  // ---------- Views ----------

  private broadcast(): void {
    const online = this.connectedIds();
    for (const ws of this.host.connections()) {
      const id = ws.getPlayerId();
      if (!id) continue;
      try {
        ws.send(JSON.stringify({ t: "state", room: this.view(id, online) }));
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
      ratingChanges: d.ratingChanges ?? null,
      matchId: d.matchId ?? null,
      accounts: Boolean(this.host.resolveUser),
      seats: d.seats.map((s, i) =>
        s
          ? {
              name: s.name,
              avatarUrl: s.avatarUrl ?? null,
              rating: s.rating ?? null,
              userId: s.userId ?? null,
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
