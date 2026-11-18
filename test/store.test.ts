import { describe, expect, it } from "vitest";
import { MemoryStore, PgStore, Store, parseAvatar } from "../src/store";

// 1x1 PNG
const PNG = "data:image/png;base64," + Buffer.concat([Buffer.from("89504e470d0a1a0a", "hex"), Buffer.alloc(120, 1)]).toString("base64");

function suite(name: string, make: () => Store) {
  describe(name, () => {
    it("registers, rates a game and ranks players", async () => {
      const s = make();
      await s.init();
      const run = Math.random().toString(36).slice(2, 7);
      const a = await s.register({ nickname: "levan_j" + run, firstName: "Levan", lastName: "Jokhadze", avatar: parseAvatar(PNG), password: "secret123" });
      const b = await s.register({ nickname: "ნიკა" + run, firstName: "Nika", lastName: "Test", avatar: parseAvatar(PNG), password: "secret123" });
      await expect(s.register({ nickname: ("levan_j" + run).toUpperCase(), firstName: "X", lastName: "Y", avatar: parseAvatar(PNG), password: "secret123" })).rejects.toThrow(/taken/);
      await expect(s.register({ nickname: "a b", firstName: "X", lastName: "Y", avatar: parseAvatar(PNG), password: "secret123" })).rejects.toThrow(/Nickname/);
      expect(a.user.rating).toBe(1500);
      expect((await s.userByToken(a.token))?.id).toBe(a.user.id);
      expect(await s.userByToken("nope-nope-nope-nope-nope")).toBeNull();

      const { matchId, changes } = await s.rateGame({
        code: "ABCDE", mode: "standard", khishti: -200, pairs: false,
        userIds: [a.user.id, null, b.user.id, null], names: ["levan_j", "Bot Gio", "ნიკა", "Bot Luka"],
        totals: [900, 400, 100, -300], pulkas: [{ deals: [], total: [1, 2, 3, 4] }],
      });
      expect(changes[1]).toBeNull();
      expect(changes[0]!.delta).toBeGreaterThan(0);
      expect(changes[2]!.after).toBe(1500 + changes[2]!.delta);

      const me = await s.userById(a.user.id);
      expect(me).toMatchObject({ games: 1, wins: 1, rating: changes[0]!.after });
      const lb = await s.leaderboard(1000);
      const mine = lb.find((u) => u.id === a.user.id)!;
      expect(mine.rank).toBeLessThan(lb.find((u) => u.id === b.user.id)!.rank);
      const games = await s.recentGames(a.user.id, 5);
      expect(games[0]).toMatchObject({ id: matchId, place: 1, score: 900, ratingBefore: 1500 });
      expect(games[0].players.map((p) => p.name)).toEqual(["levan_j", "Bot Gio", "ნიკა", "Bot Luka"]);
      expect(await s.recentGames(a.user.id, 5, games[0].finishedAt)).toEqual([]);
      const m = (await s.match(matchId))!;
      expect(m.players[2]).toMatchObject({ name: "ნიკა", place: 3, bot: false, ratingBefore: 1500 });
      expect(m.players[1]).toMatchObject({ bot: true, ratingAfter: null });
      expect(m.pulkas).toEqual([{ deals: [], total: [1, 2, 3, 4] }]);

      // Log in from another device with the password; both sessions keep working.
      await expect(s.login(a.user.nickname.toUpperCase(), "wrong-pass")).rejects.toThrow(/Wrong/);
      await expect(s.login("nobody_" + run, "secret123")).rejects.toThrow(/Wrong/);
      const second = await s.login(a.user.nickname.toUpperCase(), "secret123");
      expect(second.user.id).toBe(a.user.id);
      expect(second.user.hasPassword).toBe(true);
      expect((await s.userByToken(second.token))?.id).toBe(a.user.id);
      expect((await s.userByToken(a.token))?.id).toBe(a.user.id);
      await s.logout(second.token);
      expect(await s.userByToken(second.token)).toBeNull();
      await s.updateProfile(a.user.id, { password: "newpass1" });
      await expect(s.login(a.user.nickname, "secret123")).rejects.toThrow(/Wrong/);
      expect((await s.login(a.user.nickname, "newpass1")).user.id).toBe(a.user.id);
      await expect(s.register({ nickname: "short_" + run, firstName: "X", lastName: "Y", avatar: parseAvatar(PNG), password: "123" })).rejects.toThrow(/Password/);

      const up = await s.updateProfile(a.user.id, { firstName: "Levani", nickname: "levani" + run, avatar: parseAvatar(PNG), password: "secret123" });
      expect(up.firstName).toBe("Levani");
      expect(up.nickname).toBe("levani" + run);
      expect(up.avatarVersion).toBe(2);
      expect((await s.avatar(a.user.id))?.type).toBe("image/png");
    });

    it("rates a pairs game by team", async () => {
      const s = make();
      await s.init();
      const tag = Math.random().toString(36).slice(2, 8);
      const a = await s.register({ nickname: "pa_" + tag, firstName: "A", lastName: "A", avatar: parseAvatar(PNG), password: "secret123" });
      const b = await s.register({ nickname: "pb_" + tag, firstName: "B", lastName: "B", avatar: parseAvatar(PNG), password: "secret123" });
      // a (seat 0) and b (seat 2) are partners; a scored little but the team won 600 to 500.
      const { matchId, changes } = await s.rateGame({
        code: "PAIRS", mode: "standard", khishti: -200, pairs: true,
        userIds: [a.user.id, null, b.user.id, null], names: ["a", "Bot Gio", "b", "Bot Luka"],
        totals: [-100, 400, 700, 100], pulkas: [],
      });
      expect(changes[0]!.delta).toBe(20);
      expect(changes[2]!.delta).toBe(20);
      const m = (await s.match(matchId))!;
      expect(m.pairs).toBe(true);
      expect(m.players.map((p) => p.place)).toEqual([1, 2, 1, 2]);
      expect((await s.userById(a.user.id))!.wins).toBe(1);
      expect((await s.recentGames(a.user.id, 1))[0]).toMatchObject({ pairs: true, place: 1, score: -100 });
    });
  });
}

suite("MemoryStore", () => new MemoryStore());
if (process.env.TEST_DATABASE_URL) suite("PgStore", () => new PgStore(process.env.TEST_DATABASE_URL!));

describe("parseAvatar", () => {
  it("rejects non-images and missing photos", () => {
    expect(() => parseAvatar("")).toThrow();
    expect(() => parseAvatar("data:image/png;base64," + Buffer.alloc(200).toString("base64"))).toThrow();
  });
});
