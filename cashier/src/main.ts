import "./styles.css";
import { CashierApp } from "./app";
import { registerServiceWorker } from "./push";

const root = document.getElementById("app");
const toasts = document.getElementById("toasts");
if (root && toasts) {
  const app = new CashierApp(root, toasts);
  void registerServiceWorker();
  void app.start(window.location.pathname, window.location.hash);
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
