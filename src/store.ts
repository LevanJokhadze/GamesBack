// Player accounts, ratings and game history for the Node server.
// PgStore is used when DATABASE_URL is set (Railway Postgres); MemoryStore otherwise (local dev, tests).

import { createHash, randomBytes, randomUUID, scrypt as scryptCb, timingSafeEqual } from "node:crypto";
import { promisify } from "node:util";
import pg from "pg";
import { BOT_RATING, START_RATING, eloDeltas } from "./engine/elo";
import { rankingScores } from "./engine/game";
import type { FinishedGame, RatingChange } from "./room-core";

export interface User {
  id: string;
  nickname: string;
  firstName: string;
  lastName: string;
  rating: number;
  games: number;
  wins: number;
  peak: number;
  avatarVersion: number;
  createdAt: string;
  /** Can log in with nickname + password on any device. */
  hasPassword: boolean;
}

export interface Avatar {
  type: string;
  data: Buffer;
}

export interface GameRecord {
  id: string;
  finishedAt: string;
  mode: string;
  pairs: boolean;
  score: number;
  place: number;
  ratingBefore: number;
  ratingAfter: number;
  /** Everyone at the table, by seat. */
  players: { name: string; userId: string | null; score: number; place: number }[];
}

export interface MatchPlayer {
  seat: number;
  name: string;
  userId: string | null;
  bot: boolean;
  avatarUrl: string | null;
  score: number;
  place: number;
  ratingBefore: number | null;
  ratingAfter: number | null;
}

export interface Match {
  id: string;
  finishedAt: string;
  mode: string;
  khishti: number;
  /** Played in pairs: seats 0 & 2 against 1 & 3; `place` is the team's place. */
  pairs: boolean;
  players: MatchPlayer[];
  pulkas: unknown[];
}

export class InputError extends Error {}

export interface ProfilePatch {
  nickname?: string;
  firstName?: string;
  lastName?: string;
  avatar?: Avatar;
  password?: string;
}

/** 3-20 letters (Latin or Georgian), digits, "_" or "."; unique ignoring case. */
export function cleanNickname(v: unknown): string {
  const s = String(v ?? "").trim();
  if (!/^[A-Za-z0-9_.\u10D0-\u10FF]{3,20}$/.test(s)) {
    throw new InputError("Nickname must be 3-20 letters, digits, _ or .");
  }
  return s;
}

export interface Store {
  init(): Promise<void>;
  register(p: { nickname: string; firstName: string; lastName: string; avatar: Avatar; password: string }): Promise<{ token: string; user: User }>;
  /** Nickname (any case) + password -> a new session token for this device. */
  login(nickname: string, password: string): Promise<{ token: string; user: User }>;
  logout(token: string): Promise<void>;
  userByToken(token: string): Promise<User | null>;
  userById(id: string): Promise<User | null>;
  updateProfile(id: string, p: ProfilePatch): Promise<User>;
  avatar(id: string): Promise<Avatar | null>;
  leaderboard(limit: number): Promise<(User & { rank: number })[]>;
  /** Games of a player, newest first; `before` is an ISO time for paging. */
  recentGames(id: string, limit: number, before?: string): Promise<GameRecord[]>;
  match(id: string): Promise<Match | null>;
  rateGame(game: FinishedGame): Promise<{ matchId: string; changes: (RatingChange | null)[] }>;
  /** Takes rating points for leaving or not responding; returns the new rating. */
  penalize(userId: string, amount: number, reason: string, roomCode: string): Promise<number | null>;
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

const scrypt = promisify(scryptCb) as (pw: string, salt: Buffer, len: number) => Promise<Buffer>;

/** 6-100 characters. */
export function cleanPassword(v: unknown): string {
  const s = String(v ?? "");
  if (s.length < 6) throw new InputError("Password must be at least 6 characters");
  if (s.length > 100) throw new InputError("Password is too long");
  return s;
}

export async function hashPassword(pw: string): Promise<string> {
  const salt = randomBytes(16);
  const key = await scrypt(pw, salt, 32);
  return `scrypt$${salt.toString("base64")}$${key.toString("base64")}`;
}

export async function checkPassword(pw: string, stored: string | null | undefined): Promise<boolean> {
  const m = String(stored ?? "").match(/^scrypt\$([^$]+)\$([^$]+)$/);
  if (!m) {
    await scrypt(pw, randomBytes(16), 32); // same time either way
    return false;
  }
  const want = Buffer.from(m[2], "base64");
  const got = await scrypt(pw, Buffer.from(m[1], "base64"), want.length);
  return timingSafeEqual(want, got);
}

const BAD_LOGIN = "Wrong nickname or password";

function newToken(): string {
  return randomBytes(32).toString("base64url");
}

function hashToken(token: string): string {
  return createHash("sha256").update(token).digest("hex");
}

/** Places (1 = best); equal scores share a place. In pairs, partners share their team's place. */
function places(totals: number[], pairs = false): number[] {
  const r = rankingScores(totals, pairs);
  if (pairs) return r.map((t) => (r.some((o) => o > t) ? 2 : 1));
  return r.map((t) => 1 + r.filter((o) => o > t).length);
}

/** Pure rating step shared by both stores. */
function computeChanges(game: FinishedGame, users: Map<string, User>): (RatingChange | null)[] {
  const scores = rankingScores(game.totals, Boolean(game.pairs));
  const players = game.userIds.map((id, i) => {
    const u = id ? users.get(id) : undefined;
    return u
      ? { rating: u.rating, games: u.games, peak: u.peak, score: scores[i] }
      : { rating: BOT_RATING, games: 1000, peak: BOT_RATING, score: scores[i] };
  });
  const deltas = eloDeltas(players, { pairs: Boolean(game.pairs) });
  return game.userIds.map((id, i) => {
    const u = id ? users.get(id) : undefined;
    if (!u) return null;
    return { before: u.rating, after: u.rating + deltas[i], delta: deltas[i] };
  });
}

// ---------- in-memory store ----------

export class MemoryStore implements Store {
  private users = new Map<string, User & { tokenHash: string; avatar: Avatar; passwordHash: string | null }>();
  private sessions = new Map<string, string>();
  private games: { id: string; finishedAt: string; game: FinishedGame; changes: (RatingChange | null)[] }[] = [];

