import { Api, ApiError, type DeviceInfo, type Txn } from "./api";
import { Chime } from "./audio";
import { h, replace } from "./dom";
import { clock, parseAmountInput } from "./format";
import { disablePush, enablePush } from "./push";
import { LiveStream } from "./stream";
import { TitleFlasher } from "./title";
import { ToastStack } from "./ui/toast";
import {
  HIGHLIGHT_MS,
  LIST_LIMIT,
  hero,
  inviteScreen,
  message,
  pairScreen,
  revokedScreen,
  shiftStart,
  topBar,
  txnList,
  verifyBox,
  verifyResult,
} from "./ui/screens";
import { loadVoice, type Voice } from "./voice";

const DISCONNECT_BANNER_MS = 5_000;
const ACTIVITY_MS = 5 * 60_000;

export class CashierApp {
  private info: DeviceInfo | null = null;
  private items: Txn[] = [];
  private highlightUntil = 0;
  private stream: LiveStream | null = null;
  private connected = true;
  private disconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private lastActivity = 0;
  private verifyTimer: ReturnType<typeof setTimeout> | null = null;
  private readonly chime = new Chime();
  private readonly title = new TitleFlasher("Орлого");
  private readonly toasts: ToastStack;
  private voice: Voice | null = null;
  private refs: { status?: HTMLElement; clock?: HTMLElement; banner?: HTMLElement; sound?: HTMLElement; hero?: HTMLElement; list?: HTMLElement; footer?: HTMLElement } = {};

  constructor(
    private readonly root: HTMLElement,
    toastRoot: HTMLElement,
    private readonly api: Api = new Api(),
  ) {
    this.toasts = new ToastStack(toastRoot, 8);
  }

