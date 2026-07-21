#!/usr/bin/env python3
"""
fetch_and_build.py — queries USGS M2M for all downloadable declassified scenes
and builds a self-contained index.html map with dataset, satellite type, and
date range filters. Filters start OFF (additive model — click to show).
"""

import os
import re
import json
import time
import sqlite3
import requests
from datetime import datetime

M2M_URL = "https://m2m.cr.usgs.gov/api/api/json/stable/"

DATASETS = {
    "corona2":    "5e839feb64cee663",
    "declassii":  "5e839ff8ba6eead0",
    "declassiii": "5e7c41f38f5a8fa1",
}

DATASET_LABELS = {
    "corona2":    "Declass I — CORONA/ARGON/LANYARD",
    "declassii":  "Declass II — GAMBIT/HEXAGON",
    "declassiii": "Declass III — HEXAGON",
}

DATASET_COLORS = {
    "corona2":    "#00ff88",
    "declassii":  "#00aaff",
    "declassiii": "#ff9900",
}

# Hash IDs needed for EarthExplorer metadata URLs
DATASET_IDS = {
    "corona2":    "5e839febdccb64b3",
    "declassii":  "5e839ff7d71d4811",
    "declassiii": "5e7c41f3ffaaf662",
}

# KH-6 (LANYARD) flew three missions; get_satellite_type maps 8001–8003 to KH-6.
# Used to pull unscanned LANYARD frames out of the large corona2 dataset.
LANYARD_MISSIONS = ["8001", "8002", "8003"]

# Satellite display order
SAT_ORDER = [
    "KH-1", "KH-2", "KH-3", "KH-4", "KH-4A", "KH-4B",
    "KH-5 (ARGON)", "KH-6 (LANYARD)",
    "KH-7 (GAMBIT)",
    "KH-9 Mapping Camera",   # declassii panoramic mapping missions
    "KH-9 (HEXAGON)",        # declassiii panoramic
    "Unknown",
]


# ---------------------------------------------------------------------------
# Satellite type logic
# ---------------------------------------------------------------------------

def get_satellite_type(mission, dataset):
    if not mission:
        return "Unknown"

    mission_str = mission.split("-")[0] if "-" in mission else mission
    is_argon = mission_str.endswith("A")
    if is_argon:
        mission_str = mission_str[:-1]

    try:
        n = int(mission_str)
    except ValueError:
        return "Unknown"

    if dataset == "corona2":
        if is_argon:             return "KH-5 (ARGON)"
        if 8001 <= n <= 8003:    return "KH-6 (LANYARD)"
        if n == 9009:            return "KH-1"
        if n in (9013, 9017, 9019):                      return "KH-2"
        if n in (9022, 9023, 9025, 9028, 9029):          return "KH-3"
        if 9031 <= n <= 9062:    return "KH-4"   # 9031-9032,9035,9037-9062 etc
        if 1001 <= n <= 1052:    return "KH-4A"
        if 1101 <= n <= 1117:    return "KH-4B"

    elif dataset == "declassii":
        if 1200 <= n <= 1299:    return "KH-9 Mapping Camera"
        return "KH-7 (GAMBIT)"   # default for declassii

    elif dataset == "declassiii":
        return "KH-9 (HEXAGON)"

    return "Unknown"


def mission_sort_key(m):
    """Sort key for mission numbers, which may carry a letter suffix (e.g. ARGON '9066A')."""
    match = re.match(r'(\d+)', m)
    return (int(match.group(1)) if match else 0, m)


# Properties the map can rebuild client-side, so they are not worth storing
# 107k times in the served file (~26% of it). See slim_features().
DERIVED_PROPS = ("datasetLabel", "color", "earthExplorerUrl", "year")


def feat_year(props):
    """Acquisition year, derived from acquisitionDate."""
    y = (props.get("acquisitionDate") or "")[:4]
    return int(y) if y.isdigit() else None


def slim_features(features):
    """Strip derivable properties from features (idempotent; handles legacy data)."""
    removed = 0
    for f in features:
        p = f.get("properties")
        if not p:
            continue
        for k in DERIVED_PROPS:
            if k in p:
                del p[k]
                removed += 1
    if removed:
        print(f"  Slimmed {removed:,} derivable properties from {len(features):,} features")


def get_mission_from_scene(scene):
    for item in scene.get("metadata", []):
        if item.get("fieldName") == "Mission":
            return item.get("value")
    return None


# ---------------------------------------------------------------------------
# M2M helpers
# ---------------------------------------------------------------------------

def login(username, token):
    resp = requests.post(
        M2M_URL + "login-token",
        json={"username": username, "token": token},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errorCode"):
        raise RuntimeError(f"Login failed: {data['errorMessage']}")
    print("  Logged in to M2M API")
    return data["data"]


def logout(api_key):
    try:
        requests.post(M2M_URL + "logout", headers={"X-Auth-Token": api_key}, timeout=10)
    except Exception:
        pass
    print("  Logged out")


# 10000 is allowed but unreliable — USGS truncates responses that large
# ("Response ended prematurely"). Smaller batches complete far more often.
SCENE_BATCH = 2000
# Per-request hard wall-clock cap. requests' `timeout` is only a per-read gap,
# so a trickling response never trips it (a single call once ran 56 minutes).
REQUEST_HARD_CAP = 150


class SceneSearchError(Exception):
    """Raised when a scene-search can't be completed within its retries/deadline."""


def _post_json_bounded(url, payload, headers, hard_timeout):
    """POST and return parsed JSON, enforcing a hard total-time cap on the whole
    request. Streams the body so a slow/trickling response is abandoned instead
    of hanging until the CI job is killed."""
    deadline = time.time() + hard_timeout
    resp = requests.post(url, json=payload, headers=headers,
                         timeout=(30, 60), stream=True)
    try:
        resp.raise_for_status()
        chunks = []
        for chunk in resp.iter_content(chunk_size=65536):
            if time.time() > deadline:
                raise SceneSearchError(f"request exceeded {hard_timeout}s hard cap")
            chunks.append(chunk)
    finally:
        resp.close()
    return json.loads(b"".join(chunks))


def _m2m(api_key, endpoint, payload, deadline, retries=4):
    """One M2M API call with retries and an overall fetch deadline (epoch
    seconds). Raises SceneSearchError on deadline or repeated transient failure;
    a genuine API errorCode is raised immediately (not retried). Returns the
    endpoint's `data` payload (a dict for scene-search, a list for
    dataset-filters, etc.)."""
    headers = {"X-Auth-Token": api_key}
    for attempt in range(retries):
        remaining = deadline - time.time()
        if remaining <= 0:
            raise SceneSearchError("fetch deadline reached")
        try:
            data = _post_json_bounded(
                M2M_URL + endpoint, payload, headers,
                hard_timeout=min(remaining, REQUEST_HARD_CAP))
        except (requests.exceptions.RequestException, SceneSearchError, ValueError) as e:
            if attempt == retries - 1 or time.time() >= deadline:
                raise SceneSearchError(f"{endpoint} failed after {attempt+1} tr{'y' if attempt==0 else 'ies'}: {e}")
            wait = min(2 ** attempt, 10)
            print(f"    transient {endpoint} error (attempt {attempt+1}/{retries}), retry in {wait}s: {e}")
            time.sleep(wait)
            continue
        if data.get("errorCode"):
            raise SceneSearchError(f"API error: {data['errorMessage']}")
        return data.get("data")
    raise SceneSearchError(f"{endpoint}: retries exhausted")


def _scene_search(api_key, payload, deadline, retries=4):
    """scene-search wrapper returning the results dict ({} if absent)."""
    return _m2m(api_key, "scene-search", payload, deadline, retries) or {}


def get_metadata_filter_id(api_key, dataset, field_terms, deadline):
    """Discover the metadata filterId whose field label contains any of
    field_terms (case-insensitive substring), so we can filter a scene-search
    server-side. Logs the available labels and returns None if nothing matches,
    so the run log tells us what the field is actually called."""
    data = _m2m(api_key, "dataset-filters", {"datasetName": dataset}, deadline)
    filters = data or []
    terms = [t.lower() for t in field_terms]
    labels = []
    for filt in filters:
        label = (filt.get("fieldLabel") or "").strip()
        labels.append(label)
        if any(t in label.lower() for t in terms):
            print(f"    matched {dataset} filter '{label}' (id {filt.get('id')})")
            return filt.get("id")
    print(f"    no {field_terms} filter for {dataset}; available fields: {labels}")
    return None


def search_available(api_key, dataset, filter_id, deadline):
    all_scenes = []
    starting   = 1
    while True:
        data = _scene_search(api_key, {
            "datasetName":    dataset,
            "maxResults":     SCENE_BATCH,
            "startingNumber": starting,
            "metadataType":   "full",
            "sceneFilter": {
                "metadataFilter": {
                    "filterType": "value",
                    "filterId":   filter_id,
                    "value":      "Y",
                }
            },
        }, deadline)

        scenes = data.get("results", [])
        if not scenes:
            break
        all_scenes.extend(scenes)
        print(f"    {len(all_scenes):,} scenes retrieved...")
        if len(scenes) < SCENE_BATCH:
            break
        starting += SCENE_BATCH
        time.sleep(0.3)

    return all_scenes


def search_all(api_key, dataset, deadline):
    """Fetch ALL scenes for a dataset regardless of scan/availability status."""
    all_scenes = []
    starting   = 1
    while True:
        data = _scene_search(api_key, {
            "datasetName":    dataset,
            "maxResults":     SCENE_BATCH,
            "startingNumber": starting,
            "metadataType":   "full",
        }, deadline)

        scenes = data.get("results", [])
        if not scenes:
            break
        all_scenes.extend(scenes)
        print(f"    {len(all_scenes):,} scenes retrieved...")
        if len(scenes) < SCENE_BATCH:
            break
        starting += SCENE_BATCH
        time.sleep(0.3)

    return all_scenes


def search_by_missions(api_key, dataset, mission_filter_id, missions, deadline):
    """Fetch all scenes for the given mission values (server-side metadata
    filter), regardless of scan/availability status. Used to pull a small
    subset out of a large dataset — e.g. KH-6 (LANYARD) from corona2."""
    child = [{"filterType": "value", "filterId": mission_filter_id, "value": m}
             for m in missions]
    metadata_filter = (child[0] if len(child) == 1
                       else {"filterType": "or", "childFilters": child})

    all_scenes = []
    starting   = 1
    while True:
        data = _scene_search(api_key, {
            "datasetName":    dataset,
            "maxResults":     SCENE_BATCH,
            "startingNumber": starting,
            "metadataType":   "full",
            "sceneFilter":    {"metadataFilter": metadata_filter},
        }, deadline)

        scenes = data.get("results", [])
        if not scenes:
            break
        all_scenes.extend(scenes)
        print(f"    {len(all_scenes):,} scenes retrieved...")
        if len(scenes) < SCENE_BATCH:
            break
        starting += SCENE_BATCH
        time.sleep(0.3)

    return all_scenes


# ---------------------------------------------------------------------------
# GeoJSON conversion
# ---------------------------------------------------------------------------

CORONA_CAMERA_LABELS = {
    'DF': 'Forward', 'DA': 'Aft', 'DV': 'Vertical',
    'AF': 'Forward', 'AA': 'Aft', 'AV': 'Vertical',
    'MF': 'Forward', 'MA': 'Aft',
    'MC': 'Mapping',
}
HEXAGON_CAMERA_LABELS = {'F': 'Forward', 'A': 'Aft'}
DECLASSII_CAMERA_LABELS = {'H': 'GAMBIT', 'L': 'Mapping'}

def get_camera_from_entity(entity_id, dataset):
    import re
    if dataset == "corona2":
        # ARGON format: DS09066A001MC037 (KH-5)
        m = re.match(r'DS\d{5}A\d{3}([A-Z]{2})', entity_id)
        if not m:
            # Zero-padded format: DS009031001DF... (KH-1/2/3/4/6)
            m = re.match(r'DS\d{6}\d{3}([A-Z]{2})', entity_id)
        if not m:
            # Short format: DS1052-2231DA... (KH-4A/KH-4B)
            m = re.match(r'DS\d{4}-\d{4}([A-Z]{2})', entity_id)
        if m:
            return CORONA_CAMERA_LABELS.get(m.group(1), m.group(1))
    elif dataset == "declassii":
        # Hyphenated Mapping Camera format: DZB1216-500523L001001
        # (DZC is the same layout — ~263 scenes went unlabelled while this
        #  only matched DZB, leaving them with no camera or mission)
        m = re.match(r'DZ[BC]\d{4}-\d{6}([A-Z])\d{6}', entity_id)
        if not m:
            # Non-hyphenated GAMBIT format: DZB00403800118H006001
            m = re.match(r'DZ[BC]\d{11}([A-Z])\d{6}', entity_id)
        if m:
            return DECLASSII_CAMERA_LABELS.get(m.group(1), m.group(1))
    elif dataset == "declassiii":
        m = re.match(r'D3C\d+-\d+([AF])', entity_id)
        if m:
            return HEXAGON_CAMERA_LABELS.get(m.group(1), m.group(1))
    return None

def get_mission_from_entity(entity_id, dataset):
    import re
    if dataset == "corona2":
        # ARGON format: DS09066A001MC037 → "9066A" (KH-5)
        m = re.match(r'DS(\d{5})A\d{3}', entity_id)
        if m:
            return str(int(m.group(1))) + "A"
        # Zero-padded format: DS009031... → "9031" (KH-1/2/3/4/6)
        m = re.match(r'DS(\d{6})', entity_id)
        if m:
            return str(int(m.group(1)))
        # Short format: DS1052-... → "1052" (KH-4A/KH-4B)
        m = re.match(r'DS(\d{4})-', entity_id)
        if m:
            return str(int(m.group(1)))
    elif dataset == "declassii":
        m = re.match(r'DZ[BC](\d+)-', entity_id)
        if m: return m.group(1)
    elif dataset == "declassiii":
        m = re.match(r'D3C(\d+)-', entity_id)
        if m: return m.group(1)
    return None


def scene_to_feature(scene, dataset):
    # Prefer spatialCoverage (actual footprint polygon) over spatialBounds (bbox)
    geom = scene.get("spatialCoverage") or scene.get("spatialFootprint") or scene.get("spatialBounds")
    if not geom or not isinstance(geom, dict) or "type" not in geom:
        return None

    entity_id = scene.get("entityId", "")

    acq = ""
    tc = scene.get("temporalCoverage")
    if isinstance(tc, dict):
        acq = tc.get("startDate", "")
    if not acq:
        acq = scene.get("acquisitionDate", "")

    # Prefer full-resolution browsePath over thumbnailPath
    browse_url = ""
    browse = scene.get("browse")
    if browse and isinstance(browse, list):
        browse_url = browse[0].get("browsePath") or browse[0].get("thumbnailPath", "")

    mission  = get_mission_from_scene(scene) or get_mission_from_entity(entity_id, dataset)
    sat_type = get_satellite_type(mission, dataset)
    camera   = get_camera_from_entity(entity_id, dataset)
    mission_num = get_mission_from_entity(entity_id, dataset)

    # NB: datasetLabel / color / earthExplorerUrl / year are intentionally NOT
    # stored — the map rebuilds them from `dataset`, `entityId` and
    # `acquisitionDate`. See DERIVED_PROPS / slim_features().
    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "entityId":        entity_id,
            "dataset":         dataset,
            "displayId":       scene.get("displayId", ""),
            "acquisitionDate": acq,
            "satellite":       sat_type,
            "mission":         mission_num,
            "camera":          camera,
            "browse":          browse_url,
            "scanned":         True,
            "publishDate":     (scene.get("publishDate", "").split(" ")[0]
                                if scene.get("publishDate") else ""),
            "firstSeenAvailable": "",
        },
    }


def scene_to_feature_unscanned(scene, dataset, scanned_ids):
    """Convert a scene to a GeoJSON feature, marking it as unscanned if not in scanned_ids."""
    f = scene_to_feature(scene, dataset)
    if f is None:
        return None
    entity_id = f["properties"]["entityId"]
    if entity_id in scanned_ids:
        return None  # Already included as a scanned feature
    f["properties"]["scanned"] = False
    return f


# ---------------------------------------------------------------------------
# Download-cart script template
# ---------------------------------------------------------------------------
# Emitted by the in-page cart. Kept as a plain string (NOT run through the
# build_html f-string) so its many braces need no escaping; the browser fills
# __SCENES__ / __GENERATED__ by simple text replacement. Stdlib-only so users
# don't need to pip-install anything.
DOWNLOAD_SCRIPT = r'''#!/usr/bin/env python3
"""
Download declassified satellite scenes from USGS EarthExplorer (M2M API).
Generated by the Declassified Satellite Map on __GENERATED__.

Setup:
  1. Create a free USGS account and request M2M access:
       https://ers.cr.usgs.gov/
  2. Generate an Application Token (Profile > Application Tokens).
  3. Run this script:
       python download_declass_scenes.py
     Credentials are read from the M2M_USERNAME / M2M_TOKEN environment
     variables if set, otherwise you'll be prompted.

Files are saved to ./declass_downloads/. Uses only the Python standard library.
"""
import os, sys, json, time, getpass, urllib.request, urllib.error

API    = "https://m2m.cr.usgs.gov/api/api/json/stable/"
OUTDIR = "declass_downloads"
LABEL  = "declass_map_queue"

# Scenes in the download queue:
SCENES = __SCENES__


def api(endpoint, payload, token=None):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(API + endpoint, data=data)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Auth-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        sys.exit("HTTP %s calling %s: %s" % (e.code, endpoint, e.read().decode(errors="replace")[:300]))
    if body.get("errorCode"):
        sys.exit("API error on %s: %s" % (endpoint, body.get("errorMessage")))
    return body.get("data")


def main():
    if not SCENES:
        print("Queue is empty - nothing to download.")
        return
    user  = os.environ.get("M2M_USERNAME") or input("USGS username: ").strip()
    token = os.environ.get("M2M_TOKEN") or getpass.getpass("M2M application token: ").strip()

    print("Logging in as %s ..." % user)
    api_key = api("login-token", {"username": user, "token": token})

    try:
        by_ds = {}
        for s in SCENES:
            by_ds.setdefault(s["dataset"], []).append(s["entityId"])

        downloads = []
        for ds, eids in by_ds.items():
            print("Fetching download options for %d scene(s) in %s ..." % (len(eids), ds))
            opts = api("download-options", {"datasetName": ds, "entityIds": eids}, api_key) or []
            best = {}
            for o in opts:
                if not o.get("available"):
                    continue
                eid = o.get("entityId")
                is_bundle = "bundle" in (o.get("productName", "").lower())
                if eid not in best or (is_bundle and not best[eid][1]):
                    best[eid] = (o, is_bundle)
            for eid in eids:
                if eid in best:
                    downloads.append({"entityId": eid, "productId": best[eid][0]["id"]})
                else:
                    print("  ! no downloadable product for %s" % eid)

        if not downloads:
            sys.exit("None of the queued scenes have a downloadable product.")

        print("Requesting %d download(s) ..." % len(downloads))
        req = api("download-request", {"downloads": downloads, "label": LABEL}, api_key) or {}
        ready = {}
        for d in req.get("availableDownloads", []) or []:
            ready[d["url"]] = d
        preparing = req.get("preparingDownloads", []) or []

        if preparing and len(ready) < len(downloads):
            print("%d download(s) staging; polling up to 10 min ..." % (len(downloads) - len(ready)))
            deadline = time.time() + 600
            while time.time() < deadline and len(ready) < len(downloads):
                time.sleep(10)
                ret = api("download-retrieve", {"label": LABEL}, api_key) or {}
                for d in ret.get("available", []) or []:
                    ready[d["url"]] = d
                print("  %d/%d ready ..." % (len(ready), len(downloads)))

        os.makedirs(OUTDIR, exist_ok=True)
        urls = list(ready.keys())
        print("Downloading %d file(s) to ./%s/ ..." % (len(urls), OUTDIR))
        for i, url in enumerate(urls, 1):
            fname = url.split("/")[-1].split("?")[0] or ("scene_%d" % i)
            dest = os.path.join(OUTDIR, fname)
            print("  [%d/%d] %s" % (i, len(urls), fname))
            try:
                urllib.request.urlretrieve(url, dest)
            except Exception as e:
                print("    failed: %s" % e)

        missing = len(downloads) - len(urls)
        if missing > 0:
            print("Done. %d still staging - re-run later to fetch them." % missing)
        else:
            print("Done.")
    finally:
        try:
            api("logout", {}, api_key)
        except Exception:
            pass


if __name__ == "__main__":
    main()
'''


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def build_html(geojson):
    import re as _re, collections as _col
    # Scene features are served from an external file (DATA_URL) so index.html
    # stays small; only lightweight derived data is embedded below.
    generated      = geojson["metadata"]["generated"]
    total          = geojson["metadata"]["total"]
    counts         = geojson["metadata"]["counts"]
    year_min       = geojson["metadata"]["year_min"]
    year_max       = geojson["metadata"]["year_max"]
    sat_types      = geojson["metadata"]["sat_types"]
    ds_colors_json = json.dumps(DATASET_COLORS)
    ds_labels_json = json.dumps(DATASET_LABELS)
    ds_ee_json     = json.dumps(DATASET_IDS)
    dl_script_json = json.dumps(DOWNLOAD_SCRIPT)

    # Build mission lists, camera sets, and year counts from features
    missions_by_ds = _col.defaultdict(dict)   # {dataset: {mission: count}}
    cameras_by_ds  = _col.defaultdict(set)    # {dataset: {camera_label}}
    year_counts     = _col.defaultdict(int)   # {year: count}
    date_min = date_max = ""
    avail_seed = ""   # earliest firstSeenAvailable = the monitoring backfill date
    for feat in geojson["features"]:
        p = feat["properties"]
        ds  = p.get("dataset", "")
        m   = p.get("mission")
        cam = p.get("camera")
        acq = p.get("acquisitionDate", "")[:10]
        yr  = feat_year(p)
        fsa = p.get("firstSeenAvailable", "")
        if m:
            missions_by_ds[ds][m] = missions_by_ds[ds].get(m, 0) + 1
        if cam:
            cameras_by_ds[ds].add(cam)
        if yr:
            year_counts[yr] += 1
        if acq:
            if not date_min or acq < date_min: date_min = acq
            if not date_max or acq > date_max: date_max = acq
        if fsa and (not avail_seed or fsa < avail_seed):
            avail_seed = fsa

    year_counts_json = json.dumps({str(y): year_counts[y]
                                   for y in range(year_min, year_max + 1)})

    # Build mission checklist HTML per dataset
    DS_ORDER = ["corona2", "declassii", "declassiii"]
    DS_SHORT = {"corona2": "CORONA", "declassii": "GAMBIT", "declassiii": "HEXAGON"}
    mission_sections_html = ""
    missions_json = {}
    for ds in DS_ORDER:
        ms = missions_by_ds.get(ds, {})
        if not ms:
            continue
        missions_json[ds] = sorted(ms.keys(), key=mission_sort_key)
        items = "".join(
            f'<label class="ms-item"><input type="checkbox" class="ms-chk" data-ds="{ds}" value="{m}" checked>'
            f'<span class="ms-num">{m}</span><span class="ms-count">{c:,}</span></label>'
            for m, c in sorted(ms.items(), key=lambda x: mission_sort_key(x[0]))
        )
        mission_sections_html += (
            f'<div class="ms-group" data-ds="{ds}">'
            f'<div class="ms-header" data-ds="{ds}">'
            f'<span class="ms-ds-label">{DS_SHORT[ds]}</span>'
            f'<button class="ms-all" data-ds="{ds}" data-action="all">All</button>'
            f'<button class="ms-all" data-ds="{ds}" data-action="none">None</button>'
            f'</div>'
            f'<div class="ms-items">{items}</div>'
            f'</div>'
        )

    # Camera filter chips — per dataset, only if >1 camera
    camera_chips_html = ""
    all_cameras = set()
    for ds in DS_ORDER:
        cams = sorted(cameras_by_ds.get(ds, set()))
        if len(cams) <= 1:
            continue
        all_cameras.update(cams)
        for cam in cams:
            camera_chips_html += (
                f'<button class="cam-btn on" data-cam="{cam}" data-ds="{ds}">'
                f'<span class="cam-ds">{DS_SHORT[ds]}</span>{cam}</button>'
            )

    # Build sat→missions map and mission→date range for smart linking (feature 4)
    sat_mission_map = _col.defaultdict(list)   # {sat_type: [mission_num, ...]}
    mission_dates   = {}                        # {mission_num: {ds, lo, hi}}
    for feat in geojson["features"]:
        p = feat["properties"]
        sat = p.get("satellite")
        m   = p.get("mission")
        ds  = p.get("dataset", "")
        acq = p.get("acquisitionDate", "")[:10]
        if sat and m and m not in sat_mission_map[sat]:
            sat_mission_map[sat].append(m)
        if m and acq:
            if m not in mission_dates:
                mission_dates[m] = {"ds": ds, "lo": acq, "hi": acq}
            else:
                if acq < mission_dates[m]["lo"]: mission_dates[m]["lo"] = acq
                if acq > mission_dates[m]["hi"]: mission_dates[m]["hi"] = acq

    sat_mission_map_str = json.dumps({k: sorted(v, key=mission_sort_key)
                                       for k, v in sat_mission_map.items()})
    mission_dates_str   = json.dumps(mission_dates)

    missions_json_str = json.dumps(missions_json)

    counts_html = " &nbsp;|&nbsp; ".join(
        f'<span class="dot" style="background:{DATASET_COLORS[ds]}"></span>'
        f'{DATASET_LABELS[ds].split("—")[0].strip()}: '
        f'<strong>{counts.get(ds,0):,}</strong>'
        for ds in DATASET_LABELS if ds in counts
    )

    # Colour-code sat buttons by family
    SAT_COLORS = {
        "KH-1":               "#4dff8a",
        "KH-2":               "#4dff8a",
        "KH-3":               "#4dff8a",
        "KH-4":               "#4dff8a",
        "KH-4A":              "#4dff8a",
        "KH-4B":              "#4dff8a",
        "KH-5 (ARGON)":       "#a3ffcc",
        "KH-6 (LANYARD)":     "#a3ffcc",
        "KH-7 (GAMBIT)":      "#4db8ff",
        "KH-9 Mapping Camera": "#ffa64d",
        "KH-9 (HEXAGON)":     "#ffa64d",
        "Unknown":            "#777777",
    }
    sat_buttons = "\n      ".join(
        f'<button class="sat-btn" data-sat="{s}" style="--sat-c:{SAT_COLORS.get(s, "#888")}">{s}</button>'
        for s in sat_types
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Declassified Satellite — Available Downloads</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
html{{height:100%}}
body{{
  background:#121212;color:#e0e0e0;
  font-family:'Roboto',system-ui,-apple-system,sans-serif;
  height:100%;display:flex;flex-direction:column;overflow:hidden;margin:0;
  font-size:13px;
}}

/* ── Header ── */
#header{{
  background:#1e1e1e;border-bottom:1px solid #2c2c2c;
  padding:10px 20px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;
  z-index:1000;box-shadow:0 2px 4px rgba(0,0,0,.4);
}}
#header h1{{font-size:15px;font-weight:500;color:#fff;white-space:nowrap;letter-spacing:.01em}}
#header h1 span{{color:#888;font-weight:400;margin-left:8px;font-size:12px}}
#stats{{font-size:12px;color:#888;display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:3px;opacity:.85}}
#search-wrap{{margin-left:auto;position:relative;display:flex;align-items:center}}
#search-wrap svg{{position:absolute;left:10px;opacity:.5;pointer-events:none}}
#search{{
  background:#2a2a2a;border:1px solid #3a3a3a;color:#ddd;
  padding:7px 12px 7px 32px;border-radius:8px;font-size:13px;
  width:200px;outline:none;transition:border-color .15s,background .15s;
}}
#search:focus{{border-color:#90caf9;background:#2e2e2e}}
#search::placeholder{{color:#555}}

