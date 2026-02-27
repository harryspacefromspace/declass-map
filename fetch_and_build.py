#!/usr/bin/env python3
"""
fetch_and_build.py — queries USGS M2M for all downloadable declassified scenes
and builds a self-contained index.html map with dataset, satellite type, and
date range filters. Filters start OFF (additive model — click to show).
"""

import os
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
        if 9001 <= n <= 9009:    return "KH-1"
        if 9010 <= n <= 9015:    return "KH-2"
        if 9016 <= n <= 9024:    return "KH-3"
        if 9025 <= n <= 9062:    return "KH-4"
        if 1001 <= n <= 1052:    return "KH-4A"
        if 1101 <= n <= 1117:    return "KH-4B"

    elif dataset == "declassii":
        if 1200 <= n <= 1299:    return "KH-9 Mapping Camera"
        return "KH-7 (GAMBIT)"   # default for declassii

    elif dataset == "declassiii":
        return "KH-9 (HEXAGON)"

    return "Unknown"


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


# ---------------------------------------------------------------------------
# GeoJSON conversion
# ---------------------------------------------------------------------------

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

    mission  = get_mission_from_scene(scene)
    sat_type = get_satellite_type(mission, dataset)

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
            "browse":          browse_url,
            "color":           DATASET_COLORS.get(dataset, "#ffffff"),
            "earthExplorerUrl": (
                f"https://earthexplorer.usgs.gov/scene/metadata/full/"
                f"{DATASET_IDS.get(dataset, dataset)}/{entity_id}/"
            ),
        },
    }


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def build_html(geojson):
    geojson_str    = json.dumps(geojson)
    generated      = geojson["metadata"]["generated"]
    total          = geojson["metadata"]["total"]
    counts         = geojson["metadata"]["counts"]
    year_min       = geojson["metadata"]["year_min"]
    year_max       = geojson["metadata"]["year_max"]
    sat_types      = geojson["metadata"]["sat_types"]
    ds_colors_json = json.dumps(DATASET_COLORS)

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
body{{background:#0a0a0a;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}}

/* ── Header ── */
#header{{
  background:#0f0f0f;border-bottom:1px solid #1a1a1a;
  padding:9px 16px;display:flex;align-items:center;gap:14px;flex-wrap:wrap;z-index:1000;
}}
#header h1{{font-size:13px;font-weight:600;color:#e8e8e8;white-space:nowrap;letter-spacing:.01em}}
#header h1 span{{color:#3a3a3a;font-weight:400;margin-left:6px;font-size:11px}}
#stats{{font-size:11px;color:#444;display:flex;align-items:center;gap:5px;flex-wrap:wrap}}
.dot{{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:2px;opacity:.7}}
#search-wrap{{margin-left:auto;position:relative;display:flex;align-items:center}}
#search-wrap svg{{position:absolute;left:8px;opacity:.35;pointer-events:none}}
#search{{background:#161616;border:1px solid #242424;color:#ccc;padding:5px 9px 5px 28px;
  border-radius:6px;font-size:11px;width:160px;outline:none;transition:border-color .15s}}
#search:focus{{border-color:#444;background:#1a1a1a}}
#search::placeholder{{color:#383838}}

