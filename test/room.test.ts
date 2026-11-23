import { describe, expect, it, vi } from "vitest";
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

    // Audio through the server goes only to people in voice, and only from someone in voice.
    const bBefore = b.sent.length;
    const wBefore = w.sent.length;
    await room.onMessage(a.c, JSON.stringify({ t: "pcm", d: "AAAA" }));
    expect(b.sent.slice(bBefore)).toEqual([]); // b has not joined voice
    await room.onMessage(b.c, JSON.stringify({ t: "voice", on: true }));
    const b2 = b.sent.length;
    await room.onMessage(a.c, JSON.stringify({ t: "pcm", d: "AAAA" }));
    expect(b.sent.slice(b2)).toEqual([{ t: "pcm", from: peerOf("u_a"), d: "AAAA" }]);
    expect(w.sent.slice(wBefore).some((m) => m.t === "pcm")).toBe(false);
    const a2 = a.sent.length;
    await room.onMessage(w.c, JSON.stringify({ t: "pcm", d: "AAAA" })); // w is not in voice
    expect(a.sent.slice(a2).some((m) => m.t === "pcm")).toBe(false);
  });
});

describe("players who stop responding", () => {
  it("warns, hands the seat to a bot (-5), and removes the player after 2 more minutes (-25)", async () => {
    const { vi } = await import("vitest");
    vi.useFakeTimers({ toFake: ["Date"] });
    try {
      const conns: Conn[] = [];
      const penalties: [string, number, string][] = [];
      const rating = new Map<string, number>([["a", 1500], ["b", 1500]]);
      const host: RoomHost = {
        connections: () => conns,
        persist: async () => {},
        setTimer: async () => {},
        resolveUser: async (token) => ({ id: token, name: `nick_${token}`, avatarUrl: null, rating: 1500 }),
        penalize: async (userId, amount, reason) => {
          penalties.push([userId, amount, reason]);
          rating.set(userId, rating.get(userId)! - amount);
          return rating.get(userId)!;
        },
      };
      const room = new RoomCore(host, null);
      await room.init("AFKAB");
      const sent: any[] = [];
      const ca = fakeConn(sent);
      const cb = fakeConn([]);
      conns.push(ca, cb);
      await room.onMessage(ca, JSON.stringify({ t: "hello", token: "a" }));
      await room.onMessage(cb, JSON.stringify({ t: "hello", token: "b" }));
      await room.onMessage(ca, JSON.stringify({ t: "start" }));
      const view = () => [...sent].reverse().find((m) => m.t === "state")!.room;
      const seatOf = (name: string) => view().seats.findIndex((s: any) => s?.name === name);

      // Let the bots play until it is a human's turn.
      const humanTurn = async () => {
        for (let i = 0; i < 50; i++) {
          const v = view();
          const s = v.seats[v.game.turn];
          if (s && !s.bot && !s.afkKickAt) return v.game.turn as number;
          vi.setSystemTime(Date.now() + 2_000);
          await room.onTimer();
        }
        throw new Error("no human turn");
      };
      const seat = await humanTurn();
      const who = view().seats[seat].name as string;
      const uid = who.replace("nick_", "");

      // 30 s: warning, one more minute, nobody penalized yet.
      vi.setSystemTime(Date.now() + 31_000);
      await room.onTimer();
      expect(view().turnStage).toBe("warn");
      expect(view().chat.at(-1).sys).toMatchObject({ kind: "warn", name: who });
      expect(penalties).toEqual([]);

      // Another minute: -5 and a bot plays the seat.
      vi.setSystemTime(Date.now() + 61_000);
      await room.onTimer();
      expect(penalties).toEqual([[uid, 5, "afk"]]);
      expect(view().seats[seat].afkKickAt).toBeGreaterThan(Date.now());
      expect(view().chat.some((m: any) => m.sys?.kind === "afk" && m.sys.penalty === 5)).toBe(true);

      // Coming back stops the kick clock.
      const conn = uid === "a" ? ca : cb;
      await room.onMessage(conn, JSON.stringify({ t: "back" }));
      expect(view().seats[seat].afkKickAt).toBeNull();
      expect(view().chat.at(-1).sys).toMatchObject({ kind: "back" });

      // Stop responding again: warn, bot (-5), then 2 minutes later removed (-25).
      const seat2 = await humanTurn();
      const uid2 = view().seats[seat2].name.replace("nick_", "");
      vi.setSystemTime(Date.now() + 31_000);
      await room.onTimer();
      vi.setSystemTime(Date.now() + 61_000);
      await room.onTimer();
      expect(view().seats[seat2].afkKickAt).not.toBeNull();
      vi.setSystemTime(Date.now() + 121_000);
      await room.onTimer();
      expect(penalties.slice(1)).toEqual([[uid2, 5, "afk"], [uid2, 25, "abandoned"]]);
      expect(view().seats[seat2]).toMatchObject({ left: true, bot: true });
      expect(view().chat.some((m: any) => m.sys?.kind === "kicked" && m.sys.penalty === 25)).toBe(true);
      expect(view().status).toBe("playing");
      expect(seatOf("nick_" + uid2)).toBe(seat2);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("bura rooms", () => {
  it("plays a whole Bura match in pairs with bots and rates it as mode bura", async () => {
    vi.useFakeTimers();
    try {
      const { buraBot } = await import("../src/bura");
      const conns: Conn[] = [];
      let rated: any = null;
      const host: RoomHost = {
        connections: () => conns,
        persist: async () => {},
        setTimer: async () => {},
        resolveUser: async (token) => ({ id: token, name: `nick_${token}`, avatarUrl: null, rating: 1500 }),
        rateGame: async (g) => {
          rated = g;
          return { matchId: "m1", changes: [null, null, null, null] };
        },
      };
      const room = new RoomCore(host, null);
      await room.init("BURAX", "bura");
      expect(room.data!.config).toMatchObject({ game: "bura", pairs: true });
      const sent: any[] = [];
      const c = fakeConn(sent);
      conns.push(c);
      await room.onMessage(c, JSON.stringify({ t: "hello", token: "a" }));
      // Pairs can't be switched off for Bura.
      await room.onMessage(c, JSON.stringify({ t: "config", config: { pairs: false } }));
      expect(room.data!.config.pairs).toBe(true);
      await room.onMessage(c, JSON.stringify({ t: "start" }));
      const last = () => [...sent].reverse().find((m) => m.t === "state")?.room;
      expect(last().game.kind).toBe("bura");
      expect(last().game.hand).toHaveLength(5);
      // Other players' hands never reach this player.
      expect(JSON.stringify(last()).match(/"hand":/g)).toHaveLength(1);
      for (let i = 0; i < 5000 && room.data!.status === "playing"; i++) {
        const g: any = room.data!.game;
        if (g.phase === "playing" && g.turn === 0) {
          await room.onMessage(c, JSON.stringify({ t: "bura", action: buraBot(g, 0) }));
        } else {
          vi.setSystemTime(Date.now() + 10_000);
          await room.onTimer();
        }
      }
      expect(room.data!.status).toBe("finished");
      expect(rated).toMatchObject({ mode: "bura", pairs: true });
      expect(Math.max(...rated.totals)).toBeGreaterThanOrEqual(11);
      expect(rated.totals[0]).toBe(rated.totals[2]);
      expect(sent.some((m) => m.t === "error")).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });
});
