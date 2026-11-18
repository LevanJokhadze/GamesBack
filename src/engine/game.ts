// Joker game engine. Pure and serialisable: every function takes a GameState
// and returns a new one (or mutates a structuredClone of it).

import { Card, Suit, SUITS, fullDeck, rng, shuffle, sortHand } from "./cards";

export const SEATS = 4;

export type GameMode = "standard" | "nines";

export interface GameConfig {
  mode: GameMode;
  /** Points for "khishti": bid at least one trick and took none. */
  khishti: number;
  /** Play in pairs: partners sit opposite (seats 0 & 2 against 1 & 3) and add their points together. */
  pairs: boolean;
}

export const DEFAULT_CONFIG: GameConfig = { mode: "standard", khishti: -200, pairs: false };

/** Team of a seat in pairs mode: 0 (seats 0 and 2) or 1 (seats 1 and 3). */
export function teamOf(seat: number): number {
  return seat % 2;
}

/** Combined points of the two teams. */
export function teamTotals(totals: number[]): [number, number] {
  return [totals[0] + totals[2], totals[1] + totals[3]];
}

/**
 * The score that decides places: a player's own total, or in pairs mode the
 * team's total (so partners always share a place).
 */
export function rankingScores(totals: number[], pairs: boolean): number[] {
  if (!pairs) return totals.slice();
  const t = teamTotals(totals);
  return totals.map((_, i) => t[teamOf(i)]);
}

export type JokerMode =
  | { type: "high"; suit?: Suit } // lead: "wants" the highest card of suit; follow: takes the trick
  | { type: "low"; suit?: Suit }; // lead: "takes" (lowest of suit); follow: gives the trick away

export interface Play {
  seat: number;
  card: Card;
  joker?: JokerMode;
}

export interface DealResult {
  handSize: number;
  bids: number[];
  took: number[];
  points: number[];
}

export interface PulkaResult {
  deals: DealResult[];
  /** Seats that hit every bid in the pulka. */
  premium: number[];
  /** Bonus added at the end of the pulka (premium). */
  bonus: number[];
  total: number[];
}

export type Phase = "trump" | "bidding" | "playing" | "gameOver";

export interface GameState {
  config: GameConfig;
  seed: number;
  /** Deal schedule: list of pulkas, each a list of hand sizes. */
  schedule: number[][];
  pulka: number;
  dealInPulka: number;
  dealer: number;
  handSize: number;
  phase: Phase;
  hands: Card[][];
  /** Card turned up to choose trump (deals with fewer than 9 cards). */
  trumpCard: Card | null;
  trump: Suit | null;
  bids: (number | null)[];
  took: number[];
  turn: number;
  trick: Play[];
  lastTrick: { plays: Play[]; winner: number } | null;
  pulkas: PulkaResult[];
  totals: number[];
  dealsPlayed: number;
}

export function makeSchedule(mode: GameMode): number[][] {
  if (mode === "nines") return [[9, 9, 9, 9], [9, 9, 9, 9], [9, 9, 9, 9], [9, 9, 9, 9]];
  return [
    [1, 2, 3, 4, 5, 6, 7, 8],
    [9, 9, 9, 9],
    [8, 7, 6, 5, 4, 3, 2, 1],
    [9, 9, 9, 9],
  ];
}

export function newGame(config: Partial<GameConfig> = {}, seed = Date.now()): GameState {
  const cfg: GameConfig = { ...DEFAULT_CONFIG, ...config };
  const schedule = makeSchedule(cfg.mode);
  const rand = rng(seed);
  const state: GameState = {
    config: cfg,
    seed,
    schedule,
    pulka: 0,
    dealInPulka: 0,
    dealer: Math.floor(rand() * SEATS),
    handSize: schedule[0][0],
    phase: "bidding",
    hands: [[], [], [], []],
    trumpCard: null,
    trump: null,
    bids: [null, null, null, null],
    took: [0, 0, 0, 0],
    turn: 0,
    trick: [],
    lastTrick: null,
    pulkas: [{ deals: [], premium: [], bonus: [0, 0, 0, 0], total: [0, 0, 0, 0] }],
    totals: [0, 0, 0, 0],
    dealsPlayed: 0,
  };
  return startDeal(state);
}

function next(seat: number): number {
  return (seat + 1) % SEATS;
}

