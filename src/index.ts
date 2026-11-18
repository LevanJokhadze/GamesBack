// Worker entry: small HTTP API plus WebSocket upgrade routed to the room's Durable Object.
import { Env, Room } from "./room";

export { Room };

const CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const CODE_RE = /^[A-HJ-NP-Z2-9]{5}$/;

function randomCode(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(5));
  return Array.from(bytes, (b) => CODE_ALPHABET[b % CODE_ALPHABET.length]).join("");
}

function corsHeaders(request: Request, env: Env): Record<string, string> {
  const origin = request.headers.get("Origin") ?? "";
  const allowed = (env.ALLOWED_ORIGINS ?? "*").split(",").map((s) => s.trim()).filter(Boolean);
  const ok = allowed.includes("*") || allowed.includes(origin);
  return ok && origin
    ? {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        Vary: "Origin",
      }
    : {};
}

function originAllowed(request: Request, env: Env): boolean {
  const origin = request.headers.get("Origin");
  if (!origin) return true; // non-browser clients
  const allowed = (env.ALLOWED_ORIGINS ?? "*").split(",").map((s) => s.trim());
  return allowed.includes("*") || allowed.includes(origin);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const cors = corsHeaders(request, env);
    const json = (body: unknown, status = 200) =>
      new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...cors } });

    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });

    if (url.pathname === "/" || url.pathname === "/api/health") {
      return json({ ok: true, service: "joker-backend" });
    }

    if (url.pathname === "/api/rooms" && request.method === "POST") {
      if (!originAllowed(request, env)) return json({ error: "origin not allowed" }, 403);
      const body = (await request.json().catch(() => ({}))) as { game?: string };
      const game = body?.game === "bura" ? "bura" : "joker";
      for (let attempt = 0; attempt < 6; attempt++) {
        const code = randomCode();
        const stub = env.ROOMS.get(env.ROOMS.idFromName(code));
        const res = await stub.fetch("https://room/init", {
          method: "POST",
          body: JSON.stringify({ code, game }),
        });
        if (res.ok) return json({ code }, 201);
      }
      return json({ error: "could not create room" }, 503);
    }

    const m = url.pathname.match(/^\/api\/rooms\/([A-Za-z0-9]+)(\/ws)?$/);
    if (m) {
      const code = m[1].toUpperCase();
      if (!CODE_RE.test(code)) return json({ error: "room not found" }, 404);
      const stub = env.ROOMS.get(env.ROOMS.idFromName(code));
      if (m[2]) {
        if (!originAllowed(request, env)) return new Response("origin not allowed", { status: 403 });
        return stub.fetch(new Request("https://room/ws", request));
      }
      const res = await stub.fetch("https://room/info");
      if (!res.ok) return json({ error: "room not found" }, 404);
      return json(await res.json());
    }

    return json({ error: "not found" }, 404);
  },
} satisfies ExportedHandler<Env>;
