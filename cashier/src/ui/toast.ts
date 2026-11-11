import type { Txn } from "../api";
import { h, replace } from "../dom";

// In-page toast (SPEC §8.2): 360×112, bottom-right, accent bar, auto-dismiss (5–20 s, default 8),
// hover pauses the timer, a thin progress line shows the remaining time, at most 3 stacked and
// the rest collapse into "+N бусад".
export const MAX_VISIBLE = 3;

interface Entry {
  el: HTMLElement;
  remaining: number;
  started: number;
  timer: ReturnType<typeof setTimeout> | null;
}

export class ToastStack {
  private entries: Entry[] = [];
  private hidden = 0;
  private readonly more: HTMLElement;

  constructor(
    private readonly root: HTMLElement,
    private seconds: number,
    // The agent's overlay window resizes itself to its toasts and closes them on click.
    private readonly options: { onChange?: (visible: number) => void; dismissOnClick?: boolean } = {},
  ) {
    this.more = h("div", { class: "toast-more", hidden: true });
    this.root.append(this.more);
  }

  setSeconds(seconds: number): void {
    this.seconds = Math.min(20, Math.max(5, seconds));
  }

  push(txn: Partial<Txn>, showAlias: boolean): void {
    if (this.entries.length >= MAX_VISIBLE) {
      this.hidden += 1;
      replace(this.more, `+${this.hidden} бусад`);
      this.more.hidden = false;
      return;
    }
    const progress = h("div", { class: "toast-progress" });
    progress.style.animationDuration = `${this.seconds}s`;
    const el = h(
      "div",
      { class: "toast", role: "status" },
      h("p", { class: "toast-label" }, txn.test ? "Туршилт · Орлого" : "Орлого"),
      h("p", { class: "toast-amount" }, txn.amount_text ?? ""),
      h("p", { class: "toast-desc" }, txn.description ?? ""),
      h(
        "p",
        { class: "toast-meta" },
        h("span", {}, showAlias ? (txn.account_alias ?? "") : ""),
        h("span", {}, txn.time_text ?? ""),
      ),
      progress,
    );
    const entry: Entry = { el, remaining: this.seconds * 1000, started: Date.now(), timer: null };
    el.addEventListener("mouseenter", () => this.pause(entry));
    el.addEventListener("mouseleave", () => this.resume(entry));
    if (this.options.dismissOnClick) {
      el.addEventListener("click", () => {
        if (entry.timer) clearTimeout(entry.timer);
        this.dismiss(entry);
      });
    }
    this.root.append(el);
    this.entries.push(entry);
    this.resume(entry);
    this.options.onChange?.(this.entries.length);
  }

  private pause(entry: Entry): void {
    if (entry.timer) clearTimeout(entry.timer);
    entry.timer = null;
    entry.remaining -= Date.now() - entry.started;
    entry.el.classList.add("paused");
  }

  private resume(entry: Entry): void {
    entry.started = Date.now();
    entry.el.classList.remove("paused");
    entry.timer = setTimeout(() => this.dismiss(entry), Math.max(0, entry.remaining));
  }

  private dismiss(entry: Entry): void {
    entry.el.remove();
    this.entries = this.entries.filter((e) => e !== entry);
    if (this.entries.length === 0) {
      this.hidden = 0;
      this.more.hidden = true;
    }
    this.options.onChange?.(this.entries.length);
  }

  get visibleCount(): number {
    return this.entries.length;
  }

  clear(): void {
    for (const e of [...this.entries]) {
      if (e.timer) clearTimeout(e.timer);
      this.dismiss(e);
    }
  }
}
