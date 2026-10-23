/* Glovebox SPA — vanilla JS, no build step.
 *
 * Architecture: a single page that swaps content into #view based on the
 * hash (#/dashboard, #/fuel, #/service, #/reminders, #/settings). All data
 * comes from /api/* endpoints (see app/main.py). No framework, no build
 * step, no virtual DOM — the render functions build HTML strings and swap
 * them in with one DOM operation.
 *
 * Conventions:
 *   - `state` is the single source of truth for the loaded view's data.
 *   - `render()` is the only thing that touches #view. View functions build
 *     into a detached <div>; render() swaps it in with showView().
 *   - All user actions go through a single delegated click handler at the
 *     bottom of the file, dispatched by `data-action` attributes.
 */
'use strict';

/* Same-origin by default. The preview injects a proxy base via index.html;
 * an unreplaced token (shipped source) falls back to same-origin so the app
 * works at any LAN hostname. */
let API_BASE = window.GLOVEBOX_API_BASE || '';
if (API_BASE.includes('__PORT')) API_BASE = '';

/* The single source of truth for the current view.
 *   - `vid` is the selected vehicle's id (null during onboarding)
 *   - `view` is the current route name (one of VIEWS)
 *   - `data` caches the last API response per view so re-renders after
 *     add/edit/delete don't refetch unnecessarily
 *   - `chartMode`/`chartRange` are dashboard chart preferences
 *   - `accent`/`version` come from /api/settings on boot
 */
const state = {
  chartMode: 'spending',
  chartRange: 12,
  version: null,
  vehicles: [],
  vid: null,
  view: 'dashboard',
  data: {},
};

/* ------------------------------------------------------------------ utils */
/* Small DOM + formatting helpers used everywhere. $ / $$ are querySelector
 * shorthands; esc() escapes user-entered strings before they go into
 * template literals (defense against XSS through stored data); the fmt*
 * helpers format numbers/dates/money consistently with the user's locale. */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

function esc(s) {
  // Escape HTML special chars. Used on every value we interpolate into a
  // template literal — user-entered descriptions/notes/stations could
  // otherwise inject markup. The replace map is a single-pass lookup.
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(path, opts = {}) {
  // Thin fetch wrapper that throws on non-2xx and returns parsed JSON (or
  // null for 204). The error message is pulled from the JSON `detail` field
  // when present, so the UI shows the server's actual validation message
  // instead of a generic "request failed".
  const res = await fetch(API_BASE + '/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try { const j = await res.json(); detail = typeof j.detail === 'string' ? j.detail : 'Check the form fields.'; } catch {}
    throw new Error(detail);
  }
  return res.status === 204 ? null : res.json();
}

function toast(msg, err = false) {
  // Auto-dismissing transient message at the bottom of the screen. The
  // `err` flag colours it red (used for catch blocks in the action handler).
  const el = document.createElement('div');
  el.className = 'toast' + (err ? ' err' : '');
  el.textContent = msg;
  $('#toast-root').appendChild(el);
  setTimeout(() => el.remove(), 3200);
}

const fmtMoney = (v, cur = '$') => v == null ? '—' : cur + Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtNum = (v, d = 1) => v == null ? '—' : Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d });
const fmtInt = v => v == null ? '—' : Number(v).toLocaleString('en-US');
const fmtDate = iso => iso ? new Date(iso + 'T00:00:00').toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' }) : '—';
// Human-readable file size for attachment chips. B / KB / MB only — attachments
// are capped at 25 MB on the server, so GB+ is never needed.
function fmtSize(bytes) {
  if (bytes == null) return '—';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

function vLabel() {
  // Return the currently selected vehicle's record (or a default gas-US
  // vehicle when none is selected yet, so unit helpers don't NPE during
  // onboarding). The dUnit/fUnit/isElectric helpers below all go through this.
  const v = state.vehicles.find(x => x.id === state.vid);
  return v || { currency: '$', distance_unit: 'mi', fuel_unit: 'gal' };
}
const FUEL_UNIT_LABELS = { gal: 'gal', l: 'L', gal_uk: 'imp gal', kwh: 'kWh' };
// File types accepted by the attachment picker. Mirrors the server's allowlist
// (ALLOWED_MIME / ALLOWED_EXT in app/main.py) so the OS file dialog only offers
// selectable types; the server still validates, so a bypassed picker is safe.
const ATTACH_ACCEPT = '.png,.jpg,.jpeg,.gif,.webp,.pdf,.txt,.csv,image/*,application/pdf,text/plain,text/csv';
const dUnit = () => vLabel().distance_unit === 'km' ? 'km' : 'mi';
const fUnit = () => FUEL_UNIT_LABELS[vLabel().fuel_unit] || 'gal';
const isElectric = (v) => (v || vLabel()).fuel_type === 'electric';
const isElectricById = (vid) => {
  // Like isElectric but takes an explicit vid — used by chartModes() and
  // updateNavLabels() which may need to check a vehicle that isn't the
  // current one (e.g. when the dropdown changes).
  const v = state.vehicles.find(x => x.id === vid);
  return v ? v.fuel_type === 'electric' : false;
};
// Vehicle-type-aware nouns for the fuel/energy log. Returns the right word
// for buttons, headers, and hints so EVs get "charge" and gas gets "fill-up".
function evTerms(v) {
  const ev = (v || vLabel()).fuel_type === 'electric';
  return ev ? {
    addLabel: 'Add charge',
    logTitle: 'Energy',
    countNoun: 'charges',
    fillNoun: 'charge',
    fullLabel: 'Charged to full',
    fullBadge: 'Full',
    partialHint: 'Leave unchecked for partial charges — efficiency is only computed between full charges.',
    missedLabel: 'I missed logging a charge before this one',
    missedHint: 'Restarts efficiency tracking at this entry so the gap doesn\'t inflate your numbers.',
    stationLabel: 'Charging location (optional)',
    stationPlaceholder: 'Tesla Supercharger · Home',
    qtyLabel: 'Quantity',
    tankCol: 'Charge',
    spendLabel: 'Energy cost',
    efficiencyName: 'Efficiency',
  } : {
    addLabel: 'Add fill-up',
    logTitle: 'Fuel',
    countNoun: 'fill-ups',
    fillNoun: 'fill-up',
    fullLabel: 'Filled the tank full',
    fullBadge: 'Full',
    partialHint: 'Leave unchecked for partial fills — economy is only computed between full tanks.',
    missedLabel: 'I missed logging a fill-up before this one',
    missedHint: 'Restarts economy tracking at this entry so the gap doesn\'t inflate your numbers.',
    stationLabel: 'Station (optional)',
    stationPlaceholder: 'Shell · Orlando',
    qtyLabel: 'Quantity',
    tankCol: 'Tank',
    spendLabel: 'Fuel cost',
    efficiencyName: 'Fuel Efficiency',
  };
}
const dUnitFor = v => v.distance_unit === 'km' ? 'km' : 'mi';
const fUnitFor = v => FUEL_UNIT_LABELS[v.fuel_unit] || 'gal';

/* ------------------------------------------------------------------ theme */
/* Light/dark theme. Three sources of truth, in priority order:
 *   1. The user's explicit in-app choice (stored in localStorage 'glovebox-theme')
 *   2. The OS prefers-color-scheme (live-updating when the user hasn't chosen)
 *   3. The default (light)
 * The theme drives a data-theme attribute on <html>, which the CSS variables
 * in styles.css react to. Themed icons (favicon, manifest, etc.) are swapped
 * in updateAppIcons() so they match.
 */

function currentTheme() {
  return document.documentElement.getAttribute('data-theme') ||
    (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
}

function currentIconFolder() {
  // The folder name is `<accent>-<theme>`, e.g. "blue-dark". This matches
  // the static/icons/<accent>-<theme>/ directory layout and the per-theme
  // manifest files (manifest-<accent>-<theme>.webmanifest).
  return `${state.accent || 'blue'}-${currentTheme()}`;
}

function updateAppIcons() {
  const folder = currentIconFolder();
  const icon = `icons/${folder}/icon-192-rounded.png`;
  const fav = $('#favicon');
  if (fav) fav.setAttribute('href', icon);
  const apple = $('#apple-touch-icon');
  if (apple) apple.setAttribute('href', `icons/${folder}/apple-touch-icon.png`);
  const manifest = $('#app-manifest');
  if (manifest) manifest.setAttribute('href', `manifest-${folder}.webmanifest`);
  $$('[data-app-icon]').forEach(img => img.setAttribute('src', icon));
}

const THEME_KEY = 'glovebox-theme';
const ACCENT_KEY = 'glovebox-accent';
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } },
};

(function initTheme() {
  const mq = matchMedia('(prefers-color-scheme: dark)');
  // An explicit in-app choice wins; otherwise follow the OS (live).
  const saved = store.get(THEME_KEY);
  let dark = saved ? saved === 'dark' : mq.matches;
  const apply = () => {
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light');
    updateAppIcons();
    // Keep the browser/status-bar colour in step with the in-app theme toggle.
    const chrome = dark ? '#141413' : '#f4f3ef';
    $$('meta[name="theme-color"]').forEach(m => m.setAttribute('content', chrome));
    const t = $('#theme-toggle');
    if (t) t.setAttribute('aria-label', 'Switch to ' + (dark ? 'light' : 'dark') + ' mode');
  };
  mq.addEventListener('change', e => {
    if (store.get(THEME_KEY)) return; // user chose explicitly
    dark = e.matches;
    apply();
  });
  document.addEventListener('DOMContentLoaded', () => {
    $('#theme-toggle').addEventListener('click', () => {
      dark = !dark;
      store.set(THEME_KEY, dark ? 'dark' : 'light');
      apply();
    });
    apply();
  });
})();

/* ------------------------------------------------------------------ accent */
/* The accent color is instance-wide (one choice for everyone using this
 * Glovebox). The five options match the icon theme folders in static/icons/
 * and the manifest files — adding a new accent means adding all three. */

const ACCENTS = [
  ['amber', 'Amber', '#e8b04b'], ['teal', 'Teal', '#55c4b8'], ['blue', 'Blue', '#6da8ee'],
  ['crimson', 'Crimson', '#e88585'], ['violet', 'Violet', '#b795ea'],
];
const DEFAULT_ACCENT = 'blue';

function applyAccent(name) {
  // Set the accent and persist it to localStorage (so the inline script in
  // index.html can pick it up on the next first paint). Falls back to the
  // default if `name` isn't a known accent.
  const known = ACCENTS.find(a => a[0] === name) || ACCENTS.find(a => a[0] === DEFAULT_ACCENT);
  state.accent = known[0];
  store.set(ACCENT_KEY, known[0]);
  document.documentElement.setAttribute('data-accent', known[0]);
  updateAppIcons();
}

/* ------------------------------------------------------------------ router */

const VIEWS = ['dashboard', 'fuel', 'service', 'reminders', 'settings'];

// Deep-link query keys the app knows how to act on. The Quick-log QR code
// encodes `#/fuel?add-fuel=1&vehicle=N`; a normal visit to `#/fuel` (no
// query) does nothing special. We parse the hash query ourselves because
// the browser's Location API doesn't expose it (URL hashes don't have a
// searchParams).
//
// `add-fuel` opens the Add fill-up modal. `vehicle` preselects a vehicle —
// useful when each vehicle has its own QR code, so scanning the EV's QR
// opens the EV with the Add charge modal ready.
const DEEP_LINK_KEYS = new Set(['add-fuel', 'vehicle']);

function deepLinkRequested(key) {
  if (!DEEP_LINK_KEYS.has(key)) return false;
  const q = location.hash.split('?')[1] || '';
  return new URLSearchParams(q).get(key) === '1';
}

function deepLinkValue(key) {
  if (!DEEP_LINK_KEYS.has(key)) return null;
  const q = location.hash.split('?')[1] || '';
  return new URLSearchParams(q).get(key);
}

