// Installed app (PWA): the page's own files come from the cache when the network is
// slow or gone; the shop's data (/api) never goes into the cache, it is always live.
const CACHE = "shop-admin-v4";
const SHELL = ["/", "/i18n/catalog.js", "/static/admin.css", "/static/admin.js", "/static/inventory.js", "/static/business.js", "/static/projects.js", "/static/i18n.js",
  "/static/icon.svg", "/static/icon-192.png", "/manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin || !SHELL.includes(url.pathname)) return;
  // network first: a new version is used as soon as it is published
  e.respondWith(fetch(e.request).then((resp) => {
    if (resp.ok) { const copy = resp.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return resp;
  }).catch(() => caches.match(e.request)));
});
