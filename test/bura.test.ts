import { describe, expect, it } from "vitest";
import { makeCard } from "../src/engine/cards";
import {
  BuraState,
  applyBura,
  buraActor,
  buraBot,
  buraView,
  newBura,
  pilePoints,
  setBeats,
} from "../src/bura";

const c = (id: string) => makeCard(Number(id.slice(0, -1).replace("A", "14").replace("K", "13").replace("Q", "12").replace("J", "11")), id.slice(-1) as any);

function playOut(seed: number): BuraState {
  let s = newBura(seed);
  for (let i = 0; i < 20000 && s.phase !== "gameOver"; i++) {
    if (s.phase === "roundOver") {
      s = applyBura(s, -1, { t: "next" });
      continue;
    }
    const seat = buraActor(s)!;
    s = applyBura(s, seat, buraBot(s, seat));
  }
  return s;
}

describe("bura card logic", () => {
  it("beats one-for-one: same suit higher, or trump over plain", () => {
    // Trump spades. Led 7C + 9C.
    expect(setBeats([c("8C"), c("JC")], [c("7C"), c("9C")], "S")).toBe(true);
    expect(setBeats([c("8C"), c("8H")], [c("7C"), c("9C")], "S")).toBe(false);
    expect(setBeats([c("6S"), c("8C")], [c("7C"), c("9C")], "S")).toBe(true);
    // 10 beats K; only A beats 10.
    expect(setBeats([c("10H")], [c("KH")], "S")).toBe(true);
    expect(setBeats([c("KH")], [c("10H")], "S")).toBe(false);
    expect(setBeats([c("AH")], [c("10H")], "S")).toBe(true);
    // A trump is beaten only by a higher trump.
    expect(setBeats([c("AH")], [c("6S")], "S")).toBe(false);
    expect(setBeats([c("7S")], [c("6S")], "S")).toBe(true);
  });
});

