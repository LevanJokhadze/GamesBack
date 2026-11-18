// A simple Bura bot: keeps its trumps and points, beats when it is worth it,
// and feeds points to its partner when the partner is winning.

import { Card, Suit } from "../engine/cards";
import {
  BuraAction,
  BuraState,
  bestSet,
  buraNeed,
  canDeclareBura,
  canMalyutka,
  canRaise,
  partnerOf,
  points,
  setBeats,
  strength,
  teamOf,
} from "./engine";

/** How much a card is worth keeping: points, plus extra for trumps. */
function keep(c: Card, trump: Suit): number {
  return points(c) + (c.suit === trump ? 12 + strength(c) : strength(c) / 4);
}

function subsets<T>(arr: T[], k: number): T[][] {
  const out: T[][] = [];
  const pick = (start: number, acc: T[]) => {
    if (acc.length === k) {
      out.push(acc.slice());
      return;
    }
    for (let i = start; i < arr.length; i++) {
      acc.push(arr[i]);
      pick(i + 1, acc);
      acc.pop();
    }
  };
  pick(0, []);
  return out;
}

const cost = (cards: Card[], trump: Suit) => cards.reduce((s, c) => s + keep(c, trump), 0);
const ids = (cards: Card[]) => cards.map((c) => c.id);

/** The cheapest k cards to throw away. */
function cheapest(hand: Card[], k: number, trump: Suit): Card[] {
  return hand.slice().sort((a, b) => keep(a, trump) - keep(b, trump)).slice(0, k);
}

/** The k highest-point non-trump cards (to give the partner), else cheapest. */
function feed(hand: Card[], k: number, trump: Suit): Card[] {
  return hand
    .slice()
    .sort((a, b) => {
      const ta = a.suit === trump ? 1 : 0;
      const tb = b.suit === trump ? 1 : 0;
      if (ta !== tb) return ta - tb;
      return points(b) - points(a);
    })
    .slice(0, k);
}

function trumpCount(hand: Card[], trump: Suit): number {
  return hand.filter((c) => c.suit === trump).length;
}

export function buraBot(s: BuraState, seat: number): BuraAction {
  const hand = s.hands[seat];
  const trump = s.trump;
  if (canDeclareBura(s, seat)) return { t: "bura" };
  const need = buraNeed(s);
  if (need === "approve") return { t: "approve", yes: s.raise!.stage === "partner" ? true : trumpCount(hand, trump) >= 2 };
  if (need === "respond") {
    const strong = trumpCount(hand, trump) + hand.filter((c) => c.rank === 14).length;
    if (strong >= 4 && s.raise!.to < 6 && !s.crawford) return { t: "respond", answer: "raise" };
    if (strong === 0 && s.raise!.to >= 4) return { t: "respond", answer: "fold" };
    return { t: "respond", answer: "accept" };
  }
  if (need === "lead") {
    if (canRaise(s, seat) && trumpCount(hand, trump) >= 4) return { t: "raise" };
    // Several zero-point cards of one plain suit go out together; otherwise the cheapest card.
    const bySuit = new Map<string, Card[]>();
    for (const c of hand) if (c.suit !== trump && points(c) === 0) bySuit.set(c.suit!, [...(bySuit.get(c.suit!) ?? []), c]);
    const group = [...bySuit.values()].sort((a, b) => b.length - a.length)[0];
    if (group && group.length >= 2) return { t: "lead", cards: ids(group) };
    return { t: "lead", cards: ids(cheapest(hand, 1, trump)) };
  }
  const t = s.trick!;
  if (need === "answer" && canMalyutka(s, seat) && teamOf(t.best) !== teamOf(seat)) return { t: "answer", cards: ids(hand) };
  const mine = t.plays.find((p) => p.seat === seat);
  const have = mine ? mine.cards : [];
  const k = t.size - have.length;
  const target = bestSet(t);
  const kind = need === "topUp" ? "topUp" : "answer";
  const partnerWinning = t.best === partnerOf(seat) || t.best === seat;
  const last = kind === "answer" ? t.plays.length === 3 && t.topUps.length === 0 : t.topUps.length === 1;
  if (partnerWinning) {
    // Partner holds the trick: give points if nobody can take it back, else throw cheap cards.
    const chosen = last ? feed(hand, k, trump) : cheapest(hand, k, trump);
    return { t: kind, cards: ids(chosen) };
  }
  const onTable = t.plays.reduce((sum, p) => sum + p.cards.reduce((a, c) => a + points(c), 0), 0);
  let best: Card[] | null = null;
  for (const sub of subsets(hand, k)) {
    if (!setBeats([...have, ...sub], target, trump)) continue;
    if (!best || cost(sub, trump) < cost(best, trump)) best = sub;
  }
  if (best) {
    const usesTrump = best.some((c) => c.suit === trump);
    if (!usesTrump || onTable >= 10 || k >= 2 || s.stock.length === 0) return { t: kind, cards: ids(best) };
  }
  return { t: kind, cards: ids(cheapest(hand, k, trump)) };
}
