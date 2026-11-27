import { describe, expect, it } from "vitest";
import {
  BgState,
  applyBackgammon,
  bgActor,
  bgAutoDelay,
  bgBot,
  bgView,
  canDouble,
  countAt,
  legalSteps,
  newBackgammon,
  pipCount,
  startBoard,
} from "../src/backgammon";

/** A position to test with: seat 0's checkers as {index: n}, seat 1's as {index: n}. */
function pos(base: BgState, mine: Record<number, number>, theirs: Record<number, number>, extra: Partial<BgState> = {}): BgState {
  const board = Array(24).fill(0);
  for (const [i, n] of Object.entries(mine)) board[Number(i)] += n;
  for (const [i, n] of Object.entries(theirs)) board[Number(i)] -= n;
  const count = (o: Record<number, number>) => Object.values(o).reduce((a, b) => a + b, 0);
  return {
    ...base,
    board,
    bar: [0, 0],
    off: [15 - count(mine), 15 - count(theirs)],
    phase: "moving",
    turn: 0,
    moved: [],
    turnStart: { board: board.slice(), bar: [0, 0], off: [15 - count(mine), 15 - count(theirs)] },
    ...extra,
  };
}

function withDice(s: BgState, dice: number[]): BgState {
  const left = dice[0] === dice[1] ? [dice[0], dice[0], dice[0], dice[0]] : dice.slice();
  return { ...s, dice, left, phase: "moving" };
}

