#!/usr/bin/env python3
"""Publish the filter facets the tile map builds its controls from.

The old GeoJSON map could count its own filter options because it held every
feature in memory. The tile map only ever has the tiles in view, so deriving
chips from what happens to be loaded gives a list that changes as you pan —
KH-1 and KH-2 went missing from the satellite list that way.

So the counts are computed once here, over the whole archive, and published as
a small JSON. A few tens of KB buys authoritative satellite, mission, camera
and year facets that never depend on where the map is pointed.
"""
import collections
import json
import sys


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "available_scenes.geojson"
    dest = sys.argv[2] if len(sys.argv) > 2 else "facets.json"

    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)

    sats = collections.Counter()
    missions = collections.defaultdict(collections.Counter)   # satellite -> mission
    cameras = collections.Counter()                           # (dataset, camera)
    years = collections.Counter()
    datasets = collections.Counter()
    scanned = unscanned = 0
    date_lo, date_hi = None, None

    for feat in data.get("features") or []:
        p = feat.get("properties") or {}
        sat = p.get("satellite") or "Unknown"
        sats[sat] += 1
        datasets[(p.get("dataset") or "").lower()] += 1

        mission = p.get("mission")
        if mission:
            missions[sat][str(mission)] += 1

        cam = p.get("camera")
        if cam:
            cameras[((p.get("dataset") or "").lower(), cam)] += 1

        if p.get("scanned") is False:
            unscanned += 1
        else:
            scanned += 1

        date = (p.get("acquisitionDate") or "")[:10]
        if len(date) == 10:
            years[date[:4]] += 1
            if date_lo is None or date < date_lo:
                date_lo = date
            if date_hi is None or date > date_hi:
                date_hi = date

    if not sats:
        print("facets: no features — refusing to publish empty facets")
        sys.exit(1)

    doc = {
        "total": sum(sats.values()),
        "scanned": scanned,
        "unscanned": unscanned,
        "dateMin": date_lo,
        "dateMax": date_hi,
        # Ordered the way the map lists them: KH-1, KH-2, … rather than by count.
        "satellites": [{"name": s, "n": n} for s, n in sorted(sats.items())],
        "missions": {s: [{"m": m, "n": n} for m, n in sorted(c.items())]
                     for s, c in sorted(missions.items())},
        "cameras": [{"dataset": ds, "camera": cam, "n": n}
                    for (ds, cam), n in sorted(cameras.items())],
        "years": [{"y": y, "n": n} for y, n in sorted(years.items())],
        "datasets": [{"name": d, "n": n} for d, n in sorted(datasets.items())],
    }

    with open(dest, "w", encoding="utf-8") as out:
        json.dump(doc, out, separators=(",", ":"))

    print(f"facets: {doc['total']:,} scenes · {len(doc['satellites'])} satellites · "
          f"{sum(len(v) for v in doc['missions'].values())} missions · "
          f"{len(doc['cameras'])} cameras · {doc['dateMin']}..{doc['dateMax']} -> {dest}")


if __name__ == "__main__":
    main()
