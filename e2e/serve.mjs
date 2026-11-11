// Minimal static server for screenshot tests (no dependencies).
//   node serve.mjs cashier 4173  -> cashier/dist with SPA fallback (API calls are mocked by Playwright)
//   node serve.mjs admin 4174    -> rendered admin pages + the admin /static folder
//   node serve.mjs agent 4175    -> cashier/dist-agent (desktop agent pages; Tauri IPC is faked)
import { createReadStream, existsSync, statSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";
import { fileURLToPath } from "node:url";

const here = fileURLToPath(new URL(".", import.meta.url));
const [mode, port] = process.argv.slice(2);
const TYPES = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
  ".svg": "image/svg+xml", ".woff2": "font/woff2", ".json": "application/json",
  ".webmanifest": "application/manifest+json",
};

function resolve(urlPath) {
  const clean = normalize(decodeURIComponent(urlPath.split("?")[0])).replace(/^(\.\.[/\\])+/, "");
  if (mode === "cashier") {
    const root = join(here, "..", "cashier", "dist");
    const file = join(root, clean);
    return existsSync(file) && statSync(file).isFile() ? file : join(root, "index.html");
  }
  if (mode === "agent") return join(here, "..", "cashier", "dist-agent", clean);
  if (clean.startsWith("/static/")) return join(here, "..", "server", "src", "khugjil", "admin", clean);
  return join(here, "admin-html", clean.endsWith(".html") ? clean : `${clean}.html`);
}

createServer((req, res) => {
  const file = resolve(req.url ?? "/");
  if (!existsSync(file)) {
    res.writeHead(404).end();
    return;
  }
  res.writeHead(200, { "content-type": TYPES[extname(file)] ?? "application/octet-stream" });
  createReadStream(file).pipe(res);
}).listen(Number(port), "127.0.0.1");
