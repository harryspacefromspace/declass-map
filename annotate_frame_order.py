#!/usr/bin/env python3
"""Stamp each footprint with its position through its mission, before tiling.

The map's frame-order filter keeps the earliest slice of each mission — "first
10%", say. That ranking needs the whole mission in flight order, which the
vector tiles can't provide: a tile holds only what is geographically near, not a
mission in sequence. So the rank has to exist as a plain property on each
feature at tile-build time.

Reads a GeoJSON, adds an integer `fr` (0-100, position within mission) to every
feature that has a mission, and writes it back. Run this on
available_scenes.geojson just before tippecanoe.
"""
import json
import sys

import flight_order as fo


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "available_scenes.geojson"
    dest = sys.argv[2] if len(sys.argv) > 2 else src

    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)

    stamped, missions = fo.annotate(data.get("features") or [])

    with open(dest, "w", encoding="utf-8") as out:
        json.dump(data, out, separators=(",", ":"))

    total = len(data.get("features") or [])
    print(f"frame order: stamped {stamped:,} of {total:,} features "
          f"across {missions} missions -> {dest}")

    # Every feature with a mission should have been ranked; a gap means a feature
    # slipped past group_by_mission and the filter would silently drop it.
    missing = sum(1 for f in data["features"]
                  if f["properties"].get("mission") and "fr" not in f["properties"])
    if missing:
        print(f"frame order: WARNING {missing:,} missioned features got no rank")


if __name__ == "__main__":
    main()
