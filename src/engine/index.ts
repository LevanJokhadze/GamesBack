export * from "./cards";
export * from "./game";
export * from "./bot";

import { BotAction } from "./bot";
import { GameState, chooseTrump, placeBid, playCard } from "./game";

export function applyAction(s: GameState, seat: number, a: BotAction): GameState {
  if (a.t === "trump") return chooseTrump(s, seat, a.suit);
  if (a.t === "bid") return placeBid(s, seat, a.bid);
  return playCard(s, seat, a.cardId, a.joker);
}
export * from "./elo";
