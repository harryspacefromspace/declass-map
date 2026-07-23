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
    "airbases":  'nwr["military"="airfield"];',
    "silos":     'nwr["bunker_type"="missile_silo"];',
    # Reactors doubled as plutonium sources — prime reconnaissance targets.
    "nuclear":   'nwr["power"="plant"]["plant:source"="nuclear"];',
    # Submarine pens, especially SSBN, were top imaging priorities.
    "naval":     'nwr["military"="naval_base"];',
    "spaceport": 'nwr["aeroway"="spaceport"];',
}

FALLBACK_NAMES = {
    "airbases":  "Military airfield",
    "silos":     "Missile silo",
    "nuclear":   "Nuclear power plant",
    "naval":     "Naval base",
    "spaceport": "Spaceport",
}

# Nuclear test sites are curated, not from OSM: the tag that exists
# (military=nuclear_explosion_site) marks ~2,800 individual shot points, which
# would bury the map under Nevada and Semipalatinsk. What's useful is the ~19
# test *ranges* themselves, which are famous and stable — so they're listed by
# hand. (name, lat, lon)
TEST_SITES = [
    ("Nevada Test Site (NNSS)",              37.116, -116.056),
    ("Semipalatinsk Test Site (Polygon)",    50.430,   77.810),
    ("Novaya Zemlya Test Site",              73.400,   54.800),
    ("Lop Nur Test Base",                    41.530,   88.300),
    ("Bikini Atoll",                         11.600,  165.380),
    ("Enewetak Atoll",                       11.500,  162.330),
    ("Maralinga (Australia)",               -30.170,  131.620),
    ("Emu Field (Australia)",               -28.530,  132.420),
    ("Montebello Islands (Australia)",      -20.420,  115.550),
    ("Mururoa Atoll",                       -21.850, -138.900),
    ("Fangataufa Atoll",                    -22.240, -138.750),
    ("Reggane (Algeria)",                    26.310,    0.060),
    ("In Ekker (Algeria)",                   24.060,    5.050),
    ("Pokhran Range (India)",                27.080,   71.720),
    ("Ras Koh / Chagai (Pakistan)",          28.830,   64.770),
    ("Kiritimati / Christmas Island",         1.870, -157.400),
    ("Johnston Atoll",                       16.730, -169.530),
    ("Amchitka Island (Alaska)",             51.470,  179.100),
    ("Malden Island (Grapple)",              -4.030, -154.980),
]


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
        # Best-effort per layer: one Overpass query timing out shouldn't drop
        # the others. The floor check below refuses to publish if too little
        # came back overall, so a bad Overpass day keeps the previous file.
        try:
            got = to_features(overpass(selector), kind)
        except Exception as exc:
            print(f"  {kind}: FAILED ({exc}) — skipping this run")
            continue
        if not got:
            print(f"  {kind}: no features returned — skipping")
            continue
        counts[kind] = len(got)
        features.extend(got)
        print(f"  {kind}: {len(got):,} sites")

    # Curated nuclear test sites, always present (no network dependency).
    for name, lat, lon in TEST_SITES:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(lon, 5), round(lat, 5)]},
            "properties": {"n": name, "k": "testsite"},
        })
    counts["testsite"] = len(TEST_SITES)
    print(f"  testsite: {len(TEST_SITES)} sites (curated)")

    # If Overpass gave us almost nothing, don't overwrite a good file with a
    # near-empty one — CI keeps the last upload.
    osm_total = sum(v for k, v in counts.items() if k != "testsite")
    if osm_total < 100:
        raise RuntimeError(
            f"only {osm_total} OSM overlay features — refusing to publish")

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