function stripDeepLink(key) {
  if (!DEEP_LINK_KEYS.has(key)) return;
  const parts = location.hash.split('?');
  if (parts.length < 2) return;
  const params = new URLSearchParams(parts[1]);
  if (!params.has(key)) return;
  params.delete(key);
  const rest = params.toString();
  // history.replaceState updates the URL silently (no hashchange, no
  // re-render), so the modal we just opened isn't torn down and the
  // clean URL won't trigger a re-open on the next refresh.
  history.replaceState(null, '', '#' + parts[0].slice(1) + (rest ? '?' + rest : ''));
}

function route() {
  // Hash-based router. The hash is `#/<view>?<query>`; the view name is
  // everything up to `?`, and the query is parsed separately (deep links).
  // If the hash is empty or unknown, fall back to the dashboard.
  //
  // The vehicle preselect from a deep-link `?vehicle=N` is applied here so
  // the dropdown is synced before the first render — otherwise the dropdown
  // would briefly show the alphabetically-first vehicle, then snap to N.
  const hash = location.hash.replace('#/', '').split('?')[0] || 'dashboard';
  state.view = VIEWS.includes(hash) ? hash : 'dashboard';
  // Apply a vehicle preselect from the URL (e.g. from a vehicle-specific
  // QR code). The dropdown handler strips this once the user manually picks
  // a different vehicle, so refresh respects the user's last explicit choice
  // — not a stale URL hint. The dropdown is synced here too, otherwise the
  // URL could change `state.vid` while the dropdown still shows the
  // loadVehicles() default (which is the alphabetically-first vehicle).
  const vehicleParam = deepLinkValue('vehicle');
  if (vehicleParam != null) {
    const vid = Number(vehicleParam);
    if (state.vehicles.some(v => v.id === vid)) {
      state.vid = vid;
      const sel = $('#vehicle-select');
      if (sel) sel.value = String(vid);
    }
  }
  $$('.tab, .bottom-nav a').forEach(a => {
    const on = a.dataset.view === state.view;
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  });
  render();
}

/* Rendering rules (these are what keep tab switches smooth):
 *  1. The previous page stays on screen until the next one is ready, so the
 *     page never collapses to a placeholder and re-expands (no layout shift).
 *  2. A skeleton appears only if the request is slow (>250ms) and the view is
 *     actually changing.
 *  3. Each render builds into a detached node and swaps in with one DOM
 *     operation. A newer render cancels older ones, so fast taps (or slow
 *     responses arriving out of order) can never leave the wrong page showing.
 *  4. Scroll resets to the top only when the view changes, never on a
 *     re-render after add/edit/delete, so your place in a list is kept.
 *
 * The renderToken / shownView module-level vars make the cancellation work:
 * every render() bumps renderToken; if a newer render has started by the
 * time this one finishes, the older one's DOM swap is skipped.
 */
let renderToken = 0;
let skeletonTimer = 0;
let shownView = null;

function showView(view, node, viewChanged) {
  // Atomic DOM swap: replace #view's children with the new node's children.
  // If the view changed, scroll to top and replay the fade-in animation
  // (the `void offsetWidth` is the standard trick to retrigger a CSS animation).
  view.replaceChildren(...node.childNodes);
  view.removeAttribute('aria-busy');
  if (viewChanged) {
    window.scrollTo({ top: 0, left: 0, behavior: 'instant' });
    view.classList.remove('view-enter');
    void view.offsetWidth; // restart the one-shot fade
    view.classList.add('view-enter');
  }
}

async function render() {
  // The single render entry point. Cancellable via renderToken so a newer
  // render() (e.g. user tapped a tab while the previous one was loading)
  // wins. Shows a skeleton after 250ms if the request is slow — the delay
  // avoids skeleton flicker on fast network round-trips.
  const view = $('#view');
  const token = ++renderToken;
  const viewChanged = shownView !== state.view;
  clearTimeout(skeletonTimer);
  view.setAttribute('aria-busy', 'true');

  if (viewChanged && shownView !== null) {
    skeletonTimer = setTimeout(() => {
      if (token !== renderToken) return;
      const sk = document.createElement('div');
      sk.className = 'card skeleton';
      sk.style.height = '220px';
      view.replaceChildren(sk);
      window.scrollTo({ top: 0, left: 0, behavior: 'instant' }); // so the placeholder isn't stranded mid-page
    }, 250);
  }

  const buf = document.createElement('div');
  try {
    if (state.vehicles.length === 0 && state.view !== 'settings') {
      clearTimeout(skeletonTimer);
      view.innerHTML = onboardingHtml();
      view.removeAttribute('aria-busy');
      shownView = state.view;
      submitModal(async d => {
        await api('/vehicles', { method: 'POST', body: cleanVehicle(d) });
        await loadVehicles();
        toast('Vehicle added');
        render();
      });
      return;
    }
    if (state.view === 'dashboard') await renderDashboard(buf);
    else if (state.view === 'fuel') await renderFuel(buf);
    else if (state.view === 'service') await renderService(buf);
    else if (state.view === 'reminders') await renderReminders(buf);
    else renderSettings(buf);
  } catch (e) {
    buf.innerHTML = `<div class="empty-state">
      <h3>Something went wrong</h3><p>${esc(e.message)}</p></div>`;
  }
  if (token !== renderToken) return; // a newer render took over
  clearTimeout(skeletonTimer);
  shownView = state.view;
  showView(view, buf, viewChanged);

  // Deep-link: ?add-fuel=1 (the query the Quick-log QR code encodes) opens
  // the Add fill-up modal automatically on the Fuel page. The query stays in
  // the URL until the user closes the modal (closeModal strips it), so a
  // refresh while the modal is open reopens it; once closed, the URL is
  // clean and refresh / re-navigation won't reopen. Normal visits to #/fuel
  // (no query) never trigger this. We also skip if a modal is already open
  // for the SAME vehicle — but if it's open for a different vehicle (e.g.
  // the user navigated from one vehicle's QR URL to another's via hashchange)
  // we close and reopen it so the form labels and units match the new vehicle.
  if (state.view === 'fuel' && deepLinkRequested('add-fuel')) {
    const existing = document.querySelector('.modal[role="dialog"]');
    if (!existing) {
      openAddFuelModal();
    } else {
      // A modal is already open. Reopen only if the vehicle changed (the
      // user navigated between two vehicle-specific QR URLs without closing
      // the first). We don't reopen on every render() — that would tear
      // down a form the user is in the middle of filling out.
      const currentVid = deepLinkValue('vehicle');
      const expectedVid = currentVid != null ? Number(currentVid) : state.vid;
      const currentVehicleName = existing.getAttribute('aria-label') || '';
      const v = state.vehicles.find(x => x.id === expectedVid);
      const expectedLabel = v ? evTerms(v).addLabel : evTerms().addLabel;
      if (currentVehicleName !== expectedLabel) {
        openAddFuelModal();
      }
    }
  }
}

async function loadVehicles(selectId) {
  // Fetch the vehicle list and populate the dropdown. If the previously
  // selected vehicle is no longer present (deleted, or first load), fall
  // back to selectId (if provided) or the first vehicle in the list. Also
  // updates the Fuel/Energy nav label to match the new selection.
  state.vehicles = await api('/vehicles');
  const sel = $('#vehicle-select');
  sel.innerHTML = state.vehicles.map(v => {
    const label = [v.year, v.make, v.model].filter(Boolean).join(' ') || v.name;
    return `<option value="${v.id}">${esc(label)}${v.archived ? ' (archived)' : ''}</option>`;
  }).join('');
  if (!state.vehicles.some(v => v.id === state.vid)) {
    state.vid = selectId ?? state.vehicles[0]?.id ?? null;
  }
  if (state.vid != null) sel.value = String(state.vid);
  sel.disabled = state.vehicles.length === 0;
  updateNavLabels();
}

/* Update the Fuel/Energy tab label (top-nav and bottom-nav) to match the
 * currently selected vehicle's fuel_type. Gas vehicles show "Fuel", electric
 * vehicles show "Energy". The Records tab label is static and never changes.
 * The route name (#/fuel) and view name ('fuel') are NOT renamed — only the
 * user-visible label is. */
function updateNavLabels() {
  const label = isElectricById(state.vid) ? 'Energy' : 'Fuel';
  const topTab = document.getElementById('tab-fuel');
  if (topTab) topTab.textContent = label;
  const botLink = document.querySelector('.bottom-nav a[data-view="fuel"]');
  if (botLink) {
    // The bottom-nav link wraps an SVG icon plus a text node; update only
    // the trailing text node so we don't touch the icon markup.
    const textNode = [...botLink.childNodes].find(n => n.nodeType === Node.TEXT_NODE);
    if (textNode) textNode.nodeValue = label;
  }
}

/* ------------------------------------------------------------------ shared */
/* Small helpers shared across views: pageHead (title + actions row), the
 * ICONS map (inline SVG strings — no icon font, no extra HTTP), and
 * emptyState (the friendly placeholder shown when a list is empty). */

function pageHead(title, sub, actions = '') {
  return `<div class="page-head">
    <div><h1 class="page-title">${esc(title)}</h1>
    ${sub ? `<p class="page-sub">${esc(sub)}</p>` : ''}</div>
    <div class="btn-row">${actions}</div>
  </div>`;
}

// Inline SVG icons. Keeping them as strings avoids an icon-font dependency
// and lets us style them with currentColor. The `qr` and `print` icons are
// used by the Quick-log QR feature; the rest are the standard UI icons.
const ICONS = {
  download: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>',
  swap: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m17 3 4 4-4 4"/><path d="M3 7h18"/><path d="m7 21-4-4 4-4"/><path d="M21 17H3"/></svg>',
  car: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 17h2c.6 0 1-.4 1-1v-3c0-.9-.7-1.7-1.5-1.9C18.7 10.6 16 10 16 10s-1.3-1.4-2.2-2.3c-.5-.4-1.1-.7-1.8-.7H5c-.6 0-1.1.4-1.4.9l-1.4 2.9A3.7 3.7 0 0 0 2 12v4c0 .6.4 1 1 1h2"/><circle cx="7" cy="17" r="2"/><path d="M9 17h6"/><circle cx="17" cy="17" r="2"/></svg>',
  clipboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="8" height="4" x="8" y="2" rx="1" ry="1"/><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><path d="M12 11h4"/><path d="M12 16h4"/><path d="M8 11h.01"/><path d="M8 16h.01"/></svg>',
  fuel: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 22V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v16"/><path d="M15 10h2a2 2 0 0 1 2 2v6a1.5 1.5 0 0 0 3 0V9l-4-4"/><path d="M4 10h10"/><path d="M7 6v16"/></svg>',
  wrench: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94L6.5 19.5a2.12 2.12 0 0 1-3-3L12 10a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>',
  bell: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>',
  gauge: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 14 4-4"/><path d="M3.34 19a10 10 0 1 1 17.32 0"/></svg>',
  edit: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z"/></svg>',
  trash: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>',
  qr: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><path d="M14 14h3v3"/><path d="M21 14v.01"/><path d="M14 21h3"/><path d="M21 18v3"/><path d="M18 18v.01"/></svg>',
  print: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M6 9V2h12v7"/><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"/><rect x="6" y="14" width="12" height="8" rx="1"/></svg>',
  paperclip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>',
  file: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
};

