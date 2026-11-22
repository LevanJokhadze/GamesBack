// No-limit Texas Hold'em cash table with play chips. Pure and serialisable like the other
// engines: every function takes a PokerState and returns a new one.
//
// Rules (confirmed with the project owner: cash table, unrated):
// - 2 to 9 seats. Everyone sits down with 100 big blinds of play chips.
// - A player who runs out of chips can take a free rebuy; it applies from the next hand.
//   Players can sit down and stand up between hands (standing up during a hand folds it).
// - Standard no-limit betting: the minimum bet is one big blind, a raise must be at least
//   the size of the last full raise, and an all-in for less does not reopen the betting
//   for players who already acted. Heads-up the button posts the small blind and acts
//   first before the flop, last after it.
// - Side pots at showdown, uncalled bets go back, an odd chip goes to the first winner
//   left of the button.
// - The table never ends by itself: the host ends the session.

import { Card, rng, shuffle } from "../engine/cards";
import { RuleError } from "../engine/game";
import { HandCategory, bestHand, pokerDeck } from "./hand";

export const POKER_MIN_SEATS = 2;
export const POKER_MAX_SEATS = 9;
export const POKER_DEFAULT_SEATS = 6;
/** Big blind choices; the small blind is half. */
export const BIG_BLINDS = [10, 20, 50, 100];
export const DEFAULT_BIG_BLIND = 20;
export const BUY_IN_BBS = 100;
const SHOWDOWN_PAUSE_MS = 5500;
const FOLD_PAUSE_MS = 2500;

export class PokerError extends RuleError {}

export type Street = 0 | 1 | 2 | 3; // preflop, flop, turn, river

export type PokerAction =
  | { t: "fold" }
  | { t: "check" }
  | { t: "call" }
  /** Bet or raise so that your total bet this street is `to` (all-in if it is more than you have). */
  | { t: "raise"; to: number }
  | { t: "rebuy" }
  /** Server only: deal the next hand. */
  | { t: "next" };

export interface PotResult {
  amount: number;
  winners: number[];
}

export interface ShownHand {
  seat: number;
  cards: Card[];
  cat: HandCategory;
  best: Card[];
}

export interface HandResult {
  handNo: number;
  board: Card[];
  pots: PotResult[];
  /** Hands shown at the showdown (empty when everyone else folded). */
  shown: ShownHand[];
  /** Chips won or lost in this hand, per seat. */
  net: number[];
  uncontested: boolean;
}

export interface PokerState {
  kind: "poker";
  seed: number;
  n: number;
  bb: number;
  buyIn: number;
  phase: "waiting" | "betting" | "handOver";
  handNo: number;
  seated: boolean[];
  stacks: number[];
  /** Chips taken from the bank by whoever sits in the seat (buy-in plus rebuys). */
  bought: number[];
  /** Rebuy asked for; applied when the next hand starts. */
  rebuy: boolean[];
  button: number;
  sbSeat: number;
  bbSeat: number;
  street: Street;
  board: Card[];
  deck: Card[];
  hole: (Card[] | null)[];
  inHand: boolean[];
  folded: boolean[];
  /** Chips put in this street. */
  bets: number[];
  /** Chips put in this hand (including this street). */
  committed: number[];
  currentBet: number;
  /** Size of the last full bet or raise this street. */
  minRaise: number;
  acted: boolean[];
  /** May still raise: false after acting until somebody makes a full raise. */
  raiseOpen: boolean[];
  turn: number;
  /** Last action per seat this street, for the table ("check", "call", "raise", "fold", "allin"). */
  said: (string | null)[];
  result: HandResult | null;
  step: number;
}

const clone = (s: PokerState): PokerState => JSON.parse(JSON.stringify(s));
const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);

