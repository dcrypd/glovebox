"""Glovebox — a simple, modern self-hosted vehicle log.

FastAPI + SQLite, single container, no external services.
"""

from __future__ import annotations

import calendar
import csv
import io
import mimetypes
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .db import Database
from .economy import FUEL_UNIT_LABELS, QUANTITY_PER_UNIT, economy_label, compute_fuel_stats, quantity_from_minor
from .pdf_export import all_data_pdf, fuel_pdf, service_pdf
from .models import (
    FuelIn,
    FuelPatch,
    ReminderIn,
    ReminderPatch,
    ServiceIn,
    ServicePatch,
    SettingsIn,
    VehicleIn,
    VehiclePatch,
    to_cents,
    to_minor_units,
    to_tenths,
)

DB_PATH = os.environ.get("GLOVEBOX_DB", str(Path(__file__).resolve().parent.parent / "data" / "Glovebox.db"))
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
# Attachments are stored on disk in a sibling directory of the SQLite DB, so in
# Docker they share the persistent /data volume (and survive container rebuilds),
# and locally they land in ./data/attachments/. The directory is created lazily
# on first upload / first file access (see _attachments_dir()).
ATTACH_DIR = Path(DB_PATH).resolve().parent / "attachments"

__version__ = "1.0.1"

app = FastAPI(title="Glovebox", version=__version__, docs_url=None, redoc_url=None)
db = Database(DB_PATH)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def row_vehicle(r) -> dict:
    """Serialize a vehicle plus its counts and latest odometer in a single query.

    One round-trip fetches the fuel/service counts, active reminders, and
    the highest odometer reading across both tables — so the vehicle list
    and the dropdown don't need an N+1 follow-up per row. Returns the
    JSON shape the frontend's state.vehicles stores.
    """
    s = db.one(
        "SELECT"
        " (SELECT COUNT(*) FROM fuel_entries WHERE vehicle_id=?) AS fuel_count,"
        " (SELECT MAX(odometer_tenths) FROM fuel_entries WHERE vehicle_id=?) AS fuel_max,"
        " (SELECT COUNT(*) FROM service_entries WHERE vehicle_id=?) AS service_count,"
        " (SELECT MAX(odometer_tenths) FROM service_entries WHERE vehicle_id=?) AS service_max,"
        " (SELECT COUNT(*) FROM reminders WHERE vehicle_id=? AND done=0) AS active_reminders",
        (r["id"],) * 5,
    )
    odo = [x for x in (s["fuel_max"], s["service_max"]) if x is not None]
    return {
        "id": r["id"],
        "name": r["name"],
        "year": r["year"],
        "make": r["make"],
        "model": r["model"],
        "plate": r["plate"],
        "fuel_type": r["fuel_type"],
        "distance_unit": r["distance_unit"],
        "fuel_unit": r["fuel_unit"],
        "currency": r["currency"],
        "archived": bool(r["archived"]),
        "fuel_count": s["fuel_count"],
        "service_count": s["service_count"],
        "active_reminders": s["active_reminders"],
        "latest_odometer": (max(odo) / 10) if odo else None,
    }


def get_vehicle(vid: int) -> dict:
    """Fetch + serialize a vehicle, raising 404 if missing. Use this when
    you want the JSON-ready shape; use get_vehicle_raw when you need the
    underlying sqlite3.Row (e.g. to read fuel_unit for a unit conversion)."""
    r = db.one("SELECT * FROM vehicles WHERE id=?", (vid,))
    if r is None:
        raise HTTPException(404, "vehicle not found")
    return row_vehicle(r)


def get_vehicle_raw(vid: int):
    """Fetch the raw sqlite3.Row for a vehicle, raising 404 if missing.
    Used by routes that need to read fuel_unit / fuel_type / distance_unit
    for conversions (the serialized dict has them too, but the raw row is
    what the conversion helpers expect)."""
    r = db.one("SELECT * FROM vehicles WHERE id=?", (vid,))
    if r is None:
        raise HTTPException(404, "vehicle not found")
    return r


def row_fuel(r, vehicle, stats, attach_counts=None) -> dict:
    """Serialize one fuel/charge row for the API.

    `vehicle` is the raw vehicle row (for fuel_unit / currency). `stats` is
    the FuelStats computed over the whole entry list — we use it to look up
    the per-row economy (only the closing entry of each interval has one)
    and the suspect flag. `attach_counts` (optional) is a {entry_id: count}
    map so the Fuel table can show a paperclip badge without an N+1 query
    per row; absent means 0.
    """
    minor_unit = QUANTITY_PER_UNIT[vehicle["fuel_unit"]]
    entry = {
        "id": r["id"],
        "date": r["date"],
        "odometer": r["odometer_tenths"] / 10,
        "quantity": float(quantity_from_minor(r["quantity_minor"], vehicle["fuel_unit"])),
        "cost": r["cost_cents"] / 100,
        "full_tank": bool(r["full_tank"]),
        "missed_before": bool(r["missed_before"]),
        # suspect flag is computed by compute_fuel_stats() — true when this
        # row closes an interval whose economy is far from the median.
        "suspect": r["id"] in stats.suspect_ids,
        "station": r["station"],
        "notes": r["notes"],
        # Price per energy unit (e.g. $/gal or $/kWh) for this fill. None
        # when quantity is zero (shouldn't normally happen, but the API
        # guards against div-by-zero).
        "unit_price": (
            float((Decimal(r["cost_cents"]) / Decimal(100)) / (Decimal(r["quantity_minor"]) / minor_unit))
            if r["quantity_minor"]
            else None
        ),
        # Number of files attached to this entry (for the paperclip badge).
        # fuel_payload() fetches all counts for the vehicle in one query and
        # passes the map in, so this is O(1) per row.
        "attachment_count": (attach_counts or {}).get(r["id"], 0),
    }
    # Only attach `economy` to the closing entry of each interval — partial
    # fills and the baseline full tank have no economy figure.
    interval = stats.closing.get(r["id"])
    if interval is not None:
        entry["economy"] = float(
            interval.economy(vehicle["fuel_unit"]).quantize(Decimal("0.01"))
        )
    return entry


def fuel_entries_for(vid: int):
    """Return a vehicle's fuel/charge rows sorted oldest-first (the order
    compute_fuel_stats expects). Other routes that want newest-first reverse
    the result themselves."""
    return db.query(
        "SELECT * FROM fuel_entries WHERE vehicle_id=? ORDER BY date, id", (vid,)
    )


