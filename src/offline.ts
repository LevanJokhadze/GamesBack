// Score sheets for Joker played in person, with real cards.
//
// One player (the scorekeeper) creates the sheet with four registered accounts, writes each
// deal's bids and tricks, and the server scores it with the online rules. When the game is
// over the scorekeeper sends it for confirmation: it changes ratings only once all four players
// have confirmed it from their own accounts. Anyone at the table can open the sheet live, and
// any of them can send it back for a correction instead of confirming.

import { randomUUID } from "node:crypto";
import { DEFAULT_CONFIG, GameConfig, RuleError, SEATS } from "./engine/game";
import { SheetDeal, SheetScore, scoreSheet, sheetDealer, sheetSizes, validateSheet } from "./engine/sheet";
import type { RatingChange } from "./room-core";
import { InputError, Store, User } from "./store";

export type SheetStatus = "recording" | "pending" | "rated" | "cancelled";

export interface OfflineSheet {
  id: string;
  createdBy: string;
  createdAt: string;
  updatedAt: string;
  /** Bumped on every write; a write must name the version it read. */
  version: number;
  status: SheetStatus;
  config: GameConfig;
  /** userId per seat, in seating order (clockwise). */
  players: string[];
  /** Seat that deals the first deal. */
  firstDealer: number;
  deals: SheetDeal[];
  /** Players who confirmed the finished sheet. */
  confirmed: string[];
  /** Last player who sent it back for a correction. */
  rejected: { userId: string; at: string } | null;
  matchId: string | null;
  ratingChanges: (RatingChange | null)[] | null;
}

/** Most sheets one person may keep open (being written or waiting for confirmation). */
export const MAX_OPEN_SHEETS = 3;

export interface SheetPlayer {
  id: string;
  nickname: string;
  firstName: string;
  lastName: string;
  rating: number;
  avatarUrl: string;
}

export interface SheetView extends Omit<OfflineSheet, "players"> {
  players: SheetPlayer[];
  score: SheetScore;
  /** Dealer of the next deal to write, or -1 when the game is complete. */
  dealer: number;
  /** Your seat, or null if you are not at this table. */
  you: number | null;
  /** You may edit the sheet (the scorekeeper, while it is being written or waits for confirmation). */
  canEdit: boolean;
  /** You still have to confirm it. */
  mustConfirm: boolean;
}

function cleanConfig(c: unknown): GameConfig {
  const x = (c ?? {}) as Partial<GameConfig>;
  const mode = x.mode === "nines" ? "nines" : "standard";
  const khishti = [-200, -500, -900].includes(Number(x.khishti)) ? Number(x.khishti) : DEFAULT_CONFIG.khishti;
  return { mode, khishti, pairs: Boolean(x.pairs) };
}

/** Serialises work on one sheet inside this process (the server runs as one replica). */
const locks = new Map<string, Promise<unknown>>();
function locked<T>(id: string, fn: () => Promise<T>): Promise<T> {
  const prev = locks.get(id) ?? Promise.resolve();
  const run = prev.then(fn, fn);
  const tail = run.catch(() => undefined);
  locks.set(id, tail);
  tail.then(() => {
    if (locks.get(id) === tail) locks.delete(id);
  });
  return run;
}

export class Sheets {
  constructor(private store: Store, private avatarUrl: (u: User) => string) {}

  private async view(s: OfflineSheet, me: User): Promise<SheetView> {
    const users = await Promise.all(s.players.map((id) => this.store.userById(id)));
    const score = scoreSheet(s.config, s.deals);
    const seat = s.players.indexOf(me.id);
    const next = s.deals.length && !s.deals[s.deals.length - 1].took ? s.deals.length - 1 : s.deals.length;
    return {
      ...s,
      players: users.map((u, i) =>
        u
          ? { id: u.id, nickname: u.nickname, firstName: u.firstName, lastName: u.lastName, rating: u.rating, avatarUrl: this.avatarUrl(u) }
          : { id: s.players[i], nickname: "?", firstName: "", lastName: "", rating: 0, avatarUrl: "" },
      ),
      score,
      dealer: score.complete ? -1 : sheetDealer(s.firstDealer, next),
      you: seat >= 0 ? seat : null,
      canEdit: s.createdBy === me.id && (s.status === "recording" || s.status === "pending"),
      mustConfirm: s.status === "pending" && seat >= 0 && !s.confirmed.includes(me.id),
    };
  }

  private async load(id: string): Promise<OfflineSheet> {
    const s = await this.store.sheetGet(id);
    if (!s) throw new NotFound();
    return s;
  }

  private async save(s: OfflineSheet, readVersion: number): Promise<OfflineSheet> {
    const next = { ...s, version: readVersion + 1, updatedAt: new Date().toISOString() };
    if (!(await this.store.sheetPut(next, readVersion))) throw new Conflict();
    return next;
  }