export function newPoker(opts: { seats: number; bb?: number; seated: boolean[] }, seed = Date.now()): PokerState {
  const n = Math.max(POKER_MIN_SEATS, Math.min(POKER_MAX_SEATS, opts.seats));
  const bb = BIG_BLINDS.includes(opts.bb ?? 0) ? opts.bb! : DEFAULT_BIG_BLIND;
  const buyIn = bb * BUY_IN_BBS;
  const seated = Array.from({ length: n }, (_, i) => Boolean(opts.seated[i]));
  const z = () => Array<number>(n).fill(0);
  const f = () => Array<boolean>(n).fill(false);
  const s: PokerState = {
    kind: "poker",
    seed: seed >>> 0,
    n,
    bb,
    buyIn,
    phase: "waiting",
    handNo: 0,
    seated,
    stacks: seated.map((x) => (x ? buyIn : 0)),
    bought: seated.map((x) => (x ? buyIn : 0)),
    rebuy: f(),
    button: -1,
    sbSeat: -1,
    bbSeat: -1,
    street: 0,
    board: [],
    deck: [],
    hole: Array(n).fill(null),
    inHand: f(),
    folded: f(),
    bets: z(),
    committed: z(),
    currentBet: 0,
    minRaise: bb,
    acted: f(),
    raiseOpen: f(),
    turn: -1,
    said: Array(n).fill(null),
    result: null,
    step: 0,
  };
  return startHand(s);
}

// ---------- helpers ----------

function nextFrom(s: PokerState, from: number, ok: (i: number) => boolean): number {
  for (let k = 1; k <= s.n; k++) {
    const i = (((from + k) % s.n) + s.n) % s.n;
    if (ok(i)) return i;
  }
  return -1;
}

const live = (s: PokerState, i: number) => s.inHand[i] && !s.folded[i];
/** In the hand and still has chips to bet with. */
const canAct = (s: PokerState, i: number) => live(s, i) && s.stacks[i] > 0;
const liveCount = (s: PokerState) => s.inHand.filter((_, i) => live(s, i)).length;
const actorCount = (s: PokerState) => s.inHand.filter((_, i) => canAct(s, i)).length;

function needsAct(s: PokerState, i: number): boolean {
  if (!canAct(s, i)) return false;
  if (actorCount(s) <= 1) {
    // Everyone else is all-in: only a call is left to make, if that.
    const others = Math.max(0, ...s.bets.filter((_, j) => j !== i && live(s, j)));
    return s.bets[i] < others;
  }
  return !s.acted[i] || s.bets[i] < s.currentBet;
}

function put(s: PokerState, i: number, amount: number): void {
  const a = Math.min(amount, s.stacks[i]);
  s.stacks[i] -= a;
  s.bets[i] += a;
  s.committed[i] += a;
}

/** Random source for one hand (the seed and the hand number). */
function handRand(s: PokerState): () => number {
  return rng((s.seed ^ Math.imul(s.handNo + 1, 0x9e3779b1)) >>> 0);
}

// ---------- dealing ----------

function startHand(state: PokerState): PokerState {
  const s = state;
  for (let i = 0; i < s.n; i++) {
    if (s.rebuy[i] && s.seated[i] && s.stacks[i] === 0) {
      s.stacks[i] = s.buyIn;
      s.bought[i] += s.buyIn;
    }
    s.rebuy[i] = false;
  }
  const players = s.seated.map((x, i) => x && s.stacks[i] > 0);
  s.board = [];
  s.deck = [];
  s.hole = Array(s.n).fill(null);
  s.inHand = players.slice();
  s.folded = Array(s.n).fill(false);
  s.bets = Array(s.n).fill(0);
  s.committed = Array(s.n).fill(0);
  s.acted = Array(s.n).fill(false);
  s.raiseOpen = players.slice();
  s.said = Array(s.n).fill(null);
  s.street = 0;
  s.currentBet = 0;
  s.minRaise = s.bb;
  s.turn = -1;
  if (players.filter(Boolean).length < 2) {
    s.phase = "waiting";
    s.inHand = Array(s.n).fill(false);
    s.sbSeat = s.bbSeat = -1;
    return s;
  }
  const rand = handRand(s);
  const isIn = (i: number) => players[i];
  s.button = s.button < 0 ? nextFrom(s, Math.floor(rand() * s.n) - 1, isIn) : nextFrom(s, s.button, isIn);
  const headsUp = players.filter(Boolean).length === 2;
  s.sbSeat = headsUp ? s.button : nextFrom(s, s.button, isIn);
  s.bbSeat = nextFrom(s, s.sbSeat, isIn);
  s.deck = shuffle(pokerDeck(), rand);
  // Two cards each, one at a time, starting left of the button.
  for (let round = 0; round < 2; round++) {
    let i = s.button;
    for (let k = 0; k < s.n; k++) {
      i = (i + 1) % s.n;
      if (!isIn(i)) continue;
      (s.hole[i] ??= []).push(s.deck.pop()!);
    }
  }
  put(s, s.sbSeat, s.bb / 2);
  put(s, s.bbSeat, s.bb);
  s.currentBet = s.bb;
  s.minRaise = s.bb;
  s.phase = "betting";
  s.result = null;
  s.handNo += 1;
  const first = nextFrom(s, s.bbSeat, (i) => needsAct(s, i));
  if (first < 0) return finishStreet(s);
  s.turn = first;
  return s;
}

