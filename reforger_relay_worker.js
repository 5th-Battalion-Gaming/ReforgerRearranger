/**
 * Reforger Load Order — private CORS relay (Cloudflare Worker)
 * ------------------------------------------------------------
 * The Reforger Workshop (reforger.armaplatform.com) sends no CORS headers,
 * so browsers can't fetch it directly from a webpage. This ~40-line Worker
 * is your own permanent relay. Cloudflare's free tier (no credit card)
 * allows 100,000 requests/day — a full refresh of a 100-mod list costs
 * about 110 requests, and responses are edge-cached for 5 minutes, so
 * real usage won't come anywhere near the limit.
 *
 * DEPLOY — see DEPLOY.md for the full walkthrough. Short version:
 *   1. https://dash.cloudflare.com -> Workers & Pages -> Create Worker
 *   2. Name it (e.g. "reforger-relay"), replace the starter code with
 *      this file, click Deploy
 *   3. Verify: open  https://<your-worker>.workers.dev/  in a browser —
 *      you should see the OK self-test message
 *   4. In reforger_loadorder.html set:
 *        const CUSTOM_RELAY = "https://<your-worker>.workers.dev/?url=";
 *
 * Security: only forwards to the allowlisted host below, so it can't be
 * abused as a general-purpose open proxy.
 */

const ALLOWED_HOSTS = ["reforger.armaplatform.com"];

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Max-Age": "86400"
};

export default {
  async fetch(request) {
    // CORS preflight (not normally needed for simple GETs, but harmless)
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS });
    }
    if (request.method !== "GET") {
      return new Response("GET only", { status: 405, headers: CORS });
    }

    const target = new URL(request.url).searchParams.get("url");

    // Self-test: visiting the bare worker URL confirms deployment worked
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
        ...CORS
      }
    });
  }
};
