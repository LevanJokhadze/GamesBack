import { describe, expect, it } from "vitest";
import { applyAction, botAction, newGame, scoreSheet, sheetDealer, validateSheet } from "../src/engine";
import type { GameConfig } from "../src/engine";
import { Conflict, Forbidden, NotFound, Sheets } from "../src/offline";
import { MemoryStore, PgStore, Store, parseAvatar } from "../src/store";

const PNG = "data:image/png;base64," + Buffer.concat([Buffer.from("89504e470d0a1a0a", "hex"), Buffer.alloc(120, 1)]).toString("base64");

function playOnline(config: Partial<GameConfig>, seed: number) {
  let s = newGame(config, seed);
  const dealers: number[] = [];
  let last = -1;
  while (s.phase !== "gameOver") {
    if (s.dealsPlayed !== last) {
      dealers.push(s.dealer);
      last = s.dealsPlayed;
    }
    s = applyAction(s, s.turn, botAction(s, s.turn));
  }
  return { s, dealers };
}

describe("score sheet scoring", () => {
  it("gives exactly the online game's points for the same bids and tricks", () => {
    for (const config of [{ mode: "standard" as const, khishti: -200, pairs: false }, { mode: "nines" as const, khishti: -500, pairs: true }]) {
      for (let seed = 1; seed <= 15; seed++) {
        const { s, dealers } = playOnline(config, seed);
        const deals = s.pulkas.flatMap((p) => p.deals.map((d) => ({ bids: d.bids, took: d.took })));
        const clean = validateSheet(s.config, deals);
        const score = scoreSheet(s.config, clean);
        expect(score.complete).toBe(true);
        expect(score.totals).toEqual(s.totals);
        expect(score.pulkas).toEqual(s.pulkas);
        dealers.forEach((d, k) => expect(sheetDealer(dealers[0], k)).toBe(d));
      }
    }
  });

  it("scores a sheet that is partly written", () => {
    const cfg: GameConfig = { mode: "standard", khishti: -200, pairs: false };
    const score = scoreSheet(cfg, validateSheet(cfg, [
      { bids: [1, 1, 0, 1], took: [1, 0, 0, 0] }, // 1 card: bids may not add up to 1... they add up to 3
      { bids: [0, 1, 0, 0], took: null },
    ]));
    expect(score.done).toBe(1);
    expect(score.pulkas[0].deals[0].points).toEqual([100, -200, 50, -200]);
    expect(score.totals).toEqual([100, -200, 50, -200]);
    expect(score.complete).toBe(false);
  });

  it("rejects what can't happen", () => {
    const cfg: GameConfig = { mode: "standard", khishti: -200, pairs: false };
    expect(() => validateSheet(cfg, [{ bids: [1, 0, 0, 0], took: null }])).toThrow(/dealer/);
    expect(() => validateSheet(cfg, [{ bids: [2, 0, 0, 0], took: null }])).toThrow(/0 to 1/);
    expect(() => validateSheet(cfg, [{ bids: [0, 0, 0, 0], took: [1, 1, 0, 0] }])).toThrow(/add up/);
    expect(() => validateSheet(cfg, [{ bids: [0, 0, 0, 0], took: null }, { bids: [0, 0, 0, 0], took: null }])).toThrow(/missing/);
    expect(() => validateSheet(cfg, [{ bids: [0, 0, 0, 0.5], took: null }])).toThrow();
  });
});