function finishStreet(s: PokerState): PokerState {
  if (liveCount(s) <= 1) return award(s);
  const runOut = actorCount(s) <= 1;
  if (s.street === 3) return award(s);
  s.street = (s.street + 1) as Street;
  s.deck.pop(); // burn
  const deal = s.street === 1 ? 3 : 1;
  for (let k = 0; k < deal; k++) s.board.push(s.deck.pop()!);
  s.bets = Array(s.n).fill(0);
  s.currentBet = 0;
  s.minRaise = s.bb;
  s.acted = Array(s.n).fill(false);
  s.raiseOpen = s.inHand.map((_, i) => canAct(s, i));
  s.said = s.said.map((x) => (x === "fold" || x === "allin" ? x : null));
  if (runOut) return finishStreet(s);
  const first = nextFrom(s, s.button, (i) => needsAct(s, i));
  if (first < 0) return finishStreet(s);
  s.turn = first;
  return s;
}

/** End of the hand: return the uncalled bet, split the pots, show the hands. */
function award(s: PokerState): PokerState {
  const n = s.n;
  const before = s.stacks.map((x, i) => x + s.committed[i]);
  // Uncalled part of the biggest bet goes back.
  const order = s.committed.map((c, i) => ({ c, i })).sort((a, b) => b.c - a.c);
  if (order.length > 1 && order[0].c > order[1].c) {
    const back = order[0].c - order[1].c;
    s.committed[order[0].i] -= back;
    s.stacks[order[0].i] += back;
  }
  const alive = s.inHand.map((_, i) => live(s, i));
  const pots: PotResult[] = [];
  const shown: ShownHand[] = [];
  const uncontested = alive.filter(Boolean).length === 1;
  if (uncontested) {
    const w = alive.indexOf(true);
    pots.push({ amount: sum(s.committed), winners: [w] });
  } else {
    const ranks = new Map<number, number>();
    for (let i = 0; i < n; i++) {
      if (!alive[i]) continue;
      const r = bestHand([...s.hole[i]!, ...s.board]);
      ranks.set(i, r.score);
      shown.push({ seat: i, cards: s.hole[i]!, cat: r.cat, best: r.best });
    }
    const levels = [...new Set(s.committed.filter((c, i) => alive[i] && c > 0))].sort((a, b) => a - b);
    let prev = 0;
    for (const lv of levels) {
      const amount = sum(s.committed.map((c) => Math.max(0, Math.min(c, lv) - prev)));
      const eligible = [...ranks.keys()].filter((i) => s.committed[i] >= lv);
      if (amount > 0) {
        const top = Math.max(...eligible.map((i) => ranks.get(i)!));
        const winners = eligible.filter((i) => ranks.get(i) === top);
        const last = pots.at(-1);
        if (last && last.winners.join() === winners.join()) last.amount += amount;
        else pots.push({ amount, winners });
      }
      prev = lv;
    }
    // Chips a folded player put in above every live player's total.
    const extra = sum(s.committed.map((c) => Math.max(0, c - prev)));
    if (extra > 0 && pots.length) pots[pots.length - 1].amount += extra;
  }
  for (const p of pots) {
    const share = Math.floor(p.amount / p.winners.length);
    let rest = p.amount - share * p.winners.length;
    for (const w of p.winners) s.stacks[w] += share;
    // Odd chips: first winner left of the button.
    for (let k = 1; k <= n && rest > 0; k++) {
      const i = (s.button + k) % n;
      if (p.winners.includes(i)) {
        s.stacks[i] += 1;
        rest -= 1;
      }
    }
  }
  s.result = {
    handNo: s.handNo,
    board: s.board.slice(),
    pots,
    shown,
    net: s.stacks.map((x, i) => x - before[i]),
    uncontested,
  };
  s.phase = "handOver";
  s.turn = -1;
  return s;
}

