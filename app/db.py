"""SQLite persistence for Glovebox.

The database lives under /data (Docker volume) with a small migration
framework so upgrades preserve existing data. A single connection per
request with a short busy timeout keeps write transactions brief.

Schema overview (one SQLite file, four tables):
  vehicles        one row per vehicle, with immutable type/unit fields
  fuel_entries     one row per fill-up / charge
  service_entries  one row per service/repair/upgrade/tax record
  reminders        one row per reminder (one-shot or recurring)
  settings         generic key/value store for instance-wide settings

Money is integer cents, fuel/energy is the integer minor unit of the
vehicle's fuel_unit (ml for gal/l/gal_uk, Wh for kwh), odometer readings
are integer tenths of the vehicle's distance unit. Keeping everything as
integers means the economy math in app/economy.py is exact.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

MIGRATIONS: list[str] = [
    # Numbered SQL scripts applied in order at startup. Each script is wrapped
    # in a single transaction with its version recorded in schema_migrations,
    # so an interrupted upgrade leaves no half-applied state. Add new
    # migrations by appending to this list — never edit an existing one,
    # because installs that already ran it won't re-run it.
    # 1 — initial schema
    """
    CREATE TABLE vehicles (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL,
        year          INTEGER,
        make          TEXT,
        model         TEXT,
        plate         TEXT,
        fuel_type     TEXT    NOT NULL DEFAULT 'gas',
        distance_unit TEXT    NOT NULL DEFAULT 'mi',
        fuel_unit     TEXT    NOT NULL DEFAULT 'gal',
        currency      TEXT    NOT NULL DEFAULT '$',
        archived      INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    """,
    """
    CREATE TABLE fuel_entries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_id      INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
        date            TEXT    NOT NULL,
        odometer_tenths INTEGER NOT NULL,
        -- quantity_minor is the integer minor unit of the energy
        -- quantity: millilitres for gas vehicles (gal/l/gal_uk) and
        -- watt-hours for electric vehicles (kwh). The vehicle's fuel_unit
        -- decides which conversion factor to apply at read time.
        quantity_minor  INTEGER NOT NULL,
        cost_cents      INTEGER NOT NULL DEFAULT 0,
        full_tank       INTEGER NOT NULL DEFAULT 1,
        missed_before   INTEGER NOT NULL DEFAULT 0,
        station         TEXT,
        notes           TEXT,
        created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX idx_fuel_vehicle ON fuel_entries(vehicle_id, date);
    """,
    """
    CREATE TABLE service_entries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_id      INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
        date            TEXT    NOT NULL,
        odometer_tenths INTEGER,
        description     TEXT    NOT NULL,
        category        TEXT    NOT NULL DEFAULT 'service',
        cost_cents      INTEGER,
        notes           TEXT,
        created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX idx_service_vehicle ON service_entries(vehicle_id, date);
    """,
    """
    CREATE TABLE reminders (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_id            INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
        title                 TEXT    NOT NULL,
        notes                 TEXT,
        due_date              TEXT,
        due_odometer_tenths   INTEGER,
        recur_distance_tenths INTEGER,
        recur_months          INTEGER,
        done                  INTEGER NOT NULL DEFAULT 0,
        completed_at          TEXT,
        created_at            TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX idx_reminders_vehicle ON reminders(vehicle_id, done);
    """,
    # 5 — instance-wide settings (generic key/value store)
    # One row per setting, shared by everyone using this instance. Adding a new
    # setting needs NO schema change: just pick a new key (e.g. 'accent',
    # 'default_currency') and use Database.get_setting / set_setting. Values
    # are stored as text — encode other types (JSON, "1"/"0") at the call site.
    # Defaults live in code, not in the table, so an absent row means "default".
    """
    CREATE TABLE settings (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    """,
    # 6 — attachments for fuel/service entries (receipts, photos, PDFs, …)
    # Files live on disk under <data>/attachments/ (a sibling of the SQLite DB,
    # so in Docker they share the persistent /data volume and survive restarts
    # and container rebuilds). This row holds the metadata: the original
    # filename (for display), the on-disk stored_name (a uuid + extension so
    # same-name uploads don't collide and the original filename never touches
    # the filesystem), mime_type and size_bytes.
    #
    # entry_type ('fuel' | 'service') + entry_id form a polymorphic reference
    # to either fuel_entries or service_entries — there's no FK because a single
    # column can't reference two tables. Cascade cleanup is handled explicitly
    # in the fuel/service/vehicle delete routes (see app/main.py), which delete
    # the on-disk file alongside the row. vehicle_id IS a real FK with ON DELETE
    # CASCADE as a backstop, but the delete-vehicle route still purges the files
    # before the cascade removes the rows.
    """
    CREATE TABLE attachments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        vehicle_id  INTEGER NOT NULL REFERENCES vehicles(id) ON DELETE CASCADE,
        entry_type  TEXT    NOT NULL,
        entry_id    INTEGER NOT NULL,
        filename    TEXT    NOT NULL,
        stored_name TEXT    NOT NULL,
        mime_type   TEXT,
        size_bytes  INTEGER,
        created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
    );
    CREATE INDEX idx_attachments_entry ON attachments(entry_type, entry_id);
    """,
]

DATA_DIR = Path(__file__).resolve().parent.parent / "data"


class Database:
    """Thin wrapper around sqlite3 with a per-thread connection cache.

    Each thread gets its own Connection (sqlite3.Connection objects can't
    be shared across threads by default). WAL mode + foreign keys + a short
    busy_timeout are set once per connection so writes don't block reads and
    ON DELETE CASCADE works without extra triggers.
    """
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._local = threading.local()
        self._migrate()

    # -- connection -----------------------------------------------------

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(self.path, timeout=10)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode = WAL")
            c.execute("PRAGMA foreign_keys = ON")
            c.execute("PRAGMA busy_timeout = 5000")
            self._local.conn = c
        return c

    def _migrate(self) -> None:
        """Apply any pending migrations in order. Each migration runs inside a
        transaction with its version recorded in schema_migrations, so an
        interrupted upgrade leaves no half-applied state. Already-applied
        migrations are skipped (their versions are in the `applied` set)."""
        c = self.conn()
        c.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL"
            " DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')))"
        )
        applied = {
            r["version"] for r in c.execute("SELECT version FROM schema_migrations")
        }
        for i, script in enumerate(MIGRATIONS, start=1):
            if i in applied:
                continue
            with c:
                c.executescript(script)
                c.execute("INSERT INTO schema_migrations (version) VALUES (?)", (i,))

    # -- helpers ---------------------------------------------------------
    # Thin shorthands over conn().execute. `execute` opens a transaction
    # (the `with c:` block commits on success / rolls back on exception) and
    # returns lastrowid so INSERT callers get the new row's id back.

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self.conn().execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self.conn().execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple = ()) -> int:
        c = self.conn()
        with c:
            cur = c.execute(sql, params)
        return cur.lastrowid

    # -- instance-wide settings -------------------------------------------
    # The settings table is a generic key/value store shared by everyone on
    # this instance (currently only `accent`). Adding a new setting needs
    # no schema change — just pick a key and use these helpers. Defaults live
    # in code (the `default` arg to get_setting), not in the table, so an
    # absent row means "use the default".

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')",
            (key, value),
        )
