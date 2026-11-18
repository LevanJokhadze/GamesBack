import { describe, expect, it } from "vitest";
import { eloDeltas, expectedScore, kFactor } from "../src/engine";

describe("elo", () => {
  it("uses the chess expected score", () => {
    expect(expectedScore(1500, 1500)).toBe(0.5);
    expect(expectedScore(1900, 1500)).toBeCloseTo(0.909, 3);
  });
  it("uses FIDE K-factors", () => {
    expect(kFactor({ games: 0, peak: 1500 })).toBe(40);
    expect(kFactor({ games: 30, peak: 1500 })).toBe(20);
    expect(kFactor({ games: 100, peak: 2410 })).toBe(10);
  });
  it("equal new players: winner +20, last -20", () => {
    const p = (score: number) => ({ rating: 1500, games: 0, peak: 1500, score });
    expect(eloDeltas([p(900), p(500), p(300), p(-100)])).toEqual([20, 7, -7, -20]);
  });
  it("draws give nothing between equals", () => {
    const p = { rating: 1500, games: 50, peak: 1500, score: 100 };
    expect(eloDeltas([p, p, p, p])).toEqual([0, 0, 0, 0]);
  });
  it("beating stronger players is worth more", () => {
    const strong = { rating: 1900, games: 50, peak: 1900, score: 0 };
    const weak = { rating: 1300, games: 50, peak: 1300, score: 500 };
    const [w] = eloDeltas([weak, strong, strong, strong]);
    const [s] = eloDeltas([{ ...strong, score: 500 }, { ...weak, score: 0 }, { ...weak, score: 0 }, { ...weak, score: 0 }]);
    expect(w).toBeGreaterThan(s);
  });
});
