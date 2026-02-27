#!/bin/bash
# Run this ONCE locally to stop tracking the large files.
# After this, .gitignore will prevent them being re-added.

git rm --cached scenes.db 2>/dev/null && echo "Untracked scenes.db"
git rm --cached available_scenes.geojson 2>/dev/null && echo "Untracked available_scenes.geojson"
git commit -m "Stop tracking large generated files (now in .gitignore)"
git push
