import { execFileSync } from "node:child_process";
import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, test, type Page, type Response } from "@playwright/test";

// Real-browser flows against the full local stack (scripts/dev-up.sh: Caddy -> API -> worker).
// These catch what in-process HTTP tests cannot: Origin/Referrer behaviour of real form posts,
// __Host- cookies, HTMX dialogs, the SSE stream through the proxy. Each run uses its own new shop.
const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const ADMIN = "https://admin.khugjil.localhost:8443";
const APP = "https://app.khugjil.localhost:8443";
const RUN = Date.now().toString(36);
const TENANT_FILE = join(ROOT, ".dev", `flow-tenant-${RUN}.txt`);
const PASSWORD = "flow-test-password-not-real";
const DEMO_PIN = "482913";

function seed(...args: string[]): string {
  return execFileSync(join(ROOT, "scripts", "dev-seed.sh"), args, {
    env: { ...process.env, KHJ_DEV_TENANT_FILE: TENANT_FILE },
    encoding: "utf8",
  });
}

function base32(secret: string): Buffer {
  const alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  let bits = 0;
  let value = 0;
  const out: number[] = [];
  for (const c of secret.replace(/=+$/, "").toUpperCase()) {
    value = (value << 5) | alphabet.indexOf(c);
    bits += 5;
    if (bits >= 8) {
      out.push((value >>> (bits - 8)) & 255);
      bits -= 8;
    }
  }
  return Buffer.from(out);
}

/** RFC 6238 (SHA-1, 6 digits, 30 s), like pyotp. `offset` in time steps. */
function totp(secret: string, offset = 0): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(Math.floor(Date.now() / 30_000) + offset));
  const h = createHmac("sha1", base32(secret)).update(counter).digest();
  const o = h[h.length - 1]! & 15;
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

/** Every 403 and 5xx is a failure: a real browser must never be refused by our own guards. */
function watch(page: Page, bad: string[]): void {
  page.on("response", (r: Response) => {
    if (r.status() === 403 || r.status() >= 500) bad.push(`${r.request().method()} ${r.status()} ${r.url()}`);
  });
}

test.describe.configure({ mode: "serial" });

let invite = "";
let totpSecret = "";
const login = `owner-${RUN}`;

test.beforeAll(() => {
  seed("tenant", `Flow ${RUN}`);
  seed(); // accounts, groups, the two demo cashiers, history
  invite = /owner_invite=(.+)/.exec(readFileSync(TENANT_FILE, "utf8"))![1]!.trim();
});

test("owner accepts the invite, sets a password and enrolls TOTP", async ({ page }) => {
  const bad: string[] = [];
  watch(page, bad);
  await page.goto(invite);
  await expect(page.locator("h1")).toHaveText("Бүртгэл үүсгэх");
  expect(page.url()).toBe(`${ADMIN}/invite/setup`); // the token left the address bar
  await page.fill("input[name=login_name]", login);
  await page.fill("input[name=password]", PASSWORD);
  await page.fill("input[name=password2]", PASSWORD);
  for (const box of await page.locator("input[type=checkbox]").all()) await box.check();
  await page.click("button[type=submit]");
  await expect(page.locator("h1")).toHaveText("Хоёр шаттай баталгаажуулалт");
  totpSecret = (await page.locator("code.tabular").textContent())!.trim();
  await page.fill("input[name=code]", totp(totpSecret));
  await page.click("button[type=submit]");
  await expect(page.locator("h1")).toHaveText("Бүртгэл үүслээ");
  await expect(page.locator("ol.codes li")).toHaveCount(10);
  expect(bad).toEqual([]);
});

test("owner signs in, pairs a cashier PC, sees a live payment, revokes the PC", async ({ browser }) => {
  const bad: string[] = [];
  const admin = await (await browser.newContext()).newPage();
  watch(admin, bad);

  await admin.goto(`${ADMIN}/login`);
  await admin.fill("input[name=login_name]", login);
  await admin.fill("input[name=password]", PASSWORD);
  await admin.click("button[type=submit]");
  await expect(admin.locator("h1")).toHaveText("Баталгаажуулах код");
  await admin.fill("input[name=code]", totp(totpSecret, 1)); // the enrolment used the current step
  await admin.click("button[type=submit]");
  await expect(admin.locator("main h1")).toHaveText("Тойм");

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

  seed("txn", "21500", `Flow ${RUN}`);
  await expect(cashier.locator(".toast").first()).toContainText("21,500 ₮", { timeout: 20_000 });
  await expect(cashier.locator(".hero-amount")).toHaveText("21,500 ₮");
  await expect(cashier.locator("tbody tr").first()).toContainText(`Flow ${RUN}`);

  await admin.goto(`${ADMIN}/devices`);
  const row = admin.locator("tr", { hasText: "Flow-касс" });
  await expect(row).toContainText("Б. Сараа");
  await row.locator("button:has-text('Цуцлах')").click();
  await admin.locator("#confirm-dialog [data-dialog-ok]").click();
  await expect(admin.locator("tr", { hasText: "Flow-касс" })).toHaveCount(0);
  await expect(cashier.locator("h1")).toHaveText("Төхөөрөмжийн эрх цуцлагдсан", { timeout: 20_000 });

  await admin.click("button:has-text('Гарах')");
  await expect(admin).toHaveURL(`${ADMIN}/login`);
  expect(bad).toEqual([]);
});
