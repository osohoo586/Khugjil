import { defineConfig } from "vitest/config";

// No inline scripts are emitted (strict CSP): the module-preload polyfill is disabled and the
// service worker is built as a separate entry served at /sw.js.
export default defineConfig({
  build: {
    target: "es2022",
    modulePreload: { polyfill: false },
    rollupOptions: {
      input: { main: "index.html", sw: "src/sw.ts" },
      output: {
        entryFileNames: (chunk) => (chunk.name === "sw" ? "sw.js" : "assets/[name]-[hash].js"),
      },
    },
  },
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  test: { environment: "jsdom", globals: true, include: ["src/**/*.test.ts"] },
});
