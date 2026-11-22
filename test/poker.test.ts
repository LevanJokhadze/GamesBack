import { describe, expect, it } from "vitest";
import { makeCard } from "../src/engine/cards";
import { applyPoker, bestHand, newPoker, pokerBot, pokerLeave, pokerOptions, pokerSit, pokerView, PokerState } from "../src/poker";

const c = (id: string) => makeCard(Number(id.slice(0, -1).replace("A", "14").replace("K", "13").replace("Q", "12").replace("J", "11").replace("T", "10")), id.slice(-1) as "S");
const hand = (ids: string) => bestHand(ids.split(" ").map(c));

function total(s: PokerState) {
  return s.stacks.reduce((a, b) => a + b, 0) + s.committed.reduce((a, b) => a + b, 0) * (s.phase === "betting" ? 1 : 0);
}

describe("hand ranking", () => {
  it("orders the categories", () => {
    expect(hand("AS KS QS JS TS 2H 3D").cat).toBe(8);
    expect(hand("9S 9H 9D 9C 2S 3H 4D").cat).toBe(7);
    expect(hand("9S 9H 9D 2C 2S 3H 4D").cat).toBe(6);
    expect(hand("AS 9S 7S 4S 2S 3H 4D").cat).toBe(5);
    expect(hand("AS 2H 3D 4C 5S KH KD").cat).toBe(4);
    expect(hand("AS 2H 3D 4C 5S KH KD").best.map((x) => x.rank).sort((a, b) => a - b)).toEqual([2, 3, 4, 5, 14]);
    expect(hand("9S 9H 9D 2C 5S 3H KD").cat).toBe(3);
    expect(hand("9S 9H 2D 2C 5S 3H KD").cat).toBe(2);
    expect(hand("9S 9H 2D 7C 5S 3H KD").cat).toBe(1);
    expect(hand("9S JH 2D 7C 5S 3H KD").cat).toBe(0);
  });
  it("uses kickers and treats the wheel as the lowest straight", () => {
    expect(hand("AS AH KD 7C 5S").score).toBeGreaterThan(hand("AD AC QD 7H 5H").score);
    expect(hand("6S 2H 3D 4C 5S").score).toBeGreaterThan(hand("AS 2H 3D 4C 5S").score);
    expect(hand("AS AH KD 7C 5S").score).toBe(hand("AD AC KH 7H 5H").score);
  });
});

function playOut(s: PokerState, hands: number) {
  const start = total(s);
  let guard = 0;
  while (s.handNo < hands && guard++ < 20000) {
    if (s.phase === "handOver") {
      for (let i = 0; i < s.n; i++) if (s.seated[i] && s.stacks[i] === 0) s = applyPoker(s, i, { t: "rebuy" });
      s = applyPoker(s, -1, { t: "next" });
      continue;
    }
    expect(s.phase).toBe("betting");
    s = applyPoker(s, s.turn, pokerBot(s, s.turn));
    const bought = s.bought.reduce((a, b) => a + b, 0);
    expect(total(s) + (s.phase === "handOver" ? 0 : 0)).toBe(bought);
  }
  expect(start).toBeGreaterThan(0);
  return s;
}