def fuel_payload(vid: int) -> dict:
    """Build the full Fuel-page payload: vehicle, summary, per-row entries
    (newest-first), and the per-interval summary the chart could use.

    This is what GET /api/vehicles/{vid}/fuel returns, and what the PDF
    and CSV exporters reuse — so any change to the per-row shape propagates
    to all three outputs consistently.
    """
    vehicle = get_vehicle_raw(vid)
    rows = fuel_entries_for(vid)
    stats = compute_fuel_stats([dict(r) for r in rows], vehicle["fuel_unit"])
    acounts = attachment_counts_for(vid, "fuel")
    entries = [row_fuel(r, vehicle, stats, acounts) for r in reversed(rows)]
    avg = stats.avg_economy(vehicle["fuel_unit"])
    cpd = stats.cost_per_distance()
    summary = {
        "economy_label": economy_label(vehicle["distance_unit"], vehicle["fuel_unit"]),
        "avg_economy": float(avg.quantize(Decimal("0.01"))) if avg is not None else None,
        "cost_per_distance": float(cpd.quantize(Decimal("0.001"))) if cpd is not None else None,
        "distance_logged": float(stats.total_distance),
        # total_fuel_spend sums every row's cost, not just valid intervals —
        # it's "how much you spent at the pump/charger", which the user
        # expects to match their bank statements.
        "total_fuel_spend": sum(r["cost_cents"] for r in rows) / 100,
    }
    end_dates = {r["id"]: r["date"] for r in rows}
    return {
        "vehicle": row_vehicle(vehicle),
        "entries": entries,
        "summary": summary,
        "intervals": [
            {
                "end_date": end_dates[iv.end_entry_id],
                "distance": float(iv.distance),
                "economy": float(
                    iv.economy(vehicle["fuel_unit"]).quantize(Decimal("0.01"))
                ),
            }
            for iv in stats.intervals
        ],
    }


def row_service(r, vehicle, attach_counts=None) -> dict:
    return {
        "id": r["id"],
        "date": r["date"],
        "odometer": (r["odometer_tenths"] / 10) if r["odometer_tenths"] is not None else None,
        "description": r["description"],
        "category": r["category"],
        "cost": (r["cost_cents"] / 100) if r["cost_cents"] is not None else None,
        "notes": r["notes"],
        # Number of files attached to this record (for the paperclip badge).
        # list_service() fetches all counts for the vehicle in one query; the
        # PDF/CSV export call sites pass None (0), since they don't render it.
        "attachment_count": (attach_counts or {}).get(r["id"], 0),
    }


def reminder_months_ahead(base: date, months: int) -> date:
    """Add calendar months, clamping the day to the target month's last day
    (Jan 31 + 1 month = Feb 28/29, not Mar 1).

    Used by complete_reminder() to advance recurring reminders. Plain
    `date + timedelta(days=30)` would drift across month boundaries.
    """
    y, m = divmod(base.month - 1 + months, 12)
    y += base.year
    m += 1
    last = calendar.monthrange(y, m)[1]
    return date(y, m, min(base.day, last))


def reminder_status(rem: dict, latest_odo: float | None, today: date) -> tuple[str, int | None]:
    """Return (status, days_or_miles_left) for an active reminder.

    Status is one of: overdue (past due_date or due_odometer), soon (≤30 days
    or ≤500 distance-units left), or upcoming. The 30-day / 500-distance
    thresholds are picked so the dashboard badge is informative without
    crying wolf — they're not user-configurable.
    """
    days_left = None
    if rem["due_date"]:
        d = date.fromisoformat(rem["due_date"])
        days_left = (d - today).days
        if days_left < 0:
            return "overdue", days_left
    if rem["due_odometer_tenths"] is not None and latest_odo is not None:
        miles_left = (rem["due_odometer_tenths"] / 10) - latest_odo
        if miles_left <= 0:
            return "overdue", int(miles_left)
        if days_left is None or days_left > 30:
            if miles_left <= 500:
                return "soon", int(miles_left)
    if days_left is not None and days_left <= 30:
        return "soon", days_left
    return "upcoming", days_left


def row_reminder(r, latest_odo: float | None) -> dict:
    d = dict(r)
    status, left = reminder_status(d, latest_odo, date.today())
    return {
        "id": d["id"],
        "title": d["title"],
        "notes": d["notes"],
        "due_date": d["due_date"],
        "due_odometer": (d["due_odometer_tenths"] / 10) if d["due_odometer_tenths"] is not None else None,
        "recur_distance": (d["recur_distance_tenths"] / 10) if d["recur_distance_tenths"] is not None else None,
        "recur_months": d["recur_months"],
        "done": bool(d["done"]),
        "status": status,
        "days_left": left,
    }


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Settings (instance-wide)
# ---------------------------------------------------------------------------
# Accent color is the only instance-wide setting today, but the settings table
# is a generic key/value store (see db.py) so adding a new one needs no schema
# change — just pick a key and use db.get_setting / db.set_setting.

ACCENT_COLORS = ("amber", "teal", "blue", "crimson", "violet")
DEFAULT_ACCENT = "blue"


def get_accent() -> str:
    value = db.get_setting("accent", DEFAULT_ACCENT)
    return value if value in ACCENT_COLORS else DEFAULT_ACCENT


@app.get("/api/settings")
def read_settings():
    return {"accent": get_accent(), "accent_options": list(ACCENT_COLORS), "version": __version__}


@app.put("/api/settings")
def update_settings(body: SettingsIn):
    if body.accent is not None:
        if body.accent not in ACCENT_COLORS:
            raise HTTPException(422, "Unknown accent color.")
        db.set_setting("accent", body.accent)
    return read_settings()


# ---------------------------------------------------------------------------
# Vehicles
# ---------------------------------------------------------------------------

@app.get("/api/vehicles")
def list_vehicles():
    return [row_vehicle(r) for r in db.query("SELECT * FROM vehicles ORDER BY archived, name")]


@app.post("/api/vehicles", status_code=201)
def create_vehicle(body: VehicleIn):
    vid = db.execute(
        "INSERT INTO vehicles (name, year, make, model, plate, fuel_type, distance_unit, fuel_unit, currency)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (body.name.strip(), body.year, body.make, body.model, body.plate,
         body.fuel_type, body.distance_unit, body.fuel_unit, body.currency[:4] or "$"),
    )
    return get_vehicle(vid)


@app.get("/api/vehicles/{vid}")
def read_vehicle(vid: int):
    return get_vehicle(vid)


