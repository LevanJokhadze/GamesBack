import { describe, expect, it } from "vitest";
import { Conn, RoomCore, RoomHost, peerOf } from "../src/room-core";

function fakeConn(sent: any[]): Conn {
  let id: string | null = null;
  return {
    send: (t) => sent.push(JSON.parse(t)),
    close: () => {},
    getPlayerId: () => id,
    setPlayerId: (v) => (id = v),
  };
}

function setup() {
  const conns: Conn[] = [];
  const host: RoomHost = {
    connections: () => conns,
    persist: async () => {},
    setTimer: async () => {},
    resolveUser: async (token) => ({ id: token, name: `nick_${token}`, avatarUrl: null, rating: 1500 }),
  };
  const room = new RoomCore(host, null);
  const join = async (token: string) => {
    const sent: any[] = [];
    const c = fakeConn(sent);
    conns.push(c);
    await room.onMessage(c, JSON.stringify({ t: "hello", token }));
    return { c, sent, last: () => [...sent].reverse().find((m) => m.t === "state")?.room };
  };
  return { room, join };
}

describe("room people, spectators, chat and voice relay", () => {
  it("seats the first four, lets later people watch without cards, and relays voice signalling", async () => {
    const { room, join } = setup();
    await room.init("ABCDE");
    const a = await join("a");
    const b = await join("b");
    await room.onMessage(a.c, JSON.stringify({ t: "start" }));
    expect(a.last().status).toBe("playing");

    const w = await join("w"); // joins a started table: spectator
    const view = w.last();
    expect(view.you).toBeNull();
    expect(view.game.hand).toEqual([]);
    expect(view.people.map((p: any) => p.name).sort()).toEqual(["nick_a", "nick_b", "nick_w"]);
    expect(view.people.find((p: any) => p.name === "nick_w").seat).toBeNull();
    // No player ids leak to clients.
    expect(JSON.stringify(view)).not.toContain("u_a");

    await room.onMessage(w.c, JSON.stringify({ t: "chat", text: "hi all" }));
    expect(a.last().chat.at(-1)).toMatchObject({ name: "nick_w", seat: null, text: "hi all" });

    await room.onMessage(a.c, JSON.stringify({ t: "voice", on: true }));
    expect(b.last().people.find((p: any) => p.name === "nick_a").voice).toBe(true);

    const before = b.sent.length;
    await room.onMessage(a.c, JSON.stringify({ t: "rtc", to: peerOf("u_b"), data: { sdp: "x" } }));
    expect(b.sent.slice(before)).toEqual([{ t: "rtc", from: peerOf("u_a"), data: { sdp: "x" } }]);
    expect(w.sent.some((m) => m.t === "rtc")).toBe(false);

    await room.onMessage(a.c, JSON.stringify({ t: "talk", on: true }));
    expect(w.sent.at(-1)).toEqual({ t: "talk", peer: peerOf("u_a"), on: true });
  });
});
