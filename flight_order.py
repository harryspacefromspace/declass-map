#!/usr/bin/env python3
"""Sort a mission's frames into the order the camera actually shot them.

The scene ID encodes mission + sequence segment + camera + frame, but sorting on
the segment alone is only right about 60% of the time. Sorting by
(acquisitionDate, segment, frame) is clean across all three datasets — missions
span a median of 9 days (79 for HEXAGON), so the date breaks the ties the
segment can't.

This is the shared basis for two things: the map's frame-order filter, which
keeps the earliest slice of each mission, and the frame browser, which steps
through neighbours. Both have to agree on what "next frame" means, so the
ordering lives here rather than in each of them.
"""
import re

# Ported from the map's SEQ_PATS. Groups are (mission-ish, segment, code, frame).
SEQ_PATS = {
    "corona2": [
        re.compile(r"^DS(\d{5}A)(\d{3})([A-Z]{2})(\d+)$"),
        re.compile(r"^DS(\d{6})(\d{3})([A-Z]{2})(\d+)$"),
        re.compile(r"^DS(\d{4})-(\d{4})([A-Z]{2})(\d+)$"),
    ],
    # Declass II ends in two 3-digit fields: the frame, then a sub-frame counter
    # that is 001 on ~97% of scenes. Only the first is the frame number.
    "declassii": [
        re.compile(r"^DZ[BC](\d{4})-(\d{6})([A-Z])(\d{3})(\d{3})$"),
        re.compile(r"^DZ[BC](\d{6})(\d{5})([A-Z])(\d{3})(\d{3})$"),
    ],
    "declassiii": [
        re.compile(r"^D3C(\d+)-(\d+)([A-Z])(\d+)$"),
    ],
}

# Sorts after every real segment/frame, so unparseable IDs sink to the end of
# their mission instead of pretending to be frame 0.
LAST = float("inf")


def parse_seq(entity_id, dataset):
    """-> (segment, frame, code) or None when the ID doesn't parse."""
    for pat in SEQ_PATS.get(dataset or "", []):
        m = pat.match(entity_id or "")
        if m:
            return int(m.group(2)), int(m.group(4)), m.group(3)
    return None


def sort_key(props):
    """The ordering: acquisition date, then segment, then frame."""
    parsed = parse_seq(props.get("entityId") or "", (props.get("dataset") or "").lower())
    seg, frame = (parsed[0], parsed[1]) if parsed else (LAST, LAST)
    return ((props.get("acquisitionDate") or ""), seg, frame,
            props.get("entityId") or "")


def mission_key(props):
    """Frames are ranked within a mission, and a mission is per-dataset."""
    return ((props.get("dataset") or "").lower(), str(props.get("mission") or ""))


def group_by_mission(features):
    """-> {(dataset, mission): [features in flight order]}"""
    groups = {}
    for feat in features:
        props = feat.get("properties") or {}
        if not props.get("mission"):
            continue
        groups.setdefault(mission_key(props), []).append(feat)
    for frames in groups.values():
        frames.sort(key=lambda f: sort_key(f.get("properties") or {}))
    return groups


def annotate(features):
    """Stamp each feature with its position through its mission, 0-100.

    The map filters on this: "first 10%" is fr <= 10. A percentile rather than a
    raw index because missions range from a handful of frames to tens of
    thousands, so an absolute cutoff would mean completely different things.
    """
    groups = group_by_mission(features)
    stamped = 0
    for frames in groups.values():
        last = len(frames) - 1
        for i, feat in enumerate(frames):
            # A one-frame mission is the whole mission; call it position 0.
            feat["properties"]["fr"] = 0 if last <= 0 else round(i * 100 / last)
            stamped += 1
    return stamped, len(groups)
