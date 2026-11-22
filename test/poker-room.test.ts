import { describe, expect, it } from "vitest";
import { Conn, RoomCore, RoomHost } from "../src/room-core";

function setup() {
  const conns: Conn[] = [];
  let rated = 0;
  const host: RoomHost = {
    connections: () => conns,
    persist: async () => {},
    setTimer: async () => {},
    resolveUser: async (token) => ({ id: token, name: `nick_${token}`, avatarUrl: null, rating: 1500 }),
    rateGame: async () => {
      rated++;
      return { matchId: "m", changes: [] };
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
  return { room, join, rated: () => rated };
}

describe("poker room", () => {
  it("resizes the table, plays hands with bots, seats late joiners, and is never rated", async () => {
    const { room, join, rated } = setup();
    await room.init("POKER", "poker");
    const a = await join("a");
    expect(a.last().seats.length).toBe(6);
    await a.say({ t: "config", config: { seats: 3, bb: 50 } });
    expect(a.last().seats.length).toBe(3);
    expect(a.last().config).toMatchObject({ game: "poker", seats: 3, bb: 50 });
    await a.say({ t: "start" });
    let v = a.last();
    expect(v.status).toBe("playing");
    expect(v.game.kind).toBe("poker");
    expect(v.game.stacks.reduce((x: number, y: number) => x + y, 0) + v.game.bets.reduce((x: number, y: number) => x + y, 0)).toBe(3 * 5000);
    expect(v.game.hole[v.you].length).toBe(2);

    // Let the bots and the timer play a few hands; the human folds or checks when asked.
    for (let k = 0; k < 400 && room.data!.game && (room.data!.game as any).handNo < 4; k++) {
      v = a.last();
      if (v.game.phase === "betting" && v.game.turn === v.you) {
        await a.say({ t: "poker", action: v.game.canCheck ? { t: "check" } : { t: "call" } });
      } else {
        room.data!.deadline = Date.now() - 100;
        await room.onTimer();
      }
    }
    expect((room.data!.game as any).handNo).toBeGreaterThanOrEqual(4);

    // A friend opens the link mid-game and takes a bot's place.
    const b = await join("b");
    const vb = b.last();
    expect(vb.you).not.toBeNull();
    expect(vb.seats[vb.you].name).toBe("nick_b");

    // Standing up keeps them watching, and they are not sat down again on reconnect.
    await b.say({ t: "stand" });
    expect(b.last().you).toBeNull();
    const b2 = await join("b");
    expect(b2.last().you).toBeNull();
    expect(b2.last().ledger.find((r: any) => r.name === "nick_b")).toBeTruthy();

    await a.say({ t: "end" });
    expect(a.last().status).toBe("finished");
    expect(rated()).toBe(0);
  });

  it("switching a lobby between games changes the seat count", async () => {
    const { room, join } = setup();
    await room.init("SWTCH");
    const a = await join("a");
    expect(a.last().seats.length).toBe(4);
    await a.say({ t: "config", config: { game: "poker", seats: 9 } });
    expect(a.last().seats.length).toBe(9);
    await a.say({ t: "config", config: { game: "bura" } });
    expect(a.last().seats.length).toBe(4);
    expect(a.last().seats[a.last().you].name).toBe("nick_a");
    expect(room.data!.config.game).toBe("bura");
  });
});
