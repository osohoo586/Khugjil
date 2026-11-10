import { txn } from "../dom.test";
import { MAX_VISIBLE, ToastStack } from "./toast";

describe("ToastStack", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("shows at most 3 and collapses the rest into +N бусад", () => {
    const root = document.createElement("div");
    const stack = new ToastStack(root, 8);
    for (let i = 0; i < 5; i += 1) stack.push(txn({ id: `t${i}` }), true);
    expect(stack.visibleCount).toBe(MAX_VISIBLE);
    expect(root.querySelector(".toast-more")?.textContent).toBe("+2 бусад");
    expect(root.querySelector(".toast-meta")?.textContent).toContain("Төв дэлгүүр");
    vi.advanceTimersByTime(8000);
    expect(stack.visibleCount).toBe(0);
    expect((root.querySelector(".toast-more") as HTMLElement).hidden).toBe(true);
  });

  it("pauses while hovered and labels test notifications", () => {
    const root = document.createElement("div");
    const stack = new ToastStack(root, 5);
    stack.push(txn({ test: true }), false);
    const el = root.querySelector(".toast") as HTMLElement;
    expect(el.textContent).toContain("Туршилт");
    vi.advanceTimersByTime(2000);
    el.dispatchEvent(new Event("mouseenter"));
    vi.advanceTimersByTime(10_000);
    expect(stack.visibleCount).toBe(1);
    el.dispatchEvent(new Event("mouseleave"));
    vi.advanceTimersByTime(3100);
    expect(stack.visibleCount).toBe(0);
  });

  it("clamps the duration to 5-20 s", () => {
    const stack = new ToastStack(document.createElement("div"), 8);
    stack.setSeconds(99);
    stack.push(txn(), false);
    vi.advanceTimersByTime(19_900);
    expect(stack.visibleCount).toBe(1);
    stack.clear();
    expect(stack.visibleCount).toBe(0);
  });
});
