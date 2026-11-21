// 5-card Bura in pairs (2 against 2). Pure and serialisable, like the Joker engine:
// every function takes a BuraState and returns a new one.
//
// Rules (confirmed with the project owner, see README "Bura"):
// - 36 cards, 6..A. Order A > 10 > K > Q > J > 9 > 8 > 7 > 6. Points A 11, 10 10, K 4, Q 3, J 2.
// - Partners sit opposite: seats 0 & 2 (team 0) against 1 & 3 (team 1).
// - 5 cards each, the next card is trump and lies at the bottom of the stock (drawn last).
// - The leader plays 1-5 cards of one suit; everyone answers with the same number of cards.
//   A set takes the trick over only if it beats the current winning set one-for-one
//   (higher card of the same suit, or a trump against a non-trump). Highest beater wins.
//   Cards that don't beat go face down.
// - Malyutka: all 5 cards of one non-trump suit, may be played when answering any lead;
//   it becomes the winning set and everyone who already played tops up to 5 cards.
// - Bura: 5 trumps in hand, shown on your turn, wins the round at its current value.
// - Raises davi x2 ... shashi x6: proposed by the leader before leading, both partners must agree,
//   then the other team answers (accept, raise again, give up). If the answering partners
//   disagree, the raise counts as accepted.
// - 61+ of 120 points wins the round, 60-60 is a tie. No penalties. Match to 11.
// - First round: a random card picks the dealing team (red = team 0, black = team 1).
//   Later rounds: a player of the team that won the last round deals.

import { Card, Suit, SUITS, makeCard, rng, shuffle } from "../engine/cards";
import { RuleError } from "../engine/game";

export const BURA_TARGET = 11;
export const MAX_STAKE = 6;
const ROUND_PAUSE_MS = 6000;

export const CARD_POINTS: Record<number, number> = { 14: 11, 10: 10, 13: 4, 12: 3, 11: 2 };
/** Strength for beating: A > 10 > K > Q > J > 9 > 8 > 7 > 6. */
const STRENGTH: Record<number, number> = { 14: 9, 10: 8, 13: 7, 12: 6, 11: 5, 9: 4, 8: 3, 7: 2, 6: 1 };

export class BuraError extends RuleError {}

export function points(c: Card): number {
  return CARD_POINTS[c.rank] ?? 0;
}
export function strength(c: Card): number {
  return STRENGTH[c.rank];
}
export function teamOf(seat: number): number {
  return seat % 2;
}
export function partnerOf(seat: number): number {
  return (seat + 2) % 4;
}
function nextSeat(seat: number): number {
  return (seat + 1) % 4;
}

export function buraDeck(): Card[] {
  const deck: Card[] = [];
  for (const suit of SUITS) for (let rank = 6; rank <= 14; rank++) deck.push(makeCard(rank, suit));
  return deck;
}

export interface TrickPlay {
  seat: number;
  cards: Card[];
  /** Per card: shown face up (beat the winning set or led). Face-down cards are hidden from others. */
  up: boolean[];
}

export interface Trick {
  leader: number;
  /** Cards each player plays (5 after a malyutka). */
  size: number;
  plays: TrickPlay[];
  /** Seat holding the winning set right now. */
  best: number;
  /** Seat that played a malyutka in this trick. */
  malyutka: number | null;
  /** Seats that must still add cards after a malyutka, in order. */
  topUps: number[];
}

export type RaiseStage = "partner" | "answer" | "answerPartner";

export interface Raise {
  /** Seat that proposed the raise. */
  by: number;
  /** The leader whose turn it is (play resumes with them). */
  leader: number;
  /** Stake if accepted. */
  to: number;
  stage: RaiseStage;
  /** Answering player (stage answer / answerPartner). */
  answerer: number | null;
  answer: "raise" | "fold" | null;
}

export interface RoundResult {
  dealer: number;
  trump: Suit;
  /** Card points per team (null when the round ended early). */
  points: [number, number] | null;
  /** Winning team, or null for a tie. */
  winner: number | null;
  /** Match points won. */
  value: number;
  stake: number;
  how: "points" | "tie" | "bura" | "fold";
  /** Seat that showed a bura, or that gave up. */
  seat: number | null;
  /** The bura hand, shown to everyone. */
  bura: Card[] | null;
  match: [number, number];
}

export type BuraPhase = "playing" | "roundOver" | "gameOver";

