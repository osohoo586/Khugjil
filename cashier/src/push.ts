import type { Api } from "./api";
import { serviceWorkerUrl } from "./tt";

export function urlBase64ToUint8Array(value: string): Uint8Array<ArrayBuffer> {
  const padded = (value + "=".repeat((4 - (value.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

export function pushSupported(): boolean {
  return "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

export async function registerServiceWorker(): Promise<void> {
  if (!("serviceWorker" in navigator)) return;
  try {
    await navigator.serviceWorker.register(serviceWorkerUrl(), { scope: "/" });
  } catch {
    // Without a service worker the page still works; only background notifications are lost.
  }
}

/** Must be called from a user gesture (the "Ээлж эхлүүлэх" click). */
export async function enablePush(api: Api, vapidKey: string | null): Promise<boolean> {
  if (!vapidKey || !pushSupported()) return false;
  const permission = Notification.permission === "default" ? await Notification.requestPermission() : Notification.permission;
  if (permission !== "granted") return false;
  const registration = await navigator.serviceWorker.ready;
  const existing = await registration.pushManager.getSubscription();
  const subscription =
    existing ??
    (await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(vapidKey),
    }));
  await api.pushSubscribe(subscription.toJSON());
  return true;
}

export async function disablePush(api: Api): Promise<void> {
  try {
    await api.pushUnsubscribe();
    if (!pushSupported()) return;
    const registration = await navigator.serviceWorker.ready;
    await (await registration.pushManager.getSubscription())?.unsubscribe();
  } catch {
    // The server already deleted the subscription when the shift ended.
  }
}
