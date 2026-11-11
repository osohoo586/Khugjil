import { expect, test, type Page } from "@playwright/test";

// The desktop agent's overlay window (PLAN-PHASE2 §1.1), rendered from the agent build with the
// Tauri IPC bridge replaced by a recorder: Rust would send `khj://overlay` events and receive
// `overlay_layout` calls to size and show the window.
const BASE = "http://127.0.0.1:4175";

type Call = [string, Record<string, unknown> | undefined];
interface Fake {
  calls: Call[];
  emit(event: string, payload: unknown): void;
}

async function fakeTauri(page: Page) {
  await page.addInitScript(() => {
    const callbacks = new Map<number, (message: unknown) => void>();
    const listeners = new Map<string, number[]>();
    let next = 1;
    const fake = {
      calls: [] as unknown[],
      emit(event: string, payload: unknown) {
        for (const id of listeners.get(event) ?? []) callbacks.get(id)?.({ event, id: 0, payload });
      },
    };
    const w = window as unknown as Record<string, unknown>;
    w.__khj = fake;
    w.__TAURI_INTERNALS__ = {
      transformCallback(cb: (message: unknown) => void) {
        const id = next++;
        callbacks.set(id, cb);
        return id;
      },
      unregisterCallback(id: number) {
        callbacks.delete(id);
      },
      async invoke(cmd: string, args?: Record<string, unknown>) {
        fake.calls.push([cmd, args]);
        if (cmd === "plugin:event|listen" && args) {
          const event = String(args.event);
          listeners.set(event, [...(listeners.get(event) ?? []), Number(args.handler)]);
          return listeners.get(event)?.length;
        }
        return null;
      },
    };
  });
}

const calls = (page: Page, cmd: string) =>
  page.evaluate((c) => (window as unknown as { __khj: Fake }).__khj.calls.filter(([n]) => n === c), cmd);

test("agent overlay: hostile description as text, sized to content, closes on click", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 420 });
  await fakeTauri(page);
  await page.goto(`${BASE}/overlay.html`);
  await expect.poll(async () => (await calls(page, "overlay_ready")).length).toBe(1);
  await page.evaluate(() =>
    (window as unknown as { __khj: Fake }).__khj.emit("khj://overlay", {
      amount_text: "45,000 ₮",
      description: '<img src=x onerror="document.title=1">Хүргэлт',
      time_text: "08:39",
      account_alias: "Салбар-2",
      test: false,
    }),
  );
  const toast = page.locator(".toast");
  await expect(toast).toContainText("45,000 ₮");
  await expect(page.locator(".toast img")).toHaveCount(0);
  await expect(page.locator(".toast-desc")).toHaveText('<img src=x onerror="document.title=1">Хүргэлт');
  await page.evaluate(() => document.fonts.ready);
  await expect(page.locator("#toasts")).toHaveScreenshot("agent-overlay.png");
  const sized = async () => (await calls(page, "overlay_layout")).at(-1)?.[1]?.height;
  await expect.poll(sized).toBeGreaterThanOrEqual(112);
  await toast.click();
  await expect(toast).toHaveCount(0);
  await expect.poll(sized).toBe(0);
  expect(await page.title()).toBe("Орлого");
});