export interface BuraState {
  kind: "bura";
  seed: number;
  round: number;
  target: number;
  match: [number, number];
  /** First round only: the card that chose the dealing team. */
  cut: Card | null;
  dealer: number;
  phase: BuraPhase;
  trump: Suit;
  trumpCard: Card;
  stock: Card[];
  hands: Card[][];
  piles: [Card[], Card[]];
  trick: Trick | null;
  lastTrick: { plays: TrickPlay[]; winner: number } | null;
  turn: number;
  stake: number;
  /** Team that made the last accepted raise; the other team raises next. */
  lastRaiseTeam: number | null;
  raise: Raise | null;
  /** No raises this round (a team is one point from winning). */
  crawford: boolean;
  crawfordUsed: boolean;
  rounds: RoundResult[];
  /** Counts every action, so the room can tell turns apart. */
  step: number;
}

export type BuraAction =
  | { t: "lead"; cards: string[] }
  | { t: "answer"; cards: string[] }
  | { t: "topUp"; cards: string[] }
  | { t: "bura" }
  | { t: "raise" }
  | { t: "approve"; yes: boolean }
  | { t: "respond"; answer: "accept" | "raise" | "fold" }
  | { t: "next" };

// ---------- Card logic ----------

/** Does card b beat card a? */
export function cardBeats(b: Card, a: Card, trump: Suit): boolean {
  if (b.suit === a.suit) return strength(b) > strength(a);
  return b.suit === trump && a.suit !== trump;
}

/** Does set `by` beat every card of `target` one-for-one? (Same size; small, so brute force.) */
export function setBeats(by: Card[], target: Card[], trump: Suit): boolean {
  if (by.length !== target.length) return false;
  const used = new Array(by.length).fill(false);
  // Hardest targets first prunes the search.
  const order = target.slice().sort((x, y) => (y.suit === trump ? 10 : 0) + strength(y) - ((x.suit === trump ? 10 : 0) + strength(x)));
  const go = (i: number): boolean => {
    if (i === order.length) return true;
    for (let j = 0; j < by.length; j++) {
      if (!used[j] && cardBeats(by[j], order[i], trump)) {
        used[j] = true;
        if (go(i + 1)) return true;
        used[j] = false;
      }
    }
    return false;
  };
  return go(0);
}

export function isBura(hand: Card[], trump: Suit): boolean {
  return hand.length === 5 && hand.every((c) => c.suit === trump);
}

export function isMalyutka(hand: Card[], trump: Suit): boolean {
  return hand.length === 5 && hand[0].suit !== trump && hand.every((c) => c.suit === hand[0].suit);
}

export function sortBuraHand(hand: Card[], trump: Suit): Card[] {
  const order: Record<Suit, number> = { S: 0, H: 1, C: 2, D: 3 };
  return hand.slice().sort((a, b) => {
    if (a.suit !== b.suit) {
      if (a.suit === trump) return -1;
      if (b.suit === trump) return 1;
      return order[a.suit!] - order[b.suit!];
    }
    return strength(b) - strength(a);
  });
}

/** The set currently winning the trick. */
export function bestSet(t: Trick): Card[] {
  return t.plays.find((p) => p.seat === t.best)!.cards;
}

export function pilePoints(pile: Card[]): number {
  return pile.reduce((s, c) => s + points(c), 0);
}

// ---------- Setup ----------

export function newBura(seed = Date.now()): BuraState {
  const rand = rng(seed);
  const cut = shuffle(buraDeck(), rand)[0];
  const red = cut.suit === "H" || cut.suit === "D";
  const s = {
    kind: "bura",
    seed,
    round: 0,
    target: BURA_TARGET,
    match: [0, 0],
    cut,
    dealer: red ? 0 : 1,
    crawfordUsed: false,
    rounds: [],
    step: 0,
  } as unknown as BuraState;
  return deal(s);
}

function deal(s: BuraState): BuraState {
  const deck = shuffle(buraDeck(), rng(s.seed + (s.round + 1) * 7919));
  const hands: Card[][] = [[], [], [], []];
  let seat = nextSeat(s.dealer);
  let k = 0;
  for (let i = 0; i < 20; i++) {
    hands[seat].push(deck[k++]);
    seat = nextSeat(seat);
  }
  s.trumpCard = deck[k++];
  s.trump = s.trumpCard.suit!;
  // The trump card lies face up under the stock and is drawn last.
  s.stock = [...deck.slice(k), s.trumpCard];
  s.hands = hands.map((h) => sortBuraHand(h, s.trump));
  s.piles = [[], []];
  s.trick = null;
  s.lastTrick = null;
  s.turn = nextSeat(s.dealer);
  s.stake = 1;
  s.lastRaiseTeam = null;
  s.raise = null;
  s.phase = "playing";
  return s;
}

