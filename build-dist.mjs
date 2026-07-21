// Assembles the directory Cloudflare Pages publishes.
//
// Deliberately an allowlist: the repo root also holds scenes.db, monitor.py,
// config templates and the 55MB scene file, none of which should be served.
// Publishing the repo root would expose all of it.
import { mkdirSync, copyFileSync, existsSync, writeFileSync } from 'node:fs';

const OUT = 'dist';
const PUBLISH = ['index.html'];

mkdirSync(OUT, { recursive: true });

let copied = 0;
for (const file of PUBLISH) {
  if (!existsSync(file)) {
    console.error(`build: missing ${file} — run fetch_and_build.py first`);
    process.exit(1);
  }
  copyFileSync(file, `${OUT}/${file}`);
  copied++;
}

// Keep Pages' asset router away from /data/* so the Function always handles it.
writeFileSync(`${OUT}/_routes.json`, JSON.stringify({
  version: 1,
  include: ['/data/*'],
  exclude: []
}, null, 2));

console.log(`build: ${copied} file(s) -> ${OUT}/`);
