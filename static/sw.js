/* Glovebox service worker.
 *
 * - App shell (HTML/CSS/JS/fonts/icons) is precached so the app opens offline
 *   and installs cleanly.
 * - Shell files are served network-first (always fresh when online, cached
 *   copy when offline or the network is slow); fonts and icons are cache-first.
 * - /api/ requests are never intercepted or cached: vehicle data is always live.
 *
 * Bump VERSION when you change the precache list.
 */
const VERSION = 'glovebox-v5';
// Cache namespace. Bump this when the precache list below changes, or
// when app.js / styles.css ship breaking changes — the activate handler
// deletes any cache that doesn't match VERSION, so old shells are evicted on
// the next page load after the user upgrades.
const SCOPE = self.registration.scope; // works under a sub-path too
const abs = p => new URL(p, SCOPE).href;

// App shell: the minimum set of files needed to open the app offline.
// /api/ requests are deliberately NOT in here — vehicle data is always
// fetched live (see the fetch handler below). Every themed manifest and
// icon is precached so the PWA can install with the right icon for any
// accent + theme combination the user might pick.
const SHELL = [
  './', 'css/styles.css', 'js/app.js', 'manifest.webmanifest',
  'fonts/archivo-latin.woff2', 'fonts/inter-latin.woff2', 'fonts/jetbrainsmono-latin.woff2',
  ...["manifest-amber-dark.webmanifest", "manifest-amber-light.webmanifest", "manifest-blue-dark.webmanifest", "manifest-blue-light.webmanifest", "manifest-crimson-dark.webmanifest", "manifest-crimson-light.webmanifest", "manifest-teal-dark.webmanifest", "manifest-teal-light.webmanifest", "manifest-violet-dark.webmanifest", "manifest-violet-light.webmanifest"],
  ...["icons/amber-dark/icon-192-rounded.png", "icons/amber-dark/icon-512-rounded.png", "icons/amber-dark/maskable-512.png", "icons/amber-dark/apple-touch-icon.png", "icons/amber-light/icon-192-rounded.png", "icons/amber-light/icon-512-rounded.png", "icons/amber-light/maskable-512.png", "icons/amber-light/apple-touch-icon.png", "icons/blue-dark/icon-192-rounded.png", "icons/blue-dark/icon-512-rounded.png", "icons/blue-dark/maskable-512.png", "icons/blue-dark/apple-touch-icon.png", "icons/blue-light/icon-192-rounded.png", "icons/blue-light/icon-512-rounded.png", "icons/blue-light/maskable-512.png", "icons/blue-light/apple-touch-icon.png", "icons/crimson-dark/icon-192-rounded.png", "icons/crimson-dark/icon-512-rounded.png", "icons/crimson-dark/maskable-512.png", "icons/crimson-dark/apple-touch-icon.png", "icons/crimson-light/icon-192-rounded.png", "icons/crimson-light/icon-512-rounded.png", "icons/crimson-light/maskable-512.png", "icons/crimson-light/apple-touch-icon.png", "icons/teal-dark/icon-192-rounded.png", "icons/teal-dark/icon-512-rounded.png", "icons/teal-dark/maskable-512.png", "icons/teal-dark/apple-touch-icon.png", "icons/teal-light/icon-192-rounded.png", "icons/teal-light/icon-512-rounded.png", "icons/teal-light/maskable-512.png", "icons/teal-light/apple-touch-icon.png", "icons/violet-dark/icon-192-rounded.png", "icons/violet-dark/icon-512-rounded.png", "icons/violet-dark/maskable-512.png", "icons/violet-dark/apple-touch-icon.png", "icons/violet-light/icon-192-rounded.png", "icons/violet-light/icon-512-rounded.png", "icons/violet-light/maskable-512.png", "icons/violet-light/apple-touch-icon.png"],
].map(abs);

// install: precache the shell so the app opens offline on first visit.
// `cache: 'reload'` bypasses any stale browser cache so the precached
// copies are genuinely fresh. skipWaiting() activates the new SW
// immediately rather than waiting for all tabs to close.
self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(VERSION)
      .then(cache => cache.addAll(SHELL.map(u => new Request(u, { cache: 'reload' }))))
      .then(() => self.skipWaiting())
  );
});

// activate: evict any old cache versions (so we don't leak disk space
// forever) and claim existing clients so this SW takes over immediately.
self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== VERSION).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

const withTimeout = (promise, ms) =>
  new Promise((resolve, reject) => {
    const t = setTimeout(() => reject(new Error('timeout')), ms);
    promise.then(v => { clearTimeout(t); resolve(v); }, e => { clearTimeout(t); reject(e); });
  });

// Network-first with a 4s timeout: serve fresh when online, fall back to
// the cached copy when the network is slow or down. `fallbackUrl` lets
// navigation requests fall back to the cached shell (./) even when the
// URL has query params — `ignoreSearch: true` matches regardless of them.
async function networkFirst(request, fallbackUrl) {
  const cache = await caches.open(VERSION);
  try {
    const res = await withTimeout(fetch(request, { cache: 'no-cache' }), 4000);
    if (res.ok) cache.put(fallbackUrl || request, res.clone());
    return res;
  } catch (err) {
    const hit = await cache.match(fallbackUrl || request, { ignoreSearch: true });
    if (hit) return hit;
    throw err;
  }
}

// Cache-first: serve from cache immediately, only hit the network on a
// miss. Used for fonts and icons, which change rarely and benefit from
// zero-latency loads. A successful fetch is cached for next time.
async function cacheFirst(request) {
  const cache = await caches.open(VERSION);
  const hit = await cache.match(request);
  if (hit) return hit;
  const res = await fetch(request);
  if (res.ok) cache.put(request, res.clone());
  return res;
}

// Route every GET through the appropriate strategy:
//   - non-GET (POST/PATCH/DELETE) → bypass the SW entirely (we never cache writes)
//   - cross-origin → bypass (don't proxy third-party requests)
//   - /api/ → bypass (vehicle data is always live)
//   - navigation (the HTML shell) → network-first with the cached shell as fallback
//   - fonts/icons → cache-first (they're versioned and rarely change)
//   - everything else (js/css/manifests) → network-first so updates ship promptly
self.addEventListener('fetch', event => {
  const { request } = event;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.includes('/api/')) return; // live data: let the network handle it

  if (request.mode === 'navigate') {
    event.respondWith(networkFirst(request, abs('./')));
  } else if (/\/(fonts|icons)\//.test(url.pathname)) {
    event.respondWith(cacheFirst(request));
  } else {
    event.respondWith(networkFirst(request));
  }
});
