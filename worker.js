// Serves the map (static assets) and its scene data (private R2 bucket).
//
// The bucket has no public access and no custom domain — it is reachable only
// through the SCENES binding below, so the data can't be pulled straight from
// R2. Because it's served from the same origin as the map there's no CORS for
// another site's JavaScript to use either.
//
// Anyone who opens devtools on our own page can still see the request — that's
// unavoidable while the browser renders the data — but off-site embedding and
// bucket enumeration are both closed off.

const ALLOWED_ORIGINS = new Set([
  'https://declass-map.spacefromspace.com',
  'http://localhost:8788',      // wrangler dev
  'http://127.0.0.1:8788'
]);

// Only these keys are reachable, so this can never be used to walk the bucket
// even if something else ends up written into it.
const SERVABLE = new Set(['scenes.geojson']);

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (!url.pathname.startsWith('/data/')) {
      return env.ASSETS.fetch(request);
    }

    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method not allowed', { status: 405 });
    }

    const key = url.pathname.slice('/data/'.length);
    if (!SERVABLE.has(key)) {
      return new Response('Not found', { status: 404 });
    }

    // Direct navigation and same-origin requests send no Origin header, so
    // absence is fine; a *foreign* Origin is what we turn away.
    const origin = request.headers.get('Origin');
    if (origin && !ALLOWED_ORIGINS.has(origin)) {
      return new Response('Forbidden', { status: 403 });
    }

    const obj = await env.SCENES.get(key);
    if (!obj) {
      return new Response('Scene data not published yet', { status: 503 });
    }

    const headers = new Headers();
    obj.writeHttpMetadata(headers);
    headers.set('etag', obj.httpEtag);
    // application/json rather than geo+json: Cloudflare's edge compresses known
    // text types automatically, taking this from ~55MB to ~5MB on the wire.
    headers.set('content-type', 'application/json; charset=utf-8');
    // Rebuilt once a day; revalidation is cheap thanks to the etag.
    headers.set('cache-control', 'public, max-age=1800, stale-while-revalidate=86400');

    // Honour conditional requests so a repeat visit costs a 304, not 55MB.
    if (request.headers.get('if-none-match') === obj.httpEtag) {
      return new Response(null, { status: 304, headers });
    }

    return new Response(request.method === 'HEAD' ? null : obj.body, { headers });
  }
};
