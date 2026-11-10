import { expect, test } from "@playwright/test";

const BASE = "http://127.0.0.1:4174";
const PAGES = ["overview", "members", "devices", "pair", "email", "history", "login"];

for (const scheme of ["light", "dark"] as const) {
  test.describe(`admin (${scheme})`, () => {
    test.use({ colorScheme: scheme, viewport: { width: 1280, height: 900 } });
    for (const name of PAGES) {
      test(name, async ({ page }) => {
        await page.clock.install({ time: new Date("2026-10-15T06:32:00Z") });
        await page.goto(`${BASE}/${name}`);
        await page.evaluate(() => document.fonts.ready);
        await expect(page).toHaveScreenshot(`admin-${name}-${scheme}.png`, { fullPage: true });
      });
    }
  });
}

test("admin pages carry no inline scripts", async ({ page }) => {
  for (const name of PAGES) {
    await page.goto(`${BASE}/${name}`);
    const inline = await page.locator("script:not([src])").count();
    expect(inline, name).toBe(0);
  }
});
