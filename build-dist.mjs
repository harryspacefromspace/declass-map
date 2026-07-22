// Assembles the directory published as the Worker's static assets.
//
// Deliberately an allowlist: the repo root also holds scenes.db, monitor.py,
// config templates and the 55MB scene file, none of which should be served.
// Publishing the repo root would expose all of it.
import { mkdirSync, copyFileSync, existsSync } from 'node:fs';
import { dirname } from 'node:path';

const OUT = 'dist';
const PUBLISH = [
  'index.html',
  // Vector-tile preview: reads /data/scenes.pmtiles by range request instead
  // of pulling the whole scene file. Vendored so the page has no CDN deps.
  'tiles.html',
  'vendor/maplibre-gl.js',
  'vendor/maplibre-gl.css',
  'vendor/pmtiles.js',
  // Count labels on the zoomed-out cells. Noto Sans is OFL-licensed; only the
  // Latin range is needed, since the labels are digits.
  'vendor/fonts/NotoSans/0-255.pbf',
];

mkdirSync(OUT, { recursive: true });

let copied = 0;
for (const file of PUBLISH) {
  if (!existsSync(file)) {
    console.error(`build: missing ${file} — run fetch_and_build.py first`);
    process.exit(1);
  }
  mkdirSync(dirname(`${OUT}/${file}`), { recursive: true });
  copyFileSync(file, `${OUT}/${file}`);
  copied++;
}

console.log(`build: ${copied} file(s) -> ${OUT}/`);
