#!/usr/bin/env python3
"""
fetch_and_build.py

Reads available scene entity IDs from scenes.db (maintained by the monitoring
workflow), fetches their spatial footprints from the USGS M2M API, and builds
a self-contained index.html map.

This is fast because we only query metadata for scenes already known to be
available — typically a few thousand — rather than scanning the full archive.
"""

import os
import json
import time
import sqlite3
import requests
from datetime import datetime

M2M_URL = "https://m2m.cr.usgs.gov/api/api/json/stable/"
DB_PATH  = "scenes.db"

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


def fetch_scene_metadata_batch(api_key, dataset, entity_ids):
    """
    Fetch spatial bounds and metadata for a list of entity IDs.
    Uses scene-list-add / scene-list-get to look up specific scenes by ID.
    Returns a list of scene result dicts.
    """
    list_name = f"mapbuild_{int(time.time())}"

    # Add scenes to a temporary named list
    add_resp = requests.post(
        M2M_URL + "scene-list-add",
        json={
            "listId":      list_name,
            "datasetName": dataset,
            "entityIds":   entity_ids,
        },
        headers={"X-Auth-Token": api_key},
        timeout=60,
    )
    add_resp.raise_for_status()
    add_data = add_resp.json()
    if add_data.get("errorCode"):
        print(f"    scene-list-add error: {add_data['errorMessage']}")
        return []

    # Retrieve the scenes (includes spatialBounds)
    get_resp = requests.post(
        M2M_URL + "scene-list-get",
        json={
            "listId":      list_name,
            "datasetName": dataset,
        },
        headers={"X-Auth-Token": api_key},
        timeout=120,
    )
    get_resp.raise_for_status()
    get_data = get_resp.json()
    if get_data.get("errorCode"):
        print(f"    scene-list-get error: {get_data['errorMessage']}")
        return []

    results = get_data.get("data", {})
    if isinstance(results, dict):
        results = results.get("results", [])

    # Clean up the temporary list
    try:
        requests.post(
            M2M_URL + "scene-list-remove",
            json={"listId": list_name},
            headers={"X-Auth-Token": api_key},
            timeout=10,
        )
    except Exception:
        pass

    return results or []


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def load_scenes_from_db(db_path):
    """
    Read all available scenes from scenes.db.
    Returns: { dataset_name: [ {entity_id, acquisition_date}, ... ] }
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(
            f"scenes.db not found at '{db_path}'. "
            "The monitoring workflow must run at least once before the map can be built."
        )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("PRAGMA table_info(scenes)")
    cols = {row["name"] for row in cur.fetchall()}
    print(f"  scenes.db columns: {sorted(cols)}")

    date_col = "acquisition_date" if "acquisition_date" in cols else None

    query = "SELECT entity_id, dataset"
    if date_col:
        query += f", {date_col}"
    query += " FROM scenes"

    cur.execute(query)
    rows = cur.fetchall()
    conn.close()

    by_dataset = {}
    for row in rows:
        ds = row["dataset"]
        by_dataset.setdefault(ds, []).append({
            "entity_id":       row["entity_id"],
            "acquisition_date": row[date_col] if date_col else "",
        })

    for ds, scenes in by_dataset.items():
        print(f"  {DATASET_LABELS.get(ds, ds)}: {len(scenes):,} scenes in db")

    return by_dataset


# ---------------------------------------------------------------------------
# GeoJSON conversion
# ---------------------------------------------------------------------------

def scene_to_feature(scene, dataset, db_entry):
    geom = scene.get("spatialBounds") or scene.get("spatialCoverage")
    if not geom or not isinstance(geom, dict) or "type" not in geom:
        return None

    entity_id = scene.get("entityId", db_entry.get("entity_id", ""))

    acq = ""
    tc = scene.get("temporalCoverage")
    if isinstance(tc, dict):
        acq = tc.get("startDate", "")
    if not acq:
        acq = scene.get("acquisitionDate", "") or db_entry.get("acquisition_date", "")

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


def fetch_footprints(api_key, dataset, db_scenes):
    """Fetch footprints for all scenes in a dataset, in batches of 250."""
    entity_ids = [s["entity_id"] for s in db_scenes]
    db_lookup  = {s["entity_id"]: s for s in db_scenes}

    features   = []
    batch_size = 250

    for i in range(0, len(entity_ids), batch_size):
        batch = entity_ids[i:i + batch_size]
        print(f"    Fetching {i + 1}–{min(i + batch_size, len(entity_ids))} of {len(entity_ids)}...")

        scenes = fetch_scene_metadata_batch(api_key, dataset, batch)
        for scene in scenes:
            eid = scene.get("entityId", "")
            f = scene_to_feature(scene, dataset, db_lookup.get(eid, {}))
            if f:
                features.append(f)

        time.sleep(0.3)

    print(f"    Footprints retrieved: {len(features)} of {len(entity_ids)}")
    return features


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
        f'<span class="dot" style="background:{DATASET_COLORS.get(ds,"#fff")}"></span>'
        f'{DATASET_LABELS.get(ds,ds).split("—")[0].strip()}: '
        f'<strong>{counts.get(ds,0):,}</strong>'
        for ds in DATASET_LABELS if ds in counts
    )

    filter_buttons = "\n    ".join(
        f'<button class="filter-btn active" data-ds="{ds}" '
        f'style="--c:{DATASET_COLORS.get(ds,"#fff")}">'
        f'{DATASET_LABELS.get(ds,ds).split("—")[0].strip()}</button>'
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
    const ds=btn.dataset.ds;
    visible[ds]=!visible[ds];
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

    print(f"Reading available scenes from {DB_PATH}...")
    by_dataset = load_scenes_from_db(DB_PATH)
    if not by_dataset:
        raise RuntimeError("scenes.db is empty — run the monitoring workflow first")

    print("\nLogging in to USGS M2M API...")
    api_key = login(username, token)

    all_features = []
    try:
        for dataset, db_scenes in by_dataset.items():
            label = DATASET_LABELS.get(dataset, dataset)
            print(f"\n  {label} ({len(db_scenes):,} scenes)...")
            all_features.extend(fetch_footprints(api_key, dataset, db_scenes))
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

    with open("available_scenes.geojson", "w") as f:
        json.dump(geojson, f)
    print("\nSaved available_scenes.geojson")

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_html(geojson))
    print("Saved index.html")

    print(f"\nDone — {len(all_features):,} scenes mapped.")


if __name__ == "__main__":
    main()
