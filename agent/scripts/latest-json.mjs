// Writes the updater manifest (latest.json) for the NSIS build in the given directory.
// Usage: node agent/scripts/latest-json.mjs <bundle/nsis dir> > latest.json
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

const dir = process.argv[2];
const conf = JSON.parse(readFileSync(new URL("../src-tauri/tauri.conf.json", import.meta.url), "utf8"));
const installer = readdirSync(dir).find((f) => f.endsWith("-setup.exe"));
if (!installer) throw new Error("no *-setup.exe in " + dir);
const signature = readFileSync(join(dir, `${installer}.sig`), "utf8").trim();
const base = conf.plugins.updater.endpoints[0].replace(/latest\.json$/, "");
const manifest = {
  version: conf.version,
  notes: `Хугжил касс програм ${conf.version}`,
  pub_date: new Date().toISOString(),
  platforms: {
    "windows-x86_64": { signature, url: base + encodeURIComponent(installer) },
  },
};
process.stdout.write(JSON.stringify(manifest, null, 2) + "\n");