function emptyState(icon, title, text, action) {
  return `<div class="empty-state">
    <div class="empty-icon">${icon}</div>
    <h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
}

/* ------------------------------------------------------------------ dashboard */
/* The dashboard is the default view: KPI tiles, a 12/24-month chart, upcoming
 * reminders, and recent activity. The chart has three modes (spending,
 * distance, efficiency) which the user toggles with the chart-toggle button.
 * For EVs the efficiency mode is labeled "Efficiency" (mi/kWh); for gas it's
 * "Fuel Efficiency" (mpg) — see chartModes() below.
 */

async function renderDashboard(view) {
  const s = state.data.summary = await api(`/vehicles/${state.vid}/summary?months=${state.chartRange}`);
  const v = s.vehicle;
  const econ = s.avg_economy;
  const ev = isElectric(v);
  const energyNoun = ev ? 'Energy' : 'Fuel';
  const sessionNoun = ev ? 'charge' : 'fill-up';
  const CHART_MODES = chartModes();

  const tiles = `
  <div class="grid grid-tiles">
    <div class="card stat-tile"><div class="stat-label">Latest odometer</div>
      <div class="stat-value">${fmtInt(s.latest_odometer)}<span class="stat-unit">${dUnit()}</span></div>
      <div class="stat-foot">highest recorded reading</div></div>
    <div class="card stat-tile"><div class="stat-label">${ev ? 'Avg efficiency' : 'Avg economy'}</div>
      <div class="stat-value">${econ == null ? '—' : fmtNum(econ)}<span class="stat-unit">${esc(s.economy_label)}</span></div>
      <div class="stat-foot">over ${fmtInt(s.distance_logged)} ${dUnit()} logged</div></div>
    <div class="card stat-tile"><div class="stat-label">Logged spending</div>
      <div class="stat-value">${fmtMoney(s.spend.total, v.currency)}</div>
      <div class="stat-foot">${energyNoun.toLowerCase()} ${fmtMoney(s.spend.fuel, v.currency)} · service ${fmtMoney(s.spend.service, v.currency)}</div></div>
    <div class="card stat-tile"><div class="stat-label">${energyNoun} cost</div>
      <div class="stat-value">${s.fuel_cost_per_distance == null ? '—' : fmtMoney(s.fuel_cost_per_distance, v.currency)}<span class="stat-unit">/${dUnit()}</span></div>
      <div class="stat-foot">across logged intervals</div></div>
  </div>`;

  const legendItems = state.chartMode === 'distance'
    ? `<span><i style="background:var(--accent)"></i>Distance (${dUnit()})</span>`
    : state.chartMode === 'efficiency'
      ? `<span><i style="background:var(--accent)"></i>${ev ? 'Efficiency' : 'Fuel efficiency'} (${esc(s.economy_label)})</span>`
      : `<span><i style="background:var(--accent)"></i>${energyNoun}</span><span><i style="background:var(--chart-grid)"></i>Records</span>`;
  const gapNote = { spending: 'No spending', distance: 'No odometer readings', efficiency: `No closing full ${ev ? 'charge' : 'fill-up'}` }[state.chartMode];
  const gapItem = chartSummary(s.monthly, state.chartMode).gaps ? `<span>– ${gapNote}</span>` : '';
  const bars = `
    <div class="card">
      <div class="chart-box">
        <div class="chart-title"><button type="button" class="chart-toggle" data-action="toggle-chart" aria-label="Switch chart to ${esc(CHART_MODES[nextChartMode()].name)}">${CHART_MODES[state.chartMode].name} ${ICONS.swap}</button> · last <button type="button" class="chart-toggle" data-action="toggle-range" aria-label="Show last ${state.chartRange === 12 ? 24 : 12} months">${state.chartRange} months ${ICONS.swap}</button></div>
        ${barChart(s.monthly, v.currency, state.chartMode, dUnit(), s.economy_label)}
        <div class="legend">${legendItems}${gapItem}</div>
      </div>
    </div>`;

  const reminders = s.reminders.length ? `
    <div class="card">
      <div class="card-head"><h2 class="card-title">Upcoming reminders</h2>
        <a class="btn btn-sm" href="#/reminders">All reminders</a></div>
      <div class="list">${s.reminders.slice(0, 4).map(reminderRow).join('')}</div>
    </div>` : '';

  const activity = s.recent.length ? `
    <div class="card">
      <div class="card-head"><h2 class="card-title">Recent activity</h2></div>
      <div class="list">${s.recent.map(a => `
        <div class="list-item">
          <div class="activity-icon ${a.type}">${a.type === 'fuel' ? ICONS.fuel : ICONS.wrench}</div>
          <div class="list-main"><div class="list-title">${esc(a.label)}</div>
            <div class="list-sub">${fmtDate(a.date)} · ${esc(a.detail)}</div></div>
          <div class="list-end"><span class="list-cost">${fmtMoney(a.cost, v.currency)}</span></div>
        </div>`).join('')}
      </div>
    </div>` : '';

  view.innerHTML = `
    ${pageHead(v.name, [v.year, v.make, v.model].filter(Boolean).join(' ') + (v.plate ? ' · ' + v.plate : ''))}
    ${tiles}
    <div class="grid grid-2" style="margin-top:var(--space-4)">
      ${bars}
    </div>
    <div class="grid grid-2" style="margin-top:var(--space-4)">${reminders}${activity}</div>
    ${(!s.recent.length && !s.reminders.length) ? emptyState(ICONS.clipboard, 'Nothing logged yet',
      `Add a ${sessionNoun} or a record and your dashboard will fill up.`,
      `<a class="btn btn-primary" href="#/fuel">${ev ? 'Add a charge' : 'Add a fill-up'}</a>`) : ''}`;
}
// Chart mode names depend on the current vehicle's fuel type: 'Fuel Efficiency'
// for gas, 'Efficiency' for EVs (where the metric is mi/kWh, not mpg). The
// mode KEYS (spending/distance/efficiency) and the rotation are constant.
function chartModes() {
  const ev = isElectricById(state.vid);
  return {
    spending: { name: 'Spending', next: 'distance' },
    distance: { name: 'Distance', next: 'efficiency' },
    efficiency: { name: ev ? 'Efficiency' : 'Fuel Efficiency', next: 'spending' },
  };
}
const nextChartMode = () => chartModes()[state.chartMode].next;

const CHART_VALUE = {
  spending: m => (m.fuel + m.service) / 100,
  distance: m => m.distance || 0,
  efficiency: m => m.efficiency || 0,
};

function chartLabel(mode, total, sym, dense = false) {
  if (mode === 'efficiency') return total.toFixed(1);
  if (mode === 'distance') return total >= 1000 ? Math.round(total / 100) / 10 + 'k' : Math.round(total).toLocaleString('en-US');
  if (total >= 1000) return sym + Math.round(total / 100) / 10 + 'k';
  return sym + (dense ? Math.round(total) : total.toFixed(2));
}

function chartSummary(monthly, mode) {
  const vals = monthly.map(CHART_VALUE[mode]).filter(v => v > 0);
  return {
    avg: vals.length >= 2 ? vals.reduce((x, y) => x + y, 0) / vals.length : null,
    gaps: vals.length < monthly.length,
  };
}

const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const showsYear = (ym, i) => i === 0 || ym.slice(5) === '01';
const monthName = ym => MONTH_NAMES[Number(ym.slice(5)) - 1];
const yearTag = ym => `’${ym.slice(2, 4)}`;

function barChart(monthly, currency, mode = 'spending', unit = 'mi', effLabel = '') {
  const sym = currency || '$';
  const W = 560, H = 180, PAD = 6;
  const dense = monthly.length > 12;
  const fs = dense ? 8 : 9;
  const valueOf = CHART_VALUE[mode];
  const ariaLabel = { spending: 'Monthly spending chart', distance: `Monthly distance chart (${unit})`, efficiency: `Monthly fuel efficiency chart (${effLabel})` }[mode];
  const max = Math.max(...monthly.map(valueOf), 1);
  const bw = (W - PAD * 2) / monthly.length;
  const gap = Math.min(8, bw * 0.3);
  const bars = monthly.map((m, i) => {
    const total = valueOf(m);
    const x = PAD + i * bw;
    const h = (total / max) * (H - 46);
    const fh = mode === 'spending' ? (total ? (m.fuel / 100 / total) * h : 0) : h;
    const sh = h - fh;
    const cx = (x + bw / 2).toFixed(1);
    const top = total > 0
      ? `<text x="${cx}" y="${(H - 32 - h).toFixed(1)}" text-anchor="middle" font-size="${fs}" fill="var(--text-muted)">${chartLabel(mode, total, sym, dense)}</text>`
      : `<text x="${cx}" y="${H - 32}" text-anchor="middle" font-size="${fs}" fill="var(--text-faint)">–</text>`;
    const label = dense
      ? `<text x="${cx}" y="${H - 14}" text-anchor="middle" font-size="${fs}" fill="var(--text-muted)">${monthName(m.ym)}</text>`
        + (showsYear(m.ym, i) ? `<text x="${cx}" y="${H - 4}" text-anchor="middle" font-size="${fs}" fill="var(--text-faint)">${yearTag(m.ym)}</text>` : '')
      : `<text x="${cx}" y="${H - 14}" text-anchor="middle" font-size="${fs}" fill="var(--text-muted)">${monthName(m.ym)}${showsYear(m.ym, i) ? ' ' + yearTag(m.ym) : ''}</text>`;
    return `
    <rect x="${(x + gap / 2).toFixed(1)}" y="${(H - 28 - fh).toFixed(1)}" width="${(bw - gap).toFixed(1)}" height="${Math.max(fh, 0).toFixed(1)}" rx="2" fill="var(--accent)"/>
    <rect x="${(x + gap / 2).toFixed(1)}" y="${(H - 28 - h).toFixed(1)}" width="${(bw - gap).toFixed(1)}" height="${Math.max(sh, 0).toFixed(1)}" rx="2" fill="var(--chart-grid)"/>
    ${label}
    ${top}`;
  }).join('');
  const { avg } = chartSummary(monthly, mode);
  let avgMark = '';
  if (avg != null) {
    const y = H - 28 - (avg / max) * (H - 46);
    const ty = y < 12 ? y + 11 : y - 3;
    avgMark = `
    <line x1="${PAD}" x2="${W - PAD}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}" stroke="var(--text-faint)" stroke-opacity="0.45" stroke-width="1" stroke-dasharray="4 3"/>
    <text x="${PAD}" y="${ty.toFixed(1)}" font-size="9" fill="var(--text-faint)" stroke="var(--surface)" stroke-width="3" paint-order="stroke" stroke-linejoin="round">Avg ${chartLabel(mode, avg, sym)}</text>`;
  }
  return `<svg class="chart-svg" viewBox="0 0 ${W} ${H}" role="img" aria-label="${ariaLabel}">${bars}${avgMark}</svg>`;
}

/* ------------------------------------------------------------------ fuel */
/* The Fuel / Energy view. For gas vehicles it shows fill-ups with mpg/L-per-100km
 * economy; for EVs it shows charges with mi/kWh or km/kWh efficiency. The
 * filter chips (All / Full tank / Partial) only affect display, not the
 * underlying economy math. Each row gets edit + delete icons; the closing
 * entry of each interval also shows its economy figure and a "Check" badge
 * if the suspect-interval flag fired.
 */

async function renderFuel(view) {
  const p = state.data.fuel = await api(`/vehicles/${state.vid}/fuel`);
  const v = p.vehicle;
  const t = evTerms(v);
  const filter = state.data.fuelFilter || 'all';
  const rows = p.entries.filter(e => filter === 'all' || (filter === 'full' ? e.full_tank : !e.full_tank));

  view.innerHTML = `
    ${pageHead(t.logTitle, `${fmtInt(p.summary.distance_logged)} ${dUnit()} · ${p.summary.avg_economy ?? '—'} ${p.summary.economy_label} average`,
      `<button class="btn btn-primary" data-action="add-fuel">${ICONS.fuel} ${t.addLabel}</button>
       <button class="btn" data-action="export-fuel">${ICONS.download} Export</button>`)}
    <div class="btn-row" style="margin-bottom:var(--space-4)">
      ${['all', 'full', 'partial'].map(f => `<button class="chip-btn ${f === filter ? 'active' : ''}" data-action="fuel-filter" data-filter="${f}">${f === 'all' ? 'All' : f === 'full' ? (v.fuel_type === 'electric' ? 'Full charge' : 'Full tank') : 'Partial'}</button>`).join('')}
    </div>
    ${rows.length ? `<div class="card table-wrap"><table class="data">
      <thead><tr>
        <th>Date</th><th class="num">Odometer</th><th class="num">Quantity</th>
        <th class="num">Cost</th><th class="num">${esc(v.currency === '$' ? '$' : v.currency)}/${fUnit()}</th>
        <th class="num">${v.fuel_type === 'electric' ? 'Efficiency' : 'Economy'}</th><th>${esc(t.tankCol)}</th><th></th>
      </tr></thead>
      <tbody>${rows.map(e => `
        <tr>
          <td>${fmtDate(e.date)}${attachBadgeHtml(e.attachment_count)}</td>
          <td class="num mono">${fmtInt(e.odometer)}</td>
          <td class="num mono">${fmtNum(e.quantity, 3)}</td>
          <td class="num mono">${fmtMoney(e.cost, v.currency)}</td>
          <td class="num mono">${e.unit_price == null ? '—' : fmtMoney(e.unit_price, v.currency)}</td>
          <td class="num mono">${e.economy == null
            ? (e.missed_before ? '<span class="badge badge-upcoming" title="A ' + (v.fuel_type === 'electric' ? 'charge' : 'fill-up') + ' was missed before this entry, so efficiency restarts here">Gap</span>' : '—')
            : fmtNum(e.economy) + ' ' + esc(p.summary.economy_label)
              + (e.suspect ? ' <span class="badge badge-soon" title="Unusual compared with your average — possible missed ' + (v.fuel_type === 'electric' ? 'charge' : 'fill-up') + ' or typo">Check</span>' : '')}</td>
          <td>${e.full_tank ? '<span class="badge badge-full">Full</span>' : '<span class="badge badge-partial">Partial</span>'}</td>
          <td><div class="row-actions">
            <button class="icon-btn" style="width:32px;height:32px" data-action="edit-fuel" data-id="${e.id}" aria-label="Edit entry">${ICONS.edit}</button>
            <button class="icon-btn" style="width:32px;height:32px" data-action="del-fuel" data-id="${e.id}" aria-label="Delete entry">${ICONS.trash}</button>
          </div></td>
        </tr>`).join('')}
      </tbody></table></div>`
    : emptyState(ICONS.fuel, v.fuel_type === 'electric' ? 'No charging sessions yet' : 'No fill-ups yet',
      v.fuel_type === 'electric'
        ? 'Log your first charging session to start tracking efficiency. Mark partial charges so the math stays honest.'
        : 'Log your first fill-up to start tracking fuel economy. Mark partial tanks so the math stays honest.',
      `<button class="btn btn-primary" data-action="add-fuel">${ICONS.fuel} ${t.addLabel}</button>`)}`;
}

/* ------------------------------------------------------------------ service */
/* The Records view (named "service" internally — the route, view name, and
 * API endpoints all keep the original "service" identifier; only the
 * user-visible label is "Records"). Shows service/repair/upgrade/tax/other
 * records with date, odometer, description, category badge, and cost.
 */

async function renderService(view) {
  const rows = state.data.service = await api(`/vehicles/${state.vid}/service`);
  const v = vLabel();
  view.innerHTML = `
    ${pageHead('Records', `${rows.length} record${rows.length === 1 ? '' : 's'} logged`,
      `<button class="btn btn-primary" data-action="add-service">${ICONS.wrench} Add record</button>
       <button class="btn" data-action="export-service">${ICONS.download} Export</button>`)}
    ${rows.length ? `<div class="card table-wrap"><table class="data">
      <thead><tr><th>Date</th><th class="num">Odometer</th><th>Description</th><th>Category</th><th class="num">Cost</th><th></th></tr></thead>
      <tbody>${rows.map(e => `
        <tr>
          <td>${fmtDate(e.date)}${attachBadgeHtml(e.attachment_count)}</td>
          <td class="num mono">${e.odometer == null ? '—' : fmtInt(e.odometer)}</td>
          <td class="wrap">${esc(e.description)}${e.notes ? `<div class="faint" style="font-size:var(--text-xs)">${esc(e.notes)}</div>` : ''}</td>
          <td><span class="badge badge-cat">${esc(e.category)}</span></td>
          <td class="num mono">${e.cost == null ? '—' : fmtMoney(e.cost, v.currency)}</td>
          <td><div class="row-actions">
            <button class="icon-btn" style="width:32px;height:32px" data-action="edit-service" data-id="${e.id}" aria-label="Edit record">${ICONS.edit}</button>
            <button class="icon-btn" style="width:32px;height:32px" data-action="del-service" data-id="${e.id}" aria-label="Delete record">${ICONS.trash}</button>
          </div></td>
        </tr>`).join('')}
      </tbody></table></div>`
    : emptyState(ICONS.wrench, 'No records yet', 'Oil changes, tires, repairs, taxes — log them here and they stay with the vehicle.',
      `<button class="btn btn-primary" data-action="add-service">${ICONS.wrench} Add record</button>`)}`;
}

/* ------------------------------------------------------------------ reminders */
/* Each reminder row shows the title, due date and/or due odometer, recurring
 * triggers ("every 5000 mi + every 6 mo"), and a status badge (overdue / due
 * soon / upcoming). The Done button either completes a one-shot reminder
 * or advances a recurring one to the next interval — see offerServiceRecord
 * for the "log a service record while completing" flow.
 */

function reminderRow(r) {
  const v = vLabel();
  const due = [];
  if (r.due_date) due.push(fmtDate(r.due_date));
  if (r.due_odometer != null) due.push(`${fmtInt(r.due_odometer)} ${dUnit()}`);
  const recur = [];
  if (r.recur_distance) recur.push(`every ${fmtInt(r.recur_distance)} ${dUnit()}`);
  if (r.recur_months) recur.push(`every ${r.recur_months} mo`);
  const status = { overdue: ['Overdue', 'badge-overdue'], soon: ['Due soon', 'badge-soon'], upcoming: ['Upcoming', 'badge-upcoming'] }[r.status];
  return `
  <div class="list-item">
    <div class="activity-icon">${ICONS.bell}</div>
    <div class="list-main">
      <div class="list-title">${esc(r.title)}</div>
      <div class="list-sub">${due.join(' · ') || 'no due date'}${recur.length ? ' · ' + recur.join(' + ') : ''}${r.notes ? ' · ' + esc(r.notes) : ''}</div>
    </div>
    <div class="list-end">
      <span class="badge ${status[1]}">${status[0]}</span>
      <div class="row-actions">
        <button class="btn btn-sm" data-action="done-reminder" data-id="${r.id}">Done</button>
        <button class="icon-btn" style="width:32px;height:32px" data-action="edit-reminder" data-id="${r.id}" aria-label="Edit reminder">${ICONS.edit}</button>
        <button class="icon-btn" style="width:32px;height:32px" data-action="del-reminder" data-id="${r.id}" aria-label="Delete reminder">${ICONS.trash}</button>
      </div>
    </div>
  </div>`;
}

async function renderReminders(view) {
  const rows = state.data.reminders = await api(`/vehicles/${state.vid}/reminders`);
  view.innerHTML = `
    ${pageHead('Reminders', `${rows.length} active`, `<button class="btn btn-primary" data-action="add-reminder">${ICONS.bell} Add reminder</button>`)}
    ${rows.length ? `<div class="card"><div class="list">${rows.map(reminderRow).join('')}</div></div>`
      : emptyState(ICONS.bell, 'No reminders', 'Set one for your next oil change or inspection. Recurring reminders roll forward automatically when you mark them done.',
        `<button class="btn btn-primary" data-action="add-reminder">${ICONS.bell} Add reminder</button>`)}`;
}

/* ------------------------------------------------------------------ settings */
/* The Settings view: garage (list of vehicles with Edit/Archive/Delete),
 * data exports (CSV + PDF per category + all-data), accent picker, install
 * card (only shown in non-standalone mode), and the about blurb. The Quick
 * log section that used to live here was removed — vehicle QR codes are now
 * accessed from the small QR icon in the Add fill-up modal head.
 */

function renderSettings(view) {
  const list = state.vehicles.length ? `
    <div class="card"><div class="list">${state.vehicles.map(v => `
      <div class="list-item">
        <div class="list-main">
          <div class="list-title">${esc(v.name)}${v.archived ? ' <span class="badge badge-upcoming">archived</span>' : ''}</div>
          <div class="list-sub">${[v.year, v.make, v.model].filter(Boolean).join(' ') || '—'} · ${v.fuel_type === 'electric' ? 'Electric' : 'Gas'} · ${dUnitFor(v)} · ${fUnitFor(v)}${v.plate ? ' · ' + esc(v.plate) : ''}</div>
        </div>
        <div class="list-end"><div class="row-actions">
          <button class="btn btn-sm" data-action="edit-vehicle" data-id="${v.id}">Edit</button>
          <button class="btn btn-sm" data-action="archive-vehicle" data-id="${v.id}">${v.archived ? 'Restore' : 'Archive'}</button>
          <button class="btn btn-sm btn-danger-ghost" data-action="del-vehicle" data-id="${v.id}">Delete</button>
        </div></div>
      </div>`).join('')}</div></div>`
    : emptyState(ICONS.car, 'No vehicles', 'Add your first vehicle to get started.', `<button class="btn btn-primary" data-action="add-vehicle">Add vehicle</button>`);

  view.innerHTML = `
    ${pageHead('Settings')}
    <div class="grid grid-2">
      <div>
        <p class="section-title">Garage</p>
        ${list}
        ${state.vehicles.length ? `<div class="btn-row" style="margin-top:var(--space-4)"><button class="btn" data-action="add-vehicle">Add vehicle</button></div>` : ''}
      </div>
      <div>
        <p class="section-title">Data</p>
        <div class="card card-pad">
          <dl class="kv">
            <dt>Export ${isElectric() ? 'energy' : 'fuel'}</dt><dd>${state.vid != null ? `<a href="${API_BASE}/api/vehicles/${state.vid}/export/fuel.csv">CSV</a> · <a href="${API_BASE}/api/vehicles/${state.vid}/export/fuel.pdf?theme=${currentTheme()}">PDF</a>` : `<span class="muted">—</span>`}</dd>
            <dt>Export records</dt><dd>${state.vid != null ? `<a href="${API_BASE}/api/vehicles/${state.vid}/export/service.csv">CSV</a> · <a href="${API_BASE}/api/vehicles/${state.vid}/export/service.pdf?theme=${currentTheme()}">PDF</a>` : `<span class="muted">—</span>`}</dd>
            <dt>Export all data</dt><dd>${state.vid != null ? `<a href="${API_BASE}/api/vehicles/${state.vid}/export/all.csv">CSV</a> · <a href="${API_BASE}/api/vehicles/${state.vid}/export/all.pdf?theme=${currentTheme()}">PDF</a>` : `<span class="muted">—</span>`}</dd>
            <dt>Storage</dt><dd>SQLite · /data volume</dd>
            <dt>Backup</dt><dd>stop container, copy /data</dd>
          </dl>
        </div>
        <p class="section-title" style="margin-top:var(--space-6)">Appearance</p>
        <div class="card card-pad">
          <p class="muted" style="font-size:var(--text-sm)">Accent color — applies to this instance for everyone.</p>
          <div class="accent-picker" role="group" aria-label="Accent color">${ACCENTS.map(([id, label, hex]) => `
            <button type="button" class="accent-option" data-action="set-accent" data-accent="${id}" aria-pressed="${state.accent === id}"><span class="accent-swatch" style="background:${hex}"></span>${label}</button>`).join('')}
          </div>
        </div>
        ${installCardHtml()}
        <p class="section-title" style="margin-top:var(--space-6)">About</p>
        <div class="card card-pad">
          <p class="muted" style="font-size:var(--text-sm)">Glovebox · self-hosted vehicle log. Single container, no cloud, no accounts. Designed for a trusted home network — put it behind your reverse proxy if you expose it.</p>
          ${state.version ? `<p class="faint" style="font-size:var(--text-xs);margin-top:var(--space-2)">Version ${esc(state.version)}</p>` : ''}
        </div>
      </div>
    </div>`;
}

function installCardHtml() {
  if (isStandalone()) return '';
  let body = '';
  if (state.installPrompt) {
    body = `<p class="muted" style="font-size:var(--text-sm)">Add Glovebox to your home screen to open it like an app.</p>
      <div class="btn-row" style="margin-top:var(--space-3)"><button class="btn btn-primary" data-action="install-app">Install app</button></div>`;
  } else if (isIOS()) {
    body = `<p class="muted" style="font-size:var(--text-sm)">To install: tap the Share button in Safari, then <strong>Add to Home Screen</strong>.</p>`;
  } else {
    return '';
  }
  return `<p class="section-title" style="margin-top:var(--space-6)">Install</p><div class="card card-pad">${body}</div>`;
}

/* Quick-log QR code: encodes `${base}#/fuel?add-fuel=1&vehicle=N` so scanning
 * it with a phone camera opens this Glovebox on the Fuel page with the right
 * vehicle preselected and the Add fill-up modal ready. The URL is resolved
 * against the current page so it follows whatever origin/path/proxy the
 * user is visiting through — no hardcoded demo domain. The PNG is rendered
 * by the backend (same-origin check there prevents the endpoint from being
 * used to encode arbitrary external URLs).
 *
 * Each vehicle gets its own QR (the `vehicle` query makes the code
 * vehicle-specific), so a multi-vehicle garage can print one per vehicle.
 * Download copies the same PNG the browser already has cached; Print opens
 * a tiny dedicated window with just the QR + caption so it prints cleanly
 * without the rest of the Settings page.
 */
function vehicleQrUrl(vid) {
  // Returns the deep-link URL for a specific vehicle. If `vid` is null/NaN
  // (no vehicles yet) the URL omits the vehicle param, which falls back to
  // the auto-selected first vehicle on load.
  const base = window.location.href.split('#')[0];
  const query = (vid != null && !Number.isNaN(vid))
    ? `?add-fuel=1&vehicle=${vid}`
    : '?add-fuel=1';
  return new URL(`#/fuel${query}`, base).href;
}

function quickLogQrUrl() {
  // The Settings page QR uses the currently selected vehicle, so it's
  // always the right one for the user's active context.
  return vehicleQrUrl(state.vid);
}

/* ------------------------------------------------------------------ onboarding */

function onboardingHtml() {
  return `<div class="onboarding">
    <img class="brand-mark" data-app-icon src="icons/${currentIconFolder()}/icon-192-rounded.png" width="30" height="30" alt="" aria-hidden="true">
    <h1>Welcome to Glovebox</h1>
    <p>A simple log for fuel, service and reminders. Add your first vehicle to get rolling.</p>
    <form class="modal-body card card-pad" id="onboarding-form">${vehicleFormHtml(null)}
      <button class="btn btn-primary" type="submit" style="width:100%">Add vehicle</button>
    </form>
  </div>`;
}

/* ------------------------------------------------------------------ forms */
/* Modal + form helpers. modal() is the generic dialog builder; the form
 * helpers (vehicleFormHtml, fuelFormHtml, etc.) return HTML strings for the
 * modal body. submitModal() wires up the form's submit handler so a successful
 * save closes the modal and re-renders; a failed save leaves the form in
 * place with an error message at the top.
 */

function modal(title, bodyHtml, footHtml, options = {}) {
  // Build a modal into #modal-root. closeModal() is called first to clear any
  // existing modal (so opening a new one from inside another doesn't stack).
  // `options.extraHeadActions` is optional HTML (e.g. a small QR icon
  // button) inserted before the close button. Used by the Add fill-up modal
  // to surface a "show this vehicle's QR code" action without adding a
  // dedicated UI element to every modal.
  closeModal();
  const root = $('#modal-root');
  const extraHead = options.extraHeadActions || '';
  root.innerHTML = `
    <div class="modal-overlay">
      <div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-head"><h2 class="modal-title">${esc(title)}</h2>
          <div class="modal-head-actions">${extraHead}<button class="icon-btn" data-close-modal aria-label="Close">
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
          </button></div></div>
        <form class="modal-body" id="modal-form">${bodyHtml}</form>
        <div class="modal-foot">${footHtml}</div>
      </div>
    </div>`;
  const first = $('.modal-body input, .modal-body select', root);
  // Skip auto-focus on touch screens: it pops the keyboard over the form.
  if (first && !matchMedia('(pointer: coarse)').matches) first.focus();
}

function exportModal(kind) {
  const base = `${API_BASE}/api/vehicles/${state.vid}/export/${kind}`;
  const what = kind === 'fuel' ? (isElectric() ? 'charge log' : 'fuel log') : 'record history';
  // PDF reports pick up the app's brand icon, so pass the active theme along
  // (accent is already known to the server; theme lives only in the browser).
  const opt = (ext, title, sub) => `
    <a class="export-opt" data-export-link href="${base}.${ext}${ext === 'pdf' ? `?theme=${currentTheme()}` : ''}" download>
      <span class="export-opt-icon">${ICONS.download}</span>
      <span class="export-opt-text"><strong>${title}</strong><small>${sub}</small></span>
    </a>`;
  modal(
    `Export ${what}`,
    `<div class="export-options">
      ${opt('csv', 'CSV', 'Spreadsheet-ready data')}
      ${opt('pdf', 'PDF', 'Print-friendly report')}
    </div>`,
    '<button class="btn" type="button" data-close-modal>Cancel</button>'
  );
}

function findReminder(id) {
  return [...(state.data.reminders || []), ...(state.data.summary?.reminders || [])].find(r => r.id === id);
}

async function completeReminder(id) {
  const res = await api(`/reminders/${id}/complete`, { method: 'POST' });
  toast(res.recurring ? 'Rescheduled for next interval' : 'Marked done');
  render();
}

async function offerServiceRecord(rem) {
  let odometer = null;
  try { odometer = (await api(`/vehicles/${state.vid}`)).latest_odometer; } catch { /* leave blank */ }
  modal(
    'Mark done and log record',
    `<p class="field-hint" style="margin-bottom:var(--space-4)">Add a record for “${esc(rem.title)}”, or just mark the reminder done.</p>`
      + serviceFormHtml({ date: todayISO(), odometer, description: rem.title, category: 'service', notes: rem.notes }),
    `<button class="btn" data-action="done-reminder-only" data-id="${rem.id}">Just mark done</button><button class="btn btn-primary" data-type="save" form="modal-form">Save &amp; mark done</button>`
  );
  submitModal(async (d, form) => {
    // Create the service record (JSON), upload any chosen attachments to it by
    // id, then complete the reminder. Upload failures are non-fatal — the
    // record + reminder completion still go through; we just flag the count.
    const sr = await api(`/vehicles/${state.vid}/service`, { method: 'POST', body: cleanService(d) });
    const errs = await uploadAttachments(form, 'service', sr.id, state.vid);
    const res = await api(`/reminders/${rem.id}/complete`, { method: 'POST' });
    const base = res.recurring ? 'Record saved · rescheduled for next interval' : 'Record saved · reminder done';
    toast(errs.length ? `${base} · ${errs.length} attachment(s) failed` : base, errs.length > 0);
    render();
  });
}

function closeModal() {
  // Only strip the deep-link query if a modal was actually open — opening a
  // new modal also calls closeModal() first to clear any existing one, and we
  // don't want that path to wipe the URL (otherwise a refresh while the
  // deep-linked modal is open wouldn't reopen it).
  const hadModal = $('#modal-root').innerHTML.trim() !== '';
  $('#modal-root').innerHTML = '';
  if (hadModal) {
    // If this modal was opened by a deep link (?add-fuel=1), strip the query
    // so a refresh or re-navigation doesn't reopen it. history.replaceState
    // avoids triggering hashchange / re-render.
    stripDeepLink('add-fuel');
  }
}

// Enlarged QR-code preview for a single vehicle. Rendered as a second overlay
// appended to #modal-root (NOT via modal(), which would closeModal() the Add
// fill-up modal underneath). Closing the preview leaves the underlying modal
// intact, so the user can review the QR and then go back to filling in the
// form. The preview is its own dialog: Esc, click-outside, and the close
// button all dismiss just the preview.
function showQrPreview(vid) {
  const root = $('#modal-root');
  const v = state.vehicles.find(x => x.id === vid);
  if (!v) return;
  const target = vehicleQrUrl(vid);
  const src = `${API_BASE}/api/qr.png?url=${encodeURIComponent(target)}`;
  const noun = v.fuel_type === 'electric' ? 'charge' : 'fill-up';
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay qr-preview-overlay';
  overlay.setAttribute('role', 'dialog');
  overlay.setAttribute('aria-modal', 'true');
  overlay.setAttribute('aria-label', 'Quick-log QR');
  overlay.innerHTML = `
    <div class="modal qr-preview" role="dialog" aria-modal="true" aria-label="Quick-log QR">
      <div class="modal-head"><h2 class="modal-title">Quick-log QR</h2>
        <div class="modal-head-actions"><button class="icon-btn" data-qr-preview-close aria-label="Close">
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>
        </button></div>
      </div>
      <div class="modal-body qr-preview-body">
        <div class="qr-preview-frame">
          <img src="${esc(src)}" alt="QR code that opens Glovebox ready to log a ${esc(noun)} for ${esc(v.name)}" width="280" height="280" loading="lazy">
        </div>
        <p class="qr-preview-caption">Scan with your phone camera to open Glovebox ready to log a ${esc(noun)} for <strong>${esc(v.name)}</strong>.</p>
        <p class="qr-preview-url faint">${esc(target)}</p>
      </div>
      <div class="modal-foot">
        <button class="btn" data-action="download-qr" data-url="${esc(src)}">${ICONS.download} Download</button>
        <button class="btn" data-action="print-qr" data-url="${esc(src)}" data-vid="${vid}">${ICONS.print} Print</button>
        <button class="btn btn-primary" data-qr-preview-close>Done</button>
      </div>
    </div>`;
  root.appendChild(overlay);
  // Click handling (backdrop click and [data-qr-preview-close] buttons) is
  // done by the consolidated global click handler in app.js, which checks for
  // .qr-preview-overlay BEFORE the [data-close-modal] / modal-overlay checks
  // so the underlying Add fill-up modal stays intact. Esc is handled by the
  // consolidated Esc handler at the bottom of the file.
}

// Opens the Add fill-up / Add charge modal. Extracted from the click handler
// so the deep-link path (the QR code's URL) can reuse the exact same modal,
// validation, and save logic — no duplicated code. The modal head includes
// a small QR icon button (when a vehicle is selected) that opens an enlarged
// preview of this vehicle's QR code with download/print options — one QR per
// vehicle, so a multi-vehicle garage can have a separate code for each.
function openAddFuelModal() {
  const addLabel = evTerms().addLabel;
  const v = vLabel();
  // The QR icon button is hidden when there's no current vehicle (onboarding
  // state) — there'd be nothing to encode. data-vid carries the vehicle id
  // into the click handler so the preview shows the right vehicle's QR even
  // if the user switches vehicles while the modal is open.
  const qrBtn = (v && v.id != null)
    ? `<button class="icon-btn" data-action="show-vehicle-qr" data-vid="${v.id}" aria-label="Show QR code for ${esc(v.name)}" title="Vehicle QR code">${ICONS.qr}</button>`
    : '';
  modal(addLabel, fuelFormHtml(null),
    `<button class="btn" data-close-modal>Cancel</button><button class="btn btn-primary" data-type="save" form="modal-form">Save</button>`,
    { extraHeadActions: qrBtn });
  submitModal(async (d, form) => {
    // Create the entry first (JSON), then upload any chosen attachments to it
    // by id. Upload failures are non-fatal — the entry is already saved, so we
    // surface a warning toast rather than leaving the modal open (which would
    // risk a duplicate entry on a second Save click).
    const res = await api(`/vehicles/${state.vid}/fuel`, { method: 'POST', body: cleanFuel(d) });
    const errs = await uploadAttachments(form, 'fuel', res.id, state.vid);
    if (errs.length) toast(`Saved · ${errs.length} attachment(s) failed`, true);
    else toast(isElectric() ? 'Charge saved' : 'Fill-up saved');
    render();
  });
}

function confirmModal(title, text, action, id) {
  modal(title, `<p class="muted" style="font-size:var(--text-sm)">${esc(text)}</p>`,
    `<button class="btn" data-close-modal>Cancel</button>
     <button class="btn btn-danger-ghost" data-action="${action}" data-id="${id}" style="background:var(--danger);color:#fff;border-color:var(--danger)">Delete</button>`);
}

function vehicleFormHtml(v) {
  // Fuel type and units are immutable once the vehicle has any fuel entries,
  // because they decide how every historical quantity_minor is interpreted.
  const lockUnits = !!(v && v.fuel_count > 0);
  const lockAttr = lockUnits ? 'disabled' : '';
  const ft = v?.fuel_type === 'electric' ? 'electric' : 'gas';
  const fuelUnitOptions = ft === 'electric'
    ? '<option value="kwh" selected>kWh</option>'
    : '<option value="gal" ' + (v?.fuel_unit === 'gal' ? 'selected' : '') + '>US gallons</option>'
      + '<option value="l" ' + (v?.fuel_unit === 'l' ? 'selected' : '') + '>Litres</option>'
      + '<option value="gal_uk" ' + (v?.fuel_unit === 'gal_uk' ? 'selected' : '') + '>Imperial gallons</option>';
  const hint = v
    ? '<p class="field-hint">' + (lockUnits
        ? 'Fuel type and units are fixed once entries have been logged, so historical records keep their meaning. Delete the entries first to change them.'
        : 'Fuel type and units are fixed once entries have been logged.') + '</p>'
    : '';
  return `
    <input type="hidden" name="id" value="${v?.id ?? ''}">
    <div class="field"><label for="vf-name">Name</label>
      <input id="vf-name" name="name" required maxlength="80" placeholder="Daily driver" value="${esc(v?.name ?? '')}"></div>
    <div class="field-row">
      <div class="field"><label for="vf-year">Year</label><input id="vf-year" name="year" type="number" min="1900" max="2100" value="${v?.year ?? ''}"></div>
      <div class="field"><label for="vf-make">Make</label><input id="vf-make" name="make" placeholder="Toyota" value="${esc(v?.make ?? '')}"></div>
    </div>
    <div class="field-row">
      <div class="field"><label for="vf-model">Model</label><input id="vf-model" name="model" placeholder="Tacoma" value="${esc(v?.model ?? '')}"></div>
      <div class="field"><label for="vf-plate">Plate</label><input id="vf-plate" name="plate" placeholder="ABC-1234" value="${esc(v?.plate ?? '')}"></div>
    </div>
    <div class="field-row">
      <div class="field"><label for="vf-ft">Fuel type</label>
        <select id="vf-ft" name="fuel_type" data-action="fuel-type-change" ${lockAttr}>
          <option value="gas" ${ft !== 'electric' ? 'selected' : ''}>Gas</option>
          <option value="electric" ${ft === 'electric' ? 'selected' : ''}>Electric</option>
        </select></div>
      <div class="field"><label for="vf-du">Distance unit</label>
        <select id="vf-du" name="distance_unit" ${lockAttr}>
          <option value="mi" ${v?.distance_unit !== 'km' ? 'selected' : ''}>Miles</option>
          <option value="km" ${v?.distance_unit === 'km' ? 'selected' : ''}>Kilometres</option>
        </select></div>
    </div>
    <div class="field-row">
      <div class="field"><label for="vf-fu">Fuel unit</label>
        <select id="vf-fu" name="fuel_unit" ${lockAttr}>
          ${fuelUnitOptions}
        </select></div>
      <div class="field"><label for="vf-cur">Currency symbol</label>
        <input id="vf-cur" name="currency" maxlength="4" placeholder="$" value="${esc(v?.currency ?? '$')}"></div>
    </div>
    ${hint}`;
}

/* ------------------------------------------------------------------ attachments */
/* Per-entry file attachments (receipts, photos, PDFs). The file picker lives
 * inside the fuel/service forms; on save, the entry is created/updated first
 * (JSON), then each chosen file is uploaded separately (multipart) to
 * /api/attachments with the entry's id. In edit mode, the existing attachments
 * are listed above the picker with open / download / remove actions.
 *
 * The upload is non-fatal: if the entry save succeeds but one or more uploads
 * fail, we still close the modal and surface a warning toast — the entry itself
 * is persisted, and the user can re-attach the failed files via Edit. This
 * avoids the "click Save again → duplicate entry" trap.
 */

function attachmentsFieldHtml(entryType, entry) {
  // The picker is always shown; in edit mode an existing-attachments list is
  // rendered above it (populated asynchronously by refreshAttachments, because
  // modal() builds its HTML synchronously). isEdit requires a real entry id —
  // offerServiceRecord() passes a pre-filled-but-new object (no id) which must
  // be treated as "add", not "edit", so it doesn't try to load attachments for
  // a phantom entry_id.
  const isEdit = !!(entry && entry.id);
  const list = isEdit
    ? `<div class="attach-list" id="attach-list" data-entry-type="${entryType}" data-entry-id="${entry.id}"><p class="field-hint">Loading attachments…</p></div>`
    : '';
  return `
    <div class="field attach-field">
      <label for="af-files">Attachments (optional)</label>
      ${list}
      <input id="af-files" name="attachments" type="file" multiple accept="${ATTACH_ACCEPT}">
      <p class="field-hint">Images, PDFs, TXT or CSV · up to 25 MB each.</p>
    </div>`;
}

function attachBadgeHtml(count) {
  // The small paperclip + count shown next to a row's date when it has
  // attachments. Returns '' for zero so rows without attachments stay clean.
  if (!count) return '';
  return ` <span class="attach-badge" title="${count} attachment${count === 1 ? '' : 's'}">${ICONS.paperclip}<span class="attach-count">${count}</span></span>`;
}

function attachItemHtml(a) {
  // One attachment chip: thumbnail (images) or file icon (PDF/text), the
  // original filename + size, and open / download / remove actions. "Open"
  // and the thumbnail/name link to disposition=inline (browser preview in a
  // new tab); the download icon links to disposition=attachment.
  const view = API_BASE + a.url;
  const dl = API_BASE + a.download_url;
  const preview = a.is_image
    ? `<a href="${esc(view)}" target="_blank" rel="noopener" class="attach-thumb"><img src="${esc(view)}" alt="${esc(a.filename)}" loading="lazy"></a>`
    : `<a href="${esc(view)}" target="_blank" rel="noopener" class="attach-icon" aria-label="Open ${esc(a.filename)}">${ICONS.file}</a>`;
  return `
  <div class="attach-chip">
    ${preview}
    <div class="attach-meta">
      <a href="${esc(view)}" target="_blank" rel="noopener" class="attach-name" title="${esc(a.filename)}">${esc(a.filename)}</a>
      <span class="attach-size">${fmtSize(a.size_bytes)}</span>
    </div>
    <div class="attach-actions">
      <a class="icon-btn" href="${esc(dl)}" download title="Download">${ICONS.download}</a>
      <button class="icon-btn" type="button" data-action="del-attachment" data-id="${a.id}" title="Remove">${ICONS.trash}</button>
    </div>
  </div>`;
}

async function refreshAttachments(entryType, entryId) {
  // Populate the #attach-list inside an open edit modal. No-op if the modal has
  // already closed (the list element is gone) — this makes it safe to call
  // right after modal() even if a faster user action dismissed it.
  const list = $('#attach-list');
  if (!list) return;
  try {
    const items = await api(`/attachments?entry_type=${entryType}&entry_id=${entryId}`);
    list.innerHTML = items.length
      ? items.map(attachItemHtml).join('')
      : '<p class="field-hint">No attachments yet.</p>';
  } catch {
    list.innerHTML = '<p class="field-hint">Could not load attachments.</p>';
  }
}

async function uploadAttachments(form, entryType, entryId, vehicleId) {
  // Upload every file in the picker, one request per file (multipart/form-data).
  // Returns an array of per-file error messages rather than throwing, so the
  // caller can finish closing the modal and surface a warning toast — the entry
  // itself is already saved by the time we get here.
  const input = form.querySelector('#af-files');
  if (!input || !input.files || !input.files.length) return [];
  const errors = [];
  for (const file of input.files) {
    try {
      const fd = new FormData();
      fd.append('file', file);
      fd.append('entry_type', entryType);
      fd.append('entry_id', String(entryId));
      fd.append('vehicle_id', String(vehicleId));
      const res = await fetch(API_BASE + '/api/attachments', { method: 'POST', body: fd });
      if (!res.ok) {
        let detail = `Upload failed (${res.status})`;
        try { const j = await res.json(); detail = typeof j.detail === 'string' ? j.detail : detail; } catch {}
        errors.push(`${file.name}: ${detail}`);
      }
    } catch (e) {
      errors.push(`${file.name}: ${e.message}`);
    }
  }
  // Clear the picker so a re-submit (e.g. the user fixes a validation error and
  // saves again) doesn't re-upload the same files as duplicates.
  input.value = '';
  return errors;
}

function fuelFormHtml(e) {
  const v = vLabel();
  const t = evTerms(v);
  return `
    <input type="hidden" name="id" value="${e?.id ?? ''}">
    <div class="field-row">
      <div class="field"><label for="ff-date">Date</label><input id="ff-date" name="date" type="date" required value="${esc(e?.date ?? todayISO())}"></div>
      <div class="field"><label for="ff-odo">Odometer (${dUnit()})</label><input id="ff-odo" name="odometer" type="number" step="0.1" min="0" required placeholder="45000" value="${e?.odometer ?? ''}"></div>
    </div>
    <div class="field-row">
      <div class="field"><label for="ff-qty">${t.qtyLabel} (${fUnit()})</label><input id="ff-qty" name="quantity" type="number" step="0.001" min="0" required placeholder="14.5" value="${e?.quantity ?? ''}"></div>
      <div class="field"><label for="ff-cost">Total cost</label><input id="ff-cost" name="cost" type="number" step="0.01" min="0" placeholder="52.30" value="${e?.cost ?? ''}"></div>
    </div>
    <div class="field"><label for="ff-station">${t.stationLabel}</label><input id="ff-station" name="station" placeholder="${t.stationPlaceholder}" value="${esc(e?.station ?? '')}"></div>
    <div class="field check-row">
      <input type="checkbox" id="ff-full" name="full_tank" ${e?.full_tank !== false ? 'checked' : ''}>
      <label for="ff-full">${t.fullLabel}</label>
    </div>
    <p class="field-hint">${t.partialHint}</p>
    <div class="field check-row">
      <input type="checkbox" id="ff-missed" name="missed_before" ${e?.missed_before ? 'checked' : ''}>
      <label for="ff-missed">${t.missedLabel}</label>
    </div>
    <p class="field-hint">${t.missedHint}</p>
    <div class="field"><label for="ff-notes">Notes (optional)</label><textarea id="ff-notes" name="notes" rows="2">${esc(e?.notes ?? '')}</textarea></div>
    ${attachmentsFieldHtml('fuel', e)}`;
}

function serviceFormHtml(e) {
  return `
    <input type="hidden" name="id" value="${e?.id ?? ''}">
    <div class="field-row">
      <div class="field"><label for="sf-date">Date</label><input id="sf-date" name="date" type="date" required value="${esc(e?.date ?? todayISO())}"></div>
      <div class="field"><label for="sf-odo">Odometer (${dUnit()}, optional)</label><input id="sf-odo" name="odometer" type="number" step="0.1" min="0" placeholder="45000" value="${e?.odometer ?? ''}"></div>
    </div>
    <div class="field"><label for="sf-desc">Description</label><input id="sf-desc" name="description" required maxlength="200" placeholder="Oil change · 0W-20 synthetic" value="${esc(e?.description ?? '')}"></div>
    <div class="field-row">
      <div class="field"><label for="sf-cat">Category</label>
        <select id="sf-cat" name="category">
          ${['service', 'repair', 'upgrade', 'tax', 'other'].map(cat => `<option value="${cat}" ${e?.category === cat ? 'selected' : ''}>${cat[0].toUpperCase() + cat.slice(1)}</option>`).join('')}
        </select></div>
      <div class="field"><label for="sf-cost">Cost (optional)</label><input id="sf-cost" name="cost" type="number" step="0.01" min="0" placeholder="75.00" value="${e?.cost ?? ''}"></div>
    </div>
    <div class="field"><label for="sf-notes">Notes (optional)</label><textarea id="sf-notes" name="notes" rows="2">${esc(e?.notes ?? '')}</textarea></div>
    ${attachmentsFieldHtml('service', e)}`;
}

function reminderFormHtml(r) {
  return `
    <input type="hidden" name="id" value="${r?.id ?? ''}">
    <div class="field"><label for="rf-title">Title</label><input id="rf-title" name="title" required maxlength="200" placeholder="Oil change" value="${esc(r?.title ?? '')}"></div>
    <div class="field-row">
      <div class="field"><label for="rf-date">Due date</label><input id="rf-date" name="due_date" type="date" value="${esc(r?.due_date ?? '')}"></div>
      <div class="field"><label for="rf-odo">Due at odometer (${dUnit()})</label><input id="rf-odo" name="due_odometer" type="number" step="0.1" min="0" placeholder="55000" value="${r?.due_odometer ?? ''}"></div>
    </div>
    <p class="field-hint" style="margin-bottom:var(--space-4)">Set either or both — whichever comes first triggers it.</p>
    <div class="field-row">
      <div class="field"><label for="rf-rd">Recur every … ${dUnit()}</label><input id="rf-rd" name="recur_distance" type="number" step="1" min="0" placeholder="5000" value="${r?.recur_distance ?? ''}"></div>
      <div class="field"><label for="rf-rm">Recur every … months</label><input id="rf-rm" name="recur_months" type="number" step="1" min="0" max="120" placeholder="6" value="${r?.recur_months ?? ''}"></div>
    </div>
    <div class="field"><label for="rf-notes">Notes (optional)</label><textarea id="rf-notes" name="notes" rows="2">${esc(r?.notes ?? '')}</textarea></div>`;
}

function todayISO() {
  // Local calendar date (en-CA yields ISO YYYY-MM-DD); toISOString() would use
  // UTC and pre-fill yesterday's date for UTC+ timezones in the early morning.
  return new Date().toLocaleDateString('en-CA');
}

function formValues(formEl) {
  const data = {};
  new FormData(formEl).forEach((value, key) => { data[key] = value; });
  return data;
}

function submitModal(handler) {
  // Wire up the form's submit handler. On success: call the handler (which
  // does the API call + re-render) and close the modal. On failure: re-enable
  // the save button, restore its label, and show the error inline at the
  // top of the form so the user can fix and retry.
  const form = $('.modal-body');
  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const btn = $('.modal-foot button[data-type="save"]');
    const btnLabel = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }
    try {
      await handler(formValues(form), form);
      closeModal();
    } catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = btnLabel; }
      form.insertAdjacentHTML('afterbegin', `<p class="field-error" role="alert" style="margin-bottom:var(--space-3)">${esc(e.message)}</p>`);
    }
  });
}

