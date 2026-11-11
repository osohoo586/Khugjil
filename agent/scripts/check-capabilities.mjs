// CI guard for the agent's attack surface (PLAN-PHASE2 §4, §5 E3): fails when the Tauri config or
// capabilities are loosened (new permissions, remote URLs, global Tauri object, risky plugins).
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../src-tauri/", import.meta.url));
const errors = [];
const fail = (msg) => errors.push(msg);

const conf = JSON.parse(readFileSync(join(root, "tauri.conf.json"), "utf8"));
const app = conf.app ?? {};
if (app.withGlobalTauri !== false) fail("app.withGlobalTauri must be false");
if ((app.windows ?? []).length !== 0) fail("windows are created in Rust (navigation guards); app.windows must be empty");
const sec = app.security ?? {};
const csp = sec.csp ?? {};
const expectCsp = {
  "default-src": "'none'",
  "script-src": "'self'",
  "connect-src": "ipc: http://ipc.localhost",
  "base-uri": "'none'",
  "form-action": "'none'",
  "frame-ancestors": "'none'",
  "require-trusted-types-for": "'script'",
};
for (const [k, v] of Object.entries(expectCsp)) if (csp[k] !== v) fail(`csp ${k} must be ${v}`);
if (Object.values(csp).some((v) => /unsafe-|https?:\/\/(?!ipc\.localhost)|\*/.test(v))) fail("csp must not allow unsafe-*, remote origins or wildcards");
for (const key of ["dangerousDisableAssetCspModification", "dangerousRemoteDomainIpcAccess", "assetProtocol"]) {
  if (key in sec) fail(`security.${key} must not be set`);
}
if (sec.freezePrototype !== true) fail("security.freezePrototype must be true");
if (conf.build?.devUrl) fail("build.devUrl must not be set (the webviews load bundled assets only)");
if (conf.bundle?.windows?.nsis?.installMode !== "currentUser") fail("NSIS installMode must be currentUser (no admin rights)");
const endpoints = conf.plugins?.updater?.endpoints ?? [];
if (endpoints.length === 0 || endpoints.some((e) => !e.startsWith("https://app.khugjil.usukh.shop/agent/"))) {
  fail("updater endpoints must be https://app.khugjil.usukh.shop/agent/…");
}

const ALLOWED = {
  main: [
    "core:event:allow-listen", "core:event:allow-unlisten", "allow-api-request", "allow-stream-start",
    "allow-stream-stop", "allow-overlay-show", "allow-overlay-config", "allow-overlay-clear", "allow-attention",
  ],
  overlay: ["core:event:allow-listen", "core:event:allow-unlisten", "allow-overlay-ready", "allow-overlay-layout"],
};
const capDir = join(root, "capabilities");
const seen = new Set();
for (const file of readdirSync(capDir).filter((f) => f.endsWith(".json"))) {
  const cap = JSON.parse(readFileSync(join(capDir, file), "utf8"));
  const allowed = ALLOWED[cap.identifier];
  if (!allowed) {
    fail(`${file}: unknown capability ${cap.identifier}`);
    continue;
  }
  seen.add(cap.identifier);
  if ("remote" in cap) fail(`${file}: remote URLs must never get IPC access`);
  if (JSON.stringify(cap.windows) !== JSON.stringify([cap.identifier])) fail(`${file}: must apply to window "${cap.identifier}" only`);
  if ("webviews" in cap || "platforms" in cap) fail(`${file}: unexpected webviews/platforms scope`);
  for (const p of cap.permissions ?? []) {
    const id = typeof p === "string" ? p : p.identifier;
    if (!allowed.includes(id)) fail(`${file}: permission ${id} is not on the allowlist`);
  }
}
for (const id of Object.keys(ALLOWED)) if (!seen.has(id)) fail(`capability ${id} missing`);

const cargo = readFileSync(join(root, "Cargo.toml"), "utf8");
for (const plugin of ["shell", "fs", "http", "opener", "dialog", "clipboard-manager", "process", "os", "websocket", "sql", "store"]) {
  if (new RegExp(`^tauri-plugin-${plugin}\\s*=`, "m").test(cargo)) fail(`tauri-plugin-${plugin} must not be used`);
}
if (/features\s*=\s*\[[^\]]*"devtools"/.test(cargo)) fail("the devtools feature must stay off");

if (errors.length) {
  console.error(errors.map((e) => `capability check: ${e}`).join("\n"));
  process.exit(1);
}
console.log("capability check: ok");