@app.patch("/api/vehicles/{vid}")
def update_vehicle(vid: int, body: VehiclePatch):
    """Partial-update a vehicle. Only the rename-able fields are patchable;
    fuel_type / distance_unit / fuel_unit are NOT (they're immutable after
    creation — see VehiclePatch). Omitted fields stay unchanged; explicit
    null clears a nullable value (year/make/model/plate)."""
    get_vehicle_raw(vid)
    sets, vals = [], []
    provided = body.model_fields_set
    if "name" in provided and body.name is not None:
        sets.append("name=?"); vals.append(body.name.strip())
    if "year" in provided:
        sets.append("year=?"); vals.append(body.year)
    if "make" in provided:
        sets.append("make=?"); vals.append(body.make or None)
    if "model" in provided:
        sets.append("model=?"); vals.append(body.model or None)
    if "plate" in provided:
        sets.append("plate=?"); vals.append(body.plate or None)
    if "currency" in provided and body.currency:
        sets.append("currency=?"); vals.append(body.currency[:4])
    if body.archived is not None:
        sets.append("archived=?"); vals.append(1 if body.archived else 0)
    if sets:
        vals.append(vid)
        db.execute(f"UPDATE vehicles SET {', '.join(sets)} WHERE id=?", tuple(vals))
    return get_vehicle(vid)


@app.delete("/api/vehicles/{vid}")
def delete_vehicle(vid: int):
    get_vehicle_raw(vid)
    # Purge every attachment file for this vehicle before the vehicle row (and
    # its cascaded fuel/service/reminder/attachment rows) go away. The ON
    # DELETE CASCADE on attachments.vehicle_id removes the rows, but it can't
    # unlink the files on disk — so we do that explicitly here.
    delete_attachments_for_vehicle(vid)
    db.execute("DELETE FROM vehicles WHERE id=?", (vid,))
    return {"deleted": vid}


# ---------------------------------------------------------------------------
# Fuel
# ---------------------------------------------------------------------------

@app.get("/api/vehicles/{vid}/fuel")
def list_fuel(vid: int):
    return fuel_payload(vid)


@app.post("/api/vehicles/{vid}/fuel", status_code=201)
def create_fuel(vid: int, body: FuelIn):
    vehicle = get_vehicle_raw(vid)
    minor = to_minor_units(body.quantity, vehicle["fuel_unit"])
    cents = to_cents(body.cost) or 0
    fid = db.execute(
        "INSERT INTO fuel_entries (vehicle_id, date, odometer_tenths, quantity_minor, cost_cents, full_tank,"
        " missed_before, station, notes)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (vid, body.date, to_tenths(body.odometer), minor, cents,
         1 if body.full_tank else 0, 1 if body.missed_before else 0, body.station, body.notes),
    )
    payload = fuel_payload(vid)
    return {"id": fid, **payload}


@app.patch("/api/fuel/{fid}")
def update_fuel(fid: int, body: FuelPatch):
    """Partial-update a fuel/charge row. Re-reads the vehicle so quantity
    conversions use the right fuel_unit (which is immutable on the vehicle).
    Omitted fields stay unchanged; explicit null clears a nullable value."""
    r = db.one("SELECT * FROM fuel_entries WHERE id=?", (fid,))
    if r is None:
        raise HTTPException(404, "entry not found")
    vehicle = get_vehicle_raw(r["vehicle_id"])
    sets, vals = [], []
    provided = body.model_fields_set
    if "date" in provided and body.date is not None:
        sets.append("date=?"); vals.append(body.date)
    if "odometer" in provided and body.odometer is not None:
        sets.append("odometer_tenths=?"); vals.append(to_tenths(body.odometer))
    if "quantity" in provided and body.quantity is not None:
        sets.append("quantity_minor=?"); vals.append(to_minor_units(body.quantity, vehicle["fuel_unit"]))
    if "cost" in provided and body.cost is not None:
        sets.append("cost_cents=?"); vals.append(to_cents(body.cost) or 0)
    if body.full_tank is not None:
        sets.append("full_tank=?"); vals.append(1 if body.full_tank else 0)
    if body.missed_before is not None:
        sets.append("missed_before=?"); vals.append(1 if body.missed_before else 0)
    if "station" in provided:
        sets.append("station=?"); vals.append(body.station or None)
    if "notes" in provided:
        sets.append("notes=?"); vals.append(body.notes or None)
    if sets:
        vals.append(fid)
        db.execute(f"UPDATE fuel_entries SET {', '.join(sets)} WHERE id=?", tuple(vals))
    return fuel_payload(r["vehicle_id"])


@app.delete("/api/fuel/{fid}")
def delete_fuel(fid: int):
    r = db.one("SELECT * FROM fuel_entries WHERE id=?", (fid,))
    if r is None:
        raise HTTPException(404, "entry not found")
    # Drop this entry's attachments (rows + on-disk files) before the entry row
    # goes away — there's no FK from attachments.entry_id to fuel_entries, so
    # the cleanup is explicit.
    delete_attachments_for("fuel", fid)
    db.execute("DELETE FROM fuel_entries WHERE id=?", (fid,))
    return {"deleted": fid}


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

@app.get("/api/vehicles/{vid}/service")
def list_service(vid: int):
    get_vehicle_raw(vid)
    rows = db.query(
        "SELECT * FROM service_entries WHERE vehicle_id=? ORDER BY date DESC, id DESC", (vid,)
    )
    # Fetch attachment counts for every service record of this vehicle in one
    # query, so the Records table can show a paperclip badge per row without
    # an N+1 follow-up.
    acounts = attachment_counts_for(vid, "service")
    return [row_service(r, None, acounts) for r in rows]


@app.post("/api/vehicles/{vid}/service", status_code=201)
def create_service(vid: int, body: ServiceIn):
    get_vehicle_raw(vid)
    cents = to_cents(body.cost)
    sid = db.execute(
        "INSERT INTO service_entries (vehicle_id, date, odometer_tenths, description, category, cost_cents, notes)"
        " VALUES (?,?,?,?,?,?,?)",
        (vid, body.date, to_tenths(body.odometer) if body.odometer is not None else None,
         body.description.strip(), body.category, cents, body.notes),
    )
    return {"id": sid}


