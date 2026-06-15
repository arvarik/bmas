const CACHE_NAME = "bmas-swarm-cache-v2";
const STATIC_ASSETS = [
  "/",
  "/globals.css",
  "/views.css",
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

  // Bypass cache entirely for POST/PUT/DELETE and dynamic API/SSE routes
  if (url.pathname.startsWith("/api") || event.request.method !== "GET") {
    return;
  }

  // Handle caching for layout pages and static assets
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      if (cachedResponse) {
        // Asynchronous background refresh for document paths
        if (event.request.headers.get("accept")?.includes("text/html")) {
          fetch(event.request)
            .then((networkResponse) => {
              if (networkResponse.status === 200) {
                caches
                  .open(CACHE_NAME)
                  .then((cache) => cache.put(event.request, networkResponse));
              }
            })
            .catch(() => {});
        }
        return cachedResponse;
      }

      return fetch(event.request).then((response) => {
        // Cache static local assets dynamically upon discovery
        if (
          response &&
          response.status === 200 &&
          url.origin === self.location.origin &&
          !url.pathname.startsWith("/_next/webpack-hmr")
        ) {
          const responseToCache = response.clone();
          caches.open(CACHE_NAME).then((cache) => {
            cache.put(event.request, responseToCache);
          });
        }
        return response;
      });
    })
  );
});
