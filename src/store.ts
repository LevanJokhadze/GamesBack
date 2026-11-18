// Player accounts, ratings and game history for the Node server.
// PgStore is used when DATABASE_URL is set (Railway Postgres); MemoryStore otherwise (local dev, tests).

import { createHash, randomBytes, randomUUID } from "node:crypto";
import pg from "pg";
import { BOT_RATING, START_RATING, eloDeltas } from "./engine/elo";
import type { FinishedGame, RatingChange } from "./room-core";

export interface User {
  id: string;
  firstName: string;
  lastName: string;
  rating: number;
  games: number;
  wins: number;
  peak: number;
  avatarVersion: number;
  createdAt: string;
}

export interface Avatar {
  type: string;
  data: Buffer;
}

export interface GameRecord {
  id: string;
  finishedAt: string;
  mode: string;
  score: number;
  place: number;
  ratingBefore: number;
  ratingAfter: number;
}

export class InputError extends Error {}

export interface Store {
  init(): Promise<void>;
  register(firstName: string, lastName: string, avatar: Avatar): Promise<{ token: string; user: User }>;
  userByToken(token: string): Promise<User | null>;
  userById(id: string): Promise<User | null>;
  updateProfile(id: string, p: { firstName?: string; lastName?: string; avatar?: Avatar }): Promise<User>;
  avatar(id: string): Promise<Avatar | null>;
  leaderboard(limit: number): Promise<(User & { rank: number })[]>;
  recentGames(id: string, limit: number): Promise<GameRecord[]>;
  rateGame(game: FinishedGame): Promise<(RatingChange | null)[]>;
}

// ---------- validation helpers ----------

export function cleanPersonName(v: unknown): string {
  const s = String(v ?? "")
    .replace(/[\u0000-\u001f\u007f<>]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 30);
  if (!s) throw new InputError("Name is required");
  return s;
}

const MAX_AVATAR = 300 * 1024;

/** Accepts a data: URL with a JPEG, PNG or WebP image. */
export function parseAvatar(v: unknown): Avatar {
  const m = String(v ?? "").match(/^data:(image\/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)$/);
  if (!m) throw new InputError("A photo is required");
  const data = Buffer.from(m[2], "base64");
  if (data.length < 100 || data.length > MAX_AVATAR) throw new InputError("Photo is too large");
  const jpeg = data[0] === 0xff && data[1] === 0xd8;
  const png = data.subarray(0, 4).toString("hex") === "89504e47";
  const webp = data.subarray(0, 4).toString() === "RIFF" && data.subarray(8, 12).toString() === "WEBP";
  if (!(jpeg || png || webp)) throw new InputError("That file is not an image");
  return { type: jpeg ? "image/jpeg" : png ? "image/png" : "image/webp", data };
}

function newToken(): string {
  return randomBytes(32).toString("base64url");
}

