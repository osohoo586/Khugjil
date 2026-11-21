import { Api, type DeviceInfo, type Txn } from "./api";
import { CashierApp } from "./app";
import type { Platform } from "./platform";
import { webPlatform } from "./platform-web";
import type { StreamHandlers } from "./stream";
import { txn } from "./dom.test";

const info: DeviceInfo = {
  device_name: "Касс-1", tenant_name: "Тест ХХК", accent: "navy", toast_seconds: 8,
  voice_enabled: false, voice_max_amount: null, show_today_totals: false, pin_shifts: true, multiple_accounts: false,
  accounts: ["Төв дэлгүүр"], members: [{ id: "m1", display_name: "Б. Сараа" }],
  shift: { id: "s1", member_id: "m1", display_name: "Б. Сараа", started_at: "2026-10-15T00:00:00Z" },
  vapid_public_key: null,
};

function fakeApi(routes: Record<string, unknown>): Api {
  const calls: string[] = [];
  const api = new Api(async (path, init) => {
    calls.push(`${init.method} ${path}`);
    const key = Object.keys(routes).find((k) => path.startsWith(k));
    const body = key ? routes[key] : {};
    return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
  });
  (api as unknown as { calls: string[] }).calls = calls;
  return api;
}

class FakeES {
  addEventListener() {}
  close() {}
}

