import { Api } from "./api";
import { txn } from "./dom.test";

const invoked: { cmd: string; args: unknown }[] = [];
let listener: ((e: { payload: unknown }) => void) | null = null;
let reply: (cmd: string, args: unknown) => unknown = () => undefined;

vi.mock("@tauri-apps/api/core", () => ({
  invoke: async (cmd: string, args?: unknown) => {
    invoked.push({ cmd, args });
    return reply(cmd, args);
  },
}));
vi.mock("@tauri-apps/api/event", () => ({
  listen: async (_name: string, cb: (e: { payload: unknown }) => void) => {
    listener = cb;
    return () => {
      listener = null;
    };
  },
}));

const { AgentStream, agentPlatform, ipcTransport, toOverlayNote } = await import("./platform-agent");

const flush = () => new Promise((r) => setTimeout(r, 0));

describe("agent platform", () => {
  beforeEach(() => {
    invoked.length = 0;
    listener = null;
    reply = () => undefined;
  });

  it("sends API calls through the api_request command and maps status codes", async () => {
    reply = (cmd) => (cmd === "api_request" ? { status: 426, body: { error: "update_required", message: "Касс програмаа шинэчилнэ үү." } } : undefined);
    const api = new Api(ipcTransport);
    await expect(api.device()).rejects.toMatchObject({ status: 426, code: "update_required" });
    expect(invoked[0]).toEqual({ cmd: "api_request", args: { method: "GET", path: "/api/v1/device", body: null } });

    reply = () => ({ status: 200, body: { paired: true } });
    await api.pair({ code: "ABCD-EFGH-JKMN" });
    expect(invoked[1]).toEqual({ cmd: "api_request", args: { method: "POST", path: "/api/v1/pair", body: { code: "ABCD-EFGH-JKMN" } } });
  });

  it("treats a rejected command as a network error", async () => {
    reply = () => {
      throw new Error("offline");
    };
    await expect(new Api(ipcTransport).device()).rejects.toMatchObject({ status: 0, code: "network" });
  });

  it("dispatches stream events from Rust and stops on revocation", async () => {
    const seen: string[] = [];
    const stream = new AgentStream({
      onTxn: (t) => seen.push(`txn ${t.amount_text}`),
      onTest: () => seen.push("test"),
      onShiftEnded: () => seen.push("shift_ended"),
      onRevoked: () => seen.push("revoked"),
      onSettings: () => seen.push("settings"),
      onStatus: (ok) => seen.push(`status ${ok}`),
      onReconnected: () => seen.push("reconnected"),
    });
    stream.connect();
    await flush();
    expect(invoked.map((c) => c.cmd)).toEqual(["stream_start"]);
    const emit = (payload: unknown) => listener?.({ payload });
    emit({ kind: "status", connected: true });
    emit({ kind: "txn", data: JSON.stringify(txn()) });
    emit({ kind: "test", data: "{}" });
    emit({ kind: "settings_changed" });
    emit({ kind: "shift_ended" });
    emit({ kind: "status", connected: false });
    emit({ kind: "reconnected" });
    emit({ kind: "device_revoked" });
    emit({ kind: "txn", data: JSON.stringify(txn()) }); // after close: ignored
    expect(seen).toEqual([
      "status true", "txn 2,800 ₮", "test", "settings", "shift_ended", "status false", "reconnected", "revoked",
    ]);
    expect(invoked.map((c) => c.cmd)).toContain("stream_stop");
  });

  it("does not start the stream when closed before listening began", async () => {
    const stream = new AgentStream({
      onTxn: () => undefined, onTest: () => undefined, onShiftEnded: () => undefined, onRevoked: () => undefined,
      onSettings: () => undefined, onStatus: () => undefined, onReconnected: () => undefined,
    });
    stream.connect();
    stream.close();
    await flush();
    expect(invoked.map((c) => c.cmd)).not.toContain("stream_start");
  });

  it("shows notes in the overlay as plain strings, alias only when wanted", async () => {
    const p = agentPlatform(new Api(ipcTransport));
    const hostile = txn({ description: '<img src=x onerror="alert(1)">Хүргэлт', account_alias: "Салбар-2" });
    p.notifier.show(hostile, false);
    p.notifier.setSeconds(12);
    p.notifier.clear();
    p.attention.notify("Орлого 2,800 ₮");
    p.attention.stop();
    await flush();
    expect(invoked).toEqual([
      { cmd: "overlay_show", args: { note: { amount_text: "2,800 ₮", description: '<img src=x onerror="alert(1)">Хүргэлт', time_text: "08:37", account_alias: "", test: false } } },
      { cmd: "overlay_config", args: { seconds: 12 } },
      { cmd: "overlay_clear", args: undefined },
      { cmd: "attention", args: { on: true } },
      { cmd: "attention", args: { on: false } },
    ]);
    expect(toOverlayNote({ test: true }, true)).toEqual({ amount_text: "", description: "", time_text: "", account_alias: "", test: true });
    expect(p.kind).toBe("agent");
    expect(p.navigate).toBeUndefined();
  });
});