/* ── Toolbar ── */
#filters{{
  background:#1e1e1e;border-bottom:1px solid #2c2c2c;
  padding:8px 20px;display:flex;align-items:center;gap:8px;
  flex-wrap:nowrap;position:relative;z-index:800;
}}
.tb-btn{{
  display:flex;align-items:center;gap:6px;
  background:#2a2a2a;border:1px solid #3a3a3a;color:#bbb;
  padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px;
  transition:all .15s;white-space:nowrap;flex-shrink:0;font-weight:500;
  letter-spacing:.01em;
}}
.tb-btn:hover{{background:#333;border-color:#555;color:#fff}}
.tb-btn.active{{background:#1565c0;border-color:#1976d2;color:#fff}}
.tb-btn.has-filter{{background:#1a3a5c;border-color:#1976d2;color:#90caf9}}
.tb-caret{{font-size:10px;opacity:.6;transition:transform .15s}}
.tb-btn.active .tb-caret{{transform:rotate(180deg)}}
#reset-btn{{
  background:transparent;border:1px solid #3a3a3a;color:#888;
  padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px;
  transition:all .15s;margin-left:auto;white-space:nowrap;
}}
#reset-btn:hover{{border-color:#888;color:#ddd;background:#2a2a2a}}

/* ── Dropdown panels ── */
.dd-panel{{
  position:absolute;top:calc(100% + 6px);
  background:#1e1e1e;border:1px solid #2c2c2c;border-radius:12px;
  z-index:2000;min-width:220px;max-width:520px;
  opacity:0;pointer-events:none;transform:translateY(-6px);
  transition:opacity .15s,transform .15s;
  box-shadow:0 8px 32px rgba(0,0,0,.7);
}}
.dd-panel.open{{opacity:1;pointer-events:auto;transform:translateY(0)}}
.dd-inner{{padding:16px 18px}}
.dd-label{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#888;margin-bottom:10px;font-weight:500}}
.dd-chips{{display:flex;flex-wrap:wrap;gap:6px}}
.dd-head{{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}}
.dd-clear{{
  font-size:11px;color:#888;cursor:pointer;background:transparent;
  border:1px solid #3a3a3a;padding:3px 10px;border-radius:6px;transition:all .15s;
}}
.dd-clear:hover{{color:#fff;border-color:#888;background:#2a2a2a}}
.dd-divider{{border:none;border-top:1px solid #2c2c2c;margin:12px 0}}

/* Satellite buttons */
.sat-btn{{
  background:#2a2a2a;border:1px solid #3a3a3a;color:#aaa;
  padding:6px 12px;border-radius:8px;cursor:pointer;font-size:12px;
  transition:all .15s;white-space:nowrap;--sat-c:#888;font-weight:500;
}}
.sat-btn:hover{{background:#333;border-color:#555;color:#fff}}
.sat-btn.on{{
  background:color-mix(in srgb,var(--sat-c) 18%,#1e1e1e);
  border-color:color-mix(in srgb,var(--sat-c) 60%,transparent);
  color:var(--sat-c);
}}
.sat-quick{{
  font-size:12px;color:#666;cursor:pointer;padding:4px 8px;border-radius:6px;
  transition:all .12s;background:#2a2a2a;border:1px solid #3a3a3a;
}}
.sat-quick:hover{{color:#ddd;background:#333}}

/* Camera chips */
.cam-btn{{
  background:#2a2a2a;border:1px solid #3a3a3a;color:#aaa;
  padding:6px 12px;border-radius:8px;cursor:pointer;font-size:12px;
  transition:all .15s;white-space:nowrap;display:flex;align-items:center;gap:5px;font-weight:500;
}}
.cam-btn:hover{{background:#333;border-color:#555;color:#fff}}
.cam-btn.on{{background:#1a3a2a;border-color:#2e7d4f;color:#81c995}}
.cam-ds{{font-size:10px;color:#666;text-transform:uppercase;letter-spacing:.05em}}
.cam-btn.on .cam-ds{{color:#4caf7a}}

/* Date inputs */
.date-input{{
  background:#2a2a2a;border:1px solid #3a3a3a;color:#ddd;
  padding:7px 10px;border-radius:8px;font-size:13px;
  outline:none;cursor:pointer;width:130px;color-scheme:dark;
  transition:border-color .15s;
}}
.date-input:focus{{border-color:#90caf9;background:#2e2e2e}}
.date-sep{{font-size:13px;color:#666}}
.date-row{{display:flex;align-items:center;gap:10px}}

/* Year histogram */
#yr-histogram{{display:flex;align-items:flex-end;gap:2px;height:48px;margin-bottom:10px;cursor:pointer}}
.yr-bar{{
  flex:1;background:#2a2a2a;border-radius:2px 2px 0 0;
  transition:background .1s;min-height:2px;position:relative;
}}
.yr-bar:hover{{background:#42a5f5}}
.yr-bar.in-range{{background:#1976d2}}
.yr-bar.in-range:hover{{background:#42a5f5}}
.yr-val{{font-size:13px;color:#bbb;min-width:36px;text-align:center;font-variant-numeric:tabular-nums;font-weight:500}}
.slider-wrap{{position:relative;width:200px;height:24px;flex-shrink:0;cursor:pointer;user-select:none}}
#slider-track{{position:absolute;top:50%;left:0;right:0;height:3px;background:#333;transform:translateY(-50%);border-radius:2px;pointer-events:none}}
#slider-fill{{position:absolute;top:50%;height:3px;background:#1976d2;transform:translateY(-50%);border-radius:2px;pointer-events:none}}
#slider-fill.active{{background:#42a5f5}}
.thumb{{position:absolute;top:50%;width:14px;height:14px;background:#42a5f5;border-radius:50%;transform:translate(-50%,-50%);pointer-events:none;box-shadow:0 0 0 3px rgba(66,165,245,.2);transition:background .15s}}
.thumb.dragging{{background:#90caf9;box-shadow:0 0 0 6px rgba(66,165,245,.25)}}
.slider-row{{display:flex;align-items:center;gap:10px;margin-top:12px}}

/* Basemap buttons */
.bm-btn{{
  background:#2a2a2a;border:1px solid #3a3a3a;color:#aaa;
  padding:6px 12px;border-radius:8px;cursor:pointer;font-size:12px;
  transition:all .15s;font-weight:500;
}}
.bm-btn:hover{{background:#333;border-color:#555;color:#fff}}
.bm-btn.on{{background:#1a2a3a;border-color:#1976d2;color:#90caf9}}

/* Frame-order slider */
.fr-slider{{position:relative;height:30px;width:100%;cursor:pointer;user-select:none;touch-action:none}}
.fr-track{{position:absolute;top:50%;left:7px;right:7px;height:3px;margin-top:-1.5px;background:#333;border-radius:2px}}
.fr-fill{{position:absolute;top:50%;height:3px;margin-top:-1.5px;background:#42a5f5;border-radius:2px}}
.fr-thumb{{position:absolute;top:50%;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;
  background:#42a5f5;box-shadow:0 0 0 3px rgba(66,165,245,.2);cursor:grab}}
.fr-thumb:focus-visible{{outline:2px solid #90caf9;outline-offset:3px}}
.fr-ticks{{display:flex;justify-content:space-between;align-items:baseline;font-size:11px;color:#666;margin-top:2px}}
#fr-read{{color:#90caf9;font-variant-numeric:tabular-nums;font-weight:500}}
.sb-seq{{color:#5b6470;font-variant-numeric:tabular-nums}}

/* Recently-available chips */
.rc-btn{{
  background:#2a2a2a;border:1px solid #3a3a3a;color:#aaa;
  padding:6px 12px;border-radius:8px;cursor:pointer;font-size:12px;
  transition:all .15s;white-space:nowrap;font-weight:500;
}}
.rc-btn:hover{{background:#333;border-color:#555;color:#fff}}
.rc-btn.on{{background:#0d2018;border-color:#66bb6a88;color:#66bb6a}}
.dd-note{{font-size:11px;color:#666;line-height:1.5;margin-top:2px}}

/* Mission checklist */
.ms-group{{margin-bottom:14px}}
.ms-header{{display:flex;align-items:center;gap:8px;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #2c2c2c}}
.ms-ds-label{{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#888;flex:1;font-weight:500}}
.ms-all{{
  font-size:11px;color:#888;cursor:pointer;padding:3px 8px;border-radius:5px;
  background:#2a2a2a;border:1px solid #3a3a3a;transition:all .12s;
}}
.ms-all:hover{{color:#fff;border-color:#666;background:#333}}
.ms-items{{display:flex;flex-direction:column;gap:2px;max-height:220px;overflow-y:auto}}
.ms-items::-webkit-scrollbar{{width:4px}}
.ms-items::-webkit-scrollbar-thumb{{background:#333;border-radius:2px}}
.ms-item{{display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:6px;cursor:pointer}}
.ms-item:hover{{background:#2a2a2a}}
.ms-item input{{accent-color:#42a5f5;width:13px;height:13px;cursor:pointer;flex-shrink:0}}
.ms-num{{font-size:12px;color:#bbb;flex:1;font-variant-numeric:tabular-nums;font-weight:500}}
.ms-count{{font-size:11px;color:#666;font-variant-numeric:tabular-nums}}

/* ── Filter summary bar ── */
#filter-summary{{
  background:#1a1a1a;border-bottom:1px solid #2c2c2c;
  padding:0 20px;display:flex;align-items:center;gap:6px;flex-wrap:wrap;
  min-height:0;max-height:0;overflow:hidden;transition:max-height .2s,padding .2s;
}}
#filter-summary.visible{{min-height:36px;max-height:72px;padding:6px 20px}}
.fs-pill{{
  display:inline-flex;align-items:center;gap:5px;
  background:#1a3a5c;border:1px solid #1976d2;color:#90caf9;
  padding:3px 10px 3px 12px;border-radius:20px;font-size:12px;white-space:nowrap;font-weight:500;
}}
.fs-pill button{{
  background:none;border:none;color:#64b5f6;cursor:pointer;font-size:14px;
  padding:0 0 0 2px;line-height:1;transition:color .1s;
}}
.fs-pill button:hover{{color:#fff}}
.fs-clear-all{{
  font-size:12px;color:#888;cursor:pointer;padding:3px 10px;
  background:#2a2a2a;border:1px solid #3a3a3a;border-radius:20px;
  transition:all .12s;margin-left:4px;
}}
.fs-clear-all:hover{{color:#fff;border-color:#888}}

#view-group{{margin-left:auto;display:flex;align-items:center;gap:5px}}

/* Map */
#stage{{flex:1;display:flex;min-height:0;min-width:0;position:relative}}
#map{{flex:1;position:relative;min-width:0}}
#globe-wrap{{flex:1;position:relative;background:#000014;overflow:hidden;display:none;min-width:0}}
#globe-wrap.on{{display:block}}
#globe-container{{position:absolute;inset:0}}
.globe-bub{{
  display:flex;align-items:center;justify-content:center;border-radius:50%;
  background:rgba(232,163,61,.17);border:1.5px solid rgba(232,163,61,.9);
  color:#ffeccb;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  font-weight:600;font-variant-numeric:tabular-nums;cursor:pointer;
  transition:background .12s,transform .12s;backdrop-filter:blur(1px);
  text-shadow:0 1px 2px rgba(0,0,0,.6);user-select:none;
}}
.globe-bub:hover{{background:rgba(232,163,61,.34);transform:scale(1.09)}}
#globe-hud{{
  position:absolute;bottom:20px;left:50%;transform:translateX(-50%);z-index:5;
  background:rgba(18,18,18,.92);backdrop-filter:blur(8px);
  border:1px solid #2c2c2c;color:#bbb;padding:7px 18px;border-radius:24px;
  font-size:13px;pointer-events:none;white-space:nowrap;font-weight:500;
  box-shadow:0 2px 8px rgba(0,0,0,.5);
}}

/* ── Right sidebar — scenes in view ── */
#sidebar{{
  width:330px;flex-shrink:0;background:#1a1a1a;border-left:1px solid #2c2c2c;
  display:none;flex-direction:column;min-height:0;
}}
#sidebar.open{{display:flex}}
#sb-head{{
  display:flex;align-items:flex-start;justify-content:space-between;
  padding:12px 14px;border-bottom:1px solid #2c2c2c;
}}
#sb-title{{font-size:13px;color:#fff;font-weight:500}}
#sb-count{{font-size:11px;color:#666;margin-top:3px}}
#sb-close{{
  background:transparent;border:none;color:#666;font-size:15px;cursor:pointer;
  padding:2px 7px;border-radius:6px;transition:all .12s;line-height:1.2;
}}
#sb-close:hover{{color:#fff;background:#2a2a2a}}
#sb-controls{{padding:10px 14px;border-bottom:1px solid #2c2c2c}}
#sb-sort{{
  width:100%;background:#2a2a2a;border:1px solid #3a3a3a;color:#ddd;
  padding:7px 10px;border-radius:8px;font-size:12px;outline:none;cursor:pointer;
  color-scheme:dark;font-family:inherit;
}}
#sb-sort:focus{{border-color:#90caf9}}
#sb-inview-wrap{{
  display:flex;align-items:center;gap:7px;margin-top:9px;
  font-size:11px;color:#888;cursor:pointer;user-select:none;
}}
#sb-inview-wrap:hover{{color:#ddd}}
#sb-inview{{accent-color:#42a5f5;width:13px;height:13px;cursor:pointer;flex-shrink:0}}
#sb-list{{flex:1;overflow-y:auto;padding:8px;min-height:0}}
#sb-list::-webkit-scrollbar{{width:6px}}
#sb-list::-webkit-scrollbar-thumb{{background:#333;border-radius:3px}}
.sb-item{{display:flex;gap:10px;padding:8px;border-radius:8px;cursor:pointer;transition:background .12s}}
.sb-item:hover{{background:#2a2a2a}}
.sb-item.active{{background:#1a3a5c}}
.sb-thumb-ph{{
  width:44px;height:44px;flex-shrink:0;border-radius:6px;background:#121212;
  border:1px dashed #2c2c2c;display:flex;align-items:center;justify-content:center;
  font-size:14px;color:#333;
}}
.sb-item:hover .sb-thumb-ph{{color:#555;border-color:#3a3a3a}}
.sb-meta{{min-width:0;flex:1}}
.sb-id{{
  font-size:11px;color:#ddd;font-family:monospace;letter-spacing:.02em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
}}
.sb-sub{{font-size:11px;color:#888;margin-top:3px}}
.sb-tags{{display:flex;gap:4px;flex-wrap:wrap;margin-top:4px}}
.sb-tag{{font-size:9px;padding:1px 5px;border-radius:3px;border:1px solid #2c2c2c;color:#888;letter-spacing:.03em}}
.sb-tag.un{{color:#ffa726;border-color:#ffa72633;background:#ffa7260a}}
.sb-tag.pub{{color:#66bb6a;border-color:#66bb6a33;background:#66bb6a0a}}
.sb-tag.new{{color:#81c995;border-color:#2e7d4f55;background:#2e7d4f0a}}
#sb-more{{
  margin:8px;padding:8px;background:#2a2a2a;border:1px solid #3a3a3a;color:#aaa;
  border-radius:8px;font-size:12px;cursor:pointer;display:none;font-weight:500;
  transition:all .12s;
}}
#sb-more:hover{{background:#333;color:#fff;border-color:#555}}
#sb-empty{{padding:28px 14px;text-align:center;color:#444;font-size:12px;line-height:1.7}}
#globe-too-many{{
  position:absolute;inset:0;display:none;
  align-items:center;justify-content:center;flex-direction:column;
  background:rgba(0,0,20,.82);z-index:10;text-align:center;gap:10px;
}}
#globe-too-many p{{color:#ccc;font-size:15px;line-height:1.6;margin:0}}
#globe-too-many strong{{color:#fff;font-size:22px;display:block;margin-bottom:4px}}

/* Empty state */
#empty-state{{
  position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  text-align:center;pointer-events:none;z-index:500;
  opacity:1;transition:opacity .3s;
}}
#empty-state.hidden{{opacity:0}}
#empty-state p{{font-size:15px;color:#444;margin-bottom:8px;font-weight:500}}
#empty-state small{{font-size:13px;color:#333}}

/* Counter */
#counter{{
  position:absolute;bottom:20px;left:50%;transform:translateX(-50%);
  background:rgba(18,18,18,.92);backdrop-filter:blur(8px);
  border:1px solid #2c2c2c;color:#888;padding:7px 18px;
  border-radius:24px;font-size:13px;z-index:1000;pointer-events:none;
  transition:all .2s;white-space:nowrap;font-weight:500;
  box-shadow:0 2px 8px rgba(0,0,0,.5);
}}
#counter.has-scenes{{color:#bbb;border-color:#3a3a3a}}

/* ── Overlays button ── */
#ov-toggle{{
  position:absolute;bottom:56px;left:16px;z-index:1000;
  background:rgba(18,18,18,.92);backdrop-filter:blur(8px);
  border:1px solid #2c2c2c;color:#aaa;padding:8px 14px 8px 12px;
  border-radius:10px;font-size:13px;cursor:pointer;font-weight:500;
  display:flex;align-items:center;gap:8px;transition:all .15s;white-space:nowrap;
  box-shadow:0 2px 8px rgba(0,0,0,.5);
}}
#ov-toggle:hover{{border-color:#3a3a3a;color:#ddd;background:#2a2a2a}}
#ov-toggle.has-active{{border-color:#7e57c2;color:#b39ddb}}
#ov-toggle svg{{flex-shrink:0;transition:transform .2s}}
#ov-toggle.open svg{{transform:rotate(180deg)}}

/* ── Overlays panel (opens upward) ── */
#ov-panel{{
  position:absolute;bottom:96px;left:16px;z-index:999;
  background:rgba(18,18,18,.95);backdrop-filter:blur(12px);
  border:1px solid #2c2c2c;border-radius:12px;padding:16px;
  width:230px;display:none;flex-direction:column;gap:10px;
  box-shadow:0 8px 24px rgba(0,0,0,.6);
}}
#ov-panel.open{{display:flex}}
.ov-section{{font-size:11px;color:#666;text-transform:uppercase;letter-spacing:.1em;margin-bottom:2px;font-weight:500}}
.ov-btn{{
  background:#2a2a2a;border:1px solid #3a3a3a;color:#aaa;
  padding:8px 12px;border-radius:8px;font-size:13px;
  cursor:pointer;text-align:left;transition:all .15s;
  display:flex;align-items:center;gap:8px;width:100%;font-weight:500;
}}
.ov-btn:hover{{border-color:#555;color:#fff;background:#333}}
.ov-btn.on{{border-color:#7e57c244;color:#b39ddb;background:#7e57c20a}}
.ov-icon{{font-size:15px;flex-shrink:0}}
.ov-badge{{margin-left:auto;font-size:11px;color:#666;background:#1e1e1e;padding:2px 7px;border-radius:10px;}}
.ov-btn.on .ov-badge{{color:#9575cd}}

/* Published toggle — toolbar variant */
#published-toggle{{
  display:flex;align-items:center;gap:6px;
  background:#2a2a2a;border:1px solid #3a3a3a;color:#bbb;
  padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px;
  transition:all .15s;white-space:nowrap;flex-shrink:0;font-weight:500;
}}
#published-toggle:hover{{background:#333;border-color:#555;color:#fff}}
#published-toggle.on{{background:#0d2018;border-color:#66bb6a88;color:#66bb6a}}
.published-dot{{width:7px;height:7px;border-radius:50%;background:#444;flex-shrink:0;transition:all .2s}}
#published-toggle.on .published-dot{{background:#66bb6a;box-shadow:0 0 5px #66bb6a88}}
#unscanned-toggle{{
  display:flex;align-items:center;gap:6px;
  background:#2a2a2a;border:1px solid #3a3a3a;color:#bbb;
  padding:7px 14px;border-radius:8px;cursor:pointer;font-size:13px;
  transition:all .15s;white-space:nowrap;flex-shrink:0;font-weight:500;
}}
#unscanned-toggle:hover{{background:#333;border-color:#555;color:#fff}}
#unscanned-toggle.on{{background:#1a1100;border-color:#f57c0088;color:#ffa726}}
.unscanned-dot{{width:7px;height:7px;border-radius:50%;background:#444;flex-shrink:0;transition:all .2s}}
#unscanned-toggle.on .unscanned-dot{{background:#ffa726;box-shadow:0 0 5px #ffa72688}}

/* ── USGS status widget ── */
#usgs-status{{
  position:absolute;bottom:20px;right:16px;z-index:1000;
  background:rgba(18,18,18,.92);backdrop-filter:blur(8px);
  border:1px solid #2c2c2c;color:#666;
  padding:6px 12px 6px 10px;border-radius:20px;
  font-size:12px;display:flex;align-items:center;gap:7px;
  cursor:default;transition:border-color .3s,color .3s;white-space:nowrap;
  box-shadow:0 2px 8px rgba(0,0,0,.5);font-weight:500;
}}
#usgs-status.up{{color:#888;border-color:#1a3d2a}}
#usgs-status.down{{color:#ef9a9a;border-color:#5c1a1a}}
#usgs-status.checking{{color:#555;border-color:#2c2c2c}}
#status-dot{{width:8px;height:8px;border-radius:50%;background:#444;flex-shrink:0;transition:background .4s,box-shadow .4s}}
#usgs-status.up #status-dot{{background:#66bb6a;box-shadow:0 0 6px #66bb6a99;animation:pulse-up 2.5s ease-in-out infinite}}
#usgs-status.down #status-dot{{background:#ef5350;box-shadow:0 0 6px #ef535099}}
#usgs-status.checking #status-dot{{background:#555;animation:pulse-check .8s ease-in-out infinite}}
@keyframes pulse-check{{0%,100%{{opacity:.3}}50%{{opacity:1}}}}
@keyframes pulse-up{{0%,100%{{box-shadow:0 0 4px #66bb6a66}}50%{{box-shadow:0 0 10px #66bb6acc}}}}

/* ── Download modal ── */
.pu-dl-btn{{
  font-size:12px;color:#66bb6a;background:transparent;
  padding:6px 12px;border:1px solid #66bb6a33;border-radius:6px;
  cursor:pointer;transition:all .15s;white-space:nowrap;font-weight:500;
}}
.pu-dl-btn:hover{{background:#66bb6a15;border-color:#66bb6a66}}
.pu-dl-btn:disabled{{opacity:.35;cursor:default}}
#dl-modal{{
  position:fixed;inset:0;z-index:9000;display:none;
  align-items:center;justify-content:center;
  background:rgba(0,0,0,.8);backdrop-filter:blur(4px);
}}
#dl-modal.open{{display:flex}}
#dl-box{{
  background:#1e1e1e;border:1px solid #2c2c2c;border-radius:16px;
  padding:24px 28px;width:360px;max-width:90vw;
  box-shadow:0 24px 64px rgba(0,0,0,.95);
}}
#dl-box h4{{font-size:15px;color:#fff;margin-bottom:5px;font-weight:500}}
#dl-box .dl-sub{{font-size:12px;color:#888;margin-bottom:18px}}
.dl-field{{margin-bottom:12px}}
.dl-field label{{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:5px;font-weight:500}}
.dl-field input{{
  width:100%;background:#2a2a2a;border:1px solid #3a3a3a;color:#ddd;
  padding:8px 12px;border-radius:8px;font-size:13px;outline:none;
  box-sizing:border-box;transition:border-color .15s;
}}
.dl-field input:focus{{border-color:#42a5f5}}
#dl-status{{font-size:12px;color:#888;min-height:18px;margin:12px 0;line-height:1.5}}
#dl-status.err{{color:#ef5350}}
#dl-status.ok{{color:#66bb6a}}
.dl-actions{{display:flex;gap:10px;margin-top:16px}}
.dl-actions button{{flex:1;padding:9px 0;border-radius:8px;font-size:13px;cursor:pointer;border:1px solid;transition:all .15s;font-weight:500}}
#dl-go{{background:#1b5e20;border-color:#66bb6a44;color:#66bb6a}}
#dl-go:hover{{background:#2e7d32;border-color:#66bb6aaa}}
#dl-go:disabled{{opacity:.4;cursor:wait}}
#dl-cancel{{background:transparent;border-color:#3a3a3a;color:#888}}
#dl-cancel:hover{{border-color:#888;color:#ddd;background:#2a2a2a}}
#dl-save-creds{{font-size:11px;color:#666;display:flex;align-items:center;gap:6px;margin-top:12px;cursor:pointer}}

/* ── Download queue ── */
#cart-btn{{position:relative}}
#cart-btn.on{{background:#3a2a12;border-color:#e0913f88;color:#ffb74d}}
.pu-cart-btn{{
  font-size:12px;color:#ffb74d;background:transparent;
  padding:6px 12px;border:1px solid #ffb74d33;border-radius:6px;
  cursor:pointer;transition:all .15s;white-space:nowrap;font-weight:500;
}}
.pu-cart-btn:hover{{background:#ffb74d15;border-color:#ffb74d66}}
.pu-cart-btn.in{{background:#3a2a12;border-color:#ffb74d66;color:#ffcc80}}
.sb-cart{{
  flex-shrink:0;align-self:center;width:26px;height:26px;border-radius:6px;
  background:#2a2a2a;border:1px solid #3a3a3a;color:#888;cursor:pointer;
  font-size:14px;line-height:1;transition:all .12s;
}}
.sb-cart:hover{{background:#333;color:#fff;border-color:#555}}
.sb-cart.in{{background:#3a2a12;border-color:#ffb74d66;color:#ffb74d}}
#cart-modal{{
  position:fixed;inset:0;z-index:9000;display:none;
  align-items:center;justify-content:center;
  background:rgba(0,0,0,.8);backdrop-filter:blur(4px);
}}
#cart-modal.open{{display:flex}}
#cart-box{{
  background:#1e1e1e;border:1px solid #2c2c2c;border-radius:16px;
  padding:22px 24px;width:460px;max-width:92vw;max-height:86vh;
  display:flex;flex-direction:column;box-shadow:0 24px 64px rgba(0,0,0,.95);
}}
.cart-head{{display:flex;align-items:center;justify-content:space-between;margin-bottom:4px}}
.cart-head h4{{font-size:15px;color:#fff;font-weight:500}}
.cart-head h4 span{{color:#888;font-weight:400;font-size:13px;margin-left:4px}}
#cart-close{{background:transparent;border:none;color:#666;font-size:16px;cursor:pointer;padding:2px 8px;border-radius:6px;transition:all .12s}}
#cart-close:hover{{color:#fff;background:#2a2a2a}}
#cart-list{{flex:1;overflow-y:auto;margin:14px 0;min-height:40px;display:flex;flex-direction:column;gap:4px}}
#cart-list::-webkit-scrollbar{{width:6px}}
#cart-list::-webkit-scrollbar-thumb{{background:#333;border-radius:3px}}
.cart-item{{display:flex;align-items:center;gap:10px;padding:7px 9px;border-radius:8px;background:#242424}}
.cart-item .ci-id{{font-size:11px;font-family:monospace;color:#ddd;flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
.cart-item .ci-sub{{font-size:11px;color:#777;white-space:nowrap}}
.ci-rm{{background:none;border:none;color:#666;cursor:pointer;font-size:14px;padding:0 4px;transition:color .12s}}
.ci-rm:hover{{color:#ef5350}}
#cart-empty{{color:#555;font-size:13px;text-align:center;padding:26px 10px;line-height:1.6}}
.cart-note{{font-size:11px;color:#888;line-height:1.6;background:#242424;border:1px solid #2c2c2c;border-radius:8px;padding:10px 12px;margin-bottom:14px}}
.cart-note a{{color:#42a5f5;text-decoration:none}}
.cart-note a:hover{{text-decoration:underline}}
.cart-actions{{display:flex;gap:9px}}
.cart-actions button{{padding:9px 14px;border-radius:8px;font-size:13px;cursor:pointer;border:1px solid;transition:all .15s;font-weight:500}}
#cart-dl-script{{flex:1;background:#2e2418;border-color:#e0913f66;color:#ffb74d}}
#cart-dl-script:hover{{background:#3a2c1c;border-color:#e0913faa}}
#cart-dl-script:disabled{{opacity:.4;cursor:default}}
#cart-copy{{background:transparent;border-color:#3a3a3a;color:#aaa}}
#cart-copy:hover{{border-color:#888;color:#fff;background:#2a2a2a}}
#cart-clear{{background:transparent;border-color:#3a3a3a;color:#888}}
#cart-clear:hover{{border-color:#ef535088;color:#ef9a9a;background:#2a1a1a}}

/* Popup */
.leaflet-popup-tip-container,.leaflet-popup-tip{{display:none!important}}
.leaflet-popup-content-wrapper{{
  background:#1e1e1e!important;border:1px solid #2c2c2c!important;
  border-radius:12px!important;box-shadow:0 16px 40px rgba(0,0,0,.95)!important;
  color:#e0e0e0!important;
}}
.leaflet-popup-content{{margin:0!important;padding:0!important}}
.leaflet-popup-close-button{{color:#666!important;font-size:18px!important;padding:8px 10px!important;top:2px!important;right:2px!important}}
.leaflet-popup-close-button:hover{{color:#fff!important;background:none!important}}
.pu{{width:280px;padding:16px}}
.pu-img{{width:100%;max-height:200px;object-fit:contain;object-position:center;
  border-radius:8px;margin-bottom:12px;display:block;cursor:pointer;background:#121212;
  border:1px solid #2c2c2c}}
.pu h3{{font-size:13px;font-weight:500;color:#fff;margin-bottom:8px;font-family:monospace;letter-spacing:.03em;line-height:1.4}}
.pu-tags{{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}}
.pu-tag{{font-size:11px;padding:3px 9px;border-radius:4px;border:1px solid #2c2c2c;color:#aaa;background:#2a2a2a}}
.pu-tag.sat{{color:#ddd;border-color:#3a3a3a}}
.pu .meta{{font-size:12px;color:#888;margin-bottom:12px;line-height:1.8}}
.pu-footer{{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}}
.pu-nav{{display:flex;align-items:center;gap:6px}}
.pu-nav button{{
  background:#2a2a2a;border:1px solid #3a3a3a;color:#aaa;
  padding:6px 12px;border-radius:6px;cursor:pointer;font-size:12px;
  transition:all .15s;font-weight:500;
}}
.pu-nav button:hover{{background:#333;color:#fff;border-color:#555}}
.pu-nav button:disabled{{opacity:.25;cursor:default}}
.pu-nav .pu-count{{font-size:12px;color:#666;white-space:nowrap;min-width:44px;text-align:center}}
.pu-mosaic-btn{{
  width:100%;margin-top:8px;
  background:#1a2a1a;border:1px solid #2e7d4f44;color:#81c995;
  padding:7px 12px;border-radius:8px;font-size:12px;
  cursor:pointer;text-align:center;transition:all .15s;font-weight:500;letter-spacing:.01em;
}}
.pu-mosaic-btn:hover{{background:#1a3a1a;border-color:#2e7d4f88;color:#a5d6a7}}
.pu a{{
  font-size:12px;color:#42a5f5;text-decoration:none;
  padding:5px 12px;border:1px solid #42a5f522;border-radius:6px;transition:all .15s;font-weight:500;
}}
.pu a:hover{{background:#42a5f512;border-color:#42a5f544}}
.pu-tag-unscanned{{color:#ffa726;border-color:#ffa72633;background:#ffa7260a}}
.pu-tag-published{{color:#66bb6a;border-color:#66bb6a33;background:#66bb6a0a}}
.pu-unscanned-badge{{
  width:100%;background:#1a1200;border:1px dashed #ffa72633;border-radius:8px;
  color:#ffa726;font-size:12px;text-align:center;padding:12px;margin-bottom:12px;
  font-weight:500;letter-spacing:.02em;
}}
.pu-unscanned-label{{font-size:11px;color:#888}}
.leaflet-control-zoom{{border:1px solid #2c2c2c!important;border-radius:8px!important;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.5)!important}}
.leaflet-control-zoom a{{
  background:#1e1e1e!important;color:#aaa!important;border-color:#2c2c2c!important;
  width:32px!important;height:32px!important;line-height:32px!important;font-size:16px!important;
}}
.leaflet-control-zoom a:hover{{background:#2a2a2a!important;color:#fff!important}}
.leaflet-control-attribution{{background:rgba(0,0,0,.5)!important;color:#444!important;font-size:10px!important}}
.leaflet-control-attribution a{{color:#444!important}}

/* ── Narrow screens (must stay last: these override the base rules above) ──
   #filters is nowrap by design; on a phone that pushed List/Globe/basemaps
   off-screen with no way to reach them. Wrap rather than scroll, because
   .dd-panel dropdowns are absolutely positioned inside #filters and an
   overflow-x:auto here would clip them vertically. */
/* 1280px, not 900: the toolbar has grown (Cart, List, Frame order) and needs
   ~1270px on one row, so anything narrower was clipping controls unreachably */
@media (max-width:1280px){{
  #header{{padding:8px 12px;gap:10px}}
  #filters{{flex-wrap:wrap;padding:8px 12px}}
  #reset-btn{{margin-left:0}}
  /* wrap too — it's a nested flex row, so wrapping #filters alone left it
     overflowing (425px of buttons in a 390px viewport) */
  #view-group{{margin-left:0;flex-wrap:wrap}}
  #search-wrap{{margin-left:0;width:100%}}
  #search{{width:100%}}
}}
/* Sidebar takes over the stage on small screens instead of squeezing the map */
@media (max-width:700px){{
  #sidebar{{position:absolute;inset:0;width:auto;border-left:none;z-index:1200}}
}}
</style>
</head>
<body>

<div id="header">
  <h1>🛰 Declassified Satellite <span>Available Downloads</span></h1>
  <div id="stats">{counts_html} &nbsp;·&nbsp; Updated <strong>{generated[:10]}</strong></div>
  <div id="search-wrap">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
    <input id="search" type="text" placeholder="Search entity ID or lat, lon…" autocomplete="off" />
  </div>
</div>

<div id="filters">

  <!-- Satellite dropdown -->
  <button class="tb-btn" id="tb-sat">Satellite <span class="tb-caret">▾</span></button>
  <div class="dd-panel" id="dd-sat" style="left:16px">
    <div class="dd-inner">
      <div class="dd-head"><div class="dd-label">Satellite system</div><button class="dd-clear" data-target="sat">Clear</button></div>
      <div class="dd-chips">
        {sat_buttons}
      </div>
      <hr class="dd-divider">
      <div style="display:flex;gap:4px">
        <button class="sat-quick" id="sat-all">All</button>
        <button class="sat-quick" id="sat-none">None</button>
      </div>
    </div>
  </div>

  <!-- Camera dropdown -->
  <button class="tb-btn" id="tb-cam">Camera <span class="tb-caret">▾</span></button>
  <div class="dd-panel" id="dd-cam">
    <div class="dd-inner">
      <div class="dd-head"><div class="dd-label">Camera type</div><button class="dd-clear" data-target="cam">Clear</button></div>
      <div class="dd-chips">{camera_chips_html}</div>
    </div>
  </div>

  <!-- Date dropdown -->
  <button class="tb-btn" id="tb-date">Date <span class="tb-caret">▾</span></button>
  <div class="dd-panel" id="dd-date">
    <div class="dd-inner">
      <div class="dd-head"><div class="dd-label">Exact date range</div><button class="dd-clear" data-target="date">Clear</button></div>
      <div class="date-row">
        <input type="date" class="date-input" id="date-lo" value="{date_min}" min="{date_min}" max="{date_max}">
        <span class="date-sep">→</span>
        <input type="date" class="date-input" id="date-hi" value="{date_max}" min="{date_min}" max="{date_max}">
      </div>
      <hr class="dd-divider">
      <div class="dd-label">Year range</div>
      <div id="yr-histogram"></div>
      <div class="slider-row">
        <span class="yr-val" id="yr-lo">{year_min}</span>
        <div class="slider-wrap" id="slider-wrap">
          <div id="slider-track"></div>
          <div id="slider-fill"></div>
          <div class="thumb" id="thumb-lo"></div>
          <div class="thumb" id="thumb-hi"></div>
        </div>
        <span class="yr-val" id="yr-hi">{year_max}</span>
      </div>
    </div>
  </div>

  <!-- Missions dropdown -->
  <button class="tb-btn" id="tb-mission">Missions <span class="tb-caret">▾</span></button>
  <div class="dd-panel" id="dd-mission" style="max-height:420px;overflow-y:auto">
    <div class="dd-inner">
      <div class="dd-head" style="margin-bottom:10px"><div class="dd-label">Mission</div><button class="dd-clear" data-target="mission">Clear</button></div>
      {mission_sections_html}
    </div>
  </div>

  <!-- Frame order dropdown -->
  <button class="tb-btn" id="tb-frame">⏱ Frame order <span class="tb-caret">▾</span></button>
  <div class="dd-panel" id="dd-frame">
    <div class="dd-inner">
      <div class="dd-head"><div class="dd-label">Position in mission</div><button class="dd-clear" data-target="frame">Clear</button></div>
      <div class="fr-slider" id="fr-slider">
        <div class="fr-track"></div><div class="fr-fill"></div>
      </div>
      <div class="fr-ticks"><span>start</span><span id="fr-read">all frames</span><span>end</span></div>
      <div class="dd-chips" style="margin-top:12px">
        <button class="rc-btn" data-frame="10">First 10%</button>
        <button class="rc-btn" data-frame="25">First 25%</button>
        <button class="rc-btn" data-frame="50">First half</button>
        <button class="rc-btn" data-frame="100">All</button>
      </div>
      <hr class="dd-divider">
      <div class="dd-note">Orders each mission by acquisition date, then by the flight sequence in the scene ID,
        and keeps the chosen slice. Useful when a camera degraded in flight — narrow to the earliest frames.
        Applied per mission, so it works with several selected at once.</div>
    </div>
  </div>

  <!-- Recently available dropdown -->
  <button class="tb-btn" id="tb-recent">🆕 Recently available <span class="tb-caret">▾</span></button>
  <div class="dd-panel" id="dd-recent">
    <div class="dd-inner">
      <div class="dd-head"><div class="dd-label">Became downloadable within</div><button class="dd-clear" data-target="recent">Clear</button></div>
      <div class="dd-chips" id="recent-chips">
        <button class="rc-btn" data-days="1">24 hours</button>
        <button class="rc-btn" data-days="7">7 days</button>
        <button class="rc-btn" data-days="30">30 days</button>
        <button class="rc-btn" data-days="90">90 days</button>
      </div>
      <hr class="dd-divider">
      <div class="dd-note">Based on when the USGS monitor first saw each scene available. Scenes present at launch are dated {avail_seed}.</div>
    </div>
  </div>

  <!-- Unscanned toggle -->
  <button id="unscanned-toggle">
    <span class="unscanned-dot"></span>
    All scenes
  </button>

  <!-- Published toggle -->
  <button id="published-toggle">
    <span class="published-dot"></span>
    Hide published
  </button>

  <!-- Basemap dropdown -->
  <button id="reset-btn">Reset</button>
  <div id="view-group">
    <button id="cart-btn" class="bm-btn" title="Download queue — build a batch download script">⬇ Queue</button>
    <button id="sb-toggle" class="bm-btn" title="List the scenes drawn on the map">☰ List</button>
    <button id="globe-btn" class="bm-btn" title="Switch to globe view">🌐 Globe</button>
    <span style="width:1px;height:16px;background:#3a3a3a;margin:0 2px;display:inline-block"></span>
    <button class="bm-btn on" data-bm="dark">Dark</button>
    <button class="bm-btn" data-bm="satellite">Satellite</button>
    <button class="bm-btn" data-bm="hybrid">Hybrid</button>
    <button class="bm-btn" data-bm="osm">OSM</button>
  </div>
</div>

<div id="filter-summary"></div>

<div id="stage">

<!-- globe.gl takes over #globe-container and replaces its children, so the
     overlays must live outside it or they get destroyed on mount -->
<div id="globe-wrap">
  <div id="globe-container"></div>
  <div id="globe-hud"><span id="globe-count"></span></div>
  <div id="globe-too-many">
    <strong id="globe-too-many-count"></strong>
    <p>Too many scenes to draw on the globe.<br>Filter down to under 3,000 to see footprints.</p>
  </div>
</div>

<div id="map">
  <div id="empty-state">
    <p>No scenes selected</p>
    <small>Choose a satellite type to show footprints</small>
  </div>

  <div id="counter">0 of {total:,} scenes</div>

  <!-- Overlays button -->
  <button id="ov-toggle">
    <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
      <path d="M1 3h10M1 6h10M1 9h10" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/>
    </svg>
    Overlays
    <svg width="10" height="10" viewBox="0 0 10 10" fill="none">
      <path d="M2 3.5L5 6.5L8 3.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
    </svg>
  </button>
  <div id="ov-panel">
    <button class="ov-btn" data-ov="airbases"><span class="ov-icon">✈</span>Military Airbases<span class="ov-badge" id="badge-airbases"></span></button>
    <button class="ov-btn" data-ov="silos"><span class="ov-icon">🚀</span>ICBM / Missile Sites<span class="ov-badge" id="badge-silos"></span></button>
  </div>

  <!-- USGS status -->
  <div id="usgs-status" class="checking" title="USGS EarthExplorer API — checks every 60s">
    <span id="status-dot"></span>
    <span id="status-label">USGS …</span>
  </div>

</div>

<!-- Right sidebar — scenes currently drawn on the map -->
<aside id="sidebar">
  <div id="sb-head">
    <div>
      <div id="sb-title">Scenes on map</div>
      <div id="sb-count">—</div>
    </div>
    <button id="sb-close" title="Close panel">✕</button>
  </div>
  <div id="sb-controls">
    <select id="sb-sort">
      <option value="date-desc">Newest acquired first</option>
      <option value="date-asc">Oldest acquired first</option>
      <option value="seq-asc">Frame order (earliest first)</option>
      <option value="avail-desc">Recently available first</option>
      <option value="sat-asc">Satellite</option>
      <option value="mission-asc">Mission</option>
    </select>
    <label id="sb-inview-wrap">
      <input type="checkbox" id="sb-inview"> Only scenes in current view
    </label>
  </div>
  <div id="sb-list"></div>
  <button id="sb-more">Load more</button>
</aside>

</div><!-- /#stage -->

<!-- Download modal (outside map, fixed overlay) -->
<div id="dl-modal">
  <div id="dl-box">
    <h4>Download Scene</h4>
    <div class="dl-sub" id="dl-scene-id">—</div>
    <div class="dl-field"><label>USGS Username</label><input id="dl-user" type="text" placeholder="EarthExplorer username" autocomplete="username"/></div>
    <div class="dl-field"><label>M2M App Token</label><input id="dl-token" type="password" placeholder="application token (not password)" autocomplete="off"/></div>
    <div id="dl-status"></div>
    <label id="dl-save-creds"><input type="checkbox" id="dl-remember"> Remember credentials in this browser</label>
    <div class="dl-actions">
      <button id="dl-go">⬇ Download</button>
      <button id="dl-cancel">Cancel</button>
    </div>
  </div>
</div>

<!-- Download queue modal -->
<div id="cart-modal">
  <div id="cart-box">
    <div class="cart-head">
      <h4>Download queue <span id="cart-count-lbl"></span></h4>
      <button id="cart-close" title="Close">✕</button>
    </div>
    <div id="cart-list"></div>
    <div id="cart-empty">Your download queue is empty.<br>Click a scene and press <strong>＋ Queue</strong>, or add scenes from the List panel.</div>
    <div class="cart-note">
      Builds a Python script that downloads every queued scene from USGS M2M.
      You'll need a free M2M application token — <a href="https://ers.cr.usgs.gov/" target="_blank">get one ↗</a>.
      No Python packages required.
    </div>
    <div class="cart-actions">
      <button id="cart-dl-script">⬇ Download script (.py)</button>
      <button id="cart-copy">Copy</button>
      <button id="cart-clear">Clear</button>
    </div>
  </div>
</div>

<script>
let GEOJSON     = {{type:'FeatureCollection', features:[]}};  // populated from DATA_URL at load
// Same-origin path served by a Pages Function reading a private R2 bucket.
// Same origin means no CORS, so no other site's JavaScript can fetch it.
const DATA_URL  = '/data/scenes.geojson';
const DS_COLORS = {ds_colors_json};
// Rebuilt client-side instead of being stored on all 107k features
const DS_LABELS = {ds_labels_json};
const DS_EE_IDS = {ds_ee_json};
const eeUrl = p => 'https://earthexplorer.usgs.gov/scene/metadata/full/'
                   + (DS_EE_IDS[p.dataset] || p.dataset) + '/' + p.entityId + '/';
const DL_SCRIPT_TEMPLATE = {dl_script_json};
const YEAR_MIN  = {year_min};
const YEAR_MAX  = {year_max};
const YEAR_COUNTS = {year_counts_json};
const AVAIL_SEED = "{avail_seed}";  // backfill date; firstSeen == this means "available since ≤ this"

// ── Leaflet ───────────────────────────────────────────────────────────────────
const map = L.map('map', {{center:[35,30], zoom:2, preferCanvas:true, zoomControl:true}});

const BASEMAPS = {{
  dark:      L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',
               {{attribution:'© CartoDB © OpenStreetMap', subdomains:'abcd', maxZoom:19}}),
  satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
               {{attribution:'© Esri © USGS', maxZoom:19}}),
  hybrid:    [
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
      {{attribution:'© Esri © USGS', maxZoom:19}}),
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{{z}}/{{y}}/{{x}}',
      {{opacity:0.7, maxZoom:19}})
  ],
  osm:       L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
               {{attribution:'© OpenStreetMap contributors', maxZoom:19}})
}};
let activeBmLayers = [];
let currentBm = 'dark';
// Declared up here on purpose: setBasemap() runs at load and reaches these via
// applyGlobeBasemap(). Declaring them further down put them in the temporal
// dead zone, and the ReferenceError took the rest of the script with it.
let globeMode = false;
let globeInstance = null;
let pendingGlobePov = null;   // view restored from the URL, applied once the globe exists

// The globe can draw the same slippy tiles as the flat map, which is far
// sharper than a single 2048x1024 texture for the whole planet.
const GLOBE_TILES = {{
  dark:      (x, y, l) => `https://${{'abcd'[Math.abs(x + y) % 4]}}.basemaps.cartocdn.com/dark_all/${{l}}/${{x}}/${{y}}.png`,
  satellite: (x, y, l) => `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${{l}}/${{y}}/${{x}}`,
  hybrid:    (x, y, l) => `https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/${{l}}/${{y}}/${{x}}`,
  osm:       (x, y, l) => `https://${{'abc'[Math.abs(x + y) % 3]}}.tile.openstreetmap.org/${{l}}/${{x}}/${{y}}.png`
}};
function applyGlobeBasemap() {{
  if (!globeInstance) return;
  const fn = GLOBE_TILES[currentBm] || GLOBE_TILES.dark;
  // Cached tiles are keyed by position, not by source, so swapping the URL
  // alone left the old basemap on screen until you happened to drag somewhere
  // new. Clearing the cache makes it refetch the current view immediately.
  try {{ globeInstance.globeTileEngineClearCache(); }} catch (e) {{}}
  globeInstance.globeImageUrl(null).globeTileEngineUrl(fn);
}}

function setBasemap(key) {{
  const bm = BASEMAPS[key];
  if (!bm) return;              // ignore .bm-btn styled buttons that aren't basemaps
  activeBmLayers.forEach(l => map.removeLayer(l));
  activeBmLayers = [];
  const arr = Array.isArray(bm) ? bm : [bm];
  // Add in order: first layer goes furthest back
  arr.forEach(l => l.addTo(map));
  arr[0].bringToBack();          // imagery always at the very back
  activeBmLayers = [...arr];
  // Only real basemap buttons own the 'on' state here — #globe-btn and
  // #sb-toggle share the .bm-btn look but manage their own toggle state.
  document.querySelectorAll('.bm-btn[data-bm]').forEach(b => b.classList.toggle('on', b.dataset.bm===key));
  currentBm = key;
  applyGlobeBasemap();   // keep the globe on the same basemap
}}
setBasemap('dark');
// Ensure Leaflet knows the correct map size after initial render
setTimeout(() => map.invalidateSize(), 100);

// ── Filter state ──────────────────────────────────────────────────────────────
const SAT_MISSION_MAP = {sat_mission_map_str};
const MISSION_DATES   = {mission_dates_str};
const SAT_DS = {{
  "KH-1":"corona2","KH-2":"corona2","KH-3":"corona2",
  "KH-4":"corona2","KH-4A":"corona2","KH-4B":"corona2",
  "KH-5 (ARGON)":"corona2","KH-6 (LANYARD)":"corona2",
  "KH-7 (GAMBIT)":"declassii",
  "KH-9 Mapping Camera":"declassii",
  "KH-9 (HEXAGON)":"declassiii"
}};
const satActive = {{}};
// Nothing selected on load — user picks what they want
document.querySelectorAll('.sat-btn').forEach(b => {{
  satActive[b.dataset.sat] = false;
}});

const MISSIONS_BY_DS = {missions_json_str};
let yearLo = YEAR_MIN, yearHi = YEAR_MAX, yearFiltering = false, searchQ = '';
let dateLo = '', dateHi = '';
let filterMMDD = ''; // MM-DD only filter (ignores year)

// Mission state: null = all on, Set = only these missions active
const missionActive = {{}};
Object.keys(MISSIONS_BY_DS).forEach(ds => {{ missionActive[ds] = null; }});

// Camera state keyed by "dataset|camera"
const cameraActive = {{}};
document.querySelectorAll('.cam-btn').forEach(b => {{
  cameraActive[b.dataset.ds + '|' + b.dataset.cam] = true;
}});

let showUnscanned = false;
let hidePublished = false;
let recentDays = 0;   // 0 = off; else filter to scenes that became downloadable within N days
// Frame-order window, as a percentage of each mission's flight sequence
let frameLo = 0, frameHi = 100;
let dateFilter = null;
// (globeMode / globeInstance are declared above, near setBasemap)

// ── Layers ────────────────────────────────────────────────────────────────────
const layers = {{}};
let visibleFeats = [];

function styleFor(ds) {{
  const c = DS_COLORS[ds] || '#fff';
  return {{color:c, weight:1, fillColor:c, fillOpacity:0.13}};
}}
function styleHover(ds) {{
  const c = DS_COLORS[ds] || '#fff';
  return {{color:c, weight:2, fillColor:c, fillOpacity:0.42}};
}}
function styleUnscanned() {{
  return {{color:'#ffa726', weight:1, fillColor:'#ffa726', fillOpacity:0.04,
           dashArray:'4 4'}};
}}
function styleUnscannedHover() {{
  return {{color:'#ffa726', weight:2, fillColor:'#ffa726', fillOpacity:0.15,
           dashArray:'4 4'}};
}}

function buildLayers() {{
  Object.values(layers).forEach(l => {{ try {{ map.removeLayer(l); }} catch(e) {{}} }});
  visibleFeats = [];

  let feats = GEOJSON.features.filter(f => {{
    const p = f.properties;
    // Unscanned: only show if toggle is on, and satellite filter matches
    if (p.scanned === false) {{
      if (!showUnscanned) return false;
      if (recentDays > 0) return false;  // unscanned film is not "available to download"
      if (!satActive[p.satellite]) return false;
      return true;  // unscanned skip other filters
    }}

    // Hide published scenes if toggle is on
    if (hidePublished && p.published) return false;
    if (!satActive[p.satellite]) return false;

    // Recently-available filter (based on when the scene became downloadable).
    // Only genuine new scenes count — those seen after the initial backfill run.
    if (recentDays > 0) {{
      const fsa = p.firstSeenAvailable;
      if (!fsa || fsa <= AVAIL_SEED) return false;
      const cutoff = Date.now() - recentDays * 86400000;
      if (Date.parse(fsa + 'T00:00:00Z') < cutoff) return false;
    }}

    // Year slider
    if (yearFiltering && p.year !== null && (p.year < yearLo || p.year > yearHi)) return false;

    // Exact date range
    const acq = (p.acquisitionDate || '').slice(0, 10);
    if (dateLo && acq && acq < dateLo) return false;
    if (dateHi && acq && acq > dateHi) return false;

    // Month-day only filter (cross-year)
    if (filterMMDD && acq && acq.slice(5) !== filterMMDD) return false;

    // Exact date (mosaic mode)
    if (dateFilter && acq !== dateFilter) return false;

    // Mission filter
    const ms = missionActive[p.dataset];
    if (ms !== null && p.mission && !ms.has(p.mission)) return false;

    // Camera filter
    if (p.camera) {{
      const key = p.dataset + '|' + p.camera;
      if (key in cameraActive && cameraActive[key] === false) return false;
    }}

    // Search
    if (searchQ) {{
      const q = searchQ.toLowerCase();
      if (!p.entityId.toLowerCase().includes(q) && !(p.displayId||'').toLowerCase().includes(q)) return false;
    }}
    return true;
  }});

  // Frame-order window — applied last, over whatever the other filters left.
  // Ranked within each mission separately, since frame 1 of two different
  // missions aren't comparable; "the first 25%" then means the first 25% of
  // every mission on screen.
  if (frameLo > 0 || frameHi < 100) {{
    const groups = {{}};
    for (const f of feats) {{
      const p = f.properties;
      const k = p.dataset + '|' + (p.mission || '?');
      (groups[k] = groups[k] || []).push(f);
    }}
    const keep = new Set();
    for (const g of Object.values(groups)) {{
      g.sort(seqCompare);
      const lo = Math.floor(g.length * frameLo / 100);
      const hi = Math.ceil(g.length * frameHi / 100);
      for (let i = lo; i < hi; i++) keep.add(g[i]);
    }}
    feats = feats.filter(f => keep.has(f));
  }}

  const byDs = {{}};
  const unscannedFeats = [];
  feats.forEach(f => {{
    if (f.properties.scanned === false) {{
      unscannedFeats.push(f);
    }} else {{
      const ds = f.properties.dataset;
      if (!byDs[ds]) byDs[ds] = [];
      byDs[ds].push(f);
    }}
  }});

  Object.entries(byDs).forEach(([ds, dsFeats]) => {{
    layers[ds] = L.geoJSON({{type:'FeatureCollection', features:dsFeats}}, {{
      style: () => styleFor(ds),
      onEachFeature: (feat, layer) => {{
        layer.on('mouseover', () => layer.setStyle(styleHover(feat.properties.dataset)));
        layer.on('mouseout',  () => layer.setStyle(styleFor(feat.properties.dataset)));
      }}
    }}).addTo(map);
  }});

  // Render unscanned as a separate dimmed dashed layer, underneath scanned
  if (unscannedFeats.length) {{
    layers['_unscanned'] = L.geoJSON({{type:'FeatureCollection', features:unscannedFeats}}, {{
      style: styleUnscanned,
      onEachFeature: (feat, layer) => {{
        layer.on('mouseover', () => layer.setStyle(styleUnscannedHover()));
        layer.on('mouseout',  () => layer.setStyle(styleUnscanned()));
      }}
    }}).addTo(map);
    // Push unscanned below scanned layers
    layers['_unscanned'].bringToBack();
  }}

  visibleFeats = feats;
  updateCounter(feats.length);
  updateToolbarState();
  updateFilterSummary();
  updateGlobe();
  updateSidebar(true);
}}

function updateCounter(n) {{
  const el = document.getElementById('counter');
  const total = GEOJSON.features.length;
  el.textContent = n.toLocaleString() + ' of ' + total.toLocaleString() + ' scenes';
  el.classList.toggle('has-scenes', n > 0);
  document.getElementById('empty-state').classList.toggle('hidden', n > 0);
}}

// ── Load scene data (external file keeps index.html small) ────────────────────
// Scene IDs encode a flight sequence after the mission number:
//   DS008003 016 DV 001  ->  mission, sequence segment, camera, frame
// Parsed here rather than stored, so it costs nothing in the served file.
const SEQ_PATS = {{
  corona2: [
    /^DS(\\d{{5}}A)(\\d{{3}})([A-Z]{{2}})(\\d+)$/,
    /^DS(\\d{{6}})(\\d{{3}})([A-Z]{{2}})(\\d+)$/,
    /^DS(\\d{{4}})-(\\d{{4}})([A-Z]{{2}})(\\d+)$/
  ],
  // Declass II ends in two 3-digit fields: frame, then a sub-frame counter
  // that is 001 on ~97% of scenes. Only the first is the frame number.
  declassii: [
    /^DZ[BC](\\d{{4}})-(\\d{{6}})([A-Z])(\\d{{3}})(\\d{{3}})$/,
    /^DZ[BC](\\d{{6}})(\\d{{5}})([A-Z])(\\d{{3}})(\\d{{3}})$/
  ],
  declassiii: [
    /^D3C(\\d+)-(\\d+)([A-Z])(\\d+)$/
  ]
}};
function parseSeq(entityId, dataset) {{
  const pats = SEQ_PATS[dataset] || [];
  for (const re of pats) {{
    const m = entityId.match(re);
    if (m) return {{seq: parseInt(m[2], 10), frame: parseInt(m[4], 10), code: m[3]}};
  }}
  return null;
}}

// USGS calls the digits+letter a "Direction Flag" (e.g. 053D): the digits are
// the revolution number, the letter the direction — D descending, A ascending.
// CORONA's two-letter codes are <direction><camera position>, so the direction
// is the first letter. On KH-4A/4B the flag is 4 digits: <recovery bucket 1-2>
// followed by the revolution.
function dirFlag(p) {{
  if (p.seq == null || !p.seqCode || p.seqCode.length !== 2) return null;
  const d = p.seqCode[0];
  if (d !== 'A' && d !== 'D') return null;
  const wide = p.seq > 999;                       // KH-4A/4B: bucket + rev
  const rev = wide ? p.seq % 1000 : p.seq;
  const bucket = wide ? Math.floor(p.seq / 1000) : null;
  return {{
    rev: rev, bucket: bucket, dir: d,
    label: d === 'A' ? 'ascending' : 'descending',
    flag: String(p.seq).padStart(3, '0') + d
  }};
}}

// True flight order. Date first: sequence segments are only reliable *within*
// a day (verified across the archive — segment alone is right ~60% of the time,
// date+segment is 100%). Unparsed scenes sort last rather than jumping the queue.
function seqCompare(a, b) {{
  const pa = a.properties, pb = b.properties;
  const da = pa.acquisitionDate || '', db = pb.acquisitionDate || '';
  if (da !== db) return da < db ? -1 : 1;
  const sa = pa.seq   == null ? Infinity : pa.seq,   sb = pb.seq   == null ? Infinity : pb.seq;
  if (sa !== sb) return sa - sb;
  const fa = pa.frame == null ? Infinity : pa.frame, fb = pb.frame == null ? Infinity : pb.frame;
  if (fa !== fb) return fa - fb;
  return (pa.entityId || '').localeCompare(pb.entityId || '');
}}

// `year`/`seq`/`frame` aren't stored per-feature; derive once so filters stay fast.
function hydrate(feats) {{
  for (const f of feats) {{
    const p = f.properties;
    const y = parseInt((p.acquisitionDate || '').slice(0, 4), 10);
    p.year = isNaN(y) ? null : y;
    const s = parseSeq(p.entityId || '', p.dataset);
    p.seq = s ? s.seq : null;
    p.frame = s ? s.frame : null;
    p.seqCode = s ? s.code : null;
  }}
}}

(function loadSceneData() {{
  const counter = document.getElementById('counter');
  counter.textContent = 'Loading scenes…';
  fetch(DATA_URL)
    .then(r => {{ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }})
    .then(data => {{ GEOJSON = data; hydrate(GEOJSON.features); buildLayers(); }})
    .catch(err => {{
      console.error('Scene data load failed:', err);
      counter.textContent = 'Failed to load scenes — retrying…';
      setTimeout(loadSceneData, 5000);
    }});
}})();

// ── Multi-scene popup ─────────────────────────────────────────────────────────
function ptInPoly(ll, geom) {{
  const pt = [ll.lng, ll.lat];
  function inRing(pt, ring) {{
    let inside = false;
    for (let i=0,j=ring.length-1;i<ring.length;j=i++) {{
      const xi=ring[i][0],yi=ring[i][1],xj=ring[j][0],yj=ring[j][1];
      if (((yi>pt[1])!==(yj>pt[1])) && pt[0]<(xj-xi)*(pt[1]-yi)/(yj-yi)+xi) inside=!inside;
    }}
    return inside;
  }}
  function testPoly(rings) {{
    if (!inRing(pt,rings[0])) return false;
    for (let i=1;i<rings.length;i++) if (inRing(pt,rings[i])) return false;
    return true;
  }}
  if (geom.type==='Polygon') return testPoly(geom.coordinates);
  if (geom.type==='MultiPolygon') return geom.coordinates.some(p=>testPoly(p));
  return false;
}}

function polyArea(geom) {{
  function ra(ring) {{
    let a=0;
    for (let i=0,j=ring.length-1;i<ring.length;j=i++) a+=(ring[j][0]+ring[i][0])*(ring[j][1]-ring[i][1]);
    return Math.abs(a/2);
  }}
  if (geom.type==='Polygon') return ra(geom.coordinates[0]);
  if (geom.type==='MultiPolygon') return geom.coordinates.reduce((s,p)=>s+ra(p[0]),0);
  return 0;
}}

const popup = L.popup({{maxWidth:290, autoPan:true, closeButton:true}});
// Stop all clicks inside the popup from bubbling to the map
popup.on('add', () => {{
  const el = popup.getElement();
  if (el) L.DomEvent.disableClickPropagation(el);
}});
let puFeats=[], puIdx=0, highlightLayer=null;

function highlightFootprint(feat) {{
  if (highlightLayer) {{ map.removeLayer(highlightLayer); highlightLayer=null; }}
  if (!feat) return;
  const c = DS_COLORS[feat.properties.dataset]||'#fff';
  highlightLayer = L.geoJSON(feat, {{
    style:{{color:'#ffffff', weight:2, fillColor:c, fillOpacity:0, dashArray:'5 4'}}
  }}).addTo(map);
}}

function renderPopup() {{
  highlightFootprint(puFeats[puIdx]);
  const p   = puFeats[puIdx].properties;
  const c   = DS_COLORS[p.dataset]||'#fff';
  const date = p.acquisitionDate ? p.acquisitionDate.slice(0,10) : '—';
  const dsShort = (DS_LABELS[p.dataset] || p.dataset).split('—')[0].trim();
  const isUnscanned = p.scanned === false;

  // Identity: mission + frame, and the USGS direction flag where we can read it
  let idBits = [];
  if (p.mission) idBits.push(`Mission ${{p.mission}}`);
  if (p.frame != null) idBits.push(`frame ${{String(p.frame).padStart(3,'0')}}`);
  if (p.camera) idBits.push(p.camera);
  let metaHtml = idBits.length ? `🎞 ${{idBits.join(' · ')}}<br>` : '';

  const df = dirFlag(p);
  if (df) {{
    metaHtml += `<span title="USGS Direction Flag — revolution number plus direction">`
      + `🧭 Rev ${{df.rev}} · ${{df.label}}`
      + (df.bucket ? ` · bucket ${{df.bucket}}` : '')
      + ` <span style="color:#555">(${{df.flag}})</span></span><br>`;
  }}

  // Availability metadata
  metaHtml += `📅 Acquired ${{date}}`;
  if (p.publishDate) metaHtml += `<br>📥 USGS published ${{p.publishDate}}`;
  const fsa = p.firstSeenAvailable;
  if (fsa && fsa > AVAIL_SEED) {{
    metaHtml += `<br><span style="color:#66bb6a">🆕 Newly available ${{fsa}}</span>`;
  }}

  const imgHtml = p.browse
    ? `<img class="pu-img" src="${{p.browse}}" onerror="this.style.display='none'" title="Click to view full image" onclick="window.open('${{p.browse}}','_blank')">`
    : isUnscanned
      ? `<div class="pu-unscanned-badge">No preview available</div>`
      : '';

  const nav = puFeats.length > 1 ? `
    <div class="pu-nav">
      <button id="pu-prev" ${{puIdx===0?'disabled':''}}>← Prev</button>
      <span class="pu-count">${{puIdx+1}} / ${{puFeats.length}}</span>
      <button id="pu-next" ${{puIdx===puFeats.length-1?'disabled':''}}>Next →</button>
    </div>` : '';

  const footerActions = isUnscanned
    ? `<a href="${{eeUrl(p)}}" target="_blank">EarthExplorer ↗</a>
       <span class="pu-unscanned-label">📷 Film not yet scanned</span>`
    : `<a href="${{eeUrl(p)}}" target="_blank">EarthExplorer ↗</a>
       <button class="pu-cart-btn" data-eid="${{p.entityId}}">${{cartHas(p.entityId) ? '✓ Queued' : '＋ Queue'}}</button>
       <button class="pu-dl-btn" data-eid="${{p.entityId}}" data-ds="${{p.dataset}}">⬇ Download</button>`;

  const mosaicDate = p.acquisitionDate?.slice(0,10) || '';
  const mosaicHtml = !isUnscanned && p.mission && p.camera
    ? `<button class="pu-mosaic-btn" data-sat="${{p.satellite}}" data-ds="${{p.dataset}}" data-mission="${{p.mission}}" data-cam="${{p.camera}}" data-date="${{mosaicDate}}">⊞ Mission ${{p.mission}} · ${{p.camera}}${{mosaicDate ? ' · '+mosaicDate : ''}}</button>`
    : '';

  popup.setContent(`<div class="pu">
    ${{imgHtml}}
    <h3>${{p.entityId}}</h3>
    <div class="pu-tags">
      <span class="pu-tag sat">${{p.satellite}}</span>
      <span class="pu-tag" style="color:${{c}}99;border-color:${{c}}28">${{dsShort}}</span>
      ${{isUnscanned ? '<span class="pu-tag pu-tag-unscanned">Unscanned</span>' : ''}}
      ${{p.published ? '<span class="pu-tag pu-tag-published">✓ On SFS</span>' : ''}}
    </div>
    <div class="meta">${{metaHtml}}</div>
    <div class="pu-footer">
      ${{footerActions}}
      ${{nav}}
    </div>
    ${{mosaicHtml}}
  </div>`);

  setTimeout(() => {{
    const prev = document.getElementById('pu-prev');
    const next = document.getElementById('pu-next');
    if (prev) prev.addEventListener('click', e=>{{ e.stopPropagation(); e.preventDefault(); puIdx--; renderPopup(); }});
    if (next) next.addEventListener('click', e=>{{ e.stopPropagation(); e.preventDefault(); puIdx++; renderPopup(); }});
    const dlBtn = popup.getElement()?.querySelector('.pu-dl-btn');
    if (dlBtn) dlBtn.addEventListener('click', e => {{
      e.stopPropagation();
      openDownloadModal(dlBtn.dataset.eid, dlBtn.dataset.ds);
    }});
    const cartBtn = popup.getElement()?.querySelector('.pu-cart-btn');
    if (cartBtn) cartBtn.addEventListener('click', e => {{
      e.stopPropagation();
      cartToggle(puFeats[puIdx].properties);
      reflectCartBtn(cartBtn);
    }});
    const mosaicBtn = popup.getElement()?.querySelector('.pu-mosaic-btn');
    if (mosaicBtn) mosaicBtn.addEventListener('click', e => {{
      e.stopPropagation();
      const {{sat, ds, mission, cam}} = mosaicBtn.dataset;
      // Only this satellite
      Object.keys(satActive).forEach(k => {{ satActive[k] = k === sat; }});
      document.querySelectorAll('.sat-btn').forEach(b => b.classList.toggle('on', b.dataset.sat === sat));
      // Only this mission for its dataset; reset others to all
      Object.keys(missionActive).forEach(d => {{ missionActive[d] = d === ds ? new Set([mission]) : null; }});
      document.querySelectorAll('.ms-chk').forEach(c => {{
        c.checked = c.dataset.ds !== ds || c.value === mission;
      }});
      // Only this camera for its dataset; keep other datasets' cameras on
      document.querySelectorAll('.cam-btn').forEach(b => {{
        const key = b.dataset.ds + '|' + b.dataset.cam;
        const on = b.dataset.ds !== ds || b.dataset.cam === cam;
        cameraActive[key] = on;
        b.classList.toggle('on', on);
      }});
      // Exact date filter
      dateFilter = mosaicBtn.dataset.date || null;
      map.closePopup();
      buildLayers();
    }});
  }}, 0);
}}

map.on('click', e => {{
  const hits = visibleFeats.filter(f => ptInPoly(e.latlng, f.geometry));
  if (!hits.length) return;
  hits.sort((a,b) => polyArea(a.geometry)-polyArea(b.geometry));
  puFeats=hits; puIdx=0;
  popup.setLatLng(e.latlng).addTo(map);
  renderPopup();
}});
map.on('popupclose', () => {{ if (highlightLayer) {{ map.removeLayer(highlightLayer); highlightLayer=null; }} }});

// ── Sidebar: list of the scenes drawn on the map ──────────────────────────────
const SB_PAGE = 60;
let sbOpen = false, sbInView = false, sbLimit = SB_PAGE, sbShown = [];

// Cached bbox per feature: [minLng, minLat, maxLng, maxLat]
function featBBox(f) {{
  if (f._bb !== undefined) return f._bb;
  const g = f.geometry;
  if (!g || !g.coordinates) return (f._bb = null);
  let minX=Infinity, minY=Infinity, maxX=-Infinity, maxY=-Infinity;
  const scan = ring => {{
    for (const c of ring) {{
      if (c[0]<minX) minX=c[0];
      if (c[0]>maxX) maxX=c[0];
      if (c[1]<minY) minY=c[1];
      if (c[1]>maxY) maxY=c[1];
    }}
  }};
  if (g.type === 'Polygon') g.coordinates.forEach(scan);
  else if (g.type === 'MultiPolygon') g.coordinates.forEach(poly => poly.forEach(scan));
  else return (f._bb = null);
  if (!isFinite(minX)) return (f._bb = null);
  return (f._bb = [minX, minY, maxX, maxY]);
}}

function featsInView() {{
  const b = map.getBounds();
  const w = b.getWest(), e = b.getEast(), s = b.getSouth(), n = b.getNorth();
  return visibleFeats.filter(f => {{
    const bb = featBBox(f);
    if (!bb) return false;
    return !(bb[2] < w || bb[0] > e || bb[3] < s || bb[1] > n);
  }});
}}

const SB_SORTS = {{
  'seq-asc':    seqCompare,   // true flight order: date, then scene sequence
  'date-desc':  (a,b) => (b.properties.acquisitionDate||'').localeCompare(a.properties.acquisitionDate||''),
  'date-asc':   (a,b) => (a.properties.acquisitionDate||'').localeCompare(b.properties.acquisitionDate||''),
  'avail-desc': (a,b) => (b.properties.firstSeenAvailable||'').localeCompare(a.properties.firstSeenAvailable||''),
  'sat-asc':    (a,b) => (a.properties.satellite||'').localeCompare(b.properties.satellite||'')
                          || (b.properties.acquisitionDate||'').localeCompare(a.properties.acquisitionDate||''),
  'mission-asc':(a,b) => (a.properties.mission||'').localeCompare(b.properties.mission||'', undefined, {{numeric:true}})
                          || (b.properties.acquisitionDate||'').localeCompare(a.properties.acquisitionDate||''),
}};

function updateSidebar(resetPage) {{
  if (!sbOpen) return;
  if (resetPage) sbLimit = SB_PAGE;

  const feats = (sbInView ? featsInView() : visibleFeats).slice();
  feats.sort(SB_SORTS[document.getElementById('sb-sort').value] || SB_SORTS['date-desc']);

  document.getElementById('sb-title').textContent = sbInView ? 'Scenes in view' : 'Scenes on map';
  document.getElementById('sb-count').textContent =
    feats.length.toLocaleString() + ' scene' + (feats.length === 1 ? '' : 's');

  const list = document.getElementById('sb-list');
  const more = document.getElementById('sb-more');
  if (!feats.length) {{
    list.innerHTML = '<div id="sb-empty">No scenes to list.<br>Pick a satellite or adjust the filters.</div>';
    more.style.display = 'none';
    sbShown = [];
    return;
  }}

  sbShown = feats.slice(0, sbLimit);
  list.innerHTML = sbShown.map((f, i) => {{
    const p = f.properties;
    const d = (p.acquisitionDate || '').slice(0,10) || '—';
    // No <img> here on purpose: USGS browse images are up to ~7.5MB each, so a
    // list of them would be brutal. The preview loads on click, in the popup.
    const thumb = `<div class="sb-thumb-ph">▦</div>`;
    const tags = [];
    tags.push(p.scanned === false
      ? '<span class="sb-tag un">Unscanned</span>'
      : '<span class="sb-tag">Downloadable</span>');
    if (p.published) tags.push('<span class="sb-tag pub">On SFS</span>');
    if (p.firstSeenAvailable && p.firstSeenAvailable > AVAIL_SEED)
      tags.push('<span class="sb-tag new">New</span>');
    // Cart toggle (downloadable scenes only); string-built to avoid nested templates
    const cartCell = p.scanned === false ? '' :
      '<button class="sb-cart ' + (cartHas(p.entityId) ? 'in' : '') + '" data-eid="' +
      p.entityId + '" title="Add to download queue">' + (cartHas(p.entityId) ? '✓' : '＋') + '</button>';
    return `<div class="sb-item" data-i="${{i}}">
      ${{thumb}}
      <div class="sb-meta">
        <div class="sb-id">${{p.entityId}}</div>
        <div class="sb-sub">${{d}} · ${{p.satellite || '—'}}${{p.seq != null ? ' · <span class="sb-seq">seq ' + p.seq + '·' + p.frame + '</span>' : ''}}</div>
        <div class="sb-tags">${{tags.join('')}}</div>
      </div>
      ${{cartCell}}
    </div>`;
  }}).join('');

  if (feats.length > sbLimit) {{
    more.style.display = 'block';
    more.textContent = 'Load more (' + (feats.length - sbLimit).toLocaleString() + ' more)';
  }} else {{
    more.style.display = 'none';
  }}
}}

// Row click → open that scene's popup (pan only if it's off-screen)
document.getElementById('sb-list').addEventListener('click', e => {{
  // Cart toggle button takes precedence over opening the scene
  const cbtn = e.target.closest('.sb-cart');
  if (cbtn) {{
    e.stopPropagation();
    const item = cbtn.closest('.sb-item');
    const cf = item && sbShown[parseInt(item.dataset.i)];
    if (cf) cartToggle(cf.properties);
    return;
  }}
  const el = e.target.closest('.sb-item');
  if (!el) return;
  const f = sbShown[parseInt(el.dataset.i)];
  if (!f) return;
  const bb = featBBox(f);
  if (!bb) return;
  const center = L.latLng((bb[1]+bb[3])/2, (bb[0]+bb[2])/2);
  if (!map.getBounds().contains(center)) map.panTo(center);
  puFeats = [f]; puIdx = 0;
  popup.setLatLng(center).addTo(map);
  renderPopup();
  document.querySelectorAll('.sb-item').forEach(x => x.classList.remove('active'));
  el.classList.add('active');
}});

function setSidebar(open) {{
  sbOpen = open;
  document.getElementById('sidebar').classList.toggle('open', open);
  document.getElementById('sb-toggle').classList.toggle('on', open);
  // Map width changed — let Leaflet re-measure, then refresh the list
  setTimeout(() => {{ map.invalidateSize(); updateSidebar(true); }}, 0);
}}
document.getElementById('sb-toggle').addEventListener('click', () => setSidebar(!sbOpen));
document.getElementById('sb-close').addEventListener('click', () => setSidebar(false));
document.getElementById('sb-sort').addEventListener('change', () => updateSidebar(true));
document.getElementById('sb-inview').addEventListener('change', e => {{
  sbInView = e.target.checked;
  updateSidebar(true);
}});
document.getElementById('sb-more').addEventListener('click', () => {{
  sbLimit += SB_PAGE;
  updateSidebar(false);
}});
// Only recompute on pan/zoom when the list is actually view-limited
map.on('moveend', () => {{ if (sbOpen && sbInView) updateSidebar(true); }});

// ── Download queue ────────────────────────────────────────────────────────────
const CART_KEY = 'declass_cart_v1';
const cart = new Map();   // entityId -> {{entityId, dataset, satellite, acquisitionDate}}
try {{
  JSON.parse(localStorage.getItem(CART_KEY) || '[]').forEach(s => {{
    if (s && s.entityId) cart.set(s.entityId, s);
  }});
}} catch(e) {{}}

function cartSave() {{ try {{ localStorage.setItem(CART_KEY, JSON.stringify([...cart.values()])); }} catch(e) {{}} }}
function cartHas(eid) {{ return cart.has(eid); }}
function cartAdd(p) {{
  if (!p || p.scanned === false) return;   // unscanned scenes aren't downloadable
  cart.set(p.entityId, {{
    entityId: p.entityId, dataset: p.dataset,
    satellite: p.satellite || '', acquisitionDate: (p.acquisitionDate || '').slice(0, 10)
  }});
  cartSave(); updateCartUI();
}}
function cartRemove(eid) {{ cart.delete(eid); cartSave(); updateCartUI(); }}
function cartToggle(p) {{ if (cartHas(p.entityId)) cartRemove(p.entityId); else cartAdd(p); }}

function reflectCartBtn(btn) {{
  const inCart = cartHas(btn.dataset.eid);
  btn.classList.toggle('in', inCart);
  btn.textContent = inCart ? '✓ Queued' : '＋ Queue';
}}

function updateCartUI() {{
  const n = cart.size;
  const btn = document.getElementById('cart-btn');
  btn.textContent = '⬇ Queue' + (n ? ' (' + n + ')' : '');
  btn.classList.toggle('on', n > 0);
  const pc = popup.getElement && popup.getElement() && popup.getElement().querySelector('.pu-cart-btn');
  if (pc) reflectCartBtn(pc);
  document.querySelectorAll('.sb-cart').forEach(b => {{
    const inCart = cartHas(b.dataset.eid);
    b.classList.toggle('in', inCart);
    b.textContent = inCart ? '✓' : '＋';
  }});
  if (document.getElementById('cart-modal').classList.contains('open')) renderCart();
}}

function renderCart() {{
  const list  = document.getElementById('cart-list');
  const items = [...cart.values()];
  document.getElementById('cart-count-lbl').textContent = items.length ? '(' + items.length + ')' : '';
  document.getElementById('cart-dl-script').disabled = !items.length;
  document.getElementById('cart-copy').disabled = !items.length;
  document.getElementById('cart-empty').style.display = items.length ? 'none' : 'block';
  list.innerHTML = items.map(s => {{
    const dsShort = (DS_LABELS[s.dataset] || s.dataset).split('—')[0].trim();
    return '<div class="cart-item"><span class="ci-id">' + s.entityId +
      '</span><span class="ci-sub">' + (s.acquisitionDate || '') + ' · ' + dsShort +
      '</span><button class="ci-rm" data-eid="' + s.entityId + '" title="Remove">✕</button></div>';
  }}).join('');
}}

document.getElementById('cart-btn').addEventListener('click', () => {{
  renderCart();
  document.getElementById('cart-modal').classList.add('open');
}});
document.getElementById('cart-close').addEventListener('click', () =>
  document.getElementById('cart-modal').classList.remove('open'));
document.getElementById('cart-modal').addEventListener('click', e => {{
  if (e.target === document.getElementById('cart-modal'))
    document.getElementById('cart-modal').classList.remove('open');
}});
document.getElementById('cart-list').addEventListener('click', e => {{
  const rm = e.target.closest('.ci-rm');
  if (rm) cartRemove(rm.dataset.eid);
}});
document.getElementById('cart-clear').addEventListener('click', () => {{
  if (cart.size && confirm('Remove all ' + cart.size + ' scene(s) from the download queue?')) {{
    cart.clear(); cartSave(); updateCartUI();
  }}
}});

function buildCartScript() {{
  const scenes = [...cart.values()].map(s => ({{entityId: s.entityId, dataset: s.dataset}}));
  const stamp = new Date().toISOString().slice(0, 19).replace('T', ' ') + ' UTC';
  return DL_SCRIPT_TEMPLATE
    .split('__SCENES__').join(JSON.stringify(scenes, null, 2))
    .split('__GENERATED__').join(stamp);
}}
document.getElementById('cart-dl-script').addEventListener('click', () => {{
  if (!cart.size) return;
  const blob = new Blob([buildCartScript()], {{type: 'text/x-python'}});
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = 'download_declass_scenes.py';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}});
document.getElementById('cart-copy').addEventListener('click', async () => {{
  if (!cart.size) return;
  const btn = document.getElementById('cart-copy');
  const old = btn.textContent;
  try {{
    await navigator.clipboard.writeText(buildCartScript());
    btn.textContent = 'Copied ✓';
  }} catch(e) {{
    btn.textContent = 'Copy failed';
  }}
  setTimeout(() => {{ btn.textContent = old; }}, 1500);
}});

updateCartUI();

// ── Satellite buttons ─────────────────────────────────────────────────────────
function syncMissionsToSat(sat, on) {{
  // When a satellite is toggled, check/uncheck its missions in the mission panel
  const missions = SAT_MISSION_MAP[sat] || [];
  if (!missions.length) return;
  missions.forEach(m => {{
    // Find which dataset this mission belongs to
    const md = MISSION_DATES[m];
    if (!md) return;
    const ds = md.ds;
    const chk = document.querySelector(`.ms-chk[data-ds="${{ds}}"][value="${{m}}"]`);
    if (chk) chk.checked = on;
  }});
  // Recompute missionActive for affected datasets
  Object.keys(MISSIONS_BY_DS).forEach(ds => {{
    const all = MISSIONS_BY_DS[ds] || [];
    const checked = [...document.querySelectorAll(`.ms-chk[data-ds="${{ds}}"]`)]
      .filter(c => c.checked).map(c => c.value);
    missionActive[ds] = checked.length === all.length ? null : new Set(checked);
  }});
}}

document.querySelectorAll('.sat-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const s = btn.dataset.sat;
    satActive[s] = !satActive[s];
    btn.classList.toggle('on', satActive[s]);
    syncMissionsToSat(s, satActive[s]);
    buildLayers();
  }});
}});
document.getElementById('sat-all').addEventListener('click', () => {{
  Object.keys(satActive).forEach(k => satActive[k] = true);
  document.querySelectorAll('.sat-btn').forEach(b => b.classList.add('on'));
  Object.keys(missionActive).forEach(ds => {{ missionActive[ds] = null; }});
  document.querySelectorAll('.ms-chk').forEach(c => {{ c.checked = true; }});
  buildLayers();
}});
document.getElementById('sat-none').addEventListener('click', () => {{
  Object.keys(satActive).forEach(k => satActive[k] = false);
  document.querySelectorAll('.sat-btn').forEach(b => b.classList.remove('on'));
  Object.keys(missionActive).forEach(ds => {{ missionActive[ds] = new Set(); }});
  document.querySelectorAll('.ms-chk').forEach(c => {{ c.checked = false; }});
  buildLayers();
}});

// ── Year slider ───────────────────────────────────────────────────────────────
// ── Year slider (custom — no native inputs) ────────────────────────────────
const sliderWrap = document.getElementById('slider-wrap');
const thumbLo = document.getElementById('thumb-lo');
const thumbHi = document.getElementById('thumb-hi');
const fill    = document.getElementById('slider-fill');

function sliderPct(v) {{ return (v - YEAR_MIN) / (YEAR_MAX - YEAR_MIN) * 100; }}
function sliderVal(p) {{ return Math.round(YEAR_MIN + p * (YEAR_MAX - YEAR_MIN)); }}
function sliderClamp(v, a, b) {{ return Math.max(a, Math.min(b, v)); }}

// ── Year histogram ────────────────────────────────────────────────────────────
(function buildHistogram() {{
  const el = document.getElementById('yr-histogram');
  if (!el) return;
  const maxCount = Math.max(...Object.values(YEAR_COUNTS));
  for (let y = YEAR_MIN; y <= YEAR_MAX; y++) {{
    const count = YEAR_COUNTS[y] || 0;
    const bar = document.createElement('div');
    bar.className = 'yr-bar';
    bar.dataset.year = y;
    bar.style.height = Math.max(2, Math.round((count / maxCount) * 100)) + '%';
    bar.title = `${{y}}: ${{count.toLocaleString()}} scenes`;
    bar.addEventListener('click', () => {{
      // Click a bar to set yearLo to that year; shift-click to set yearHi
      yearLo = y; yearHi = y; yearFiltering = true;
      updateSlider(); buildLayers();
    }});
    el.appendChild(bar);
  }}
}})();

function updateHistogram() {{
  document.querySelectorAll('.yr-bar').forEach(bar => {{
    const y = parseInt(bar.dataset.year);
    bar.classList.toggle('in-range', y >= yearLo && y <= yearHi);
  }});
}}

function updateSlider() {{
  const lp = sliderPct(yearLo), hp = sliderPct(yearHi);
  thumbLo.style.left = lp + '%';
  thumbHi.style.left = hp + '%';
  fill.style.left  = lp + '%';
  fill.style.width = (hp - lp) + '%';
  document.getElementById('yr-lo').textContent = yearLo;
  document.getElementById('yr-hi').textContent = yearHi;
  const active = yearLo > YEAR_MIN || yearHi < YEAR_MAX;
  fill.classList.toggle('active', active);
  updateHistogram();
}}

let sliderDragging = null;

function xToPct(clientX) {{
  const r = sliderWrap.getBoundingClientRect();
  return sliderClamp((clientX - r.left) / r.width, 0, 1);
}}

sliderWrap.addEventListener('pointerdown', e => {{
  e.preventDefault();
  sliderWrap.setPointerCapture(e.pointerId);
  const p   = xToPct(e.clientX);
  const lop = sliderPct(yearLo) / 100;
  const hip = sliderPct(yearHi) / 100;
  sliderDragging = Math.abs(p - lop) <= Math.abs(p - hip) ? 'lo' : 'hi';
  thumbLo.classList.toggle('dragging', sliderDragging === 'lo');
  thumbHi.classList.toggle('dragging', sliderDragging === 'hi');
  moveDragging(p);
}});

sliderWrap.addEventListener('pointermove', e => {{
  if (!sliderDragging) return;
  moveDragging(xToPct(e.clientX));
}});

sliderWrap.addEventListener('pointerup', () => {{
  thumbLo.classList.remove('dragging');
  thumbHi.classList.remove('dragging');
  sliderDragging = null;
}});

function moveDragging(p) {{
  const v = sliderVal(p);
  if (sliderDragging === 'lo') {{
    yearLo = sliderClamp(v, YEAR_MIN, yearHi);
  }} else {{
    yearHi = sliderClamp(v, yearLo, YEAR_MAX);
  }}
  yearFiltering = yearLo > YEAR_MIN || yearHi < YEAR_MAX;
  updateSlider();
  buildLayers();
}}

updateSlider();

// ── Reset ─────────────────────────────────────────────────────────────────────
function resetAllFilters() {{
  // Satellites — all on
  Object.keys(satActive).forEach(k => satActive[k] = true);
  document.querySelectorAll('.sat-btn').forEach(b => b.classList.add('on'));
  // Year slider
  yearLo=YEAR_MIN; yearHi=YEAR_MAX; yearFiltering=false;
  updateSlider();
  // Dates
  dateLo=''; dateHi='';
  const dlo = document.getElementById('date-lo'), dhi = document.getElementById('date-hi');
  if (dlo) dlo.value = dlo.min;
  if (dhi) dhi.value = dhi.max;
  // Cameras
  document.querySelectorAll('.cam-btn').forEach(b => {{
    b.classList.add('on');
    cameraActive[b.dataset.ds + '|' + b.dataset.cam] = true;
  }});
  // Missions
  Object.keys(missionActive).forEach(ds => {{ missionActive[ds] = null; }});
  document.querySelectorAll('.ms-chk').forEach(c => {{ c.checked = true; }});
  // Exact date
  dateFilter = null;
  // Recently available
  recentDays = 0;
  document.querySelectorAll('.rc-btn').forEach(b => b.classList.remove('on'));
  // Frame order
  frameLo = 0; frameHi = 100;
  if (window.paintFrameSlider) window.paintFrameSlider();
  // Search
  searchQ=''; document.getElementById('search').value='';
  buildLayers();
}}
document.getElementById('reset-btn').addEventListener('click', resetAllFilters);

// ── Per-dropdown Clear buttons ─────────────────────────────────────────────────
document.querySelectorAll('.dd-clear').forEach(btn => {{
  btn.addEventListener('click', e => {{
    e.stopPropagation();
    const t = btn.dataset.target;
    if (t === 'sat') {{
      Object.keys(satActive).forEach(k => satActive[k] = true);
      document.querySelectorAll('.sat-btn').forEach(b => b.classList.add('on'));
      Object.keys(missionActive).forEach(ds => {{ missionActive[ds] = null; }});
      document.querySelectorAll('.ms-chk').forEach(c => {{ c.checked = true; }});
    }} else if (t === 'cam') {{
      document.querySelectorAll('.cam-btn').forEach(b => {{
        b.classList.add('on');
        cameraActive[b.dataset.ds + '|' + b.dataset.cam] = true;
      }});
    }} else if (t === 'date') {{
      yearLo=YEAR_MIN; yearHi=YEAR_MAX; yearFiltering=false; updateSlider();
      dateLo=''; dateHi='';
      const dlo=document.getElementById('date-lo'), dhi=document.getElementById('date-hi');
      if (dlo) dlo.value=dlo.min;
      if (dhi) dhi.value=dhi.max;
    }} else if (t === 'mission') {{
      Object.keys(missionActive).forEach(ds => {{ missionActive[ds] = null; }});
      document.querySelectorAll('.ms-chk').forEach(c => {{ c.checked = true; }});
    }} else if (t === 'recent') {{
      recentDays = 0;
      document.querySelectorAll('.rc-btn').forEach(b => b.classList.remove('on'));
    }} else if (t === 'frame') {{
      frameLo = 0; frameHi = 100;
      if (window.paintFrameSlider) window.paintFrameSlider();
    }}
    buildLayers();
  }});
}});

// ── Filter summary bar ────────────────────────────────────────────────────────
function updateFilterSummary() {{
  const bar = document.getElementById('filter-summary');
  const pills = [];

  // Satellites off
  const offSats = Object.entries(satActive).filter(([,v])=>!v).map(([k])=>k);
  const allSats = Object.keys(satActive);
  if (offSats.length > 0 && offSats.length < allSats.length) {{
    const onSats = allSats.filter(k=>satActive[k]);
    onSats.forEach(s => {{
      pills.push(`<span class="fs-pill">${{s}}<button data-action="sat" data-val="${{s}}">×</button></span>`);
    }});
  }} else if (offSats.length === allSats.length) {{
    pills.push(`<span class="fs-pill">No satellites<button data-action="sat-all">×</button></span>`);
  }}

  // Camera
  const offCams = Object.entries(cameraActive).filter(([,v])=>!v).map(([k])=>k);
  offCams.forEach(key => {{
    const [ds, cam] = key.split('|');
    const dsShort = {{'corona2':'CORONA','declassii':'GAMBIT','declassiii':'HEXAGON'}}[ds]||ds;
    pills.push(`<span class="fs-pill">${{dsShort}} ${{cam}}<button data-action="cam" data-val="${{key}}">×</button></span>`);
  }});

  // Date
  const dlo = document.getElementById('date-lo'), dhi = document.getElementById('date-hi');
  if (dateFilter) {{
    pills.push(`<span class="fs-pill">📅 ${{dateFilter}}<button data-action="exact-date">×</button></span>`);
  }} else if ((dateLo && dateLo !== dlo?.min) || (dateHi && dateHi !== dhi?.max)) {{
    const lo = dateLo || dlo?.min || '';
    const hi = dateHi || dhi?.max || '';
    pills.push(`<span class="fs-pill">${{lo}} → ${{hi}}<button data-action="date">×</button></span>`);
  }} else if (yearFiltering) {{
    pills.push(`<span class="fs-pill">${{yearLo}}–${{yearHi}}<button data-action="year">×</button></span>`);
  }}

  // Recently available
  if (recentDays > 0) {{
    const lbl = {{1:'24h', 7:'7 days', 30:'30 days', 90:'90 days'}}[recentDays] || recentDays+'d';
    pills.push(`<span class="fs-pill">🆕 New ≤ ${{lbl}}<button data-action="recent">×</button></span>`);
  }}

  // Frame order
  if (frameLo > 0 || frameHi < 100) {{
    pills.push(`<span class="fs-pill">⏱ Frames ${{frameLo}}–${{frameHi}}%<button data-action="frame">×</button></span>`);
  }}

  // Missions — one pill per dataset, never one per mission number
  Object.entries(missionActive).forEach(([ds, ms]) => {{
    if (ms === null) return;
    const dsShort = {{'corona2':'CORONA','declassii':'GAMBIT','declassiii':'HEXAGON'}}[ds]||ds;
    const total = (MISSIONS_BY_DS[ds]||[]).length;
    const label = ms.size === 0
      ? `No ${{dsShort}} missions`
      : `${{dsShort}}: ${{ms.size}} of ${{total}} missions`;
    pills.push(`<span class="fs-pill">${{label}}<button data-action="mission-ds" data-val="${{ds}}">×</button></span>`);
  }});

  if (pills.length === 0) {{
    bar.innerHTML = '';
    bar.classList.remove('visible');
    return;
  }}

  bar.innerHTML = pills.join('') +
    `<button class="fs-clear-all" onclick="resetAllFilters()">Clear all</button>`;
  bar.classList.add('visible');

  // Wire up pill ✕ buttons
  bar.querySelectorAll('.fs-pill button').forEach(btn => {{
    btn.addEventListener('click', e => {{
      e.stopPropagation();
      const action = btn.dataset.action;
      if (action === 'sat') {{
        const s = btn.dataset.val;
        satActive[s] = false;
        document.querySelector(`.sat-btn[data-sat="${{s}}"]`)?.classList.remove('on');
        syncMissionsToSat(s, false);
      }} else if (action === 'sat-all') {{
        Object.keys(satActive).forEach(k => satActive[k]=true);
        document.querySelectorAll('.sat-btn').forEach(b=>b.classList.add('on'));
        Object.keys(missionActive).forEach(ds=>{{missionActive[ds]=null;}});
        document.querySelectorAll('.ms-chk').forEach(c=>{{c.checked=true;}});
      }} else if (action === 'cam') {{
        const key = btn.dataset.val;
        cameraActive[key] = true;
        document.querySelector(`.cam-btn[data-ds="${{key.split('|')[0]}}"][data-cam="${{key.split('|')[1]}}"]`)?.classList.add('on');
      }} else if (action === 'exact-date') {{
        dateFilter = null;
      }} else if (action === 'date') {{
        dateLo=''; dateHi='';
        const dlo=document.getElementById('date-lo'), dhi=document.getElementById('date-hi');
        if (dlo) dlo.value=dlo.min; if (dhi) dhi.value=dhi.max;
      }} else if (action === 'year') {{
        yearLo=YEAR_MIN; yearHi=YEAR_MAX; yearFiltering=false; updateSlider();
      }} else if (action === 'mission-ds') {{
        const ds=btn.dataset.val;
        missionActive[ds]=null;
        document.querySelectorAll(`.ms-chk[data-ds="${{ds}}"]`).forEach(c=>{{c.checked=true;}});
      }} else if (action === 'recent') {{
        recentDays = 0;
        document.querySelectorAll('.rc-btn').forEach(b => b.classList.remove('on'));
      }} else if (action === 'frame') {{
        frameLo = 0; frameHi = 100;
        if (window.paintFrameSlider) window.paintFrameSlider();
      }}
      buildLayers();
    }});
  }});
}}

// ── Camera filter ─────────────────────────────────────────────────────────────
document.querySelectorAll('.cam-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const key = btn.dataset.ds + '|' + btn.dataset.cam;
    cameraActive[key] = !cameraActive[key];
    btn.classList.toggle('on', cameraActive[key]);
    buildLayers();
  }});
}});

// ── Frame-order slider ────────────────────────────────────────────────────────
(function initFrameSlider() {{
  const el = document.getElementById('fr-slider');
  const fill = el.querySelector('.fr-fill');
  const thumbs = [0, 1].map(i => {{
    const t = document.createElement('div');
    t.className = 'fr-thumb'; t.tabIndex = 0; t.setAttribute('role', 'slider');
    t.setAttribute('aria-label', i === 0 ? 'Start of range' : 'End of range');
    el.appendChild(t); return t;
  }});
  let drag = null;
  const pos = v => 7 + v / 100 * (el.clientWidth - 14);
  window.paintFrameSlider = function () {{
    thumbs[0].style.left = pos(frameLo) + 'px';
    thumbs[1].style.left = pos(frameHi) + 'px';
    fill.style.left = pos(frameLo) + 'px';
    fill.style.width = Math.max(0, pos(frameHi) - pos(frameLo)) + 'px';
    thumbs[0].setAttribute('aria-valuenow', frameLo);
    thumbs[1].setAttribute('aria-valuenow', frameHi);
    document.getElementById('fr-read').textContent =
      (frameLo === 0 && frameHi === 100) ? 'all frames' : frameLo + '%–' + frameHi + '%';
  }};
  const valAt = clientX => {{
    const r = el.getBoundingClientRect();
    return Math.round(Math.max(0, Math.min(1, (clientX - r.left - 7) / (r.width - 14))) * 100);
  }};
  function apply(v) {{
    if (drag === 0) frameLo = Math.min(v, frameHi); else frameHi = Math.max(v, frameLo);
    window.paintFrameSlider(); buildLayers();
  }}
  el.addEventListener('pointerdown', e => {{
    const v = valAt(e.clientX);
    drag = Math.abs(v - frameLo) <= Math.abs(v - frameHi) ? 0 : 1;
    el.setPointerCapture(e.pointerId); apply(v);
  }});
  el.addEventListener('pointermove', e => {{ if (drag !== null) apply(valAt(e.clientX)); }});
  ['pointerup','pointercancel'].forEach(ev => el.addEventListener(ev, () => {{ drag = null; }}));
  thumbs.forEach((t, i) => t.addEventListener('keydown', e => {{
    const d = e.key === 'ArrowLeft' ? -1 : e.key === 'ArrowRight' ? 1 : 0;
    if (!d) return;
    e.preventDefault(); drag = i; apply((i === 0 ? frameLo : frameHi) + d); drag = null;
  }}));
  window.paintFrameSlider();
}})();

document.querySelectorAll('[data-frame]').forEach(btn => {{
  btn.addEventListener('click', () => {{
    frameLo = 0; frameHi = parseInt(btn.dataset.frame, 10);
    window.paintFrameSlider(); buildLayers();
  }});
}});

// ── Recently-available filter ─────────────────────────────────────────────────
document.querySelectorAll('.rc-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const d = parseInt(btn.dataset.days);
    recentDays = (recentDays === d) ? 0 : d;  // click again to turn off
    document.querySelectorAll('.rc-btn').forEach(b =>
      b.classList.toggle('on', parseInt(b.dataset.days) === recentDays));
    buildLayers();
  }});
}});

// ── Exact date filter ─────────────────────────────────────────────────────────
document.getElementById('date-lo').addEventListener('change', e => {{
  dateLo = e.target.value;
  // Keep year slider in sync
  if (dateLo) {{
    const y = parseInt(dateLo.slice(0,4));
    if (y > yearLo) {{ yearLo = Math.min(y, yearHi); updateSlider(); }}
  }}
  buildLayers();
}});
document.getElementById('date-hi').addEventListener('change', e => {{
  dateHi = e.target.value;
  if (dateHi) {{
    const y = parseInt(dateHi.slice(0,4));
    if (y < yearHi) {{ yearHi = Math.max(y, yearLo); updateSlider(); }}
  }}
  buildLayers();
}});

// ── Dropdown system ───────────────────────────────────────────────────────────
const DD_PAIRS = [
  ['tb-sat',     'dd-sat'],
  ['tb-cam',     'dd-cam'],
  ['tb-date',    'dd-date'],
  ['tb-mission', 'dd-mission'],
  ['tb-frame',   'dd-frame'],
  ['tb-recent',  'dd-recent'],
];

function closeAllDropdowns(except) {{
  DD_PAIRS.forEach(([btnId, panelId]) => {{
    if (panelId === except) return;
    document.getElementById(btnId).classList.remove('active');
    document.getElementById(panelId).classList.remove('open');
  }});
}}

DD_PAIRS.forEach(([btnId, panelId]) => {{
  const btn   = document.getElementById(btnId);
  const panel = document.getElementById(panelId);
  btn.addEventListener('click', e => {{
    e.stopPropagation();
    const opening = !panel.classList.contains('open');
    closeAllDropdowns(null);
    if (opening) {{
      // Position relative to #filters, clamped so panel doesn't overflow right edge
      const filtersEl = document.getElementById('filters');
      const filtersRect = filtersEl.getBoundingClientRect();
      const btnRect = btn.getBoundingClientRect();
      const panelWidth = 280;
      let left = btnRect.left - filtersRect.left;
      if (left + panelWidth > filtersRect.width) {{
        left = filtersRect.width - panelWidth;
      }}
      panel.style.left = Math.max(0, left) + 'px';
      btn.classList.add('active');
      panel.classList.add('open');
    }}
  }});
}});

// Close on outside click
document.addEventListener('click', () => closeAllDropdowns(null));
document.querySelectorAll('.dd-panel').forEach(p =>
  p.addEventListener('click', e => e.stopPropagation()));

// Update toolbar button state to reflect active filters
function updateToolbarState() {{
  // Which datasets have at least one active satellite?
  const activeDsSet = new Set(
    Object.entries(satActive).filter(([,v])=>v).map(([k])=>SAT_DS[k]).filter(Boolean)
  );

  // Show/hide camera chips by dataset
  let anyCamVisible = false;
  document.querySelectorAll('.cam-btn').forEach(b => {{
    const visible = activeDsSet.has(b.dataset.ds);
    b.style.display = visible ? '' : 'none';
    if (visible) anyCamVisible = true;
  }});
  document.getElementById('tb-cam').style.display = anyCamVisible ? '' : 'none';

  // Show/hide mission groups AND individual missions by active satellites
  const activeSats = new Set(Object.entries(satActive).filter(([,v])=>v).map(([k])=>k));

  let anyMissionVisible = false;
  document.querySelectorAll('.ms-group').forEach(g => {{
    const ds = g.dataset.ds;
    if (!activeDsSet.has(ds)) {{
      g.style.display = 'none';
      return;
    }}
    // Within the group, show only missions belonging to an active satellite
    let anyItemVisible = false;
    g.querySelectorAll('.ms-item').forEach(item => {{
      const chk = item.querySelector('.ms-chk');
      const mission = chk?.value;
      // Check if this mission belongs to any active satellite
      const missionBelongsToActiveSat = [...activeSats].some(sat => {{
        const satMissions = SAT_MISSION_MAP[sat] || [];
        return satMissions.includes(mission);
      }});
      item.style.display = missionBelongsToActiveSat ? '' : 'none';
      if (missionBelongsToActiveSat) anyItemVisible = true;
    }});
    g.style.display = anyItemVisible ? '' : 'none';
    if (anyItemVisible) anyMissionVisible = true;
  }});
  document.getElementById('tb-mission').style.display = anyMissionVisible ? '' : 'none';

  // Toolbar has-filter states
  const anySat = Object.values(satActive).some(Boolean);
  const allSat = Object.values(satActive).every(Boolean);
  document.getElementById('tb-sat').classList.toggle('has-filter', anySat && !allSat);

  const camOff = Object.entries(cameraActive)
    .some(([key, v]) => !v && activeDsSet.has(key.split('|')[0]));
  document.getElementById('tb-cam').classList.toggle('has-filter', camOff);

  const dateFiltered = (dateLo && dateLo !== document.getElementById('date-lo').min) ||
                       (dateHi && dateHi !== document.getElementById('date-hi').max) ||
                       yearFiltering;
  document.getElementById('tb-date').classList.toggle('has-filter', dateFiltered);

  const missionFiltered = Object.entries(missionActive)
    .some(([ds, ms]) => ms !== null && activeDsSet.has(ds));
  document.getElementById('tb-mission').classList.toggle('has-filter', missionFiltered);

  document.getElementById('tb-recent').classList.toggle('has-filter', recentDays > 0);
  document.getElementById('tb-frame').classList.toggle('has-filter', frameLo > 0 || frameHi < 100);
}}

// ── Mission checkboxes
document.querySelectorAll('.ms-chk').forEach(chk => {{
  chk.addEventListener('change', () => {{
    const ds = chk.dataset.ds;
    const checked = [...document.querySelectorAll(`.ms-chk[data-ds="${{ds}}"]`)]
      .filter(c => c.checked).map(c => c.value);
    const all = MISSIONS_BY_DS[ds] || [];
    missionActive[ds] = checked.length === all.length ? null : new Set(checked);
    buildLayers();
  }});
}});

// Mission all/none buttons
document.querySelectorAll('.ms-all').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const ds     = btn.dataset.ds;
    const all    = btn.dataset.action === 'all';
    document.querySelectorAll(`.ms-chk[data-ds="${{ds}}"]`).forEach(c => {{ c.checked = all; }});
    missionActive[ds] = all ? null : new Set();
    buildLayers();
  }});
}});

// ── Basemap ───────────────────────────────────────────────────────────────────
// [data-bm] only — #globe-btn and #sb-toggle reuse the .bm-btn styling
document.querySelectorAll('.bm-btn[data-bm]').forEach(btn =>
  btn.addEventListener('click', () => setBasemap(btn.dataset.bm)));

// ── Search with zoom ─────────────────────────────────────────────────────────
let st;
// ── Coordinate search ──────────────────────────────────────────────────────────
// Matches "26.311583, 82.444639" (comma, whitespace, or both between the numbers)
let coordMarker = null;
function tryCoordSearch(str) {{
  const m = str.match(/^\\s*(-?\\d{{1,3}}(?:\\.\\d+)?)\\s*[,\\s]\\s*(-?\\d{{1,3}}(?:\\.\\d+)?)\\s*$/);
  if (!m) return false;
  const lat = parseFloat(m[1]), lon = parseFloat(m[2]);
  if (!isFinite(lat) || !isFinite(lon)) return false;
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return false;
  if (coordMarker) map.removeLayer(coordMarker);
  coordMarker = L.marker([lat, lon]).addTo(map)
    .bindPopup(lat.toFixed(6) + ', ' + lon.toFixed(6));
  if (globeMode) {{ document.getElementById('globe-btn').click(); }}
  map.setView([lat, lon], 11);
  coordMarker.openPopup();
  return true;
}}

document.getElementById('search').addEventListener('input', e => {{
  clearTimeout(st);
  st = setTimeout(() => {{
    searchQ = e.target.value.trim();
    // Coordinate jump takes priority over entity-ID filtering
    if (tryCoordSearch(searchQ)) {{ searchQ = ''; return; }}
    if (coordMarker) {{ map.removeLayer(coordMarker); coordMarker = null; }}
    buildLayers();
    if (searchQ.length >= 4) {{
      const matches = GEOJSON.features.filter(f =>
        (f.properties.displayId || '').toLowerCase().includes(searchQ.toLowerCase()) ||
        (f.properties.entityId  || '').toLowerCase().includes(searchQ.toLowerCase())
      );
      if (matches.length === 1) {{
        const b = L.geoJSON(matches[0]).getBounds();
        if (b.isValid()) map.fitBounds(b, {{padding:[40,40], maxZoom:10}});
      }} else if (matches.length > 1 && matches.length <= 50) {{
        const group = L.featureGroup(matches.map(f => L.geoJSON(f)));
        const b = group.getBounds();
        if (b.isValid()) map.fitBounds(b, {{padding:[40,40], maxZoom:8}});
      }}
    }}
  }}, 300);
}});

// ── Overlays ──────────────────────────────────────────────────────────────────
// Hardcoded Cold War sites (instant, no API needed)
const CW_SILOS = [
  // United States ICBM fields
  {{n:"Minot AFB (Minuteman III)",         lat:48.4156, lon:-101.3580, k:"silos"}},
  {{n:"Malmstrom AFB (Minuteman III)",      lat:47.5077, lon:-111.1838, k:"silos"}},
  {{n:"F.E. Warren AFB (Minuteman III)",    lat:41.1450, lon:-104.8692, k:"silos"}},
  {{n:"Grand Forks AFB silo field",         lat:47.9611, lon:-97.4011,  k:"silos"}},
  {{n:"Ellsworth AFB (Minuteman II)",       lat:44.1451, lon:-103.1035, k:"silos"}},
  {{n:"Whiteman AFB (Minuteman II)",        lat:38.7279, lon:-93.5479,  k:"silos"}},
  {{n:"McConnell AFB (Titan II)",           lat:37.6218, lon:-97.2682,  k:"silos"}},
  {{n:"Davis-Monthan AFB (Titan II)",       lat:32.1665, lon:-110.8831, k:"silos"}},
  {{n:"Little Rock AFB (Titan II)",         lat:34.9169, lon:-92.1498,  k:"silos"}},
  {{n:"Vandenberg SFB (test silos)",        lat:34.7420, lon:-120.5724, k:"silos"}},
  {{n:"Atlas F Silo — Wichita KS",          lat:37.5420, lon:-97.6350,  k:"silos"}},
  {{n:"Atlas E Silo — Fairchild AFB",       lat:47.6151, lon:-117.9559, k:"silos"}},
  {{n:"Titan I Complex — Lowry AFB",        lat:39.7220, lon:-104.5950, k:"silos"}},
  {{n:"Peacekeeper silo — Warren",          lat:41.1500, lon:-104.8200, k:"silos"}},
  // Soviet / Russian ICBM fields
  {{n:"Plesetsk Cosmodrome (ICBM test)",    lat:62.9271, lon:40.5777,   k:"silos"}},
  {{n:"Dombarovsky ICBM field",             lat:50.7936, lon:59.8586,   k:"silos"}},
  {{n:"Kozelsk ICBM field (SS-19)",         lat:54.0363, lon:35.7847,   k:"silos"}},
  {{n:"Tatishchevo ICBM field (SS-19)",     lat:51.6736, lon:45.9730,   k:"silos"}},
  {{n:"Uzhur ICBM field (SS-18 Satan)",     lat:55.3000, lon:89.8167,   k:"silos"}},
  {{n:"Pervomaysk SS-24 silos (Ukraine)",   lat:48.0450, lon:30.8550,   k:"silos"}},
  {{n:"Derazhnya SS-19 silos (Ukraine)",    lat:49.2614, lon:27.3972,   k:"silos"}},
  {{n:"Kartaly ICBM field",                 lat:53.0667, lon:60.6833,   k:"silos"}},
  {{n:"Bershet ICBM field (Perm)",          lat:57.9500, lon:55.9500,   k:"silos"}},
  {{n:"Aleysk ICBM field (Siberia)",        lat:52.5000, lon:82.8000,   k:"silos"}},
  {{n:"Zhangiz-Tobe (Kazakhstan)",          lat:49.8000, lon:82.2000,   k:"silos"}},
  {{n:"Plokštinė R-12 MRBM (Lithuania)",   lat:55.8506, lon:22.0428,   k:"silos"}},
  {{n:"Gyrovoye SS-20 depot (Russia)",      lat:55.1500, lon:37.5000,   k:"silos"}},
  // China
  {{n:"DF-5 Silo Field — Luoning",          lat:34.3900, lon:111.6700,  k:"silos"}},
  {{n:"DF-41 Silo Field — Yumen",           lat:40.2800, lon:97.0500,   k:"silos"}},
  {{n:"DF-41 Silo Field — Hami",            lat:42.8000, lon:93.5000,   k:"silos"}},
  // France
  {{n:"Plateau d'Albion S-3 IRBM field",   lat:44.1167, lon:5.6167,    k:"silos"}},
];

const CW_AIRBASES = [
  // USA Cold War strategic airbases
  {{n:"Thule Air Base (Greenland)",         lat:76.5311, lon:-68.7032,  k:"airbases"}},
  {{n:"Eielson AFB (Alaska)",               lat:64.6654, lon:-147.1021, k:"airbases"}},
  {{n:"Elmendorf AFB (Alaska)",             lat:61.2507, lon:-149.8066, k:"airbases"}},
  {{n:"Loring AFB (Maine)",                 lat:46.9496, lon:-67.8879,  k:"airbases"}},
  {{n:"Plattsburgh AFB (New York)",         lat:44.6509, lon:-73.4682,  k:"airbases"}},
  {{n:"Griffiss AFB (New York)",            lat:43.2338, lon:-75.4068,  k:"airbases"}},
  {{n:"Westover AFB (Massachusetts)",       lat:42.1963, lon:-72.5348,  k:"airbases"}},
  {{n:"Barksdale AFB (Louisiana)",          lat:32.5018, lon:-93.6627,  k:"airbases"}},
  {{n:"Dyess AFB (Texas)",                  lat:32.4208, lon:-99.8543,  k:"airbases"}},
  {{n:"Ellsworth AFB (South Dakota)",       lat:44.1451, lon:-103.1035, k:"airbases"}},
  {{n:"Offutt AFB — SAC HQ (Nebraska)",     lat:41.1182, lon:-95.9124,  k:"airbases"}},
  {{n:"Minot AFB (North Dakota)",           lat:48.4156, lon:-101.3580, k:"airbases"}},
  {{n:"Malmstrom AFB (Montana)",            lat:47.5077, lon:-111.1838, k:"airbases"}},
  {{n:"March AFB (California)",             lat:33.8808, lon:-117.2590, k:"airbases"}},
  {{n:"Castle AFB (California)",            lat:37.3808, lon:-120.5680, k:"airbases"}},
  {{n:"Fairchild AFB (Washington)",         lat:47.6151, lon:-117.6559, k:"airbases"}},
  {{n:"Grand Forks AFB (North Dakota)",     lat:47.9611, lon:-97.4011,  k:"airbases"}},
  {{n:"Seymour Johnson AFB (NC)",           lat:35.3394, lon:-77.9606,  k:"airbases"}},
  {{n:"Sawyer AFB (Michigan)",              lat:46.3528, lon:-87.3952,  k:"airbases"}},
  // NATO Europe forward bases
  {{n:"RAF Lakenheath (UK)",                lat:52.4093, lon:0.5610,    k:"airbases"}},
  {{n:"RAF Mildenhall (UK)",                lat:52.3619, lon:0.4864,    k:"airbases"}},
  {{n:"RAF Upper Heyford (UK)",             lat:51.9333, lon:-1.2333,   k:"airbases"}},
  {{n:"RAF Greenham Common (UK)",           lat:51.3667, lon:-1.3000,   k:"airbases"}},
  {{n:"Ramstein AB (West Germany)",         lat:49.4369, lon:7.6003,    k:"airbases"}},
  {{n:"Spangdahlem AB (West Germany)",      lat:49.9726, lon:6.6925,    k:"airbases"}},
  {{n:"Bitburg AB (West Germany)",          lat:49.9455, lon:6.5648,    k:"airbases"}},
  {{n:"Hahn AB (West Germany)",             lat:50.0133, lon:7.2686,    k:"airbases"}},
  {{n:"Zweibrücken AB (West Germany)",      lat:49.2094, lon:7.4003,    k:"airbases"}},
  {{n:"Soesterberg AB (Netherlands)",       lat:52.1277, lon:5.2761,    k:"airbases"}},
  {{n:"Volkel AB (Netherlands)",            lat:51.6564, lon:5.7073,    k:"airbases"}},
  {{n:"Kleine Brogel AB (Belgium)",         lat:51.1683, lon:5.4700,    k:"airbases"}},
  {{n:"Aviano AB (Italy)",                  lat:46.0319, lon:12.5966,   k:"airbases"}},
  {{n:"Incirlik AB (Turkey)",               lat:37.0021, lon:35.4258,   k:"airbases"}},
  {{n:"Torrejon AB (Spain)",                lat:40.4967, lon:-3.4456,   k:"airbases"}},
  {{n:"Morón AB (Spain)",                   lat:37.1749, lon:-5.6149,   k:"airbases"}},
  {{n:"Keflavík NAS (Iceland)",             lat:63.9850, lon:-22.6056,  k:"airbases"}},
  {{n:"Andøya Air Base (Norway)",           lat:69.2925, lon:16.1444,   k:"airbases"}},
  {{n:"Bodø Main Air Station (Norway)",     lat:67.2692, lon:14.3653,   k:"airbases"}},
  // Soviet / Warsaw Pact strategic airbases
  {{n:"Kubinka AB (Soviet bombers)",        lat:55.6113, lon:36.6597,   k:"airbases"}},
  {{n:"Engel's AB (Tu-95 Bears)",           lat:51.4629, lon:46.1771,   k:"airbases"}},
  {{n:"Ryazan Dyagilevo (Tu-22)",           lat:54.6147, lon:39.5714,   k:"airbases"}},
  {{n:"Mochische AB (Tu-95)",               lat:54.8400, lon:82.9400,   k:"airbases"}},
  {{n:"Dolon AB (Tu-95 Bears)",             lat:49.9467, lon:76.0300,   k:"airbases"}},
  {{n:"Ukrainka AB (Tu-95/160 Bears)",      lat:51.1694, lon:128.4469,  k:"airbases"}},
  {{n:"Soltsy-2 AB (Tu-16 Badgers)",        lat:58.1400, lon:30.3000,   k:"airbases"}},
  {{n:"Zhukovka AB (Blackjacks)",           lat:53.5700, lon:33.7500,   k:"airbases"}},
  {{n:"Mirgorod AB (Ukraine)",              lat:49.9553, lon:33.6136,   k:"airbases"}},
  {{n:"Bykhov AB (Belarus)",                lat:53.5167, lon:30.2333,   k:"airbases"}},
  {{n:"Templin AB (East Germany)",          lat:53.1167, lon:13.5000,   k:"airbases"}},
  {{n:"Wittstock AB (East Germany)",        lat:53.2167, lon:12.5000,   k:"airbases"}},
  {{n:"Welzow AB (East Germany)",           lat:51.5833, lon:14.1333,   k:"airbases"}},
  {{n:"Legnica AB (Poland)",                lat:51.2000, lon:16.2000,   k:"airbases"}},
  {{n:"Lask AB (Poland)",                   lat:51.5517, lon:19.1808,   k:"airbases"}},
];

// OurAirports CSV URL — fetched once, parsed client-side, filtered to military
const OURAIRPORTS_URL = 'https://davidmegginson.github.io/ourairports-data/airports.csv';

const ovLayers = {{}};
let ourairportsCache = null;

function ovMarker(lat, lon, name, key) {{
  const colors = {{silos:'#ff4d4d', airbases:'#4d9fff'}};
  const c = colors[key] || '#aaa';
  return L.circleMarker([lat, lon], {{
    radius:5, color:c, fillColor:c, fillOpacity:.75, weight:1.5, opacity:.9
  }}).bindPopup(
    `<div style="font-size:11px;color:#ccc;background:#141414;padding:6px 10px;border-radius:6px;max-width:200px">${{name}}</div>`,
    {{className:'ov-popup', closeButton:false}}
  );
}}

// Parse the OurAirports CSV (only grab the columns we need)
function parseOurAirportsCSV(text) {{
  const lines = text.split('\\n');
  const header = lines[0].split(',').map(h => h.replace(/"/g,'').trim());
  const iName = header.indexOf('name');
  const iLat  = header.indexOf('latitude_deg');
  const iLon  = header.indexOf('longitude_deg');
  const iType = header.indexOf('type');
  const results = [];
  for (let i = 1; i < lines.length; i++) {{
    // Simple CSV parse — handles quoted fields
    const row = lines[i].match(/(".*?"|[^,]+|(?<=,)(?=,)|(?<=,)$|^(?=,))/g);
    if (!row) continue;
    const clean = row.map(v => v.replace(/^"|"$/g,'').trim());
    if (clean[iType] === 'military' && clean[iLat] && clean[iLon]) {{
      const lat = parseFloat(clean[iLat]);
      const lon = parseFloat(clean[iLon]);
      if (!isNaN(lat) && !isNaN(lon)) {{
        results.push({{n: clean[iName] || 'Military Airport', lat, lon, k:'airbases'}});
      }}
    }}
  }}
  return results;
}}

async function toggleOverlay(key) {{
  const btn = document.querySelector(`.ov-btn[data-ov="${{key}}"]`);

  // Toggle off if already showing
  if (ovLayers[key]) {{
    map.removeLayer(ovLayers[key]);
    delete ovLayers[key];
    btn?.classList.remove('on');
    updateOvToggle();
    return;
  }}

  if (btn) {{ btn.disabled = true; btn.style.opacity = '0.5'; }}

  try {{
    let points = [];

    if (key === 'silos') {{
      points = CW_SILOS;

    }} else if (key === 'airbases') {{
      // Start with hardcoded Cold War bases immediately
      points = [...CW_AIRBASES];

      // Then fetch OurAirports for comprehensive global military airports
      if (!ourairportsCache) {{
        try {{
          const resp = await fetch(OURAIRPORTS_URL);
          if (resp.ok) {{
            const text = await resp.text();
            ourairportsCache = parseOurAirportsCSV(text);
          }}
        }} catch(e) {{
          console.warn('OurAirports fetch failed, using hardcoded only:', e);
        }}
      }}
      if (ourairportsCache) {{
        // Merge: deduplicate by proximity (skip if within 5km of a hardcoded site)
        const merged = [...CW_AIRBASES];
        for (const ap of ourairportsCache) {{
          const tooClose = CW_AIRBASES.some(cw =>
            Math.abs(cw.lat - ap.lat) < 0.05 && Math.abs(cw.lon - ap.lon) < 0.05
          );
          if (!tooClose) merged.push(ap);
        }}
        points = merged;
      }}
    }}

    const layer = L.layerGroup(points.map(p => ovMarker(p.lat, p.lon, p.n, key)));
    layer.addTo(map);
    ovLayers[key] = layer;
    btn?.classList.add('on');
    const badge = document.getElementById(`badge-${{key}}`);
    if (badge) badge.textContent = points.length;
    updateOvToggle();

  }} catch(e) {{
    console.error('Overlay error:', e);
  }}

  if (btn) {{
    btn.disabled = false;
    btn.style.opacity = '';
    if (ovLayers[key]) {{
      const badge = document.getElementById(`badge-${{key}}`);
      if (badge) badge.textContent = ovLayers[key].getLayers().length;
    }}
  }}
}}

function updateOvToggle() {{
  const tog = document.getElementById('ov-toggle');
  if (tog) tog.classList.toggle('has-active', Object.keys(ovLayers).length > 0);
}}

// ── Published toggle ──────────────────────────────────────────────────────────
document.getElementById('published-toggle').addEventListener('click', () => {{
  hidePublished = !hidePublished;
  const btn = document.getElementById('published-toggle');
  btn.classList.toggle('on', hidePublished);
  btn.childNodes[2].textContent = hidePublished ? 'Show published' : 'Hide published';
  buildLayers();
}});

// ── Unscanned toggle ──────────────────────────────────────────────────────────
document.getElementById('unscanned-toggle').addEventListener('click', () => {{
  showUnscanned = !showUnscanned;
  const btn = document.getElementById('unscanned-toggle');
  btn.classList.toggle('on', showUnscanned);
  btn.childNodes[2].textContent = showUnscanned ? 'Scanned only' : 'All scenes';
  buildLayers();
}});

document.getElementById('ov-toggle').addEventListener('click', () => {{
  const panel = document.getElementById('ov-panel');
  const tog   = document.getElementById('ov-toggle');
  panel.classList.toggle('open');
  tog.classList.toggle('open');
}});
document.querySelectorAll('.ov-btn').forEach(btn =>
  btn.addEventListener('click', () => toggleOverlay(btn.dataset.ov))
);

// ── M2M Download ──────────────────────────────────────────────────────────────
const M2M = 'https://m2m.cr.usgs.gov/api/api/json/stable/';
async function m2mPost(endpoint, body, apiKey) {{
  const headers = {{'Content-Type':'application/json'}};
  if (apiKey) headers['X-Auth-Token'] = apiKey;
  const resp = await fetch(M2M + endpoint, {{method:'POST', headers, body:JSON.stringify(body)}});
  if (!resp.ok) throw new Error(`HTTP ${{resp.status}} on ${{endpoint}}`);
  const data = await resp.json();
  if (data.errorCode) throw new Error(data.errorMessage || data.errorCode);
  return data.data;
}}
let dlEid = null, dlDs = null;
function openDownloadModal(entityId, dataset) {{
  dlEid = entityId; dlDs = dataset;
  document.getElementById('dl-scene-id').textContent = entityId;
  document.getElementById('dl-status').textContent = '';
  document.getElementById('dl-status').className = '';
  document.getElementById('dl-go').disabled = false;
  const saved = JSON.parse(localStorage.getItem('m2m_creds') || 'null');
  if (saved) {{
    document.getElementById('dl-user').value  = saved.user  || '';
    document.getElementById('dl-token').value = saved.token || '';
    document.getElementById('dl-remember').checked = true;
  }}
  document.getElementById('dl-modal').classList.add('open');
}}
document.getElementById('dl-cancel').addEventListener('click', () =>
  document.getElementById('dl-modal').classList.remove('open'));
document.getElementById('dl-modal').addEventListener('click', e => {{
  if (e.target === document.getElementById('dl-modal'))
    document.getElementById('dl-modal').classList.remove('open');
}});
document.getElementById('dl-go').addEventListener('click', async () => {{
  const username = document.getElementById('dl-user').value.trim();
  const token    = document.getElementById('dl-token').value.trim();
  if (!username || !token) {{ setDlStatus('Enter username and token.','err'); return; }}
  if (document.getElementById('dl-remember').checked)
    localStorage.setItem('m2m_creds', JSON.stringify({{user:username, token}}));
  else localStorage.removeItem('m2m_creds');
  const btn = document.getElementById('dl-go');
  btn.disabled = true;
  const setDlStatus = (msg, cls='') => {{
    const el = document.getElementById('dl-status');
    el.textContent = msg; el.className = cls;
  }};
  try {{
    setDlStatus('Logging in…');
    const apiKey = await m2mPost('login-token', {{username, token}});
    try {{
      setDlStatus('Fetching download options…');
      const options = await m2mPost('download-options', {{datasetName:dlDs, entityIds:[dlEid]}}, apiKey);
      const avail = (options||[]).filter(o=>o.available);
      if (!avail.length) throw new Error('No downloadable products for this scene.');
      const product = avail.find(o=>/bundle/i.test(o.productName)) || avail[0];
      setDlStatus('Requesting download URL…');
      const dlResult = await m2mPost('download-request', {{
        downloads:[{{entityId:dlEid, productId:product.id}}], label:'declass_map'
      }}, apiKey);
      let url = dlResult?.availableDownloads?.[0]?.url;
      if (!url && dlResult?.preparingDownloads?.length) {{
        setDlStatus('Staging — polling…');
        const deadline = Date.now() + 120_000;
        while (Date.now() < deadline) {{
          await new Promise(r => setTimeout(r, 5000));
          setDlStatus(`Polling… (${{Math.round((deadline-Date.now())/1000)}}s left)`);
          const ret = await m2mPost('download-retrieve', {{label:'declass_map'}}, apiKey);
          url = ret?.available?.[0]?.url;
          if (url) break;
        }}
      }}
      if (!url) throw new Error('Timed out. Try again shortly.');
      setDlStatus('Starting download…','ok');
      const a = document.createElement('a');
      a.href=url; a.download=''; a.target='_blank';
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      setDlStatus(`✓ Download started — ${{product.productName}}`,'ok');
    }} finally {{
      try {{ await m2mPost('logout', {{}}, apiKey); }} catch(e) {{}}
    }}
  }} catch(err) {{
    document.getElementById('dl-status').textContent = `Error: ${{err.message}}`;
    document.getElementById('dl-status').className = 'err';
    btn.disabled = false;
  }}
}});

// ── USGS status check ─────────────────────────────────────────────────────────
async function checkUsgsStatus() {{
  const el = document.getElementById('usgs-status');
  const label = document.getElementById('status-label');
  el.className = 'checking'; label.textContent = 'USGS …';
  try {{
    const ctrl = new AbortController();
    const tid  = setTimeout(() => ctrl.abort(), 8000);
    await fetch('https://m2m.cr.usgs.gov/api/api/json/stable/', {{
      method:'GET', signal:ctrl.signal, mode:'no-cors', cache:'no-store'
    }});
    clearTimeout(tid);
    el.className = 'up'; label.textContent = 'USGS Online';
    el.title = `USGS online as of ${{new Date().toLocaleTimeString()}}`;
  }} catch(err) {{
    el.className = 'down';
    label.textContent = err.name === 'AbortError' ? 'USGS Timeout' : 'USGS Down';
    el.title = `USGS unreachable at ${{new Date().toLocaleTimeString()}}`;
  }}
}}
checkUsgsStatus();
setInterval(checkUsgsStatus, 60_000);

// ── Shareable view in the URL ─────────────────────────────────────────────────
// Keeps position in the address bar so a view can be linked. replaceState, not
// pushState — panning shouldn't fill up the back button. Other params
// (from/to/mmdd) are preserved.
let urlT;
function syncUrl() {{
  clearTimeout(urlT);
  urlT = setTimeout(() => {{
    const p = new URLSearchParams(window.location.search);
    ['lat', 'lon', 'z', 'alt', 'globe'].forEach(k => p.delete(k));
    if (globeMode) {{
      p.set('globe', '1');
      const pov = globeInstance && globeInstance.pointOfView();
      if (pov) {{
        p.set('lat', pov.lat.toFixed(4));
        p.set('lon', pov.lng.toFixed(4));
        p.set('alt', pov.altitude.toFixed(3));
      }}
    }} else {{
      const c = map.getCenter();
      p.set('lat', c.lat.toFixed(4));
      p.set('lon', (((c.lng + 180) % 360 + 360) % 360 - 180).toFixed(4));  // unwrap
      p.set('z', String(map.getZoom()));
    }}
    const qs = p.toString();
    history.replaceState(null, '', qs ? '?' + qs : window.location.pathname);
  }}, 400);
}}
map.on('moveend', syncUrl);

// Defined here but called at the very end of the script: restoring a globe view
// clicks #globe-btn, whose listener is registered further down.
function restoreView() {{
  const p = new URLSearchParams(window.location.search);
  const lat = parseFloat(p.get('lat')), lon = parseFloat(p.get('lon'));
  const hasPos = isFinite(lat) && isFinite(lon);
  if (p.get('globe') === '1') {{
    const alt = parseFloat(p.get('alt'));
    pendingGlobePov = hasPos
      ? {{lat: lat, lng: lon, altitude: isFinite(alt) ? alt : 2.5}} : null;
    document.getElementById('globe-btn').click();
  }} else if (hasPos) {{
    const z = parseInt(p.get('z'), 10);
    map.setView([lat, lon], isFinite(z) ? z : map.getZoom());
  }}
}}

// ── URL param date filter ──────────────────────────────────────────────────────
(function() {{
  const params = new URLSearchParams(window.location.search);
  const from = params.get('from');
  const to   = params.get('to') || params.get('from');
  const mmdd = params.get('mmdd'); // MM-DD only filter e.g. ?mmdd=07-13

  if (mmdd && /^\\d{{2}}-\\d{{2}}$/.test(mmdd)) {{
    filterMMDD = mmdd;
    buildLayers();
    // Open date dropdown so user can see something is active
    const tbDate = document.getElementById('tb-date');
    const ddDate = document.getElementById('dd-date');
    if (tbDate && ddDate) {{
      tbDate.classList.add('active');
      ddDate.classList.add('open');
    }}
    return;
  }}

  if (!from) return;
  const dlo = document.getElementById('date-lo');
  const dhi = document.getElementById('date-hi');
  if (dlo && from >= dlo.min && from <= dlo.max) {{
    dlo.value = from;
    dlo.dispatchEvent(new Event('change'));
  }}
  if (dhi && to >= dhi.min && to <= dhi.max) {{
    dhi.value = to;
    dhi.dispatchEvent(new Event('change'));
  }}
  const tbDate = document.getElementById('tb-date');
  const ddDate = document.getElementById('dd-date');
  if (tbDate && ddDate) {{
    tbDate.classList.add('active');
    ddDate.classList.add('open');
  }}
}})();

// ── Globe view ────────────────────────────────────────────────────────────────
// Footprint polygons are one 3D mesh each, so they're draw-call bound: ~10fps at
// 3,000 and unusable beyond. The heatmap is a single batched layer and holds
// 60fps with the whole archive. So: heatmap while the view is crowded, real
// footprints once few enough are actually on screen.
// Measured: 500 polys ~59fps, 1000 ~28, 1500 ~18, 3000 ~10. 1500 is the point
// where dragging is still usable; past that it turns to slideshow.
const GLOBE_POLY_CAP = 1500;
let globeFeats = [];      // current selection, minus date-line crossers
let globeHeatPts = [];    // [lng, lat] per feature, rebuilt only on filter change
let globeCells = [];      // coarse {{lat,lng,n}} bins, used to build count bubbles
let globeLayerMode = '';

// Single hue ramping through lightness, rather than a rainbow: denser always
// reads as brighter. Low densities fade out so the globe isn't washed in colour.
function heatColor(t) {{
  // globe.gl calls this with no value while the layer initialises
  const raw = isFinite(t) ? Math.max(0, Math.min(1, t)) : 0;
  // Coverage is heavily skewed — a handful of cells dwarf the rest — so a
  // linear ramp leaves most of the map in the near-black end and only the
  // peaks visible. Gamma-lift so moderate density still reads.
  const s = Math.pow(raw, 0.7);
  // Alpha climbs quickly: most cells sit low in the range, so a slow ramp left
  // almost the whole field invisible.
  const stops = [[26,22,70,0.00], [78,40,120,0.62], [168,60,96,0.82],
                 [232,124,46,0.92], [252,204,124,0.97], [255,248,232,1.00]];
  const f = s * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(f)), k = f - i;
  const a = stops[i], b = stops[i + 1];
  const mix = n => Math.round(a[n] + (b[n] - a[n]) * k);
  const alpha = (a[3] + (b[3] - a[3]) * k) * heatDim;
  return 'rgba(' + mix(0) + ',' + mix(1) + ',' + mix(2) + ',' + alpha.toFixed(3) + ')';
}}

// Zoomed right out the heatmap IS the information. Zoomed in, the whole view is
// dense so it just saturates — fade it back there and let the counts carry it.
let heatDim = 1, heatDimApplied = -1;
function heatDimFor(altitude) {{
  const t = Math.max(0, Math.min(1, (altitude - 0.25) / 1.05));
  return 0.3 + 0.7 * t;
}}

// Coarse bins over the current selection; clustered per camera move to make
// the count bubbles. Binning first keeps clustering cheap on 100k+ scenes.
function buildGlobeCells() {{
  const step = 2, m = new Map();
  for (const f of globeFeats) {{
    const c = featCentroid(f);
    if (!c) continue;
    const gx = Math.floor((c[1] + 180) / step), gy = Math.floor((c[0] + 90) / step);
    const k = gx + ',' + gy;
    let e = m.get(k);
    if (!e) m.set(k, e = {{lat: (gy + 0.5) * step - 90, lng: (gx + 0.5) * step - 180, n: 0}});
    e.n++;
  }}
  globeCells = [...m.values()];
}}

// Greedy clustering by angular distance, seeded from the busiest bin. The
// threshold scales with the visible angle, so bubbles stay evenly spaced on
// screen at any zoom — the same idea as screen-space marker clustering.
function clusterCells(maxAngle, pov) {{
  // Only cluster what's on screen. Clustering the whole globe produced
  // thousands of groups when zoomed in, nearly all of them behind the horizon.
  const D = Math.PI / 180;
  // Stop short of the limb: the surface is edge-on there, so bubbles pile up in
  // a crowded rim however well they're spaced in angular terms.
  const cosView = Math.cos(Math.min(Math.PI / 2, (maxAngle / 0.24) * 0.85));
  const vla = pov.lat * D, vlo = pov.lng * D;
  const vs = Math.sin(vla), vc = Math.cos(vla);
  const pts = globeCells.filter(c => {{
    const la = c.lat * D, lo = c.lng * D;
    return vs * Math.sin(la) + vc * Math.cos(la) * Math.cos(lo - vlo) >= cosView;
  }}).sort((a, b) => b.n - a.n);
  const used = new Array(pts.length).fill(false);
  const cosT = Math.cos(maxAngle);
  const out = [];
  for (let i = 0; i < pts.length; i++) {{
    if (used[i]) continue;
    used[i] = true;
    const la1 = pts[i].lat * D, lo1 = pts[i].lng * D;
    const s1 = Math.sin(la1), c1 = Math.cos(la1);
    let sx = 0, sy = 0, sz = 0, tot = 0;
    const add = q => {{
      const la = q.lat * D, lo = q.lng * D, cl = Math.cos(la);
      sx += cl * Math.cos(lo) * q.n; sy += cl * Math.sin(lo) * q.n; sz += Math.sin(la) * q.n;
      tot += q.n;
    }};
    add(pts[i]);
    for (let j = i + 1; j < pts.length; j++) {{
      if (used[j]) continue;
      const la2 = pts[j].lat * D, lo2 = pts[j].lng * D;
      if (s1 * Math.sin(la2) + c1 * Math.cos(la2) * Math.cos(lo2 - lo1) >= cosT) {{
        used[j] = true; add(pts[j]);
      }}
    }}
    const len = Math.sqrt(sx * sx + sy * sy + sz * sz) || 1;
    out.push({{lat: Math.asin(sz / len) / D, lng: Math.atan2(sy, sx) / D, n: tot}});
  }}
  return out;
}}

function featCentroid(f) {{
  if (f._cc !== undefined) return f._cc;
  const bb = featBBox(f);
  return (f._cc = bb ? [(bb[1] + bb[3]) / 2, (bb[0] + bb[2]) / 2] : null);  // [lat, lng]
}}

// Angular radius of what's actually on screen. Two limits apply: the horizon
// (acos(1/d)) and the camera's field of view. Zoomed in the FOV dominates by a
// long way — at altitude 0.08 the horizon is 22 deg but the FOV shows ~2 deg,
// so using the horizon alone kept the view "crowded" no matter how far you zoomed.
function visibleAngle(altitude) {{
  const d = 1 + Math.max(altitude, 0.001);
  const horizon = Math.acos(Math.min(1, 1 / d));
  let fov = 50, aspect = 1.6;
  try {{
    const cam = globeInstance.camera();
    fov = cam.fov || 50;
    aspect = cam.aspect || 1.6;
  }} catch (e) {{}}
  // camera.fov is the VERTICAL angle, but the screen corners reach a good deal
  // further — using the vertical angle alone left the edges of the view empty.
  // Corner half-angle: atan(tan(fov/2) * sqrt(1 + aspect^2)).
  const halfV = (fov / 2) * Math.PI / 180;
  const corner = Math.atan(Math.tan(halfV) * Math.sqrt(1 + aspect * aspect));
  // plus a margin, since a footprint whose centre is off-screen can still have
  // most of its area on-screen
  const a = Math.min(corner * 1.2, Math.PI / 2 - 1e-6);
  const s = d * Math.sin(a);
  if (s >= 1) return horizon;                       // cone overshoots the globe
  const theta = Math.PI - a - (Math.PI - Math.asin(s));
  return Math.max(0, Math.min(theta, horizon));
}}

function featsInView(pov) {{
  const cosH = Math.cos(visibleAngle(pov.altitude));
  const la1 = pov.lat * Math.PI / 180, lo1 = pov.lng * Math.PI / 180;
  const s1 = Math.sin(la1), c1 = Math.cos(la1);
  const out = [];
  for (const f of globeFeats) {{
    const c = featCentroid(f);
    if (!c) continue;
    const la2 = c[0] * Math.PI / 180, lo2 = c[1] * Math.PI / 180;
    if (s1 * Math.sin(la2) + c1 * Math.cos(la2) * Math.cos(lo2 - lo1) >= cosH) out.push(f);
  }}
  return out;
}}

function refreshGlobeLayers() {{
  if (!globeInstance || !globeMode) return;
  const hud = document.getElementById('globe-count');
  const total = GEOJSON.features.length.toLocaleString();

  if (!globeFeats.length) {{
    if (globeLayerMode !== 'empty') {{
      globeInstance.polygonsData([]).heatmapsData([]).htmlElementsData([]);
      globeLayerMode = 'empty';
    }}
    if (hud) hud.textContent = '0 of ' + total + ' scenes';
    return;
  }}

  const pov = globeInstance.pointOfView();
  const inView = featsInView(pov);
  if (inView.length <= GLOBE_POLY_CAP) {{
    globeInstance.heatmapsData([]).htmlElementsData([]).polygonsData(inView);
    globeLayerMode = 'polygons';
    if (hud) hud.textContent = inView.length.toLocaleString() + ' of ' + total +
      ' scenes' + (inView.length < globeFeats.length
        ? ' in view · ' + globeFeats.length.toLocaleString() + ' selected' : '');
  }} else {{
    heatDim = heatDimFor(pov.altitude);
    // Re-setting the data is what makes globe.gl re-run the colour function
    const dimChanged = Math.abs(heatDim - heatDimApplied) > 0.04;
    if (globeLayerMode !== 'heatmap' || dimChanged) {{
      globeInstance.polygonsData([]).heatmapsData([]).heatmapsData([{{points: globeHeatPts}}]);
      heatDimApplied = heatDim;
      globeLayerMode = 'heatmap';
    }}
    // Count bubbles on top of the field: the heatmap shows where, these say
    // how many. Re-clustered on every camera move so spacing stays even.
    const clusters = clusterCells(visibleAngle(pov.altitude) * 0.24, pov);
    const peak = clusters.reduce((m, c) => Math.max(m, c.n), 1);
    clusters.forEach(c => {{ c.peak = peak; c.alt = pov.altitude; }});
    globeInstance.htmlElementsData(clusters);
    if (hud) hud.textContent = globeFeats.length.toLocaleString() + ' of ' + total +
      ' scenes · ' + clusters.length + ' groups — zoom in for footprints';
  }}
}}

// three-globe treats a ring's winding as choosing which side of the sphere is
// "inside", and wants exterior rings clockwise — feed it a counter-clockwise
// one and it fills the COMPLEMENT, so a 2° footprint swallows the globe.
//
// The archive is not consistently wound: ~342 of 108k rings are already
// clockwise. Blanket-reversing therefore inverted exactly those, which drew as
// giant overlapping shapes across the oceans. So normalise instead of reverse:
// exterior ring clockwise, holes counter-clockwise. Cached per feature; the
// flat map is unaffected and keeps the original geometry.
function ringIsCW(r) {{
  let s = 0;
  for (let i = 0; i < r.length - 1; i++) s += (r[i+1][0] - r[i][0]) * (r[i+1][1] + r[i][1]);
  return s > 0;
}}
function normRings(rings) {{
  return rings.map((r, i) => {{
    const wantCW = (i === 0);          // exterior CW, holes CCW
    return ringIsCW(r) === wantCW ? r : r.slice().reverse();
  }});
}}
function globeGeom(f) {{
  if (f._gg) return f._gg;
  const g = f.geometry || {{}};
  const out = g.type === 'Polygon'
    ? {{type: 'Polygon', coordinates: normRings(g.coordinates)}}
    : g.type === 'MultiPolygon'
      ? {{type: 'MultiPolygon', coordinates: g.coordinates.map(normRings)}}
      : g;
  return (f._gg = out);
}}

// A footprint crossing the antimeridian has coordinates jumping +179 -> -179,
// which draws as a band right around the globe. They can't be rendered without
// splitting them, so leave them off the globe (~0.3% of scenes).
function globeSafe(f) {{
  if (f._gsafe !== undefined) return f._gsafe;
  const bb = featBBox(f);
  return (f._gsafe = !!bb && (bb[2] - bb[0]) <= 180);
}}

// Called whenever the selection changes; camera moves go straight to
// refreshGlobeLayers() since the underlying set hasn't changed.
function updateGlobe() {{
  if (!globeInstance || !globeMode) return;
  const overlay = document.getElementById('globe-too-many');
  if (overlay) overlay.style.display = 'none';   // no longer a capped view
  globeFeats = visibleFeats.filter(globeSafe);
  globeHeatPts = globeFeats.map(f => {{ const c = featCentroid(f); return [c[1], c[0]]; }});
  buildGlobeCells();
  globeLayerMode = '';                            // force the layer to rebuild
  refreshGlobeLayers();
}}

function _startGlobe() {{
  const el = document.getElementById('globe-container');
  if (!globeInstance) {{
    globeInstance = Globe()
      .backgroundImageUrl('https://unpkg.com/three-globe/example/img/night-sky.png')
      .atmosphereColor('rgba(100,150,255,0.25)')
      .atmosphereAltitude(0.1)
      .polygonGeoJsonGeometry(globeGeom)
      // Unscanned film gets the same dashed-orange treatment as the flat map
      .polygonCapColor(f => f.properties.scanned === false
        ? 'rgba(255,167,38,0.18)'
        : (DS_COLORS[f.properties.dataset] || '#fff') + '55')
      .polygonSideColor(() => 'rgba(0,0,0,0)')
      .polygonStrokeColor(f => f.properties.scanned === false
        ? '#ffa726' : (DS_COLORS[f.properties.dataset] || '#fff'))
      // Flush to the surface. Any real altitude makes footprints parallax
      // against the ground as you orbit, so they look mis-placed; this is just
      // enough to stay off the sphere without being visibly raised.
      .polygonAltitude(0.0004)
      // heatmapPoints defaults to identity, so the points array has to be
      // pulled out explicitly — without this globe.gl throws internally and
      // renders nothing while still reporting a heatmap layer.
      .heatmapPoints(d => d.points)
      .heatmapPointLat(d => d[1])
      .heatmapPointLng(d => d[0])
      .heatmapPointWeight(1)
      // Bandwidth is in degrees and doubles as the smoothing radius — the
      // layer has no resolution setting, so a wider kernel is what removes the
      // blocky look. Below ~2 the density never rises enough to be visible at all.
      .heatmapBandwidth(3.4)
      // Accessor, not the ramp itself: globe.gl calls it with the heatmap datum
      // and expects a colour *function* back (same shape as heatmapPoints).
      .heatmapColorFn(() => heatColor)
      .heatmapColorSaturation(1.2)   // pushes more of the field up into the ramp
      // Painted flat on the globe — equal base and top means no 3D relief
      .heatmapBaseAltitude(0.0008)
      .heatmapTopAltitude(0.0008)
      .heatmapsTransitionDuration(0)
      .htmlLat(d => d.lat)
      .htmlLng(d => d.lng)
      .htmlAltitude(0.02)
      .htmlTransitionDuration(0)
      .htmlElementVisibilityModifier((el, isVisible) => {{
        el.style.opacity = isVisible ? 1 : 0;              // hide the far side
        el.style.pointerEvents = isVisible ? 'auto' : 'none';
      }})
      .htmlElement(d => {{
        const el = document.createElement('div');
        el.className = 'globe-bub';
        el.textContent = d.n >= 1000 ? (d.n / 1000).toFixed(1) + 'k' : d.n;
        const size = Math.round(24 + Math.pow(d.n / d.peak, 0.42) * 34);
        el.style.width = el.style.height = size + 'px';
        el.style.fontSize = Math.max(9, Math.min(13, size * 0.32)) + 'px';
        el.title = d.n.toLocaleString() + ' scenes — click to zoom in';
        el.addEventListener('click', () => {{
          globeInstance.pointOfView(
            {{lat: d.lat, lng: d.lng, altitude: Math.max(0.05, d.alt * 0.42)}}, 800);
        }});
        return el;
      }})
      .polygonLabel(f => {{
        const p = f.properties;
        const date = p.acquisitionDate ? p.acquisitionDate.slice(0,10) : '—';
        return `<div style="background:#111c;padding:6px 10px;border-radius:6px;font-size:12px;color:#eee;line-height:1.5">
          <b>${{p.satellite}}</b> &middot; ${{p.entityId}}<br>📅 ${{date}}
        </div>`;
      }})
      (el);
    applyGlobeBasemap();
    // Swap between heatmap and footprints as the camera moves. Debounced so a
    // drag doesn't rebuild the layer on every frame.
    let zt;
    globeInstance.onZoom(() => {{
      clearTimeout(zt);
      zt = setTimeout(() => {{ refreshGlobeLayers(); syncUrl(); }}, 180);
    }});
    if (pendingGlobePov) {{
      globeInstance.pointOfView(pendingGlobePov, 0);
      pendingGlobePov = null;
    }}
    globeInstance.width(el.clientWidth).height(el.clientHeight);
    new ResizeObserver(() => {{
      globeInstance.width(el.clientWidth).height(el.clientHeight);
    }}).observe(el);
  }}
  updateGlobe();
}}

document.getElementById('globe-btn').addEventListener('click', () => {{
  globeMode = !globeMode;
  const mapEl = document.getElementById('map');
  const btn   = document.getElementById('globe-btn');
  mapEl.style.display = globeMode ? 'none' : '';
  document.getElementById('globe-wrap').classList.toggle('on', globeMode);
  btn.classList.toggle('on', globeMode);
  btn.title = globeMode ? 'Switch to flat map' : 'Switch to globe view';

  syncUrl();
  if (!globeMode) {{
    // Leaflet mis-measures while hidden — re-measure now the map is back
    setTimeout(() => map.invalidateSize(), 0);
    return;
  }}

  if (typeof Globe === 'undefined') {{
    btn.textContent = '🌐 Loading…';
    const s = document.createElement('script');
    s.src = 'https://unpkg.com/globe.gl';
    s.onload = () => {{ btn.textContent = '🌐 Globe'; _startGlobe(); }};
    s.onerror = () => {{
      btn.textContent = '🌐 Globe';
      const o = document.getElementById('globe-too-many');
      if (o) {{
        o.style.display = 'flex';
        o.innerHTML = '<strong>Globe unavailable</strong>' +
          '<p>Could not load the globe library. Check your connection and try again.</p>';
      }}
    }};
    document.head.appendChild(s);
  }} else {{
    _startGlobe();
  }}
}});

// Last: every listener it may need is now registered
restoreView();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

SFS_FOOTPRINTS_URL = "https://raw.githubusercontent.com/harryspacefromspace/sfs-map-data/main/declassified_footprints.geojson"

def fetch_published_ids():
    """Fetch entity IDs already published on SpaceFromSpace."""
    try:
        resp = requests.get(SFS_FOOTPRINTS_URL, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        ids = set()
        for feat in data.get("features", []):
            p = feat.get("properties", {})
            eid = p.get("entity_id") or p.get("entityId") or p.get("imageId") or ""
            if eid:
                # Normalise: uppercase, strip hyphens for fuzzy matching
                ids.add(eid.upper().replace("-", ""))
        print(f"  Loaded {len(ids):,} published scene IDs from SpaceFromSpace")
        return ids
    except Exception as e:
        print(f"  WARNING: could not fetch SFS published IDs — {e}")
        return set()


def stamp_availability(features, db_path="scenes.db"):
    """Join scenes.db to stamp each feature with the date it became downloadable.

    - firstSeenAvailable: when the monitor first detected the scene as available
    - publishDate: USGS's own publishDate (filled only if not already set)
    Matches on entityId. Scenes not in the DB (e.g. unscanned film) are left blank.
    """
    if not os.path.exists(db_path):
        print(f"  WARNING: {db_path} not found — skipping availability dates")
        return
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute(
            "SELECT entity_id, publish_date, first_seen_available FROM scenes"
        ).fetchall()
        conn.close()
    except Exception as e:
        print(f"  WARNING: could not read {db_path} — {e}")
        return

    by_id = {r[0]: (r[1], r[2]) for r in rows}
    n = 0
    for f in features:
        rec = by_id.get(f["properties"].get("entityId", ""))
        if not rec:
            continue
        pub, fsa = rec
        if fsa:
            f["properties"]["firstSeenAvailable"] = str(fsa)[:10]
        if pub and not f["properties"].get("publishDate"):
            f["properties"]["publishDate"] = str(pub)[:10]
        n += 1
    print(f"  Stamped availability dates on {n:,} of {len(features):,} features from {db_path}")


def load_previous_features(path="available_scenes.geojson"):
    """Load features from the last successful run, grouped by dataset."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            prev = json.load(f)
        by_dataset = {}
        for feat in prev.get("features", []):
            ds = feat.get("properties", {}).get("dataset")
            if ds:
                by_dataset.setdefault(ds, []).append(feat)
        print(f"  Loaded {sum(len(v) for v in by_dataset.values()):,} features from previous run as fallback")
        return by_dataset
    except Exception as e:
        print(f"  WARNING: could not load previous geojson — {e}")
        return {}


def main():
    username = os.environ.get("M2M_USERNAME")
    token    = os.environ.get("M2M_TOKEN")
    if not username or not token:
        raise RuntimeError("M2M_USERNAME and M2M_TOKEN must be set")

    # Load published scene IDs from SpaceFromSpace
    print("Fetching published scene IDs from SpaceFromSpace...")
    published_ids = fetch_published_ids()

    # Load previous run's features before we start, so we can fall back per-dataset
    print("Loading previous run as fallback...")
    prev_by_dataset = load_previous_features()

    print("Logging in to USGS M2M API...")
    api_key = login(username, token)

    # Hard cap on all M2M fetching so a slow/flaky USGS can't hang the build
    # until CI kills it. Once passed, remaining searches raise and fall back to
    # the previous run's data — the map still gets built. Overridable via env
    # for manual runs; default leaves headroom under the 60-min CI step.
    fetch_budget = int(os.environ.get("FETCH_BUDGET_SECONDS", "2400"))
    deadline = time.time() + fetch_budget
    print(f"  Fetch budget: {fetch_budget // 60} min")

    all_features = []
    failed = []
    try:
        for dataset, filter_id in DATASETS.items():
            print(f"\n  {DATASET_LABELS[dataset]}...")
            try:
                scenes = search_available(api_key, dataset, filter_id, deadline)
                fresh = []
                for scene in scenes:
                    f = scene_to_feature(scene, dataset)
                    if f:
                        fresh.append(f)
                all_features.extend(fresh)
                print(f"  {len(fresh):,} features with spatial bounds")
            except Exception as e:
                print(f"  WARNING: {dataset} failed — {e}")
                fallback = prev_by_dataset.get(dataset, [])
                if fallback:
                    print(f"  Using {len(fallback):,} features from previous run for {dataset}")
                    all_features.extend(fallback)
                else:
                    print(f"  No previous data for {dataset} — skipping")
                failed.append(dataset)

        # Datasets pulled in full (scanned + unscanned). Pulling all three takes
        # the file from ~58 MB to a projected ~280 MB, well past GitHub's 100 MB
        # per-file limit, so this stays on declassii until the scene data is
        # served from object storage. Then: FULL_PULL_DATASETS=corona2,declassii,declassiii
        full_pull = [d for d in os.environ.get("FULL_PULL_DATASETS", "declassii").split(",")
                     if d.strip() in DATASETS]
        print(f"\n  Full pull enabled for: {', '.join(full_pull) or '(none)'}")
        for dataset in full_pull:
            print(f"\n  {DATASET_LABELS[dataset]} — unscanned frames...")
            try:
                scanned_ids = {f["properties"]["entityId"]
                               for f in all_features
                               if f["properties"]["dataset"] == dataset}
                every = search_all(api_key, dataset, deadline)
                unscanned = []
                for scene in every:
                    f = scene_to_feature_unscanned(scene, dataset, scanned_ids)
                    if f:
                        unscanned.append(f)
                all_features.extend(unscanned)
                print(f"  {len(unscanned):,} unscanned features added "
                      f"(from {len(every):,} total in {dataset})")
            except Exception as e:
                print(f"  WARNING: unscanned {dataset} fetch failed — {e}")

        # KH-6 (LANYARD) unscanned frames, pulled with a server-side mission
        # filter. Redundant once corona2 is in full_pull, but until then it's the
        # only way those 888 frames get on the map without hauling all of corona2.
        if "corona2" not in full_pull:
            print(f"\n  Declass I — unscanned KH-6 (LANYARD) frames...")
            try:
                mission_fid = get_metadata_filter_id(api_key, "corona2", ["mission"], deadline)
                if not mission_fid:
                    print("  Could not find a Mission filter for corona2 — skipping KH-6")
                else:
                    kh6_scanned = {f["properties"]["entityId"] for f in all_features
                                   if f["properties"]["dataset"] == "corona2"
                                   and f["properties"].get("satellite") == "KH-6 (LANYARD)"}
                    lanyard = search_by_missions(api_key, "corona2", mission_fid,
                                                 LANYARD_MISSIONS, deadline)
                    added = 0
                    for scene in lanyard:
                        f = scene_to_feature_unscanned(scene, "corona2", kh6_scanned)
                        if f and f["properties"].get("satellite") == "KH-6 (LANYARD)":
                            all_features.append(f)
                            added += 1
                    print(f"  {added:,} unscanned KH-6 features added "
                          f"(from {len(lanyard):,} LANYARD scenes returned)")
            except Exception as e:
                print(f"  WARNING: unscanned KH-6 fetch failed — {e}")

    finally:
        logout(api_key)

    if not all_features:
        raise RuntimeError("All datasets failed and no previous data available — nothing to build")

    if failed:
        print(f"\nWARNING: {len(failed)} dataset(s) used fallback data: {', '.join(failed)}")

    # Stamp published property — normalise entity ID same way as fetch_published_ids
    published_count = 0
    for f in all_features:
        eid = f["properties"].get("entityId", "").upper().replace("-", "")
        is_published = eid in published_ids
        f["properties"]["published"] = is_published
        if is_published:
            published_count += 1
    print(f"  {published_count:,} scenes marked as published on SpaceFromSpace")

    # Stamp availability dates (publishDate + first-seen-available) from scenes.db
    print("Stamping availability dates from scenes.db...")
    stamp_availability(all_features)

    # Drop client-derivable properties (also cleans them off fallback features
    # loaded from an older available_scenes.geojson)
    slim_features(all_features)

    counts    = {}
    years     = []
    sat_seen  = []
    for f in all_features:
        p  = f["properties"]
        ds = p["dataset"]
        counts[ds] = counts.get(ds, 0) + 1
        y = feat_year(p)
        if y:
            years.append(y)
        st = p.get("satellite", "Unknown")
        if st not in sat_seen:
            sat_seen.append(st)

    sat_seen.sort(key=lambda x: SAT_ORDER.index(x) if x in SAT_ORDER else 99)

    geojson = {
        "type":     "FeatureCollection",
        "features": all_features,
        "metadata": {
            "generated": datetime.utcnow().isoformat() + "Z",
            "total":     len(all_features),
            "counts":    counts,
            "year_min":  min(years) if years else 1960,
            "year_max":  max(years) if years else 1984,
            "sat_types": sat_seen,
        },
    }

    print(f"\nTotal features: {len(all_features):,}")
    print(f"Year range: {geojson['metadata']['year_min']}–{geojson['metadata']['year_max']}")
    print(f"Satellite types: {sat_seen}")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(geojson))
    print("Saved index.html")

    # available_scenes.geojson is both the fallback cache AND the file the map
    # fetches at runtime (DATA_URL), so keep it in sync with the built HTML.
    # all_features already merges per-dataset fallbacks, so it stays complete
    # even on a partial run. Minified to keep the served file small.
    with open("available_scenes.geojson", "w") as f:
        json.dump(geojson, f, separators=(",", ":"))
    size = os.path.getsize("available_scenes.geojson")
    if failed:
        print(f"Saved available_scenes.geojson (partial run — {', '.join(failed)} used fallback data)")
    else:
        print("Saved available_scenes.geojson (full run)")
    # This now goes to R2, not git, so GitHub's 100 MB file limit no longer
    # applies. What matters instead is that every visitor downloads this file:
    # it compresses to roughly a tenth, so ~10 MB raw is ~1 MB on the wire.
    print(f"  {size/1e6:.1f} MB  (~{size/1e7:.1f} MB compressed on the wire)")
    if size > 150_000_000:
        print("  !! Very large for a single download — visitors fetch all of this")
        print("     before the map draws. Time to move to tiles (PMTiles).")
    elif size > 80_000_000:
        print("  !  Getting heavy for a single download; tiles would serve only"
              " what's in view.")

    print(f"\nDone — {len(all_features):,} scenes mapped.")


def build_only(geojson_path="available_scenes.geojson"):
    """Build index.html from existing geojson without hitting the API."""
    if not os.path.exists(geojson_path):
        raise RuntimeError(f"{geojson_path} not found — run without --build-only first")
    print(f"Loading {geojson_path}...")
    with open(geojson_path) as f:
        geojson = json.load(f)
    n = len(geojson.get("features", []))
    print(f"  {n:,} features loaded")
    # Refresh availability dates from scenes.db on every rebuild
    stamp_availability(geojson["features"])
    slim_features(geojson["features"])
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(geojson))
    print("Saved index.html")
    # Rewrite the served data file (minified) so the map fetches the freshly
    # stamped data, and so it shrinks from any previously pretty-printed version.
    with open(geojson_path, "w") as f:
        json.dump(geojson, f, separators=(",", ":"))
    print(f"Saved {geojson_path} (served data, minified)")
    print(f"\nDone — {n:,} scenes mapped (build only, no API calls).")


def patch_html_cameras(html_path="index.html"):
    """Fix camera/mission fields for KH-4A/KH-4B in existing index.html (no API needed)."""
    if not os.path.exists(html_path):
        raise RuntimeError(f"{html_path} not found")

    print(f"Reading {html_path}...")
    with open(html_path, encoding="utf-8") as f:
        content = f.read()

    marker = "const GEOJSON   = "
    start = content.index(marker) + len(marker)
    end   = content.index(";\nconst DS_COLORS", start)
    geojson = json.loads(content[start:end])

    n_cam = n_mis = 0
    for feat in geojson["features"]:
        p   = feat["properties"]
        eid = p.get("entityId", "")
        ds  = p.get("dataset", "")
        if p.get("camera") is None:
            cam = get_camera_from_entity(eid, ds)
            if cam:
                p["camera"] = cam
                n_cam += 1
        if p.get("mission") is None:
            mis = get_mission_from_entity(eid, ds)
            if mis:
                p["mission"] = mis
                n_mis += 1

    print(f"Fixed camera for {n_cam:,} features")
    print(f"Fixed mission for {n_mis:,} features")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(geojson))
    print("Saved index.html")
    print(f"\nDone — camera/mission fields patched without API calls.")


if __name__ == "__main__":
    import sys
    if "--build-only" in sys.argv:
        build_only()
    elif "--patch" in sys.argv:
        patch_html_cameras()
    else:
        main()