  async create(me: User, body: Record<string, unknown>): Promise<SheetView> {
    const players = Array.isArray(body.players) ? body.players.map(String) : [];
    if (players.length !== SEATS) throw new InputError("Choose four players");
    if (new Set(players).size !== SEATS) throw new InputError("Each player can sit only once");
    if (!players.includes(me.id)) throw new InputError("You must be one of the players");
    const users = await Promise.all(players.map((id) => this.store.userById(id)));
    if (users.some((u) => !u)) throw new InputError("Unknown player");
    const open = (await this.store.sheetList(me.id, 50)).filter((s) => s.createdBy === me.id && (s.status === "recording" || s.status === "pending"));
    if (open.length >= MAX_OPEN_SHEETS) throw new InputError(`You already have ${MAX_OPEN_SHEETS} unfinished sheets. Finish or delete one first.`);
    const firstDealer = Number(body.firstDealer ?? 0);
    if (!Number.isInteger(firstDealer) || firstDealer < 0 || firstDealer >= SEATS) throw new InputError("Bad first dealer");
    const now = new Date().toISOString();
    const s: OfflineSheet = {
      id: randomUUID(),
      createdBy: me.id,
      createdAt: now,
      updatedAt: now,
      version: 1,
      status: "recording",
      config: cleanConfig(body.config),
      players: users.map((u) => u!.id),
      firstDealer,
      deals: [],
      confirmed: [],
      rejected: null,
      matchId: null,
      ratingChanges: null,
    };
    if (!(await this.store.sheetPut(s, null))) throw new Conflict();
    return this.view(s, me);
  }

  async get(me: User, id: string): Promise<SheetView> {
    const s = await this.load(id);
    if (!s.players.includes(me.id)) throw new NotFound();
    return this.view(s, me);
  }

  /** Your sheets that are open (being written or waiting for confirmation), newest first. */
  async list(me: User): Promise<SheetView[]> {
    const rows = await this.store.sheetList(me.id, 20);
    return Promise.all(rows.filter((s) => s.status === "recording" || s.status === "pending").map((s) => this.view(s, me)));
  }

  /**
   * The scorekeeper writes the deals (the whole list, so an earlier deal can be corrected).
   * Editing a sheet that waits for confirmation sends it back to "recording" and clears every
   * confirmation, so nobody confirms numbers they have not seen.
   */
  async setDeals(me: User, id: string, version: number, deals: unknown): Promise<SheetView> {
    return locked(id, async () => {
      const s = await this.load(id);
      if (s.createdBy !== me.id) throw new Forbidden("Only the scorekeeper can write the sheet");
      if (s.status !== "recording" && s.status !== "pending") throw new InputError("This sheet is closed");
      if (version !== s.version) throw new Conflict();
      const clean = validateSheet(s.config, deals);
      const changed = JSON.stringify(clean) !== JSON.stringify(s.deals);
      if (!changed) return this.view(s, me);
      const next: OfflineSheet = { ...s, deals: clean, status: "recording", confirmed: [] };
      return this.view(await this.save(next, s.version), me);
    });
  }

  /** The scorekeeper finishes the game and asks the other three to confirm it. */
  async submit(me: User, id: string, version: number): Promise<SheetView> {
    return locked(id, async () => {
      const s = await this.load(id);
      if (s.createdBy !== me.id) throw new Forbidden("Only the scorekeeper can do that");
      if (s.status !== "recording") throw new InputError("This sheet is not being written");
      if (version !== s.version) throw new Conflict();
      if (!scoreSheet(s.config, s.deals).complete) throw new InputError(`Write all ${sheetSizes(s.config.mode).length} deals first`);
      const next: OfflineSheet = { ...s, status: "pending", confirmed: [me.id], rejected: null };
      return this.view(await this.save(next, s.version), me);
    });
  }

  /** A player confirms the finished sheet. The last confirmation rates the game. */
  async confirm(me: User, id: string, version: number): Promise<SheetView> {
    return locked(id, async () => {
      const s = await this.load(id);
      if (!s.players.includes(me.id)) throw new NotFound();
      if (s.status !== "pending") throw new InputError("This sheet is not waiting for confirmation");
      if (version !== s.version) throw new Conflict();
      const confirmed = s.confirmed.includes(me.id) ? s.confirmed : [...s.confirmed, me.id];
      let next: OfflineSheet = { ...s, confirmed };
      if (s.players.every((p) => confirmed.includes(p))) {
        const users = await Promise.all(s.players.map((p) => this.store.userById(p)));
        const score = scoreSheet(s.config, s.deals);
        const rated = await this.store.rateGame({
          code: "OFFLINE",
          source: "offline",
          mode: s.config.mode,
          khishti: s.config.khishti,
          pairs: s.config.pairs,
          userIds: s.players,
          names: users.map((u, i) => u?.nickname ?? `#${i + 1}`),
          totals: score.totals,
          pulkas: score.pulkas,
        });
        next = { ...next, status: "rated", matchId: rated.matchId, ratingChanges: rated.changes };
      }
      return this.view(await this.save(next, s.version), me);
    });
  }

  /** A player sends the finished sheet back to the scorekeeper for a correction. */
  async reject(me: User, id: string, version: number): Promise<SheetView> {
    return locked(id, async () => {
      const s = await this.load(id);
      if (!s.players.includes(me.id)) throw new NotFound();
      if (s.status !== "pending") throw new InputError("This sheet is not waiting for confirmation");
      if (version !== s.version) throw new Conflict();
      const next: OfflineSheet = { ...s, status: "recording", confirmed: [], rejected: { userId: me.id, at: new Date().toISOString() } };
      return this.view(await this.save(next, s.version), me);
    });
  }

  /** The scorekeeper deletes a sheet that was never rated. */
  async cancel(me: User, id: string): Promise<void> {
    return locked(id, async () => {
      const s = await this.load(id);
      if (s.createdBy !== me.id) throw new Forbidden("Only the scorekeeper can delete the sheet");
      if (s.status === "rated") throw new InputError("A rated game can't be deleted");
      if (s.status === "cancelled") return;
      await this.save({ ...s, status: "cancelled" }, s.version);
    });
  }
}

export class NotFound extends Error {}
export class Forbidden extends Error {}
/** Someone else changed the sheet since you loaded it. */
export class Conflict extends Error {}

export { RuleError };
