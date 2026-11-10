import { expect, test, type Page } from "@playwright/test";

const BASE = "http://127.0.0.1:4173";
const NOW = new Date("2026-10-15T00:40:00Z");

const info = {
  device_name: "Касс-1", tenant_name: "Жишээ ХХК", accent: "green", toast_seconds: 20,
  voice_enabled: false, voice_max_amount: null, show_today_totals: true, multiple_accounts: true,
  accounts: ["Төв дэлгүүр", "Салбар-2"], members: [{ id: "m1", display_name: "Б. Сараа" }],
  shift: { id: "s1", member_id: "m1", display_name: "Б. Сараа", started_at: "2026-10-15T00:00:00Z" },
  vapid_public_key: null,
};

function txn(seq: number, amount_text: string, description: string, time: string, alias: string) {
  return {
    id: `t${seq}`, seq, amount: "0", currency: "MNT", amount_text, description,
    txn_at: `2026-10-15T00:${time.slice(3)}:00Z`, time_text: time, datetime_text: `2026.10.15 ${time}`,
    account_alias: alias, test: false,
  };
}

const items = [
  txn(5, "2,800 ₮", "Худалдан авалт", "08:37", "Төв дэлгүүр"),
  txn(4, "15,000 ₮", "Захиалга 4411", "08:21", "Салбар-2"),
  txn(3, "120,500.50 ₮", "Түрээсийн төлбөр, 10-р сар", "08:05", "Төв дэлгүүр"),
  txn(2, "3,000 ₮", "Б. Батаагийн хүүхдийн цэцэрлэгийн төлбөр, 10-р сарын хоол", "07:58", "Салбар-2"),
  txn(1, "250 USD", "Invoice 0091", "07:41", "Төв дэлгүүр"),
];

async function mockApi(page: Page, opts: { shift?: boolean; events?: { type: string; data: string }[] } = {}) {
  await page.clock.install({ time: NOW });
  await page.route("**/api/v1/device", (r) => r.fulfill({ json: opts.shift === false ? { ...info, shift: null } : info }));
  await page.route("**/api/v1/transactions*", (r) => r.fulfill({ json: { items } }));
  await page.route("**/api/v1/summary/today", (r) => r.fulfill({ json: { count: 14, total_text: "312,500 ₮" } }));
  await page.route("**/api/v1/verify*", (r) => r.fulfill({ json: { matches: [items[0]] } }));
  await page.route("**/api/v1/transactions/*/ack", (r) => r.fulfill({ json: { acked: true } }));
  await page.route("**/voice/manifest.json", (r) => r.fulfill({ status: 404 }));
  await page.addInitScript((events: { type: string; data: string }[]) => {
    class FakeEventSource extends EventTarget {
      constructor(readonly url: string) {
        super();
        const w = window as unknown as { __esCount?: number };
        w.__esCount = (w.__esCount ?? 0) + 1;
        const first = w.__esCount === 1;
        setTimeout(() => {
          // First connection: open, then replay the scripted events. Every reconnect attempt
          // afterwards fails, like a real outage.
          if (!first) return this.dispatchEvent(new Event("error"));
          this.dispatchEvent(new Event("open"));
          for (const e of events) this.dispatchEvent(new MessageEvent(e.type, { data: e.data }));
        }, 0);
      }
      close() {}
    }
    (window as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
  }, opts.events ?? []);
}

test("cashier full layout (>= 1024 px)", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await mockApi(page);
  await page.goto(BASE);
  await page.locator("#verify").fill("2800");
  await expect(page.locator(".verify-result")).toHaveText("Орсон байна — 08:37, Худалдан авалт");
  await expect(page).toHaveScreenshot("cashier-full.png");
});

test("cashier compact layout (docked, 420 px)", async ({ page }) => {
  await page.setViewportSize({ width: 420, height: 900 });
  await mockApi(page);
  await page.goto(BASE);
  await expect(page.locator(".hero-amount")).toHaveText("2,800 ₮");
  await expect(page).toHaveScreenshot("cashier-compact.png");
});

test("cashier phone layout (PWA)", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await mockApi(page);
  await page.goto(BASE);
  await expect(page.locator(".hero-amount")).toBeVisible();
  await expect(page).toHaveScreenshot("cashier-phone.png");
});

test("shift start screen", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 700 });
  await mockApi(page, { shift: false });
  await page.goto(BASE);
  await expect(page.getByRole("button", { name: "Ээлж эхлүүлэх" })).toBeVisible();
  await expect(page).toHaveScreenshot("cashier-shift-start.png");
});

test("toast, hostile description as text, then disconnected banner after 5 s", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  const hostile = txn(6, "45,000 ₮", '<img src=x onerror="document.title=1">Хүргэлт', "08:39", "Салбар-2");
  await mockApi(page, { events: [{ type: "txn", data: JSON.stringify(hostile) }, { type: "error", data: "" }] });
  await page.goto(BASE);
  const toast = page.locator(".toast").first();
  await expect(toast).toContainText("45,000 ₮");
  await expect(page.locator(".toast img")).toHaveCount(0);
  await expect(toast).toHaveScreenshot("toast.png");
  await page.clock.fastForward(6000);
  await expect(page.locator(".banner-error")).toBeVisible();
});