function hashToken(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

/** Places (1 = best) from totals; equal totals share a place. */
function places(totals: number[]): number[] {
  return totals.map((t) => 1 + totals.filter((o) => o > t).length);
}

/** Pure rating step shared by both stores. */
function computeChanges(game: FinishedGame, users: Map<string, User>): (RatingChange | null)[] {
  const players = game.userIds.map((id, i) => {
    const u = id ? users.get(id) : undefined;
    return u
      ? { rating: u.rating, games: u.games, peak: u.peak, score: game.totals[i] }
      : { rating: BOT_RATING, games: 1000, peak: BOT_RATING, score: game.totals[i] };
  });
  const deltas = eloDeltas(players);
  return game.userIds.map((id, i) => {
    const u = id ? users.get(id) : undefined;
    if (!u) return null;
    return { before: u.rating, after: u.rating + deltas[i], delta: deltas[i] };
  });
}

// ---------- in-memory store ----------

export class MemoryStore implements Store {
  private users = new Map<string, User & { tokenHash: string; avatar: Avatar }>();
  private games: { id: string; finishedAt: string; mode: string; userIds: (string | null)[]; totals: number[]; changes: (RatingChange | null)[] }[] = [];

  async init() {}

  async register(firstName: string, lastName: string, avatar: Avatar) {
    const token = newToken();
    const user: User = {
      id: randomUUID(),
      firstName: cleanPersonName(firstName),
      lastName: cleanPersonName(lastName),
      rating: START_RATING,
      games: 0,
      wins: 0,
      peak: START_RATING,
      avatarVersion: 1,
      createdAt: new Date().toISOString(),
    };
    this.users.set(user.id, { ...user, tokenHash: hashToken(token), avatar });
    return { token, user };
  }

  private pub(u: User & { tokenHash: string; avatar: Avatar }): User {
    const { tokenHash: _t, avatar: _a, ...rest } = u;
    return { ...rest };
  }

  async userByToken(token: string) {
    const h = hashToken(token);
    for (const u of this.users.values()) if (u.tokenHash === h) return this.pub(u);
    return null;
  }

  async userById(id: string) {
    const u = this.users.get(id);
    return u ? this.pub(u) : null;
  }

  async updateProfile(id: string, p: { firstName?: string; lastName?: string; avatar?: Avatar }) {
    const u = this.users.get(id);
    if (!u) throw new InputError("Unknown user");
    if (p.firstName !== undefined) u.firstName = cleanPersonName(p.firstName);
    if (p.lastName !== undefined) u.lastName = cleanPersonName(p.lastName);
    if (p.avatar) {
      u.avatar = p.avatar;
      u.avatarVersion += 1;
    }
    return this.pub(u);
  }

  async avatar(id: string) {
    return this.users.get(id)?.avatar ?? null;
  }

  async leaderboard(limit: number) {
    return [...this.users.values()]
      .filter((u) => u.games > 0)
      .sort((a, b) => b.rating - a.rating || a.createdAt.localeCompare(b.createdAt))
      .slice(0, limit)
      .map((u, i) => ({ ...this.pub(u), rank: i + 1 }));
  }

  async recentGames(id: string, limit: number) {
    return this.games
      .filter((g) => g.userIds.includes(id))
      .slice(-limit)
      .reverse()
      .map((g) => {
        const i = g.userIds.indexOf(id);
        return {
          id: g.id,
          finishedAt: g.finishedAt,
          mode: g.mode,
          score: g.totals[i],
          place: places(g.totals)[i],
          ratingBefore: g.changes[i]!.before,
          ratingAfter: g.changes[i]!.after,
        };
      });
  }

  async rateGame(game: FinishedGame) {
    const users = new Map<string, User>();
    for (const id of game.userIds) if (id && this.users.has(id)) users.set(id, this.users.get(id)!);
    const changes = computeChanges(game, users);
    const pl = places(game.totals);
    game.userIds.forEach((id, i) => {
      const c = changes[i];
      const u = id ? this.users.get(id) : undefined;
      if (!u || !c) return;
      u.rating = c.after;
      u.games += 1;
      if (pl[i] === 1) u.wins += 1;
      u.peak = Math.max(u.peak, c.after);
    });
    this.games.push({ id: randomUUID(), finishedAt: new Date().toISOString(), mode: game.mode, userIds: game.userIds, totals: game.totals, changes });
    return changes;
  }
}

// ---------- Postgres store ----------

const SCHEMA = `
CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash text NOT NULL UNIQUE,
  first_name text NOT NULL,
  last_name text NOT NULL,
  avatar bytea NOT NULL,
  avatar_type text NOT NULL,
  avatar_version int NOT NULL DEFAULT 1,
  rating int NOT NULL DEFAULT ${START_RATING},
  games int NOT NULL DEFAULT 0,
  wins int NOT NULL DEFAULT 0,
  peak int NOT NULL DEFAULT ${START_RATING},
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_rating_idx ON users (rating DESC) WHERE games > 0;
CREATE TABLE IF NOT EXISTS games (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  room_code text NOT NULL,
  mode text NOT NULL,
  finished_at timestamptz NOT NULL DEFAULT now(),
  totals int[] NOT NULL
);
CREATE TABLE IF NOT EXISTS game_players (
  game_id uuid NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  seat int NOT NULL,
  user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  score int NOT NULL,
  place int NOT NULL,
  rating_before int,
  rating_after int,
  PRIMARY KEY (game_id, seat)
);
CREATE INDEX IF NOT EXISTS game_players_user_idx ON game_players (user_id, game_id);
`;

const USER_COLS = `id, first_name, last_name, rating, games, wins, peak, avatar_version, created_at`;

function rowToUser(r: Record<string, unknown>): User {
  return {
    id: String(r.id),
    firstName: String(r.first_name),
    lastName: String(r.last_name),
    rating: Number(r.rating),
    games: Number(r.games),
    wins: Number(r.wins),
    peak: Number(r.peak),
    avatarVersion: Number(r.avatar_version),
    createdAt: new Date(r.created_at as string).toISOString(),
  };
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export class PgStore implements Store {
  private pool: pg.Pool;

  constructor(url: string) {
    this.pool = new pg.Pool({
      connectionString: url,
      max: 5,
      ssl: /sslmode=require/.test(url) ? { rejectUnauthorized: false } : undefined,
    });
  }

  async init() {
    await this.pool.query(SCHEMA);
  }

  async register(firstName: string, lastName: string, avatar: Avatar) {
    const token = newToken();
    const { rows } = await this.pool.query(
      `INSERT INTO users (token_hash, first_name, last_name, avatar, avatar_type)
       VALUES ($1, $2, $3, $4, $5) RETURNING ${USER_COLS}`,
      [hashToken(token), cleanPersonName(firstName), cleanPersonName(lastName), avatar.data, avatar.type],
    );
    return { token, user: rowToUser(rows[0]) };
  }

  async userByToken(token: string) {
    const { rows } = await this.pool.query(
      `UPDATE users SET last_seen = now() WHERE token_hash = $1 RETURNING ${USER_COLS}`,
      [hashToken(token)],
    );
    return rows[0] ? rowToUser(rows[0]) : null;
  }

  async userById(id: string) {
    if (!UUID_RE.test(id)) return null;
    const { rows } = await this.pool.query(`SELECT ${USER_COLS} FROM users WHERE id = $1`, [id]);
    return rows[0] ? rowToUser(rows[0]) : null;
  }

  async updateProfile(id: string, p: { firstName?: string; lastName?: string; avatar?: Avatar }) {
    const { rows } = await this.pool.query(
      `UPDATE users SET
         first_name = COALESCE($2, first_name),
         last_name = COALESCE($3, last_name),
         avatar = COALESCE($4, avatar),
         avatar_type = COALESCE($5, avatar_type),
         avatar_version = avatar_version + CASE WHEN $4::bytea IS NULL THEN 0 ELSE 1 END
       WHERE id = $1 RETURNING ${USER_COLS}`,
      [
        id,
        p.firstName !== undefined ? cleanPersonName(p.firstName) : null,
        p.lastName !== undefined ? cleanPersonName(p.lastName) : null,
        p.avatar?.data ?? null,
        p.avatar?.type ?? null,
      ],
    );
    if (!rows[0]) throw new InputError("Unknown user");
    return rowToUser(rows[0]);
  }

  async avatar(id: string) {
    if (!UUID_RE.test(id)) return null;
    const { rows } = await this.pool.query(`SELECT avatar, avatar_type FROM users WHERE id = $1`, [id]);
    return rows[0] ? { type: String(rows[0].avatar_type), data: rows[0].avatar as Buffer } : null;
  }

  async leaderboard(limit: number) {
    const { rows } = await this.pool.query(
      `SELECT ${USER_COLS}, rank() OVER (ORDER BY rating DESC) AS rank
       FROM users WHERE games > 0 ORDER BY rating DESC, created_at ASC LIMIT $1`,
      [limit],
    );
    return rows.map((r) => ({ ...rowToUser(r), rank: Number(r.rank) }));
  }

  async recentGames(id: string, limit: number) {
    if (!UUID_RE.test(id)) return [];
    const { rows } = await this.pool.query(
      `SELECT g.id, g.finished_at, g.mode, gp.score, gp.place, gp.rating_before, gp.rating_after
       FROM game_players gp JOIN games g ON g.id = gp.game_id
       WHERE gp.user_id = $1 ORDER BY g.finished_at DESC LIMIT $2`,
      [id, limit],
    );
    return rows.map((r) => ({
      id: String(r.id),
      finishedAt: new Date(r.finished_at).toISOString(),
      mode: String(r.mode),
      score: Number(r.score),
      place: Number(r.place),
      ratingBefore: Number(r.rating_before),
      ratingAfter: Number(r.rating_after),
    }));
  }

  async rateGame(game: FinishedGame) {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      const ids = [...new Set(game.userIds.filter((x): x is string => !!x))];
      const { rows } = await client.query(`SELECT ${USER_COLS} FROM users WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE`, [ids]);
      const users = new Map(rows.map((r) => [String(r.id), rowToUser(r)]));
      const changes = computeChanges(game, users);
      const pl = places(game.totals);
      const g = await client.query(`INSERT INTO games (room_code, mode, totals) VALUES ($1, $2, $3) RETURNING id`, [
        game.code,
        game.mode,
        game.totals,
      ]);
      for (let i = 0; i < game.userIds.length; i++) {
        const c = changes[i];
        const uid = game.userIds[i] && users.has(game.userIds[i]!) ? game.userIds[i] : null;
        await client.query(
          `INSERT INTO game_players (game_id, seat, user_id, score, place, rating_before, rating_after)
           VALUES ($1, $2, $3, $4, $5, $6, $7)`,
          [g.rows[0].id, i, uid, game.totals[i], pl[i], c?.before ?? null, c?.after ?? null],
        );
        if (uid && c) {
          await client.query(
            `UPDATE users SET rating = $2, games = games + 1, wins = wins + $3, peak = GREATEST(peak, $2) WHERE id = $1`,
            [uid, c.after, pl[i] === 1 ? 1 : 0],
          );
        }
      }
      await client.query("COMMIT");
      return changes;
    } catch (e) {
      await client.query("ROLLBACK");
      throw e;
    } finally {
      client.release();
    }
  }
}

export function makeStore(): Store {
  const url = process.env.DATABASE_URL;
  return url ? new PgStore(url) : new MemoryStore();
}
