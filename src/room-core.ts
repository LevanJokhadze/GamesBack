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
import { BuraAction, BuraState, applyBura, buraActor, buraAutoDelay, buraBot, buraView, newBura } from "./bura";
import {
  BIG_BLINDS,
  DEFAULT_BIG_BLIND,
  POKER_DEFAULT_SEATS,
  POKER_MAX_SEATS,
  POKER_MIN_SEATS,
  PokerAction,
  PokerState,
  applyPoker,
  newPoker,
  pokerAutoDelay,
  pokerBot,
  pokerLeave,
  pokerSit,
  pokerView,
} from "./poker";

/** Which card game a room plays. */
export type GameKind = "joker" | "bura" | "poker";

/** Room settings: the Joker options plus which game is played. */
export interface RoomConfig extends GameConfig {
  game: GameKind;
  /** Poker: table size (2-9). */
  seats?: number;
  /** Poker: big blind (the small blind is half, the buy-in 100 big blinds). */
  bb?: number;
}

type AnyGame = GameState | BuraState | PokerState;

function isBuraGame(g: AnyGame | null | undefined): g is BuraState {
  return (g as BuraState | null)?.kind === "bura";
}
function isPokerGame(g: AnyGame | null | undefined): g is PokerState {
  return (g as PokerState | null)?.kind === "poker";
}

function parseGame(v: unknown): GameKind | null {
  return v === "bura" || v === "joker" || v === "poker" ? v : null;
}

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
  /** Takes rating points from a player who stopped playing; returns the new rating. */
  penalize?(userId: string, amount: number, reason: string, roomCode: string): Promise<number | null>;
}

export interface UserInfo {
  id: string;
  name: string;
  avatarUrl: string | null;
  rating: number;
}

export interface FinishedGame {
  code: string;
  /** "offline" = played in person and written on a score sheet. Default "online". */
  source?: "online" | "offline";
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
  /** Stopped responding: a bot plays for them until they come back or this time passes. */
  afkKickAt?: number | null;
  /** Removed from the match for not coming back; a bot plays the seat. */
  left?: boolean;
}

/** Server notice shown in the chat (the client translates it). */
export interface SysNotice {
  kind: "warn" | "afk" | "back" | "kicked";
  name: string;
  /** Rating points taken, if any. */
  penalty?: number;
}

interface ChatLine {
  seat: number | null;
  name: string;
  text: string;
  at: number;
  sys?: SysNotice;
}

export interface RoomData {
  code: string;
  createdAt: number;
  lastActivity: number;
  hostId: string | null;
  status: "lobby" | "playing" | "finished";
  seats: (Seat | null)[];
  config: RoomConfig;
  game: AnyGame | null;
  /** When the current actor will be auto-played (bots, timeouts). */
  deadline: number | null;
  chat: ChatLine[];
  /** Identifies whose turn the deadline was computed for. */
  turnKey?: string;
  /** "turn": normal time to play; "warn": the extra minute before a bot takes over. */
  turnStage?: "turn" | "warn";
  /** Rating changes of the last finished game, per seat. */
  ratingChanges?: (RatingChange | null)[] | null;
  matchId?: string | null;
  /** Poker: players who stood up on purpose are not sat down again when they reconnect. */
  standing?: string[];
  /** Poker: chips won or lost by players who left the table this session. */
  ledger?: { name: string; net: number }[];
}

type ClientMsg =
  | { t: "hello"; playerId?: string; name?: string; token?: string }
  | { t: "start" }
  | { t: "config"; config: Partial<RoomConfig> }
  | { t: "seat"; seat: number }
  | { t: "addBot"; seat: number }
  | { t: "removeSeat"; seat: number }
  | { t: "trump"; suit: Suit | null }
  | { t: "bid"; bid: number }
  | { t: "play"; cardId: string; joker?: JokerMode }
  | { t: "bura"; action: BuraAction }
  | { t: "poker"; action: PokerAction }
  | { t: "stand" }
  | { t: "end" }
  | { t: "chat"; text: string }
  | { t: "rtc"; to: string; data: unknown }
  | { t: "voice"; on: boolean }
  | { t: "talk"; on: boolean }
  | { t: "pcm"; d: string }
  | { t: "back" }
  | { t: "rematch" }
  | { t: "ping" };