// ---------- actions ----------

export function minRaiseTo(s: PokerState): number {
  return s.currentBet === 0 ? s.bb : s.currentBet + s.minRaise;
}

export interface Options {
  toCall: number;
  canCheck: boolean;
  canCall: boolean;
  canRaise: boolean;
  minTo: number;
  maxTo: number;
}

/** What `seat` may do now (all false when it is not their turn). */
export function pokerOptions(s: PokerState, seat: number | null): Options {
  const none = { toCall: 0, canCheck: false, canCall: false, canRaise: false, minTo: 0, maxTo: 0 };
  if (seat === null || s.phase !== "betting" || s.turn !== seat) return none;
  const toCall = Math.max(0, s.currentBet - s.bets[seat]);
  const maxTo = s.bets[seat] + s.stacks[seat];
  const othersCanAct = s.inHand.some((_, i) => i !== seat && canAct(s, i));
  const canRaise = s.raiseOpen[seat] && maxTo > s.currentBet && othersCanAct;
  return {
    toCall: Math.min(toCall, s.stacks[seat]),
    canCheck: toCall === 0,
    canCall: toCall > 0,
    canRaise,
    minTo: canRaise ? Math.min(maxTo, minRaiseTo(s)) : 0,
    maxTo: canRaise ? maxTo : 0,
  };
}

function afterAct(s: PokerState, seat: number): PokerState {
  s.acted[seat] = true;
  s.raiseOpen[seat] = false;
  if (s.stacks[seat] === 0 && live(s, seat)) s.said[seat] = "allin";
  if (liveCount(s) <= 1) return award(s);
  const next = nextFrom(s, seat, (i) => needsAct(s, i));
  if (next < 0) return finishStreet(s);
  s.turn = next;
  return s;
}

export function applyPoker(state: PokerState, seat: number, a: PokerAction): PokerState {
  const s = clone(state);
  s.step += 1;
  if (a.t === "next") {
    if (s.phase !== "handOver") throw new PokerError("Hand is not over");
    return startHand(s);
  }
  if (a.t === "rebuy") {
    if (!s.seated[seat]) throw new PokerError("Take a seat first");
    if (s.stacks[seat] > 0 || (s.inHand[seat] && s.phase === "betting" && !s.folded[seat])) throw new PokerError("You still have chips");
    s.rebuy[seat] = true;
    return s.phase === "waiting" ? startHand(s) : s;
  }
  if (s.phase !== "betting") throw new PokerError("No hand in play");
  if (seat !== s.turn) throw new PokerError("Not your turn");
  const o = pokerOptions(s, seat);
  switch (a.t) {
    case "fold":
      s.folded[seat] = true;
      s.said[seat] = "fold";
      return afterAct(s, seat);
    case "check":
      if (!o.canCheck) throw new PokerError("You can't check, there is a bet");
      s.said[seat] = "check";
      return afterAct(s, seat);
    case "call":
      if (!o.canCall) throw new PokerError("Nothing to call");
      put(s, seat, o.toCall);
      s.said[seat] = "call";
      return afterAct(s, seat);
    case "raise": {
      if (!o.canRaise) throw new PokerError("You can't raise now");
      let to = Math.floor(Number(a.to));
      if (!Number.isFinite(to)) throw new PokerError("Bad amount");
      if (to >= o.maxTo) to = o.maxTo;
      else if (to < minRaiseTo(s)) throw new PokerError(`The minimum is ${minRaiseTo(s)}`);
      const increase = to - s.currentBet;
      put(s, seat, to - s.bets[seat]);
      if (increase >= s.minRaise) {
        // A full raise reopens the betting for everyone.
        s.minRaise = increase;
        for (let i = 0; i < s.n; i++) if (i !== seat && canAct(s, i)) s.raiseOpen[i] = true;
      }
      s.said[seat] = s.currentBet === 0 ? "bet" : "raise";
      s.currentBet = to;
      for (let i = 0; i < s.n; i++) if (i !== seat) s.acted[i] = false;
      return afterAct(s, seat);
    }
    default:
      throw new PokerError("Bad move");
  }
}

