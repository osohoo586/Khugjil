import { readFileSync } from "node:fs";
import { expect, test, type Page } from "@playwright/test";
import { acceptOwnerInvite, ADMIN, APP, confirmAndSubmit, PASSWORD, Shop, signIn, watch } from "./helpers";

// Every admin form, submitted the way a real browser submits it (native form posts, HTMX, the
// confirm dialog, step-up), plus the cashier side of a staff invite. A 403 or 5xx anywhere fails.
const shop = new Shop();
const bad: string[] = [];
let page: Page;

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ browser }) => {
  shop.create();
  page = await (await browser.newContext({ acceptDownloads: true })).newPage();
  watch(page, bad);
  await acceptOwnerInvite(page, shop);
  await signIn(page, shop);
});

test.afterAll(() => expect(bad).toEqual([]));

test("accounts: add, rename, deactivate and reactivate", async () => {
  await page.goto(`${ADMIN}/accounts`);
  await page.fill("input[name=number]", "4444444444"); // fake, like the test fixtures
  await page.fill("form[action='/accounts'] input[name=alias]", "Flow данс");
  await page.click("button:has-text('Нэмэх')");
  const row = page.locator("tr", { has: page.locator(".inline-edit input[value='Flow данс']") });
  await expect(row).toContainText("4444");
  await expect(row).not.toContainText("4444444444"); // the full number is never shown

  await row.locator(".inline-edit input[name=alias]").fill("Flow данс 2");
  await row.locator("button:has-text('Хадгалах')").click();
  const renamed = page.locator("tr", { has: page.locator(".inline-edit input[value='Flow данс 2']") });
  await expect(renamed).toHaveCount(1);

  await confirmAndSubmit(page, renamed.locator("button:has-text('Идэвхгүй болгох')"));
  await expect(renamed.locator("button:has-text('Идэвхжүүлэх')")).toHaveCount(1);
  await confirmAndSubmit(page, renamed.locator("button:has-text('Идэвхжүүлэх')"));
  await expect(renamed.locator("button:has-text('Идэвхгүй болгох')")).toHaveCount(1);
});

test("groups: create a group and link the account", async () => {
  await page.goto(`${ADMIN}/groups`);
  await page.fill("form[action='/groups'] input[name=name]", "Flow бүлэг");
  await page.click("button:has-text('Үүсгэх')");
  if (!/\/groups\/[0-9a-f-]{36}$/.test(page.url())) {
    await page.locator("tr", { hasText: "Flow бүлэг" }).locator("a:has-text('Засах')").click();
  }
  await expect(page.locator("main h1")).toHaveText("Flow бүлэг");
  await page.locator("label", { hasText: "Flow данс 2" }).locator("input[name=account_ids]").check();
  await page.click("button:has-text('Хадгалах')");
  await page.goto(`${ADMIN}/groups`);
  await expect(page.locator("tr", { hasText: "Flow бүлэг" }).locator("td").nth(2)).toHaveText("1"); // accounts
});

test("members: invite a cashier, who sets a PIN on the cashier site", async ({ browser }) => {
  await page.goto(`${ADMIN}/members`);
  await page.fill("form[action='/members'] input[name=display_name]", "Flow кассчин");
  await page.selectOption("form[action='/members'] select[name=role]", "cashier");
  await page.locator("form[action='/members'] label", { hasText: "Flow бүлэг" }).locator("input").check();
  await page.click("button:has-text('Урилга үүсгэх')");
  const link = (await page.locator("#invite-link").textContent())!.trim();
  expect(link.startsWith(`${APP}/invite#t=`)).toBe(true);

  const cashier = await (await browser.newContext()).newPage();
  watch(cashier, bad);
  await cashier.goto(link);
  await cashier.check("#agree");
  await cashier.fill("#pin1", "583920");
  await cashier.fill("#pin2", "583920");
  await cashier.click("button:has-text('Бүртгүүлэх')");
  await expect(cashier.locator(".notice")).toContainText("Бүртгэл амжилттай боллоо");
  await cashier.context().close();

  await page.goto(`${ADMIN}/members`);
  const row = page.locator("tr", { hasText: "Flow кассчин" });
  await expect(row).toHaveCount(1);
  await expect(row).not.toContainText("Урилга");
});