  async init() {}

  private nickTaken(nick: string, exceptId?: string): boolean {
    for (const u of this.users.values()) if (u.id !== exceptId && u.nickname.toLowerCase() === nick.toLowerCase()) return true;
    return false;
  }

  async register({ nickname, firstName, lastName, avatar, password }: { nickname: string; firstName: string; lastName: string; avatar: Avatar; password: string }) {
    const token = newToken();
    const nick = cleanNickname(nickname);
    const passwordHash = await hashPassword(cleanPassword(password));
    if (this.nickTaken(nick)) throw new InputError("That nickname is taken");
    const user: User = {
      id: randomUUID(),
      nickname: nick,
      firstName: cleanPersonName(firstName),
      lastName: cleanPersonName(lastName),
      rating: START_RATING,
      games: 0,
      wins: 0,
      peak: START_RATING,
      avatarVersion: 1,
      createdAt: new Date().toISOString(),
      hasPassword: true,
    };
    this.users.set(user.id, { ...user, tokenHash: hashToken(token), avatar, passwordHash });
    return { token, user };
  }

  private pub(u: User & { tokenHash: string; avatar: Avatar; passwordHash: string | null }): User {
    const { tokenHash: _t, avatar: _a, passwordHash, ...rest } = u;
    return { ...rest, hasPassword: !!passwordHash };
  }

  async login(nickname: string, password: string) {
    const nick = String(nickname ?? "").trim().toLowerCase();
    const u = [...this.users.values()].find((x) => x.nickname.toLowerCase() === nick);
    if (!(await checkPassword(String(password ?? ""), u?.passwordHash)) || !u) throw new InputError(BAD_LOGIN);
    const token = newToken();
    this.sessions.set(hashToken(token), u.id);
    return { token, user: this.pub(u) };
  }

  async logout(token: string) {
    this.sessions.delete(hashToken(token));
  }

  async userByToken(token: string) {
    const h = hashToken(token);
    const sid = this.sessions.get(h);
    if (sid && this.users.has(sid)) return this.pub(this.users.get(sid)!);
    for (const u of this.users.values()) if (u.tokenHash === h) return this.pub(u);
    return null;
  }

  async userById(id: string) {
    const u = this.users.get(id);
    return u ? this.pub(u) : null;
  }

