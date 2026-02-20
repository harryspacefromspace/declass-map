# Declassified Satellite — Private Download Map

A private Leaflet map showing all USGS declassified satellite scenes currently available for download across DECLASSI, DECLASSII, and DECLASSIII. Updates automatically every Monday.

## What it does

- Queries the USGS M2M API for all scenes in all three declass datasets
- Filters to only scenes with downloads **currently available**
- Generates a self-contained `index.html` with the data baked in
- Also saves raw `available_scenes.geojson` for other uses

## Map features

- Dark Leaflet map with footprint polygons colour-coded by dataset
- Click any footprint for a popup with thumbnail + EarthExplorer link
- Toggle datasets on/off with the buttons in the header
- Search by entity ID or display ID

## Setup (one-time)

### 1. Create this repo as **Private** on GitHub

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**

| Secret name | Value |
|-------------|-------|
| `M2M_USERNAME` | Your USGS ERS username |
| `M2M_TOKEN` | Your M2M API token from [ers.cr.usgs.gov](https://ers.cr.usgs.gov) |

### 3. Enable GitHub Pages (to view the map in your browser)

Go to **Settings → Pages** and set:
- Source: **Deploy from a branch**
- Branch: `main` / `root`

This requires a **GitHub Pro** account (~$4/mo). The map will be accessible at:
```
https://YOUR-USERNAME.github.io/YOUR-REPO-NAME/
```

The URL is not publicly listed anywhere — it's private by obscurity. If you want actual login protection, see "Advanced: Password protection" below.

> **No GitHub Pro?** You can still use this — just clone the repo locally and open `index.html` in your browser after each weekly run.

### 4. Run manually the first time

Go to **Actions → Update Declassified Map → Run workflow**

This will take several minutes (it's querying potentially tens of thousands of scenes). Once it finishes, `index.html` and `available_scenes.geojson` will appear in your repo.

## Schedule

Runs every **Monday at 5:00 AM UTC**. You can also trigger it manually from the Actions tab at any time.

## Advanced: Password protection

If you want the GitHub Pages URL to actually require a password, you can add a simple login screen. Ask Claude to add an `AUTH_PASSWORD` secret and a login overlay to the HTML.

## Files

| File | Purpose |
|------|---------|
| `fetch_and_build.py` | Main script — queries M2M, generates HTML |
| `index.html` | Generated self-contained map (committed by the Action) |
| `available_scenes.geojson` | Generated raw data (committed by the Action) |
| `.github/workflows/weekly-update.yml` | Scheduled Action |