describe("bura game", () => {
  it("deals 5 each with the trump card at the bottom of the stock", () => {
    const s = newBura(42);
    expect(s.hands.every((h) => h.length === 5)).toBe(true);
    expect(s.stock.length).toBe(16);
    expect(s.stock.at(-1)).toEqual(s.trumpCard);
    // Red cut card: team 0 deals; black: team 1.
    expect(s.dealer).toBe(s.cut!.suit === "H" || s.cut!.suit === "D" ? 0 : 1);
    expect(s.turn).toBe((s.dealer + 1) % 4);
  });

  it("bots play whole matches to 11 with all 120 points counted every round", () => {
    for (let seed = 1; seed <= 40; seed++) {
      const s = playOut(seed);
      expect(s.phase).toBe("gameOver");
      expect(Math.max(...s.match)).toBeGreaterThanOrEqual(11);
      for (const r of s.rounds) {
        if (r.points) expect(r.points[0] + r.points[1]).toBe(120);
        if (r.how === "points") expect(Math.max(...r.points!)).toBeGreaterThan(60);
        if (r.how === "tie") expect(r.points).toEqual([60, 60]);
      }
    }
  });

  it("highest beater wins: a later set must beat the current winning set", () => {
    let s = newBura(7);
    s.trump = "S";
    s.hands = [
      [c("7C"), c("6H"), c("6D"), c("7H"), c("7D")],
      [c("9C"), c("8H"), c("8D"), c("9H"), c("9D")],
      [c("8C"), c("JH"), c("JD"), c("QH"), c("QD")],
      [c("AC"), c("KH"), c("KD"), c("AH"), c("AD")],
    ];
    s.turn = 0;
    s = applyBura(s, 0, { t: "lead", cards: ["7C"] });
    s = applyBura(s, 1, { t: "answer", cards: ["9C"] }); // beats 7
    s = applyBura(s, 2, { t: "answer", cards: ["8C"] }); // beats 7 but not 9: face down
    expect(s.trick!.best).toBe(1);
    expect(buraView(s, 0).trick!.plays[2].cards).toEqual([null]);
    expect(buraView(s, 2).trick!.plays[2].cards[0]).toMatchObject({ id: "8C" });
    s = applyBura(s, 3, { t: "answer", cards: ["14C"] }); // ace beats 9
    expect(s.lastTrick!.winner).toBe(3);
    expect(pilePoints(s.piles[1])).toBe(11);
    expect(s.turn).toBe(3);
    expect(s.hands.every((h) => h.length === 5)).toBe(true);
  });

  it("bura (5 trumps) wins the round at its value", () => {
    let s = newBura(3);
    s.trump = "S";
    s.hands[s.turn] = [c("6S"), c("7S"), c("8S"), c("9S"), c("10S")];
    const team = s.turn % 2;
    s = applyBura(s, s.turn, { t: "bura" });
    expect(s.phase).toBe("roundOver");
    expect(s.match[team]).toBe(1);
    expect(s.rounds[0].how).toBe("bura");
  });

  it("malyutka is played out of turn and the others top up to 5", () => {
    let s = newBura(9);
    s.trump = "S";
    s.hands = [
      [c("7C"), c("6H"), c("6D"), c("7H"), c("7D")],
      [c("6C"), c("8C"), c("9C"), c("JC"), c("QC")],
      [c("8H"), c("JH"), c("JD"), c("QH"), c("QD")],
      [c("10C"), c("KH"), c("KD"), c("AH"), c("AD")],
    ];
    s.turn = 0;
    s = applyBura(s, 0, { t: "lead", cards: ["7C"] });
    expect(buraView(s, 1).canMalyutka).toBe(true);
    s = applyBura(s, 1, { t: "answer", cards: ["6C", "8C", "9C", "11C", "12C"] });
    expect(s.trick!.size).toBe(5);
    expect(s.trick!.best).toBe(1);
    s = applyBura(s, 2, { t: "answer", cards: ["8H", "11H", "11D", "12H", "12D"] });
    s = applyBura(s, 3, { t: "answer", cards: ["10C", "13H", "13D", "14H", "14D"] });
    expect(s.turn).toBe(0); // the leader tops up
    s = applyBura(s, 0, { t: "topUp", cards: ["6H", "6D", "7H", "7D"] });
    expect(s.lastTrick!.winner).toBe(1);
  });

  it("raises need both partners; answering partners who disagree accept", () => {
    let s = newBura(5);
    const leader = s.turn;
    s = applyBura(s, leader, { t: "raise" });
    expect(s.turn).toBe((leader + 2) % 4);
    s = applyBura(s, s.turn, { t: "approve", yes: false });
    expect(s.raise).toBeNull();
    expect(s.turn).toBe(leader);
    expect(s.stake).toBe(1);

    s = applyBura(s, leader, { t: "raise" });
    s = applyBura(s, s.turn, { t: "approve", yes: true });
    expect(s.turn).toBe((leader + 1) % 4);
    s = applyBura(s, s.turn, { t: "respond", answer: "fold" });
    s = applyBura(s, s.turn, { t: "approve", yes: false }); // partner disagrees: accepted
    expect(s.stake).toBe(2);
    expect(s.turn).toBe(leader);
    expect(() => applyBura(s, leader, { t: "raise" })).toThrow(); // teams alternate

    let f = newBura(5);
    const l2 = f.turn;
    f = applyBura(f, l2, { t: "raise" });
    f = applyBura(f, f.turn, { t: "approve", yes: true });
    f = applyBura(f, f.turn, { t: "respond", answer: "fold" });
    f = applyBura(f, f.turn, { t: "approve", yes: true });
    expect(f.phase).toBe("roundOver");
    expect(f.match[l2 % 2]).toBe(1); // value before the raise
  });

  it("the team that won the round deals the next one", () => {
    let s = newBura(11);
    s.trump = "S";
    s.hands[s.turn] = [c("6S"), c("7S"), c("8S"), c("9S"), c("10S")];
    const winner = s.turn % 2;
    s = applyBura(s, s.turn, { t: "bura" });
    s = applyBura(s, -1, { t: "next" });
    expect(s.dealer % 2).toBe(winner);
  });
});
