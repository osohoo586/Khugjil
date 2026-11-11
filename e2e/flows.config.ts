import { defineConfig } from "@playwright/test";

// Browser flows against the running local stack (start it with scripts/dev-up.sh first).
// No screenshots here; the screenshot suite is playwright.config.ts.
export default defineConfig({
  testDir: "flows",
  reporter: "list",
  workers: 1,
  timeout: 120_000,
  use: {
    ignoreHTTPSErrors: true, // Caddy's local CA
    locale: "mn-MN",
    timezoneId: "Asia/Ulaanbaatar",
    serviceWorkers: "block",
    trace: "retain-on-failure",
    launchOptions: { args: ["--host-resolver-rules=MAP *.khugjil.localhost 127.0.0.1"] },
  },
});
