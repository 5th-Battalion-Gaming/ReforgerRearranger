/**
 * Reforger Load Order — private CORS relay (Cloudflare Worker)
 * ------------------------------------------------------------
 * Relays public Arma Reforger Workshop pages with a CORS consent header
 * so the browser-based load order tool can read them. Free tier allows
 * 100,000 requests/day; responses are edge-cached for 5 minutes.
 *
 * Deploys standalone (paste into a Worker) or combined with the app via
 * wrangler.jsonc — see DEPLOY.md.
 *
 * Security posture:
 *  - Forwards ONLY to the allowlisted host below (not an open proxy)
 *  - Never follows redirects, so an open redirect on the allowlisted host
 *    can't be used to reach somewhere else through this worker
 *  - GET only, and only on the relay paths below
 *  - Refuses oversized upstream responses instead of buffering them
 *  - Relayed HTML is served with `CSP: sandbox`, so if a person navigates
 *    to a relayed page directly, no scripts execute on YOUR origin.
 *    fetch()-based consumers (the app) are unaffected.
 *  - Cross-origin reads are opt-in (see ALLOWED_ORIGINS), so other websites
 *    can't quietly spend your relay quota.
 */

const ALLOWED_HOSTS = ["reforger.armaplatform.com"];

/**
 * Paths that act as the relay. Anything else gets a 404 rather than being
 * answered, so the worker isn't a catch-all for every unmatched URL.
 * "/" covers the standalone deploy (…workers.dev/?url=…); "/relay" covers
 * the combined deploy, where static files are served first and only
 * unmatched paths reach this code.
 */
const RELAY_PATHS = ["/", "/relay", "/relay/"];

/**
 * A changelog page is roughly 200 KB. Anything wildly past that isn't a page
 * we asked for, so refuse it rather than buffering it into the worker.
 */
const MAX_BYTES = 5 * 1024 * 1024;

/**
 * Origins whose JavaScript may READ relayed responses.
 *
 * The recommended combined deploy (app + relay in one Worker) is same-origin
 * and needs no CORS grant at all, so it works with this list left empty.
 *
 * Add your app's origin ONLY if you host the app somewhere other than the
 * relay, e.g.
 *   const ALLOWED_ORIGINS = ["https://reforger.example.com"];
 * Use "*" to let any website's visitors spend your relay quota.
 *
 * Note this only governs *browsers on other sites*; direct server-side
 * requests can't be origin-verified by anyone.
 */
const ALLOWED_ORIGINS = [];

const ANY_ORIGIN =
  ALLOWED_ORIGINS === "*" ||
  (Array.isArray(ALLOWED_ORIGINS) && ALLOWED_ORIGINS.includes("*"));

function corsHeaders(request) {
  const h = {
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Max-Age": "86400",
    "Vary": "Origin"
  };
  const origin = request.headers.get("Origin");
  if (ANY_ORIGIN) {
    h["Access-Control-Allow-Origin"] = "*";
  } else if (origin && ALLOWED_ORIGINS.includes(origin)) {
    h["Access-Control-Allow-Origin"] = origin;
  }
  // No header at all otherwise: the browser refuses the cross-origin read.
  return h;
}

/**
 * Read a response body, giving up if it runs past `max` bytes.
 * Returns null when the cap is hit, so the caller can answer with an error
 * instead of holding an arbitrarily large buffer in memory.
 */
async function readCapped(res, max) {
  const declared = Number(res.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > max) return null;
  if (!res.body) return new Uint8Array(0);

  const reader = res.body.getReader();
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) { await reader.cancel(); return null; }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) { out.set(c, at); at += c.byteLength; }
  return out;
}

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const CORS = corsHeaders(request);

    if (!RELAY_PATHS.includes(url.pathname)) {
      return new Response("Not found", {
        status: 404,
        headers: { "Content-Type": "text/plain", "X-Robots-Tag": "noindex" }
      });
    }

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== "GET") {
      return new Response("GET only", { status: 405, headers: CORS });
    }

    const target = url.searchParams.get("url");

    // Self-test: visiting the bare relay URL confirms deployment worked
    if (!target) {
      return new Response(
        "Reforger relay OK. Usage: ?url=<encoded armaplatform.com URL>",
        { status: 200, headers: { "Content-Type": "text/plain", ...CORS } }
      );
    }

    let t;
    try { t = new URL(target); } catch {
      return new Response("Malformed target URL", { status: 400, headers: CORS });
    }
    if (t.protocol !== "https:" || !ALLOWED_HOSTS.includes(t.hostname)) {
      return new Response("Target host not allowed", { status: 403, headers: CORS });
    }

    let upstream;
    try {
      upstream = await fetch(t.toString(), {
        headers: { "User-Agent": "Mozilla/5.0 (ReforgerLoadOrderRelay/1.0)" },
        // The allowlist above vets the URL we ASK for, not wherever it might
        // point next. Following a redirect would make this an open proxy the
        // day the allowlisted host gains an open redirect, so don't.
        redirect: "manual",
        cf: { cacheTtl: 300, cacheEverything: true } // 5-minute edge cache
      });
    } catch {
      return new Response("Upstream fetch failed", { status: 502, headers: CORS });
    }

    if (upstream.status >= 300 && upstream.status < 400) {
      return new Response("Upstream redirected; not followed", {
        status: 502, headers: CORS
      });
    }

    const body = await readCapped(upstream, MAX_BYTES);
    if (body === null) {
      return new Response("Upstream response too large", { status: 502, headers: CORS });
    }

    return new Response(body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") || "text/html; charset=utf-8",
        "Cache-Control": "public, max-age=300",
        // Relayed pages are DATA for fetch(), never a site to render on
        // this origin: sandbox disables scripts/forms if navigated to.
        "Content-Security-Policy": "sandbox",
        "X-Content-Type-Options": "nosniff",
        "X-Robots-Tag": "noindex",
        ...CORS
      }
    });
  }
};
