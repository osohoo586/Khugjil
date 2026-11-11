// Browser platform: cookie transport, EventSource, in-page toasts, title flashing, Web Push.
import { Api, cookieTransport } from "./api";
import type { Platform } from "./platform";
import { disablePush, enablePush } from "./push";
import { LiveStream } from "./stream";
import { TitleFlasher } from "./title";
import { ToastStack } from "./ui/toast";

export function webPlatform(toastRoot: HTMLElement, api: Api = new Api(cookieTransport)): Platform {
  const toasts = new ToastStack(toastRoot, 8);
  return {
    kind: "web",
    api,
    notifier: {
      show: (txn, showAlias) => toasts.push(txn, showAlias),
      setSeconds: (seconds) => toasts.setSeconds(seconds),
      clear: () => toasts.clear(),
    },
    attention: new TitleFlasher("Орлого"),
    live: (handlers) => new LiveStream(handlers),
    enableBackground: async (info) => {
      await enablePush(api, info.vapid_public_key).catch(() => false);
    },
    disableBackground: () => disablePush(api),
    navigate: (path) => window.location.assign(path),
  };
}
