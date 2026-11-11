import type { CashierApp } from "./app";

/** Clock tick, activity reports and the layout switch, shared by the web and agent entries. */
export function wireWindow(app: CashierApp): void {
  setInterval(() => app.tick(), 30_000);
  for (const type of ["pointerdown", "keydown"]) {
    window.addEventListener(type, () => app.noteActivity(), { passive: true });
  }
  const setLayout = () => {
    const w = window.innerWidth;
    document.body.dataset.layout = w >= 1024 ? "full" : w > 480 ? "medium" : "compact";
  };
  window.addEventListener("resize", setLayout);
  setLayout();
}
