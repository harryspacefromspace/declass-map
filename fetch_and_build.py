#!/usr/bin/env python3
"""
fetch_and_build.py
Queries USGS M2M API for all DECLASSI/II/III scenes with downloads available,
then generates a self-contained map.html with the data baked in.
"""

import os
import json
import time
import requests
from datetime import datetime

M2M_URL = "https://m2m.cr.usgs.gov/api/api/json/stable/"

# M2M internal dataset names — confirmed working from the USGS monitoring project.
# EarthExplorer display names like "DECLASSI" do NOT work in the API.
DATASETS = [
    "corona2",               # Declass I  — CORONA / ARGON / LANYARD (KH-1 to KH-6, 1960–1972)
    "5e839ff7d71d4811",      # Declass II — GAMBIT / HEXAGON mapping camera (KH-7 + KH-9, 1966–1984)
    "5e7c41f3ffaaf662",      # Declass III — HEXAGON (KH-9, 1970s–1980s)
]

DATASET_LABELS = {
    "corona2":          "Declass I — CORONA/ARGON/LANYARD",
    "5e839ff7d71d4811": "Declass II — GAMBIT/HEXAGON",
    "5e7c41f3ffaaf662": "Declass III — HEXAGON",
}

DATASET_COLORS = {
    "corona2":          "#00ff88",
    "5e839ff7d71d4811": "#00aaff",
    "5e7c41f3ffaaf662": "#ff9900",
}


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
    requests.post(M2M_URL + "logout", headers={"X-Auth-Token": api_key}, timeout=10)
    print("  Logged out")


