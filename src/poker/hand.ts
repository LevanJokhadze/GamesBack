// Texas Hold'em hand ranking: the best 5 of 7 cards.

import { Card, SUITS, Suit, makeCard } from "../engine/cards";

/** 0 high card, 1 pair, 2 two pair, 3 three of a kind, 4 straight, 5 flush, 6 full house, 7 four of a kind, 8 straight flush. */
export type HandCategory = 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8;

export interface HandRank {
  /** Compare with `>`: higher wins, equal splits. */
  score: number;
  cat: HandCategory;
  /** The five cards that make the hand. */
  best: Card[];
}

/** 52 cards, 2..A. */
export function pokerDeck(): Card[] {
  const deck: Card[] = [];
  for (const suit of SUITS) for (let rank = 2; rank <= 14; rank++) deck.push(makeCard(rank, suit));
  return deck;
}

function pack(cat: number, ranks: number[]): number {
  let s = cat;
  for (let i = 0; i < 5; i++) s = s * 16 + (ranks[i] ?? 0);
  return s;
}

/** Score of exactly five cards. */
export function rank5(cards: Card[]): { score: number; cat: HandCategory } {
  const ranks = cards.map((c) => c.rank).sort((a, b) => b - a);
  const flush = cards.every((c) => c.suit === cards[0].suit);
  const uniq = [...new Set(ranks)];
  let straightHigh = 0;
  if (uniq.length === 5) {
    if (ranks[0] - ranks[4] === 4) straightHigh = ranks[0];
    else if (ranks[0] === 14 && ranks[1] === 5) straightHigh = 5; // A-2-3-4-5
  }
  if (straightHigh && flush) return { score: pack(8, [straightHigh]), cat: 8 };
  const counts = new Map<number, number>();
  for (const r of ranks) counts.set(r, (counts.get(r) ?? 0) + 1);
  // Groups by size, then by rank.
  const groups = [...counts.entries()].sort((a, b) => b[1] - a[1] || b[0] - a[0]);
  const order = groups.map((g) => g[0]);
  if (groups[0][1] === 4) return { score: pack(7, order), cat: 7 };
  if (groups[0][1] === 3 && groups[1][1] === 2) return { score: pack(6, order), cat: 6 };
  if (flush) return { score: pack(5, ranks), cat: 5 };
  if (straightHigh) return { score: pack(4, [straightHigh]), cat: 4 };
  if (groups[0][1] === 3) return { score: pack(3, order), cat: 3 };
  if (groups[0][1] === 2 && groups[1][1] === 2) return { score: pack(2, order), cat: 2 };
  if (groups[0][1] === 2) return { score: pack(1, order), cat: 1 };
  return { score: pack(0, ranks), cat: 0 };
}

const COMBOS: Record<number, number[][]> = {};
function combos(n: number): number[][] {
  if (COMBOS[n]) return COMBOS[n];
  const out: number[][] = [];
  const pick = (from: number, acc: number[]) => {
    if (acc.length === 5) return void out.push(acc.slice());
    for (let i = from; i < n; i++) {
      acc.push(i);
      pick(i + 1, acc);
      acc.pop();
    }
  };
  pick(0, []);
  return (COMBOS[n] = out);
}

/** Best five of five to seven cards. */
export function bestHand(cards: Card[]): HandRank {
  if (cards.length < 5) throw new Error("need 5 cards");
  let best: HandRank | null = null;
  for (const idx of combos(cards.length)) {
    const pick = idx.map((i) => cards[i]);
    const r = rank5(pick);
    if (!best || r.score > best.score) best = { ...r, best: pick };
  }
  return best!;
}

/** Score only (bots). */
export function scoreOf(cards: Card[]): number {
  let best = -1;
  for (const idx of combos(cards.length)) {
    const s = rank5(idx.map((i) => cards[i])).score;
    if (s > best) best = s;
  }
  return best;
}

export function suitOf(c: Card): Suit {
  return c.suit!;
}
