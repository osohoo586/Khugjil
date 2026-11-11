// What differs between the browser cashier (web) and the Windows desktop agent (PLAN-PHASE2 §3).
// The screens and CashierApp are shared; each build injects one Platform.
import type { Api, DeviceInfo, Txn } from "./api";
import type { StreamHandlers } from "./stream";

export type PlatformKind = "web" | "agent";

/** A live event connection (SSE in the browser, the agent's Rust stream in Tauri). */
export interface LiveConnection {
  connect(): void;
  close(): void;
}

/** Shows an incoming transaction: in-page toasts on the web, the always-on-top overlay in the agent. */
export interface Notifier {
  show(txn: Partial<Txn>, showAlias: boolean): void;
  setSeconds(seconds: number): void;
  clear(): void;
}

/** Draws the eye when the window is not focused: title flashing (web), taskbar flash (agent). */
export interface Attention {
  notify(text: string): void;
  stop(): void;
}

export interface Platform {
  readonly kind: PlatformKind;
  readonly api: Api;
  readonly notifier: Notifier;
  readonly attention: Attention;
  live(handlers: StreamHandlers): LiveConnection;
  /** Called from the shift-start click (a user gesture). Web: Web Push; agent: nothing. */
  enableBackground(info: DeviceInfo): Promise<void>;
  disableBackground(): Promise<void>;
  /** Top-level navigation ("/" or "/pair"). Absent: CashierApp re-renders in place. */
  readonly navigate?: (path: string) => void;
}