// ---------- Queries ----------

/** Who must act now (null while the round result is shown or the game is over). */
export function buraActor(s: BuraState): number | null {
  return s.phase === "playing" ? s.turn : null;
}

/** What the player to act needs to do. */
export function buraNeed(s: BuraState): "lead" | "answer" | "topUp" | "approve" | "respond" | null {
  if (s.phase !== "playing") return null;
  if (s.raise) return s.raise.stage === "answer" ? "respond" : "approve";
  if (!s.trick) return "lead";
  if (s.trick.topUps.length && s.trick.plays.length === 4) return "topUp";
  return "answer";
}

export function canRaise(s: BuraState, seat: number): boolean {
  return (
    s.phase === "playing" &&
    s.turn === seat &&
    !s.raise &&
    !s.trick &&
    !s.crawford &&
    s.stake < MAX_STAKE &&
    s.lastRaiseTeam !== teamOf(seat)
  );
}

export function canDeclareBura(s: BuraState, seat: number): boolean {
  return s.phase === "playing" && s.turn === seat && !s.raise && isBura(s.hands[seat], s.trump);
}

/** Cards the seat must play now (0 = none). */
export function cardsNeeded(s: BuraState, seat: number): number {
  if (s.phase !== "playing" || s.turn !== seat || s.raise) return 0;
  const t = s.trick;
  if (!t) return 0;
  const mine = t.plays.find((p) => p.seat === seat);
  if (mine) return t.size - mine.cards.length;
  return t.size;
}

export function canMalyutka(s: BuraState, seat: number): boolean {
  const t = s.trick;
  return (
    s.phase === "playing" &&
    s.turn === seat &&
    !s.raise &&
    !!t &&
    t.size < 5 &&
    t.malyutka === null &&
    !t.plays.some((p) => p.seat === seat) &&
    isMalyutka(s.hands[seat], s.trump)
  );
}

// ---------- Actions ----------

function take(hand: Card[], ids: string[]): { cards: Card[]; rest: Card[] } {
  if (new Set(ids).size !== ids.length) throw new BuraError("Pick different cards");
  const cards: Card[] = [];
  for (const id of ids) {
    const c = hand.find((x) => x.id === id);
    if (!c) throw new BuraError("You don't have that card");
    cards.push(c);
  }
  return { cards, rest: hand.filter((c) => !ids.includes(c.id)) };
}

