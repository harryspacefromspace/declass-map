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
//
// scenes.geojson is declared as JSON so Cloudflare's edge compresses it; the
// PMTiles archive must NOT be — its tiles are individually gzipped already, and
// re-compressing would defeat the range reads that make it worth having.
const SERVABLE = new Map([
  ['scenes.geojson', 'application/json; charset=utf-8'],
  ['scenes.pmtiles', 'application/octet-stream'],
  // Military airbases and missile sites from OSM, for the overlays panel.
  ['overlays.geojson', 'application/json; charset=utf-8'],
  // Satellite/mission/camera/year counts over the whole archive. The tile map
  // can't count its own filter options — it only holds the tiles in view.
  ['facets.json', 'application/json; charset=utf-8'],
  // Catalogue of the per-mission frame files below.
  ['frames/_missions.json', 'application/json; charset=utf-8'],
]);

// One file per mission, in flight order, for the frame browser. Matched by
// shape rather than listed one by one — there are 159 of them and the set moves
// with the archive. The pattern is deliberately strict: lowercase dataset,
// alphanumeric mission, nothing else, so this can't be walked or escaped with
// dots or slashes.
const FRAME_FILE = /^frames\/[a-z0-9]+_[0-9]+[A-Z]?\.json$/;

function contentTypeFor(key) {
  if (SERVABLE.has(key)) return SERVABLE.get(key);
  if (FRAME_FILE.test(key)) return 'application/json; charset=utf-8';
  return null;
}

// PMTiles works by reading a few byte ranges out of a large archive: the header
// and directory first, then only the tiles in view. Without Range support the
// client would have to pull the whole file, which is the problem it exists to
// solve.
// Translate a Range header into an R2 range option. Deliberately does NOT need
// the object size: asking R2 for the size first would mean a HeadObject on top
// of the GetObject, i.e. two Class B operations for every tile a map draws.
// R2 clamps the range itself and reports back what it actually served.
//
// Three outcomes, because RFC 7233 treats them differently:
//   IGNORE  — not a byte range we understand; serve the whole object (200)
//   null    — well-formed but impossible; 416
//   {..}    — an R2 range option
const IGNORE = Symbol('ignore');

function rangeSpec(header) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m) return IGNORE;
  const [, rawStart, rawEnd] = m;
  if (rawStart === '' && rawEnd === '') return IGNORE;

  if (rawStart === '') {                        // suffix: last N bytes
    const n = Number(rawEnd);
    return n > 0 ? { suffix: n } : null;
  }

  const offset = Number(rawStart);
  if (rawEnd === '') return { offset };
  const end = Number(rawEnd);
  return end < offset ? null : { offset, length: end - offset + 1 };
}

// R2 signals an out-of-bounds range by throwing rather than returning null.
const isRangeError = (e) => String(e && e.message || e).toLowerCase().includes('range');

// USGS serves browse images with `X-Content-Type-Options: nosniff` and no CORS
// headers, and rejects some cross-origin request patterns outright with a 500.
// Chromium's Opaque Response Blocking then refuses to paint them in an <img>.
// Proxying them through our own origin sidesteps both: the browser sees a
// same-origin image, and we can add the caching headers USGS omits so the edge
// serves repeats without hitting them again.
const BROWSE_HOST = 'ims.cr.usgs.gov';
// declassN / DIT path segments only, so this can't be turned into a general
// open proxy for arbitrary URLs.
const BROWSE_PATH = /^\/(browse|thumbnail)\/[\w./-]+\.jpg$/i;