@app.patch("/api/service/{sid}")
def update_service(sid: int, body: ServicePatch):
    r = db.one("SELECT * FROM service_entries WHERE id=?", (sid,))
    if r is None:
        raise HTTPException(404, "entry not found")
    sets, vals = [], []
    provided = body.model_fields_set
    if "date" in provided and body.date is not None:
        sets.append("date=?"); vals.append(body.date)
    if "odometer" in provided:
        sets.append("odometer_tenths=?"); vals.append(to_tenths(body.odometer) if body.odometer is not None else None)
    if "description" in provided and body.description is not None:
        sets.append("description=?"); vals.append(body.description.strip())
    if "category" in provided and body.category is not None:
        sets.append("category=?"); vals.append(body.category)
    if "cost" in provided:
        sets.append("cost_cents=?"); vals.append(to_cents(body.cost))
    if "notes" in provided:
        sets.append("notes=?"); vals.append(body.notes or None)
    if sets:
        vals.append(sid)
        db.execute(f"UPDATE service_entries SET {', '.join(sets)} WHERE id=?", tuple(vals))
    return {"ok": True}


@app.delete("/api/service/{sid}")
def delete_service(sid: int):
    r = db.one("SELECT * FROM service_entries WHERE id=?", (sid,))
    if r is None:
        raise HTTPException(404, "entry not found")
    # Drop this record's attachments (rows + on-disk files) before the row goes
    # away — same explicit-cleanup reason as delete_fuel.
    delete_attachments_for("service", sid)
    db.execute("DELETE FROM service_entries WHERE id=?", (sid,))
    return {"deleted": sid}


# ---------------------------------------------------------------------------
# Reminders
# ---------------------------------------------------------------------------

@app.get("/api/vehicles/{vid}/reminders")
def list_reminders(vid: int):
    get_vehicle_raw(vid)
    rows = db.query(
        "SELECT * FROM reminders WHERE vehicle_id=? AND done=0 ORDER BY due_date IS NULL, due_date",
        (vid,),
    )
    latest_odo = vehicle_latest_odo(vid)
    return [row_reminder(r, latest_odo) for r in rows]


def vehicle_latest_odo(vid: int) -> float | None:
    o = db.one(
        "SELECT MAX(o) m FROM ("
        " SELECT MAX(odometer_tenths) o FROM fuel_entries WHERE vehicle_id=?"
        " UNION ALL SELECT MAX(odometer_tenths) o FROM service_entries WHERE vehicle_id=?"
        ") WHERE o IS NOT NULL",
        (vid, vid),
    )
    return (o["m"] / 10) if o and o["m"] is not None else None


@app.post("/api/vehicles/{vid}/reminders", status_code=201)
def create_reminder(vid: int, body: ReminderIn):
    get_vehicle_raw(vid)
    rid = db.execute(
        "INSERT INTO reminders (vehicle_id, title, notes, due_date, due_odometer_tenths,"
        " recur_distance_tenths, recur_months)"
        " VALUES (?,?,?,?,?,?,?)",
        (vid, body.title.strip(), body.notes, body.due_date,
         to_tenths(body.due_odometer) if body.due_odometer is not None else None,
         to_tenths(body.recur_distance) if body.recur_distance is not None else None,
         body.recur_months),
    )
    return {"id": rid}


@app.patch("/api/reminders/{rid}")
def update_reminder(rid: int, body: ReminderPatch):
    r = db.one("SELECT * FROM reminders WHERE id=?", (rid,))
    if r is None:
        raise HTTPException(404, "reminder not found")
    sets, vals = [], []
    provided = body.model_fields_set
    if "title" in provided and body.title is not None:
        sets.append("title=?"); vals.append(body.title.strip())
    if "notes" in provided:
        sets.append("notes=?"); vals.append(body.notes or None)
    if "due_date" in provided:
        sets.append("due_date=?"); vals.append(body.due_date or None)
    if "due_odometer" in provided:
        sets.append("due_odometer_tenths=?"); vals.append(to_tenths(body.due_odometer) if body.due_odometer is not None else None)
    if "recur_distance" in provided:
        sets.append("recur_distance_tenths=?"); vals.append(to_tenths(body.recur_distance) if body.recur_distance is not None else None)
    if "recur_months" in provided:
        sets.append("recur_months=?"); vals.append(body.recur_months)
    if body.done is not None:
        sets.append("done=?"); vals.append(1 if body.done else 0)
        sets.append("completed_at=?"); vals.append(datetime.now(timezone.utc).isoformat() if body.done else None)
    if sets:
        vals.append(rid)
        db.execute(f"UPDATE reminders SET {', '.join(sets)} WHERE id=?", tuple(vals))
    return {"ok": True}


@app.post("/api/reminders/{rid}/complete")
def complete_reminder(rid: int):
    """Mark a reminder done, OR — if it's a recurring reminder — advance its
    due_date/due_odometer by the recur interval instead.

    Recurring reminders never complete: they roll forward so the next
    interval is due. Non-recurring reminders just flip to done=1. The
    advance also clears any criterion that doesn't recur (e.g. a
    distance-only recurring reminder clears its stale date) so a
    long-expired date can't keep the reminder overdue forever.
    """
    r = db.one("SELECT * FROM reminders WHERE id=?", (rid,))
    if r is None:
        raise HTTPException(404, "reminder not found")
    today = date.today()
    recurring = r["recur_months"] is not None or r["recur_distance_tenths"] is not None
    if recurring:
        # Advance every recurring criterion; clear criteria that don't recur
        # so a stale deadline can't keep the reminder overdue forever.
        new_date = (
            reminder_months_ahead(today, r["recur_months"])
            if r["recur_months"] is not None else None
        )
        latest = vehicle_latest_odo(r["vehicle_id"])
        new_odo = (
            to_tenths(latest + r["recur_distance_tenths"] / 10)
            if r["recur_distance_tenths"] is not None and latest is not None
            else None
        )
        db.execute(
            "UPDATE reminders SET due_date=?, due_odometer_tenths=? WHERE id=?",
            # Store an ISO string: passing a date object would rely on the
            # sqlite3 default date adapter, deprecated as of Python 3.12.
            (new_date.isoformat() if new_date else None, new_odo, rid),
        )
        return {"recurring": True, "due_date": new_date, "due_odometer": (new_odo / 10) if new_odo is not None else None}
    db.execute(
        "UPDATE reminders SET done=1, completed_at=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), rid),
    )
    return {"recurring": False, "done": True}


@app.delete("/api/reminders/{rid}")
def delete_reminder(rid: int):
    r = db.one("SELECT * FROM reminders WHERE id=?", (rid,))
    if r is None:
        raise HTTPException(404, "reminder not found")
    db.execute("DELETE FROM reminders WHERE id=?", (rid,))
    return {"deleted": rid}


