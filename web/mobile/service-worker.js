const CACHE_NAME = "devseek-mobile-v23";
const APP_SHELL = ["/", "/manifest.webmanifest", "/service-worker.js", "/mobile/icon.svg"];

function isAppShellRequest(requestUrl) {
  return requestUrl.origin === self.location.origin
    && (
      requestUrl.pathname === "/"
      || requestUrl.pathname.endsWith("/index.html")
      || requestUrl.pathname.endsWith("/manifest.webmanifest")
      || requestUrl.pathname.endsWith("/service-worker.js")
      || requestUrl.pathname.endsWith("/mobile/icon.svg")
    );
}

async function cacheResponse(request, response) {
  if (!response || response.status !== 200 || response.type !== "basic") {
    return response;
  }
  const clone = response.clone();
  const cache = await caches.open(CACHE_NAME);
  await cache.put(request, clone);
  return response;
}

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(APP_SHELL)),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((key) => key !== CACHE_NAME)
          .map((key) => caches.delete(key)),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") {
    return;
  }

  const requestUrl = new URL(event.request.url);
  if (requestUrl.origin !== self.location.origin) {
    return;
  }

  if (requestUrl.pathname.startsWith("/api/") || requestUrl.pathname.startsWith("/preview/")) {
    event.respondWith(fetch(event.request));
    return;
  }

  if (isAppShellRequest(requestUrl)) {
    event.respondWith(
      fetch(event.request)
        .then((response) => cacheResponse(event.request, response))
        .catch(() => caches.match(event.request)),
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      const networkFetch = fetch(event.request)
        .then((response) => cacheResponse(event.request, response));

      if (cachedResponse) {
        networkFetch.catch(() => {});
        return cachedResponse;
      }

      return networkFetch;
    }),
  );
});