function startDeal(s: GameState): GameState {
  const rand = rng(s.seed + s.dealsPlayed * 7919);
  const deck = shuffle(fullDeck(), rand);
  const n = s.handSize;
  const hands: Card[][] = [[], [], [], []];
  // Deal starting left of the dealer.
  let seat = next(s.dealer);
  for (let i = 0; i < n * SEATS; i++) {
    hands[seat].push(deck[i]);
    seat = next(seat);
  }
  s.hands = hands;
  s.bids = [null, null, null, null];
  s.took = [0, 0, 0, 0];
  s.trick = [];
  s.turn = next(s.dealer);
  if (n < 9) {
    const turned = deck[n * SEATS];
    s.trumpCard = turned;
    s.trump = turned.joker ? null : turned.suit;
    s.phase = "bidding";
    s.hands = hands.map((h) => sortHand(h, s.trump));
  } else {
    // Nine-card deal: the player left of the dealer looks at their first
    // three cards and names the trump (or no trump).
    s.trumpCard = null;
    s.trump = null;
    s.phase = "trump";
  }
  return s;
}

/** Cards a seat may see. During trump choice in 9-card deals only the first three are visible. */
export function visibleHand(s: GameState, seat: number): Card[] {
  if (s.phase === "trump") return s.hands[seat].slice(0, 3);
  return s.hands[seat];
}

export class RuleError extends Error {}

export function chooseTrump(state: GameState, seat: number, suit: Suit | null): GameState {
  if (state.phase !== "trump") throw new RuleError("Not choosing trump now");
  if (seat !== state.turn) throw new RuleError("Not your turn");
  if (suit !== null && !SUITS.includes(suit)) throw new RuleError("Bad suit");
  const s = structuredClone(state);
  s.trump = suit;
  s.phase = "bidding";
  s.hands = s.hands.map((h) => sortHand(h, s.trump));
  return s;
}

/** The bid the dealer may not make (so the total does not equal the hand size), or null. */
export function forbiddenBid(s: GameState, seat: number): number | null {
  if (seat !== s.dealer) return null;
  const others = s.bids.reduce<number>((acc, b, i) => (i === seat ? acc : acc + (b ?? 0)), 0);
  const f = s.handSize - others;
  return f >= 0 && f <= s.handSize ? f : null;
}

export function legalBids(s: GameState, seat: number): number[] {
  if (s.phase !== "bidding" || s.turn !== seat) return [];
  const f = forbiddenBid(s, seat);
  const out: number[] = [];
  for (let b = 0; b <= s.handSize; b++) if (b !== f) out.push(b);
  return out;
}

export function placeBid(state: GameState, seat: number, bid: number): GameState {
  if (state.phase !== "bidding") throw new RuleError("Not bidding now");
  if (seat !== state.turn) throw new RuleError("Not your turn");
  if (!Number.isInteger(bid) || !legalBids(state, seat).includes(bid)) {
    throw new RuleError("That bid is not allowed");
  }
  const s = structuredClone(state);
  s.bids[seat] = bid;
  if (seat === s.dealer) {
    s.phase = "playing";
    s.turn = next(s.dealer);
  } else {
    s.turn = next(seat);
  }
  return s;
}

/** The suit everyone must follow in the current trick, and what the lead demands. */
function leadInfo(s: GameState): { suit: Suit | null; wantsHighest: boolean } {
  const lead = s.trick[0];
  if (!lead) return { suit: null, wantsHighest: false };
  if (lead.card.joker) {
    return { suit: lead.joker?.suit ?? null, wantsHighest: lead.joker?.type === "high" };
  }
  return { suit: lead.card.suit, wantsHighest: false };
}

export function legalCards(s: GameState, seat: number): Card[] {
  if (s.phase !== "playing" || s.turn !== seat) return [];
  const hand = s.hands[seat];
  if (s.trick.length === 0) return hand.slice();
  const { suit, wantsHighest } = leadInfo(s);
  const jokers = hand.filter((c) => c.joker);
  const ofSuit = suit ? hand.filter((c) => c.suit === suit) : [];
  if (ofSuit.length) {
    if (wantsHighest) {
      const top = ofSuit.reduce((a, b) => (b.rank > a.rank ? b : a));
      return [top, ...jokers];
    }
    return [...ofSuit, ...jokers];
  }
  const trumps = s.trump ? hand.filter((c) => c.suit === s.trump) : [];
  if (trumps.length) return [...trumps, ...jokers];
  return hand.slice();
}

