name: Update Declassified Map

on:
  schedule:
    - cron: "0 5 * * *"   # Every day at 5am UTC
  workflow_dispatch:        # Manual trigger from GitHub Actions tab

jobs:
  update:
    runs-on: ubuntu-latest

    permissions:
      contents: write       # Required to commit the updated files

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install requests staticmap Pillow

      - name: Generate config.json from secrets
        run: |
          cat > config.json << 'CONFIGEOF'
          {
            "usgs": {
              "username": "${{ secrets.M2M_USERNAME }}",
              "token": "${{ secrets.M2M_TOKEN }}"
            },
            "database": "scenes.db",
            "notifications": {
              "telegram": {
                "enabled": false
              }
            }
          }
          CONFIGEOF

      - name: Step 1 — Run monitoring script (updates scenes.db)
        run: python monitor.py

      - name: Step 2 — Build map from scenes.db
        run: python fetch_and_build.py
        env:
          M2M_USERNAME: ${{ secrets.M2M_USERNAME }}
          M2M_TOKEN: ${{ secrets.M2M_TOKEN }}

      - name: Commit updated files
        run: |
          git config user.name  "declass-map-bot"
          git config user.email "bot@users.noreply.github.com"
          git add scenes.db index.html available_scenes.geojson
          git diff --staged --quiet || git commit -m "Daily update $(date -u +%Y-%m-%d)"
          git push