export function applyBura(state: BuraState, seat: number, a: BuraAction): BuraState {
  const s = structuredClone(state);
  if (a.t === "next") {
    if (s.phase !== "roundOver") throw new BuraError("Nothing to continue");
    s.round += 1;
    s.step += 1;
    return deal(s);
  }
  if (s.phase !== "playing") throw new BuraError("The round is over");
  if (s.turn !== seat) throw new BuraError("Not your turn");
  s.step += 1;
  switch (a.t) {
    case "bura": {
      if (!canDeclareBura(s, seat)) throw new BuraError("You don't have a bura");
      return endRound(s, teamOf(seat), "bura", seat);
    }
    case "raise": {
      if (!canRaise(s, seat)) throw new BuraError("You can't raise now");
      s.raise = { by: seat, leader: seat, to: s.stake + 1, stage: "partner", answerer: null, answer: null };
      s.turn = partnerOf(seat);
      return s;
    }
    case "approve": {
      const r = s.raise;
      if (!r || r.stage === "answer") throw new BuraError("Nothing to approve");
      const yes = Boolean(a.yes);
      if (r.stage === "partner") {
        if (!yes) {
          s.raise = null;
          s.turn = r.leader;
          return s;
        }
        r.stage = "answer";
        r.answerer = nextSeat(r.by);
        s.turn = r.answerer;
        return s;
      }
      // answerPartner: both answering partners agreed to raise again or to give up.
      if (!yes) return acceptRaise(s);
      if (r.answer === "fold") return endRound(s, teamOf(r.by), "fold", r.answerer);
      // Raise again: the first raise is accepted and the other team must answer the new one.
      s.stake = r.to;
      s.lastRaiseTeam = teamOf(r.by);
      const by = r.answerer!;
      s.raise = { by, leader: r.leader, to: s.stake + 1, stage: "answer", answerer: nextSeat(by), answer: null };
      s.turn = nextSeat(by);
      return s;
    }
    case "respond": {
      const r = s.raise;
      if (!r || r.stage !== "answer") throw new BuraError("Nothing to answer");
      if (a.answer === "accept") return acceptRaise(s);
      if (a.answer === "raise") {
        if (r.to >= MAX_STAKE || s.crawford) throw new BuraError("You can't raise higher");
      } else if (a.answer !== "fold") throw new BuraError("Bad answer");
      r.answer = a.answer;
      r.stage = "answerPartner";
      s.turn = partnerOf(r.answerer!);
      return s;
    }
    case "lead": {
      if (s.raise || s.trick) throw new BuraError("You can't lead now");
      const hand = s.hands[seat];
      const n = a.cards?.length ?? 0;
      if (n < 1 || n > 5 || n > hand.length) throw new BuraError("Lead 1 to 5 cards");
      const { cards, rest } = take(hand, a.cards);
      if (!cards.every((c) => c.suit === cards[0].suit)) throw new BuraError("Lead cards of one suit");
      s.hands[seat] = rest;
      s.trick = { leader: seat, size: n, plays: [{ seat, cards, up: cards.map(() => true) }], best: seat, malyutka: null, topUps: [] };
      return advance(s);
    }
    case "answer": {
      const t = s.trick;
      if (!t || s.raise || t.plays.some((p) => p.seat === seat)) throw new BuraError("You can't play now");
      const hand = s.hands[seat];
      const malyutka = a.cards?.length === 5 && t.size < 5;
      if (malyutka) {
        if (!canMalyutka(s, seat)) throw new BuraError("That is not a malyutka");
        const { cards } = take(hand, a.cards);
        s.hands[seat] = [];
        t.plays.push({ seat, cards, up: cards.map(() => true) });
        t.topUps = t.plays.filter((p) => p.seat !== seat).map((p) => p.seat);
        t.size = 5;
        t.best = seat;
        t.malyutka = seat;
        return advance(s);
      }
      if (a.cards?.length !== t.size) throw new BuraError(`Play ${t.size} card${t.size > 1 ? "s" : ""}`);
      const { cards, rest } = take(hand, a.cards);
      s.hands[seat] = rest;
      const beats = setBeats(cards, bestSet(t), s.trump);
      t.plays.push({ seat, cards, up: cards.map(() => beats) });
      if (beats) t.best = seat;
      return advance(s);
    }
    case "topUp": {
      const t = s.trick;
      if (!t || buraNeed(s) !== "topUp" || t.topUps[0] !== seat) throw new BuraError("You can't add cards now");
      const mine = t.plays.find((p) => p.seat === seat)!;
      const need = t.size - mine.cards.length;
      if (a.cards?.length !== need) throw new BuraError(`Add ${need} card${need > 1 ? "s" : ""}`);
      const { cards, rest } = take(s.hands[seat], a.cards);
      s.hands[seat] = rest;
      const all = [...mine.cards, ...cards];
      const beats = setBeats(all, bestSet(t), s.trump);
      mine.cards = all;
      mine.up = beats ? all.map(() => true) : [...mine.up, ...cards.map(() => false)];
      if (beats) t.best = seat;
      t.topUps.shift();
      return advance(s);
    }
    default:
      throw new BuraError("Unknown action");
  }
}

function acceptRaise(s: BuraState): BuraState {
  const r = s.raise!;
  s.stake = r.to;
  s.lastRaiseTeam = teamOf(r.by);
  s.raise = null;
  s.turn = r.leader;
  return s;
}

/** Moves the turn on after a play; resolves the trick when everyone is done. */
function advance(s: BuraState): BuraState {
  const t = s.trick!;
  if (t.plays.length < 4) {
    s.turn = nextSeat(t.plays[t.plays.length - 1].seat);
    return s;
  }
  if (t.topUps.length) {
    s.turn = t.topUps[0];
    return s;
  }
  // Trick done: the winning set's team takes every card.
  const winner = t.best;
  const plays = t.plays.slice().sort((a, b) => ((a.seat - t.leader + 4) % 4) - ((b.seat - t.leader + 4) % 4));
  for (const p of plays) s.piles[teamOf(winner)].push(...p.cards);
  s.lastTrick = { plays, winner };
  s.trick = null;
  // Refill from the stock, one card at a time from the winner clockwise.
  let seat = winner;
  let guard = 0;
  while (s.stock.length && s.hands.some((h) => h.length < 5) && guard++ < 40) {
    if (s.hands[seat].length < 5) s.hands[seat].push(s.stock.shift()!);
    seat = nextSeat(seat);
  }
  s.hands = s.hands.map((h) => sortBuraHand(h, s.trump));
  if (s.hands.every((h) => h.length === 0)) return endRound(s, null, "points", null);
  s.turn = winner;
  return s;
}

