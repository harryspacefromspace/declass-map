#!/usr/bin/env python3
"""
fetch_and_build.py

Searches USGS M2M for all downloadable declassified scenes using the same
filter approach as monitor.py, extracts spatial bounds from the results,
and builds a self-contained index.html map.
"""

import os
import json
import time
import requests
from datetime import datetime

M2M_URL = "https://m2m.cr.usgs.gov/api/api/json/stable/"

# Dataset names and their "Download Available = Y" filter IDs
# These match monitor.py exactly
DATASETS = {
    "corona2":   "5e839feb64cee663",
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
    """
    Search for all scenes with Download Available = Y.
    Mirrors monitor.py's search_dataset() exactly — these results
    include spatialBounds in each scene object.
    """
    all_scenes = []
    starting  = 1
    batch     = 10000

    while True:
        resp = requests.post(
            M2M_URL + "scene-search",
            json={
                "datasetName":   dataset,
                "maxResults":    batch,
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
        print(f"    Retrieved {len(all_scenes):,} scenes...")

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

    thumbnail = ""
    browse = scene.get("browse")
    if browse and isinstance(browse, list):
        thumbnail = browse[0].get("thumbnailPath", "")

    return {
        "type": "Feature",
        "geometry": geom,
        "properties": {
            "entityId":        entity_id,
            "dataset":         dataset,
            "datasetLabel":    DATASET_LABELS.get(dataset, dataset),
            "displayId":       scene.get("displayId", ""),
            "acquisitionDate": acq,
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
    ds_colors_json = json.dumps(DATASET_COLORS)

    counts_html = " &nbsp;|&nbsp; ".join(
        f'<span class="dot" style="background:{DATASET_COLORS[ds]}"></span>'
        f'{DATASET_LABELS[ds].split("—")[0].strip()}: '
        f'<strong>{counts.get(ds, 0):,}</strong>'
        for ds in DATASET_LABELS if ds in counts
    )

    filter_buttons = "\n    ".join(
        f'<button class="filter-btn active" data-ds="{ds}" '
        f'style="--c:{DATASET_COLORS[ds]}">'
        f'{DATASET_LABELS[ds].split("—")[0].strip()}</button>'
        for ds in DATASET_LABELS if ds in counts
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
body{{background:#0d0d0d;color:#e0e0e0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;height:100vh;display:flex;flex-direction:column}}
#header{{background:#111;border-bottom:1px solid #1e1e1e;padding:10px 16px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;z-index:1000}}
#header h1{{font-size:14px;font-weight:600;color:#fff;white-space:nowrap}}
#header h1 span{{color:#555;font-weight:400;margin-left:6px}}
#stats{{font-size:11px;color:#666;display:flex;align-items:center;gap:4px;flex-wrap:wrap}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:3px}}
#controls{{display:flex;align-items:center;gap:8px;margin-left:auto;flex-wrap:wrap}}
.filter-btn{{background:#161616;border:1px solid #2a2a2a;color:var(--c,#ccc);padding:4px 11px;border-radius:4px;cursor:pointer;font-size:11px;transition:all .15s}}
.filter-btn:hover{{background:#1e1e1e;border-color:#444}}
.filter-btn.inactive{{opacity:.35}}
#search{{background:#161616;border:1px solid #2a2a2a;color:#ccc;padding:4px 9px;border-radius:4px;font-size:11px;width:150px;outline:none}}
#search:focus{{border-color:#444}}
#map{{flex:1}}
#counter{{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.72);backdrop-filter:blur(4px);border:1px solid #2a2a2a;color:#777;padding:5px 13px;border-radius:20px;font-size:11px;z-index:1000;pointer-events:none}}
.leaflet-popup-content-wrapper{{background:#1a1a1a!important;border:1px solid #2e2e2e!important;border-radius:8px!important;box-shadow:0 8px 24px rgba(0,0,0,.7)!important;color:#e0e0e0!important}}
.leaflet-popup-tip{{background:#1a1a1a!important}}
.pu img{{width:100%;border-radius:4px;margin-bottom:7px;display:block}}
.pu h3{{font-size:12px;font-weight:600;color:#fff;margin-bottom:3px;font-family:monospace}}
.pu .meta{{font-size:11px;color:#777;margin-bottom:7px;line-height:1.6}}
.pu a{{display:inline-block;font-size:11px;color:#00aaff;text-decoration:none;padding:3px 9px;border:1px solid #00aaff33;border-radius:4px;transition:all .15s}}
.pu a:hover{{background:#00aaff18}}
.leaflet-control-zoom a{{background:#1a1a1a!important;color:#888!important;border-color:#2a2a2a!important}}
.leaflet-control-attribution{{background:rgba(0,0,0,.5)!important;color:#444!important}}
.leaflet-control-attribution a{{color:#444!important}}
</style>
</head>
<body>
<div id="header">
  <h1>🛰 Declassified Satellite <span>Available Downloads</span></h1>
  <div id="stats">{counts_html} &nbsp;|&nbsp; Updated <strong>{generated[:10]}</strong></div>
  <div id="controls">
    <input id="search" type="text" placeholder="Search entity ID…" />
    {filter_buttons}
  </div>
</div>
<div id="map"></div>
<div id="counter">{total:,} scenes</div>
<script>
const GEOJSON={geojson_str};
const DS_COLORS={ds_colors_json};
const map=L.map('map',{{center:[20,0],zoom:2,preferCanvas:true}});
L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png',{{attribution:'© CartoDB © OpenStreetMap',subdomains:'abcd',maxZoom:19}}).addTo(map);
const layers={{}};
const visible=Object.fromEntries(Object.keys(DS_COLORS).map(k=>[k,true]));
function styleFor(ds){{const c=DS_COLORS[ds]||'#fff';return{{color:c,weight:1,fillColor:c,fillOpacity:0.12}};}}
function buildLayers(q){{
  Object.values(layers).forEach(l=>{{try{{map.removeLayer(l)}}catch(e){{}}}});
  let shown=0;
  Object.keys(DS_COLORS).forEach(ds=>{{
    const feats=GEOJSON.features.filter(f=>{{
      if(f.properties.dataset!==ds)return false;
      if(q){{const lq=q.toLowerCase();return f.properties.entityId.toLowerCase().includes(lq)||(f.properties.displayId||'').toLowerCase().includes(lq);}}
      return true;
    }});
    layers[ds]=L.geoJSON({{type:'FeatureCollection',features:feats}},{{
      style:()=>styleFor(ds),
      onEachFeature:(feat,layer)=>{{
        const p=feat.properties;
        const thumb=p.thumbnail?`<img src="${{p.thumbnail}}" onerror="this.style.display='none'">`:'';
        const date=p.acquisitionDate?`<br>Date: ${{p.acquisitionDate.slice(0,10)}}`:'';
        layer.bindPopup(`<div class="pu" style="min-width:210px">${{thumb}}<h3>${{p.entityId}}</h3><div class="meta">${{p.datasetLabel}}${{date}}</div><a href="${{p.earthExplorerUrl}}" target="_blank">EarthExplorer →</a></div>`);
        layer.on('mouseover',()=>layer.setStyle({{fillOpacity:0.45}}));
        layer.on('mouseout',()=>layer.setStyle(styleFor(ds)));
      }}
    }});
    if(visible[ds]){{layers[ds].addTo(map);shown+=feats.length;}}
  }});
  document.getElementById('counter').textContent=shown.toLocaleString()+' scenes';
}}
buildLayers('');
document.querySelectorAll('.filter-btn').forEach(btn=>{{
  btn.addEventListener('click',()=>{{
    const ds=btn.dataset.ds;visible[ds]=!visible[ds];
    btn.classList.toggle('inactive',!visible[ds]);
    buildLayers(document.getElementById('search').value.trim());
  }});
}});
let t;
document.getElementById('search').addEventListener('input',e=>{{clearTimeout(t);t=setTimeout(()=>buildLayers(e.target.value.trim()),300);}});
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
        raise RuntimeError("M2M_USERNAME and M2M_TOKEN environment variables must be set")

    print("Logging in to USGS M2M API...")
    api_key = login(username, token)

    all_features = []
    try:
        for dataset, filter_id in DATASETS.items():
            label = DATASET_LABELS[dataset]
            print(f"\n  {label}...")
            scenes = search_available(api_key, dataset, filter_id)
            print(f"  Converting {len(scenes):,} scenes to GeoJSON features...")
            for scene in scenes:
                f = scene_to_feature(scene, dataset)
                if f:
                    all_features.append(f)
            print(f"  {sum(1 for f in all_features if f['properties']['dataset'] == dataset):,} features with spatial bounds")
    finally:
        logout(api_key)

    counts = {}
    for f in all_features:
        ds = f["properties"]["dataset"]
        counts[ds] = counts.get(ds, 0) + 1

    geojson = {
        "type": "FeatureCollection",
        "features": all_features,
        "metadata": {
            "generated": datetime.utcnow().isoformat() + "Z",
            "total":     len(all_features),
            "counts":    counts,
        },
    }

    print(f"\nTotal features: {len(all_features):,}")

    with open("available_scenes.geojson", "w") as f:
        json.dump(geojson, f)
    print("Saved available_scenes.geojson")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(geojson))
    print("Saved index.html")

    print(f"\nDone — {len(all_features):,} scenes mapped.")


if __name__ == "__main__":
    main()
