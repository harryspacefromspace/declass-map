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
input[type=range]{{position:absolute;top:0;left:0;width:100%;height:100%;opacity:0;cursor:pointer;pointer-events:none;margin:0}}.thumb{{position:absolute;top:50%;width:12px;height:12px;background:#484848;border-radius:50%;transform:translate(-50%,-50%);pointer-events:none;border:1px solid #666;transition:background .15s;box-shadow:0 0 0 3px rgba(255,255,255,.04)}}.thumb.active{{background:#888}}

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
  arr.forEach(l => {{ l.addTo(map); l.bringToBack(); activeBmLayers.push(l); }});
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
      ${{nav}}
    </div>
  </div>`);

  setTimeout(() => {{
    const prev = document.getElementById('pu-prev');
    const next = document.getElementById('pu-next');
    if (prev) prev.addEventListener('click', e=>{{ e.stopPropagation(); puIdx--; renderPopup(); }});
    if (next) next.addEventListener('click', e=>{{ e.stopPropagation(); puIdx++; renderPopup(); }});
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
map.on('popupopen', () => {{
  const el = popup.getElement();
  if (el) L.DomEvent.stopPropagation(el);
}});

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
// Proximity-based drag: mousedown picks the nearest thumb, then we drive it manually
const sliderWrap = document.querySelector('.slider-wrap');
let dragging = null;

function getPct(e) {{
  const rect = sliderWrap.getBoundingClientRect();
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  return Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
}}
function pctToYear(p) {{ return Math.round(YEAR_MIN + p * (YEAR_MAX - YEAR_MIN)); }}

sliderWrap.addEventListener('mousedown', startDrag);
sliderWrap.addEventListener('touchstart', startDrag, {{passive:false}});

function startDrag(e) {{
  e.preventDefault();
  const pct  = getPct(e);
  const val  = pctToYear(pct);
  const dLo  = Math.abs(val - yearLo);
  const dHi  = Math.abs(val - yearHi);
  // If equal distance and at max, prefer lo so it can be dragged left
  dragging = (dLo <= dHi && !(val === yearHi && yearLo === yearHi)) ? 'lo' : 'hi';
  moveDrag(e);
}}

function moveDrag(e) {{
  if (!dragging) return;
  const val = pctToYear(getPct(e));
  if (dragging === 'lo') {{
    yearLo = Math.min(val, yearHi);
  }} else {{
    yearHi = Math.max(val, yearLo);
  }}
  yearFiltering = yearLo > YEAR_MIN || yearHi < YEAR_MAX;
  updateSlider();
  buildLayers();
}}

window.addEventListener('mousemove', e => {{ if (dragging) moveDrag(e); }});
window.addEventListener('touchmove',  e => {{ if (dragging) moveDrag(e); }}, {{passive:false}});
window.addEventListener('mouseup',  () => {{ dragging = null; }});
window.addEventListener('touchend', () => {{ dragging = null; }});

updateSlider();

// ── Reset ─────────────────────────────────────────────────────────────────────
document.getElementById('reset-btn').addEventListener('click', () => {{
  Object.keys(satActive).forEach(k => satActive[k]=false);
  document.querySelectorAll('.sat-btn').forEach(b => b.classList.remove('on'));
  yearLo=YEAR_MIN; yearHi=YEAR_MAX; yearFiltering=false;
  updateSlider();
  searchQ=''; document.getElementById('search').value='';
  buildLayers();
}});

// ── Basemap ───────────────────────────────────────────────────────────────────
document.querySelectorAll('.bm-btn').forEach(btn =>
  btn.addEventListener('click', () => setBasemap(btn.dataset.bm)));

// ── Search ────────────────────────────────────────────────────────────────────
let st;
document.getElementById('search').addEventListener('input', e => {{
  clearTimeout(st);
  st=setTimeout(()=>{{ searchQ=e.target.value.trim(); buildLayers(); }},300);
}});
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