# ---------------------------------------------------------------------------
# Summary / dashboard
# ---------------------------------------------------------------------------

@app.get("/api/vehicles/{vid}/summary")
def vehicle_summary(vid: int, span: int = Query(12, ge=1, le=60, alias="months")):
    """Dashboard payload: KPIs, monthly spending/distance/efficiency chart
    data, recent activity, and upcoming reminders for the selected vehicle.

    `months` controls the chart range (12 default, 24 max). All monthly
    aggregation happens here so the frontend gets a ready-to-render array.
    """
    vehicle = get_vehicle_raw(vid)
    fuel_rows = fuel_entries_for(vid)
    stats = compute_fuel_stats([dict(r) for r in fuel_rows], vehicle["fuel_unit"])
    fuel_spend = sum(r["cost_cents"] for r in fuel_rows)
    svc_rows = db.query(
        "SELECT * FROM service_entries WHERE vehicle_id=? ORDER BY date, id", (vid,)
    )
    svc_spend = sum(r["cost_cents"] or 0 for r in svc_rows)

    today = date.today()
    # Build the month buckets newest-last so the chart reads left-to-right.
    # Each bucket starts at zero for fuel/service spend; distance/efficiency
    # are filled in below.
    months = []
    for i in range(span - 1, -1, -1):
        y, m = divmod(today.month - 1 - i, 12)
        months.append({"ym": f"{today.year + y:04d}-{m + 1:02d}", "fuel": 0, "service": 0})
    idx = {mo["ym"]: mo for mo in months}

    # Distance per month: the month's highest odometer reading minus the highest
    # reading before it. One left-to-right pass over sorted readings (a running
    # max) instead of rescanning all readings for every month.
    # Using max-in-month minus max-before-month (rather than first-minus-last)
    # handles sparse logging: a month with only one reading still gets a
    # sensible distance from the previous month's high-water mark.
    readings = sorted(
        (r["date"][:7], r["odometer_tenths"]) for r in (*fuel_rows, *svc_rows)
        if r["odometer_tenths"] is not None
    )
    i, prev_max = 0, None
    for mo in months:
        lo = hi = None
        # Consume any readings before this month, advancing the running max.
        while i < len(readings) and readings[i][0] < mo["ym"]:
            o = readings[i][1]
            if prev_max is None or o > prev_max:
                prev_max = o
            i += 1
        # Readings inside this month: track the month's min and max.
        while i < len(readings) and readings[i][0] == mo["ym"]:
            o = readings[i][1]
            if lo is None or o < lo:
                lo = o
            if hi is None or o > hi:
                hi = o
            i += 1
        if hi is None:
            mo["distance"] = 0
        else:
            # Distance = month's high minus the highest reading before the
            # month started (or the month's own low if there was no prior
            # reading, e.g. the very first month).
            start = prev_max if prev_max is not None else lo
            mo["distance"] = max(hi - start, 0) / 10
            if prev_max is None or hi > prev_max:
                prev_max = hi

    # Monthly efficiency: full-tank intervals credited to the month they close,
    # in the vehicle's own distance and fuel units. Crediting to the closing
    # month (not the starting one) matches how a user thinks about it — "in
    # March I got X mpg" — and keeps a long road-trip interval from inflating
    # the month it started in.
    closed_on = {r["id"]: r["date"][:7] for r in fuel_rows}
    eff_totals: dict[str, list[int]] = {}
    for iv in stats.intervals:
        tot = eff_totals.setdefault(closed_on[iv.end_entry_id], [0, 0])
        tot[0] += iv.distance_tenths
        tot[1] += iv.quantity_minor
    for mo in months:
        dist_tenths, qty_minor = eff_totals.get(mo["ym"], (0, 0))
        mo["efficiency"] = (
            float(((Decimal(dist_tenths) / 10) / (Decimal(qty_minor) / QUANTITY_PER_UNIT[vehicle["fuel_unit"]]))
                  .quantize(Decimal("0.01")))
            if qty_minor > 0 else None
        )

    for r in fuel_rows:
        mo = idx.get(r["date"][:7])
        if mo:
            mo["fuel"] += r["cost_cents"]
    for r in svc_rows:
        mo = idx.get(r["date"][:7])
        if mo:
            mo["service"] += r["cost_cents"] or 0

    is_electric = vehicle["fuel_type"] == "electric"
    recent = []
    for r in fuel_rows[-5:][::-1]:
        recent.append({
            "type": "fuel",
            "date": r["date"],
            "label": r["station"] or ("Charging session" if is_electric else "Fill-up"),
            "detail": f"{quantity_from_minor(r['quantity_minor'], vehicle['fuel_unit'])} {FUEL_UNIT_LABELS[vehicle['fuel_unit']]}",
            "cost": r["cost_cents"] / 100,
        })
    for r in svc_rows[-5:][::-1]:
        recent.append({
            "type": "service",
            "date": r["date"],
            "label": r["description"],
            "detail": r["category"],
            "cost": (r["cost_cents"] or 0) / 100,
        })
    recent.sort(key=lambda x: x["date"], reverse=True)
    recent = recent[:6]

    vrow = row_vehicle(vehicle)
    latest_odo = vrow["latest_odometer"]
    reminders = [
        row_reminder(r, latest_odo)
        for r in db.query(
            "SELECT * FROM reminders WHERE vehicle_id=? AND done=0"
            " ORDER BY due_date IS NULL, due_date, due_odometer_tenths IS NULL",
            (vid,),
        )
    ]

    avg = stats.avg_economy(vehicle["fuel_unit"])
    cpd = stats.cost_per_distance()
    return {
        "vehicle": vrow,
        "latest_odometer": latest_odo,
        "economy_label": economy_label(vehicle["distance_unit"], vehicle["fuel_unit"]),
        "avg_economy": float(avg.quantize(Decimal("0.01"))) if avg is not None else None,
        "distance_logged": float(stats.total_distance),
        "fuel_cost_per_distance": float(cpd.quantize(Decimal("0.001"))) if cpd is not None else None,
        "spend": {"fuel": fuel_spend / 100, "service": svc_spend / 100, "total": (fuel_spend + svc_spend) / 100},
        "monthly": months,
        "recent": recent,
        "reminders": reminders,
    }


# ---------------------------------------------------------------------------
# CSV export
# ---------------------------------------------------------------------------
# Each export returns a StreamingResponse with a Content-Disposition header so
# the browser saves under a vehicle-specific filename. The `quantity_<unit>`
# column header carries the vehicle's fuel unit (gal/L/imp gal/kWh) so a
# spreadsheet user can see at a glance which unit the values are in.