async function proxyBrowse(request, url, ctx) {
  const target = url.pathname.slice('/img/'.length);      // e.g. browse/DIT/…jpg
  const path = '/' + target;
  if (!BROWSE_PATH.test(path)) {
    return new Response('Not found', { status: 404 });
  }

  const upstream = `https://${BROWSE_HOST}${path}`;
  const cache = caches.default;
  const cacheKey = new Request(upstream, { method: 'GET' });

  let hit = await cache.match(cacheKey);
  if (hit) return hit;

  // A browser-like request; USGS 500s on some automated patterns.
  const res = await fetch(upstream, {
    headers: {
      'User-Agent': 'Mozilla/5.0 (declass-map image proxy)',
      'Accept': 'image/jpeg,image/*;q=0.8,*/*;q=0.5',
      'Referer': `https://${BROWSE_HOST}/`,
    },
  });
  if (!res.ok) {
    return new Response('Upstream image error', { status: 502 });
  }

  const headers = new Headers();
  headers.set('content-type', 'image/jpeg');
  // These rarely change; cache hard so the edge and browser both keep them.
  headers.set('cache-control', 'public, max-age=604800, immutable');
  headers.set('access-control-allow-origin', 'https://declass-map.spacefromspace.com');

  const out = new Response(res.body, { status: 200, headers });
  // Populate the edge cache without blocking the response.
  ctx.waitUntil(cache.put(cacheKey, out.clone()));
  return out;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname.startsWith('/img/')) {
      if (request.method !== 'GET' && request.method !== 'HEAD') {
        return new Response('Method not allowed', { status: 405 });
      }
      return proxyBrowse(request, url, ctx);
    }

    if (!url.pathname.startsWith('/data/')) {
      return env.ASSETS.fetch(request);
    }

    if (request.method !== 'GET' && request.method !== 'HEAD') {
      return new Response('Method not allowed', { status: 405 });
    }

    const key = url.pathname.slice('/data/'.length);
    const contentType = contentTypeFor(key);
    if (!contentType) {
      return new Response('Not found', { status: 404 });
    }

    // Direct navigation and same-origin requests send no Origin header, so
    // absence is fine; a *foreign* Origin is what we turn away.
    const origin = request.headers.get('Origin');
    if (origin && !ALLOWED_ORIGINS.has(origin)) {
      return new Response('Forbidden', { status: 403 });
    }

    // Headers common to every path. `obj` is whatever R2 handed back — an
    // R2Object for HEAD, an R2ObjectBody otherwise; both expose httpEtag/size.
    const respond = (obj, extra) => {
      const headers = new Headers();
      obj.writeHttpMetadata(headers);
      headers.set('etag', obj.httpEtag);
      headers.set('content-type', contentType);
      // Rebuilt once a day; revalidation is cheap thanks to the etag.
      headers.set('cache-control', 'public, max-age=1800, stale-while-revalidate=86400');
      headers.set('accept-ranges', 'bytes');
      for (const [k, v] of Object.entries(extra || {})) headers.set(k, v);
      return headers;
    };

    // Compare conditionals loosely: when Cloudflare compresses a response it
    // hands the client a WEAK etag (W/"..."), which the browser echoes back — a
    // strict comparison against our strong etag would miss and re-send the lot.
    const sameEtag = (header, etag) => {
      const ours = etag.replace(/^W\//, '');
      return header.split(',').some(t => t.trim().replace(/^W\//, '') === ours);
    };

    const rangeHeader = request.headers.get('range');
    const inm = request.headers.get('if-none-match');

    // ── HEAD: metadata only, no body to fetch ──────────────────────────────
    if (request.method === 'HEAD') {
      const head = await env.SCENES.head(key);
      if (!head) return new Response('Scene data not published yet', { status: 503 });
      if (inm && sameEtag(inm, head.httpEtag)) {
        return new Response(null, { status: 304, headers: respond(head) });
      }
      return new Response(null, {
        headers: respond(head, { 'content-length': String(head.size) }),
      });
    }

    // ── Ranged GET: the hot path, one R2 read ──────────────────────────────
    const spec = rangeHeader ? rangeSpec(rangeHeader) : IGNORE;

    if (rangeHeader && spec !== IGNORE) {
      // A well-formed but impossible range still owes the client the object
      // size, which is the one case worth a metadata lookup for.
      if (spec === null) {
        const head = await env.SCENES.head(key);
        return new Response('Range not satisfiable', {
          status: 416,
          headers: { 'content-range': `bytes */${head ? head.size : 0}` },
        });
      }

      let part;
      try {
        part = await env.SCENES.get(key, { range: spec });
      } catch (e) {
        if (!isRangeError(e)) throw e;
        const head = await env.SCENES.head(key);
        return new Response('Range not satisfiable', {
          status: 416,
          headers: { 'content-range': `bytes */${head ? head.size : 0}` },
        });
      }
      if (!part) return new Response('Scene data not published yet', { status: 503 });

      // If-Range: if the object changed under the client, the spec says send
      // the whole thing instead of a slice of something it no longer has.
      const ifRange = request.headers.get('if-range');
      if (ifRange && !sameEtag(ifRange, part.httpEtag)) {
        const whole = await env.SCENES.get(key);
        if (!whole) return new Response('Scene data not published yet', { status: 503 });
        return new Response(whole.body, { headers: respond(whole) });
      }

      // R2 reports what it actually served, which is what the client must be
      // told — it clamps, so the resolved range can differ from the request.
      const served = part.range || {};
      const offset = served.offset ?? spec.offset ?? (part.size - (spec.suffix || 0));
      const length = served.length ?? (part.size - offset);

      return new Response(part.body, {
        status: 206,
        headers: respond(part, {
          'content-range': `bytes ${offset}-${offset + length - 1}/${part.size}`,
          'content-length': String(length),
        }),
      });
    }

    // ── Whole-object GET; R2 evaluates the conditional so a 304 costs one read
    const obj = await env.SCENES.get(key, { onlyIf: inm ? request.headers : undefined });
    if (!obj) return new Response('Scene data not published yet', { status: 503 });
    // A precondition failure yields an R2Object with no body — that's the 304.
    if (!('body' in obj) || !obj.body) {
      return new Response(null, { status: 304, headers: respond(obj) });
    }
    return new Response(obj.body, { headers: respond(obj) });
  }
};
