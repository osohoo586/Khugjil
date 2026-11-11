import "./styles.css";
import { CashierApp } from "./app";
import { wireWindow } from "./boot";
import { webPlatform } from "./platform-web";
import { registerServiceWorker } from "./push";

const root = document.getElementById("app");
const toasts = document.getElementById("toasts");
if (root && toasts) {
  const app = new CashierApp(root, webPlatform(toasts));
  void registerServiceWorker();
  void app.start(window.location.pathname, window.location.hash);
  wireWindow(app);
}
