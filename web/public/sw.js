// Deliberately does no caching -- this is a security-sensitive admin app
// (settings, credentials, process supervision), so serving stale
// authenticated content or bypassing an auth check is a real risk a naive
// cache-first service worker could introduce. This exists purely to
// satisfy the "has an active service worker with a fetch handler"
// installability requirement; every request still just goes to the
// network untouched.
self.addEventListener("install", () => {
  self.skipWaiting()
})

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim())
})

self.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request))
})