/* ── Filter bar ── */
#filters{{
  background:#0a0a0a;border-bottom:1px solid #161616;
  padding:7px 16px;display:flex;align-items:center;gap:0;flex-wrap:nowrap;overflow-x:auto;
}}
#filters::-webkit-scrollbar{{height:0}}
.filter-section{{display:flex;align-items:center;gap:7px;padding-right:18px;margin-right:18px;border-right:1px solid #1a1a1a;flex-shrink:0}}
.filter-section:last-child{{border-right:none;padding-right:0;margin-right:0;margin-left:auto}}
.filter-label{{font-size:9.5px;color:#383838;text-transform:uppercase;letter-spacing:.08em;white-space:nowrap}}

/* Satellite buttons — colour-coded by family */
.sat-btn{{
  background:transparent;border:1px solid #1e1e1e;color:#3a3a3a;
  padding:3px 9px;border-radius:4px;cursor:pointer;font-size:10.5px;
  transition:all .12s;white-space:nowrap;flex-shrink:0;
  --sat-c:#888;
}}
.sat-btn:hover{{border-color:#3a3a3a;color:#888}}
.sat-btn.on{{
  background:color-mix(in srgb,var(--sat-c) 12%,transparent);
  border-color:color-mix(in srgb,var(--sat-c) 50%,transparent);
  color:var(--sat-c);
}}
.sat-quick{{font-size:9.5px;color:#2e2e2e;cursor:pointer;padding:2px 5px;border-radius:3px;transition:color .12s;background:none;border:none;white-space:nowrap}}
.sat-quick:hover{{color:#666}}

/* Year slider */
.yr-val{{font-size:11px;color:#555;min-width:32px;text-align:center;font-variant-numeric:tabular-nums}}
.slider-wrap{{position:relative;width:140px;height:20px;flex-shrink:0}}
#slider-track{{position:absolute;top:50%;left:0;right:0;height:2px;background:#1e1e1e;transform:translateY(-50%);border-radius:2px}}
#slider-fill{{position:absolute;top:50%;height:2px;background:#2e2e2e;transform:translateY(-50%);border-radius:2px;transition:background .2s}}
#slider-fill.active{{background:#484848}}
input[type=range]{{position:absolute;top:0;left:0;width:100%;height:100%;opacity:0;cursor:pointer;pointer-events:auto;margin:0;z-index:2}}input[type=range]::-webkit-slider-thumb{{pointer-events:auto}}input[type=range].on-top{{z-index:3}}
.thumb{{position:absolute;top:50%;width:10px;height:10px;background:#383838;border-radius:50%;transform:translate(-50%,-50%);pointer-events:none;border:1px solid #555;transition:background .15s}}
.thumb.active{{background:#777}}

/* Basemap + reset */
.bm-btn{{background:transparent;border:1px solid #1e1e1e;color:#383838;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:10px;transition:all .12s;flex-shrink:0}}
.bm-btn:hover{{border-color:#444;color:#888}}
.bm-btn.on{{background:#1e1e1e;border-color:#484848;color:#bbb}}
#reset-btn{{background:transparent;border:1px solid #1e1e1e;color:#2e2e2e;padding:3px 9px;border-radius:4px;cursor:pointer;font-size:10px;transition:all .12s;flex-shrink:0}}
#reset-btn:hover{{border-color:#444;color:#777}}

/* Map */
#map{{flex:1;position:relative}}

/* Empty state */
#empty-state{{
  position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  text-align:center;pointer-events:none;z-index:500;
  opacity:1;transition:opacity .3s;
}}
#empty-state.hidden{{opacity:0}}
#empty-state p{{font-size:13px;color:#2e2e2e;margin-bottom:6px}}
#empty-state small{{font-size:11px;color:#252525}}

/* Counter */
#counter{{
  position:absolute;bottom:16px;left:50%;transform:translateX(-50%);
  background:rgba(10,10,10,.85);backdrop-filter:blur(8px);
  border:1px solid #1e1e1e;color:#3a3a3a;padding:5px 14px;
  border-radius:20px;font-size:11px;z-index:1000;pointer-events:none;
  transition:all .2s;white-space:nowrap;
}}
#counter.has-scenes{{color:#666;border-color:#282828}}

/* ── Overlays button ── */
#ov-toggle{{
  position:absolute;top:12px;right:12px;z-index:1000;
  background:rgba(10,10,10,.85);backdrop-filter:blur(8px);
  border:1px solid #242424;color:#555;padding:6px 12px 6px 10px;
  border-radius:8px;font-size:11px;cursor:pointer;
  display:flex;align-items:center;gap:7px;transition:all .15s;white-space:nowrap;
}}
#ov-toggle:hover{{border-color:#444;color:#aaa}}
#ov-toggle.has-active{{border-color:#6644aa;color:#aa88ff}}
#ov-toggle svg{{flex-shrink:0;transition:transform .2s}}
#ov-toggle.open svg{{transform:rotate(180deg)}}

/* ── Overlays panel ── */
#ov-panel{{
  position:absolute;top:50px;right:12px;z-index:999;
  background:rgba(10,10,10,.92);backdrop-filter:blur(12px);
  border:1px solid #222;border-radius:10px;padding:14px;
  width:210px;display:none;flex-direction:column;gap:10px;
}}
#ov-panel.open{{display:flex}}
.ov-section{{font-size:9.5px;color:#333;text-transform:uppercase;
  letter-spacing:.1em;margin-bottom:2px;}}
.ov-btn{{
  background:transparent;border:1px solid #1e1e1e;color:#444;
  padding:5px 10px;border-radius:6px;font-size:10.5px;
  cursor:pointer;text-align:left;transition:all .12s;
  display:flex;align-items:center;gap:7px;width:100%;
}}
.ov-btn:hover{{border-color:#333;color:#888}}
.ov-btn.on{{border-color:#6644aa44;color:#aa88ff;background:#6644aa0a}}
.ov-icon{{font-size:13px;flex-shrink:0}}
.ov-badge{{margin-left:auto;font-size:9px;color:#333;background:#161616;
  padding:1px 5px;border-radius:10px;}}
.ov-btn.on .ov-badge{{color:#6644aa;}}

/* ── USGS status widget ── */
#usgs-status{{
  position:absolute;bottom:16px;right:12px;z-index:1000;
  background:rgba(10,10,10,.85);backdrop-filter:blur(8px);
  border:1px solid #1e1e1e;color:#3a3a3a;
  padding:5px 10px 5px 8px;border-radius:20px;
  font-size:10.5px;display:flex;align-items:center;gap:6px;
  cursor:default;transition:border-color .3s,color .3s;white-space:nowrap;
}}
#usgs-status.up{{color:#555;border-color:#1e3322}}
#usgs-status.down{{color:#774444;border-color:#441a1a}}
#usgs-status.checking{{color:#444;border-color:#1e1e1e}}
#status-dot{{width:7px;height:7px;border-radius:50%;background:#333;flex-shrink:0;transition:background .4s,box-shadow .4s}}
#usgs-status.up #status-dot{{background:#22cc66;box-shadow:0 0 6px #22cc6699;animation:pulse-up 2.5s ease-in-out infinite}}
#usgs-status.down #status-dot{{background:#cc3333;box-shadow:0 0 6px #cc333399}}
#usgs-status.checking #status-dot{{background:#555;animation:pulse-check .8s ease-in-out infinite}}
@keyframes pulse-check{{0%,100%{{opacity:.3}}50%{{opacity:1}}}}
@keyframes pulse-up{{0%,100%{{box-shadow:0 0 4px #22cc6666}}50%{{box-shadow:0 0 10px #22cc66cc}}}}

/* ── Download button & modal ── */
.pu-dl-btn{{
  font-size:10.5px;color:#44bb77;background:transparent;
  padding:4px 10px;border:1px solid #44bb7722;border-radius:5px;
  cursor:pointer;transition:all .12s;white-space:nowrap;
}}
.pu-dl-btn:hover{{background:#44bb7712;border-color:#44bb7744}}
.pu-dl-btn:disabled{{opacity:.35;cursor:default}}
#dl-modal{{
  position:fixed;inset:0;z-index:9000;display:none;
  align-items:center;justify-content:center;
  background:rgba(0,0,0,.75);backdrop-filter:blur(4px);
}}
#dl-modal.open{{display:flex}}
#dl-box{{
  background:#111;border:1px solid #2a2a2a;border-radius:12px;
  padding:20px 24px;width:340px;max-width:90vw;
  box-shadow:0 24px 64px rgba(0,0,0,.95);
}}
#dl-box h4{{font-size:12px;color:#ccc;margin-bottom:4px;font-weight:600}}
#dl-box .dl-sub{{font-size:10.5px;color:#555;margin-bottom:16px}}
.dl-field{{margin-bottom:10px}}
.dl-field label{{font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.08em;display:block;margin-bottom:4px}}
.dl-field input{{width:100%;background:#161616;border:1px solid #242424;color:#ccc;
  padding:6px 10px;border-radius:6px;font-size:11px;outline:none;box-sizing:border-box;transition:border-color .15s}}
.dl-field input:focus{{border-color:#444}}
#dl-status{{font-size:10.5px;color:#666;min-height:16px;margin:10px 0;line-height:1.5}}
#dl-status.err{{color:#cc4444}}
#dl-status.ok{{color:#44bb77}}
.dl-actions{{display:flex;gap:8px;margin-top:14px}}
.dl-actions button{{flex:1;padding:7px 0;border-radius:6px;font-size:11px;cursor:pointer;border:1px solid;transition:all .12s}}
#dl-go{{background:#0d2218;border-color:#44bb7744;color:#44bb77}}
#dl-go:hover{{background:#132d1f;border-color:#44bb77aa}}
#dl-go:disabled{{opacity:.4;cursor:wait}}
#dl-cancel{{background:transparent;border-color:#2a2a2a;color:#555}}
#dl-cancel:hover{{border-color:#444;color:#888}}
#dl-save-creds{{font-size:10px;color:#444;display:flex;align-items:center;gap:6px;margin-top:10px;cursor:pointer}}

/* Popup */
.leaflet-popup-tip-container,.leaflet-popup-tip{{display:none!important}}
.leaflet-popup-content-wrapper{{
  background:#141414!important;border:1px solid #282828!important;
  border-radius:10px!important;box-shadow:0 16px 40px rgba(0,0,0,.95)!important;
  color:#e0e0e0!important;
}}
.leaflet-popup-content{{margin:0!important;padding:0!important}}
.leaflet-popup-close-button{{color:#444!important;font-size:16px!important;padding:8px 10px!important;top:2px!important;right:2px!important}}
.leaflet-popup-close-button:hover{{color:#aaa!important;background:none!important}}
.pu{{width:260px;padding:13px}}
.pu-img{{width:100%;max-height:190px;object-fit:contain;object-position:center;
  border-radius:6px;margin-bottom:10px;display:block;cursor:pointer;background:#0d0d0d;
  border:1px solid #1e1e1e}}
.pu h3{{font-size:11.5px;font-weight:600;color:#e8e8e8;margin-bottom:6px;font-family:monospace;letter-spacing:.03em;line-height:1.4}}
.pu-tags{{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:8px}}
.pu-tag{{font-size:9.5px;padding:2px 7px;border-radius:3px;border:1px solid #222;color:#777;background:#111}}
.pu-tag.sat{{color:#aaa;border-color:#2e2e2e}}
.pu .meta{{font-size:11px;color:#555;margin-bottom:10px;line-height:1.8}}
.pu-footer{{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}}
.pu-nav{{display:flex;align-items:center;gap:5px}}
.pu-nav button{{
  background:#1a1a1a;border:1px solid #2a2a2a;color:#777;
  padding:4px 10px;border-radius:5px;cursor:pointer;font-size:10.5px;transition:all .12s;
}}
.pu-nav button:hover{{background:#242424;color:#bbb;border-color:#444}}
.pu-nav button:disabled{{opacity:.2;cursor:default}}
.pu-nav .pu-count{{font-size:10px;color:#444;white-space:nowrap;min-width:40px;text-align:center}}
.pu a{{
  font-size:10.5px;color:#4d9fff;text-decoration:none;
  padding:4px 10px;border:1px solid #4d9fff22;border-radius:5px;transition:all .12s;
}}
.pu a:hover{{background:#4d9fff12;border-color:#4d9fff44}}
.leaflet-control-zoom{{border:1px solid #1e1e1e!important;border-radius:6px!important;overflow:hidden}}
.leaflet-control-zoom a{{
  background:#111!important;color:#555!important;border-color:#1e1e1e!important;
  width:28px!important;height:28px!important;line-height:28px!important;font-size:15px!important;
}}
.leaflet-control-zoom a:hover{{background:#1e1e1e!important;color:#aaa!important}}
.leaflet-control-attribution{{background:rgba(0,0,0,.35)!important;color:#2a2a2a!important;font-size:9px!important}}
.leaflet-control-attribution a{{color:#2a2a2a!important}}
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
  <div class="filter-section">
    <span class="filter-label">Satellite</span>
    {sat_buttons}
    <button class="sat-quick" id="sat-all">All</button>
    <button class="sat-quick" id="sat-none">None</button>
  </div>

  <div class="filter-section">
    <span class="filter-label">Years</span>
    <span class="yr-val" id="yr-lo">{year_min}</span>
    <div class="slider-wrap">
      <div id="slider-track"></div>
      <div id="slider-fill"></div>
      <input type="range" id="range-lo" min="{year_min}" max="{year_max}" value="{year_min}" step="1">
      <input type="range" id="range-hi" min="{year_min}" max="{year_max}" value="{year_max}" step="1">
      <div class="thumb" id="thumb-lo"></div>
      <div class="thumb" id="thumb-hi"></div>
    </div>
    <span class="yr-val" id="yr-hi">{year_max}</span>
  </div>

  <div class="filter-section">
    <span class="filter-label">Basemap</span>
    <button class="bm-btn on" data-bm="dark">Dark</button>
    <button class="bm-btn" data-bm="satellite">Satellite</button>
    <button class="bm-btn" data-bm="hybrid">Hybrid</button>
    <button class="bm-btn" data-bm="osm">OSM</button>
  </div>

  <div class="filter-section">
    <button id="reset-btn">Reset</button>
  </div>
</div>

<div id="map">
  <div id="empty-state">
    <p>No scenes selected</p>
    <small>Choose a satellite type above to show footprints</small>
  </div>
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
  <div>
    <div class="ov-section">✈ Air Power</div>
    <button class="ov-btn" data-ov="airbases"><span class="ov-icon">✈</span>Military Airbases<span class="ov-badge" id="badge-airbases"></span></button>
    <button class="ov-btn" data-ov="airfields"><span class="ov-icon">🛩</span>Airfields / Strips<span class="ov-badge" id="badge-airfields"></span></button>
  </div>
  <div>
    <div class="ov-section">☢ Nuclear</div>
    <button class="ov-btn" data-ov="silos"><span class="ov-icon">🚀</span>ICBM Missile Silos<span class="ov-badge" id="badge-silos"></span></button>
    <button class="ov-btn" data-ov="nuclear"><span class="ov-icon">☢</span>Nuclear Sites<span class="ov-badge" id="badge-nuclear"></span></button>
  </div>
  <div>
    <div class="ov-section">🛡 Ground Forces</div>
    <button class="ov-btn" data-ov="bunkers"><span class="ov-icon">🏰</span>Bunkers / Fortifications<span class="ov-badge" id="badge-bunkers"></span></button>
    <button class="ov-btn" data-ov="naval"><span class="ov-icon">⚓</span>Naval Bases<span class="ov-badge" id="badge-naval"></span></button>
    <button class="ov-btn" data-ov="radar"><span class="ov-icon">📡</span>Radar / Early Warning<span class="ov-badge" id="badge-radar"></span></button>
  </div>
</div>

<!-- USGS status -->
<div id="usgs-status" class="checking" title="USGS EarthExplorer API — checks every 60s">
  <span id="status-dot"></span>
  <span id="status-label">USGS …</span>
</div>

<!-- Download modal -->
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

// ── Filter state ──────────────────────────────────────────────────────────────
const satActive = {{}};
document.querySelectorAll('.sat-btn').forEach(b => satActive[b.dataset.sat] = false);

let yearLo = YEAR_MIN, yearHi = YEAR_MAX, yearFiltering = false, searchQ = '';

function anySatOn() {{ return Object.values(satActive).some(Boolean); }}

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

function buildLayers() {{
  Object.values(layers).forEach(l => {{ try {{ map.removeLayer(l); }} catch(e) {{}} }});
  visibleFeats = [];

  if (!anySatOn()) {{
    updateCounter(0);
    return;
  }}

  const feats = GEOJSON.features.filter(f => {{
    const p = f.properties;
    if (!satActive[p.satellite]) return false;
    if (yearFiltering && p.year !== null && (p.year < yearLo || p.year > yearHi)) return false;
    if (searchQ) {{
      const q = searchQ.toLowerCase();
      if (!p.entityId.toLowerCase().includes(q) && !(p.displayId||'').toLowerCase().includes(q)) return false;
    }}
    return true;
  }});

  // Group by dataset for colour coding
  const byDs = {{}};
  feats.forEach(f => {{
    const ds = f.properties.dataset;
    if (!byDs[ds]) byDs[ds] = [];
    byDs[ds].push(f);
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

  visibleFeats = feats;
  updateCounter(feats.length);
}}

function updateCounter(n) {{
  const el = document.getElementById('counter');
  const total = GEOJSON.features.length;
  el.textContent = n.toLocaleString() + ' of ' + total.toLocaleString() + ' scenes';
  el.classList.toggle('has-scenes', n > 0);
  document.getElementById('empty-state').classList.toggle('hidden', n > 0 || anySatOn());
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
  const imgHtml = p.browse
    ? `<img class="pu-img" src="${{p.browse}}" onerror="this.style.display='none'" title="Click to view full image" onclick="window.open('${{p.browse}}','_blank')">`
    : '';
  const nav = puFeats.length > 1 ? `
    <div class="pu-nav">
      <button id="pu-prev" ${{puIdx===0?'disabled':''}}>← Prev</button>
      <span class="pu-count">${{puIdx+1}} / ${{puFeats.length}}</span>
      <button id="pu-next" ${{puIdx===puFeats.length-1?'disabled':''}}>Next →</button>
    </div>` : '';

  popup.setContent(`<div class="pu">
    ${{imgHtml}}
    <h3>${{p.entityId}}</h3>
    <div class="pu-tags">
      <span class="pu-tag sat">${{p.satellite}}</span>
      <span class="pu-tag" style="color:${{c}}99;border-color:${{c}}28">${{dsShort}}</span>
    </div>
    <div class="meta">📅 ${{date}}</div>
    <div class="pu-footer">
      <a href="${{p.earthExplorerUrl}}" target="_blank">EarthExplorer ↗</a>
      <button class="pu-dl-btn" data-eid="${{p.entityId}}" data-ds="${{p.dataset}}">⬇ Download</button>
      ${{nav}}
    </div>
  </div>`);

  setTimeout(() => {{
    const prev = document.getElementById('pu-prev');
    const next = document.getElementById('pu-next');
    if (prev) prev.addEventListener('click', ()=>{{ puIdx--; renderPopup(); }});
    if (next) next.addEventListener('click', ()=>{{ puIdx++; renderPopup(); }});
    const dlBtn = popup.getElement()?.querySelector('.pu-dl-btn');
    if (dlBtn) dlBtn.addEventListener('click', e => {{
      e.stopPropagation();
      openDownloadModal(dlBtn.dataset.eid, dlBtn.dataset.ds);
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
document.querySelectorAll('.sat-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const s = btn.dataset.sat;
    satActive[s] = !satActive[s];
    btn.classList.toggle('on', satActive[s]);
    buildLayers();
  }});
}});
document.getElementById('sat-all').addEventListener('click', () => {{
  Object.keys(satActive).forEach(k => satActive[k] = true);
  document.querySelectorAll('.sat-btn').forEach(b => b.classList.add('on'));
  buildLayers();
}});
document.getElementById('sat-none').addEventListener('click', () => {{
  Object.keys(satActive).forEach(k => satActive[k] = false);
  document.querySelectorAll('.sat-btn').forEach(b => b.classList.remove('on'));
  buildLayers();
}});

// ── Year slider ───────────────────────────────────────────────────────────────
const rangeLo=document.getElementById('range-lo'), rangeHi=document.getElementById('range-hi');
const thumbLo=document.getElementById('thumb-lo'), thumbHi=document.getElementById('thumb-hi');
const fill=document.getElementById('slider-fill');

function updateSlider() {{
  const lo=parseInt(rangeLo.value), hi=parseInt(rangeHi.value);
  const pct=v=>(v-YEAR_MIN)/(YEAR_MAX-YEAR_MIN)*100;
  fill.style.left=pct(lo)+'%'; fill.style.width=(pct(hi)-pct(lo))+'%';
  thumbLo.style.left=pct(lo)+'%'; thumbHi.style.left=pct(hi)+'%';
  document.getElementById('yr-lo').textContent=lo;
  document.getElementById('yr-hi').textContent=hi;
  const active=lo>YEAR_MIN||hi<YEAR_MAX;
  fill.classList.toggle('active',active);
  thumbLo.classList.toggle('active',active);
  thumbHi.classList.toggle('active',active);
}}
function updateZIndex() {{
  // Give higher z-index to whichever thumb is at/near the max end
  // so the lo thumb is always reachable on the left
  const lo=parseInt(rangeLo.value), hi=parseInt(rangeHi.value);
  const range=YEAR_MAX-YEAR_MIN;
  const loFrac=(lo-YEAR_MIN)/range, hiFrac=(hi-YEAR_MIN)/range;
  // If hi thumb is near the left side, it would cover lo — give lo priority
  rangeLo.classList.toggle('on-top', loFrac >= hiFrac - 0.01);
  rangeHi.classList.toggle('on-top', hiFrac > loFrac + 0.01);
}}
rangeLo.addEventListener('input',()=>{{
  if(parseInt(rangeLo.value)>parseInt(rangeHi.value)) rangeLo.value=rangeHi.value;
  yearLo=parseInt(rangeLo.value); yearFiltering=yearLo>YEAR_MIN||yearHi<YEAR_MAX;
  updateSlider(); updateZIndex(); buildLayers();
}});
rangeHi.addEventListener('input',()=>{{
  if(parseInt(rangeHi.value)<parseInt(rangeLo.value)) rangeHi.value=rangeLo.value;
  yearHi=parseInt(rangeHi.value); yearFiltering=yearLo>YEAR_MIN||yearHi<YEAR_MAX;
  updateSlider(); updateZIndex(); buildLayers();
}});
updateSlider(); updateZIndex();

// ── Reset ─────────────────────────────────────────────────────────────────────
document.getElementById('reset-btn').addEventListener('click', () => {{
  Object.keys(satActive).forEach(k => satActive[k]=false);
  document.querySelectorAll('.sat-btn').forEach(b => b.classList.remove('on'));
  rangeLo.value=YEAR_MIN; rangeHi.value=YEAR_MAX;
  yearLo=YEAR_MIN; yearHi=YEAR_MAX; yearFiltering=false;
  updateSlider();
  searchQ=''; document.getElementById('search').value='';
  buildLayers();
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
const OV_QUERIES = {{
  airbases: `[out:json][timeout:25];(node["military"="airfield"](__BBOX__);way["military"="airfield"](__BBOX__);node["aeroway"="aerodrome"]["military"](__BBOX__););out center;`,
  airfields: `[out:json][timeout:25];(node["aeroway"="airstrip"](__BBOX__);node["aeroway"="airfield"](__BBOX__);way["aeroway"~"aerodrome|airstrip"](__BBOX__););out center;`,
  silos:    `[out:json][timeout:25];(node["military"="missile_silo"](__BBOX__);node["military"~"silo|icbm|missile"](__BBOX__););out center;`,
  nuclear:  `[out:json][timeout:25];(node["power"="plant"]["plant:source"="nuclear"](__BBOX__);way["power"="plant"]["plant:source"="nuclear"](__BBOX__);node["military"="nuclear_explosion_site"](__BBOX__););out center;`,
  bunkers:  `[out:json][timeout:25];(node["military"~"bunker|fortification|stronghold|checkpoint"](__BBOX__);way["military"~"bunker|fortification"](__BBOX__););out center;`,
  naval:    `[out:json][timeout:25];(node["military"="naval_base"](__BBOX__);way["military"="naval_base"](__BBOX__);node["harbour"="military"](__BBOX__););out center;`,
  radar:    `[out:json][timeout:25];(node["military"~"radar|early_warning|range"](__BBOX__);node["man_made"="tower"]["military"](__BBOX__););out center;`,
}};
const OV_COLORS = {{
  airbases:'#4d9fff',airfields:'#4d9fff',silos:'#ff4d4d',
  nuclear:'#ff9933',bunkers:'#888',naval:'#4dffff',radar:'#cc44ff'
}};
const ovLayers = {{}};
const ovCache  = {{}};

function ovMarker(lat, lon, name, key) {{
  const c = OV_COLORS[key] || '#fff';
  return L.circleMarker([lat, lon], {{
    radius:5, color:c, fillColor:c, fillOpacity:.7, weight:1.5, opacity:.9
  }}).bindPopup(`<div style="font-size:11px;color:#ccc;background:#141414;padding:6px 10px;border-radius:6px">${{name||'Unknown'}}</div>`,
    {{className:'ov-popup', closeButton:false}});
}}

async function loadOverlay(key) {{
  const btn = document.querySelector(`.ov-btn[data-ov="${{key}}"]`);
  if (ovLayers[key]) {{
    map.removeLayer(ovLayers[key]); delete ovLayers[key];
    btn?.classList.remove('on');
    updateOvToggle();
    return;
  }}
  btn && (btn.disabled = true);
  const b = map.getBounds();
  const bbox = `${{b.getSouth().toFixed(3)}},${{b.getWest().toFixed(3)}},${{b.getNorth().toFixed(3)}},${{b.getEast().toFixed(3)}}`;
  const q = OV_QUERIES[key].replace(/__BBOX__/g, bbox);
  try {{
    if (!ovCache[key]) {{
      const resp = await fetch('https://overpass-api.de/api/interpreter', {{
        method:'POST', body:'data='+encodeURIComponent(q)
      }});
      if (!resp.ok) throw new Error(`Overpass HTTP ${{resp.status}}`);
      ovCache[key] = await resp.json();
    }}
    const elements = ovCache[key].elements || [];
    // nodes have lat/lon directly; ways/relations have center.lat/center.lon
    const points = elements.map(el => {{
      const lat = el.lat ?? el.center?.lat;
      const lon = el.lon ?? el.center?.lon;
      return (lat && lon) ? {{ lat, lon, name: el.tags?.name || el.tags?.operator || '' }} : null;
    }}).filter(Boolean);
    const layer = L.layerGroup(
      points.map(p => ovMarker(p.lat, p.lon, p.name, key))
    );
    layer.addTo(map);
    ovLayers[key] = layer;
    btn?.classList.add('on');
    const badge = document.getElementById(`badge-${{key}}`);
    if (badge) badge.textContent = points.length || '';
    updateOvToggle();
  }} catch(e) {{ console.warn('Overlay error:', e); }}
  btn && (btn.disabled = false);
}}

function updateOvToggle() {{
  const tog = document.getElementById('ov-toggle');
  tog.classList.toggle('has-active', Object.keys(ovLayers).length > 0);
}}

document.getElementById('ov-toggle').addEventListener('click', () => {{
  const panel = document.getElementById('ov-panel');
  const tog   = document.getElementById('ov-toggle');
  panel.classList.toggle('open');
  tog.classList.toggle('open');
}});
document.querySelectorAll('.ov-btn').forEach(btn =>
  btn.addEventListener('click', () => loadOverlay(btn.dataset.ov))
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
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    username = os.environ.get("M2M_USERNAME")
    token    = os.environ.get("M2M_TOKEN")
    if not username or not token:
        raise RuntimeError("M2M_USERNAME and M2M_TOKEN must be set")

    print("Logging in to USGS M2M API...")
    api_key = login(username, token)

    all_features = []
    try:
        for dataset, filter_id in DATASETS.items():
            print(f"\n  {DATASET_LABELS[dataset]}...")
            scenes = search_available(api_key, dataset, filter_id)
            before = len(all_features)
            for scene in scenes:
                f = scene_to_feature(scene, dataset)
                if f:
                    all_features.append(f)
            print(f"  {len(all_features) - before:,} features with spatial bounds")
    finally:
        logout(api_key)

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

    with open("available_scenes.geojson", "w") as f:
        json.dump(geojson, f)
    print("Saved available_scenes.geojson")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(geojson))
    print("Saved index.html")

    print(f"\nDone — {len(all_features):,} scenes mapped.")


if __name__ == "__main__":
    main()
