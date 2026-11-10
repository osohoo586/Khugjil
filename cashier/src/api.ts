// Versioned JSON API client (/api/v1). The transport is injectable so the Phase 2 desktop agent can
// use bearer-token auth instead of the browser cookie without changing any UI code.

export interface Txn {
  id: string;
  seq: number;
  amount: string;
  currency: string;
  amount_text: string;
  description: string;
  txn_at: string;
  time_text: string;
  datetime_text: string;
  account_alias: string | null;
  test: boolean;
}

export interface DeviceInfo {
  device_name: string;
  tenant_name: string;
  accent: "green" | "navy";
  toast_seconds: number;
  voice_enabled: boolean;
  voice_max_amount: string | null;
  show_today_totals: boolean;
  multiple_accounts: boolean;
  accounts: string[];
  members: { id: string; display_name: string }[];
  shift: { id: string; member_id: string; display_name: string; started_at: string } | null;
  vapid_public_key: string | null;
}

export interface InviteView {
  tenant_name: string;
  display_name: string;
  consent_version: number;
  consent_text: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

export type Transport = (path: string, init: RequestInit) => Promise<Response>;

export const cookieTransport: Transport = (path, init) =>
  fetch(path, { ...init, credentials: "same-origin", cache: "no-store" });

const FALLBACK = "Холболтод алдаа гарлаа. Дахин оролдоно уу.";

export class Api {
  constructor(private readonly transport: Transport = cookieTransport) {}

  private async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const init: RequestInit = { method, headers: { accept: "application/json" } };
    if (body !== undefined) {
      init.headers = { ...init.headers, "content-type": "application/json" };
      init.body = JSON.stringify(body);
    }
    let response: Response;
    try {
      response = await this.transport(`/api/v1${path}`, init);
    } catch {
      throw new ApiError(0, "network", FALLBACK);
    }
    const data: unknown = await response.json().catch(() => ({}));
    if (!response.ok) {
      const err = data as { error?: string; message?: string };
      throw new ApiError(response.status, err.error ?? "error", err.message ?? FALLBACK);
    }
    return data as T;
  }

  device() { return this.call<DeviceInfo>("GET", "/device"); }
  pair(input: { token?: string; code?: string }) { return this.call<{ paired: boolean }>("POST", "/pair", input); }
  startShift(memberId: string, pin: string) {
    return this.call<{ shift_id: string }>("POST", "/shift/start", { member_id: memberId, pin });
  }
  endShift() { return this.call<{ ended: number }>("POST", "/shift/end"); }
  activity() { return this.call<{ ok: boolean }>("POST", "/shift/activity"); }
  transactions(sinceSeq?: number) {
    const q = sinceSeq === undefined ? "" : `?since_seq=${sinceSeq}`;
    return this.call<{ items: Txn[] }>("GET", `/transactions${q}`);
  }
  verify(amount: string) {
    return this.call<{ matches: Txn[] }>("GET", `/verify?amount=${encodeURIComponent(amount)}`);
  }
  ack(id: string) { return this.call<{ acked: boolean }>("POST", `/transactions/${encodeURIComponent(id)}/ack`); }
  summaryToday() { return this.call<{ count: number; total_text: string }>("GET", "/summary/today"); }
  pushSubscribe(sub: PushSubscriptionJSON) { return this.call<{ subscribed: boolean }>("POST", "/push/subscribe", sub); }
  pushUnsubscribe() { return this.call<{ subscribed: boolean }>("DELETE", "/push/subscribe"); }
  inviteInspect(token: string) { return this.call<InviteView>("POST", "/invite/inspect", { token }); }
  inviteAccept(token: string, pin: string, consentVersion: number) {
    return this.call<{ accepted: boolean }>("POST", "/invite/accept", {
      token, pin, consent_version: consentVersion, accept: true,
    });
  }
}
