#!/usr/bin/env python3
"""Build the compact scene index the admin tool looks scenes up in.

sfs-admin's "Declass New Entry" panel used to fetch available_scenes.geojson
straight from this repo to resolve an entity ID to its date and satellite. That
file moved to R2 when it outgrew what GitHub will hold, so the fetch started
404ing and the panel reported "Scene DB unavailable".

Serving it from R2 isn't an option for that page: the bucket is private, and the
Worker in front of it is behind Cloudflare Access with a same-origin check, both
of which exist deliberately. So this publishes just the three fields the admin
actually reads, in a form small enough to keep in the repo.

  213,952 scenes  ->  8.3MB of JSON  ->  0.7MB gzipped

Satellite and dataset are dictionary-encoded because they repeat across the
whole archive. Keys are normalised the same way the admin normalises them
(uppercase, hyphens stripped) so lookups need no further work.
"""
import gzip
import json
import sys


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "available_scenes.geojson"
    dest = sys.argv[2] if len(sys.argv) > 2 else "scene-index.json.gz"

    with open(src, encoding="utf-8") as fh:
        data = json.load(fh)

    sats, datasets = [], []
    sat_ix, ds_ix = {}, {}
    scenes = {}

    for feat in data.get("features") or []:
        props = feat.get("properties") or {}
        entity = props.get("entityId")
        if not entity:
            continue

        sat = props.get("satellite") or ""
        dataset = (props.get("dataset") or "").lower()
        if sat not in sat_ix:
            sat_ix[sat] = len(sats)
            sats.append(sat)
        if dataset not in ds_ix:
            ds_ix[dataset] = len(datasets)
            datasets.append(dataset)

        key = entity.upper().replace("-", "")
        scenes[key] = [(props.get("acquisitionDate") or "")[:10],
                       sat_ix[sat], ds_ix[dataset]]

    if not scenes:
        print("scene index: no scenes found — refusing to publish an empty index")
        sys.exit(1)

    doc = {"version": 1, "sats": sats, "datasets": datasets, "scenes": scenes}
    raw = json.dumps(doc, separators=(",", ":")).encode("utf-8")

    # mtime=0 so an unchanged archive produces a byte-identical file and doesn't
    # land a pointless commit every night.
    with gzip.GzipFile(dest, "wb", compresslevel=9, mtime=0) as out:
        out.write(raw)

    print(f"scene index: {len(scenes):,} scenes, "
          f"{len(raw) / 1e6:.1f}MB raw -> {dest}")


if __name__ == "__main__":
    main()
