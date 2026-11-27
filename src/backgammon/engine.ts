// Backgammon for two players, played as a match to N points. Pure and serialisable
// like the other engines: every function takes a BgState and returns a new one.
//
// Standard rules:
// - 15 checkers each. Seat 0 moves from point 24 down to point 1 and bears off below it;
//   seat 1 moves the other way. The board is stored from seat 0's side: index 0 is
//   seat 0's 1-point, index 23 its 24-point. board[i] > 0 = seat 0 checkers, < 0 = seat 1.
// - Opening roll: each player rolls one die, the higher one moves first with both numbers
//   (equal dice are rolled again).
// - Doubles are played four times. A player must use as many dice as possible; if only
//   one of two different dice can be used, it must be the higher one.
// - A single checker (blot) that is hit goes to the bar and must enter in the opponent's
//   home board before anything else moves. Bearing off starts once all 15 are home;
//   a higher die may bear off from the highest point when no checker is further back.
// - Doubling cube: before rolling, the player with access to the cube (centred, or theirs)
//   may double. The opponent takes (the cube doubles and is theirs) or drops (losing the
//   game at the old value). No beavers, no Jacoby rule (this is match play).
// - Single game = 1 x cube, gammon (loser has borne off nothing) = 2 x cube, backgammon
//   (gammon and a loser's checker still on the bar or in the winner's home board) = 3 x cube.
// - Crawford rule: in the game right after a player first reaches match length - 1,
//   nobody may double.

import { rng } from "../engine/cards";
import { RuleError } from "../engine/game";

export const MATCH_LENGTHS = [1, 3, 5, 7, 11];
export const DEFAULT_MATCH_LENGTH = 5;
export const MAX_CUBE = 64;
const GAME_PAUSE_MS = 6000;
const BLOCKED_PAUSE_MS = 2200;
const OPENING_PAUSE_MS = 1800;

export class BgError extends RuleError {}

/** Where checkers start, as [seat 0's point number (1-24), count]. */
const START: [number, number][] = [
  [24, 2],
  [13, 5],
  [8, 3],
  [6, 5],
];

export type BgPhase = "opening" | "roll" | "moving" | "doubled" | "gameOver" | "matchOver";

export interface BgStep {
  /** Board index the checker left, or "bar". */
  from: number | "bar";
  /** Board index it reached, or "off". */
  to: number | "off";
  die: number;
  hit: boolean;
}

export interface BgGameResult {
  winner: number;
  /** 1 single, 2 gammon, 3 backgammon. */
  kind: 1 | 2 | 3;
  cube: number;
  points: number;
  /** The loser dropped a double. */
  dropped: boolean;
  /** Match score after the game. */
  score: [number, number];
  crawford: boolean;
}

interface Pos {
  board: number[];
  bar: [number, number];
  off: [number, number];
}

export interface BgState extends Pos {
  kind: "backgammon";
  phase: BgPhase;
  matchTo: number;
  score: [number, number];
  /** Game number in the match, from 1. */
  gameNo: number;
  seed: number;
  /** Dice rolled so far (feeds the random source). */
  rolls: number;
  turn: number;
  /** The dice of this turn, as rolled (two values). */
  dice: number[] | null;
  /** Dice values still to be used this turn. */
  left: number[];
  /** Opening roll of the current game: [seat 0 die, seat 1 die]. */
  opening: [number, number] | null;
  /** Steps made so far this turn (can be undone). */
  moved: BgStep[];
  /** Position at the start of the turn, for undo. */
  turnStart: Pos | null;
  /** The previous turn, for showing what the opponent did. */
  lastTurn: { seat: number; dice: number[]; steps: BgStep[] } | null;
  cube: number;
  /** -1 centred, otherwise the seat that owns it. */
  cubeOwner: number;
  /** This game is the Crawford game (no doubling). */
  crawford: boolean;
  /** The Crawford game has already been played. */
  crawfordDone: boolean;
  /** Counts every change, for the room's turn clock. */
  step: number;
  games: BgGameResult[];
  lastGame: BgGameResult | null;
}

export type BgAction =
  | { t: "roll" }
  | { t: "double" }
  | { t: "take" }
  | { t: "drop" }
  | { t: "move"; from: number | "bar"; die: number }
  | { t: "undo" }
  | { t: "done" }
  /** Internal: what happens by itself after a pause (next game, a blocked turn passes). */
  | { t: "auto" };

const other = (seat: number) => 1 - seat;
const sign = (seat: number) => (seat === 0 ? 1 : -1);

function clonePos(p: Pos): Pos {
  return { board: p.board.slice(), bar: [p.bar[0], p.bar[1]], off: [p.off[0], p.off[1]] };
}

