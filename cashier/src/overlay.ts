// Entry for the agent's overlay window: an always-on-top, non-activating stack of toasts
// (SPEC §8.2 look). Rust validates each note and forwards it here; text is rendered with
// textContent only. After every change the window is resized to its content, or hidden when empty.
import "./styles.css";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { OverlayNote } from "./platform-agent";
import { ToastStack } from "./ui/toast";

const root = document.getElementById("toasts");
if (root) {
  const report = () =>
    requestAnimationFrame(() => {
      const height = Math.ceil(root.getBoundingClientRect().height);
      void invoke("overlay_layout", { height: stack.visibleCount === 0 ? 0 : height }).catch(() => undefined);
    });
  const stack = new ToastStack(root, 8, { onChange: report, dismissOnClick: true });
  void listen<OverlayNote>("khj://overlay", (e) => stack.push(e.payload, true));
  void listen("khj://overlay-clear", () => stack.clear());
  void listen<number>("khj://overlay-seconds", (e) => stack.setSeconds(e.payload));
  void invoke("overlay_ready").catch(() => undefined);
}
