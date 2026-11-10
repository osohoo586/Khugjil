/// <reference lib="webworker" />
// Service worker: app-shell cache, Web Push display and notification clicks. API responses are
// never cached.
const sw = globalThis as unknown as ServiceWorkerGlobalScope;
export {};

const CACHE = "khj-shell-v1";
const SHELL = ["/", "/manifest.webmanifest", "/icon.svg", "/shared/tokens.css"];

sw.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => sw.skipWaiting()));
});

sw.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => sw.clients.claim()),
  );
});

sw.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.origin !== sw.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;
  if (event.request.mode === "navigate") {
    event.respondWith(fetch(event.request).catch(async () => (await caches.match("/")) ?? Response.error()));
    return;
  }
  if (url.pathname.startsWith("/assets/") || url.pathname.startsWith("/shared/")) {
    event.respondWith(
      caches.match(event.request).then(
        (hit) =>
          hit ??
          fetch(event.request).then((response) => {
            if (response.ok) {
              const copy = response.clone();
              void caches.open(CACHE).then((c) => c.put(event.request, copy));
            }
            return response;
          }),
      ),
    );
  }
});

interface PushPayload {
  title?: string;
  body?: string;
  tag?: string;
}

sw.addEventListener("push", (event) => {
  const data = (event.data?.json() ?? {}) as PushPayload;
  event.waitUntil(
    (async () => {
      const windows = await sw.clients.matchAll({ type: "window", includeUncontrolled: true });
      // The page shows its own toast and plays the chime when it is focused.
      if (windows.some((w) => w.focused && w.visibilityState === "visible")) return;
      await sw.registration.showNotification(data.title ?? "Орлого", {
        body: data.body ?? "",
        tag: data.tag ?? "khj",
        icon: "/icon.svg",
        data: { url: "/" },
      });
    })(),
  );
});

sw.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    (async () => {
      const windows = await sw.clients.matchAll({ type: "window", includeUncontrolled: true });
      const existing = windows.find((w) => new URL(w.url).pathname === "/");
      if (existing) {
        await existing.focus();
        return;
      }
      await sw.clients.openWindow("/");
    })(),
  );
});
