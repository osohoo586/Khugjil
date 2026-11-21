import { type Api, ApiError, type DeviceInfo, type Txn } from "./api";
import { Chime } from "./audio";
import { h, replace } from "./dom";
import { clock } from "./format";
import type { LiveConnection, Platform } from "./platform";
import { Feed } from "./ui/feed";
import {
  HIGHLIGHT_MS,
  LIST_LIMIT,
  inviteScreen,
  message,
  pairScreen,
  revokedScreen,
  shiftStart,
  topBar,
} from "./ui/screens";
import { loadVoice, type Voice } from "./voice";

const DISCONNECT_BANNER_MS = 5_000;
const ACTIVITY_MS = 5 * 60_000;
/** Newest bank time first, so a late arrival slots in where it belongs instead of on top. */
export function byBankTime(items: Txn[]): Txn[] {
  return [...items].sort((a, b) => Date.parse(b.txn_at) - Date.parse(a.txn_at) || b.seq - a.seq);
}

const NO_VIEWER =
  "Энэ төхөөрөмжөөр гүйлгээ харах эрхтэй хүн алга. Админ «Бүлгүүд» хуудаснаас төхөөрөмжийн бүлэгт эзэмшигчийг нэмнэ үү.";

export class CashierApp {
  private info: DeviceInfo | null = null;
  private items: Txn[] = [];
  /** Transaction id -> time until which its row stays highlighted. */
  private readonly freshUntil = new Map<string, number>();
  private feed: Feed | null = null;
  /** Without PIN shifts and with nobody to open one for: re-check on every tick. */
  private waitingForViewer = false;
  private stream: LiveConnection | null = null;
  private connected = true;
  private disconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private lastActivity = 0;
  private readonly chime = new Chime();
  private readonly api: Api;
  private voice: Voice | null = null;
  private refs: { status?: HTMLElement; clock?: HTMLElement; banner?: HTMLElement; sound?: HTMLElement; footer?: HTMLElement } = {};

  constructor(
    private readonly root: HTMLElement,
    private readonly platform: Platform,
  ) {
    this.api = platform.api;
  }

