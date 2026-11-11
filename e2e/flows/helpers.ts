import { execFileSync } from "node:child_process";
import { createHmac } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { expect, type Page, type Response } from "@playwright/test";

export const ROOT = fileURLToPath(new URL("../..", import.meta.url));
export const ADMIN = "https://admin.khugjil.localhost:8443";
export const APP = "https://app.khugjil.localhost:8443";
export const PASSWORD = "flow-test-password-not-real";
export const DEMO_PIN = "482913";

/** A fresh shop per spec file, so runs never touch the demo shop or each other. */
export class Shop {
  readonly run = Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36);
  readonly file = join(ROOT, ".dev", `flow-tenant-${this.run}.txt`);
  readonly login = `owner-${this.run}`;
  secret = "";
  private lastStep = -1;

  create(): void {
    this.seed("tenant", `Flow ${this.run}`);
    this.seed(); // accounts, groups, the two demo cashiers, history
  }

  seed(...args: string[]): string {
    return execFileSync(join(ROOT, "scripts", "dev-seed.sh"), args, {
      env: { ...process.env, KHJ_DEV_TENANT_FILE: this.file },
      encoding: "utf8",
    });
  }

  get invite(): string {
    return /owner_invite=(.+)/.exec(readFileSync(this.file, "utf8"))![1]!.trim();
  }

  /** The next TOTP code the server will accept: steps must increase (replays are refused). */
  async code(page: Page): Promise<string> {
    const current = () => Math.floor(Date.now() / 30_000);
    const step = Math.max(this.lastStep + 1, current() - 1);
    while (step > current() + 1) await page.waitForTimeout(1_000);
    this.lastStep = step;
    return totpAt(this.secret, step);
  }
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

/** RFC 6238 (SHA-1, 6 digits, 30 s steps), like pyotp. */
export function totpAt(secret: string, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const h = createHmac("sha1", base32(secret)).update(counter).digest();
  const o = h[h.length - 1]! & 15;
  return String((h.readUInt32BE(o) & 0x7fffffff) % 1_000_000).padStart(6, "0");
}

/** Every 403 and 5xx is a failure: a real browser must never be refused by our own guards. */
export function watch(page: Page, bad: string[]): void {
  page.on("response", (r: Response) => {
    if (r.status() === 403 || r.status() >= 500) bad.push(`${r.request().method()} ${r.status()} ${r.url()}`);
  });
}

export async function acceptOwnerInvite(page: Page, shop: Shop): Promise<void> {
  await page.goto(shop.invite);
  await expect(page.locator("h1")).toHaveText("Бүртгэл үүсгэх");
  expect(page.url()).toBe(`${ADMIN}/invite/setup`); // the token left the address bar
  await page.fill("input[name=login_name]", shop.login);
  await page.fill("input[name=password]", PASSWORD);
  await page.fill("input[name=password2]", PASSWORD);
  for (const box of await page.locator("input[type=checkbox]").all()) await box.check();
  await page.click("button[type=submit]");
  await expect(page.locator("h1")).toHaveText("Хоёр шаттай баталгаажуулалт");
  shop.secret = (await page.locator("code.tabular").textContent())!.trim();
  await page.fill("input[name=code]", await shop.code(page));
  await page.click("button[type=submit]");
  await expect(page.locator("h1")).toHaveText("Бүртгэл үүслээ");
  await expect(page.locator("ol.codes li")).toHaveCount(10);
}

export async function signIn(page: Page, shop: Shop): Promise<void> {
  await page.goto(`${ADMIN}/login`);
  await page.fill("input[name=login_name]", shop.login);
  await page.fill("input[name=password]", PASSWORD);
  await page.click("button[type=submit]");
  await expect(page.locator("h1")).toHaveText("Баталгаажуулах код");
  await page.fill("input[name=code]", await shop.code(page));
  await page.click("button[type=submit]");
  await expect(page.locator("main h1")).toHaveText("Тойм");
}

/** Submit a form that asks first (data-confirm) through the page's own <dialog>. */
export async function confirmAndSubmit(page: Page, button: ReturnType<Page["locator"]>): Promise<void> {
  await button.click();
  await page.locator("#confirm-dialog [data-dialog-ok]").click();
}
