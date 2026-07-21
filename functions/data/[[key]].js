// Serves scene data out of the private R2 bucket.
//
// The bucket has no public access and no custom domain — it is reachable only
// through this binding, so the data can't be pulled straight from R2. Because
// this is the same origin as the map, no CORS is involved either, which means
// another site's JavaScript can't fetch it.
//
// Requests are still readable by anyone who opens devtools on our own page —
// that's unavoidable while the browser renders the data — but off-site
// embedding and bucket enumeration are both closed off.

const ALLOWED = new Set([
  'https://declass-map.spacefromspace.com',
  'http://localhost:8899',      // local testing
  'http://127.0.0.1:8899'
]);

// Only these keys are reachable, so the Function can never be used to walk the
// bucket even if something else is written into it.
const SERVABLE = new Set(['scenes.geojson']);

export async function onRequestGet(ctx) {
  const key = (ctx.params.key || []).join('/');

  if (!SERVABLE.has(key)) {
    return new Response('Not found', { status: 404 });
  }

  // Block other sites embedding this. Direct navigation and same-origin
  // requests send no Origin header, so absence is allowed; a *foreign* Origin
  // is what we reject.
  const origin = ctx.request.headers.get('Origin');
  if (origin && !ALLOWED.has(origin)) {
    return new Response('Forbidden', { status: 403 });
  }

  const obj = await ctx.env.SCENES.get(key);
  if (!obj) {
    return new Response('Scene data not published yet', { status: 503 });
  }

  const headers = new Headers();
  obj.writeHttpMetadata(headers);          // carries contentType/encoding from upload
  headers.set('etag', obj.httpEtag);
  // application/json rather than geo+json: Cloudflare's edge compresses known
  // text types automatically, which takes the transfer from ~55MB to ~5MB.
  headers.set('content-type', 'application/json; charset=utf-8');
  // Rebuilt once a day; revalidation is cheap because of the etag.
  headers.set('cache-control', 'public, max-age=1800, stale-while-revalidate=86400');

  // Honour conditional requests so repeat visits cost a 304, not 55 MB.
  if (ctx.request.headers.get('if-none-match') === obj.httpEtag) {
    return new Response(null, { status: 304, headers });
  }

  return new Response(obj.body, { headers });
}