/* ------------------------------------------------------------------ actions */
/* Single delegated click handler for the whole document. Every interactive
 * element carries a `data-action` attribute naming what it does; the handler
 * dispatches on that. This avoids per-element listeners and lets dynamically
 * rendered HTML (modals, list rows, etc.) be interactive without explicit
 * re-binding. Confirm dialogs (data-action ending in -confirm) block
 * double-clicks by disabling the button while the request runs. */

document.addEventListener('click', async ev => {
  // Export links: let the browser start the download, then dismiss the dialog.
  if (ev.target.closest('[data-export-link]')) { setTimeout(closeModal, 250); return; }
  // QR preview overlay: clicking the backdrop (the overlay element itself)
  // or any [data-qr-preview-close] button dismisses JUST the preview and
  // leaves the underlying Add fill-up modal intact. We must short-circuit
  // here BEFORE the [data-close-modal] / modal-overlay checks below —
  // otherwise closeModal() would tear down the entire modal-root.
  const qrPreview = ev.target.closest('.qr-preview-overlay');
  if (qrPreview) {
    if (ev.target === qrPreview || ev.target.closest('[data-qr-preview-close]')) {
      qrPreview.remove();
      return;
    }
  }
  const closer = ev.target.closest('[data-close-modal]');
  if (closer) { closeModal(); return; }
  if (ev.target.classList.contains('modal-overlay')) { closeModal(); return; }

  const el = ev.target.closest('[data-action]');
  if (!el) return;
  const action = el.dataset.action;
  const id = el.dataset.id ? Number(el.dataset.id) : null;

  // Confirm-dialog buttons (del-*-confirm): block double-clicks while the request runs.
  // The dialog is closed by each branch below, only after the delete succeeds.
  if (action.endsWith('-confirm')) el.disabled = true;

  try {
    if (action === 'retry-connect') {
      await connect();
    } else if (action === 'export-fuel' || action === 'export-service') {
      exportModal(action === 'export-fuel' ? 'fuel' : 'service');
    } else if (action === 'install-app') {
      const p = state.installPrompt;
      if (p) { p.prompt(); await p.userChoice; state.installPrompt = null; render(); }
    } else if (action === 'set-accent') {
      const res = await api('/settings', { method: 'PUT', body: { accent: el.dataset.accent } });
      applyAccent(res.accent);
      $$('.accent-option').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.accent === state.accent)));
      toast('Accent color updated');
    } else if (action === 'add-vehicle') {
      modal('Add vehicle', vehicleFormHtml(null),
        `<button class="btn" data-close-modal>Cancel</button><button class="btn btn-primary" data-type="save" form="modal-form">Add</button>`);
      submitModal(async d => {
        await api('/vehicles', { method: 'POST', body: cleanVehicle(d) });
        await loadVehicles();
        toast('Vehicle added');
        render();
      });
    } else if (action === 'edit-vehicle') {
      const v = state.vehicles.find(x => x.id === id);
      modal('Edit vehicle', vehicleFormHtml(v),
        `<button class="btn" data-close-modal>Cancel</button><button class="btn btn-primary" data-type="save" form="modal-form">Save</button>`);
      submitModal(async d => {
        await api(`/vehicles/${id}`, { method: 'PATCH', body: cleanVehicle(d) });
        await loadVehicles();
        toast('Vehicle updated');
        render();
      });
    } else if (action === 'archive-vehicle') {
      const v = state.vehicles.find(x => x.id === id);
      await api(`/vehicles/${id}`, { method: 'PATCH', body: { archived: !v.archived } });
      await loadVehicles();
      toast(v.archived ? 'Vehicle restored' : 'Vehicle archived');
      render();
    } else if (action === 'del-vehicle') {
      const v = state.vehicles.find(x => x.id === id);
      confirmModal('Delete vehicle', `This permanently deletes ${v.name} and all of its records. This cannot be undone.`, 'del-vehicle-confirm', id);
    } else if (action === 'del-vehicle-confirm') {
      await api(`/vehicles/${id}`, { method: 'DELETE' });
      closeModal();
      state.vid = null;
      await loadVehicles();
      toast('Vehicle deleted');
      render();
    } else if (action === 'add-fuel') {
      openAddFuelModal();
    } else if (action === 'edit-fuel') {
      const e = state.data.fuel.entries.find(x => x.id === id);
      modal(isElectric() ? 'Edit charging session' : 'Edit fill-up', fuelFormHtml(e),
        `<button class="btn" data-close-modal>Cancel</button><button class="btn btn-primary" data-type="save" form="modal-form">Save</button>`);
      // Populate the existing-attachments list (the form HTML rendered a
      // placeholder #attach-list). Removing an attachment updates this list
      // in place via refreshAttachments(); new files are uploaded on Save.
      refreshAttachments('fuel', id);
      submitModal(async (d, form) => {
        await api(`/fuel/${id}`, { method: 'PATCH', body: cleanFuel(d) });
        const errs = await uploadAttachments(form, 'fuel', id, state.vid);
        if (errs.length) toast(`Saved · ${errs.length} attachment(s) failed`, true);
        else toast(isElectric() ? 'Charge updated' : 'Fill-up updated');
        render();
      });
    } else if (action === 'del-fuel') {
      confirmModal(isElectric() ? 'Delete charging session' : 'Delete fill-up',
        isElectric() ? 'Delete this charging session? Efficiency figures will be recalculated.' : 'Delete this fill-up record? Economy figures will be recalculated.',
        'del-fuel-confirm', id);
    } else if (action === 'del-fuel-confirm') {
      await api(`/fuel/${id}`, { method: 'DELETE' });
      closeModal();
      toast(isElectric() ? 'Charge deleted' : 'Fill-up deleted');
      render();
    } else if (action === 'add-service') {
      modal('Add record', serviceFormHtml(null),
        `<button class="btn" data-close-modal>Cancel</button><button class="btn btn-primary" data-type="save" form="modal-form">Save</button>`);
      submitModal(async (d, form) => {
        const res = await api(`/vehicles/${state.vid}/service`, { method: 'POST', body: cleanService(d) });
        const errs = await uploadAttachments(form, 'service', res.id, state.vid);
        if (errs.length) toast(`Saved · ${errs.length} attachment(s) failed`, true);
        else toast('Record saved');
        render();
      });
    } else if (action === 'edit-service') {
      const e = state.data.service.find(x => x.id === id);
      modal('Edit record', serviceFormHtml(e),
        `<button class="btn" data-close-modal>Cancel</button><button class="btn btn-primary" data-type="save" form="modal-form">Save</button>`);
      refreshAttachments('service', id);
      submitModal(async (d, form) => {
        await api(`/service/${id}`, { method: 'PATCH', body: cleanService(d) });
        const errs = await uploadAttachments(form, 'service', id, state.vid);
        if (errs.length) toast(`Saved · ${errs.length} attachment(s) failed`, true);
        else toast('Record updated');
        render();
      });
    } else if (action === 'del-service') {
      confirmModal('Delete record', 'Delete this record?', 'del-service-confirm', id);
    } else if (action === 'del-service-confirm') {
      await api(`/service/${id}`, { method: 'DELETE' });
      closeModal();
      toast('Record deleted');
      render();
    } else if (action === 'del-attachment') {
      // Remove one attachment from inside an open edit modal. The entry itself
      // is untouched; we just refresh #attach-list so the chip disappears. The
      // table's paperclip badge updates on the next render() (when the modal
      // closes and the view re-fetches).
      await api(`/attachments/${id}`, { method: 'DELETE' });
      toast('Attachment removed');
      const list = $('#attach-list');
      if (list) await refreshAttachments(list.dataset.entryType, Number(list.dataset.entryId));
    } else if (action === 'add-reminder') {
      modal('Add reminder', reminderFormHtml(null),
        `<button class="btn" data-close-modal>Cancel</button><button class="btn btn-primary" data-type="save" form="modal-form">Save</button>`);
      submitModal(async d => {
        await api(`/vehicles/${state.vid}/reminders`, { method: 'POST', body: cleanReminder(d) });
        toast('Reminder added');
        render();
      });
    } else if (action === 'edit-reminder') {
      const r = state.data.reminders.find(x => x.id === id);
      modal('Edit reminder', reminderFormHtml(r),
        `<button class="btn" data-close-modal>Cancel</button><button class="btn btn-primary" data-type="save" form="modal-form">Save</button>`);
      submitModal(async d => {
        await api(`/reminders/${id}`, { method: 'PATCH', body: cleanReminder(d) });
        toast('Reminder updated');
        render();
      });
    } else if (action === 'del-reminder') {
      confirmModal('Delete reminder', 'Delete this reminder?', 'del-reminder-confirm', id);
    } else if (action === 'del-reminder-confirm') {
      await api(`/reminders/${id}`, { method: 'DELETE' });
      closeModal();
      toast('Reminder deleted');
      render();
    } else if (action === 'done-reminder') {
      const rem = findReminder(id);
      if (rem) await offerServiceRecord(rem); else await completeReminder(id);
    } else if (action === 'done-reminder-only') {
      closeModal();
      await completeReminder(id);
    } else if (action === 'toggle-chart') {
      state.chartMode = nextChartMode();
      render();
    } else if (action === 'toggle-range') {
      state.chartRange = state.chartRange === 12 ? 24 : 12;
      render();
    } else if (action === 'fuel-filter') {
      state.data.fuelFilter = el.dataset.filter;
      render();
    } else if (action === 'fuel-type-change') {
      // Swap the fuel_unit options to match the new fuel_type. The select
      // is rebuilt in place; if the user flips back, the previous gas
      // option (defaulting to gal) is restored.
      const ft = el.value;
      const fuSelect = document.getElementById('vf-fu');
      if (!fuSelect) return;
      if (ft === 'electric') {
        fuSelect.innerHTML = '<option value="kwh" selected>kWh</option>';
      } else {
        fuSelect.innerHTML =
          '<option value="gal" selected>US gallons</option>'
          + '<option value="l">Litres</option>'
          + '<option value="gal_uk">Imperial gallons</option>';
      }
    } else if (action === 'show-vehicle-qr') {
      // Small QR icon in the Add fill-up modal head — opens the enlarged
      // preview for the vehicle named on the button (data-vid). Falls back
      // to the currently selected vehicle if the button's data-vid is missing
      // (e.g. opened from somewhere without a per-vehicle button).
      const vid = el.dataset.vid != null ? Number(el.dataset.vid) : state.vid;
      if (vid == null || Number.isNaN(vid)) { toast('Select a vehicle first', true); return; }
      showQrPreview(vid);
    } else if (action === 'download-qr') {
      // Fetch the PNG as a blob (so the browser saves under our filename,
      // not a random hash) and trigger a download via a temporary <a>.
      // If a vehicle is named on the button (data-vid), the filename is
      // vehicle-specific so downloads from a multi-vehicle garage don't
      // all overwrite each other.
      const url = el.dataset.url;
      const res = await fetch(url);
      if (!res.ok) throw new Error('QR download failed');
      const blob = await res.blob();
      const objUrl = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = objUrl;
      const vid = el.dataset.vid != null ? Number(el.dataset.vid) : null;
      const v = vid != null ? state.vehicles.find(x => x.id === vid) : null;
      const slug = v ? v.name.replace(/\s+/g, '_').replace(/[^\w-]/g, '').toLowerCase() : 'quick-log';
      a.download = `glovebox-${slug}-qr.png`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(objUrl), 1000);
      toast('Downloaded QR code');
    } else if (action === 'print-qr') {
      // Open a tiny window with just the QR + caption so the printed page
      // is clean (no nav, no other cards). Print is invoked after the
      // image loads; the popup closes itself after the print dialog.
      const url = el.dataset.url;
      const vid = el.dataset.vid != null ? Number(el.dataset.vid) : state.vid;
      const v = (vid != null && !Number.isNaN(vid)) ? state.vehicles.find(x => x.id === vid) : null;
      const target = v ? vehicleQrUrl(v.id) : quickLogQrUrl();
      const noun = v ? (v.fuel_type === 'electric' ? 'charge' : 'fill-up') : (isElectric() ? 'charge' : 'fill-up');
      const title = v ? `${v.name} — quick-log QR` : 'Glovebox quick-log QR';
      const w = window.open('', '_blank', 'width=480,height=640');
      if (!w) { toast('Allow pop-ups to print the QR', true); return; }
      w.document.write(`<!doctype html><html><head><title>${esc(title)}</title>
        <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <style>
          body { margin: 0; padding: 32px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; text-align: center; }
          h1 { font-size: 18px; margin: 0 0 8px; }
          .url { font-size: 11px; color: #555; word-break: break-all; margin: 8px auto 24px; max-width: 360px; font-family: ui-monospace, SFMono-Regular, monospace; }
          img { width: 320px; height: 320px; image-rendering: pixelated; }
          .hint { margin-top: 16px; font-size: 12px; color: #555; }
          @media print { body { padding: 0; } }
        </style></head>
        <body>
          <h1>${esc(title)}</h1>
          <div class="url">${esc(target)}</div>
          <img src="${esc(url)}" alt="QR code">
          <p class="hint">Scan with your phone camera to open Glovebox ready to log a ${esc(noun)}.</p>
          <script>window.onload = () => { setTimeout(() => { window.print(); }, 200); };</` + `script>
        </body></html>`);
      w.document.close();
    }
  } catch (e) {
    if (action.endsWith('-confirm')) el.disabled = false;
    toast(e.message, true);
  }
});

