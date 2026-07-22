#!/usr/bin/env python3
"""Publish each mission's frames in flight order, for the frame browser.

The browser answers "what came before and after this frame", which the vector
tiles can't: they hold only what's on screen, and the neighbouring frame is
frequently somewhere else entirely.

Written one file per mission rather than a single index. A combined file came
out at 30MB (4MB gzipped), most of it an entity-ID lookup table — and that table
turned out to be unnecessary, because the mission parses out of the entity ID
itself for all 213,952 frames with no exceptions. So the browser derives the
mission and fetches only that mission, which is tens of KB.

Rows are arrays, not objects: repeating seven JSON keys per frame costs more
than the values.
"""
import json
import os
import shutil
import sys

import flight_order as fo

BROWSE_PREFIX = "https://ims.cr.usgs.gov/browse/"


def centroid(geom):
    """Bounding-box centre of the outer ring, or None."""
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
    xs = [p[0] for p in rings[0]]
    ys = [p[1] for p in rings[0]]
    x0, x1 = min(xs), max(xs)
    if x1 - x0 > 180:      # antimeridian crosser; centre would be meaningless
        return None
    return round((x0 + x1) / 2, 4), round((min(ys) + max(ys)) / 2, 4)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "available_scenes.geojson"
    outdir = sys.argv[2] if len(sys.argv) > 2 else "frames"

    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)

    groups = fo.group_by_mission(data.get("features") or [])
    if not groups:
        print("frames: no missions found — refusing to publish an empty index")
        sys.exit(1)

    # Rebuilt wholesale so a mission that vanishes upstream doesn't linger.
    if os.path.isdir(outdir):
        shutil.rmtree(outdir)
    os.makedirs(outdir)

    catalogue, total, biggest = [], 0, (None, 0)

    for (dataset, mission), frames in sorted(groups.items()):
        rows = []
        for feat in frames:
            p = feat["properties"]
            browse = p.get("browse") or ""
            if browse.startswith(BROWSE_PREFIX):
                browse = browse[len(BROWSE_PREFIX):]
            c = centroid(feat.get("geometry"))
            rows.append([
                p.get("entityId") or "",
                (p.get("acquisitionDate") or "")[:10],
                browse,
                p.get("camera") or "",
                0 if p.get("scanned") is False else 1,
                c[0] if c else None,
                c[1] if c else None,
            ])

        name = f"{dataset}_{mission}.json"
        doc = {
            "mission": mission,
            "dataset": dataset,
            "satellite": frames[0]["properties"].get("satellite") or "",
            "browsePrefix": BROWSE_PREFIX,
            "frames": rows,
        }
        path = os.path.join(outdir, name)
        with open(path, "w", encoding="utf-8") as out:
            json.dump(doc, out, separators=(",", ":"))

        size = os.path.getsize(path)
        total += len(rows)
        if size > biggest[1]:
            biggest = (name, size)
        catalogue.append({
            "file": name, "mission": mission, "dataset": dataset,
            "satellite": doc["satellite"], "n": len(rows),
            "from": rows[0][1], "to": rows[-1][1],
            "scanned": sum(r[4] for r in rows),
        })

    with open(os.path.join(outdir, "_missions.json"), "w", encoding="utf-8") as out:
        json.dump({"version": 1, "missions": catalogue}, out, separators=(",", ":"))

    print(f"frames: {total:,} frames across {len(catalogue)} missions -> {outdir}/")
    print(f"        largest mission file {biggest[0]} at {biggest[1] / 1024:.0f}KB")


if __name__ == "__main__":
    main()
