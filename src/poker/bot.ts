// Poker bot: estimates its chance to win by dealing out random hands (Monte Carlo),
// compares it with the price of calling and adds a little randomness.

import { Card, rng } from "../engine/cards";
import { PokerAction, PokerState, pokerOptions } from "./engine";
import { pokerDeck, scoreOf } from "./hand";

const SIMS = 160;

/** Chance to win (ties count half) against `opponents` random hands. */
export function equity(hole: Card[], board: Card[], opponents: number, rand: () => number, sims = SIMS): number {
  if (opponents <= 0) return 1;
  const used = new Set([...hole, ...board].map((c) => c.id));
  const rest = pokerDeck().filter((c) => !used.has(c.id));
  let won = 0;
  for (let k = 0; k < sims; k++) {
    // Partial shuffle: just enough cards for the board and the opponents.
    const need = 5 - board.length + opponents * 2;
    const d = rest.slice();
    for (let i = 0; i < need; i++) {
      const j = i + Math.floor(rand() * (d.length - i));
      [d[i], d[j]] = [d[j], d[i]];
    }
    const full = [...board, ...d.slice(0, 5 - board.length)];
    const mine = scoreOf([...hole, ...full]);
    let best = 0;
    let ties = 0;
    let lost = false;
    for (let o = 0; o < opponents; o++) {
      const off = 5 - board.length + o * 2;
      const s = scoreOf([d[off], d[off + 1], ...full]);
      if (s > mine) {
        lost = true;
        break;
      }
      if (s === mine) ties++;
      best = Math.max(best, s);
    }
    if (!lost) won += ties ? 1 / (ties + 1) : 1;
  }
  return won / sims;
}

/**
 * The move a bot makes. `passive` is for a player who stopped responding: check when free,
 * otherwise fold.
 */
export function pokerBot(s: PokerState, seat: number, passive = false): PokerAction {
  const o = pokerOptions(s, seat);
  if (passive) return o.canCheck ? { t: "check" } : { t: "fold" };
  const rand = rng((s.seed ^ Math.imul(s.step + 7, 0x85ebca6b) ^ seat) >>> 0);
  const hole = s.hole[seat]!;
  const opponents = s.inHand.filter((x, i) => x && !s.folded[i] && i !== seat).length;
  const eq = equity(hole, s.board, opponents, rand);
  const pot = s.committed.reduce((a, b) => a + b, 0);
  const potOdds = o.toCall > 0 ? o.toCall / (pot + o.toCall) : 0;
  const r = rand();
  // Strength above what a fair share would be at this table.
  const edge = eq - 1 / (opponents + 1);

  const raiseTo = (frac: number): PokerAction => {
    const want = s.currentBet + Math.max(o.minTo - s.currentBet, Math.round((pot + o.toCall) * frac));
    const to = Math.min(o.maxTo, Math.max(o.minTo, want));
    return { t: "raise", to };
  };

  if (o.canRaise) {
    if (eq > 0.85 && r < 0.35 && s.street >= 2) return { t: "raise", to: o.maxTo };
    if (edge > 0.25 && r < 0.75) return raiseTo(0.6 + rand() * 0.5);
    if (edge > 0.12 && r < 0.35) return raiseTo(0.5);
    // Occasional bluff when nobody has bet.
    if (o.canCheck && r < 0.08) return raiseTo(0.5);
  }
  if (o.canCheck) return { t: "check" };
  // Short stack facing a big bet: call wider.
  const margin = 0.04 + (r - 0.5) * 0.06;
  if (eq >= potOdds + margin) return { t: "call" };
  return { t: "fold" };
}
