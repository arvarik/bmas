const CACHE_NAME = "bmas-swarm-cache-v4";
const STATIC_ASSETS = [
  "/ant-head.png",
  "/icon.png",
  "/apple-icon.png",
  "/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) => {
        return cache.addAll(STATIC_ASSETS);
      })
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => {
        return Promise.all(
          keys.map((key) => {
            if (key !== CACHE_NAME) {
              return caches.delete(key);
            }
          })
        );
      })
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);

  // Keep server data, event streams, and document responses on the network.
  // Cached HTML can reference an older client bundle after an update.
  if (
    url.origin !== self.location.origin ||
    url.pathname.startsWith("/api") ||
    event.request.method !== "GET" ||
    event.request.mode === "navigate" ||
    event.request.headers.get("accept")?.includes("text/html")
  ) {
    return;
  }

  const cacheable = STATIC_ASSETS.includes(url.pathname)
    || url.pathname.startsWith("/_next/static/");
  if (!cacheable) return;

  // Versioned Next.js assets and explicit icons are safe to cache first.
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) return cachedResponse;

      return fetch(event.request).then((response) => {
        if (response && response.status === 200) {
          const responseToCache = response.clone();
          return caches
            .open(CACHE_NAME)
            .then((cache) => cache.put(event.request, responseToCache))
            .then(() => response);
        }
        return response;
      });
    })
  );
});
