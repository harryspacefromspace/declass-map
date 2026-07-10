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


def search_available(api_key, dataset, filter_id):
    all_scenes = []
    starting   = 1
    batch      = 10000

    while True:
        resp = requests.post(
            M2M_URL + "scene-search",
            json={
                "datasetName":    dataset,
                "maxResults":     batch,
                "startingNumber": starting,
                "metadataType": "full",
                "sceneFilter": {
                    "metadataFilter": {
                        "filterType": "value",
                        "filterId":   filter_id,
                        "value":      "Y",
                    }
                },
            },
            headers={"X-Auth-Token": api_key},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorCode"):
            print(f"    API error: {data['errorMessage']}")
            break

        scenes = data.get("data", {}).get("results", [])
        if not scenes:
            break

        all_scenes.extend(scenes)
        print(f"    {len(all_scenes):,} scenes retrieved...")

        if len(scenes) < batch:
            break
        starting += batch
        time.sleep(0.5)

    return all_scenes


def search_all(api_key, dataset):
    """Fetch ALL scenes for a dataset regardless of scan/availability status."""
    all_scenes = []
    starting   = 1
    batch      = 10000

    while True:
        resp = requests.post(
            M2M_URL + "scene-search",
            json={
                "datasetName":    dataset,
                "maxResults":     batch,
                "startingNumber": starting,
                "metadataType":   "full",
            },
            headers={"X-Auth-Token": api_key},
            timeout=180,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errorCode"):
            print(f"    API error: {data['errorMessage']}")
            break

        scenes = data.get("data", {}).get("results", [])
        if not scenes:
            break

        all_scenes.extend(scenes)
        print(f"    {len(all_scenes):,} scenes retrieved...")

        if len(scenes) < batch:
            break
        starting += batch
        time.sleep(0.5)

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
        m = re.match(r'DZB\d{4}-\d{6}([A-Z])\d{6}', entity_id)
        if not m:
            # Non-hyphenated GAMBIT format: DZB00403800118H006001
            m = re.match(r'DZB\d{11}([A-Z])\d{6}', entity_id)
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
        m = re.match(r'DZB(\d+)-', entity_id)
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
    year = int(acq[:4]) if acq and len(acq) >= 4 and acq[:4].isdigit() else None

    # Prefer full-resolution browsePath over thumbnailPath
    browse_url = ""
    browse = scene.get("browse")
    if browse and isinstance(browse, list):
        browse_url = browse[0].get("browsePath") or browse[0].get("thumbnailPath", "")

    mission  = get_mission_from_scene(scene) or get_mission_from_entity(entity_id, dataset)
    sat_type = get_satellite_type(mission, dataset)
    camera   = get_camera_from_entity(entity_id, dataset)
    mission_num = get_mission_from_entity(entity_id, dataset)

    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "entityId":        entity_id,
            "dataset":         dataset,
            "datasetLabel":    DATASET_LABELS.get(dataset, dataset),
            "displayId":       scene.get("displayId", ""),
            "acquisitionDate": acq,
            "year":            year,
            "satellite":       sat_type,
            "mission":         mission_num,
            "camera":          camera,
            "browse":          browse_url,
            "scanned":         True,
            "color":           DATASET_COLORS.get(dataset, "#ffffff"),
            "earthExplorerUrl": (
                f"https://earthexplorer.usgs.gov/scene/metadata/full/"
                f"{DATASET_IDS.get(dataset, dataset)}/{entity_id}/"
            ),
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
# HTML builder
# ---------------------------------------------------------------------------

def build_html(geojson):
    import re as _re, collections as _col
    geojson_str    = json.dumps(geojson)
    generated      = geojson["metadata"]["generated"]
    total          = geojson["metadata"]["total"]
    counts         = geojson["metadata"]["counts"]
    year_min       = geojson["metadata"]["year_min"]
    year_max       = geojson["metadata"]["year_max"]
    sat_types      = geojson["metadata"]["sat_types"]
    ds_colors_json = json.dumps(DATASET_COLORS)

    # Build mission lists, camera sets, and year counts from features
    missions_by_ds = _col.defaultdict(dict)   # {dataset: {mission: count}}
    cameras_by_ds  = _col.defaultdict(set)    # {dataset: {camera_label}}
    year_counts     = _col.defaultdict(int)   # {year: count}
    date_min = date_max = ""
    for feat in geojson["features"]:
        p = feat["properties"]
        ds  = p.get("dataset", "")
        m   = p.get("mission")
        cam = p.get("camera")
        acq = p.get("acquisitionDate", "")[:10]
        yr  = p.get("year")
        if m:
            missions_by_ds[ds][m] = missions_by_ds[ds].get(m, 0) + 1
        if cam:
            cameras_by_ds[ds].add(cam)
        if yr:
            year_counts[yr] += 1
        if acq:
            if not date_min or acq < date_min: date_min = acq
            if not date_max or acq > date_max: date_max = acq

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

/* Map */
#map{{flex:1;position:relative}}
#globe-container{{flex:1;position:relative;background:#000014;overflow:hidden;display:none}}
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
</style>
</head>
<body>

<div id="header">
  <h1>🛰 Declassified Satellite <span>Available Downloads</span></h1>
  <div id="stats">{counts_html} &nbsp;·&nbsp; Updated <strong>{generated[:10]}</strong></div>
  <div id="search-wrap">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/></svg>
    <input id="search" type="text" placeholder="Search entity ID…" autocomplete="off" />
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
  <div style="margin-left:auto;display:flex;align-items:center;gap:5px">
    <button id="globe-btn" class="bm-btn" title="Switch to globe view">🌐 Globe</button>
    <span style="width:1px;height:16px;background:#3a3a3a;margin:0 2px;display:inline-block"></span>
    <button class="bm-btn on" data-bm="dark">Dark</button>
    <button class="bm-btn" data-bm="satellite">Satellite</button>
    <button class="bm-btn" data-bm="hybrid">Hybrid</button>
    <button class="bm-btn" data-bm="osm">OSM</button>
  </div>
</div>

<div id="filter-summary"></div>

<div id="globe-container">
  <div id="globe-too-many">
    <strong id="globe-too-many-count"></strong>
    <p>Too many scenes to render on the globe.<br>Use the filters to reduce to under 3,000 scenes.</p>
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

<script>
const GEOJSON   = {geojson_str};
const DS_COLORS = {ds_colors_json};
const YEAR_MIN  = {year_min};
const YEAR_MAX  = {year_max};
const YEAR_COUNTS = {year_counts_json};

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
function setBasemap(key) {{
  activeBmLayers.forEach(l => map.removeLayer(l));
  activeBmLayers = [];
  const bm = BASEMAPS[key];
  const arr = Array.isArray(bm) ? bm : [bm];
  // Add in order: first layer goes furthest back
  arr.forEach(l => l.addTo(map));
  arr[0].bringToBack();          // imagery always at the very back
  activeBmLayers = [...arr];
  document.querySelectorAll('.bm-btn').forEach(b => b.classList.toggle('on', b.dataset.bm===key));
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
let dateFilter = null;
let globeMode = false;
let globeInstance = null;

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

  const feats = GEOJSON.features.filter(f => {{
    const p = f.properties;
    // Unscanned: only show if toggle is on, and satellite filter matches
    if (p.scanned === false) {{
      if (!showUnscanned) return false;
      if (!satActive[p.satellite]) return false;
      return true;  // unscanned skip other filters
    }}

    // Hide published scenes if toggle is on
    if (hidePublished && p.published) return false;
    if (!satActive[p.satellite]) return false;

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
}}

function updateCounter(n) {{
  const el = document.getElementById('counter');
  const total = GEOJSON.features.length;
  el.textContent = n.toLocaleString() + ' of ' + total.toLocaleString() + ' scenes';
  el.classList.toggle('has-scenes', n > 0);
  document.getElementById('empty-state').classList.toggle('hidden', n > 0);
}}

buildLayers();

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
  const dsShort = p.datasetLabel.split('—')[0].trim();
  const isUnscanned = p.scanned === false;

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
    ? `<a href="${{p.earthExplorerUrl}}" target="_blank">EarthExplorer ↗</a>
       <span class="pu-unscanned-label">📷 Film not yet scanned</span>`
    : `<a href="${{p.earthExplorerUrl}}" target="_blank">EarthExplorer ↗</a>
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
    <div class="meta">📅 ${{date}}</div>
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
document.querySelectorAll('.bm-btn').forEach(btn =>
  btn.addEventListener('click', () => setBasemap(btn.dataset.bm)));

// ── Search with zoom ─────────────────────────────────────────────────────────
let st;
document.getElementById('search').addEventListener('input', e => {{
  clearTimeout(st);
  st = setTimeout(() => {{
    searchQ = e.target.value.trim();
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

// ── URL param date filter ──────────────────────────────────────────────────────
(function() {{
  const params = new URLSearchParams(window.location.search);
  const from = params.get('from');
  const to   = params.get('to') || params.get('from');
  const mmdd = params.get('mmdd'); // MM-DD only filter e.g. ?mmdd=07-13

  if (mmdd && /^\d{{2}}-\d{{2}}$/.test(mmdd)) {{
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
const MAX_GLOBE_FEATS = 3000;

function updateGlobe() {{
  if (!globeInstance || !globeMode) return;
  const tooMany = visibleFeats.length > MAX_GLOBE_FEATS;
  const overlay = document.getElementById('globe-too-many');
  overlay.style.display = tooMany ? 'flex' : 'none';
  if (tooMany) {{
    document.getElementById('globe-too-many-count').textContent =
      visibleFeats.length.toLocaleString() + ' scenes visible';
    globeInstance.polygonsData([]);
  }} else {{
    globeInstance.polygonsData(visibleFeats);
  }}
}}

function _startGlobe() {{
  const el = document.getElementById('globe-container');
  if (!globeInstance) {{
    globeInstance = Globe()
      .globeImageUrl('//unpkg.com/three-globe/example/img/earth-dark.jpg')
      .backgroundImageUrl('//unpkg.com/three-globe/example/img/night-sky.png')
      .atmosphereColor('rgba(100,150,255,0.25)')
      .atmosphereAltitude(0.1)
      .polygonGeoJsonGeometry(f => f.geometry)
      .polygonCapColor(f => (DS_COLORS[f.properties.dataset] || '#fff') + '30')
      .polygonSideColor(() => 'rgba(0,0,0,0)')
      .polygonStrokeColor(f => DS_COLORS[f.properties.dataset] || '#fff')
      .polygonAltitude(0.001)
      .polygonLabel(f => {{
        const p = f.properties;
        const date = p.acquisitionDate ? p.acquisitionDate.slice(0,10) : '—';
        return `<div style="background:#111c;padding:6px 10px;border-radius:6px;font-size:12px;color:#eee;line-height:1.5">
          <b>${{p.satellite}}</b> &middot; ${{p.entityId}}<br>📅 ${{date}}
        </div>`;
      }})
      (el);
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
  const globeEl = document.getElementById('globe-container');
  mapEl.style.display = globeMode ? 'none' : '';
  globeEl.style.display = globeMode ? 'block' : 'none';
  document.getElementById('globe-btn').classList.toggle('on', globeMode);
  document.getElementById('globe-btn').title = globeMode ? 'Switch to flat map' : 'Switch to globe view';

  if (!globeMode) return;

  if (typeof Globe === 'undefined') {{
    const s = document.createElement('script');
    s.src = '//unpkg.com/globe.gl';
    s.onload = _startGlobe;
    document.head.appendChild(s);
  }} else {{
    _startGlobe();
  }}
}});
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

    all_features = []
    failed = []
    try:
        for dataset, filter_id in DATASETS.items():
            print(f"\n  {DATASET_LABELS[dataset]}...")
            try:
                scenes = search_available(api_key, dataset, filter_id)
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

        # Fetch unscanned KH-7 (declassii) scenes — all scenes minus already-scanned
        print(f"\n  Declass II — unscanned KH-7 scenes...")
        try:
            scanned_ids = {f["properties"]["entityId"]
                           for f in all_features
                           if f["properties"]["dataset"] == "declassii"}
            all_declassii = search_all(api_key, "declassii")
            unscanned = []
            for scene in all_declassii:
                f = scene_to_feature_unscanned(scene, "declassii", scanned_ids)
                if f:
                    unscanned.append(f)
            all_features.extend(unscanned)
            print(f"  {len(unscanned):,} unscanned KH-7 features added")
        except Exception as e:
            print(f"  WARNING: unscanned KH-7 fetch failed — {e}")

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

    counts    = {}
    years     = []
    sat_seen  = []
    for f in all_features:
        p  = f["properties"]
        ds = p["dataset"]
        counts[ds] = counts.get(ds, 0) + 1
        if p.get("year"):
            years.append(p["year"])
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

    # Only overwrite the geojson cache when we have a clean full run
    # so it always contains complete data for future fallback
    if not failed:
        with open("available_scenes.geojson", "w") as f:
            json.dump(geojson, f)
        print("Saved available_scenes.geojson (full run)")
    else:
        print("Skipped overwriting available_scenes.geojson (partial run — keeping previous as fallback)")

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
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(geojson))
    print("Saved index.html")
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
