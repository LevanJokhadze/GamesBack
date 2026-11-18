// A simple heuristic bot. It tries to take exactly as many tricks as it bid.

import { Card, Suit, SUITS } from "./cards";
import {
  GameState,
  JokerMode,
  Play,
  forbiddenBid,
  legalBids,
  legalCards,
  trickWinner,
} from "./game";

export type BotAction =
  | { t: "trump"; suit: Suit | null }
  | { t: "bid"; bid: number }
  | { t: "play"; cardId: string; joker?: JokerMode };

function cardStrength(c: Card, trump: Suit | null, handSize: number): number {
  if (c.joker) return 1;
  const isTrump = trump !== null && c.suit === trump;
  if (isTrump) {
    if (c.rank >= 13) return 0.95;
    if (c.rank >= 11) return 0.65;
    return handSize >= 6 ? 0.35 : 0.2;
  }
  if (c.rank === 14) return handSize <= 3 ? 0.85 : 0.75;
  if (c.rank === 13) return handSize <= 3 ? 0.45 : 0.35;
  if (c.rank === 12 && handSize >= 7) return 0.1;
  return 0;
}

export function chooseBotTrump(hand: Card[]): Suit | null {
  const visible = hand.slice(0, 3);
  let best: Suit | null = null;
  let bestScore = 0;
  for (const suit of SUITS) {
    const cards = visible.filter((c) => c.suit === suit);
    const score = cards.length * 10 + cards.reduce((a, c) => a + c.rank, 0);
    if (cards.length >= 2 || (cards.length === 1 && cards[0].rank >= 13)) {
      if (score > bestScore) {
        bestScore = score;
        best = suit;
      }
    }
  }
  return best;
}

export function chooseBotBid(s: GameState, seat: number): number {
  const hand = s.hands[seat];
  let est = hand.reduce((a, c) => a + cardStrength(c, s.trump, s.handSize), 0);
  // Long trump suits make extra tricks.
  if (s.trump) {
    const trumps = hand.filter((c) => c.suit === s.trump).length;
    if (trumps >= 4) est += (trumps - 3) * 0.5;
  }
  let bid = Math.max(0, Math.min(s.handSize, Math.round(est * 0.8)));
  const legal = legalBids(s, seat);
  if (!legal.includes(bid)) {
    const f = forbiddenBid(s, seat);
    // Move toward the estimate.
    bid = f !== null && est > f ? Math.min(s.handSize, f + 1) : Math.max(0, (f ?? 1) - 1);
    if (!legal.includes(bid)) bid = legal[0];
  }
  return bid;
}

function wouldWin(s: GameState, seat: number, card: Card, joker?: JokerMode): boolean {
  const plays: Play[] = [...s.trick, { seat, card, joker }];
  // Pretend remaining players play nothing that matters: only check against the cards on the table.
  return trickWinner(plays, s.trump) === seat;
}

function nonJokerValue(c: Card, trump: Suit | null): number {
  if (c.joker) return 1000;
  return (trump && c.suit === trump ? 100 : 0) + c.rank;
}

export function chooseBotPlay(s: GameState, seat: number): { cardId: string; joker?: JokerMode } {
  const legal = legalCards(s, seat);
  const bid = s.bids[seat] ?? 0;
  const need = bid - s.took[seat];
  const wantTricks = need > 0;
  const normals = legal.filter((c) => !c.joker).sort((a, b) => nonJokerValue(a, s.trump) - nonJokerValue(b, s.trump));
  const joker = legal.find((c) => c.joker);
  const leading = s.trick.length === 0;
  const lastToPlay = s.trick.length === 3;

  if (leading) {
    if (wantTricks) {
      // Lead the strongest card; use the joker to pull trumps when it is likely needed.
      const tricksLeft = s.hands[seat].length;
      if (joker && (need >= tricksLeft || normals.length === 0 || need >= 2)) {
        const suit = pickJokerSuit(s, seat, true);
        return { cardId: joker.id, joker: { type: "high", suit } };
      }
      const aces = normals.filter((c) => c.rank === 14 || (s.trump && c.suit === s.trump && c.rank >= 13));
      if (aces.length) return { cardId: aces[aces.length - 1].id };
      if (normals.length) return { cardId: normals[normals.length - 1].id };
    } else {
      if (normals.length) return { cardId: normals[0].id };
      if (joker) return { cardId: joker.id, joker: { type: "low", suit: pickJokerSuit(s, seat, false) } };
    }
    return { cardId: legal[0].id };
  }

  if (wantTricks) {
    // Cheapest normal card that is currently winning (only trust it if we play last or it is very strong).
    const winners = normals.filter((c) => wouldWin(s, seat, c));
    const safe = lastToPlay ? winners : winners.filter((c) => c.rank >= 13 || (s.trump && c.suit === s.trump && c.rank >= 12));
    if (safe.length) return { cardId: safe[0].id };
    if (joker) return { cardId: joker.id, joker: { type: "high" } };
    if (winners.length) return { cardId: winners[0].id };
    if (normals.length) return { cardId: normals[0].id };
  } else {
    // Highest card that still loses; otherwise dump the joker low or the smallest card.
    const losers = normals.filter((c) => !wouldWin(s, seat, c));
    if (losers.length) return { cardId: losers[losers.length - 1].id };
    if (joker) return { cardId: joker.id, joker: { type: "low" } };
    if (normals.length) return { cardId: normals[0].id };
  }
  const c = legal[0];
  return c.joker ? { cardId: c.id, joker: { type: wantTricks ? "high" : "low" } } : { cardId: c.id };
}

function pickJokerSuit(s: GameState, seat: number, high: boolean): Suit {
  const hand = s.hands[seat].filter((c) => !c.joker);
  if (high) {
    // Pull trumps if we hold none, otherwise ask for a suit where we are short.
    if (s.trump && !hand.some((c) => c.suit === s.trump)) return s.trump;
    const counts = SUITS.map((suit) => ({ suit, n: hand.filter((c) => c.suit === suit).length }));
    counts.sort((a, b) => a.n - b.n);
    return counts.find((c) => c.suit !== s.trump)?.suit ?? counts[0].suit;
  }
  const counts = SUITS.filter((x) => x !== s.trump).map((suit) => ({ suit, n: hand.filter((c) => c.suit === suit).length }));
  counts.sort((a, b) => a.n - b.n);
  return counts[0]?.suit ?? "H";
}

export function botAction(s: GameState, seat: number): BotAction {
  if (s.phase === "trump") return { t: "trump", suit: chooseBotTrump(s.hands[seat]) };
  if (s.phase === "bidding") return { t: "bid", bid: chooseBotBid(s, seat) };
  return { t: "play", ...chooseBotPlay(s, seat) };
}