def search_scenes(api_key, dataset, starting_number=1, max_results=50000):
    """Search for all scenes in a dataset, paginating as needed."""
    payload = {
        "datasetName": dataset,
        "maxResults": min(max_results, 50000),
        "startingNumber": starting_number,
        "sceneFilter": {},
    }
    resp = requests.post(
        M2M_URL + "scene-search",
        json=payload,
        headers={"X-Auth-Token": api_key},
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errorCode"):
        raise RuntimeError(f"scene-search error: {data['errorMessage']}")
    return data["data"]


def get_download_options(api_key, dataset, entity_ids):
    """Check which scenes in a batch are available for download."""
    payload = {
        "datasetName": dataset,
        "entityIds": entity_ids,
    }
    resp = requests.post(
        M2M_URL + "download-options",
        json=payload,
        headers={"X-Auth-Token": api_key},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("errorCode"):
        print(f"    download-options error: {data['errorMessage']}")
        return {}
    
    # Build map of entityId -> available
    result = {}
    for item in (data.get("data") or []):
        eid = item.get("entityId")
        if eid and item.get("available"):
            result[eid] = True
    return result


def fetch_dataset(api_key, dataset):
    """Fetch all downloadable scenes with spatial bounds for one dataset."""
    print(f"\n  Dataset: {dataset}")
    
    all_scenes = []
    starting = 1
    batch_size = 5000
    
    while True:
        print(f"    Searching scenes {starting}–{starting + batch_size - 1}...")
        result = search_scenes(api_key, dataset, starting_number=starting, max_results=batch_size)
        scenes = result.get("results", [])
        total = result.get("totalHits", 0)
        
        if not scenes:
            break
        
        # Filter to scenes that have spatial data
        scenes_with_bounds = [s for s in scenes if s.get("spatialBounds") or s.get("spatialCoverage")]
        
        # Check download availability in batches of 250
        entity_ids = [s["entityId"] for s in scenes_with_bounds]
        available_set = {}
        for i in range(0, len(entity_ids), 250):
            chunk = entity_ids[i:i + 250]
            available_set.update(get_download_options(api_key, dataset, chunk))
            time.sleep(0.5)  # be polite to the API
        
        for scene in scenes_with_bounds:
            eid = scene["entityId"]
            if eid in available_set:
                all_scenes.append(scene)
        
        print(f"    Batch: {len(scenes)} scenes, {len(available_set)} available for download")
        
        if starting + batch_size > total:
            break
        starting += batch_size
        time.sleep(1)
    
    print(f"    Total available: {len(all_scenes)}")
    return all_scenes


def scene_to_feature(scene, dataset):
    """Convert a M2M scene result to a GeoJSON feature."""
    # Get geometry - prefer spatialBounds (polygon) over simple bbox
    geom = scene.get("spatialBounds") or scene.get("spatialCoverage")
    if not geom:
        return None
    
    # Normalise geometry - M2M returns it as a GeoJSON-compatible dict
    if isinstance(geom, dict) and "type" in geom:
        geometry = geom
    else:
        return None
    
    props = {
        "entityId": scene.get("entityId", ""),
        "dataset": dataset,
        "datasetLabel": DATASET_LABELS.get(dataset, dataset),
        "displayId": scene.get("displayId", ""),
        "acquisitionDate": scene.get("temporalCoverage", {}).get("startDate", "") if isinstance(scene.get("temporalCoverage"), dict) else scene.get("acquisitionDate", ""),
        "thumbnail": scene.get("browse", [{}])[0].get("thumbnailPath", "") if scene.get("browse") else "",
        "color": DATASET_COLORS.get(dataset, "#ffffff"),
        "earthExplorerUrl": f"https://earthexplorer.usgs.gov/scene/metadata/full/{dataset}/{scene.get('entityId', '')}/",
    }
    
    return {"type": "Feature", "geometry": geometry, "properties": props}


def build_geojson(features_by_dataset):
    features = []
    for dataset, scenes in features_by_dataset.items():
        for scene in scenes:
            f = scene_to_feature(scene, dataset)
            if f:
                features.append(f)
    
    return {
        "type": "FeatureCollection",
        "features": features,
        "metadata": {
            "generated": datetime.utcnow().isoformat() + "Z",
            "total": len(features),
            "counts": {ds: sum(1 for f in features if f["properties"]["dataset"] == ds) for ds in features_by_dataset},
        }
    }


def build_html(geojson):
    """Generate a self-contained HTML file with the map and data baked in."""
    geojson_str = json.dumps(geojson)
    generated = geojson["metadata"]["generated"]
    total = geojson["metadata"]["total"]
    counts = geojson["metadata"]["counts"]
    
    counts_html = " &nbsp;|&nbsp; ".join(
        f'<span class="legend-dot" style="background:{DATASET_COLORS[ds]}"></span>'
        f'{DATASET_LABELS[ds].split("(")[0].strip()}: <strong>{counts.get(ds, 0):,}</strong>'
        for ds in DATASET_LABELS
    )
    
    # Generate filter buttons dynamically from discovered datasets
    filter_buttons = "\n    ".join(
        f'<button class="filter-btn active" data-ds="{ds}" style="--btn-color:{DATASET_COLORS[ds]}">'
        f'{DATASET_LABELS[ds].split("(")[0].strip()}</button>'
        for ds in DATASET_LABELS
    )
    
    # Colors dict for JS
    ds_colors_json = json.dumps(DATASET_COLORS)
    
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Declassified Satellite — Available Downloads</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ background: #0d0d0d; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; height: 100vh; display: flex; flex-direction: column; }}
  
  #header {{
    background: #111;
    border-bottom: 1px solid #222;
    padding: 12px 20px;
    display: flex;
    align-items: center;
    gap: 20px;
    flex-wrap: wrap;
    z-index: 1000;
  }}
  
  #header h1 {{
    font-size: 15px;
    font-weight: 600;
    color: #fff;
    white-space: nowrap;
  }}
  
  #header h1 span {{
    color: #888;
    font-weight: 400;
    margin-left: 8px;
    font-size: 12px;
  }}
  
  #stats {{
    font-size: 12px;
    color: #888;
    display: flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
  }}
  
  .legend-dot {{
    display: inline-block;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    margin-right: 4px;
  }}
  
  #controls {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin-left: auto;
    flex-wrap: wrap;
  }}
  
  .filter-btn {{
    background: #1a1a1a;
    border: 1px solid #333;
    color: #ccc;
    padding: 5px 12px;
    border-radius: 4px;
    cursor: pointer;
    font-size: 12px;
    transition: all 0.15s;
  }}
  
  .filter-btn {{ color: var(--btn-color, #ccc); }}
  .filter-btn:hover {{ background: #222; border-color: #555; }}
  .filter-btn.inactive {{ opacity: 0.4; }}
  
  #search-box {{
    background: #1a1a1a;
    border: 1px solid #333;
    color: #ccc;
    padding: 5px 10px;
    border-radius: 4px;
    font-size: 12px;
    width: 160px;
    outline: none;
  }}
  
  #search-box:focus {{ border-color: #555; }}
  
  #map {{ flex: 1; }}
  
  #scene-count {{
    position: absolute;
    bottom: 16px;
    left: 50%;
    transform: translateX(-50%);
    background: rgba(0,0,0,0.75);
    backdrop-filter: blur(4px);
    border: 1px solid #333;
    color: #aaa;
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 12px;
    z-index: 1000;
    pointer-events: none;
  }}
  
  /* Leaflet popup customisation */
  .leaflet-popup-content-wrapper {{
    background: #1a1a1a !important;
    border: 1px solid #333 !important;
    border-radius: 8px !important;
    box-shadow: 0 8px 24px rgba(0,0,0,0.6) !important;
    color: #e0e0e0 !important;
  }}
  
  .leaflet-popup-tip {{ background: #1a1a1a !important; }}
  
  .popup-inner img {{
    width: 100%;
    border-radius: 4px;
    margin-bottom: 8px;
    display: block;
  }}
  
  .popup-inner h3 {{
    font-size: 13px;
    font-weight: 600;
    color: #fff;
    margin-bottom: 4px;
    font-family: monospace;
  }}
  
  .popup-inner .meta {{
    font-size: 11px;
    color: #888;
    margin-bottom: 8px;
    line-height: 1.6;
  }}
  
  .popup-inner a {{
    display: inline-block;
    font-size: 11px;
    color: #00aaff;
    text-decoration: none;
    padding: 4px 10px;
    border: 1px solid #00aaff44;
    border-radius: 4px;
    transition: all 0.15s;
  }}
  
  .popup-inner a:hover {{ background: #00aaff22; }}
  
  .leaflet-control-zoom a {{
    background: #1a1a1a !important;
    color: #ccc !important;
    border-color: #333 !important;
  }}
  
  .leaflet-control-attribution {{
    background: rgba(0,0,0,0.6) !important;
    color: #555 !important;
  }}
  
  .leaflet-control-attribution a {{ color: #555 !important; }}
</style>
</head>
<body>

<div id="header">
  <h1>🛰 Declassified Satellite <span>Available Downloads</span></h1>
  <div id="stats">{counts_html} &nbsp;|&nbsp; Updated: <strong>{generated[:10]}</strong></div>
  <div id="controls">
    <input id="search-box" type="text" placeholder="Search entity ID…" />
{filter_buttons}
  </div>
</div>

<div id="map"></div>
<div id="scene-count">{total:,} scenes shown</div>

<script>
const GEOJSON = {geojson_str};

const map = L.map('map', {{
  center: [20, 0],
  zoom: 2,
  zoomControl: true,
  preferCanvas: true
}});

L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
  attribution: '© CartoDB © OpenStreetMap',
  subdomains: 'abcd',
  maxZoom: 19
}}).addTo(map);

// Track layers per dataset
const layers = {{}};
const DS_COLORS = {ds_colors_json};
const visible = Object.fromEntries(Object.keys(DS_COLORS).map(k => [k, true]));

function styleFor(ds) {{
  const c = DS_COLORS[ds] || '#fff';
  return {{ color: c, weight: 1, fillOpacity: 0.12, fillColor: c }};
}}

function buildLayers(filter) {{
  // Remove existing
  Object.values(layers).forEach(l => {{ try {{ map.removeLayer(l); }} catch(e) {{}} }});
  
  let shown = 0;
  
  Object.keys(DS_COLORS).forEach(ds => {{
    const features = GEOJSON.features.filter(f => {{
      if (f.properties.dataset !== ds) return false;
      if (filter) {{
        const q = filter.toLowerCase();
        return f.properties.entityId.toLowerCase().includes(q) ||
               f.properties.displayId.toLowerCase().includes(q);
      }}
      return true;
    }});
    
    layers[ds] = L.geoJSON({{ type: 'FeatureCollection', features }}, {{
      style: () => styleFor(ds),
      onEachFeature: (feature, layer) => {{
        const p = feature.properties;
        const thumb = p.thumbnail ? `<img src="${{p.thumbnail}}" onerror="this.style.display='none'">` : '';
        const date = p.acquisitionDate ? `<br>Date: ${{p.acquisitionDate.slice(0,10)}}` : '';
        layer.bindPopup(`
          <div class="popup-inner" style="min-width:220px">
            ${{thumb}}
            <h3>${{p.entityId}}</h3>
            <div class="meta">${{p.datasetLabel}}${{date}}</div>
            <a href="${{p.earthExplorerUrl}}" target="_blank">View on EarthExplorer →</a>
          </div>
        `);
        layer.on('mouseover', () => layer.setStyle({{ fillOpacity: 0.45 }}));
        layer.on('mouseout', () => layer.setStyle(styleFor(ds)));
      }}
    }});
    
    if (visible[ds]) {{
      layers[ds].addTo(map);
      shown += features.length;
    }}
  }});
  
  document.getElementById('scene-count').textContent = shown.toLocaleString() + ' scenes shown';
}}

buildLayers('');

// Filter toggle buttons
document.querySelectorAll('.filter-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    const ds = btn.dataset.ds;
    visible[ds] = !visible[ds];
    btn.classList.toggle('inactive', !visible[ds]);
    buildLayers(document.getElementById('search-box').value.trim());
  }});
}});

