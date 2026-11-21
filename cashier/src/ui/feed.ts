import type { Txn } from "../api";
import { h, replace } from "../dom";
import { relativeTime } from "../format";

/** The cashier sees only the latest few incomes, large (owner's choice, 2026-11-21). */
export const FEED_LIMIT = 5;
const MOVE_MS = 320;
const EASE = "cubic-bezier(0.2, 0.7, 0.2, 1)";

interface Row {
  el: HTMLLIElement;
  ago: HTMLElement;
  at: Date;
}

function reducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * Keyed list: rows are created once per transaction and kept, so a new income slides in at the
 * top while the others glide down (FLIP), and a minute tick only updates the "x мин өмнө" text.
 * Descriptions are untrusted and only ever inserted as text.
 */
export class Feed {
  readonly el: HTMLElement;
  private readonly list: HTMLOListElement;
  private readonly empty: HTMLElement;
  private readonly rows = new Map<string, Row>();
  private rendered = false;

  constructor(private readonly showAlias: boolean) {
    this.list = h("ol", { class: "feed-list" });
    this.empty = h("p", { class: "feed-empty" }, "Орлого орж ирэхэд энд харагдана.");
    this.el = h("main", { class: "feed", "aria-label": "Сүүлийн орлого", "aria-live": "polite" }, this.empty, this.list);
  }

  update(items: Txn[], fresh: ReadonlySet<string>, now: Date): void {
    const visible = items.slice(0, FEED_LIMIT);
    const animate = this.rendered && !reducedMotion();
    const before = new Map<string, number>();
    if (animate) for (const [id, row] of this.rows) before.set(id, row.el.getBoundingClientRect().top);

    const keep = new Set(visible.map((t) => t.id));
    for (const [id, row] of this.rows) {
      if (!keep.has(id)) {
        row.el.remove();
        this.rows.delete(id);
      }
    }
    const entered: HTMLElement[] = [];
    visible.forEach((t, i) => {
      let row = this.rows.get(t.id);
      if (!row) {
        row = this.row(t);
        this.rows.set(t.id, row);
        if (this.rendered) entered.push(row.el);
      }
      row.el.classList.toggle("latest", i === 0);
      row.el.classList.toggle("new", fresh.has(t.id));
      replace(row.ago, relativeTime(row.at, now));
      const at = this.list.children[i];
      if (at !== row.el) this.list.insertBefore(row.el, at ?? null);
    });
    this.empty.hidden = visible.length > 0;
    this.list.hidden = visible.length === 0;

    if (animate) {
      for (const el of entered) {
        el.animate?.(
          [{ opacity: 0, transform: "translateY(-12px)" }, { opacity: 1, transform: "none" }],
          { duration: MOVE_MS, easing: EASE },
        );
      }
      for (const [id, top] of before) {
        const row = this.rows.get(id);
        if (!row) continue;
        const dy = top - row.el.getBoundingClientRect().top;
        if (dy) row.el.animate?.([{ transform: `translateY(${dy}px)` }, { transform: "none" }], { duration: MOVE_MS, easing: EASE });
      }
    }
    this.rendered = true;
  }

  private row(t: Txn): Row {
    const at = new Date(t.txn_at);
    const ago = h("span", { class: "row-ago" });
    const desc = h("p", { class: "row-desc", title: t.description }, t.description || "Гүйлгээний утгагүй");
    const el = h(
      "li",
      { class: "row", "data-id": t.id },
      h("div", { class: "row-when" }, h("span", { class: "row-time" }, t.time_text), ago),
      h(
        "div",
        { class: "row-what" },
        desc,
        this.showAlias && t.account_alias ? h("p", { class: "row-alias" }, t.account_alias) : null,
      ),
      h("p", { class: "row-amount" }, t.amount_text),
    );
    if (!t.description) desc.classList.add("muted");
    return { el, ago, at };
  }
}
