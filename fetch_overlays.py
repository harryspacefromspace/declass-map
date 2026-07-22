#!/usr/bin/env python3
"""Build the map's overlay layers from OpenStreetMap.

The overlays used to try to pull military airbases out of the OurAirports CSV
by filtering `type == "military"`. That column only ever holds small_airport,
medium_airport, large_airport, heliport, seaplane_base, balloonport or closed —
there is no "military" value — so the filter matched nothing and the map
downloaded 12.7MB of CSV on every toggle to render zero markers.

OSM has the data properly tagged instead:
  military=airfield        ~2,350 worldwide
  bunker_type=missile_silo   ~380 worldwide

`military=bunker` is deliberately NOT used for the missile layer: it holds over
107,000 features, overwhelmingly WWII pillboxes and field fortifications, which
would bury the ICBM sites the layer exists to show.

Output is a small GeoJSON published to R2 alongside the scene data. The curated
Cold War lists stay in the front end and are merged over the top, so the
overlays still work if this file is missing.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Public instances rate-limit and time out under load; the main one 504'd on a
# global query that both mirrors answered fine. Try in order.
ENDPOINTS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

UA = ("declass-map/1.0 (https://declass-map.spacefromspace.com; "
      "harry@spacefromspace.com)")

LAYERS = {
    "airbases": 'nwr["military"="airfield"];',
    "silos": 'nwr["bunker_type"="missile_silo"];',
}

FALLBACK_NAMES = {
    "airbases": "Military airfield",
    "silos": "Missile silo",
}


def overpass(selector, timeout=240):
    """Run one Overpass query, trying each mirror before giving up."""
    query = f"[out:json][timeout:{timeout}];{selector}out center tags;"
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None

    for endpoint in ENDPOINTS:
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    endpoint, data=body,
                    headers={"User-Agent": UA,
                             "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=timeout + 60) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except (urllib.error.URLError, urllib.error.HTTPError,
                    json.JSONDecodeError, TimeoutError, OSError) as exc:
                last = exc
                host = urllib.parse.urlparse(endpoint).netloc
                print(f"  {host} attempt {attempt + 1} failed: {exc}")
                time.sleep(5)

    raise RuntimeError(f"every Overpass mirror failed; last error: {last}")


def to_features(payload, kind):
    """Overpass elements -> GeoJSON points, one per site."""
    out = []
    for el in payload.get("elements", []):
        # Nodes carry lat/lon; ways and relations get a `center` from `out center`.
        lat = el.get("lat", (el.get("center") or {}).get("lat"))
        lon = el.get("lon", (el.get("center") or {}).get("lon"))
        if lat is None or lon is None:
            continue

        tags = el.get("tags") or {}
        name = (tags.get("name:en") or tags.get("name")
                or tags.get("official_name") or FALLBACK_NAMES[kind])

        out.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            "properties": {"n": name, "k": kind},
        })
    return out


def main():
    dest = sys.argv[1] if len(sys.argv) > 1 else "overlays.geojson"

    features = []
    counts = {}
    for kind, selector in LAYERS.items():
        print(f"fetching {kind} …")
        got = to_features(overpass(selector), kind)
        # A mirror can answer 200 with an empty set when it's unhappy; treating
        # that as success would quietly publish an empty overlay.
        if not got:
            raise RuntimeError(f"{kind}: Overpass returned no usable features")
        counts[kind] = len(got)
        features.extend(got)
        print(f"  {kind}: {len(got):,} sites")

    doc = {
        "type": "FeatureCollection",
        "metadata": {
            "source": "OpenStreetMap contributors (ODbL)",
            "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "counts": counts,
        },
        "features": features,
    }

    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))

    print(f"wrote {len(features):,} features -> {dest}")


if __name__ == "__main__":
    main()
