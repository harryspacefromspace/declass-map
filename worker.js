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
]);

// PMTiles works by reading a few byte ranges out of a large archive: the header
// and directory first, then only the tiles in view. Without Range support the
// client would have to pull the whole file, which is the problem it exists to
// solve.
// Three outcomes, because RFC 7233 treats them differently:
//   IGNORE  — not a byte range we understand; serve the whole object (200)
//   null    — well-formed but unsatisfiable; 416
//   {..}    — a range to read
const IGNORE = Symbol('ignore');

function parseRange(header, size) {
  const m = /^bytes=(\d*)-(\d*)$/.exec(header.trim());
  if (!m) return IGNORE;
  const [, rawStart, rawEnd] = m;
  if (rawStart === '' && rawEnd === '') return IGNORE;

  if (rawStart === '') {                        // suffix: last N bytes
    const n = Number(rawEnd);
    if (n <= 0) return null;
    const length = Math.min(n, size);
    return { offset: size - length, length };
  }

  const offset = Number(rawStart);
  if (offset >= size) return null;
  const end = rawEnd === '' ? size - 1 : Math.min(Number(rawEnd), size - 1);
  if (end < offset) return null;
  return { offset, length: end - offset + 1 };
}

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
    const contentType = SERVABLE.get(key);

    // Direct navigation and same-origin requests send no Origin header, so
    // absence is fine; a *foreign* Origin is what we turn away.
    const origin = request.headers.get('Origin');
    if (origin && !ALLOWED_ORIGINS.has(origin)) {
      return new Response('Forbidden', { status: 403 });
    }

    // Need the size before a range can be resolved, and a HEAD never needs the
    // body — one metadata lookup covers both.
    const head = await env.SCENES.head(key);
    if (!head) {
      return new Response('Scene data not published yet', { status: 503 });
    }

    const headers = new Headers();
    head.writeHttpMetadata(headers);
    headers.set('etag', head.httpEtag);
    headers.set('content-type', contentType);
    // Rebuilt once a day; revalidation is cheap thanks to the etag.
    headers.set('cache-control', 'public, max-age=1800, stale-while-revalidate=86400');
    headers.set('accept-ranges', 'bytes');

    // Compare conditionals loosely: when Cloudflare compresses a response it
    // hands the client a WEAK etag (W/"..."), which the browser echoes back — a
    // strict comparison against our strong etag would miss and re-send the lot.
    const sameEtag = (header) => {
      const ours = head.httpEtag.replace(/^W\//, '');
      return header.split(',').some(t => t.trim().replace(/^W\//, '') === ours);
    };

    const rangeHeader = request.headers.get('range');

    // A conditional GET only short-circuits when the whole object was asked
    // for; a ranged request has its own freshness rules via If-Range.
    const inm = request.headers.get('if-none-match');
    if (inm && !rangeHeader && sameEtag(inm)) {
      return new Response(null, { status: 304, headers });
    }

    if (rangeHeader) {
      // If-Range: serve the range only if the object hasn't changed underneath
      // the client, otherwise fall back to the full body as the spec requires.
      const ifRange = request.headers.get('if-range');
      if (!ifRange || sameEtag(ifRange)) {
        const range = parseRange(rangeHeader, head.size);
        if (range === null) {
          return new Response('Range not satisfiable', {
            status: 416,
            headers: { 'content-range': `bytes */${head.size}` },
          });
        }
        // Anything we don't understand falls through to the full object.
        if (range !== IGNORE) {
          const part = await env.SCENES.get(key, { range });
          if (!part) return new Response('Scene data not published yet', { status: 503 });

          const end = range.offset + range.length - 1;
          headers.set('content-range', `bytes ${range.offset}-${end}/${head.size}`);
          headers.set('content-length', String(range.length));
          return new Response(request.method === 'HEAD' ? null : part.body,
                              { status: 206, headers });
        }
      }
    }

    if (request.method === 'HEAD') {
      headers.set('content-length', String(head.size));
      return new Response(null, { headers });
    }

    const obj = await env.SCENES.get(key);
    if (!obj) return new Response('Scene data not published yet', { status: 503 });
    return new Response(obj.body, { headers });
  }
};
