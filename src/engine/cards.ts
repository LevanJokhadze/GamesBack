// Cards and deck for Joker.
// 36 cards: ranks 6..A in four suits, except the 6 of spades and 6 of clubs,
// which are replaced by two jokers.

export type Suit = "S" | "H" | "D" | "C";
export const SUITS: Suit[] = ["S", "H", "D", "C"];

export interface Card {
  id: string; // e.g. "14H" (ace of hearts), "10S", "J1"
  suit: Suit | null; // null for jokers
  rank: number; // 6..14, jokers have 0
  joker: boolean;
}

export const RANK_LABEL: Record<number, string> = {
  6: "6",
  7: "7",
  8: "8",
  9: "9",
  10: "10",
  11: "J",
  12: "Q",
  13: "K",
  14: "A",
};

export function makeCard(rank: number, suit: Suit): Card {
  return { id: `${rank}${suit}`, suit, rank, joker: false };
}

export function makeJoker(n: 1 | 2): Card {
  return { id: `J${n}`, suit: null, rank: 0, joker: true };
}

export function fullDeck(): Card[] {
  const deck: Card[] = [];
  for (const suit of SUITS) {
    for (let rank = 6; rank <= 14; rank++) {
      if (rank === 6 && (suit === "S" || suit === "C")) continue;
      deck.push(makeCard(rank, suit));
    }
  }
  deck.push(makeJoker(1), makeJoker(2));
  return deck;
}

export function cardFromId(id: string): Card {
  if (id === "J1" || id === "J2") return makeJoker(id === "J1" ? 1 : 2);
  const suit = id.slice(-1) as Suit;
  const rank = Number(id.slice(0, -1));
  if (!SUITS.includes(suit) || !(rank >= 6 && rank <= 14)) {
    throw new Error(`Bad card id ${id}`);
  }
  return makeCard(rank, suit);
}

// Small seeded PRNG (mulberry32) so games are reproducible in tests.
export function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function shuffle<T>(arr: T[], rand: () => number): T[] {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rand() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

const SUIT_ORDER: Record<Suit, number> = { S: 0, H: 1, C: 2, D: 3 };

/** Sort a hand for display: jokers first, then by suit (trump first), then rank descending. */
export function sortHand(hand: Card[], trump: Suit | null): Card[] {
  return hand.slice().sort((a, b) => {
    if (a.joker !== b.joker) return a.joker ? -1 : 1;
    if (a.joker && b.joker) return a.id.localeCompare(b.id);
    if (a.suit !== b.suit) {
      if (a.suit === trump) return -1;
      if (b.suit === trump) return 1;
      return SUIT_ORDER[a.suit!] - SUIT_ORDER[b.suit!];
    }
    return b.rank - a.rank;
  });
}