const BOT_NAMES = ["Bot Nino", "Bot Gio", "Bot Tamo", "Bot Luka", "Bot Ana", "Bot Dato", "Bot Keti", "Bot Saba", "Bot Mari"];
// BOT_DELAY_MS lets tests run whole games quickly (Node only).
const envDelay = Number((globalThis as { process?: { env?: Record<string, string> } }).process?.env?.BOT_DELAY_MS);
const BOT_DELAY = envDelay >= 0 ? envDelay : 750;
const AFTER_TRICK_DELAY = envDelay >= 0 ? envDelay : 1600;
// Time to play, then one warning minute, then a bot plays (-5) and the player has 2 minutes to
// come back before they are removed from the match (-25 more, 30 in all).
const envTurn = Number((globalThis as { process?: { env?: Record<string, string> } }).process?.env?.TURN_MS);
const TURN_MS = envTurn > 0 ? envTurn : 30_000;
const WARN_MS = envTurn > 0 ? envTurn * 2 : 60_000;
const KICK_MS = envTurn > 0 ? envTurn * 4 : 120_000;
export const AFK_PENALTY = 5;
export const KICK_PENALTY = 25;
const IDLE_CLEANUP_MS = 12 * 60 * 60 * 1000;
const KHISHTI_OPTIONS = [-200, -500, -900];

function cleanName(name: unknown): string {
  const n = String(name ?? "").replace(/[\u0000-\u001f]/g, "").trim().slice(0, 18);
  return n || "Player";
}

/**
 * Public id of a connected person, used for chat, the people list and voice-call signalling.
 * Derived from the private player id so clients never see each other's player ids.
 */
export function peerOf(playerId: string): string {
  let h1 = 0x811c9dc5;
  let h2 = 0x01000193;
  for (let i = 0; i < playerId.length; i++) {
    const c = playerId.charCodeAt(i);
    h1 = Math.imul(h1 ^ c, 0x01000193);
    h2 = Math.imul(h2 ^ c, 0x5bd1e995);
  }
  return ((h1 >>> 0).toString(36) + (h2 >>> 0).toString(36)).slice(0, 12);
}

interface Person {
  name: string;
  avatarUrl: string | null;
  /** Joined the voice chat. */
  voice: boolean;
  lastChat: number;
  /** Name, account and rating, for sitting down during a poker game. */
  profile?: Omit<Seat, "playerId">;
}

export class RoomCore {
  /** Who is connected (players and spectators); in memory only. */
  private people = new Map<string, Person>();

  constructor(private host: RoomHost, public data: RoomData | null) {}

  private async save(): Promise<void> {
    if (!this.data) return;
    this.data.lastActivity = Date.now();
    await this.host.persist(this.data);
  }

  async init(code: string, game: GameKind = "joker"): Promise<boolean> {
    if (this.data) return false;
    this.data = {
      code,
      createdAt: Date.now(),
      lastActivity: Date.now(),
      hostId: null,
      status: "lobby",
      seats: Array(game === "poker" ? POKER_DEFAULT_SEATS : SEATS).fill(null),
      config: {
        ...DEFAULT_CONFIG,
        game,
        pairs: game === "bura" ? true : game === "poker" ? false : DEFAULT_CONFIG.pairs,
        ...(game === "poker" ? { seats: POKER_DEFAULT_SEATS, bb: DEFAULT_BIG_BLIND } : {}),
      },
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
      game: d.config.game ?? "joker",
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
      if (msg.t === "rtc") return this.relayRtc(playerId, msg, raw.length);
      if (msg.t === "talk") return this.relayTalk(playerId, Boolean(msg.on));
      if (msg.t === "pcm") return this.relayPcm(playerId, msg.d);
      if (msg.t === "voice") {
        const p = this.people.get(playerId);
        if (p) p.voice = Boolean(msg.on);
        this.broadcast();
        return;
      }
      await this.onAction(playerId, msg);
    } catch (e) {
      if (e instanceof RuleError) return this.sendError(ws, e.message);
      console.error(e);
      return this.sendError(ws, "Something went wrong");
    }
  }

  async onClose(ws: Conn): Promise<void> {
    const id = ws.getPlayerId();
    ws.setPlayerId(null);
    if (id && !this.connectedIds().has(id)) this.people.delete(id);
    if (this.data) await this.afterChange();
  }

