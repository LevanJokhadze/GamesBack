// A simple backgammon bot: it tries every way to play the dice and keeps the position
// that scores best on a hand-written evaluation (race, safety, points made, primes,
// hits). Doubling decisions use the same evaluation and the pip count.

import { BgAction, BgState, applyBackgammon, canDouble, countAt, distance, legalSteps, pipCount } from "./engine";

type Pos = Pick<BgState, "board" | "bar" | "off">;

/** My checkers and the opponent's, both indexed by distance from MY bearing off (1..24). */
function sides(p: Pos, seat: number) {
  const me = Array(26).fill(0);
  const them = Array(26).fill(0);
  for (let i = 0; i < 24; i++) {
    me[distance(i, seat)] = countAt(p, i, seat);
    them[distance(i, seat)] = countAt(p, i, 1 - seat);
  }
  me[25] = p.bar[seat];
  them[0] = p.bar[1 - seat]; // the opponent enters at my far end and moves towards 25
  return { me, them };
}

/** Is there still contact (can either side still hit the other)? */
export function inContact(p: Pos, seat: number): boolean {
  const { me, them } = sides(p, seat);
  let myBack = 0;
  for (let d = 25; d >= 1; d--) if (me[d]) { myBack = d; break; }
  let theirBack = 26;
  for (let d = 0; d <= 24; d++) if (them[d]) { theirBack = d; break; }
  return myBack > theirBack;
}

/** Chance (roughly, out of 36) that a blot at distance d from an enemy checker is hit. */
const SHOT: number[] = [0, 11, 12, 14, 15, 15, 17, 6, 6, 5, 3, 2, 3, 0, 0, 1, 1, 0, 1, 0, 1, 0, 0, 0, 1];

/** Score of a position for `seat` (higher is better). */
export function evaluate(p: Pos, seat: number): number {
  const myPips = pipCount(p, seat);
  const theirPips = pipCount(p, 1 - seat);
  let score = (theirPips - myPips) * 1.0;
  score += p.off[seat] * 2 - p.off[1 - seat] * 2;
  if (!inContact(p, seat)) {
    // Pure race: pips, plus a little for checkers already home or off.
    const { me } = sides(p, seat);
    // Bring the stragglers home first (every checker outside delays bearing off),
    // then bear off as many as possible; avoid piling checkers on the low points.
    let outside = 0;
    for (let d = 7; d <= 25; d++) outside += me[d] * (2 + (d - 6) * 0.4);
    let gaps = 0;
    for (let d = 1; d <= 6; d++) if (me[d] > 3) gaps += (me[d] - 3) * (7 - d) * 0.1;
    return score * 2 - outside - gaps + p.off[seat] * 3;
  }
  const { me, them } = sides(p, seat);
  // Blots: the risk of being hit times what it costs.
  for (let d = 1; d <= 24; d++) {
    if (me[d] !== 1) continue;
    let shots = 0;
    for (let e = 0; e < d; e++) if (them[e]) shots += SHOT[d - e] ?? 0;
    shots = Math.min(shots, 30);
    score -= (shots / 36) * (25 - d + 8);
  }
  // Points made: home board and the 5, 4, 7 points matter most.
  const value = [0, 2, 3, 4, 6, 7, 6, 5, 3, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1];
  let run = 0;
  let bestRun = 0;
  for (let d = 1; d <= 24; d++) {
    if (me[d] >= 2) {
      score += value[d];
      run++;
      bestRun = Math.max(bestRun, run);
    } else run = 0;
    if (me[d] > 3) score -= (me[d] - 3) * 0.8;
  }
  score += [0, 0, 1, 3, 7, 12, 20][Math.min(bestRun, 6)];
  // Opponent on the bar is worth more the more of my home board is closed.
  let homeMade = 0;
  for (let d = 1; d <= 6; d++) if (me[d] >= 2) homeMade++;
  score += p.bar[1 - seat] * (4 + homeMade * 2.5);
  // My own checkers on the bar against their home board.
  let theirHome = 0;
  for (let d = 19; d <= 24; d++) if (them[d] >= 2) theirHome++;
  score -= p.bar[seat] * (4 + theirHome * 2.5);
  // Back checkers stuck deep in their home are a liability late in the game.
  if (myPips < 120) for (let d = 19; d <= 24; d++) score -= me[d] * 1.5;
  return score;
}

/** All distinct end positions reachable with the dice, with the first step of each. */
function finals(s: BgState): { s: BgState; first: { from: number | "bar"; die: number } | null }[] {
  const seen = new Map<string, { s: BgState; first: { from: number | "bar"; die: number } | null }>();
  const walk = (st: BgState, first: { from: number | "bar"; die: number } | null) => {
    const moves = st.phase === "moving" ? legalSteps(st) : [];
    if (!moves.length) {
      const key = `${st.board.join(",")}|${st.bar}|${st.off}`;
      if (!seen.has(key)) seen.set(key, { s: st, first });
      return;
    }
    const tried = new Set<string>();
    for (const m of moves) {
      const k = `${m.from}:${m.die}`;
      if (tried.has(k)) continue;
      tried.add(k);
      walk(applyBackgammon(st, st.turn, { t: "move", from: m.from, die: m.die }), first ?? { from: m.from, die: m.die });
    }
  };
  walk(s, null);
  return [...seen.values()];
}

/** The bot's next action in its turn (one checker at a time). */
export function bgBot(s: BgState, seat: number): BgAction {
  if (s.phase === "doubled") return shouldTake(s, seat) ? { t: "take" } : { t: "drop" };
  if (s.phase === "roll") return canDouble(s, seat) && shouldDouble(s, seat) ? { t: "double" } : { t: "roll" };
  const st: BgState = s.phase === "opening" ? { ...s, phase: "moving", left: (s.dice ?? []).slice(), moved: [] } : s;
  const options = finals(st);
  let best: (typeof options)[number] | null = null;
  let bestScore = -Infinity;
  for (const o of options) {
    // A finished game is always best.
    const v = o.s.phase === "gameOver" || o.s.phase === "matchOver" ? 1e6 : evaluate(o.s, seat);
    if (v > bestScore) {
      bestScore = v;
      best = o;
    }
  }
  if (!best || !best.first) return { t: "done" };
  return { t: "move", from: best.first.from, die: best.first.die };
}

/** Rough winning chances for `seat` from the evaluation (0..1). */
function chances(s: BgState, seat: number, onRoll: boolean): number {
  const pips = pipCount(s, seat);
  const theirs = pipCount(s, 1 - seat);
  if (!inContact(s, seat)) {
    // Race: being on roll is worth about 4 pips; each 1% of lead is worth about 3%.
    const lead = (theirs - pips + (onRoll ? 4 : -4)) / Math.max(theirs, 1);
    return Math.max(0, Math.min(1, 0.5 + lead * 3));
  }
  const e = evaluate(s, seat) + (onRoll ? 4 : -4);
  return 1 / (1 + Math.exp(-e / 22));
}

function shouldDouble(s: BgState, seat: number): boolean {
  const opp = 1 - seat;
  // Behind in the match after the Crawford game: the leader is one point away, so double at once.
  if (s.score[opp] === s.matchTo - 1 && s.score[seat] < s.matchTo - 1) return true;
  const p = chances(s, seat, true);
  return p >= 0.68 && p <= 0.9;
}

function shouldTake(s: BgState, seat: number): boolean {
  const opp = 1 - seat;
  // A double that would cost the match anyway: taking can't hurt.
  if (s.score[opp] + s.cube >= s.matchTo) return true;
  return chances(s, seat, false) >= 0.24;
}
