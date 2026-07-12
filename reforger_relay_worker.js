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
 *  - GET only
 *  - Relayed HTML is served with `CSP: sandbox`, so if a person navigates
 *    to a relayed page directly, no scripts execute on YOUR origin.
 *    fetch()-based consumers (the app) are unaffected.
 *  - Optional origin lockdown (below) to stop other websites' visitors
 *    from consuming your relay quota.
 */

const ALLOWED_HOSTS = ["reforger.armaplatform.com"];

/**
 * Leave empty to allow any website's JavaScript to read relayed responses
 * (Access-Control-Allow-Origin: *). To reserve the relay for your own
 * deployments, list your app origins, e.g.:
 *   const LOCKED_ORIGINS = ["https://reforger-loadorder.yourname.workers.dev"];
 * Same-origin requests (the combined deploy) need no CORS grant and are
 * unaffected either way. Note this only stops *browsers on other sites*;
 * direct server-side requests can't be origin-verified by anyone.
 */
const LOCKED_ORIGINS = [];

function corsHeaders(request) {
  const h = {
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Max-Age": "86400"
  };
  if (LOCKED_ORIGINS.length === 0) {
    h["Access-Control-Allow-Origin"] = "*";
  } else {
    const origin = request.headers.get("Origin");
    if (origin && LOCKED_ORIGINS.includes(origin)) {
      h["Access-Control-Allow-Origin"] = origin;
      h["Vary"] = "Origin";
    }
  }
  return h;
}

export default {
  async fetch(request) {
    const CORS = corsHeaders(request);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== "GET") {
      return new Response("GET only", { status: 405, headers: CORS });
    }

    const target = new URL(request.url).searchParams.get("url");

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

    const upstream = await fetch(t.toString(), {
      headers: { "User-Agent": "Mozilla/5.0 (ReforgerLoadOrderRelay/1.0)" },
      cf: { cacheTtl: 300, cacheEverything: true } // 5-minute edge cache
    });

    const body = await upstream.arrayBuffer();
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