describe("cash table", () => {
  it("conserves chips over many bot hands at every table size", () => {
    for (let n = 2; n <= 9; n++) {
      let s = newPoker({ seats: n, bb: 20, seated: Array(n).fill(true) }, 1000 + n);
      s = playOut(s, 25);
      expect(s.handNo).toBeGreaterThanOrEqual(25);
      expect(s.stacks.every((x) => x >= 0)).toBe(true);
    }
  }, 120_000);

  it("posts blinds heads-up with the button on the small blind", () => {
    const s = newPoker({ seats: 2, bb: 20, seated: [true, true] }, 7);
    expect(s.sbSeat).toBe(s.button);
    expect(s.bets[s.sbSeat]).toBe(10);
    expect(s.bets[s.bbSeat]).toBe(20);
    expect(s.turn).toBe(s.button);
  });

  it("gives the big blind the option and enforces the minimum raise", () => {
    let s = newPoker({ seats: 3, bb: 20, seated: [true, true, true] }, 3);
    const utg = s.turn;
    expect(() => applyPoker(s, utg, { t: "raise", to: 30 })).toThrow();
    s = applyPoker(s, utg, { t: "raise", to: 60 });
    expect(pokerOptions(s, s.turn).minTo).toBe(100);
    s = applyPoker(s, s.turn, { t: "call" }); // sb
    s = applyPoker(s, s.turn, { t: "call" }); // bb
    expect(s.street).toBe(1);
    expect(s.board.length).toBe(3);
    // Limped pot: big blind may still raise.
    let t = newPoker({ seats: 3, bb: 20, seated: [true, true, true] }, 4);
    t = applyPoker(t, t.turn, { t: "call" });
    t = applyPoker(t, t.turn, { t: "call" });
    expect(t.turn).toBe(t.bbSeat);
    expect(pokerOptions(t, t.turn).canCheck).toBe(true);
    expect(pokerOptions(t, t.turn).canRaise).toBe(true);
  });

  it("splits side pots and returns the uncalled bet", () => {
    let s = newPoker({ seats: 3, bb: 20, seated: [true, true, true] }, 11);
    // Give one player a short stack.
    const short = s.turn;
    s.stacks[short] = 100;
    s.bought[short] = 100 + 0;
    const bought = s.stacks.reduce((a, b) => a + b, 0) + s.committed.reduce((a, b) => a + b, 0);
    s = applyPoker(s, short, { t: "raise", to: 100000 }); // all-in 100
    expect(s.stacks[short]).toBe(0);
    s = applyPoker(s, s.turn, { t: "raise", to: 1000 });
    s = applyPoker(s, s.turn, { t: "raise", to: 2000 }); // all-in for the big blind
    s = applyPoker(s, s.turn, { t: "call" });
    expect(s.phase).toBe("handOver");
    expect(s.board.length).toBe(5);
    expect(s.stacks.reduce((a, b) => a + b, 0)).toBe(bought);
    const r = s.result!;
    expect(r.pots.reduce((a, p) => a + p.amount, 0)).toBe(4100);
    // The short stack can win at most the main pot (3 x 100).
    expect(s.stacks[short]).toBeLessThanOrEqual(300);
    expect(r.shown.length).toBe(3);
  });

  it("an all-in for less than a full raise does not reopen the betting", () => {
    let s = newPoker({ seats: 3, bb: 20, seated: [true, true, true] }, 12);
    const a = s.turn;
    s = applyPoker(s, a, { t: "raise", to: 100 });
    const b = s.turn;
    s.stacks[b] = 130 - s.bets[b]; // can go to 130 total: +30, less than the 80 raise
    s = applyPoker(s, b, { t: "raise", to: 130 });
    s = applyPoker(s, s.turn, { t: "fold" }); // bb
    expect(s.turn).toBe(a);
    expect(pokerOptions(s, a).canRaise).toBe(false);
    expect(pokerOptions(s, a).canCall).toBe(true);
  });

  it("lets players sit, stand up mid-hand and rebuy", () => {
    let s = newPoker({ seats: 4, bb: 20, seated: [true, false, false, false] }, 5);
    expect(s.phase).toBe("waiting");
    s = pokerSit(s, 2);
    expect(s.phase).toBe("betting");
    const other = s.turn === 0 ? 2 : 0;
    s = pokerLeave(s, other);
    expect(s.phase).toBe("handOver");
    expect(s.result!.uncontested).toBe(true);
    s = applyPoker(s, -1, { t: "next" });
    expect(s.phase).toBe("waiting");
    s = pokerSit(s, 1);
    expect(s.phase).toBe("betting");
    expect(() => applyPoker(s, 1, { t: "rebuy" })).toThrow();
    s = pokerLeave(s, s.turn);
    expect(s.phase).toBe("handOver");
    const loser = s.seated[1] ? 1 : 2;
    s.stacks[loser] = 0;
    expect(() => applyPoker(s, loser, { t: "rebuy" })).not.toThrow();
  });

  it("hides other players' cards until the showdown", () => {
    const s = newPoker({ seats: 3, bb: 20, seated: [true, true, true] }, 9);
    const v = pokerView(s, 0);
    expect(v.hole[0]?.length).toBe(2);
    expect(v.hole[1]).toBeNull();
    expect(v.hole[2]).toBeNull();
  });
});