  async updateProfile(id: string, p: ProfilePatch) {
    const u = this.users.get(id);
    if (!u) throw new InputError("Unknown user");
    if (p.nickname !== undefined) {
      const nick = cleanNickname(p.nickname);
      if (this.nickTaken(nick, id)) throw new InputError("That nickname is taken");
      u.nickname = nick;
    }
    if (p.password !== undefined) u.passwordHash = await hashPassword(cleanPassword(p.password));
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

  async recentGames(id: string, limit: number, before?: string) {
    return this.games
      .filter((g) => g.game.userIds.includes(id) && (!before || g.finishedAt < before))
      .reverse()
      .slice(0, limit)
      .map((g) => {
        const i = g.game.userIds.indexOf(id);
        const pl = places(g.game.totals, g.game.pairs);
        return {
          id: g.id,
          finishedAt: g.finishedAt,
          mode: g.game.mode,
          pairs: Boolean(g.game.pairs),
          score: g.game.totals[i],
          place: pl[i],
          ratingBefore: g.changes[i]!.before,
          ratingAfter: g.changes[i]!.after,
          players: g.game.names.map((name, s) => ({ name, userId: g.game.userIds[s], score: g.game.totals[s], place: pl[s] })),
        };
      });
  }

  async match(id: string) {
    const g = this.games.find((x) => x.id === id);
    if (!g) return null;
    const pl = places(g.game.totals, g.game.pairs);
    return {
      id: g.id,
      finishedAt: g.finishedAt,
      mode: g.game.mode,
      khishti: g.game.khishti,
      pairs: Boolean(g.game.pairs),
      pulkas: g.game.pulkas,
      players: g.game.names.map((name, s) => {
        const uid = g.game.userIds[s];
        const u = uid ? this.users.get(uid) : undefined;
        return {
          seat: s,
          name,
          userId: uid,
          bot: !uid,
          avatarUrl: u ? `/api/users/${u.id}/avatar?v=${u.avatarVersion}` : null,
          score: g.game.totals[s],
          place: pl[s],
          ratingBefore: g.changes[s]?.before ?? null,
          ratingAfter: g.changes[s]?.after ?? null,
        };
      }),
    };
  }

  async penalize(userId: string, amount: number) {
    const u = this.users.get(userId);
    if (!u) return null;
    u.rating -= amount;
    return u.rating;
  }

  async rateGame(game: FinishedGame) {
    const users = new Map<string, User>();
    for (const id of game.userIds) if (id && this.users.has(id)) users.set(id, this.users.get(id)!);
    const changes = computeChanges(game, users);
    const pl = places(game.totals, game.pairs);
    game.userIds.forEach((id, i) => {
      const c = changes[i];
      const u = id ? this.users.get(id) : undefined;
      if (!u || !c) return;
      u.rating = c.after;
      u.games += 1;
      if (pl[i] === 1) u.wins += 1;
      u.peak = Math.max(u.peak, c.after);
    });
    const matchId = randomUUID();
    this.games.push({ id: matchId, finishedAt: new Date().toISOString(), game, changes });
    return { matchId, changes };
  }
}

// ---------- Postgres store ----------

const SCHEMA = `
CREATE TABLE IF NOT EXISTS users (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  token_hash text NOT NULL UNIQUE,
  nickname text NOT NULL,
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
CREATE UNIQUE INDEX IF NOT EXISTS users_nickname_idx ON users (lower(nickname));
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash text;
CREATE TABLE IF NOT EXISTS sessions (
  token_hash text PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at timestamptz NOT NULL DEFAULT now(),
  last_seen timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS sessions_user_idx ON sessions (user_id);
CREATE TABLE IF NOT EXISTS rating_penalties (
  id bigserial PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  amount int NOT NULL,
  reason text NOT NULL,
  room_code text NOT NULL,
  at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_rating_idx ON users (rating DESC) WHERE games > 0;
CREATE TABLE IF NOT EXISTS games (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  room_code text NOT NULL,
  mode text NOT NULL,
  finished_at timestamptz NOT NULL DEFAULT now(),
  totals int[] NOT NULL,
  khishti int NOT NULL DEFAULT -200,
  pulkas jsonb NOT NULL DEFAULT '[]'
);
ALTER TABLE games ADD COLUMN IF NOT EXISTS pairs boolean NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS game_players (
  game_id uuid NOT NULL REFERENCES games(id) ON DELETE CASCADE,
  seat int NOT NULL,
  user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  name text NOT NULL DEFAULT '',
  score int NOT NULL,
  place int NOT NULL,
  rating_before int,
  rating_after int,
  PRIMARY KEY (game_id, seat)
);
CREATE INDEX IF NOT EXISTS game_players_user_idx ON game_players (user_id, game_id);
`;

const USER_COLS = `id, nickname, first_name, last_name, rating, games, wins, peak, avatar_version, created_at, (password_hash IS NOT NULL) AS has_password`;

function rowToUser(r: Record<string, unknown>): User {
  return {
    id: String(r.id),
    nickname: String(r.nickname),
    firstName: String(r.first_name),
    lastName: String(r.last_name),
    rating: Number(r.rating),
    games: Number(r.games),
    wins: Number(r.wins),
    peak: Number(r.peak),
    avatarVersion: Number(r.avatar_version),
    createdAt: new Date(r.created_at as string).toISOString(),
    hasPassword: Boolean(r.has_password),
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

  async register({ nickname, firstName, lastName, avatar, password }: { nickname: string; firstName: string; lastName: string; avatar: Avatar; password: string }) {
    const token = newToken();
    const nick = cleanNickname(nickname);
    const first = cleanPersonName(firstName);
    const last = cleanPersonName(lastName);
    const passwordHash = await hashPassword(cleanPassword(password));
    try {
      const { rows } = await this.pool.query(
        `INSERT INTO users (token_hash, nickname, first_name, last_name, avatar, avatar_type, password_hash)
         VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING ${USER_COLS}`,
        [hashToken(token), nick, first, last, avatar.data, avatar.type, passwordHash],
      );
      return { token, user: rowToUser(rows[0]) };
    } catch (e) {
      throw nickConflict(e);
    }
  }

  async login(nickname: string, password: string) {
    const { rows } = await this.pool.query(`SELECT id, password_hash FROM users WHERE lower(nickname) = lower($1)`, [String(nickname ?? "").trim()]);
    const ok = await checkPassword(String(password ?? ""), rows[0]?.password_hash);
    if (!ok || !rows[0]) throw new InputError(BAD_LOGIN);
    const token = newToken();
    await this.pool.query(`INSERT INTO sessions (token_hash, user_id) VALUES ($1, $2)`, [hashToken(token), rows[0].id]);
    const user = await this.userById(String(rows[0].id));
    return { token, user: user! };
  }

  async logout(token: string) {
    await this.pool.query(`DELETE FROM sessions WHERE token_hash = $1`, [hashToken(token)]);
  }

  async userByToken(token: string) {
    const h = hashToken(token);
    const s = await this.pool.query(`UPDATE sessions SET last_seen = now() WHERE token_hash = $1 RETURNING user_id`, [h]);
    if (s.rows[0]) return this.userById(String(s.rows[0].user_id));
    const { rows } = await this.pool.query(`UPDATE users SET last_seen = now() WHERE token_hash = $1 RETURNING ${USER_COLS}`, [h]);
    return rows[0] ? rowToUser(rows[0]) : null;
  }

  async userById(id: string) {
    if (!UUID_RE.test(id)) return null;
    const { rows } = await this.pool.query(`SELECT ${USER_COLS} FROM users WHERE id = $1`, [id]);
    return rows[0] ? rowToUser(rows[0]) : null;
  }

  async updateProfile(id: string, p: ProfilePatch) {
    let rows;
    try {
      ({ rows } = await this.pool.query(
      `UPDATE users SET
         nickname = COALESCE($6, nickname),
         first_name = COALESCE($2, first_name),
         last_name = COALESCE($3, last_name),
         avatar = COALESCE($4, avatar),
         avatar_type = COALESCE($5, avatar_type),
         avatar_version = avatar_version + CASE WHEN $4::bytea IS NULL THEN 0 ELSE 1 END,
         password_hash = COALESCE($7, password_hash)
       WHERE id = $1 RETURNING ${USER_COLS}`,
      [
        id,
        p.firstName !== undefined ? cleanPersonName(p.firstName) : null,
        p.lastName !== undefined ? cleanPersonName(p.lastName) : null,
        p.avatar?.data ?? null,
        p.avatar?.type ?? null,
        p.nickname !== undefined ? cleanNickname(p.nickname) : null,
        p.password !== undefined ? await hashPassword(cleanPassword(p.password)) : null,
      ],
      ));
    } catch (e) {
      throw nickConflict(e);
    }
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

  async recentGames(id: string, limit: number, before?: string) {
    if (!UUID_RE.test(id)) return [];
    const { rows } = await this.pool.query(
      `SELECT g.id, g.finished_at, g.mode, g.pairs, gp.score, gp.place, gp.rating_before, gp.rating_after,
              (SELECT json_agg(json_build_object('name', o.name, 'userId', o.user_id, 'score', o.score, 'place', o.place) ORDER BY o.seat)
                 FROM game_players o WHERE o.game_id = g.id) AS players
       FROM game_players gp JOIN games g ON g.id = gp.game_id
       WHERE gp.user_id = $1 AND ($3::timestamptz IS NULL OR g.finished_at < $3)
       ORDER BY g.finished_at DESC LIMIT $2`,
      [id, limit, before && !isNaN(Date.parse(before)) ? before : null],
    );
    return rows.map((r) => ({
      id: String(r.id),
      finishedAt: new Date(r.finished_at).toISOString(),
      mode: String(r.mode),
      pairs: Boolean(r.pairs),
      score: Number(r.score),
      place: Number(r.place),
      ratingBefore: Number(r.rating_before),
      ratingAfter: Number(r.rating_after),
      players: r.players ?? [],
    }));
  }

  async match(id: string) {
    if (!UUID_RE.test(id)) return null;
    const g = await this.pool.query(`SELECT id, finished_at, mode, khishti, pairs, pulkas FROM games WHERE id = $1`, [id]);
    if (!g.rows[0]) return null;
    const p = await this.pool.query(
      `SELECT gp.seat, gp.name, gp.user_id, gp.score, gp.place, gp.rating_before, gp.rating_after, u.avatar_version
       FROM game_players gp LEFT JOIN users u ON u.id = gp.user_id WHERE gp.game_id = $1 ORDER BY gp.seat`,
      [id],
    );
    const r = g.rows[0];
    return {
      id: String(r.id),
      finishedAt: new Date(r.finished_at).toISOString(),
      mode: String(r.mode),
      khishti: Number(r.khishti),
      pairs: Boolean(r.pairs),
      pulkas: r.pulkas ?? [],
      players: p.rows.map((x) => ({
        seat: Number(x.seat),
        name: String(x.name),
        userId: x.user_id ? String(x.user_id) : null,
        bot: !x.user_id,
        avatarUrl: x.user_id ? `/api/users/${x.user_id}/avatar?v=${x.avatar_version}` : null,
        score: Number(x.score),
        place: Number(x.place),
        ratingBefore: x.rating_before === null ? null : Number(x.rating_before),
        ratingAfter: x.rating_after === null ? null : Number(x.rating_after),
      })),
    };
  }

  async penalize(userId: string, amount: number, reason: string, roomCode: string) {
    if (!UUID_RE.test(userId)) return null;
    const { rows } = await this.pool.query(
      `WITH p AS (INSERT INTO rating_penalties (user_id, amount, reason, room_code) VALUES ($1, $2, $3, $4))
       UPDATE users SET rating = rating - $2 WHERE id = $1 RETURNING rating`,
      [userId, amount, reason, roomCode],
    );
    return rows[0] ? Number(rows[0].rating) : null;
  }

  async rateGame(game: FinishedGame) {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      const ids = [...new Set(game.userIds.filter((x): x is string => !!x))];
      const { rows } = await client.query(`SELECT ${USER_COLS} FROM users WHERE id = ANY($1::uuid[]) ORDER BY id FOR UPDATE`, [ids]);
      const users = new Map(rows.map((r) => [String(r.id), rowToUser(r)]));
      const changes = computeChanges(game, users);
      const pl = places(game.totals, game.pairs);
      const g = await client.query(
        `INSERT INTO games (room_code, mode, totals, khishti, pairs, pulkas) VALUES ($1, $2, $3, $4, $5, $6) RETURNING id`,
        [game.code, game.mode, game.totals, game.khishti, Boolean(game.pairs), JSON.stringify(game.pulkas)],
      );
      for (let i = 0; i < game.userIds.length; i++) {
        const c = changes[i];
        const uid = game.userIds[i] && users.has(game.userIds[i]!) ? game.userIds[i] : null;
        await client.query(
          `INSERT INTO game_players (game_id, seat, user_id, name, score, place, rating_before, rating_after)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
          [g.rows[0].id, i, uid, game.names[i] ?? "", game.totals[i], pl[i], c?.before ?? null, c?.after ?? null],
        );
        if (uid && c) {
          await client.query(
            `UPDATE users SET rating = $2, games = games + 1, wins = wins + $3, peak = GREATEST(peak, $2) WHERE id = $1`,
            [uid, c.after, pl[i] === 1 ? 1 : 0],
          );
        }
      }
      await client.query("COMMIT");
      return { matchId: String(g.rows[0].id), changes };
    } catch (e) {
      await client.query("ROLLBACK");
      throw e;
    } finally {
      client.release();
    }
  }
}

function nickConflict(e: unknown): unknown {
  return (e as { code?: string })?.code === "23505" ? new InputError("That nickname is taken") : e;
}

export function makeStore(): Store {
  const url = process.env.DATABASE_URL;
  return url ? new PgStore(url) : new MemoryStore();
}
