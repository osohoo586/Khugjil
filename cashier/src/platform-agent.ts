// Desktop agent platform (PLAN-PHASE2 §1, §3). The webview never sees the device token: every API
// call goes through the Rust command `api_request` (allowlisted paths, bearer added in Rust), the
// live stream is held by Rust and forwarded as events, and notifications go to the always-on-top
// overlay window instead of in-page toasts or Web Push.
import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";
import { Api, type Transport, type Txn } from "./api";
import type { LiveConnection, Platform } from "./platform";
import type { StreamHandlers } from "./stream";

export const STREAM_EVENT = "khj://stream";

interface IpcResponse {
  status: number;
  body: unknown;
}

/** Events the Rust stream client forwards to the main window. `data` is the SSE data line. */
export type StreamEvent =
  | { kind: "status"; connected: boolean }
  | { kind: "reconnected" }
  | { kind: "txn" | "test"; data: string }
  | { kind: "shift_ended" | "settings_changed" | "device_revoked" };

/** The note the overlay shows: plain strings only, rendered with textContent. */
export interface OverlayNote {
  amount_text: string;
  description: string;
  time_text: string;
  account_alias: string;
  test: boolean;
}

export const ipcTransport: Transport = async (path, init) => {
  const method = init.method ?? "GET";
  const body: unknown = typeof init.body === "string" ? JSON.parse(init.body) : null;
  const r = await invoke<IpcResponse>("api_request", { method, path, body });
  return new Response(JSON.stringify(r.body ?? {}), {
    status: r.status,
    headers: { "content-type": "application/json" },
  });
};

export function toOverlayNote(t: Partial<Txn>, showAlias: boolean): OverlayNote {
  return {
    amount_text: t.amount_text ?? "",
    description: t.description ?? "",
    time_text: t.time_text ?? "",
    account_alias: showAlias ? (t.account_alias ?? "") : "",
    test: t.test === true,
  };
}

export class AgentStream implements LiveConnection {
  private unlisten: UnlistenFn | null = null;
  private closed = false;

  constructor(private readonly handlers: StreamHandlers) {}

  connect(): void {
    this.closed = false;
    void listen<StreamEvent>(STREAM_EVENT, (e) => this.dispatch(e.payload)).then(async (unlisten) => {
      if (this.closed) return unlisten();
      this.unlisten = unlisten;
      await invoke("stream_start");
    });
  }

  dispatch(ev: StreamEvent): void {
    if (this.closed) return;
    const h = this.handlers;
    switch (ev.kind) {
      case "status":
        return h.onStatus(ev.connected);
      case "reconnected":
        return h.onReconnected();
      case "txn":
        return h.onTxn(JSON.parse(ev.data) as Txn);
      case "test":
        return h.onTest(JSON.parse(ev.data) as Partial<Txn>);
      case "shift_ended":
        return h.onShiftEnded();
      case "settings_changed":
        return h.onSettings();
      case "device_revoked":
        this.close();
        return h.onRevoked();
    }
  }

  close(): void {
    this.closed = true;
    this.unlisten?.();
    this.unlisten = null;
    void invoke("stream_stop").catch(() => undefined);
  }
}

export function agentPlatform(api: Api = new Api(ipcTransport)): Platform {
  const quiet = (p: Promise<unknown>) => void p.catch(() => undefined);
  return {
    kind: "agent",
    api,
    notifier: {
      show: (t, showAlias) => quiet(invoke("overlay_show", { note: toOverlayNote(t, showAlias) })),
      setSeconds: (seconds) => quiet(invoke("overlay_config", { seconds })),
      clear: () => quiet(invoke("overlay_clear")),
    },
    attention: {
      notify: () => quiet(invoke("attention", { on: true })),
      stop: () => quiet(invoke("attention", { on: false })),
    },
    live: (handlers) => new AgentStream(handlers),
    enableBackground: async () => undefined,
    disableBackground: async () => undefined,
  };
}
