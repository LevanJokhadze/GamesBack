import { describe, expect, it } from "vitest";
import {
  applyAction,
  botAction,
  cardFromId,
  dealPoints,
  forbiddenBid,
  fullDeck,
  GameState,
  legalCards,
  newGame,
  placeBid,
  trickWinner,
} from "../src/engine";

const c = cardFromId;

describe("deck", () => {
  it("has 36 cards with two jokers and no black sixes", () => {
    const d = fullDeck();
    expect(d).toHaveLength(36);
    expect(d.filter((x) => x.joker)).toHaveLength(2);
    expect(d.find((x) => x.id === "6S")).toBeUndefined();
    expect(d.find((x) => x.id === "6C")).toBeUndefined();
    expect(new Set(d.map((x) => x.id)).size).toBe(36);
  });
});

describe("scoring", () => {
  it("scores made bids", () => {
    expect(dealPoints(0, 0, 5, -200)).toBe(50);
    expect(dealPoints(1, 1, 5, -200)).toBe(100);
    expect(dealPoints(3, 3, 5, -200)).toBe(200);
    expect(dealPoints(9, 9, 9, -200)).toBe(900);
    expect(dealPoints(1, 1, 1, -200)).toBe(100);
  });
  it("scores missed bids and khishti", () => {
    expect(dealPoints(3, 2, 5, -200)).toBe(20);
    expect(dealPoints(0, 2, 5, -200)).toBe(20);
    expect(dealPoints(2, 0, 5, -500)).toBe(-500);
  });
});

describe("tricks", () => {
  it("highest of led suit wins without trump", () => {
    expect(trickWinner([
      { seat: 0, card: c("10H") },
      { seat: 1, card: c("14H") },
      { seat: 2, card: c("14S") },
      { seat: 3, card: c("12H") },
    ], null)).toBe(1);
  });
  it("trump beats led suit", () => {
    expect(trickWinner([
      { seat: 0, card: c("14H") },
      { seat: 1, card: c("7D") },
      { seat: 2, card: c("13H") },
      { seat: 3, card: c("8S") },
    ], "D")).toBe(1);
  });
  it("high joker wins, later high joker beats earlier", () => {
    expect(trickWinner([
      { seat: 0, card: c("J1"), joker: { type: "high", suit: "H" } },
      { seat: 1, card: c("14H") },
      { seat: 2, card: c("14D") },
      { seat: 3, card: c("8S") },
    ], "D")).toBe(0);
    expect(trickWinner([
      { seat: 0, card: c("J1"), joker: { type: "high", suit: "H" } },
      { seat: 1, card: c("14H") },
      { seat: 2, card: c("J2"), joker: { type: "high" } },
      { seat: 3, card: c("8S") },
    ], "D")).toBe(2);
  });
  it("low joker lead loses to the suit it names", () => {
    expect(trickWinner([
      { seat: 0, card: c("J1"), joker: { type: "low", suit: "H" } },
      { seat: 1, card: c("7H") },
      { seat: 2, card: c("10S") },
      { seat: 3, card: c("8H") },
    ], null)).toBe(3);
  });
  it("low joker on follow loses", () => {
    expect(trickWinner([
      { seat: 0, card: c("7H") },
      { seat: 1, card: c("J1"), joker: { type: "low" } },
      { seat: 2, card: c("6H") },
      { seat: 3, card: c("6D") },
    ], null)).toBe(0);
  });
});

function withHands(s: GameState, hands: string[][]): GameState {
  const t = structuredClone(s);
  t.hands = hands.map((h) => h.map(c));
  return t;
}

describe("legal cards", () => {
  const base = newGame({}, 1);
  it("must follow suit, else trump, joker always allowed", () => {
    let s = withHands(base, [["10H"], ["7H", "14S", "J1"], ["14D", "8S"], ["9S", "8S"]]);
    s.phase = "playing"; s.trump = "D"; s.turn = 1;
    s.trick = [{ seat: 0, card: c("10H") }];
    expect(legalCards(s, 1).map((x) => x.id).sort()).toEqual(["7H", "J1"]);
    s.turn = 2;
    expect(legalCards(s, 2).map((x) => x.id)).toEqual(["14D"]);
    s.turn = 3;
    expect(legalCards(s, 3).map((x) => x.id).sort()).toEqual(["8S", "9S"]);
  });
  it("joker that wants the highest forces the top card of that suit", () => {
    let s = withHands(base, [[], ["7H", "13H", "14S"], [], []]);
    s.phase = "playing"; s.trump = "D"; s.turn = 1;
    s.trick = [{ seat: 0, card: c("J1"), joker: { type: "high", suit: "H" } }];
    expect(legalCards(s, 1).map((x) => x.id)).toEqual(["13H"]);
  });
});

describe("bidding", () => {
  it("the dealer cannot make the total equal the hand size", () => {
    let s = newGame({}, 5);
    // First deal has 1 card and a turned-up trump.
    expect(s.handSize).toBe(1);
    expect(s.phase).toBe("bidding");
    const order = [1, 2, 3, 4].map((k) => (s.dealer + k) % 4);
    s = placeBid(s, order[0], 0);
    s = placeBid(s, order[1], 1);
    s = placeBid(s, order[2], 0);
    expect(forbiddenBid(s, s.dealer)).toBe(0);
    expect(() => placeBid(s, s.dealer, 0)).toThrow();
    s = placeBid(s, s.dealer, 1);
    expect(s.phase).toBe("playing");
  });
});

describe("full games with bots", () => {
  for (const mode of ["standard", "nines"] as const) {
    it(`plays ${mode} to the end`, () => {
      for (let seed = 1; seed <= 20; seed++) {
        let s = newGame({ mode }, seed);
        let guard = 0;
        while (s.phase !== "gameOver") {
          const seat = s.turn;
          s = applyAction(s, seat, botAction(s, seat));
          if (++guard > 5000) throw new Error("stuck");
        }
        const dealCount = s.pulkas.reduce((a, p) => a + p.deals.length, 0);
        expect(dealCount).toBe(mode === "standard" ? 24 : 16);
        for (const p of s.pulkas) for (const d of p.deals) {
          expect(d.took.reduce((a, b) => a + b, 0)).toBe(d.handSize);
          expect(d.bids.reduce((a, b) => a + b, 0)).not.toBe(d.handSize);
        }
        const sum = s.pulkas.reduce((acc, p) => acc.map((v, i) => v + p.total[i]), [0, 0, 0, 0]);
        expect(sum).toEqual(s.totals);
      }
    });
  }
});
