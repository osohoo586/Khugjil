import { defineConfig } from "@playwright/test";

// Screenshot baselines are platform-specific (fonts render differently per OS); Playwright stores
// them with a platform suffix. Regenerate with `npm run update` after an intended UI change.
export default defineConfig({
  testDir: "tests",
  fullyParallel: true,
  reporter: "list",
  expect: { toHaveScreenshot: { maxDiffPixelRatio: 0.01, animations: "disabled" } },
  use: { serviceWorkers: "block", locale: "mn-MN", timezoneId: "Asia/Ulaanbaatar" },
  webServer: [
    { command: "node serve.mjs cashier 4173", url: "http://127.0.0.1:4173/", reuseExistingServer: true },
    { command: "node serve.mjs admin 4174", url: "http://127.0.0.1:4174/login", reuseExistingServer: true },
    { command: "node serve.mjs agent 4175", url: "http://127.0.0.1:4175/overlay.html", reuseExistingServer: true },
  ],
});
