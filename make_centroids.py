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
import sys


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


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "available_scenes.geojson"
    dest = sys.argv[2] if len(sys.argv) > 2 else "centroids.geojson"

    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)

    features = data.get("features") or []
    written = skipped = 0

    # Newline-delimited GeoJSON: tippecanoe reads it streaming, and it keeps us
    # from holding a second full copy in memory.
    with open(dest, "w", encoding="utf-8") as out:
        for feat in features:
            point = centroid(feat.get("geometry"))
            if point is None:
                skipped += 1
                continue
            scanned = feat.get("properties", {}).get("scanned") is not False
            out.write(json.dumps({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [point[0], point[1]]},
                "properties": {
                    "n": 1,
                    "ns": 1 if scanned else 0,
                    "nu": 0 if scanned else 1,
                },
            }, separators=(",", ":")))
            out.write("\n")
            written += 1

    print(f"centroids: {written:,} written, {skipped:,} skipped "
          f"(antimeridian or no geometry) -> {dest}")

    if not written:
        print("centroids: refusing to continue with an empty count layer")
        sys.exit(1)


if __name__ == "__main__":
    main()