  /** Forwards a WebRTC offer/answer/candidate to one person; the server never looks inside. */
  private relayRtc(playerId: string, msg: { to: string; data: unknown }, size: number): void {
    if (size > 20_000) return;
    const from = peerOf(playerId);
    const out = JSON.stringify({ t: "rtc", from, data: msg.data });
    for (const ws of this.host.connections()) {
      const id = ws.getPlayerId();
      if (id && id !== playerId && peerOf(id) === msg.to) {
        try {
          ws.send(out);
        } catch {
          /* closed */
        }
      }
    }
  }

  /** Tells everyone else who is talking right now (push-to-talk). */
  private relayTalk(playerId: string, on: boolean): void {
    const out = JSON.stringify({ t: "talk", peer: peerOf(playerId), on });
    for (const ws of this.host.connections()) {
      const id = ws.getPlayerId();
      if (id && id !== playerId) {
        try {
          ws.send(out);
        } catch {
          /* closed */
        }
      }
    }
  }

  /**
   * Voice audio through the server, for when a direct (WebRTC) link can't be made, e.g. two phones
   * on mobile data with no TURN relay. A chunk is ~85 ms of 16 kHz mu-law audio, base64-encoded;
   * it goes to everyone else who joined voice. The server never looks inside.
   */
  private relayPcm(playerId: string, d: unknown): void {
    if (typeof d !== "string" || d.length > 3500) return;
    const me = this.people.get(playerId);
    if (!me?.voice) return;
    const out = JSON.stringify({ t: "pcm", from: peerOf(playerId), d });
    for (const ws of this.host.connections()) {
      const id = ws.getPlayerId();
      if (id && id !== playerId && this.people.get(id)?.voice) {
        try {
          ws.send(out);
        } catch {
          /* closed */
        }
      }
    }
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
    const prev = this.people.get(playerId);
    this.people.set(playerId, { name: profile.name, avatarUrl: profile.avatarUrl ?? null, voice: prev?.voice ?? false, lastChat: prev?.lastChat ?? 0, profile });
    let seat = this.seatOf(playerId);
    if (seat === null && d.status === "lobby") {
      const free = d.seats.findIndex((s) => s === null);
      if (free >= 0) {
        d.seats[free] = { playerId, ...profile };
        seat = free;
      }
    }
    // Poker is a cash table: newcomers sit down mid-game, at a free seat or in a bot's place.
    if (seat === null && d.status === "playing" && isPokerGame(d.game) && !d.standing?.includes(playerId)) {
      let at = d.seats.findIndex((s) => s === null);
      if (at < 0) at = d.seats.findIndex((s) => s !== null && s.playerId === null);
      if (at >= 0) {
        this.pokerSeat(at, { playerId, ...profile });
        seat = at;
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
    for (const s of d.seats) if (s) s.afkKickAt = null;
    d.ratingChanges = null;
    d.matchId = null;
    // Poker is played for play chips and is never rated.
    if (!this.host.rateGame || !d.game || isPokerGame(d.game)) return;
    const g = d.game;
    // Bura: every player gets their team's match points; the rounds are the summary.
    const result = isBuraGame(g)
      ? { mode: "bura", khishti: 0, pairs: true, totals: [0, 1, 2, 3].map((i) => g.match[i % 2]), pulkas: g.rounds as unknown[] }
      : { mode: d.config.mode, khishti: d.config.khishti, pairs: Boolean(d.config.pairs), totals: g.totals.slice(), pulkas: g.pulkas as unknown[] };
    try {
      const changes = await this.host.rateGame({
        code: d.code,
        ...result,
        userIds: d.seats.map((s) => s?.userId ?? null),
        names: d.seats.map((s, i) => s?.name ?? `#${i + 1}`),
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

  /** Poker: put someone in a seat during the game (a bot sitting there stands up first). */
  private pokerSeat(at: number, who: Seat): void {
    const d = this.data!;
    let g = d.game as PokerState;
    if (d.seats[at]) {
      this.recordLeave(at);
      g = pokerLeave(g, at);
    }
    d.seats[at] = who;
    d.game = pokerSit(g, at);
    if (who.playerId) d.standing = (d.standing ?? []).filter((x) => x !== who.playerId);
  }

  /** Poker: someone leaves the table during the game. */
  private pokerUnseat(at: number): void {
    const d = this.data!;
    if (!d.seats[at]) return;
    this.recordLeave(at);
    d.game = pokerLeave(d.game as PokerState, at);
    d.seats[at] = null;
  }

  /** Remembers what a leaving player won or lost, for the end-of-session table. */
  private recordLeave(at: number): void {
    const d = this.data!;
    const g = d.game as PokerState;
    const s = d.seats[at];
    if (!s || !g.seated[at]) return;
    const net = g.stacks[at] - g.bought[at];
    const ledger = (d.ledger ??= []);
    const row = ledger.find((r) => r.name === s.name);
    if (row) row.net += net;
    else ledger.push({ name: s.name, net });
    if (ledger.length > 40) ledger.shift();
  }

  /** Changes the number of seats in the lobby, keeping people and dropping bots first. */
  private resizeSeats(n: number): void {
    const d = this.data!;
    if (d.seats.length === n) return;
    const people = d.seats.filter((s) => s?.playerId).length;
    if (people > n) throw new RuleError("Too many players for that table");
    const next: (Seat | null)[] = Array(n).fill(null);
    const overflow: Seat[] = [];
    d.seats.forEach((s, i) => {
      if (!s) return;
      if (i < n) next[i] = s;
      else overflow.push(s);
    });
    // Players who no longer fit move to a free seat, or into a bot's place.
    for (const s of overflow.filter((x) => x.playerId)) {
      let at = next.findIndex((x) => x === null);
      if (at < 0) at = next.findIndex((x) => x !== null && x.playerId === null);
      next[at] = s;
    }
    for (const s of overflow.filter((x) => !x.playerId)) {
      const at = next.findIndex((x) => x === null);
      if (at >= 0) next[at] = s;
    }
    d.seats = next;
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
        const game: GameKind = parseGame(msg.config.game) ?? d.config.game ?? "joker";
        // Bura is always played in pairs; poker never.
        const pairs = game === "bura" ? true : game === "poker" ? false : typeof msg.config.pairs === "boolean" ? msg.config.pairs : d.config.pairs;
        if (game === "poker") {
          const want = Number(msg.config.seats ?? d.config.seats ?? POKER_DEFAULT_SEATS);
          const seats = want >= POKER_MIN_SEATS && want <= POKER_MAX_SEATS ? Math.floor(want) : d.config.seats ?? POKER_DEFAULT_SEATS;
          const bbWant = Number(msg.config.bb);
          const bb = BIG_BLINDS.includes(bbWant) ? bbWant : d.config.bb ?? DEFAULT_BIG_BLIND;
          this.resizeSeats(seats);
          d.config = { mode, khishti, pairs, game, seats, bb };
        } else {
          this.resizeSeats(SEATS);
          d.config = { mode, khishti, pairs, game };
        }
        break;
      }
      case "seat": {
        const to = Number(msg.seat);
        if (d.status === "playing" && isPokerGame(d.game)) {
          if (seat !== null) throw new RuleError("You already have a seat");
          if (!(to >= 0 && to < d.seats.length) || d.seats[to]?.playerId) throw new RuleError("That seat is taken");
          const person = this.people.get(playerId);
          this.pokerSeat(to, { playerId, ...(person?.profile ?? { name: person?.name ?? "Player" }) });
          break;
        }
        if (d.status !== "lobby") throw new RuleError("The game has started");
        if (!(to >= 0 && to < d.seats.length) || d.seats[to]) throw new RuleError("That seat is taken");
        if (seat === null) throw new RuleError("You are watching this game");
        d.seats[to] = d.seats[seat];
        d.seats[seat] = null;
        break;
      }
      case "addBot": {
        this.requireHost(playerId);
        const pokerLive = d.status === "playing" && isPokerGame(d.game);
        if (d.status !== "lobby" && !pokerLive) throw new RuleError("The game has started");
        const at = Number(msg.seat);
        if (!(at >= 0 && at < d.seats.length) || d.seats[at]) throw new RuleError("That seat is taken");
        const bot = { playerId: null, name: BOT_NAMES[at] ?? `Bot ${at + 1}` };
        if (pokerLive) this.pokerSeat(at, bot);
        else d.seats[at] = bot;
        break;
      }
      case "removeSeat": {
        this.requireHost(playerId);
        const pokerLive = d.status === "playing" && isPokerGame(d.game);
        if (d.status !== "lobby" && !pokerLive) throw new RuleError("The game has started");
        const at = Number(msg.seat);
        if (!(at >= 0 && at < d.seats.length) || !d.seats[at]) throw new RuleError("Seat is empty");
        if (d.seats[at]!.playerId === playerId) throw new RuleError("You can't remove yourself");
        const gone = d.seats[at]!.playerId;
        if (pokerLive) {
          this.pokerUnseat(at);
          if (gone) d.standing = [...(d.standing ?? []).filter((x) => x !== gone), gone];
        } else d.seats[at] = null;
        break;
      }
      case "stand": {
        if (d.status !== "playing" || !isPokerGame(d.game)) throw new RuleError("You can only stand up at a poker table");
        if (seat === null) throw new RuleError("You are watching this game");
        this.pokerUnseat(seat);
        d.standing = [...(d.standing ?? []).filter((x) => x !== playerId), playerId];
        break;
      }
      case "end": {
        this.requireHost(playerId);
        if (d.status !== "playing" || !isPokerGame(d.game)) throw new RuleError("No poker game in progress");
        await this.finishGame();
        break;
      }
      case "start": {
        this.requireHost(playerId);
        if (d.status !== "lobby") throw new RuleError("The game has started");
        for (let i = 0; i < d.seats.length; i++) if (!d.seats[i]) d.seats[i] = { playerId: null, name: BOT_NAMES[i] ?? `Bot ${i + 1}` };
        const seed = Math.floor(Math.random() * 2 ** 31);
        d.game =
          d.config.game === "bura"
            ? newBura(seed)
            : d.config.game === "poker"
              ? newPoker({ seats: d.seats.length, bb: d.config.bb, seated: d.seats.map(Boolean) }, seed)
              : newGame(d.config, seed);
        d.status = "playing";
        d.standing = [];
        d.ledger = [];
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
        d.seats = d.seats.map((s) => (s && !s.left && (s.playerId === null || online.has(s.playerId)) ? s : null));
        break;
      }
      case "chat": {
        const text = String(msg.text ?? "").replace(/[\u0000-\u001f]/g, " ").trim().slice(0, 200);
        if (!text) return;
        const person = this.people.get(playerId);
        if (person) {
          if (Date.now() - person.lastChat < 400) throw new RuleError("Slow down");
          person.lastChat = Date.now();
        }
        const name = seat !== null ? d.seats[seat]!.name : person?.name ?? "Guest";
        d.chat.push({ seat, name, text, at: Date.now() });
        if (d.chat.length > 40) d.chat = d.chat.slice(-40);
        break;
      }
      case "back": {
        if (seat !== null) this.comeBack(seat);
        break;
      }
      case "trump":
      case "bid":
      case "play": {
        if (d.status !== "playing" || !d.game) throw new RuleError("No game in progress");
        if (seat === null) throw new RuleError("You are watching this game");
        if (isBuraGame(d.game) || isPokerGame(d.game)) throw new RuleError("Unknown message");
        this.comeBack(seat);
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
      case "bura": {
        if (d.status !== "playing" || !isBuraGame(d.game)) throw new RuleError("No game in progress");
        if (seat === null) throw new RuleError("You are watching this game");
        if (!msg.action || typeof msg.action !== "object" || msg.action.t === "next") throw new RuleError("Bad move");
        this.comeBack(seat);
        d.game = applyBura(d.game, seat, msg.action);
        if (d.game.phase === "gameOver") await this.finishGame();
        break;
      }
      case "poker": {
        if (d.status !== "playing" || !isPokerGame(d.game)) throw new RuleError("No game in progress");
        if (seat === null) throw new RuleError("You are watching this game");
        if (!msg.action || typeof msg.action !== "object" || msg.action.t === "next") throw new RuleError("Bad move");
        this.comeBack(seat);
        d.game = applyPoker(d.game, seat, msg.action);
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
    if (isPokerGame(g)) {
      const auto = pokerAutoDelay(g);
      if (auto !== null) return Date.now() + (envDelay >= 0 ? envDelay : auto);
      if (g.phase !== "betting" || g.turn < 0) return null;
      const ps = d.seats[g.turn];
      if (!ps || ps.playerId === null || ps.afkKickAt) return Date.now() + BOT_DELAY;
      return Date.now() + TURN_MS;
    }
    if (isBuraGame(g)) {
      const auto = buraAutoDelay(g);
      if (auto !== null) return Date.now() + (envDelay >= 0 ? envDelay : auto);
    }
    const seat = d.seats[g.turn];
    const justFinishedTrick = isBuraGame(g)
      ? g.trick === null && g.lastTrick !== null && !g.raise
      : g.phase === "playing" && g.trick.length === 0 && g.lastTrick !== null;
    const extra = justFinishedTrick ? AFTER_TRICK_DELAY : 0;
    if (!seat || seat.playerId === null || seat.afkKickAt) return Date.now() + BOT_DELAY + extra;
    return Date.now() + TURN_MS + extra;
  }

  private notice(n: SysNotice): void {
    const d = this.data!;
    d.chat.push({ seat: null, name: "", text: "", at: Date.now(), sys: n });
    if (d.chat.length > 40) d.chat = d.chat.slice(-40);
  }

  private async penalize(seat: Seat, amount: number, reason: string): Promise<number | undefined> {
    if (!seat.userId || !this.host.penalize) return undefined;
    try {
      const r = await this.host.penalize(seat.userId, amount, reason, this.data!.code);
      if (r !== null) seat.rating = r;
      return amount;
    } catch (e) {
      console.error("penalty failed", e);
      return undefined;
    }
  }

  /** The player did not play in time, even after the warning: a bot plays for them from now on. */
  private async goAfk(i: number): Promise<void> {
    const seat = this.data!.seats[i]!;
    seat.afkKickAt = Date.now() + KICK_MS;
    const penalty = isPokerGame(this.data!.game) ? undefined : await this.penalize(seat, AFK_PENALTY, "afk");
    this.notice({ kind: "afk", name: seat.name, penalty });
  }

  /** The player is back (pressed "I'm back" or made a move): stop the bot and the kick clock. */
  private comeBack(i: number): void {
    const seat = this.data!.seats[i];
    if (!seat?.afkKickAt) return;
    seat.afkKickAt = null;
    this.notice({ kind: "back", name: seat.name });
  }

  /** Did not come back in time: removed from the match, a bot keeps the seat. */
  private async kick(i: number): Promise<void> {
    const d = this.data!;
    const seat = d.seats[i]!;
    if (isPokerGame(d.game)) {
      // Cash table: they just lose their seat (no rating at stake).
      const gone = seat.playerId;
      this.pokerUnseat(i);
      if (gone) d.standing = [...(d.standing ?? []).filter((x) => x !== gone), gone];
      this.notice({ kind: "kicked", name: seat.name });
      return;
    }
    const penalty = await this.penalize(seat, KICK_PENALTY, "abandoned");
    const gone = seat.playerId;
    d.seats[i] = { playerId: null, name: seat.name, avatarUrl: seat.avatarUrl, userId: null, rating: null, left: true };
    this.notice({ kind: "kicked", name: seat.name, penalty });
    if (gone && d.hostId === gone) {
      const online = this.connectedIds();
      d.hostId = d.seats.find((s) => s?.playerId && online.has(s.playerId))?.playerId ?? null;
    }
  }

  private nextKick(): number | null {
    const times = this.data!.seats.map((s) => s?.afkKickAt ?? null).filter((t): t is number => t !== null);
    return times.length ? Math.min(...times) : null;
  }

  private nextWake(): number {
    const d = this.data!;
    const t = [d.deadline, d.status === "playing" ? this.nextKick() : null].filter((x): x is number => x !== null);
    return t.length ? Math.min(...t) : Date.now() + IDLE_CLEANUP_MS;
  }

  private turnKey(): string {
    const g = this.data!.game;
    if (!g) return "";
    if (isBuraGame(g)) return `${g.round}:${g.step}:${g.phase}:${g.turn}`;
    if (isPokerGame(g)) return `${g.handNo}:${g.phase}:${g.street}:${g.turn}:${g.step}`;
    return `${g.dealsPlayed}:${g.phase}:${g.turn}:${g.trick.length}:${g.hands[g.turn]?.length ?? 0}`;
  }

  private async afterChange(): Promise<void> {
    const d = this.data!;
    // Reset the turn clock when the actor changes or a player goes on or off autopilot.
    const g = d.game;
    const afkKey = d.seats.map((s) => (s?.afkKickAt ? 1 : 0)).join("");
    const fullKey = `${this.turnKey()}|${afkKey}`;
    if (fullKey !== d.turnKey || d.deadline === null) {
      d.deadline = this.computeDeadline();
      d.turnKey = fullKey;
      d.turnStage = "turn";
    }
    if (d.status !== "playing" || !g) {
      d.deadline = null;
      d.turnStage = "turn";
    }
    await this.save();
    await this.host.setTimer(this.nextWake());
    this.broadcast();
  }

  async onTimer(): Promise<void> {
    const d = this.data;
    if (!d) return;
    if (d.status === "playing" && d.game) {
      const now = Date.now();
      let kicked = false;
      for (let i = 0; i < d.seats.length; i++) {
        const at = d.seats[i]?.afkKickAt;
        if (at && now >= at - 50) {
          await this.kick(i);
          kicked = true;
        }
      }
      if (d.deadline === null || now < d.deadline - 50) {
        if (kicked) await this.afterChange();
        else await this.host.setTimer(this.nextWake());
        return;
      }
      const g = d.game;
      if (isPokerGame(g)) {
        await this.pokerTimer(g, now);
        return;
      }
      if (isBuraGame(g) && buraActor(g) === null) {
        // Round result shown long enough: deal the next round.
        try {
          d.game = applyBura(g, -1, { t: "next" });
        } catch (e) {
          console.error("next round failed", e);
        }
        d.deadline = null;
        await this.afterChange();
        return;
      }
      const seat = g.turn;
      const s = d.seats[seat];
      if (s && s.playerId !== null && !s.afkKickAt) {
        if (d.turnStage !== "warn") {
          // First miss: one more minute, and everyone is told.
          d.turnStage = "warn";
          d.deadline = now + WARN_MS;
          this.notice({ kind: "warn", name: s.name });
          await this.save();
          await this.host.setTimer(this.nextWake());
          this.broadcast();
          return;
        }
        await this.goAfk(seat);
      }
      try {
        d.game = isBuraGame(g) ? applyBura(g, seat, buraBot(g, seat)) : applyAction(g, seat, botAction(g, seat));
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

  private async pokerTimer(g: PokerState, now: number): Promise<void> {
    const d = this.data!;
    if (g.phase === "handOver") {
      // Bots buy back in by themselves; then the next hand is dealt.
      let s = g;
      d.seats.forEach((seat, i) => {
        if (seat && seat.playerId === null && s.seated[i] && s.stacks[i] === 0) s = applyPoker(s, i, { t: "rebuy" });
      });
      d.game = applyPoker(s, -1, { t: "next" });
      d.deadline = null;
      await this.afterChange();
      return;
    }
    if (g.phase !== "betting" || g.turn < 0) {
      d.deadline = null;
      await this.afterChange();
      return;
    }
    const seat = g.turn;
    const s = d.seats[seat];
    if (s && s.playerId !== null && !s.afkKickAt) {
      if (d.turnStage !== "warn") {
        d.turnStage = "warn";
        d.deadline = now + WARN_MS;
        this.notice({ kind: "warn", name: s.name });
        await this.save();
        await this.host.setTimer(this.nextWake());
        this.broadcast();
        return;
      }
      await this.goAfk(seat);
    }
    try {
      // A player who stopped responding only checks or folds; bots really play.
      const passive = !!s && s.playerId !== null;
      d.game = applyPoker(g, seat, pokerBot(g, seat, passive));
    } catch (e) {
      console.error("auto play failed", e);
    }
    d.deadline = null;
    await this.afterChange();
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
      /** "warn" while the player to act is in their extra minute. */
      turnStage: d.turnStage ?? "turn",
      serverTime: Date.now(),
      chat: d.chat.slice(-30),
      /** Your public id (matches `people[].peer`). */
      peer: peerOf(playerId),
      /** Everyone connected right now, players and spectators. */
      people: [...online].map((id) => {
        const p = this.people.get(id);
        return { peer: peerOf(id), name: p?.name ?? "Guest", avatarUrl: p?.avatarUrl ?? null, seat: this.seatOf(id), voice: p?.voice ?? false };
      }),
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
              /** A bot is playing for them; they are removed at this time unless they come back. */
              afkKickAt: s.afkKickAt ?? null,
              left: Boolean(s.left),
            }
          : null,
      ),
      ledger: d.ledger ?? [],
      game: isBuraGame(g)
        ? buraView(g, you)
        : isPokerGame(g)
        ? pokerView(g, you)
        : g
        ? {
            kind: "joker" as const,
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