describe("backgammon rules", () => {
  it("sets up 15 checkers each with 167 pips", () => {
    const b = startBoard();
    expect(b.filter((x) => x > 0).reduce((a, x) => a + x, 0)).toBe(15);
    expect(b.filter((x) => x < 0).reduce((a, x) => a - x, 0)).toBe(15);
    const s = newBackgammon({}, 1);
    expect(pipCount(s, 0)).toBe(167);
    expect(pipCount(s, 1)).toBe(167);
    expect(s.opening![0]).not.toBe(s.opening![1]);
    expect(s.turn).toBe(s.opening![0] > s.opening![1] ? 0 : 1);
    expect(s.matchTo).toBe(5);
  });

  it("the opening player moves with both opening dice", () => {
    let s = newBackgammon({ matchTo: 3 }, 42);
    expect(s.phase).toBe("opening");
    expect(bgAutoDelay(s)).not.toBeNull();
    s = applyBackgammon(s, -1, { t: "auto" });
    expect(s.phase).toBe("moving");
    expect(s.left.sort()).toEqual(s.opening!.slice().sort());
  });

  it("must use both dice when possible, and the higher one when only one fits", () => {
    const base = newBackgammon({}, 3);
    // Seat 0 has one checker on index 10. Seat 1 blocks index 4 (10-6) and index 5 (10-5).
    // With 6-5: the 6 lands on a block, the 5 too: nothing is playable.
    let s = withDice(pos(base, { 10: 1, 0: 14 }, { 4: 2, 5: 2, 23: 11 }), [6, 5]);
    expect(legalSteps(s)).toEqual([]);
    // Only one die can be used: open index 5 so the 5 plays, but then 10-5=5 -> 5-6 is off the board (not all home).
    s = withDice(pos(base, { 10: 1, 0: 14 }, { 4: 2, 23: 13 }), [6, 5]);
    // 6 is blocked (index 4), 5 to index 5 then 6 from 5 is -1 (bearing off not allowed: index 10 checker moved, all home? yes after the 5)
    const steps = legalSteps(s);
    expect(steps.every((m) => m.die === 5 || m.die === 6)).toBe(true);
    // Higher-die rule: one checker on 12, seat 1 blocks 12-6-5=1 and 12-5-6=1; 6 and 5 each fit alone.
    s = withDice(pos(base, { 12: 1, 18: 14 }, { 1: 2, 23: 13 }), [6, 5]);
    // 18-6=12 and 18-5=13 are open too, so both dice can be used: every step keeps two dice usable.
    expect(legalSteps(s).length).toBeGreaterThan(0);
    s = withDice(pos(base, { 12: 1 }, { 1: 2, 7: 2, 6: 2, 23: 8 }, { off: [14, 1] } as Partial<BgState>), [6, 5]);
    // 12-6=6 blocked, 12-5=7 blocked: nothing at all.
    expect(legalSteps(s)).toEqual([]);
    s = withDice(pos(base, { 12: 1 }, { 1: 2, 7: 2, 23: 11 }), [6, 5]);
    // 12-6=6 open, then 6-5=1 blocked; 12-5=7 blocked. Only the 6 plays.
    expect(legalSteps(s)).toEqual([{ from: 12, to: 6, die: 6 }]);
    s = withDice(pos(base, { 12: 1 }, { 0: 2, 1: 2, 23: 11 }), [6, 5]);
    // 6 then 5 -> index 1 blocked; 5 then 6 -> index 1 blocked. Either die alone: must be the 6.
    expect(legalSteps(s)).toEqual([{ from: 12, to: 6, die: 6 }]);
  });

  it("enters from the bar first and hits blots", () => {
    const base = newBackgammon({}, 5);
    let s = withDice(pos(base, { 5: 14 }, { 20: 1, 21: 2, 0: 12 }, { bar: [1, 0] }), [4, 3]);
    s = { ...s, off: [0, 0] };
    const steps = legalSteps(s);
    expect(steps.every((m) => m.from === "bar")).toBe(true);
    // 24-4 = 20 is a blot: hit it; 24-3 = 21 is blocked.
    expect(steps).toEqual([{ from: "bar", to: 20, die: 4 }]);
    s = applyBackgammon(s, 0, { t: "move", from: "bar", die: 4 });
    expect(s.bar).toEqual([0, 1]);
    expect(countAt(s, 20, 0)).toBe(1);
  });

  it("bears off exactly, or with a higher die from the highest point", () => {
    const base = newBackgammon({}, 6);
    let s = withDice(pos(base, { 2: 2, 0: 1 }, { 23: 15 }), [6, 1]);
    const steps = legalSteps(s);
    // 6 bears off from index 2 (the highest point); 1 moves 2->1 or 0 off.
    expect(steps).toContainEqual({ from: 2, to: "off", die: 6 });
    expect(steps).toContainEqual({ from: 0, to: "off", die: 1 });
    // Not all home: no bearing off.
    s = withDice(pos(base, { 7: 1, 2: 2 }, { 23: 15 }), [6, 3]);
    expect(legalSteps(s).some((m) => m.to === "off")).toBe(false);
  });

  it("scores single games, gammons and backgammons with the cube", () => {
    const base = newBackgammon({ matchTo: 7 }, 8);
    // Seat 0 bears off its last checker; seat 1 has borne nothing off and has a checker in seat 0's home.
    let s = withDice(pos(base, { 0: 1 }, { 3: 1, 23: 14 }), [1, 2]);
    s = { ...s, off: [14, 0], cube: 2 };
    s = applyBackgammon(s, 0, { t: "move", from: 0, die: 2 });
    expect(s.phase).toBe("gameOver");
    expect(s.lastGame).toMatchObject({ winner: 0, kind: 3, cube: 2, points: 6 });
    expect(s.score).toEqual([6, 0]);

    s = withDice(pos(base, { 0: 1 }, { 23: 15 }), [1, 2]);
    s = { ...s, off: [14, 0], score: [0, 0] };
    s = applyBackgammon(s, 0, { t: "move", from: 0, die: 2 });
    expect(s.lastGame).toMatchObject({ kind: 2, points: 2 });

    s = withDice(pos(base, { 0: 1 }, { 23: 14 }), [1, 2]);
    s = { ...s, off: [14, 1], score: [0, 0] };
    s = applyBackgammon(s, 0, { t: "move", from: 0, die: 2 });
    expect(s.lastGame).toMatchObject({ kind: 1, points: 1 });
  });

  it("doubling: take doubles the cube and gives it away, drop loses the game", () => {
    let s: BgState = { ...newBackgammon({ matchTo: 5 }, 9), phase: "roll", turn: 0, dice: null, left: [] };
    expect(canDouble(s, 0)).toBe(true);
    expect(canDouble(s, 1)).toBe(false);
    s = applyBackgammon(s, 0, { t: "double" });
    expect(s.phase).toBe("doubled");
    expect(bgActor(s)).toBe(1);
    const taken = applyBackgammon(s, 1, { t: "take" });
    expect(taken.cube).toBe(2);
    expect(taken.cubeOwner).toBe(1);
    expect(taken.phase).toBe("moving");
    expect(taken.dice).toHaveLength(2);
    // Seat 0 no longer owns the cube.
    expect(canDouble({ ...taken, phase: "roll", turn: 0 }, 0)).toBe(false);
    expect(canDouble({ ...taken, phase: "roll", turn: 1 }, 1)).toBe(true);
    const dropped = applyBackgammon(s, 1, { t: "drop" });
    expect(dropped.lastGame).toMatchObject({ winner: 0, points: 1, dropped: true });
    expect(dropped.score).toEqual([1, 0]);
  });

  it("plays the Crawford game without doubling", () => {
    let s: BgState = { ...newBackgammon({ matchTo: 3 }, 10), phase: "roll", turn: 0, dice: null, left: [] };
    s = applyBackgammon(s, 0, { t: "double" });
    s = applyBackgammon(s, 1, { t: "drop" });
    s = { ...s, score: [2, 0] };
    s = applyBackgammon(s, -1, { t: "auto" });
    expect(s.crawford).toBe(true);
    expect(canDouble({ ...s, phase: "roll" }, s.turn)).toBe(false);
    s = { ...s, phase: "roll", turn: 1, dice: null, left: [] };
    s = applyBackgammon(s, 1, { t: "roll" });
    // After the Crawford game, doubling is allowed again.
    const after = { ...s, phase: "gameOver" as const, crawfordDone: true, score: [2, 1] as [number, number] };
    const next = applyBackgammon(after, -1, { t: "auto" });
    expect(next.crawford).toBe(false);
    expect(canDouble({ ...next, phase: "roll", turn: 1 }, 1)).toBe(true);
  });

  it("undo puts the checkers back", () => {
    let s = newBackgammon({}, 11);
    s = applyBackgammon(s, -1, { t: "auto" });
    const before = s.board.slice();
    const m = legalSteps(s)[0];
    s = applyBackgammon(s, s.turn, { t: "move", from: m.from, die: m.die });
    expect(s.board).not.toEqual(before);
    s = applyBackgammon(s, s.turn, { t: "undo" });
    expect(s.board).toEqual(before);
    expect(s.left).toHaveLength(2);
    expect(() => applyBackgammon(s, s.turn, { t: "done" })).toThrow();
  });

  it("bots play whole matches with every checker accounted for", () => {
    for (let seed = 1; seed <= 6; seed++) {
      let s = newBackgammon({ matchTo: 3 }, seed * 101);
      for (let n = 0; n < 20000 && s.phase !== "matchOver"; n++) {
        const a = bgActor(s);
        s = a === null || bgAutoDelay(s) !== null ? applyBackgammon(s, -1, { t: "auto" }) : applyBackgammon(s, a, bgBot(s, a));
        for (const seat of [0, 1]) {
          let c = s.bar[seat] + s.off[seat];
          for (let i = 0; i < 24; i++) c += countAt(s, i, seat);
          expect(c).toBe(15);
        }
      }
      expect(s.phase).toBe("matchOver");
      expect(Math.max(...s.score)).toBeGreaterThanOrEqual(3);
      const v = bgView(s, 0);
      expect(v.kind).toBe("backgammon");
    }
  });
});