def _csv_response(rows: list[dict], filename: str) -> StreamingResponse:
    """Build a CSV download response from a list of dict rows. The first row's
    keys become the header line, so callers control column order by inserting
    rows in that order. Empty `rows` produces an empty CSV (just the header
    is written by DictWriter, but only if there are rows — so empty exports
    are truly empty, no header-only file)."""
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/vehicles/{vid}/export/fuel.csv")
def export_fuel(vid: int):
    vehicle = get_vehicle_raw(vid)
    label = FUEL_UNIT_LABELS[vehicle["fuel_unit"]]
    rows = [
        {
            "date": r["date"],
            "odometer": r["odometer_tenths"] / 10,
            f"quantity_{label}": float(quantity_from_minor(r["quantity_minor"], vehicle["fuel_unit"])),
            "cost": r["cost_cents"] / 100,
            "full_tank": "yes" if r["full_tank"] else "no",
            "station": r["station"] or "",
            "notes": r["notes"] or "",
        }
        for r in fuel_entries_for(vid)
    ]
    return _csv_response(rows, f"{vehicle['name'].replace(' ', '_').lower()}_fuel.csv")


@app.get("/api/vehicles/{vid}/export/service.csv")
def export_service(vid: int):
    vehicle = get_vehicle_raw(vid)
    rows = [
        {
            "date": r["date"],
            "odometer": (r["odometer_tenths"] / 10) if r["odometer_tenths"] is not None else "",
            "description": r["description"],
            "category": r["category"],
            "cost": (r["cost_cents"] / 100) if r["cost_cents"] is not None else "",
            "notes": r["notes"] or "",
        }
        for r in db.query(
            "SELECT * FROM service_entries WHERE vehicle_id=? ORDER BY date, id", (vid,)
        )
    ]
    return _csv_response(rows, f"{vehicle['name'].replace(' ', '_').lower()}_service.csv")


# ---------------------------------------------------------------------------
# PDF export (print-friendly, Glovebox-branded) and combined "all data" export
# ---------------------------------------------------------------------------
# PDFs are generated by app/pdf_export.py from plain dict payloads built here.
# The brand icon (accent + theme-specific PNG) is read from static/icons and
# passed as bytes; pdf_export falls back to a vector mark if it's None. The
# `theme` query param controls which icon (light/dark) is embedded so a print
# matches what the user sees on screen.

def _pdf_response(data: bytes, filename: str) -> Response:
    """Wrap PDF bytes as a download response with a vehicle-specific filename."""
    return Response(
        content=data,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _report_icon(accent: str, theme: str) -> bytes | None:
    """Brand icon for the report header, resolved exactly like the app header does
    (static/js/app.js currentIconFolder): icons/<accent>-<theme>/icon-192-rounded.png.
    `accent` arrives pre-validated from get_accent() and `theme` is regex-checked by
    the route, so the folder name is always safe. Returns None (and the renderer
    falls back to its built-in vector mark) if the asset is missing."""
    try:
        return (STATIC_DIR / "icons" / f"{accent}-{theme}" / "icon-192-rounded.png").read_bytes()
    except OSError:
        return None


def _service_rows_desc(vid: int):
    return db.query(
        "SELECT * FROM service_entries WHERE vehicle_id=? ORDER BY date DESC, id DESC", (vid,)
    )


@app.get("/api/vehicles/{vid}/export/fuel.pdf")
def export_fuel_pdf(vid: int, theme: str = Query("light", pattern="^(light|dark)$")):
    accent = get_accent()
    payload = fuel_payload(vid)
    raw = get_vehicle_raw(vid)
    summary = {**payload["summary"], "fuel_unit_label": FUEL_UNIT_LABELS[raw["fuel_unit"]]}
    data = fuel_pdf(payload["vehicle"], payload["entries"], summary, accent=accent,
                    icon=_report_icon(accent, theme))
    return _pdf_response(data, f"{raw['name'].replace(' ', '_').lower()}_fuel.pdf")


@app.get("/api/vehicles/{vid}/export/service.pdf")
def export_service_pdf(vid: int, theme: str = Query("light", pattern="^(light|dark)$")):
    accent = get_accent()
    raw = get_vehicle_raw(vid)
    rows = [row_service(r, raw) for r in _service_rows_desc(vid)]
    data = service_pdf(row_vehicle(raw), rows, accent=accent, icon=_report_icon(accent, theme))
    return _pdf_response(data, f"{raw['name'].replace(' ', '_').lower()}_service.pdf")


@app.get("/api/vehicles/{vid}/export/all.csv")
def export_all_csv(vid: int):
    """Combine service + fuel/charge records into one CSV, with a `type` column
    (\"service\" or \"fuel\") to tell them apart. Service rows are listed first
    (oldest-first, matching the per-table CSVs), then fuel rows.

    A `blank` template row is spread into each row so service rows leave the
    fuel-only columns empty (and vice versa) — DictWriter writes a consistent
    header with every column present, even when a given row type doesn't use it.
    """
    vehicle = get_vehicle_raw(vid)
    unit = vehicle["fuel_unit"]
    qty_col = f"quantity_{FUEL_UNIT_LABELS[unit]}"
    blank = {
        "type": "", "date": "", "odometer": "", "description": "", "category": "",
        qty_col: "", "cost": "", "full_tank": "", "station": "", "notes": "",
    }
    service_rows = [
        {
            **blank,
            "type": "service",
            "date": r["date"],
            "odometer": (r["odometer_tenths"] / 10) if r["odometer_tenths"] is not None else "",
            "description": r["description"],
            "category": r["category"],
            "cost": (r["cost_cents"] / 100) if r["cost_cents"] is not None else "",
            "notes": r["notes"] or "",
        }
        for r in db.query("SELECT * FROM service_entries WHERE vehicle_id=? ORDER BY date, id", (vid,))
    ]
    fuel_rows = [
        {
            **blank,
            "type": "fuel",
            "date": r["date"],
            "odometer": r["odometer_tenths"] / 10,
            qty_col: float(quantity_from_minor(r["quantity_minor"], unit)),
            "cost": r["cost_cents"] / 100,
            "full_tank": "yes" if r["full_tank"] else "no",
            "station": r["station"] or "",
            "notes": r["notes"] or "",
        }
        for r in fuel_entries_for(vid)
    ]
    return _csv_response(service_rows + fuel_rows, f"{vehicle['name'].replace(' ', '_').lower()}_all_data.csv")


@app.get("/api/vehicles/{vid}/export/all.pdf")
def export_all_pdf(vid: int, theme: str = Query("light", pattern="^(light|dark)$")):
    accent = get_accent()
    payload = fuel_payload(vid)
    raw = get_vehicle_raw(vid)
    summary = {**payload["summary"], "fuel_unit_label": FUEL_UNIT_LABELS[raw["fuel_unit"]]}
    svc = [row_service(r, raw) for r in _service_rows_desc(vid)]
    data = all_data_pdf(payload["vehicle"], payload["entries"], summary, svc, accent=accent,
                        icon=_report_icon(accent, theme))
    return _pdf_response(data, f"{raw['name'].replace(' ', '_').lower()}_all_data.pdf")


# ---------------------------------------------------------------------------
# QR code (Quick-log)
# ---------------------------------------------------------------------------

# Lazy import: qrcode is only used here, so importing it lazily keeps the
# module importable even if the optional dependency is missing on a dev box.
def _make_qr_png(url: str, *, box_size: int = 8, border: int = 2) -> bytes:
    import qrcode  # local import keeps the rest of the module light
    qr = qrcode.QRCode(
        version=None,  # auto-pick the smallest version that fits
        error_correction=qrcode.constants.ERROR_CORRECT_M,  # ~15% — robust to prints
        box_size=box_size,
        border=border,  # quiet zone, in modules
    )
    qr.add_data(url)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _same_origin(request: Request, url: str) -> bool:
    """True if `url` is on the same origin as the request the client made.

    Used to gate the QR-code endpoint so an attacker can't trick a user's
    browser into rendering a QR for an arbitrary external URL (which would
    turn the instance into a QR-printing service for phishing). Same scheme,
    host, and port — path/query/fragment are unconstrained.
    """
    try:
        target = urlsplit(url)
    except ValueError:
        return False
    if not target.scheme or not target.netloc:
        return False
    # request.url is the URL FastAPI saw — for a reverse-proxy setup that
    # passes X-Forwarded-Proto/Host correctly, this matches what the browser
    # sees. If the proxy strips those headers, the user can still QR the
    # browser-seen URL by passing it explicitly via the `url` query param,
    # which is checked against the same forwarded scheme/host.
    return (target.scheme, target.netloc) == (request.url.scheme, request.url.netloc)


@app.get("/api/qr.png")
def qr_code(request: Request, url: str = Query(..., min_length=1, max_length=2048)):
    """Return a PNG QR code encoding `url`.

    `url` must be on the same origin as this request, so the QR always points
    back at this Glovebox instance (and can't be abused to encode phishing
    links). The frontend uses this to render a "Quick-log" QR in Settings that
    deep-links to the Fuel page with the Add fill-up modal open.
    """
    if not _same_origin(request, url):
        raise HTTPException(422, "URL must be on the same origin as this Glovebox instance.")
    png = _make_qr_png(url)
    return Response(
        content=png,
        media_type="image/png",
        # The QR for a given URL is stable; cache to avoid regenerating on
        # every Settings page view. The URL includes the deep-link query, so
        # a vehicle/type change produces a different cache key naturally.
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": 'inline; filename="glovebox-quick-log.png"',
        },
    )