function suite(name: string, make: () => Store) {
  describe(name, () => {
    it("rates an in-person game only after all four players confirm it", async () => {
      const store = make();
      await store.init();
      const run = Math.random().toString(36).slice(2, 7);
      const reg = (n: string) => store.register({ nickname: n + run, firstName: n, lastName: "Test", avatar: parseAvatar(PNG), password: "secret123" }).then((r) => r.user);
      const [a, b, c, d, e] = await Promise.all(["gio", "nika", "ana", "luka", "zura"].map(reg));
      const sheets = new Sheets(store, (u) => `/api/users/${u.id}/avatar`);

      expect((await store.searchUsers("nika" + run, 5)).map((u) => u.id)).toEqual([b.id]);
      expect((await store.searchUsers("NIK", 50)).some((u) => u.id === b.id)).toBe(true);
      expect(await store.searchUsers("  ", 5)).toEqual([]);

      await expect(sheets.create(e, { players: [a.id, b.id, c.id, d.id] })).rejects.toThrow(/one of the players/);
      await expect(sheets.create(a, { players: [a.id, a.id, c.id, d.id] })).rejects.toThrow(/only once/);
      let s = await sheets.create(a, { players: [a.id, b.id, c.id, d.id], firstDealer: 3, config: { mode: "nines", khishti: -500 } });
      expect(s).toMatchObject({ status: "recording", you: 0, canEdit: true, dealer: 3, config: { mode: "nines", khishti: -500, pairs: false } });
      expect(s.players.map((p) => p.nickname)).toEqual(["gio", "nika", "ana", "luka"].map((n) => n + run));
      await expect(sheets.get(e, s.id)).rejects.toBeInstanceOf(NotFound);
      expect((await sheets.get(b, s.id)).canEdit).toBe(false);
      await expect(sheets.setDeals(b, s.id, s.version, [])).rejects.toBeInstanceOf(Forbidden);

      // 16 deals of 9 cards: everyone bids 2 except the last, who bids 0 (sum 6, not 9).
      const deal = { bids: [2, 2, 2, 0], took: [2, 2, 2, 3] };
      s = await sheets.setDeals(a, s.id, s.version, [{ bids: deal.bids, took: null }]);
      expect(s.dealer).toBe(3);
      await expect(sheets.setDeals(a, s.id, s.version - 1, [])).rejects.toBeInstanceOf(Conflict);
      s = await sheets.setDeals(a, s.id, s.version, Array.from({ length: 15 }, () => deal));
      expect(s.dealer).toBe(2);
      await expect(sheets.submit(a, s.id, s.version)).rejects.toThrow(/16 deals/);
      s = await sheets.setDeals(a, s.id, s.version, Array.from({ length: 16 }, () => deal));
      expect(s.score.complete).toBe(true);
      expect(s.score.totals).toEqual([4 * (4 * 150 + 150), 4 * (4 * 150 + 150), 4 * (4 * 150 + 150), 16 * 30]);

      s = await sheets.submit(a, s.id, s.version);
      expect(s).toMatchObject({ status: "pending", confirmed: [a.id], mustConfirm: false });
      expect((await sheets.list(b)).find((x) => x.id === s.id)?.mustConfirm).toBe(true);

      // Nika sends it back; Gio fixes a deal and sends it again; everyone must confirm again.
      s = await sheets.confirm(c, s.id, s.version);
      s = await sheets.reject(b, s.id, s.version);
      expect(s).toMatchObject({ status: "recording", confirmed: [], rejected: { userId: b.id } });
      const fixed = Array.from({ length: 16 }, () => deal);
      fixed[0] = { bids: [2, 2, 2, 0], took: [3, 2, 2, 2] };
      s = await sheets.setDeals(a, s.id, s.version, fixed);
      s = await sheets.submit(a, s.id, s.version);
      s = await sheets.confirm(b, s.id, s.version);
      s = await sheets.confirm(c, s.id, s.version);
      expect(s.status).toBe("pending");
      expect((await store.userById(a.id))!.games).toBe(0);
      s = await sheets.confirm(d, s.id, s.version);
      expect(s.status).toBe("rated");
      expect(s.matchId).toBeTruthy();
      expect(s.ratingChanges!.every((x) => x !== null)).toBe(true);
      await expect(sheets.cancel(a, s.id)).rejects.toThrow(/rated/);
      await expect(sheets.setDeals(a, s.id, s.version, fixed)).rejects.toThrow(/closed/);
      expect((await sheets.list(a)).some((x) => x.id === s.id)).toBe(false);

      const m = (await store.match(s.matchId!))!;
      expect(m.source).toBe("offline");
      expect(m.mode).toBe("nines");
      expect(m.players.map((p) => p.score)).toEqual(s.score.totals);
      const ua = (await store.userById(a.id))!;
      expect(ua.games).toBe(1);
      expect((await store.recentGames(a.id, 1))[0]).toMatchObject({ id: s.matchId, source: "offline" });

      // Open sheets per scorekeeper are limited, and deleting frees one.
      const ids = [];
      for (let i = 0; i < 3; i++) ids.push((await sheets.create(a, { players: [a.id, b.id, c.id, d.id] })).id);
      await expect(sheets.create(a, { players: [a.id, b.id, c.id, d.id] })).rejects.toThrow(/unfinished/);
      await expect(sheets.cancel(b, ids[0])).rejects.toBeInstanceOf(Forbidden);
      await sheets.cancel(a, ids[0]);
      await sheets.create(a, { players: [a.id, b.id, c.id, d.id] });
    });
  });
}

suite("offline sheets (memory)", () => new MemoryStore());
const pgUrl = process.env.TEST_DATABASE_URL;
if (pgUrl) suite("offline sheets (postgres)", () => new PgStore(pgUrl));