function cleanVehicle(d) {
  // Convert raw form values into the API body shape. Empty strings become
  // null for nullable fields; the year is parsed as int (or null). The
  // `fuel_type`/`distance_unit`/`fuel_unit` come from <select> elements and
  // are always present.
  return {
    name: d.name.trim(),
    year: d.year === '' ? null : (d.year ? parseInt(d.year, 10) : null),
    make: d.make || null, model: d.model || null, plate: d.plate || null,
    currency: d.currency || '$',
    fuel_type: d.fuel_type || 'gas',
    distance_unit: d.distance_unit, fuel_unit: d.fuel_unit,
  };
}

function cleanFuel(d) {
  // Checkbox values come through as 'on' when checked, absent otherwise —
  // convert to actual booleans. Empty cost becomes 0 (the API treats cost
  // as non-nullable for fuel entries).
  return {
    date: d.date, odometer: parseFloat(d.odometer), quantity: parseFloat(d.quantity),
    cost: d.cost === '' ? 0 : parseFloat(d.cost), full_tank: d.full_tank === 'on', missed_before: d.missed_before === 'on',
    station: d.station || null, notes: d.notes || null,
  };
}
function cleanService(d) {
  // Service odometer/cost are nullable ("I didn't record the odometer for
  // this oil change"), so empty string -> null unlike fuel where odometer is required.
  return {
    date: d.date, odometer: d.odometer === '' ? null : parseFloat(d.odometer),
    description: d.description, category: d.category,
    cost: d.cost === '' ? null : parseFloat(d.cost), notes: d.notes || null,
  };
}
function cleanReminder(d) {
  return {
    title: d.title, notes: d.notes || null,
    due_date: d.due_date || null,
    due_odometer: d.due_odometer === '' ? null : parseFloat(d.due_odometer),
    recur_distance: d.recur_distance === '' ? null : parseFloat(d.recur_distance),
    recur_months: d.recur_months === '' ? null : parseInt(d.recur_months, 10),
  };
}

