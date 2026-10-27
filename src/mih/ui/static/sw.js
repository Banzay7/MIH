// Caches the app shell only. API answers are live data and always go to the network.
const SHELL = "mih-shell-v5";
const FILES = ["./", "index.html", "app.css", "app.js", "icon.svg", "manifest.webmanifest"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys()
    .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || /^\/(api|play|lists)\//.test(url.pathname)) return;
  // Network first, so an upgrade shows at once; the cache covers a flaky connection.
  e.respondWith(fetch(e.request).then((resp) => {
    if (resp.ok) { const copy = resp.clone(); caches.open(SHELL).then((c) => c.put(e.request, copy)); }
    return resp;
  }).catch(() => caches.match(e.request).then((r) => r || caches.match("index.html"))));
});