test("settings: change the toast duration", async () => {
  await page.goto(`${ADMIN}/settings`);
  await page.fill("input[name=toast_seconds]", "12");
  await page.click("button:has-text('Хадгалах')");
  await page.goto(`${ADMIN}/settings`);
  await expect(page.locator("input[name=toast_seconds]")).toHaveValue("12");
});

test("history: filter by account and export CSV", async () => {
  await page.goto(`${ADMIN}/history`);
  await expect(page.locator("tbody tr").first()).toBeVisible();
  await page.selectOption("select[name=account]", { label: "Салбар-2" });
  await page.click("button:has-text('Шүүх')");
  const rows = page.locator("tbody tr");
  expect(await rows.count()).toBeGreaterThan(0);
  for (const text of await rows.allTextContents()) expect(text).toContain("Салбар-2");

  const [download] = await Promise.all([page.waitForEvent("download"), page.click("a[href^='/history.csv']")]);
  const csv = readFileSync((await download.path())!, "utf8");
  expect(csv.split("\n").length).toBeGreaterThan(2);
  expect(csv).not.toContain("TESTPAYERMARKER"); // payer names never stored, so never exported
});

test("email: test notification, a second address, replacing and retiring (step-up when asked)", async () => {
  await page.goto(`${ADMIN}/email`);
  await page.click("button:has-text('Тест илгээх')");
  await expect(page).toHaveURL(new RegExp(`${ADMIN}/email`));
  const first = (await page.locator("#inbound-1").textContent())!.trim();
  const addresses = page.locator("section[aria-labelledby='addresses-title'] tbody tr");

  // Adding, replacing and retiring need a fresh TOTP; after it the owner is back on /email.
  async function withStepUp(act: () => Promise<void>): Promise<void> {
    await act();
    if (page.url().includes("/stepup")) {
      await expect(page.locator("h1")).toHaveText("Дахин баталгаажуулна уу");
      await page.fill("input[name=code]", await shop.code(page));
      await page.click("button:has-text('Баталгаажуулах')");
      await expect(page).toHaveURL(new RegExp(`${ADMIN}/email`));
      await act();
    }
  }

  await withStepUp(async () => {
    await page.fill("input[name=label]:not([aria-label])", "Салбар-2 Gmail");
    await page.click("button:has-text('Хаяг нэмэх')");
  });
  await expect(addresses).toHaveCount(2);
  await expect(page.locator("#inbound-1")).toHaveText(first);
  await expect(page.locator("input[aria-label='Хаягийн нэр']").nth(1)).toHaveValue("Салбар-2 Gmail");

  await withStepUp(() => confirmAndSubmit(page, addresses.first().locator("button:has-text('Солих')")));
  await expect(page.locator(".notice-success")).toContainText("Хаягийг солилоо.");
  await expect(page.locator("section[aria-labelledby='addresses-title'] tbody")).not.toContainText(first);

  await withStepUp(() => confirmAndSubmit(page, addresses.first().locator("button:has-text('Хасах')")));
  await expect(addresses).toHaveCount(1);
  await expect(addresses.locator("button:has-text('Хасах')")).toHaveCount(0); // the last one stays
});

test("audit log records the actions above", async () => {
  await page.goto(`${ADMIN}/audit`);
  expect(await page.locator("tbody tr").count()).toBeGreaterThanOrEqual(6);
});

test("remembered browser: back in with the password only, until forgotten", async () => {
  async function signOutAndPassword(): Promise<void> {
    await page.click("button:has-text('Гарах')");
    await expect(page).toHaveURL(new RegExp(`${ADMIN}/login`));
    await page.fill("input[name=login_name]", shop.login);
    await page.fill("input[name=password]", PASSWORD);
    await page.click("button[type=submit]");
  }
  await page.goto(`${ADMIN}/`);
  await signOutAndPassword();
  await expect(page.locator("main h1")).toHaveText("Тойм"); // no code this time

  await page.goto(`${ADMIN}/me/devices`);
  await expect(page.locator("tbody")).toContainText("Энэ төхөөрөмж");
  await confirmAndSubmit(page, page.locator("button:has-text('Бүгдийг мартах')"));
  await expect(page.locator(".notice-success")).toContainText("Төхөөрөмжийг мартлаа.");

  await signOutAndPassword();
  await expect(page.locator("h1")).toHaveText("Баталгаажуулах код");
});