# ---------------------------------------------------------------------------
# Attachments (per-entry file uploads: receipts, photos, PDFs, …)
# ---------------------------------------------------------------------------
# Files are stored on disk under <data>/attachments/ (ATTACH_DIR, a sibling of
# the SQLite DB). In Docker that shares the persistent /data volume, so uploads
# survive restarts and container rebuilds; locally they land in ./data/attachments/.
#
# The DB row holds the original filename (for display), the on-disk stored_name
# (a uuid + original extension so same-name uploads don't collide and the user's
# filename never touches the filesystem), the mime type and the byte size.
#
# entry_type ('fuel' | 'service') + entry_id form a polymorphic reference to
# either fuel_entries or service_entries (no FK — one column can't reference two
# tables). Cascade cleanup is handled explicitly in the fuel/service/vehicle
# delete routes above (delete_attachments_for / delete_attachments_for_vehicle),
# which unlink the file alongside the row.

ALLOWED_MIME = {
    "image/png", "image/jpeg", "image/gif", "image/webp",
    "application/pdf", "text/plain", "text/csv",
}
ALLOWED_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".txt", ".csv"}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB per file — keeps uploads quick and
# the on-disk footprint predictable. Bump here if you need larger receipts/scans.


def _attachments_dir() -> Path:
    """Return the attachments directory, creating it on first use. Lazy creation
    means a fresh install (or a volume that was emptied) doesn't need a startup
    hook — the first upload makes the directory."""
    ATTACH_DIR.mkdir(parents=True, exist_ok=True)
    return ATTACH_DIR


def row_attachment(r) -> dict:
    """Serialize one attachment row. The URLs are relative paths (no host) so
    the frontend can prepend its API_BASE and honour the same proxy/origin
    setup as every other request. `disposition=inline` opens in the browser
    (image/PDF preview); `disposition=attachment` forces a download."""
    mime = r["mime_type"] or "application/octet-stream"
    aid = r["id"]
    return {
        "id": aid,
        "filename": r["filename"],
        "mime_type": mime,
        "size_bytes": r["size_bytes"],
        "is_image": mime.startswith("image/"),
        "url": f"/api/attachments/{aid}?disposition=inline",
        "download_url": f"/api/attachments/{aid}?disposition=attachment",
        "created_at": r["created_at"],
    }


def attachment_counts_for(vehicle_id: int, entry_type: str) -> dict[int, int]:
    """Return {entry_id: attachment_count} for one vehicle + entry type, in a
    single query. Used by fuel_payload() and list_service() so the table views
    can show a per-row paperclip badge without an N+1 follow-up per entry."""
    rows = db.query(
        "SELECT entry_id, COUNT(*) AS c FROM attachments"
        " WHERE vehicle_id=? AND entry_type=? GROUP BY entry_id",
        (vehicle_id, entry_type),
    )
    return {r["entry_id"]: r["c"] for r in rows}


def _purge_attachment_files(rows) -> None:
    """Unlink the on-disk files for a list of attachment rows. Missing files are
    silently skipped (an orphaned row pointing at a deleted file is the worse
    state, but we still remove the row elsewhere — this just avoids crashing)."""
    d = _attachments_dir()
    for r in rows:
        try:
            (d / r["stored_name"]).unlink()
        except OSError:
            pass