function clone(s: BgState): BgState {
  return {
    ...s,
    ...clonePos(s),
    score: [s.score[0], s.score[1]],
    dice: s.dice ? s.dice.slice() : null,
    left: s.left.slice(),
    moved: s.moved.slice(),
    games: s.games.slice(),
  };
}

/** Checkers of `seat` on board index i. */
export function countAt(p: Pos, i: number, seat: number): number {
  const v = p.board[i] * sign(seat);
  return v > 0 ? v : 0;
}

/** How far a board index is from bearing off for `seat` (1..24). */
export function distance(i: number, seat: number): number {
  return seat === 0 ? i + 1 : 24 - i;
}

export function startBoard(): number[] {
  const b = Array(24).fill(0);
  for (const [pt, n] of START) {
    b[pt - 1] += n;
    b[24 - pt] -= n;
  }
  return b;
}

export function pipCount(p: Pos, seat: number): number {
  let pips = p.bar[seat] * 25;
  for (let i = 0; i < 24; i++) pips += countAt(p, i, seat) * distance(i, seat);
  return pips;
}

function allHome(p: Pos, seat: number): boolean {
  if (p.bar[seat] > 0) return false;
  for (let i = 0; i < 24; i++) if (countAt(p, i, seat) > 0 && distance(i, seat) > 6) return false;
  return true;
}

/** Where a checker moved from `from` with `die` lands, or null if it can't move. */
export function target(p: Pos, seat: number, from: number | "bar", die: number): number | "off" | null {
  if (p.bar[seat] > 0 && from !== "bar") return null;
  let to: number;
  if (from === "bar") {
    if (p.bar[seat] === 0) return null;
    to = seat === 0 ? 24 - die : die - 1;
  } else {
    if (!(from >= 0 && from < 24) || countAt(p, from, seat) === 0) return null;
    const dist = distance(from, seat);
    if (dist - die <= 0) {
      if (!allHome(p, seat)) return null;
      if (dist === die) return "off";
      // A higher die bears off only from the highest occupied point.
      for (let i = 0; i < 24; i++) if (countAt(p, i, seat) > 0 && distance(i, seat) > dist) return null;
      return "off";
    }
    to = seat === 0 ? from - die : from + die;
  }
  if (countAt(p, to, other(seat)) >= 2) return null;
  return to;
}

function applyStep(p: Pos, seat: number, from: number | "bar", die: number): BgStep | null {
  const to = target(p, seat, from, die);
  if (to === null) return null;
  const sg = sign(seat);
  if (from === "bar") p.bar[seat]--;
  else p.board[from] -= sg;
  let hit = false;
  if (to === "off") p.off[seat]++;
  else {
    if (countAt(p, to, other(seat)) === 1) {
      p.board[to] = 0;
      p.bar[other(seat)]++;
      hit = true;
    }
    p.board[to] += sg;
  }
  return { from, to, die, hit };
}

function sources(p: Pos, seat: number): (number | "bar")[] {
  if (p.bar[seat] > 0) return ["bar"];
  const out: number[] = [];
  for (let i = 0; i < 24; i++) if (countAt(p, i, seat) > 0) out.push(i);
  return out;
}

function without(dice: number[], die: number): number[] {
  const i = dice.indexOf(die);
  return i < 0 ? dice : [...dice.slice(0, i), ...dice.slice(i + 1)];
}

/** Most dice that can still be used from this position. */
function maxUsable(p: Pos, seat: number, dice: number[]): number {
  if (!dice.length || p.off[seat] === 15) return 0;
  let best = 0;
  for (const die of [...new Set(dice)]) {
    for (const from of sources(p, seat)) {
      const q = clonePos(p);
      if (!applyStep(q, seat, from, die)) continue;
      const n = 1 + maxUsable(q, seat, without(dice, die));
      if (n > best) best = n;
      if (best === dice.length) return best;
    }
  }
  return best;
}

/** The single checker moves the player to act may make now. */
export function legalSteps(s: BgState): { from: number | "bar"; to: number | "off"; die: number }[] {
  if (s.phase !== "moving" || !s.left.length) return [];
  const seat = s.turn;
  const need = maxUsable(s, seat, s.left);
  if (need === 0) return [];
  let out: { from: number | "bar"; to: number | "off"; die: number }[] = [];
  for (const die of [...new Set(s.left)]) {
    for (const from of sources(s, seat)) {
      const q = clonePos(s);
      const st = applyStep(q, seat, from, die);
      if (!st) continue;
      if (1 + maxUsable(q, seat, without(s.left, die)) === need) out.push({ from, to: st.to, die });
    }
  }
  // Only one of two different dice can be used: it must be the higher one, if possible.
  if (need === 1 && s.left.length === 2 && s.left[0] !== s.left[1]) {
    const hi = Math.max(...s.left);
    if (out.some((m) => m.die === hi)) out = out.filter((m) => m.die === hi);
  }
  return out;
}

