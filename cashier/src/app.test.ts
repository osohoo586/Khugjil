import { Api, type DeviceInfo, type Txn } from "./api";
import { CashierApp } from "./app";
import { txn } from "./dom.test";

const info: DeviceInfo = {
  device_name: "Касс-1", tenant_name: "Тест ХХК", accent: "navy", toast_seconds: 8,
  voice_enabled: false, voice_max_amount: null, show_today_totals: false, multiple_accounts: false,
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
    const app = new CashierApp(root, toasts, api);
    await app.start("/", "");
    expect(document.documentElement.dataset.accent).toBe("navy");
    expect(root.querySelector(".hero-amount")?.textContent).toBe("2,800 ₮");
    expect(root.querySelector(".status")?.textContent).toBe("Холбогдсон");
    const second: Txn = txn({ id: "t2", seq: 2, amount_text: "15,000 ₮" });
    app.receive(second, true);
    app.receive(second, true);
    expect(root.querySelectorAll("tbody tr")).toHaveLength(2);
    expect(toasts.querySelectorAll(".toast")).toHaveLength(1);
    expect(root.querySelector(".hero")?.className).toBe("hero new");
  });

  it("shows the shift-start screen when no shift is open", async () => {
    const root = document.createElement("div");
    const api = fakeApi({ "/api/v1/device": { ...info, shift: null } });
    await new CashierApp(root, document.createElement("div"), api).start("/", "");
    expect(root.textContent).toContain("Ээлж эхлүүлэх");
    expect(root.querySelector("select option")?.textContent).toBe("Б. Сараа");
  });

  it("strips tokens from the address bar on /pair and pairs with the token", async () => {
    const replaceState = vi.spyOn(history, "replaceState");
    const api = fakeApi({ "/api/v1/pair": { paired: true } });
    const root = document.createElement("div");
    await new CashierApp(root, document.createElement("div"), api).start("/pair", "#t=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    expect(replaceState).toHaveBeenCalledWith(null, "", "/pair");
    await new Promise((r) => setTimeout(r, 0));
    expect((api as unknown as { calls: string[] }).calls).toContain("POST /api/v1/pair");
  });
});
