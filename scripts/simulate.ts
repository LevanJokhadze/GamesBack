// Plays many all-bot games and prints how often bots hit their bid.
import { applyAction, botAction, newGame } from "../src/engine";

const games = Number(process.argv[2] ?? 200);
let deals = 0, hits = 0, khishti = 0;
for (let g = 0; g < games; g++) {
  let s = newGame({}, g + 1);
  while (s.phase !== "gameOver") s = applyAction(s, s.turn, botAction(s, s.turn));
  for (const p of s.pulkas) for (const d of p.deals) for (let i = 0; i < 4; i++) {
    deals++;
    if (d.bids[i] === d.took[i]) hits++;
    if (d.bids[i] > 0 && d.took[i] === 0) khishti++;
  }
}
console.log(`${games} games, bid hit rate ${(100 * hits / deals).toFixed(1)}%, khishti ${(100 * khishti / deals).toFixed(1)}%`);
