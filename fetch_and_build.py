#!/usr/bin/env python3
"""
fetch_and_build.py — queries USGS M2M for all downloadable declassified scenes
and builds a self-contained index.html map with dataset, satellite type, and
date range filters.
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

# Satellite types grouped for the filter UI
# Each entry: (label, datasets it can appear in, match_fn called with satellite string)
SAT_TYPES = [
    "KH-1", "KH-2", "KH-3", "KH-4", "KH-4A", "KH-4B",
    "KH-5 (ARGON)", "KH-6 (LANYARD)",
    "KH-7 (GAMBIT)", "KH-9 (HEXAGON)",
    "Unknown",
]


# ---------------------------------------------------------------------------
# Satellite type logic (mirrors monitor.py get_satellite_type exactly)
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
        if is_argon:            return "KH-5 (ARGON)"
        if 8001 <= n <= 8003:   return "KH-6 (LANYARD)"
        if 9001 <= n <= 9009:   return "KH-1"
        if 9010 <= n <= 9015:   return "KH-2"
        if 9016 <= n <= 9024:   return "KH-3"
        if 9025 <= n <= 9058:   return "KH-4"
        if 1001 <= n <= 1052:   return "KH-4A"
        if 1101 <= n <= 1117:   return "KH-4B"
    elif dataset == "declassii":
        if 4000 <= n <= 4999:   return "KH-7 (GAMBIT)"
        if 1200 <= n <= 1299:   return "KH-9 (HEXAGON)"
        return "KH-7 (GAMBIT)"   # default for declassii
    elif dataset == "declassiii":
        return "KH-9 (HEXAGON)"

    return "Unknown"


def get_mission_from_scene(scene):
    """Extract Mission field from scene metadata array."""
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
    """Search for all scenes with Download Available = Y, paginating fully."""
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

    thumbnail = ""
    browse = scene.get("browse")
    if browse and isinstance(browse, list):
        thumbnail = browse[0].get("thumbnailPath", "")

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
            "thumbnail":       thumbnail,
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

    dataset_buttons = "\n      ".join(
        f'<button class="ds-btn active" data-ds="{ds}" style="--c:{DATASET_COLORS[ds]}">'
        f'{DATASET_LABELS[ds].split("—")[0].strip()}</button>'
        for ds in DATASET_LABELS if ds in counts
    )

    sat_buttons = "\n      ".join(
        f'<button class="sat-btn active" data-sat="{s}">{s}</button>'
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
#header h1 span{{color:#555;font-weight:400;margin-left:5px}}
#stats{{font-size:11px;color:#555;display:flex;align-items:center;gap:4px;flex-wrap:wrap}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:3px}}
#search{{background:#161616;border:1px solid #2a2a2a;color:#ccc;padding:4px 9px;border-radius:4px;font-size:11px;width:145px;outline:none;margin-left:auto}}
#search:focus{{border-color:#444}}

/* Filter bar */
#filters{{background:#0f0f0f;border-bottom:1px solid #1a1a1a;padding:6px 14px;display:flex;align-items:center;gap:16px;flex-wrap:wrap}}
.filter-group{{display:flex;align-items:center;gap:6px;flex-wrap:wrap}}
.filter-label{{font-size:10px;color:#555;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap}}

.ds-btn{{background:#161616;border:1px solid #2a2a2a;color:var(--c,#ccc);padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px;transition:all .15s}}
.ds-btn:hover{{background:#1e1e1e}}
.ds-btn.inactive{{opacity:.3}}

.sat-btn{{background:#161616;border:1px solid #2a2a2a;color:#999;padding:3px 9px;border-radius:4px;cursor:pointer;font-size:10px;transition:all .15s}}
.sat-btn:hover{{background:#1e1e1e;color:#ccc}}
.sat-btn.active{{border-color:#555;color:#ddd}}
.sat-btn.inactive{{opacity:.3}}

/* Date slider */
#date-range{{display:flex;align-items:center;gap:8px}}
#date-range span{{font-size:11px;color:#777;min-width:32px;text-align:center}}
.slider-wrap{{position:relative;width:160px;height:20px}}
#slider-track{{position:absolute;top:50%;left:0;right:0;height:2px;background:#2a2a2a;transform:translateY(-50%);border-radius:2px}}
#slider-fill{{position:absolute;top:50%;height:2px;background:#444;transform:translateY(-50%);border-radius:2px}}
input[type=range]{{position:absolute;top:0;left:0;width:100%;height:100%;opacity:0;cursor:pointer;pointer-events:none}}
input[type=range].active-range{{pointer-events:auto}}
.thumb{{position:absolute;top:50%;width:12px;height:12px;background:#888;border-radius:50%;transform:translate(-50%,-50%);pointer-events:none;transition:background .15s}}

#map{{flex:1}}
#counter{{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.75);backdrop-filter:blur(4px);border:1px solid #2a2a2a;color:#666;padding:5px 14px;border-radius:20px;font-size:11px;z-index:1000;pointer-events:none}}

.leaflet-popup-content-wrapper{{background:#1a1a1a!important;border:1px solid #2e2e2e!important;border-radius:8px!important;box-shadow:0 8px 24px rgba(0,0,0,.8)!important;color:#e0e0e0!important}}
.leaflet-popup-tip{{background:#1a1a1a!important}}
.pu img{{width:100%;border-radius:4px;margin-bottom:7px;display:block}}
.pu h3{{font-size:12px;font-weight:600;color:#fff;margin-bottom:4px;font-family:monospace}}
.pu .meta{{font-size:11px;color:#777;margin-bottom:7px;line-height:1.7}}
.pu .sat-tag{{display:inline-block;font-size:10px;color:#aaa;background:#222;border:1px solid #333;border-radius:3px;padding:1px 6px;margin-bottom:6px}}
.pu a{{display:inline-block;font-size:11px;color:#00aaff;text-decoration:none;padding:3px 9px;border:1px solid #00aaff33;border-radius:4px;transition:all .15s}}
.pu a:hover{{background:#00aaff18}}
.leaflet-control-zoom a{{background:#1a1a1a!important;color:#888!important;border-color:#2a2a2a!important}}
.leaflet-control-attribution{{background:rgba(0,0,0,.5)!important;color:#444!important;font-size:9px!important}}
.leaflet-control-attribution a{{color:#444!important}}
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
  <div class="filter-group" id="date-range">
    <span class="filter-label">Years</span>
    <span id="yr-lo">{year_min}</span>
    <div class="slider-wrap" id="slider-wrap">
      <div id="slider-track"></div>
      <div id="slider-fill"></div>
      <input type="range" id="range-lo" min="{year_min}" max="{year_max}" value="{year_min}" step="1" class="active-range">
      <input type="range" id="range-hi" min="{year_min}" max="{year_max}" value="{year_max}" step="1" class="active-range">
      <div class="thumb" id="thumb-lo"></div>
      <div class="thumb" id="thumb-hi"></div>
    </div>
    <span id="yr-hi">{year_max}</span>
  </div>
</div>

<div id="map"></div>
<div id="counter">{total:,} scenes</div>

<script>
const GEOJSON   = {geojson_str};
const DS_COLORS = {ds_colors_json};
const YEAR_MIN  = {year_min};
const YEAR_MAX  = {year_max};

// ---- Leaflet setup ----
const map = L.map('map', {{center:[30,20], zoom:2, preferCanvas:true}});
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',
  {{attribution:'© CartoDB © OpenStreetMap', subdomains:'abcd', maxZoom:19}}).addTo(map);

// ---- Filter state ----
const dsActive  = Object.fromEntries(Object.keys(DS_COLORS).map(k=>[k,true]));
const satActive = {{}};
document.querySelectorAll('.sat-btn').forEach(b => satActive[b.dataset.sat] = true);
let yearLo = YEAR_MIN, yearHi = YEAR_MAX, searchQ = '';

// ---- Layer management ----
const layers = {{}};

function styleFor(ds) {{
  const c = DS_COLORS[ds] || '#fff';
  return {{color:c, weight:1, fillColor:c, fillOpacity:0.12}};
}}

function buildLayers() {{
  Object.values(layers).forEach(l => {{ try {{ map.removeLayer(l); }} catch(e) {{}} }});
  let shown = 0;

  Object.keys(DS_COLORS).forEach(ds => {{
    const feats = GEOJSON.features.filter(f => {{
      const p = f.properties;
      if (p.dataset !== ds) return false;
      if (!dsActive[ds]) return false;
      if (!satActive[p.satellite]) return false;
      if (p.year !== null && (p.year < yearLo || p.year > yearHi)) return false;
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
        const p = feat.properties;
        const thumb = p.thumbnail
          ? `<img src="${{p.thumbnail}}" onerror="this.style.display='none'">`
          : '';
        const date = p.acquisitionDate
          ? `<br>📅 ${{p.acquisitionDate.slice(0,10)}}` : '';
        layer.bindPopup(`
          <div class="pu" style="min-width:220px">
            ${{thumb}}
            <h3>${{p.entityId}}</h3>
            <span class="sat-tag">🛸 ${{p.satellite}}</span>
            <div class="meta">${{p.datasetLabel}}${{date}}</div>
            <a href="${{p.earthExplorerUrl}}" target="_blank">EarthExplorer →</a>
          </div>`);
        layer.on('mouseover', () => layer.setStyle({{fillOpacity:0.45}}));
        layer.on('mouseout',  () => layer.setStyle(styleFor(ds)));
      }}
    }});
    layers[ds].addTo(map);
    shown += feats.length;
  }});

  document.getElementById('counter').textContent = shown.toLocaleString() + ' scenes';
}}

buildLayers();

// ---- Dataset buttons ----
document.querySelectorAll('.ds-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const ds = btn.dataset.ds;
    dsActive[ds] = !dsActive[ds];
    btn.classList.toggle('inactive', !dsActive[ds]);
    buildLayers();
  }});
}});

// ---- Satellite buttons ----
document.querySelectorAll('.sat-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const s = btn.dataset.sat;
    satActive[s] = !satActive[s];
    btn.classList.toggle('inactive', !satActive[s]);
    buildLayers();
  }});
}});

// ---- Year range slider ----
const rangeLo = document.getElementById('range-lo');
const rangeHi = document.getElementById('range-hi');
const thumbLo = document.getElementById('thumb-lo');
const thumbHi = document.getElementById('thumb-hi');
const fill    = document.getElementById('slider-fill');
const yrLo    = document.getElementById('yr-lo');
const yrHi    = document.getElementById('yr-hi');

function updateSlider() {{
  const lo  = parseInt(rangeLo.value);
  const hi  = parseInt(rangeHi.value);
  const pct = v => (v - YEAR_MIN) / (YEAR_MAX - YEAR_MIN) * 100;
  const lp  = pct(lo), hp = pct(hi);
  fill.style.left  = lp + '%';
  fill.style.width = (hp - lp) + '%';
  thumbLo.style.left = lp + '%';
  thumbHi.style.left = hp + '%';
  yrLo.textContent = lo;
  yrHi.textContent = hi;
}}

rangeLo.addEventListener('input', () => {{
  if (parseInt(rangeLo.value) > parseInt(rangeHi.value))
    rangeLo.value = rangeHi.value;
  yearLo = parseInt(rangeLo.value);
  updateSlider();
  buildLayers();
}});

rangeHi.addEventListener('input', () => {{
  if (parseInt(rangeHi.value) < parseInt(rangeLo.value))
    rangeHi.value = rangeLo.value;
  yearHi = parseInt(rangeHi.value);
  updateSlider();
  buildLayers();
}});

updateSlider();

// ---- Search ----
let searchTimer;
document.getElementById('search').addEventListener('input', e => {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {{ searchQ = e.target.value.trim(); buildLayers(); }}, 300);
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

    counts = {}
    years  = []
    sat_types_seen = []
    for f in all_features:
        p  = f["properties"]
        ds = p["dataset"]
        counts[ds] = counts.get(ds, 0) + 1
        if p.get("year"):
            years.append(p["year"])
        st = p.get("satellite", "Unknown")
        if st not in sat_types_seen:
            sat_types_seen.append(st)

    # Sort satellite types in a logical order
    sat_order = ["KH-1","KH-2","KH-3","KH-4","KH-4A","KH-4B",
                 "KH-5 (ARGON)","KH-6 (LANYARD)","KH-7 (GAMBIT)","KH-9 (HEXAGON)","Unknown"]
    sat_types_seen.sort(key=lambda x: sat_order.index(x) if x in sat_order else 99)

    geojson = {
        "type":     "FeatureCollection",
        "features": all_features,
        "metadata": {
            "generated": datetime.utcnow().isoformat() + "Z",
            "total":     len(all_features),
            "counts":    counts,
            "year_min":  min(years) if years else 1960,
            "year_max":  max(years) if years else 1984,
            "sat_types": sat_types_seen,
        },
    }

    print(f"\nTotal features: {len(all_features):,}")
    print(f"Year range: {geojson['metadata']['year_min']}–{geojson['metadata']['year_max']}")
    print(f"Satellite types: {sat_types_seen}")

    with open("available_scenes.geojson", "w") as f:
        json.dump(geojson, f)
    print("Saved available_scenes.geojson")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(geojson))
    print("Saved index.html")

    print(f"\nDone — {len(all_features):,} scenes mapped.")


if __name__ == "__main__":
    main()