document.addEventListener('keydown', ev => {
  if (ev.key !== 'Escape') return;
  // Close the QR preview first if it's open. Otherwise the underlying modal
  // (or whatever else is in #modal-root) would all get torn down at once and
  // the user would lose their Add fill-up form. A second Esc dismisses the
  // remaining modal as before.
  const preview = document.querySelector('.qr-preview-overlay');
  if (preview) { preview.remove(); return; }
  closeModal();
});

/* ------------------------------------------------------------------ PWA */
/* Installability + offline support. The beforeinstallprompt event is captured
 * so the Settings page can show an "Install app" button when the browser is
 * willing to show the prompt (rather than the browser's own install banner).
 * The service worker registration is best-effort — it silently no-ops on
 * insecure origins (other than localhost), where the app still works online. */

const isStandalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const isIOS = () => /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);

window.addEventListener('beforeinstallprompt', ev => {
  ev.preventDefault();
  state.installPrompt = ev;
  if (state.view === 'settings') render();
});
window.addEventListener('appinstalled', () => {
  state.installPrompt = null;
  if (state.view === 'settings') render();
});

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('sw.js').catch(() => { /* needs HTTPS or localhost; app still works */ });
  });
}

/* ------------------------------------------------------------------ boot */
/* Load settings + vehicles, then show the current view. Also used by the
 * "Try again" button, since an installed app has no browser reload button.
 *
 * The boot sequence:
 *   1. buildBottomNav() — once, before the first render
 *   2. hashchange listener — re-runs route() on every navigation
 *   3. vehicle-select change listener — switches vehicles and re-renders
 *   4. connect() — fetches settings + vehicles, then route() shows the view
 */
