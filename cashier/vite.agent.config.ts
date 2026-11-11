import { defineConfig } from "vite";

// Desktop agent build (PLAN-PHASE2 §3): the same screens with the agent platform, plus the overlay
// window. Output goes to dist-agent/, which the Tauri app bundles. No service worker, no Web Push.
export default defineConfig({
  build: {
    outDir: "dist-agent",
    target: "es2022",
    modulePreload: { polyfill: false },
    rollupOptions: { input: { main: "agent.html", overlay: "overlay.html" } },
  },
});