function roll(s: BgState): number {
  const r = rng((s.seed ^ Math.imul(s.rolls + 1, 0x9e3779b1)) >>> 0);
  s.rolls++;
  return 1 + Math.floor(r() * 6);
}

export function canDouble(s: BgState, seat: number): boolean {
  return (
    s.phase === "roll" &&
    s.turn === seat &&
    !s.crawford &&
    s.cube < MAX_CUBE &&
    (s.cubeOwner === -1 || s.cubeOwner === seat) &&
    // A player who has already won the match with the cube as it is gains nothing.
    s.score[seat] + s.cube < s.matchTo
  );
}

function startGame(s: BgState): void {
  s.board = startBoard();
  s.bar = [0, 0];
  s.off = [0, 0];
  s.cube = 1;
  s.cubeOwner = -1;
  s.moved = [];
  s.lastTurn = null;
  s.turnStart = null;
  let a = 0;
  let b = 0;
  while (a === b) {
    a = roll(s);
    b = roll(s);
  }
  s.opening = [a, b];
  s.turn = a > b ? 0 : 1;
  s.dice = [a, b];
  s.left = [a, b];
  s.phase = "opening";
}

export function newBackgammon(opts: { matchTo?: number } = {}, seed = Date.now()): BgState {
  const matchTo = MATCH_LENGTHS.includes(Number(opts.matchTo)) ? Number(opts.matchTo) : DEFAULT_MATCH_LENGTH;
  const s: BgState = {
    kind: "backgammon",
    phase: "opening",
    matchTo,
    score: [0, 0],
    gameNo: 1,
    seed: seed >>> 0,
    rolls: 0,
    turn: 0,
    dice: null,
    left: [],
    opening: null,
    moved: [],
    turnStart: null,
    lastTurn: null,
    board: startBoard(),
    bar: [0, 0],
    off: [0, 0],
    cube: 1,
    cubeOwner: -1,
    crawford: false,
    crawfordDone: false,
    step: 0,
    games: [],
    lastGame: null,
  };
  startGame(s);
  return s;
}

/** Starts the moving part of a turn with these dice. */
function beginMoves(s: BgState, dice: number[]): void {
  s.dice = dice.slice();
  s.left = dice[0] === dice[1] ? [dice[0], dice[0], dice[0], dice[0]] : dice.slice();
  s.moved = [];
  s.turnStart = clonePos(s);
  s.phase = "moving";
}

function rollAndMove(s: BgState): void {
  beginMoves(s, [roll(s), roll(s)]);
}

/** Hands the turn to the other player: they may double first, otherwise their dice are rolled. */
function passTurn(s: BgState): void {
  s.lastTurn = { seat: s.turn, dice: s.dice ?? [], steps: s.moved };
  s.turn = other(s.turn);
  s.dice = null;
  s.left = [];
  s.moved = [];
  s.turnStart = null;
  s.phase = "roll";
  if (!canDouble(s, s.turn)) rollAndMove(s);
}

function endGame(s: BgState, winner: number, dropped: boolean): void {
  const loser = other(winner);
  let kind: 1 | 2 | 3 = 1;
  if (!dropped && s.off[loser] === 0) {
    kind = 2;
    let back = s.bar[loser] > 0;
    // A loser's checker in the winner's home board (distance 1-6 for the winner).
    for (let i = 0; i < 24; i++) if (countAt(s, i, loser) > 0 && distance(i, winner) <= 6) back = true;
    if (back) kind = 3;
  }
  const points = kind * s.cube;
  s.score[winner] += points;
  const result: BgGameResult = {
    winner,
    kind,
    cube: s.cube,
    points,
    dropped,
    score: [s.score[0], s.score[1]],
    crawford: s.crawford,
  };
  s.games.push(result);
  s.lastGame = result;
  if (s.crawford) s.crawfordDone = true;
  s.lastTurn = { seat: s.turn, dice: s.dice ?? [], steps: s.moved };
  s.left = [];
  s.moved = [];
  s.turnStart = null;
  s.phase = s.score[winner] >= s.matchTo ? "matchOver" : "gameOver";
}

function nextGame(s: BgState): void {
  s.gameNo++;
  // The Crawford game: right after someone first comes within one point of the match.
  const near = s.score.some((x) => x === s.matchTo - 1);
  s.crawford = !s.crawfordDone && near && s.matchTo > 1;
  if (s.crawford === false && near) s.crawfordDone = true;
  startGame(s);
}

/** Seat that must act now, or null when the game moves on by itself. */
export function bgActor(s: BgState): number | null {
  if (s.phase === "roll" || s.phase === "moving") return s.turn;
  if (s.phase === "doubled") return other(s.turn);
  return null;
}