def delete_attachments_for(entry_type: str, entry_id: int) -> None:
    """Remove every attachment (file + row) for one fuel/service entry. Called
    by the fuel/service delete routes so deleting an entry doesn't orphan its
    files on disk."""
    rows = db.query(
        "SELECT * FROM attachments WHERE entry_type=? AND entry_id=?",
        (entry_type, entry_id),
    )
    _purge_attachment_files(rows)
    db.execute(
        "DELETE FROM attachments WHERE entry_type=? AND entry_id=?",
        (entry_type, entry_id),
    )


def delete_attachments_for_vehicle(vehicle_id: int) -> None:
    """Remove every attachment (file + row) for a whole vehicle. Called by the
    vehicle delete route before the ON DELETE CASCADE wipes the rows, because
    the cascade can't unlink files on disk."""
    rows = db.query("SELECT * FROM attachments WHERE vehicle_id=?", (vehicle_id,))
    _purge_attachment_files(rows)
    db.execute("DELETE FROM attachments WHERE vehicle_id=?", (vehicle_id,))


def _validate_upload(file: UploadFile) -> tuple[str, str]:
    """Return (mime_type, extension) for an upload, rejecting anything that
    isn't on the allowlist. We check BOTH the extension and the mime type: the
    extension is the source of truth for the on-disk filename, and the mime
    type is what we serve back, so both must be allowed. When the browser sends
    a generic application/octet-stream (common for .pdf / .csv), we fall back to
    mimetypes.guess_type() on the filename and accept that if it's allowed."""
    original = file.filename or ""
    ext = Path(original).suffix.lower()
    mime = (file.content_type or "").split(";")[0].strip().lower()
    if mime not in ALLOWED_MIME:
        guess, _ = mimetypes.guess_type(original)
        mime = (guess or "").lower()
    if ext not in ALLOWED_EXT or mime not in ALLOWED_MIME:
        raise HTTPException(
            415, "Unsupported file type. Use an image, PDF, TXT or CSV."
        )
    return mime, ext


@app.get("/api/attachments")
def list_attachments(
    entry_type: str = Query(..., pattern="^(fuel|service)$"),
    entry_id: int = Query(...),
):
    """List attachments for one fuel/service entry. Returns metadata + URLs; the
    file bytes themselves are served by GET /api/attachments/{aid}."""
    rows = db.query(
        "SELECT * FROM attachments WHERE entry_type=? AND entry_id=? ORDER BY id",
        (entry_type, entry_id),
    )
    return [row_attachment(r) for r in rows]


@app.post("/api/attachments", status_code=201)
async def create_attachment(
    file: UploadFile = File(...),
    entry_type: str = Form(..., pattern="^(fuel|service)$"),
    entry_id: int = Form(...),
    vehicle_id: int = Form(...),
):
    """Upload one file and attach it to a fuel or service entry.

    The entry must already exist (the frontend creates the entry first, then
    uploads attachments), so we validate the (entry_type, entry_id, vehicle_id)
    triple belongs to a real entry of the right type — that prevents attaching
    a file to a phantom id, and ensures the attachment is bound to the correct
    vehicle.
    """
    # Verify the owning entry exists and belongs to the named vehicle.
    if entry_type == "fuel":
        row = db.one("SELECT vehicle_id FROM fuel_entries WHERE id=?", (entry_id,))
    else:
        row = db.one("SELECT vehicle_id FROM service_entries WHERE id=?", (entry_id,))
    if row is None:
        raise HTTPException(404, "entry not found")
    if row["vehicle_id"] != vehicle_id:
        raise HTTPException(422, "entry does not belong to this vehicle")

    mime, ext = _validate_upload(file)

    # Read into memory to enforce the size cap before writing to disk. 25 MB is
    # small enough that buffering is fine and keeps the code simple; we avoid
    # streaming-to-disk-then-truncating, which would leave a partial file behind
    # on an oversize upload.
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File too large. Maximum 25 MB.")

    stored_name = f"{uuid.uuid4().hex}{ext}"
    (_attachments_dir() / stored_name).write_bytes(data)

    aid = db.execute(
        "INSERT INTO attachments (vehicle_id, entry_type, entry_id, filename, stored_name, mime_type, size_bytes)"
        " VALUES (?,?,?,?,?,?,?)",
        (vehicle_id, entry_type, entry_id, file.filename or stored_name, stored_name, mime, len(data)),
    )
    return row_attachment(db.one("SELECT * FROM attachments WHERE id=?", (aid,)))


@app.get("/api/attachments/{aid}")
def get_attachment(
    aid: int,
    disposition: str = Query("inline", pattern="^(inline|attachment)$"),
):
    """Serve an attachment's bytes. `disposition=inline` opens in the browser
    (image/PDF preview in a new tab); `disposition=attachment` forces a
    download with the original filename. The filename is RFC 5987-encoded so
    non-ASCII names survive the Content-Disposition header intact."""
    r = db.one("SELECT * FROM attachments WHERE id=?", (aid,))
    if r is None:
        raise HTTPException(404, "attachment not found")
    path = _attachments_dir() / r["stored_name"]
    if not path.is_file():
        raise HTTPException(404, "attachment file missing")
    media = r["mime_type"] or "application/octet-stream"
    fname = r["filename"] or r["stored_name"]
    # RFC 5987: filename*=UTF-8''<percent-encoded> is understood by every modern
    # browser and handles unicode safely. We also send an ASCII filename= as a
    # fallback for very old clients.
    ascii_fallback = fname.encode("ascii", "ignore").decode("ascii") or "attachment"
    content_disposition = (
        f"{disposition}; filename=\"{ascii_fallback.replace(chr(34), '')}\"; "
        f"filename*=UTF-8''{quote(fname)}"
    )
    return FileResponse(
        str(path),
        media_type=media,
        headers={"Content-Disposition": content_disposition},
    )


@app.delete("/api/attachments/{aid}")
def delete_attachment(aid: int):
    """Remove one attachment: unlink its file on disk and delete the row. Used
    by the per-attachment trash button in the edit modal."""
    r = db.one("SELECT * FROM attachments WHERE id=?", (aid,))
    if r is None:
        raise HTTPException(404, "attachment not found")
    try:
        (_attachments_dir() / r["stored_name"]).unlink()
    except OSError:
        pass
    db.execute("DELETE FROM attachments WHERE id=?", (aid,))
    return {"deleted": aid}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
