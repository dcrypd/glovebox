<p align="center">
  <img src="static/icons/blue-light/icon-512-rounded.png" alt="Glovebox logo" width="96">
</p>

# Glovebox

A simple, modern, self-hosted vehicle log. Track fuel economy, service records, costs and maintenance reminders in one small Docker container. Your data stays in a single SQLite file that you own.

[![Build](https://img.shields.io/github/actions/workflow/status/dcrypd/glovebox/docker-image.yml?style=flat-square&logo=githubactions&logoColor=white&label=Build)](https://github.com/dcrypd/glovebox/actions/workflows/docker-image.yml)
[![Release](https://img.shields.io/github/v/release/dcrypd/glovebox?style=flat-square&logo=github&label=Release&color=1d5fb8)](https://github.com/dcrypd/glovebox/releases)
[![Docker image](https://img.shields.io/badge/Container-GHCR-1d5fb8?style=flat-square&logo=docker&logoColor=white)](https://github.com/dcrypd/glovebox/pkgs/container/glovebox)
[![License: PolyForm Noncommercial](https://img.shields.io/badge/License-PolyForm_Noncommercial-1d5fb8?style=flat-square)](LICENSE.md)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-1d5fb8?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)


## Contents

- [Features](#features)
- [Demo](#demo)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Upgrading](#upgrading)
- [Backups and restore](#backups-and-restore)
- [Install as an app (PWA)](#install-as-an-app-pwa)
- [Security](#security)
- [How fuel economy is calculated](#how-fuel-economy-is-calculated)
- [Development](#development)
- [License](#license)

## Features

- **Multi-vehicle garage.** Gas or electric, miles or km, US gallons / litres / Imperial gallons / kWh, and a per-vehicle currency symbol. Units are fixed per vehicle, so historical records never change meaning.
- **Accurate fuel / energy log.** Handles partial fills and backdated edits. Economy is computed between full tanks (or full charges for EVs), and the dashboard average is distance-weighted. Electric vehicles track mi/kWh or km/kWh.
- **Sanity checks.** A *Check* badge flags any interval more than 40% from your median (once you have at least four intervals). Tick *I missed logging a fill-up before this one* to restart tracking cleanly.
- **Service records.** Oil changes, repairs, upgrades, taxes and fees, with costs and notes.
- **Attachments.** Attach receipts, photos, and PDFs to any fuel fill-up, charge, or service record. Images show as thumbnails, PDFs/text as file icons; open, download, or remove each file from the entry's edit modal. A paperclip badge with a count marks any entry that has attachments. Up to 25 MB per file.
- **Reminders.** One-time or recurring, by date, distance or both. *Done* pre-fills a service record and rolls recurring reminders forward.
- **Dashboard.** Latest odometer, average economy (mpg, L/100km or mi/kWh as appropriate), spending, cost per mile/km, upcoming reminders, recent activity, and a monthly chart (Spending, Distance or Efficiency; 12 or 24 months).
- **Quick-log QR codes.** Each vehicle gets its own scannable QR code. Print one and keep it in the fuel door; scanning it with a phone camera opens Glovebox ready to log a fill-up or charge for that vehicle. Reachable from the small QR icon in the Add fill-up modal head, with download and print options.
- **Export.** CSV and PDF from the Fuel and Service pages, or everything at once from **Settings > Export all data**. PDFs are print-friendly with repeating headers and *Page X of Y* footers.
- **Themes.** Light and dark, with a selectable accent color. Responsive, mobile-first forms for logging a fill-up at the pump or a charging session at the station.
- **Fully offline.** Fonts and assets are bundled. Nothing loads from a CDN.

## Demo

Try Glovebox without installing anything: **[Live demo](https://demo.tryglovebox.space)**

The demo resets periodically, and all data entered will be erased.

## Quick start

Requires Docker with the Compose plugin.

```bash
git clone https://github.com/dcrypd/glovebox.git
cd glovebox
docker compose up -d
```

Open <http://localhost:8080> and add your first vehicle.

Data lives in the named volume `glovebox_data` (mounted at `/data`). It survives restarts, upgrades and container replacement.

### Alternative: plain Docker

Use this instead of Compose, not in addition to it. Both create a container named `glovebox`.

```bash
docker build -t glovebox .
docker run -d --name glovebox \
  -p 127.0.0.1:8080:8000 \
  -v glovebox_data:/data \
  glovebox
```

### Alternative: no Docker

Requires Python 3.11+.

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## Configuration

| Setting | Default | Notes |
|---|---|---|
| Host port | `127.0.0.1:8080` | Change the port mapping in `docker-compose.yml`. Use `"8080:8000"` for LAN access. |
| Container port | `8000` | Internal; normally leave as is. |
| `GLOVEBOX_DB` | `/data/glovebox.db` in Docker, `./data/glovebox.db` otherwise | Path to the SQLite database. |
| Attachments dir | `/data/attachments/` in Docker, `./data/attachments/` otherwise | Sibling of the DB; holds uploaded files. Created on first upload. |
| Volume | `glovebox_data` | Mounted at `/data`. |

## Upgrading

With Compose (builds from local source):

```bash
git pull
docker compose up -d --build
```

With plain Docker:

```bash
git pull
docker build -t glovebox .
docker rm -f glovebox
docker run -d --name glovebox -p 127.0.0.1:8080:8000 -v glovebox_data:/data glovebox
```

Database changes ship as numbered migrations in `app/db.py` and apply automatically at startup. Your data is kept. Back up before major upgrades.

## Backups and restore

Everything lives in the `/data` volume: the SQLite database file `/data/glovebox.db` (plus its `-wal` and `-shm` sidecar files while the app runs) and the `/data/attachments/` directory of uploaded files. **Back up both** — the database alone will restore your entries but not their attachments.

**Option A: stop, copy, start**

```bash
docker compose stop
docker run --rm -v glovebox_data:/data -v "$PWD":/backup alpine \
  tar czf /backup/glovebox-backup.tgz -C /data .
docker compose start
```

**Option B: online snapshot (safe while running)**

```bash
docker compose exec glovebox python -c \
  "import sqlite3; sqlite3.connect('/data/glovebox.db').execute(\"VACUUM INTO '/tmp/backup.db'\")"
docker compose cp glovebox:/tmp/backup.db ./glovebox-backup.db
docker compose cp glovebox:/data/attachments ./attachments-backup
```

**Restore:** stop the container, replace the database file *and* the `attachments/` directory in the volume, then start it again.

## Install as an app (PWA)

Glovebox installs to your home screen, opens full-screen, and keeps its interface working offline. Your data still needs the server.

- **Android (Chrome):** More → Install and create shortcut → *Install*.
- **iPhone/iPad (Safari):** Share → *Add to Home Screen*.

Browsers require **HTTPS** (or `localhost`) for install and offline support. The default Compose file listens on `127.0.0.1`, so to install from a phone, expose Glovebox through an HTTPS reverse proxy or private tunnel (for example Caddy or Tailscale Serve). Over plain HTTP the app still works in the browser, and iOS can still add it to the home screen without offline support.

Maintainer notes:

- If you change the cached file list in `static/sw.js`, bump its `VERSION`.
- If you serve through a CDN or caching proxy (such as Cloudflare), purge `js/app.js`, `css/styles.css` and `sw.js` after upgrading. Otherwise phones may keep running the old interface.

## Security

Glovebox has **no authentication**. It is designed for a trusted home network.

- The default Compose file binds to `127.0.0.1` only.
- For LAN access, change the mapping to `"8080:8000"`.
- Beyond your LAN, put it behind a reverse proxy with authentication (Authelia, nginx basic auth, Tailscale, Cloudflare Access).

## How fuel economy is calculated

A valid interval starts at one full tank (or full charge, for electric vehicles) and ends at the next.

- **Distance** is the difference in odometer readings.
- **Energy used** is every fill/charge after the starting full one (partials included) plus the ending fill/charge.
- **Baseline:** the first full fill produces no figure until the next full one.
- **Missed fill-up:** the open interval is discarded and tracking restarts there. A full tank/charge becomes the new baseline.
- **Edits, backdated entries and deletions** trigger a full recompute.
- **Dashboard average** is total distance divided by total energy across valid intervals, so a 300-mile road trip is not weighted equally with a short commute.
- **Monthly Efficiency** credits each interval to the month of its closing full tank/charge.
- **Monthly Distance** uses odometer readings from fuel and service entries: a month's highest reading minus the highest reading before it.

For electric vehicles, the same interval method applies, but energy is measured in kWh (stored as watt-hours) and the economy label is mi/kWh (or km/kWh). The same partial-fill, missed-fill and suspect-interval logic applies to charging sessions.

## Development

```bash
pip install -r requirements.txt pytest httpx
python -m pytest tests/ -q
```

```text
app/
  main.py        FastAPI routes, serialization, summaries, exports, attachments
  db.py          SQLite + migrations
  economy.py     Interval math (pure functions, heavily tested)
  models.py      Request schemas + unit conversions
  pdf_export.py  Branded PDF reports (ReportLab)
static/          Frontend (vanilla JS, no build step)
tests/           API and calculation tests
data/
  glovebox.db    SQLite database (created on first run)
  attachments/   Uploaded files, stored as UUIDs (created on first upload)
```

Money is stored as integer cents, fuel/energy as integer minor units (millilitres for gas vehicles, watt-hours for electric vehicles), and odometer readings as integer tenths of a unit, so the math is exact. Attachment files are stored on disk; the `attachments` table holds only metadata (original filename, on-disk stored name, MIME type, byte size) plus a polymorphic `(entry_type, entry_id)` reference to either `fuel_entries` or `service_entries`.

When changing the schema, add a new numbered migration to `app/db.py`. Never edit an existing one, because installs that already ran it will not re-run it.

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.

## License

Copyright (C) 2026 Daniel Phillips. Source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE.md).

- **Free for noncommercial use:** personal, hobby, research, charities, schools and government bodies.
- **Commercial use needs a separate license.** Contact the author.

Glovebox is not "open source" in the OSI sense, because the license restricts commercial use.