describe("CashierApp", () => {
  beforeEach(() => {
    vi.stubGlobal("EventSource", FakeES);
    vi.stubGlobal("fetch", async () => new Response("", { status: 404 }));
  });
  afterEach(() => vi.unstubAllGlobals());

  it("renders the main view, applies the accent and shows each transaction once", async () => {
    const root = document.createElement("div");
    const toasts = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": info, "/api/v1/transactions": { items: [txn()] } });
    const app = new CashierApp(root, webPlatform(toasts, api));
    await app.start("/", "");
    expect(document.documentElement.dataset.accent).toBe("navy");
    expect(root.querySelector(".row.latest .row-amount")?.textContent).toBe("2,800 ₮");
    expect(root.querySelector(".status")?.textContent).toBe("Холбогдсон");
    const second: Txn = txn({ id: "t2", seq: 2, amount_text: "15,000 ₮" });
    app.receive(second, true);
    app.receive(second, true);
    expect(root.querySelectorAll(".row")).toHaveLength(2);
    expect(toasts.querySelectorAll(".toast")).toHaveLength(1);
    expect(root.querySelector(".row")?.className).toBe("row latest new");
    expect(root.querySelector("#verify, table")).toBeNull(); // only the feed, no verify box or table
  });

  it("shows the shift-start screen when no shift is open", async () => {
    const root = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": { ...info, shift: null } });
    await new CashierApp(root, webPlatform(document.createElement("div"), api)).start("/", "");
    expect(root.textContent).toContain("Ээлж эхлүүлэх");
    expect(root.querySelector("select option")?.textContent).toBe("Б. Сараа");
  });

  it("without PIN shifts: no end button, no name in the top bar, a click asks for notifications", async () => {
    const root = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": { ...info, pin_shifts: false }, "/api/v1/transactions": { items: [] } });
    let background = 0;
    const platform = { ...webPlatform(document.createElement("div"), api), enableBackground: async () => void (background += 1) };
    await new CashierApp(root, platform).start("/", "");
    expect(root.textContent).not.toContain("Ээлж дуусгах");
    expect(root.querySelector(".feed-empty")?.textContent).toBe("Орлого орж ирэхэд энд харагдана.");
    expect(root.querySelector(".who")).toBeNull();
    root.querySelector<HTMLButtonElement>(".banner-sound")?.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(background).toBe(1);
  });

  it("without PIN shifts and nobody to open a shift for: explains, no PIN screen", async () => {
    const root = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": { ...info, pin_shifts: false, shift: null } });
    await new CashierApp(root, webPlatform(document.createElement("div"), api)).start("/", "");
    expect(root.textContent).not.toContain("Ээлж эхлүүлэх");
    expect(root.querySelector(".notice-error")?.textContent).toContain("эзэмшигчийг нэмнэ үү");
  });

  it("a late arrival (forwarding was down) is listed by bank time, silently", async () => {
    const root = document.createElement("div");
    const toasts = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": info, "/api/v1/transactions": { items: [txn()] } });
    const app = new CashierApp(root, webPlatform(toasts, api));
    await app.start("/", "");
    app.receive(txn({ id: "old", seq: 9, amount_text: "9,000 ₮", txn_at: "2026-10-14T03:00:00Z", late: true }), true);
    expect(toasts.querySelectorAll(".toast")).toHaveLength(0);
    expect([...root.querySelectorAll(".row")].map((r) => r.getAttribute("data-id"))).toEqual(["t1", "old"]);
    expect(root.querySelector('[data-id="old"]')?.className).toBe("row");
  });

  it("each tick fetches anything newer than the newest seq it has (lost events still arrive)", async () => {
    const root = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": info, "/api/v1/transactions": { items: [txn({ seq: 7 })] } });
    const app = new CashierApp(root, webPlatform(document.createElement("div"), api));
    await app.start("/", "");
    app.tick();
    await new Promise((r) => setTimeout(r, 0));
    expect((api as unknown as { calls: string[] }).calls).toContain("GET /api/v1/transactions?since_seq=7");
  });

  it("without a viewer, each tick asks again, so adding the owner to the group is enough", async () => {
    const root = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": { ...info, pin_shifts: false, shift: null } });
    const app = new CashierApp(root, webPlatform(document.createElement("div"), api));
    await app.start("/", "");
    app.tick();
    await new Promise((r) => setTimeout(r, 0));
    const calls = (api as unknown as { calls: string[] }).calls.filter((c) => c === "GET /api/v1/device");
    expect(calls).toHaveLength(2);
  });

  it("strips tokens from the address bar on /pair and pairs with the token", async () => {
    const replaceState = vi.spyOn(history, "replaceState");
    const api = fakeApi({ "/api/v1/pair": { paired: true } });
    const root = document.createElement("div");
    await new CashierApp(root, webPlatform(document.createElement("div"), api)).start("/pair", "#t=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    expect(replaceState).toHaveBeenCalledWith(null, "", "/pair");
    await new Promise((r) => setTimeout(r, 0));
    expect((api as unknown as { calls: string[] }).calls).toContain("POST /api/v1/pair");
  });

  it("runs on an injected platform without page navigation (agent style)", async () => {
    const shown: string[] = [];
    const flashed: string[] = [];
    let handlers: StreamHandlers | undefined;
    const api = fakeApi({ "/api/v1/device": info, "/api/v1/transactions": { items: [] } });
    const platform: Platform = {
      kind: "agent",
      api,
      notifier: { show: (t) => shown.push(t.amount_text ?? ""), setSeconds: () => undefined, clear: () => shown.push("clear") },
      attention: { notify: (text) => flashed.push(text), stop: () => undefined },
      live: (h) => {
        handlers = h;
        return { connect: () => undefined, close: () => undefined };
      },
      enableBackground: async () => undefined,
      disableBackground: async () => undefined,
    };
    const root = document.createElement("div");
    await new CashierApp(root, platform).start("/", "");
    handlers?.onTxn(txn({ id: "t9", seq: 9, amount_text: "9,000 ₮" }));
    expect(shown).toEqual(["9,000 ₮"]);
    expect(flashed).toEqual(["Орлого 9,000 ₮"]);
    handlers?.onRevoked();
    expect(shown).toEqual(["9,000 ₮", "clear"]);
    expect(root.textContent).toContain("цуцлагдсан");
    root.querySelector<HTMLButtonElement>("button")?.click();
    await new Promise((r) => setTimeout(r, 0));
    expect(root.querySelector("h1")?.textContent).toBe("Энэ компьютерийг холбох");
  });

  it("tells the cashier when the shift can see no account", async () => {
    const root = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": { ...info, accounts: [] }, "/api/v1/transactions": { items: [] } });
    await new CashierApp(root, webPlatform(document.createElement("div"), api)).start("/", "");
    expect(root.querySelector(".notice-error")?.textContent).toContain("Энэ ээлжинд харах данс алга");
  });

  it("shows no such notice when accounts are visible", async () => {
    const root = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": info, "/api/v1/transactions": { items: [] } });
    await new CashierApp(root, webPlatform(document.createElement("div"), api)).start("/", "");
    expect(root.querySelector(".notice-error")).toBeNull();
  });
});