  async start(path: string, hash: string): Promise<void> {
    const token = /(?:^|[#&])t=([A-Za-z0-9_-]{20,128})/.exec(hash)?.[1];
    if (hash) history.replaceState(null, "", path); // tokens never stay in the address bar
    if (path === "/pair") return this.showPair(token);
    if (path === "/invite" && this.platform.kind === "web") return this.showInvite(token);
    this.voice ??= await loadVoice();
    await this.load();
  }

  private go(path: string): void {
    this.stream?.close();
    this.stream = null;
    if (this.platform.navigate) this.platform.navigate(path);
    else void this.start(path, "");
  }

  // ------------------------------------------------------------ loading + screens

  private async load(): Promise<void> {
    try {
      this.info = await this.api.device();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        this.go("/pair");
        return;
      }
      replace(this.root, message(e instanceof ApiError ? e.message : "Алдаа гарлаа.", "error"));
      return;
    }
    this.waitingForViewer = false;
    document.documentElement.dataset.accent = this.info.accent;
    this.platform.notifier.setSeconds(this.info.toast_seconds);
    if (!this.info.shift) {
      this.stream?.close();
      this.stream = null;
      this.feed = null;
      if (this.info.pin_shifts) this.renderShiftStart(this.info);
      else {
        this.waitingForViewer = true;
        replace(this.root, topBar(this.info, new Date(), null).el, message(NO_VIEWER, "error"));
      }
      return;
    }
    await this.loadItems();
    this.renderMain();
    this.connect();
  }

  private renderShiftStart(info: DeviceInfo): void {
    replace(
      this.root,
      topBar(info, new Date(), null).el,
      shiftStart(info, (memberId, pin, out) => void this.startShift(memberId, pin, out)),
    );
  }

  private async startShift(memberId: string, pin: string, out: HTMLElement): Promise<void> {
    await this.chime.unlock(); // the click is the user gesture that unlocks sound
    try {
      await this.api.startShift(memberId, pin);
    } catch (e) {
      replace(out, message(e instanceof ApiError ? e.message : "Алдаа гарлаа.", "error"));
      return;
    }
    if (this.info) await this.platform.enableBackground(this.info).catch(() => undefined);
    await this.load();
  }

  private async loadItems(): Promise<void> {
    this.items = byBankTime((await this.api.transactions()).items);
  }

  private renderMain(): void {
    const info = this.info;
    if (!info) return;
    const now = new Date();
    const top = topBar(info, now, info.pin_shifts ? () => void this.endShift() : null);
    this.refs.status = top.status;
    this.refs.clock = top.clockEl;
    this.refs.banner = h("div", { class: "banner banner-error", role: "alert", hidden: true }, "Холболт тасарсан — орлогын мэдэгдэл ирэхгүй байж болзошгүй");
    this.refs.sound = h(
      "button",
      { type: "button", class: "banner banner-sound", hidden: this.chime.unlocked, onclick: () => void this.unlockSound() },
      "Дуу идэвхгүй — дарж идэвхжүүлнэ үү",
    );
    this.feed = new Feed(info.multiple_accounts);
    this.refs.footer = h("footer", { class: "totals", hidden: !info.show_today_totals });
    // No account is shared by this device, this cashier and an active group: nothing can arrive.
    const noAccounts = info.accounts.length === 0
      ? message("Энэ ээлжинд харах данс алга тул гүйлгээ ирэхгүй. Админ «Бүлгүүд» хуудаснаас таны бүлэгт данс холбоно уу.", "error")
      : null;
    replace(
      this.root,
      top.el,
      this.refs.banner,
      this.refs.sound,
      noAccounts,
      this.feed.el,
      this.refs.footer,
    );
    this.refreshDynamic();
    void this.refreshTotals();
  }

  private refreshDynamic(): void {
    const info = this.info;
    if (!info || !this.feed) return;
    const now = new Date();
    const fresh = new Set<string>();
    for (const [id, until] of this.freshUntil) {
      if (until > now.getTime()) fresh.add(id);
      else this.freshUntil.delete(id);
    }
    this.feed.update(this.items, fresh, now);
    if (this.refs.clock) replace(this.refs.clock, clock(now));
  }

  private async refreshTotals(): Promise<void> {
    if (!this.info?.show_today_totals || !this.refs.footer) return;
    try {
      const t = await this.api.summaryToday();
      replace(this.refs.footer, `Өнөөдөр: ${t.count} гүйлгээ · ${t.total_text}`);
    } catch {
      this.refs.footer.hidden = true;
    }
  }

  private async unlockSound(): Promise<void> {
    if ((await this.chime.unlock()) && this.refs.sound) this.refs.sound.hidden = true;
    // Without a PIN shift this click is the only user gesture, so it also asks for notifications.
    if (this.info && !this.info.pin_shifts) await this.platform.enableBackground(this.info).catch(() => undefined);
  }

  private async endShift(): Promise<void> {
    await this.platform.disableBackground().catch(() => undefined);
    await this.api.endShift().catch(() => undefined);
    this.platform.notifier.clear();
    this.platform.attention.stop();
    await this.load();
  }

  // ------------------------------------------------------------ live updates

  private connect(): void {
    this.stream?.close();
    this.stream = this.platform.live({
      onTxn: (t) => this.receive(t, true),
      onTest: (t) => this.receiveTest(t),
      onShiftEnded: () => {
        this.platform.notifier.clear();
        void this.load();
      },
      onRevoked: () => {
        this.platform.notifier.clear();
        this.platform.attention.stop();
        replace(this.root, revokedScreen(() => this.go("/pair")));
      },
      onSettings: () => void this.load(),
      onStatus: (ok) => this.setConnected(ok),
      onReconnected: () => void this.catchUp(),
    });
    this.stream.connect();
  }

  private setConnected(ok: boolean): void {
    this.connected = ok;
    if (this.refs.status) {
      replace(this.refs.status, ok ? "Холбогдсон" : "Дахин холбогдож байна…");
      this.refs.status.className = ok ? "status ok" : "status wait";
    }
    if (ok) {
      if (this.disconnectTimer) clearTimeout(this.disconnectTimer);
      this.disconnectTimer = null;
      if (this.refs.banner) this.refs.banner.hidden = true;
    } else if (!this.disconnectTimer) {
      this.disconnectTimer = setTimeout(() => {
        if (this.connected) return;
        if (this.refs.banner) this.refs.banner.hidden = false;
        if (this.refs.status) {
          replace(this.refs.status, "Холболт тасарсан");
          this.refs.status.className = "status bad";
        }
      }, DISCONNECT_BANNER_MS);
    }
  }

  private async catchUp(): Promise<void> {
    const since = this.items.length ? Math.max(...this.items.map((t) => t.seq)) : undefined;
    try {
      const { items } = await this.api.transactions(since);
      for (const t of [...items].reverse()) this.receive(t, true);
    } catch {
      // The next reconnect tries again.
    }
  }

  receive(t: Txn, announce: boolean): void {
    if (this.items.some((x) => x.id === t.id)) return; // exactly-once on screen
    this.items = byBankTime([t, ...this.items]).slice(0, LIST_LIMIT);
    if (!t.late) this.freshUntil.set(t.id, Date.now() + HIGHLIGHT_MS);
    this.refreshDynamic();
    setTimeout(() => this.refreshDynamic(), HIGHLIGHT_MS + 50);
    void this.api.ack(t.id).catch(() => undefined);
    void this.refreshTotals();
    // A late arrival is an old payment: announcing it could be taken for the customer at the till.
    if (!announce || !this.info || t.late) return;
    this.platform.notifier.show(t, this.info.multiple_accounts);
    this.platform.attention.notify(`Орлого ${t.amount_text}`);
    void this.announce(t);
  }

  private async announce(t: Txn): Promise<void> {
    const info = this.info;
    const spoken = info?.voice_enabled && this.voice
      ? await this.voice.speak(t.amount, t.currency, info.voice_max_amount)
      : false;
    if (!spoken) this.chime.play();
  }

  private receiveTest(t: Partial<Txn>): void {
    this.platform.notifier.show({ ...t, test: true, time_text: clock(new Date()) }, false);
    this.chime.play();
  }

  // ------------------------------------------------------------ activity

  noteActivity(): void {
    if (!this.info?.shift || Date.now() - this.lastActivity < ACTIVITY_MS) return;
    this.lastActivity = Date.now();
    void this.api.activity().catch(() => undefined);
  }

  tick(): void {
    this.refreshDynamic();
    if (this.waitingForViewer) void this.load();
    // Safety net besides the live stream: if an event was lost (a worker restart, a dropped
    // connection that never errored), the next tick still brings the income in.
    else if (this.feed && this.info?.shift && this.connected) void this.catchUp();
  }

  // ------------------------------------------------------------ pairing + invites

  private showPair(token: string | undefined): void {
    const submit = async (input: { token?: string; code?: string }, out: HTMLElement | null) => {
      try {
        await this.api.pair(input);
        this.go("/");
      } catch (e) {
        const text = e instanceof ApiError ? e.message : "Алдаа гарлаа.";
        if (out) replace(out, message(text, "error"));
        else replace(this.root, pairScreen(this.platform.kind, (code, o) => void submit({ code }, o)), message(text, "error"));
      }
    };
    if (token) {
      replace(this.root, message("Төхөөрөмжийг холбож байна…"));
      void submit({ token }, null);
      return;
    }
    replace(this.root, pairScreen(this.platform.kind, (code, out) => void submit({ code }, out)));
  }

  private async showInvite(token: string | undefined): Promise<void> {
    const invalid = "Урилгын холбоос хүчингүй эсвэл хугацаа нь дууссан байна. Админаас шинэ урилга авна уу.";
    if (!token) {
      replace(this.root, message(invalid, "error"));
      return;
    }
    let view;
    try {
      view = await this.api.inviteInspect(token);
    } catch (e) {
      replace(this.root, message(e instanceof ApiError ? e.message : invalid, "error"));
      return;
    }
    const v = view;
    replace(
      this.root,
      inviteScreen(v.tenant_name, v.display_name, v.consent_text, async (pin, pin2, agreed, out) => {
        if (!agreed) return replace(out, message("Үргэлжлүүлэхийн тулд нөхцөлийг зөвшөөрнө үү.", "error"));
        if (pin !== pin2) return replace(out, message("ПИН код хоёр удаа ижил байх ёстой.", "error"));
        try {
          await this.api.inviteAccept(token, pin, v.consent_version);
          replace(this.root, message("Бүртгэл амжилттай боллоо. Кассын төхөөрөмж дээр нэрээ сонгож, ПИН кодоороо ээлжээ эхлүүлнэ үү."));
        } catch (e) {
          replace(out, message(e instanceof ApiError ? e.message : "Алдаа гарлаа.", "error"));
        }
      }),
    );
  }
}
