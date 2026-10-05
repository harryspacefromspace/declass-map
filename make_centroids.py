#!/usr/bin/env python3
"""Reduce the scene footprints to one point each, for the zoomed-out count layer.

Below about zoom 5 a footprint is smaller than a pixel and every one of them
lands in the same tile — z0 came out at 3.8MB when we tried it. So the far-out
zooms show counts instead of geometry, and tippecanoe builds those by clustering
these points and summing the tallies below.

Each point carries three counts rather than one so the scanned/unscanned toggle
still works when zoomed out: n (all), ns (scanned), nu (not scanned).
"""
import json
import re
import sys
from datetime import date


def centroid(geom):
    """Bounding-box centre of a footprint's outer ring."""
    if not geom:
        return None
    kind = geom.get("type")
    if kind == "Polygon":
        rings = geom.get("coordinates") or []
    elif kind == "MultiPolygon":
        rings = [r for poly in geom.get("coordinates") or [] for r in poly]
    else:
        return None
    if not rings or not rings[0]:
        return None

    xs = [pt[0] for pt in rings[0]]
    ys = [pt[1] for pt in rings[0]]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)

    # A footprint crossing the antimeridian has a bbox spanning most of the
    # globe, so its centre would land in the wrong ocean. There are few enough
    # that dropping them from the counts beats smearing them across the Pacific.
    if x1 - x0 > 180:
        return None
    return ((x0 + x1) / 2.0, (y0 + y1) / 2.0)


def slug(name):
    """Satellite name as a property-safe token; tiles.html builds the same one."""
    return re.sub(r"[^A-Za-z0-9]", "", name or "")


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "available_scenes.geojson"
    dest = sys.argv[2] if len(sys.argv) > 2 else "centroids.geojson"

    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)

    features = data.get("features") or []
    written = skipped = 0
    keys = set()

    # Newline-delimited GeoJSON: tippecanoe reads it streaming, and it keeps us
    # from holding a second full copy in memory.
    with open(dest, "w", encoding="utf-8") as out:
        for feat in features:
            point = centroid(feat.get("geometry"))
            if point is None:
                skipped += 1
                continue
            props = feat.get("properties", {})
            scanned = props.get("scanned") is not False
            # Days since 1970 that the scene became downloadable (0 if unknown),
            # summed as a max per cluster so "recently available" can hide cells
            # with nothing new in them.
            fsa = str(props.get("firstSeenAvailable") or "")[:10]
            try:
                fsa_day = (date.fromisoformat(fsa) - date(1970, 1, 1)).days
            except ValueError:
                fsa_day = 0
            year = str(props.get("acquisitionDate") or "")[:4]
            # One tally per satellite/year/scanned combination, so the filters
            # for those can still be applied to a cluster that has no features
            # left to filter. Marginals can't be recombined, hence the product.
            key = f"c_{slug(props.get('satellite'))}_{year}_{'s' if scanned else 'u'}"
            keys.add(key)
            out.write(json.dumps({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [point[0], point[1]]},
                "properties": {
                    "n": 1,
                    "ns": 1 if scanned else 0,
                    "nu": 0 if scanned else 1,
                    "fsa": fsa_day,
                    key: 1,
                },
            }, separators=(",", ":")))
            out.write("\n")
            written += 1

    print(f"centroids: {written:,} written, {skipped:,} skipped "
          f"(antimeridian or no geometry) -> {dest}")

    # tippecanoe has no wildcard for --accumulate-attribute, so hand the build
    # the exact list of tally keys it needs to sum.
    with open(dest + ".attrs", "w", encoding="utf-8") as fh:
        fh.write(" ".join(f"--accumulate-attribute={k}:sum" for k in sorted(keys)))
    print(f"centroids: {len(keys)} tally keys -> {dest}.attrs")

    if not written:
        print("centroids: refusing to continue with an empty count layer")
        sys.exit(1)


if __name__ == "__main__":
    main()