/** Pause before something happens by itself (ms), or null when a player must act. */
export function bgAutoDelay(s: BgState): number | null {
  if (s.phase === "gameOver") return GAME_PAUSE_MS;
  if (s.phase === "opening") return OPENING_PAUSE_MS;
  if (s.phase === "moving" && s.moved.length === 0 && legalSteps(s).length === 0) return BLOCKED_PAUSE_MS;
  return null;
}

export function applyBackgammon(state: BgState, seat: number, a: BgAction): BgState {
  const s = clone(state);
  s.step++;
  if (a.t === "auto") {
    if (s.phase === "gameOver") nextGame(s);
    else if (s.phase === "opening") beginMoves(s, s.dice ?? [1, 2]);
    else if (s.phase === "moving" && s.moved.length === 0 && legalSteps(s).length === 0) passTurn(s);
    else throw new BgError("Nothing to do");
    return s;
  }
  if (s.phase === "matchOver" || s.phase === "gameOver") throw new BgError("The game is over");
  if (s.phase === "opening") {
    // The first player can start moving right away instead of waiting for the pause.
    if (seat !== s.turn || a.t !== "move") throw new BgError("Wait for the opening roll");
    beginMoves(s, s.dice ?? [1, 2]);
  }
  switch (a.t) {
    case "roll": {
      if (s.phase !== "roll" || seat !== s.turn) throw new BgError("Not your turn");
      rollAndMove(s);
      return s;
    }
    case "double": {
      if (!canDouble(s, seat)) throw new BgError("You can't double now");
      s.phase = "doubled";
      return s;
    }
    case "take": {
      if (s.phase !== "doubled" || seat !== other(s.turn)) throw new BgError("Not your decision");
      s.cube *= 2;
      s.cubeOwner = seat;
      rollAndMove(s);
      return s;
    }
    case "drop": {
      if (s.phase !== "doubled" || seat !== other(s.turn)) throw new BgError("Not your decision");
      endGame(s, s.turn, true);
      return s;
    }
    case "move": {
      if (s.phase !== "moving" || seat !== s.turn) throw new BgError("Not your turn");
      const from = a.from === "bar" ? "bar" : Number(a.from);
      const die = Number(a.die);
      if (!legalSteps(s).some((m) => m.from === from && m.die === die)) throw new BgError("That move is not allowed");
      const st = applyStep(s, seat, from, die)!;
      s.left = without(s.left, die);
      s.moved = [...s.moved, st];
      if (s.off[seat] === 15) endGame(s, seat, false);
      return s;
    }
    case "undo": {
      if (s.phase !== "moving" || seat !== s.turn) throw new BgError("Not your turn");
      if (!s.moved.length || !s.turnStart) throw new BgError("Nothing to undo");
      Object.assign(s, clonePos(s.turnStart));
      s.left = s.dice![0] === s.dice![1] ? [s.dice![0], s.dice![0], s.dice![0], s.dice![0]] : s.dice!.slice();
      s.moved = [];
      return s;
    }
    case "done": {
      if (s.phase !== "moving" || seat !== s.turn) throw new BgError("Not your turn");
      if (legalSteps(s).length) throw new BgError("You still have moves to play");
      passTurn(s);
      return s;
    }
    default:
      throw new BgError("Unknown move");
  }
}

// ---------- What a player may see ----------

export function bgView(s: BgState, seat: number | null) {
  const actor = bgActor(s);
  const mine = seat !== null && actor === seat;
  const opening = s.phase === "opening" && seat === s.turn;
  return {
    kind: "backgammon" as const,
    phase: s.phase,
    matchTo: s.matchTo,
    score: s.score,
    gameNo: s.gameNo,
    turn: s.turn,
    board: s.board,
    bar: s.bar,
    off: s.off,
    pips: [pipCount(s, 0), pipCount(s, 1)],
    dice: s.dice,
    left: s.left,
    opening: s.opening,
    moved: s.moved,
    lastTurn: s.lastTurn,
    cube: s.cube,
    cubeOwner: s.cubeOwner,
    crawford: s.crawford,
    games: s.games,
    lastGame: s.lastGame,
    /** Single checker moves you may make now (also during the opening pause, if you move first). */
    legal: mine || opening ? legalSteps(opening ? { ...s, phase: "moving", left: s.dice ?? [] } : s) : [],
    canDouble: seat !== null && canDouble(s, seat),
    canRoll: mine && s.phase === "roll",
    canUndo: mine && s.phase === "moving" && s.moved.length > 0,
    canDone: mine && s.phase === "moving" && s.moved.length > 0 && legalSteps(s).length === 0,
    mustAnswer: mine && s.phase === "doubled",
  };
}
