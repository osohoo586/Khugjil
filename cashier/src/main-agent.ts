// Entry for the Windows desktop agent (Tauri main window). Same screens as the web cashier.
import "./styles.css";
import { CashierApp } from "./app";
import { wireWindow } from "./boot";
import { agentPlatform } from "./platform-agent";

const root = document.getElementById("app");
if (root) {
  const app = new CashierApp(root, agentPlatform());
  void app.start("/", "");
  wireWindow(app);
}