export function validateJokerMode(s: GameState, mode: JokerMode | undefined): JokerMode {
  const leading = s.trick.length === 0;
  if (!mode || (mode.type !== "high" && mode.type !== "low")) {
    throw new RuleError("Say how the joker is played");
  }
  if (leading) {
    if (!mode.suit || !SUITS.includes(mode.suit)) throw new RuleError("Name a suit for the joker");
    return { type: mode.type, suit: mode.suit };
  }
  return { type: mode.type };
}

/** Winner of a complete trick. */
export function trickWinner(plays: Play[], trump: Suit | null): number {
  // A joker played "high" takes the trick; if both jokers are high, the later one wins.
  const highs = plays.filter((p) => p.card.joker && p.joker?.type === "high");
  if (highs.length) return highs[highs.length - 1].seat;

  const lead = plays[0];
  const ledSuit = lead.card.joker ? lead.joker?.suit ?? null : lead.card.suit;
  let best: Play | null = null;
  const score = (p: Play): number => {
    if (p.card.joker) return -1;
    if (trump && p.card.suit === trump) return 100 + p.card.rank;
    if (p.card.suit === ledSuit) return p.card.rank;
    return -1;
  };
  for (const p of plays) {
    if (score(p) < 0) continue;
    if (!best || score(p) > score(best)) best = p;
  }
  // Only possible if a "low" joker led and nobody followed the suit or trumped.
  return best ? best.seat : lead.seat;
}

export function playCard(state: GameState, seat: number, cardId: string, joker?: JokerMode): GameState {
  if (state.phase !== "playing") throw new RuleError("Not playing now");
  if (seat !== state.turn) throw new RuleError("Not your turn");
  const legal = legalCards(state, seat);
  const card = legal.find((c) => c.id === cardId);
  if (!card) {
    if (state.hands[seat].some((c) => c.id === cardId)) throw new RuleError("You must follow the rules of the lead");
    throw new RuleError("You don't have that card");
  }
  const s = structuredClone(state);
  const play: Play = { seat, card };
  if (card.joker) play.joker = validateJokerMode(s, joker);
  s.hands[seat] = s.hands[seat].filter((c) => c.id !== cardId);
  s.trick.push(play);

  if (s.trick.length < SEATS) {
    s.turn = next(seat);
    return s;
  }

  const winner = trickWinner(s.trick, s.trump);
  s.took[winner] += 1;
  s.lastTrick = { plays: s.trick, winner };
  s.trick = [];
  s.turn = winner;

  if (s.hands.every((h) => h.length === 0)) return finishDeal(s);
  return s;
}

export function dealPoints(bid: number, took: number, handSize: number, khishti: number): number {
  if (bid === took) {
    if (bid === handSize && handSize > 1) return 100 * handSize;
    if (bid === 0) return 50;
    return 50 * bid + 50;
  }
  if (took === 0) return khishti;
  return 10 * took;
}

function finishDeal(s: GameState): GameState {
  const bids = s.bids.map((b) => b ?? 0);
  const points = bids.map((b, i) => dealPoints(b, s.took[i], s.handSize, s.config.khishti));
  const pulka = s.pulkas[s.pulka];
  pulka.deals.push({ handSize: s.handSize, bids, took: s.took.slice(), points });
  for (let i = 0; i < SEATS; i++) {
    pulka.total[i] += points[i];
    s.totals[i] += points[i];
  }
  s.dealsPlayed += 1;
  s.dealer = next(s.dealer);

  const pulkaSizes = s.schedule[s.pulka];
  if (s.dealInPulka + 1 < pulkaSizes.length) {
    s.dealInPulka += 1;
  } else {
    // End of pulka: premium for anyone who hit every bid. Their best hand counts twice.
    for (let i = 0; i < SEATS; i++) {
      const hitAll = pulka.deals.every((d) => d.bids[i] === d.took[i]);
      if (hitAll) {
        pulka.premium.push(i);
        const best = Math.max(...pulka.deals.map((d) => d.points[i]));
        pulka.bonus[i] = best;
        pulka.total[i] += best;
        s.totals[i] += best;
      }
    }
    if (s.pulka + 1 >= s.schedule.length) {
      s.phase = "gameOver";
      s.hands = [[], [], [], []];
      s.turn = -1;
      return s;
    }
    s.pulka += 1;
    s.dealInPulka = 0;
    s.pulkas.push({ deals: [], premium: [], bonus: [0, 0, 0, 0], total: [0, 0, 0, 0] });
  }
  s.handSize = s.schedule[s.pulka][s.dealInPulka];
  return startDeal(s);
}

/** Who has to act now, or -1. */
export function actor(s: GameState): number {
  return s.phase === "gameOver" ? -1 : s.turn;
}
