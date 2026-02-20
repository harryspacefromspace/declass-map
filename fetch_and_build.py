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
        if 9025 <= n <= 9058:    return "KH-4"
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
    geom = scene.get("spatialBounds") or scene.get("spatialCoverage")
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
                f"{dataset}/{entity_id}/"
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

    # Dataset buttons — start active (show all by default before any filter is applied)
    dataset_buttons = "\n      ".join(
        f'<button class="ds-btn on" data-ds="{ds}" style="--c:{DATASET_COLORS[ds]}">'
        f'{DATASET_LABELS[ds].split("—")[0].strip()}</button>'
        for ds in DATASET_LABELS if ds in counts
    )

    # Satellite buttons — start OFF (additive model)
    sat_buttons = "\n      ".join(
        f'<button class="sat-btn" data-sat="{s}">{s}</button>'
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
body{{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;height:100vh;display:flex;flex-direction:column;overflow:hidden}}

#header{{background:#111;border-bottom:1px solid #1e1e1e;padding:8px 14px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;z-index:1000}}
#header h1{{font-size:13px;font-weight:600;color:#fff;white-space:nowrap}}
#header h1 span{{color:#444;font-weight:400;margin-left:5px;font-size:12px}}
#stats{{font-size:11px;color:#555;display:flex;align-items:center;gap:4px;flex-wrap:wrap}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:3px}}
#search{{background:#161616;border:1px solid #2a2a2a;color:#ccc;padding:4px 9px;border-radius:4px;font-size:11px;width:145px;outline:none;margin-left:auto}}
#search:focus{{border-color:#555}}
#search::placeholder{{color:#444}}

#filters{{background:#0d0d0d;border-bottom:1px solid #191919;padding:7px 14px;display:flex;align-items:center;gap:20px;flex-wrap:wrap}}
.filter-group{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.filter-label{{font-size:10px;color:#444;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap;min-width:50px}}

/* Dataset toggles */
.ds-btn{{
  background:transparent;border:1px solid #2a2a2a;color:#444;
  padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px;transition:all .15s;
}}
.ds-btn.on{{color:var(--c,#ccc);border-color:var(--c,#555);background:color-mix(in srgb,var(--c) 10%,transparent)}}
.ds-btn:hover{{border-color:#555;color:#aaa}}

/* Satellite toggles — additive (off by default, click to add to filter) */
.sat-btn{{
  background:transparent;border:1px solid #222;color:#444;
  padding:2px 8px;border-radius:3px;cursor:pointer;font-size:10px;transition:all .15s;
}}
.sat-btn.on{{background:#222;border-color:#555;color:#ddd}}
.sat-btn:hover{{border-color:#444;color:#999}}

/* Year slider */
#date-group{{display:flex;align-items:center;gap:8px}}
#date-group .filter-label{{min-width:unset}}
.yr-val{{font-size:11px;color:#666;min-width:30px;text-align:center}}
.slider-wrap{{position:relative;width:150px;height:20px}}
#slider-track{{position:absolute;top:50%;left:0;right:0;height:2px;background:#222;transform:translateY(-50%);border-radius:2px}}
#slider-fill{{position:absolute;top:50%;height:2px;background:#3a3a3a;transform:translateY(-50%);border-radius:2px;transition:background .2s}}
#slider-fill.active{{background:#555}}
input[type=range]{{position:absolute;top:0;left:0;width:100%;height:100%;opacity:0;cursor:pointer;pointer-events:auto;margin:0}}
.thumb{{position:absolute;top:50%;width:11px;height:11px;background:#444;border-radius:50%;transform:translate(-50%,-50%);pointer-events:none;border:1px solid #666;transition:background .15s}}
.thumb.active{{background:#888}}

.bm-btn{{background:transparent;border:1px solid #222;color:#444;padding:3px 9px;border-radius:4px;cursor:pointer;font-size:10px;transition:all .15s}}
.bm-btn:hover{{border-color:#555;color:#888}}
.bm-btn.on{{background:#1e1e1e;border-color:#555;color:#ccc}}
#reset-filters{{background:transparent;border:1px solid #222;color:#444;padding:3px 9px;border-radius:4px;cursor:pointer;font-size:10px;transition:all .15s}}
#reset-filters:hover{{border-color:#555;color:#888}}
.leaflet-popup-tip-container{{display:none!important}}

#map{{flex:1}}

#counter{{
  position:absolute;bottom:14px;left:50%;transform:translateX(-50%);
  background:rgba(0,0,0,.8);backdrop-filter:blur(6px);
  border:1px solid #222;color:#555;padding:5px 14px;
  border-radius:20px;font-size:11px;z-index:1000;pointer-events:none;
  transition:color .2s;
}}
#counter.has-scenes{{color:#888}}

/* Popup */
.leaflet-popup-content-wrapper{{background:#1a1a1a!important;border:1px solid #2e2e2e!important;border-radius:8px!important;box-shadow:0 12px 32px rgba(0,0,0,.9)!important;color:#e0e0e0!important}}
.leaflet-popup-content{{margin:0!important;padding:0!important}}
.leaflet-popup-tip{{background:#1a1a1a!important}}
.pu{{min-width:250px;max-width:270px;padding:12px}}
.pu img{{width:100%;max-height:200px;object-fit:contain;object-position:center;border-radius:4px;margin-bottom:9px;display:block;cursor:pointer;background:#111}}
.pu h3{{font-size:12px;font-weight:600;color:#fff;margin-bottom:5px;font-family:monospace;letter-spacing:.02em}}
.pu-tags{{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:7px}}
.pu-tag{{font-size:10px;padding:2px 7px;border-radius:3px;border:1px solid #333;color:#999;background:#1e1e1e}}
.pu-tag.sat{{color:#bbb;border-color:#3a3a3a}}
.pu .meta{{font-size:11px;color:#666;margin-bottom:9px;line-height:1.7}}
.pu-footer{{display:flex;align-items:center;justify-content:space-between;gap:8px}}
.pu-nav{{display:flex;align-items:center;gap:6px}}
.pu-nav button{{background:#1e1e1e;border:1px solid #333;color:#888;padding:3px 9px;border-radius:4px;cursor:pointer;font-size:11px;transition:all .15s}}
.pu-nav button:hover{{background:#2a2a2a;color:#bbb;border-color:#555}}
.pu-nav button:disabled{{opacity:.25;cursor:default}}
.pu-nav .pu-count{{font-size:10px;color:#555;white-space:nowrap}}
.pu a{{font-size:11px;color:#00aaff;text-decoration:none;padding:3px 10px;border:1px solid #00aaff22;border-radius:4px;transition:all .15s;white-space:nowrap}}
.pu a:hover{{background:#00aaff15;border-color:#00aaff55}}
.leaflet-control-zoom a{{background:#161616!important;color:#666!important;border-color:#222!important}}
.leaflet-control-attribution{{background:rgba(0,0,0,.4)!important;color:#333!important;font-size:9px!important}}
.leaflet-control-attribution a{{color:#333!important}}
</style>
</head>
<body>

<div id="header">
  <h1>🛰 Declassified Satellite <span>Available Downloads</span></h1>
  <div id="stats">{counts_html} &nbsp;|&nbsp; Updated <strong>{generated[:10]}</strong></div>
  <input id="search" type="text" placeholder="Search entity ID…" />
</div>

<div id="filters">
  <div class="filter-group">
    <span class="filter-label">Dataset</span>
    {dataset_buttons}
  </div>

  <div class="filter-group">
    <span class="filter-label">Satellite</span>
    {sat_buttons}
  </div>

  <div class="filter-group" id="date-group">
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

  <div class="filter-group" style="margin-left:auto">
    <span class="filter-label">Basemap</span>
    <button class="bm-btn on" data-bm="dark">Dark</button>
    <button class="bm-btn" data-bm="satellite">Satellite</button>
    <button class="bm-btn" data-bm="hybrid">Hybrid</button>
    <button class="bm-btn" data-bm="osm">OSM</button>
  </div>
  <button id="reset-filters">Reset filters</button>
</div>

<div id="map"></div>
<div id="counter">{total:,} scenes</div>

<script>
const GEOJSON   = {geojson_str};
const DS_COLORS = {ds_colors_json};
const YEAR_MIN  = {year_min};
const YEAR_MAX  = {year_max};

// ── Leaflet ──────────────────────────────────────────────────────────────────
const map = L.map('map', {{center:[35,30], zoom:2, preferCanvas:true}});

const BASEMAPS = {{
  dark: L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',
    {{attribution:'© CartoDB © OpenStreetMap', subdomains:'abcd', maxZoom:19}}),
  satellite: L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
    {{attribution:'© Esri © USGS', maxZoom:19}}),
  hybrid: [
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{{z}}/{{y}}/{{x}}',
      {{attribution:'© Esri © USGS', maxZoom:19}}),
    L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{{z}}/{{y}}/{{x}}',
      {{opacity:0.7, maxZoom:19}})
  ],
  osm: L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png',
    {{attribution:'© OpenStreetMap contributors', maxZoom:19}})
}};
let activeBmLayers = [];
function setBasemap(key) {{
  activeBmLayers.forEach(l => map.removeLayer(l));
  activeBmLayers = [];
  const bm = BASEMAPS[key];
  if (Array.isArray(bm)) {{ bm.forEach(l => {{ l.addTo(map); l.bringToBack(); activeBmLayers.push(l); }}); }}
  else {{ bm.addTo(map); bm.bringToBack(); activeBmLayers.push(bm); }}
  document.querySelectorAll('.bm-btn').forEach(b => b.classList.toggle('on', b.dataset.bm===key));
}}
setBasemap('dark');

// ── Filter state ─────────────────────────────────────────────────────────────
// Datasets: on by default (show all). Click to toggle off.
const dsActive = Object.fromEntries(Object.keys(DS_COLORS).map(k => [k, true]));

// Satellites: OFF by default (additive). No sat buttons on = show all.
// When any sat is turned on, show ONLY those turned on.
const satActive = {{}};
document.querySelectorAll('.sat-btn').forEach(b => satActive[b.dataset.sat] = false);

let yearLo = YEAR_MIN, yearHi = YEAR_MAX;
let yearFiltering = false;   // true only when slider has been moved from defaults
let searchQ = '';

function anySatOn() {{
  return Object.values(satActive).some(v => v);
}}

// ── Layer management ──────────────────────────────────────────────────────────
const layers = {{}};
let visibleFeats = [];  // flat list of currently-shown features for hit-testing

function styleFor(ds) {{
  const c = DS_COLORS[ds] || '#fff';
  return {{color:c, weight:1, fillColor:c, fillOpacity:0.13}};
}}
function styleHover(ds) {{
  const c = DS_COLORS[ds] || '#fff';
  return {{color:c, weight:2, fillColor:c, fillOpacity:0.45}};
}}

function buildLayers() {{
  Object.values(layers).forEach(l => {{ try {{ map.removeLayer(l); }} catch(e) {{}} }});
  visibleFeats = [];
  let shown = 0;
  const satFiltering = anySatOn();

  Object.keys(DS_COLORS).forEach(ds => {{
    if (!dsActive[ds]) return;

    const feats = GEOJSON.features.filter(f => {{
      const p = f.properties;
      if (p.dataset !== ds) return false;
      if (satFiltering && !satActive[p.satellite]) return false;
      if (yearFiltering && p.year !== null && (p.year < yearLo || p.year > yearHi)) return false;
      if (searchQ) {{
        const q = searchQ.toLowerCase();
        if (!p.entityId.toLowerCase().includes(q) &&
            !(p.displayId||'').toLowerCase().includes(q)) return false;
      }}
      return true;
    }});

    layers[ds] = L.geoJSON({{type:'FeatureCollection', features:feats}}, {{
      style: () => styleFor(ds),
      onEachFeature: (feat, layer) => {{
        layer.on('mouseover', () => layer.setStyle(styleHover(feat.properties.dataset)));
        layer.on('mouseout',  () => layer.setStyle(styleFor(feat.properties.dataset)));
      }}
    }});
    layers[ds].addTo(map);
    visibleFeats = visibleFeats.concat(feats);
    shown += feats.length;
  }});

  const counter = document.getElementById('counter');
  counter.textContent = shown.toLocaleString() + ' scenes';
  counter.classList.toggle('has-scenes', shown > 0);
}}

buildLayers();

// ── Multi-scene popup on map click ────────────────────────────────────────────
function ptInPoly(latlng, geom) {{
  const pt = [latlng.lng, latlng.lat];
  function inRing(pt, ring) {{
    let inside = false;
    for (let i=0, j=ring.length-1; i<ring.length; j=i++) {{
      const xi=ring[i][0], yi=ring[i][1], xj=ring[j][0], yj=ring[j][1];
      if (((yi>pt[1])!==(yj>pt[1])) && (pt[0] < (xj-xi)*(pt[1]-yi)/(yj-yi)+xi))
        inside = !inside;
    }}
    return inside;
  }}
  function testPoly(rings) {{
    if (!inRing(pt, rings[0])) return false;
    for (let i=1;i<rings.length;i++) if (inRing(pt,rings[i])) return false;
    return true;
  }}
  if (geom.type==='Polygon') return testPoly(geom.coordinates);
  if (geom.type==='MultiPolygon') return geom.coordinates.some(p=>testPoly(p));
  return false;
}}

function polyArea(geom) {{
  function ringArea(ring) {{
    let a=0;
    for (let i=0,j=ring.length-1;i<ring.length;j=i++)
      a += (ring[j][0]+ring[i][0])*(ring[j][1]-ring[i][1]);
    return Math.abs(a/2);
  }}
  if (geom.type==='Polygon') return ringArea(geom.coordinates[0]);
  if (geom.type==='MultiPolygon') return geom.coordinates.reduce((s,p)=>s+ringArea(p[0]),0);
  return 0;
}}

const popup = L.popup({{maxWidth:300, className:'multi-popup'}});
let puFeats=[], puIdx=0;
let highlightLayer = null;

function highlightFootprint(feat) {{
  if (highlightLayer) {{ map.removeLayer(highlightLayer); highlightLayer = null; }}
  if (!feat) return;
  const c = DS_COLORS[feat.properties.dataset] || '#fff';
  highlightLayer = L.geoJSON(feat, {{
    style: {{color:'#fff', weight:2.5, fillColor:c, fillOpacity:0, dashArray:'6 4', className:'selected-footprint'}}
  }}).addTo(map);
}}

function renderPopup() {{
  highlightFootprint(puFeats[puIdx]);
  const p = puFeats[puIdx].properties;
  const dsColor = DS_COLORS[p.dataset] || '#fff';
  const dsShort = p.datasetLabel.split('—')[0].trim();
  const date = p.acquisitionDate ? p.acquisitionDate.slice(0,10) : '—';
  const browseHtml = p.browse
    ? `<img src="${{p.browse}}" onerror="this.style.display='none'" title="View full image" onclick="window.open('${{p.browse}}','_blank')">`
    : '';
  const nav = puFeats.length > 1 ? `
    <div class="pu-nav">
      <button id="pu-prev" ${{puIdx===0?'disabled':''}}>← Prev</button>
      <span class="pu-count">${{puIdx+1}} of ${{puFeats.length}}</span>
      <button id="pu-next" ${{puIdx===puFeats.length-1?'disabled':''}}>Next →</button>
    </div>` : '';

  popup.setContent(`<div class="pu">
    ${{browseHtml}}
    <h3>${{p.entityId}}</h3>
    <div class="pu-tags">
      <span class="pu-tag sat">🛸 ${{p.satellite}}</span>
      <span class="pu-tag" style="color:${{dsColor}}aa;border-color:${{dsColor}}33">${{dsShort}}</span>
    </div>
    <div class="meta">📅 ${{date}}</div>
    <div class="pu-footer">
      <a href="${{p.earthExplorerUrl}}" target="_blank">EarthExplorer →</a>
      ${{nav}}
    </div>
  </div>`);

  setTimeout(() => {{
    const prev = document.getElementById('pu-prev');
    const next = document.getElementById('pu-next');
    if (prev) prev.addEventListener('click', ()=>{{ puIdx--; renderPopup(); }});
    if (next) next.addEventListener('click', ()=>{{ puIdx++; renderPopup(); }});
  }}, 0);
}}

map.on('click', e => {{
  const hits = visibleFeats.filter(f => ptInPoly(e.latlng, f.geometry));
  if (!hits.length) return;
  // Smallest area first (most specific scene on top)
  hits.sort((a,b) => polyArea(a.geometry) - polyArea(b.geometry));
  puFeats = hits;
  puIdx   = 0;
  popup.setLatLng(e.latlng).addTo(map);
  renderPopup();
}});
map.on('popupclose', () => {{ if (highlightLayer) {{ map.removeLayer(highlightLayer); highlightLayer = null; }} }});

// ── Dataset buttons ───────────────────────────────────────────────────────────
document.querySelectorAll('.ds-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const ds = btn.dataset.ds;
    dsActive[ds] = !dsActive[ds];
    btn.classList.toggle('on', dsActive[ds]);
    buildLayers();
  }});
}});

// ── Satellite buttons (additive) ──────────────────────────────────────────────
document.querySelectorAll('.sat-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const s = btn.dataset.sat;
    satActive[s] = !satActive[s];
    btn.classList.toggle('on', satActive[s]);
    buildLayers();
  }});
}});

// ── Year slider ───────────────────────────────────────────────────────────────
const rangeLo = document.getElementById('range-lo');
const rangeHi = document.getElementById('range-hi');
const thumbLo = document.getElementById('thumb-lo');
const thumbHi = document.getElementById('thumb-hi');
const fill    = document.getElementById('slider-fill');

function updateSlider() {{
  const lo = parseInt(rangeLo.value), hi = parseInt(rangeHi.value);
  const pct = v => (v - YEAR_MIN) / (YEAR_MAX - YEAR_MIN) * 100;
  fill.style.left  = pct(lo) + '%';
  fill.style.width = (pct(hi) - pct(lo)) + '%';
  thumbLo.style.left = pct(lo) + '%';
  thumbHi.style.left = pct(hi) + '%';
  document.getElementById('yr-lo').textContent = lo;
  document.getElementById('yr-hi').textContent = hi;
  const active = lo > YEAR_MIN || hi < YEAR_MAX;
  fill.classList.toggle('active', active);
  thumbLo.classList.toggle('active', active);
  thumbHi.classList.toggle('active', active);
}}

rangeLo.addEventListener('input', () => {{
  if (parseInt(rangeLo.value) > parseInt(rangeHi.value)) rangeLo.value = rangeHi.value;
  yearLo = parseInt(rangeLo.value);
  yearFiltering = yearLo > YEAR_MIN || yearHi < YEAR_MAX;
  updateSlider(); buildLayers();
}});
rangeHi.addEventListener('input', () => {{
  if (parseInt(rangeHi.value) < parseInt(rangeLo.value)) rangeHi.value = rangeLo.value;
  yearHi = parseInt(rangeHi.value);
  yearFiltering = yearLo > YEAR_MIN || yearHi < YEAR_MAX;
  updateSlider(); buildLayers();
}});
updateSlider();

// ── Reset ──────────────────────────────────────────────────────────────────────
document.getElementById('reset-filters').addEventListener('click', () => {{
  // Datasets back on
  Object.keys(dsActive).forEach(k => dsActive[k] = true);
  document.querySelectorAll('.ds-btn').forEach(b => b.classList.add('on'));
  // Sats back off
  Object.keys(satActive).forEach(k => satActive[k] = false);
  document.querySelectorAll('.sat-btn').forEach(b => b.classList.remove('on'));
  // Year back to full range
  rangeLo.value = YEAR_MIN; rangeHi.value = YEAR_MAX;
  yearLo = YEAR_MIN; yearHi = YEAR_MAX; yearFiltering = false;
  updateSlider();
  searchQ = ''; document.getElementById('search').value = '';
  buildLayers();
}});

// ── Basemap buttons ───────────────────────────────────────────────────────────
document.querySelectorAll('.bm-btn').forEach(btn => {{
  btn.addEventListener('click', () => setBasemap(btn.dataset.bm));
}});

// ── Search ──────────────────────────────────────────────────────────────────
let st;
document.getElementById('search').addEventListener('input', e => {{
  clearTimeout(st);
  st = setTimeout(() => {{ searchQ = e.target.value.trim(); buildLayers(); }}, 300);
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
