/**
 * Development server: this app with hot reload, every `/api/*` call forwarded
 * to the real console.
 *
 * Proxied rather than called directly because `server.py` sends no CORS
 * headers and has no OPTIONS handler — by design, it is a local operator tool
 * that binds to 127.0.0.1 and answers only its own page. Same-origin through
 * this proxy keeps it that way while still letting the UI be developed
 * against a live worker and a live job.
 *
 *   bun run dev            # then open http://127.0.0.1:5173
 *
 * The console itself must be running and is never replaced by this: it is the
 * process that holds the Local Network grant the worker inherits.
 */

import index from "./index.html";

const CONSOLE = process.env.STREAMBOT_CONSOLE ?? "http://127.0.0.1:8787";
const PORT = Number(process.env.PORT ?? 5173);

const server = Bun.serve({
  port: PORT,
  development: { hmr: true },
  routes: {
    "/api/*": async (request) => {
      const target = new URL(request.url);
      const upstream = `${CONSOLE}${target.pathname}${target.search}`;
      try {
        // Streamed through unchanged, so the SSE feed arrives as it is
        // produced rather than buffered until the console stops talking.
        return await fetch(upstream, {
          method: request.method,
          headers: request.headers,
          body: request.body,
          // @ts-expect-error - duplex is required for a streaming body
          duplex: "half",
        });
      } catch (error) {
        return Response.json(
          { ok: false, error: `console unreachable at ${CONSOLE}` },
          { status: 502 },
        );
      }
    },
    "/*": index,
  },
});

console.log(`console UI on ${server.url} — proxying /api to ${CONSOLE}`);