  async start(path: string, hash: string): Promise<void> {
    const token = /(?:^|[#&])t=([A-Za-z0-9_-]{20,128})/.exec(hash)?.[1];
    if (hash) history.replaceState(null, "", path); // tokens never stay in the address bar
    if (path === "/pair") return this.showPair(token);
    if (path === "/invite") return this.showInvite(token);
    this.voice = await loadVoice();
    await this.load();
  }

  // ------------------------------------------------------------ loading + screens

  private async load(): Promise<void> {
    try {
      this.info = await this.api.device();
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        window.location.assign("/pair");
        return;
      }
      replace(this.root, message(e instanceof ApiError ? e.message : "Алдаа гарлаа.", "error"));
      return;
    }
    document.documentElement.dataset.accent = this.info.accent;
    this.toasts.setSeconds(this.info.toast_seconds);
    if (!this.info.shift) {
      this.stream?.close();
      this.stream = null;
      this.renderShiftStart(this.info);
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
    if (this.info) await enablePush(this.api, this.info.vapid_public_key).catch(() => false);
    await this.load();
  }

  private async loadItems(): Promise<void> {
    this.items = (await this.api.transactions()).items;
  }

  private renderMain(): void {
    const info = this.info;
    if (!info) return;
    const now = new Date();
    const top = topBar(info, now, () => void this.endShift());
    this.refs.status = top.status;
    this.refs.clock = top.clockEl;
    this.refs.banner = h("div", { class: "banner banner-error", role: "alert", hidden: true }, "Холболт тасарсан — орлогын мэдэгдэл ирэхгүй байж болзошгүй");
    this.refs.sound = h(
      "button",
      { type: "button", class: "banner banner-sound", hidden: this.chime.unlocked, onclick: () => void this.unlockSound() },
      "Дуу идэвхгүй — дарж идэвхжүүлнэ үү",
    );
    this.refs.hero = h("div");
    this.refs.list = h("div");
    this.refs.footer = h("footer", { class: "totals", hidden: !info.show_today_totals });
    const verify = verifyBox((value, out) => this.onVerifyInput(value, out));
    replace(
      this.root,
      top.el,
      this.refs.banner,
      this.refs.sound,
      h("main", { class: "grid" }, h("div", { class: "main-hero" }, this.refs.hero), verify, h("div", { class: "main-list" }, this.refs.list)),
      this.refs.footer,
    );
    this.refreshDynamic();
    void this.refreshTotals();
  }

  private refreshDynamic(): void {
    const info = this.info;
    if (!info || !this.refs.hero || !this.refs.list) return;
    const now = new Date();
    replace(this.refs.hero, hero(this.items[0] ?? null, info.multiple_accounts, now, Date.now() < this.highlightUntil));
    replace(this.refs.list, txnList(this.items, info.multiple_accounts));
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
  }

  private async endShift(): Promise<void> {
    await disablePush(this.api);
    await this.api.endShift().catch(() => undefined);
    this.toasts.clear();
    this.title.stop();
    await this.load();
  }

  // ------------------------------------------------------------ live updates

  private connect(): void {
    this.stream?.close();
    this.stream = new LiveStream({
      onTxn: (t) => this.receive(t, true),
      onTest: (t) => this.receiveTest(t),
      onShiftEnded: () => void this.load(),
      onRevoked: () => {
        this.toasts.clear();
        replace(this.root, revokedScreen());
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
    const since = this.items[0]?.seq;
    try {
      const { items } = await this.api.transactions(since);
      for (const t of [...items].reverse()) this.receive(t, true);
    } catch {
      // The next reconnect tries again.
    }
  }

  receive(t: Txn, announce: boolean): void {
    if (this.items.some((x) => x.id === t.id)) return; // exactly-once on screen
    this.items = [t, ...this.items].slice(0, LIST_LIMIT);
    this.highlightUntil = Date.now() + HIGHLIGHT_MS;
    this.refreshDynamic();
    setTimeout(() => this.refreshDynamic(), HIGHLIGHT_MS + 50);
    void this.api.ack(t.id).catch(() => undefined);
    void this.refreshTotals();
    if (!announce || !this.info) return;
    this.toasts.push(t, this.info.multiple_accounts);
    this.title.notify(`Орлого ${t.amount_text}`);
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
    this.toasts.push({ ...t, test: true, time_text: clock(new Date()) }, false);
    this.chime.play();
  }

  // ------------------------------------------------------------ verify box + activity

  private onVerifyInput(value: string, out: HTMLElement): void {
    this.noteActivity();
    if (this.verifyTimer) clearTimeout(this.verifyTimer);
    const amount = parseAmountInput(value);
    if (amount === null) {
      replace(out, value.trim() ? "Дүнгээ тоогоор оруулна уу." : "");
      out.className = "verify-result muted";
      return;
    }
    this.verifyTimer = setTimeout(async () => {
      const slow = setTimeout(() => verifyResult(out, null, null), 300);
      try {
        const { matches } = await this.api.verify(amount);
        verifyResult(out, matches, null);
      } catch (e) {
        verifyResult(out, null, e instanceof ApiError ? e.message : "Алдаа гарлаа.");
      } finally {
        clearTimeout(slow);
      }
    }, 300);
  }

  noteActivity(): void {
    if (!this.info?.shift || Date.now() - this.lastActivity < ACTIVITY_MS) return;
    this.lastActivity = Date.now();
    void this.api.activity().catch(() => undefined);
  }

  tick(): void {
    this.refreshDynamic();
  }

  // ------------------------------------------------------------ pairing + invites

  private showPair(token: string | undefined): void {
    const submit = async (input: { token?: string; code?: string }, out: HTMLElement | null) => {
      try {
        await this.api.pair(input);
        window.location.assign("/");
      } catch (e) {
        const text = e instanceof ApiError ? e.message : "Алдаа гарлаа.";
        if (out) replace(out, message(text, "error"));
        else replace(this.root, pairScreen((code, o) => void submit({ code }, o)), message(text, "error"));
      }
    };
    if (token) {
      replace(this.root, message("Төхөөрөмжийг холбож байна…"));
      void submit({ token }, null);
      return;
    }
    replace(this.root, pairScreen((code, out) => void submit({ code }, out)));
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
