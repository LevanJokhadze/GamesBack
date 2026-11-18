// Chess (FIDE) Elo, extended to a four-player game.
//
// Every pair of players is treated as one chess game: the one with the higher
// final score "wins" (equal scores are a draw). The expected score and K-factor
// are exactly the chess ones; the K-factor is shared over the three opponents
// (K / 3 per pairing), so one Joker game moves a rating about as much as one
// chess game against an opponent field of that strength.

export const START_RATING = 1500;
/** Bots have this fixed rating and are never changed. */
export const BOT_RATING = 1500;

export interface EloPlayer {
  rating: number;
  /** Rated games played before this one. */
  games: number;
  /** Highest rating ever reached. */
  peak: number;
  /** Final points in the game. */
  score: number;
}

/** FIDE K-factor: 40 for the first 30 games, 10 once 2400 has been reached, otherwise 20. */
export function kFactor(p: Pick<EloPlayer, "games" | "peak">): number {
  if (p.games < 30) return 40;
  if (p.peak >= 2400) return 10;
  return 20;
}

/** Chess expected score of a player rated `a` against one rated `b`. */
export function expectedScore(a: number, b: number): number {
  return 1 / (1 + Math.pow(10, (b - a) / 400));
}

/** Rating change for every player (rounded to whole points). */
export function eloDeltas(players: EloPlayer[]): number[] {
  const n = players.length;
  return players.map((p, i) => {
    const k = kFactor(p) / (n - 1);
    let delta = 0;
    for (let j = 0; j < n; j++) {
      if (j === i) continue;
      const q = players[j];
      const actual = p.score > q.score ? 1 : p.score < q.score ? 0 : 0.5;
      delta += k * (actual - expectedScore(p.rating, q.rating));
    }
    return Math.round(delta);
  });
}