async function connect() {
  try {
    try {
      const settings = await api('/settings');
      state.version = settings.version;
      applyAccent(settings.accent);
    } catch { applyAccent(DEFAULT_ACCENT); }
    await loadVehicles();
  } catch (e) {
    // Server unreachable — show a "Try again" button. This is the only
    // top-level error handler; per-route errors surface as toasts.
    $('#view').innerHTML = `<div class="empty-state"><h3>Can't reach the server</h3><p>${esc(e.message)}</p>
      <button class="btn btn-primary" data-action="retry-connect">Try again</button></div>`;
    return;
  }
  route();
}

(async function boot() {
  buildBottomNav();
  window.addEventListener('hashchange', route);
  document.addEventListener('click', ev => {
    // Active-tab click: smooth-scroll to top (so re-tapping the current tab
    // is a "take me to the top" gesture, like a native app).
    const a = ev.target.closest('.tab, .bottom-nav a');
    if (a && a.classList.contains('active')) window.scrollTo({ top: 0, behavior: 'smooth' });
  });
  $('#vehicle-select').addEventListener('change', ev => {
    state.vid = Number(ev.target.value);
    state.data = {};
    updateNavLabels();
    // The user explicitly chose a vehicle — drop any deep-link `vehicle`
    // param so a refresh respects the dropdown, not the URL hint.
    stripDeepLink('vehicle');
    render();
  });
  await connect();
})();

function buildBottomNav() {
  const nav = document.createElement('nav');
  nav.className = 'bottom-nav';
  nav.setAttribute('aria-label', 'Sections');
  const gear = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>';
  const items = [
    ['dashboard', ICONS.gauge, 'Home'], ['fuel', ICONS.fuel, 'Fuel'],
    ['service', ICONS.wrench, 'Records'], ['reminders', ICONS.bell, 'Reminders'],
    ['settings', gear, 'Settings'],
  ];
  nav.innerHTML = items.map(([v, icon, label]) => `<a href="#/${v}" data-view="${v}">${icon}${label}</a>`).join('');
  document.body.appendChild(nav);
}