// Search
let searchTimer;
document.getElementById('search-box').addEventListener('input', e => {{
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => buildLayers(e.target.value.trim()), 300);
}});

</script>
</body>
</html>
"""
    return html


def main():
    username = os.environ.get("M2M_USERNAME")
    token = os.environ.get("M2M_TOKEN")
    
    if not username or not token:
        raise RuntimeError("M2M_USERNAME and M2M_TOKEN environment variables must be set")
    
    print("Logging in to USGS M2M API...")
    api_key = login(username, token)
    
    try:
        features_by_dataset = {}
        for dataset in DATASETS:
            features_by_dataset[dataset] = fetch_dataset(api_key, dataset)
    finally:
        logout(api_key)
    
    print("\nBuilding GeoJSON...")
    geojson = build_geojson(features_by_dataset)
    total = geojson["metadata"]["total"]
    print(f"  Total features: {total:,}")
    
    # Save GeoJSON separately (useful for debugging / other uses)
    with open("available_scenes.geojson", "w") as f:
        json.dump(geojson, f)
    print("  Saved available_scenes.geojson")
    
    # Build and save self-contained HTML
    html = build_html(geojson)
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print("  Saved index.html")
    
    print(f"\nDone. {total:,} available scenes mapped.")


if __name__ == "__main__":
    main()