// ---------- seats (the room calls these) ----------

/** Someone sits down: a fresh buy-in; they are dealt in from the next hand. */
export function pokerSit(state: PokerState, seat: number): PokerState {
  const s = clone(state);
  if (seat < 0 || seat >= s.n || s.seated[seat]) throw new PokerError("That seat is taken");
  s.seated[seat] = true;
  s.stacks[seat] = s.buyIn;
  s.bought[seat] = s.buyIn;
  s.rebuy[seat] = false;
  s.step += 1;
  return s.phase === "waiting" ? startHand(s) : s;
}

/** Someone stands up: their hand is folded and the chips they put in stay in the pot. */
export function pokerLeave(state: PokerState, seat: number): PokerState {
  let s = clone(state);
  if (!s.seated[seat]) return s;
  s.step += 1;
  if (s.phase === "betting" && live(s, seat)) {
    if (s.turn === seat) s = applyPoker(s, seat, { t: "fold" });
    else {
      s.folded[seat] = true;
      s.said[seat] = "fold";
      if (liveCount(s) <= 1) s = award(s);
      else if (!needsAct(s, s.turn)) {
        const next = nextFrom(s, s.turn, (i) => needsAct(s, i));
        s = next < 0 ? finishStreet(s) : ((s.turn = next), s);
      }
    }
  }
  s.seated[seat] = false;
  s.stacks[seat] = 0;
  s.bought[seat] = 0;
  s.rebuy[seat] = false;
  return s;
}

/** Net chips (stack minus everything bought) per seat. */
export function pokerNet(s: PokerState): number[] {
  return s.stacks.map((x, i) => (s.seated[i] ? x + (s.phase === "betting" ? s.committed[i] : 0) - s.bought[i] : 0));
}

/** Pause before the next hand (ms), or null when nothing happens by itself. */
export function pokerAutoDelay(s: PokerState): number | null {
  if (s.phase !== "handOver") return null;
  return s.result?.uncontested ? FOLD_PAUSE_MS : SHOWDOWN_PAUSE_MS;
}

export function pokerActor(s: PokerState): number | null {
  return s.phase === "betting" && s.turn >= 0 ? s.turn : null;
}

// ---------- what a player may see ----------

export function pokerView(s: PokerState, seat: number | null) {
  const shown = new Map((s.phase === "handOver" ? s.result?.shown ?? [] : []).map((h) => [h.seat, h]));
  const me = seat !== null && seat < s.n ? seat : null;
  const myCards = me !== null ? s.hole[me] : null;
  const board = s.phase === "handOver" && s.result ? s.result.board : s.board;
  let myHand: HandCategory | null = null;
  if (myCards && board.length >= 3) myHand = bestHand([...myCards, ...board]).cat;
  return {
    kind: "poker" as const,
    phase: s.phase,
    handNo: s.handNo,
    seats: s.n,
    bb: s.bb,
    sb: s.bb / 2,
    buyIn: s.buyIn,
    button: s.button,
    sbSeat: s.sbSeat,
    bbSeat: s.bbSeat,
    street: s.street,
    board,
    turn: s.turn,
    seated: s.seated,
    stacks: s.stacks,
    bets: s.bets,
    /** Chips in the middle from earlier streets. */
    pot: sum(s.committed) - sum(s.bets),
    inHand: s.inHand,
    folded: s.folded,
    said: s.said,
    /** Hole cards: your own, and everyone's at a showdown. */
    hole: s.hole.map((h, i) => (h && (i === me || shown.has(i)) ? h : null)),
    net: pokerNet(s),
    rebuy: s.rebuy,
    result: s.phase === "handOver" ? s.result : null,
    lastResult: s.result,
    myHand,
    ...pokerOptions(s, me),
    currentBet: s.currentBet,
  };
}
