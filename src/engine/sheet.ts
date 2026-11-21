// Score sheet for Joker played with real cards: players enter each deal's bids and tricks,
// and the points come from exactly the same rules as the online game (scoreDeal, closePulka).

import { GameConfig, PulkaResult, RuleError, SEATS, closePulka, makeSchedule, scoreDeal } from "./game";

/** One deal as written on the sheet. `took` is null while the deal is being played. */
export interface SheetDeal {
  bids: number[];
  took: number[] | null;
}

export interface SheetScore {
  schedule: number[][];
  pulkas: PulkaResult[];
  totals: number[];
  /** Deals with tricks written in. */
  done: number;
  /** All deals of the game, for the chosen mode. */
  count: number;
  complete: boolean;
}

/** Hand size of every deal, in order. */
export function sheetSizes(mode: GameConfig["mode"]): number[] {
  return makeSchedule(mode).flat();
}

/** Who deals deal number `index` (0-based): the deal moves one seat left each time. */
export function sheetDealer(firstDealer: number, index: number): number {
  return (firstDealer + index) % SEATS;
}

function intIn(v: unknown, lo: number, hi: number): v is number {
  return typeof v === "number" && Number.isInteger(v) && v >= lo && v <= hi;
}

/**
 * Checks what was entered against the rules: bids 0..cards, the dealer may not make the bids add
 * up to the number of cards, the tricks taken add up to the number of cards. Only the last deal
 * may still be missing its tricks. Returns a clean copy.
 */
export function validateSheet(config: GameConfig, deals: unknown): SheetDeal[] {
  const sizes = sheetSizes(config.mode);
  if (!Array.isArray(deals)) throw new RuleError("Bad deals");
  if (deals.length > sizes.length) throw new RuleError("Too many deals");
  return deals.map((raw, k) => {
    const d = raw as { bids?: unknown; took?: unknown };
    const n = sizes[k];
    const label = `Deal ${k + 1}`;
    if (!Array.isArray(d?.bids) || d.bids.length !== SEATS || !d.bids.every((b) => intIn(b, 0, n))) {
      throw new RuleError(`${label}: each bid is a number from 0 to ${n}`);
    }
    const bids = d.bids as number[];
    if (bids.reduce((a, b) => a + b, 0) === n) {
      throw new RuleError(`${label}: the dealer can't make the bids add up to ${n}`);
    }
    let took: number[] | null = null;
    if (d.took !== null && d.took !== undefined) {
      if (!Array.isArray(d.took) || d.took.length !== SEATS || !d.took.every((x) => intIn(x, 0, n))) {
        throw new RuleError(`${label}: tricks taken are numbers from 0 to ${n}`);
      }
      took = (d.took as number[]).slice();
      if (took.reduce((a, b) => a + b, 0) !== n) throw new RuleError(`${label}: tricks taken must add up to ${n}`);
    } else if (k !== deals.length - 1) {
      throw new RuleError(`${label}: tricks taken are missing`);
    }
    return { bids: bids.slice(), took };
  });
}

/** Points of a (partly) filled sheet, in the same shape as an online game's pulkas. */
export function scoreSheet(config: GameConfig, deals: SheetDeal[]): SheetScore {
  const schedule = makeSchedule(config.mode);
  const pulkas: PulkaResult[] = [];
  const totals = [0, 0, 0, 0];
  let k = 0;
  let done = 0;
  for (const sizes of schedule) {
    const pulka: PulkaResult = { deals: [], premium: [], bonus: [0, 0, 0, 0], total: [0, 0, 0, 0] };
    for (const n of sizes) {
      const d = deals[k++];
      if (!d || !d.took) break;
      const res = scoreDeal(n, d.bids, d.took, config.khishti);
      pulka.deals.push(res);
      for (let i = 0; i < SEATS; i++) pulka.total[i] += res.points[i];
      done++;
    }
    if (pulka.deals.length === 0) break;
    if (pulka.deals.length === sizes.length) closePulka(pulka);
    for (let i = 0; i < SEATS; i++) totals[i] += pulka.total[i];
    pulkas.push(pulka);
    if (pulka.deals.length < sizes.length) break;
  }
  const count = schedule.flat().length;
  return { schedule, pulkas, totals, done, count, complete: done === count };
}
