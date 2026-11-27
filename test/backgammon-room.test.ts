import { describe, expect, it } from "vitest";
import { Conn, FinishedGame, RoomCore, RoomHost } from "../src/room-core";

function setup() {
  const conns: Conn[] = [];
  const rated: FinishedGame[] = [];
  const host: RoomHost = {
    connections: () => conns,
    persist: async () => {},
    setTimer: async () => {},
    resolveUser: async (token) => ({ id: token, name: `nick_${token}`, avatarUrl: null, rating: 1500 }),
    rateGame: async (g) => {
      rated.push(g);
      return { matchId: "m", changes: g.userIds.map((u) => (u ? { before: 1500, after: 1510, delta: 10 } : null)) };
    },
  };
  const room = new RoomCore(host, null);
  const join = async (token: string) => {
    const sent: any[] = [];
    let id: string | null = null;
    const c: Conn = { send: (t) => sent.push(JSON.parse(t)), close: () => {}, getPlayerId: () => id, setPlayerId: (v) => (id = v) };
    conns.push(c);
    await room.onMessage(c, JSON.stringify({ t: "hello", token }));
    const say = (m: object) => room.onMessage(c, JSON.stringify(m));
    return { c, sent, say, last: () => [...sent].reverse().find((m) => m.t === "state")?.room };
  };
  return { room, join, rated };
}

describe("backgammon room", () => {
  it("plays a rated match against a bot", async () => {
    const { room, join, rated } = setup();
    await room.init("BGAME", "backgammon");
    const a = await join("a");
    let v = a.last();
    expect(v.seats.length).toBe(2);
    expect(v.config).toMatchObject({ game: "backgammon", matchTo: 5 });
    await a.say({ t: "config", config: { matchTo: 1 } });
    expect(a.last().config.matchTo).toBe(1);
    await a.say({ t: "start" });
    v = a.last();
    expect(v.status).toBe("playing");
    expect(v.game.kind).toBe("backgammon");
    expect(v.seats[1].bot).toBe(true);

    for (let k = 0; k < 5000 && a.last().status === "playing"; k++) {
      v = a.last();
      const g = v.game;
      if (g.mustAnswer) await a.say({ t: "bg", action: { t: "take" } });
      else if (g.canRoll) await a.say({ t: "bg", action: { t: "roll" } });
      else if (g.legal.length && (g.phase === "moving" || g.phase === "opening")) {
        await a.say({ t: "bg", action: { t: "move", from: g.legal[0].from, die: g.legal[0].die } });
      } else if (g.canDone) await a.say({ t: "bg", action: { t: "done" } });
      else {
        room.data!.deadline = Date.now() - 100;
        await room.onTimer();
      }
    }
    v = a.last();
    expect(v.status).toBe("finished");
    expect(rated).toHaveLength(1);
    expect(rated[0]).toMatchObject({ mode: "backgammon", pairs: false, userIds: ["a", null] });
    expect(Math.max(...rated[0].totals)).toBeGreaterThanOrEqual(1);
    expect(v.ratingChanges[0]).toMatchObject({ delta: 10 });
  });

  it("switching a lobby to backgammon leaves two seats and a second player joins", async () => {
    const { room, join } = setup();
    await room.init("SWBGM");
    const a = await join("a");
    await a.say({ t: "config", config: { game: "backgammon", matchTo: 7 } });
    expect(a.last().seats.length).toBe(2);
    const b = await join("b");
    expect(b.last().you).toBe(1);
    await a.say({ t: "start" });
    const g = b.last().game;
    expect(g.kind).toBe("backgammon");
    expect(g.matchTo).toBe(7);
    // Only the player to act may move.
    const actor = g.turn;
    const other = actor === 0 ? b : a;
    await other.say({ t: "bg", action: { t: "roll" } });
    expect(other.sent.some((m) => m.t === "error")).toBe(true);
  });
});
