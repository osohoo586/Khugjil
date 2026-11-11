import { expect, test } from "@playwright/test";
import { acceptOwnerInvite, ADMIN, APP, confirmAndSubmit, DEMO_PIN, Shop, signIn, watch } from "./helpers";

// Real-browser flows against the full local stack (scripts/dev-up.sh: Caddy -> API -> worker).
// These catch what in-process HTTP tests cannot: Origin/Referrer behaviour of real form posts,
// __Host- cookies, HTMX dialogs, the SSE stream through the proxy. Each spec uses its own shop.
const shop = new Shop();

test.describe.configure({ mode: "serial" });
test.beforeAll(() => shop.create());

test("owner accepts the invite, sets a password and enrolls TOTP", async ({ page }) => {
  const bad: string[] = [];
  watch(page, bad);
  await acceptOwnerInvite(page, shop);
  expect(bad).toEqual([]);
});

test("owner signs in, pairs a cashier PC, sees a live payment, revokes the PC", async ({ browser }) => {
  const bad: string[] = [];
  const admin = await (await browser.newContext()).newPage();
  watch(admin, bad);
  await signIn(admin, shop);

  await admin.goto(`${ADMIN}/devices`);
  await admin.fill("input[name=device_name]", "Flow-касс");
  for (const box of await admin.locator("input[name=group_ids]").all()) await box.check();
  await admin.click("button:has-text('Код үүсгэх')");
  const code = (await admin.locator("#htmx-dialog .code").textContent())!.trim();
  expect(code).toMatch(/^[0-9A-Z]{4}-[0-9A-Z]{4}-[0-9A-Z]{4}$/);

  const cashier = await (await browser.newContext()).newPage();
  watch(cashier, bad);
  await cashier.goto(`${APP}/pair`);
  await cashier.fill("#code", code);
  await cashier.click("button[type=submit]");
  await cashier.selectOption("select", { label: "Б. Сараа" });
  await cashier.fill("input[type=password]", DEMO_PIN);
  await cashier.click("button:has-text('Ээлж эхлүүлэх')");
  await expect(cashier.locator(".status")).toHaveText("Холбогдсон");

  shop.seed("txn", "21500", `Flow ${shop.run}`);
  await expect(cashier.locator(".toast").first()).toContainText("21,500 ₮", { timeout: 20_000 });
  await expect(cashier.locator(".hero-amount")).toHaveText("21,500 ₮");
  await expect(cashier.locator("tbody tr").first()).toContainText(`Flow ${shop.run}`);

  await admin.goto(`${ADMIN}/devices`);
  const row = admin.locator("tr", { hasText: "Flow-касс" });
  await expect(row).toContainText("Б. Сараа");
  await confirmAndSubmit(admin, row.locator("button:has-text('Цуцлах')"));
  await expect(admin.locator("tr", { hasText: "Flow-касс" })).toHaveCount(0);
  await expect(cashier.locator("h1")).toHaveText("Төхөөрөмжийн эрх цуцлагдсан", { timeout: 20_000 });

  await admin.click("button:has-text('Гарах')");
  await expect(admin).toHaveURL(`${ADMIN}/login`);
  expect(bad).toEqual([]);
});
