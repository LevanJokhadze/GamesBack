import { describe, expect, it } from "vitest";
import { MemoryStore, PgStore, Store, parseAvatar } from "../src/store";

// 1x1 PNG
const PNG = "data:image/png;base64," + Buffer.concat([Buffer.from("89504e470d0a1a0a", "hex"), Buffer.alloc(120, 1)]).toString("base64");

function suite(name: string, make: () => Store) {
  describe(name, () => {
    it("registers, rates a game and ranks players", async () => {
      const s = make();
      await s.init();
      const a = await s.register("Levan", "Jokhadze", parseAvatar(PNG));
      const b = await s.register("Nika", "Test", parseAvatar(PNG));
      expect(a.user.rating).toBe(1500);
      expect((await s.userByToken(a.token))?.id).toBe(a.user.id);
      expect(await s.userByToken("nope-nope-nope-nope-nope")).toBeNull();

      const changes = await s.rateGame({ code: "ABCDE", mode: "standard", userIds: [a.user.id, null, b.user.id, null], totals: [900, 400, 100, -300] });
      expect(changes[1]).toBeNull();
      expect(changes[0]!.delta).toBeGreaterThan(0);
      expect(changes[2]!.after).toBe(1500 + changes[2]!.delta);

      const me = await s.userById(a.user.id);
      expect(me).toMatchObject({ games: 1, wins: 1, rating: changes[0]!.after });
      const lb = await s.leaderboard(10);
      const mine = lb.find((u) => u.id === a.user.id)!;
      expect(mine.rank).toBeLessThan(lb.find((u) => u.id === b.user.id)!.rank);
      const games = await s.recentGames(a.user.id, 5);
      expect(games[0]).toMatchObject({ place: 1, score: 900, ratingBefore: 1500 });

      const up = await s.updateProfile(a.user.id, { firstName: "Levani", avatar: parseAvatar(PNG) });
      expect(up.firstName).toBe("Levani");
      expect(up.avatarVersion).toBe(2);
      expect((await s.avatar(a.user.id))?.type).toBe("image/png");
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