function endRound(s: BuraState, team: number | null, how: RoundResult["how"], seat: number | null): BuraState {
  let winner = team;
  let value = 0;
  let pts: [number, number] | null = null;
  if (how === "points") {
    pts = [pilePoints(s.piles[0]), pilePoints(s.piles[1])];
    winner = pts[0] > 60 ? 0 : pts[1] > 60 ? 1 : null;
    value = winner === null ? 0 : s.stake;
  } else if (how === "bura") {
    value = s.stake;
  } else {
    // Gave up: the raising team scores the value before the raise.
    value = s.stake;
  }
  if (winner !== null) s.match[winner] += value;
  s.rounds.push({
    dealer: s.dealer,
    trump: s.trump,
    points: pts,
    winner: winner === null ? null : winner,
    value,
    stake: s.stake,
    how: how === "points" && winner === null ? "tie" : how,
    seat,
    bura: how === "bura" && seat !== null ? s.hands[seat].slice() : null,
    match: [s.match[0], s.match[1]],
  });
  s.raise = null;
  s.trick = null;
  if (s.match[0] >= s.target || s.match[1] >= s.target) {
    s.phase = "gameOver";
    return s;
  }
  // Crawford: the round after a team first reaches target - 1 is played without raises.
  s.crawford = false;
  if (!s.crawfordUsed && (s.match[0] === s.target - 1 || s.match[1] === s.target - 1)) {
    s.crawford = true;
    s.crawfordUsed = true;
  }
  // Next dealer: a player of the winning team (partners take turns); a tie passes the deal on.
  if (winner === null) s.dealer = nextSeat(s.dealer);
  else s.dealer = teamOf(s.dealer) === winner ? partnerOf(s.dealer) : nextSeat(s.dealer);
  s.phase = "roundOver";
  return s;
}

/** Pause before the next round is dealt (ms), or null when nothing happens by itself. */
export function buraAutoDelay(s: BuraState): number | null {
  return s.phase === "roundOver" ? ROUND_PAUSE_MS : null;
}

// ---------- What a player may see ----------

export function buraView(s: BuraState, seat: number | null) {
  const hide = (p: TrickPlay) => ({
    seat: p.seat,
    cards: p.cards.map((c, i) => (p.up[i] || p.seat === seat ? c : null)),
    up: p.up,
  });
  const t = s.trick;
  const mine = seat !== null && s.turn === seat && s.phase === "playing";
  return {
    kind: "bura" as const,
    phase: s.phase,
    round: s.round,
    target: s.target,
    match: s.match,
    cut: s.round === 0 ? s.cut : null,
    dealer: s.dealer,
    turn: s.turn,
    trump: s.trump,
    /** Visible while it lies under the stock. */
    trumpCard: s.stock.length ? s.trumpCard : null,
    stockCount: s.stock.length,
    handCounts: s.hands.map((h) => h.length),
    hand: seat !== null ? s.hands[seat] : [],
    pileCounts: [s.piles[0].length, s.piles[1].length],
    trick: t
      ? { leader: t.leader, size: t.size, best: t.best, malyutka: t.malyutka, topUps: t.topUps, plays: t.plays.map(hide) }
      : null,
    lastTrick: s.lastTrick ? { winner: s.lastTrick.winner, plays: s.lastTrick.plays.map(hide) } : null,
    stake: s.stake,
    lastRaiseTeam: s.lastRaiseTeam,
    raise: s.raise,
    crawford: s.crawford,
    rounds: s.rounds,
    need: mine ? buraNeed(s) : null,
    cardsNeeded: seat !== null ? cardsNeeded(s, seat) : 0,
    canRaise: seat !== null && canRaise(s, seat),
    canBura: seat !== null && canDeclareBura(s, seat),
    canMalyutka: seat !== null && canMalyutka(s, seat),
    canRaiseAgain: !!s.raise && s.raise.to < MAX_STAKE && !s.crawford,
  };
}
