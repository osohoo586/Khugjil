import { h, replace } from "./dom";
import { FEED_LIMIT, Feed } from "./ui/feed";
import type { Txn } from "./api";

const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>';

export function txn(over: Partial<Txn> = {}): Txn {
  return {
    id: "t1", seq: 1, amount: "2800.00", currency: "MNT", amount_text: "2,800 ₮",
    description: "Худалдан авалт", txn_at: "2026-10-15T00:37:22Z", time_text: "08:37",
    datetime_text: "2026.10.15 08:37", account_alias: "Төв дэлгүүр", test: false, ...over,
  };
}

describe("untrusted descriptions are rendered as text only", () => {
  it("h() inserts strings as text nodes", () => {
    const el = h("p", {}, evil);
    expect(el.textContent).toBe(evil);
    expect(el.querySelector("img, script")).toBeNull();
  });
  it("the feed never creates elements from descriptions", () => {
    const feed = new Feed(true);
    feed.update([txn({ description: evil })], new Set(), new Date());
    expect(feed.el.querySelector("img, script")).toBeNull();
    expect(feed.el.textContent).toContain(evil);
    expect((window as { pwned?: number }).pwned).toBeUndefined();
  });
  it("rejects string event handlers", () => {
    expect(() => h("p", { onclick: "alert(1)" as unknown as EventListener })).toThrow(TypeError);
  });
  it("replace() swaps children", () => {
    const el = h("div", {}, "a");
    replace(el, "b", null, false, 3);
    expect(el.textContent).toBe("b3");
  });
});

describe("feed", () => {
  const now = new Date("2026-10-15T00:40:00Z");
  const many = Array.from({ length: 8 }, (_, i) => txn({ id: `t${i}`, seq: 8 - i, amount_text: `${i + 1},000 ₮` }));

  it("shows only the latest five, newest first and marked, with time and a short age", () => {
    const feed = new Feed(false);
    feed.update(many, new Set(["t0"]), now);
    const rows = feed.el.querySelectorAll(".row");
    expect(rows).toHaveLength(FEED_LIMIT);
    expect(rows[0]?.className).toBe("row latest new");
    expect(rows[1]?.className).toBe("row");
    expect(rows[0]?.querySelector(".row-amount")?.textContent).toBe("1,000 ₮");
    expect(rows[0]?.querySelector(".row-time")?.textContent).toBe("08:37");
    expect(rows[0]?.querySelector(".row-ago")?.textContent).toBe("3 мин өмнө");
    expect(rows[0]?.querySelector(".row-desc")?.getAttribute("title")).toBe("Худалдан авалт");
    expect(feed.el.querySelector(".row-alias")).toBeNull();
  });

  it("keeps rows across updates: a new income goes on top, the oldest drops off", () => {
    const feed = new Feed(true);
    feed.update(many.slice(1), new Set(), now);
    const kept = feed.el.querySelector('[data-id="t1"]');
    feed.update(many, new Set(["t0"]), now);
    expect(feed.el.querySelector('[data-id="t1"]')).toBe(kept);
    expect([...feed.el.querySelectorAll(".row")].map((r) => r.getAttribute("data-id"))).toEqual(["t0", "t1", "t2", "t3", "t4"]);
    expect(feed.el.querySelector(".row-alias")?.textContent).toBe("Төв дэлгүүр");
    feed.update(many, new Set(), now);
    expect(feed.el.querySelector(".row")?.className).toBe("row latest");
  });

  it("empty state, and a placeholder for an income without a description", () => {
    const feed = new Feed(false);
    feed.update([], new Set(), now);
    expect(feed.el.querySelector<HTMLElement>(".feed-empty")?.hidden).toBe(false);
    expect(feed.el.querySelector<HTMLElement>(".feed-list")?.hidden).toBe(true);
    feed.update([txn({ description: "" })], new Set(), now);
    expect(feed.el.querySelector<HTMLElement>(".feed-empty")?.hidden).toBe(true);
    expect(feed.el.querySelector(".row-desc")?.className).toBe("row-desc muted");
  });
});
