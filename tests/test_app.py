"""Tests for fuel-economy interval math and the API layer.

The suite is organized into four sections, each with its own helpers:
  1. Core API + economy math (the original tests)
  2. EV support — fuel_type field, mi/kWh efficiency, charging sessions
  3. Quick-log QR code + deep-link (?add-fuel=1) endpoint tests
  4. Vehicle-specific QR code (?add-fuel=1&vehicle=N) deep-link tests

All tests share a single TestClient pointed at a temp DB (set at import
below). State persists across tests within a section, so test order matters
within a section — but sections are independent. The browser-level deep-link
behavior (route() parsing the URL hash) is verified separately by the
scripts/smoke_test_*.py files, not here, since it requires a JS runtime.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# All tests share one DB so we don't pay the migration cost per test.
# `tempfile.mkdtemp()` gives a fresh directory per test session — pytest
# doesn't clean it up, but it's in /tmp so the OS will.
os.environ["GLOVEBOX_DB"] = tempfile.mkdtemp() + "/test.db"

from fastapi.testclient import TestClient

from app.main import app

c = TestClient(app)


# Helpers used by the core and EV sections. EV-specific helpers (_mk_ev,
# _charge) are defined further down, near their tests.
def _mk_vehicle(**kw) -> int:
    """Create a gas vehicle with sensible defaults. Returns its id."""
    body = {"name": "Test Car", **kw}
    r = c.post("/api/vehicles", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _fuel(vid, date, odo, qty, cost, full=True):
    """Log a fill-up. Asserts 201 so callers don't have to. Returns the entry id."""
    r = c.post(
        f"/api/vehicles/{vid}/fuel",
        json={"date": date, "odometer": odo, "quantity": qty, "cost": cost, "full_tank": full},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _summary(vid):
    """Fetch the dashboard summary and assert 200. Returns the parsed JSON."""
    r = c.get(f"/api/vehicles/{vid}/summary")
    assert r.status_code == 200, r.text
    return r.json()


def test_health():
    assert c.get("/api/health").json() == {"status": "ok"}


def test_first_fill_has_no_economy():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 15, 45)
    s = _summary(vid)
    assert s["avg_economy"] is None


def test_partial_fill_math():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 15, 45)
    _fuel(vid, "2026-02-01", 10400, 5, 15, full=False)
    _fuel(vid, "2026-03-01", 10800, 5, 15)
    s = _summary(vid)
    # 800 miles on 5+5=10 gallons between full tanks
    assert s["avg_economy"] == 80.0
    assert s["distance_logged"] == 800.0


def test_two_intervals_weighted_average():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 10, 30)   # baseline
    _fuel(vid, "2026-02-01", 10500, 10, 30)   # 500 mi / 10 gal = 50
    _fuel(vid, "2026-03-01", 11400, 12, 36)   # 900 mi / 12 gal = 75
    s = _summary(vid)
    # weighted: 1400 mi / 22 gal = 63.63..., not mean(50, 75)
    assert abs(s["avg_economy"] - round(1400 / 22, 2)) < 0.01


def test_backdated_entry_recomputes():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 10, 30)
    _fuel(vid, "2026-03-01", 11000, 10, 30)
    s = _summary(vid)
    assert s["avg_economy"] == 100.0
    # Backdate a partial fill between the two fulls
    _fuel(vid, "2026-02-01", 10500, 10, 30, full=False)
    s = _summary(vid)
    assert s["avg_economy"] == 50.0


def test_delete_entry_recomputes():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 10, 30)
    fid = _fuel(vid, "2026-02-01", 10500, 10, 30)
    _fuel(vid, "2026-03-01", 11000, 5, 15)
    s = _summary(vid)
    assert s["avg_economy"] == round(1000 / 15, 2)
    c.delete(f"/api/fuel/{fid}")
    s = _summary(vid)
    # Now 10000 -> 11000 with the remaining 5 gal = 200 mpg
    assert s["avg_economy"] == 200.0


def test_odometer_regression_skips_interval():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 10, 30)
    # Odometer went backwards — no valid interval, but anchor moves forward
    _fuel(vid, "2026-02-01", 9500, 10, 30)
    s = _summary(vid)
    assert s["avg_economy"] is None


def test_vehicle_crud():
    vid = _mk_vehicle(name="Truck", distance_unit="km", fuel_unit="l")
    r = c.get(f"/api/vehicles/{vid}").json()
    assert r["distance_unit"] == "km"
    r = c.patch(f"/api/vehicles/{vid}", json={"name": "Big Truck"})
    assert r.json()["name"] == "Big Truck"
    assert c.delete(f"/api/vehicles/{vid}").status_code == 200


def test_service_and_reminders():
    vid = _mk_vehicle()
    r = c.post(
        f"/api/vehicles/{vid}/service",
        json={"date": "2026-02-15", "odometer": 10500, "description": "Oil change", "cost": 75.25},
    )
    assert r.status_code == 201
    rows = c.get(f"/api/vehicles/{vid}/service").json()
    assert rows[0]["cost"] == 75.25
    assert rows[0]["description"] == "Oil change"

    r = c.post(
        f"/api/vehicles/{vid}/reminders",
        json={"title": "Rotate tires", "due_date": "2026-01-01"},
    )
    rid = r.json()["id"]
    rems = c.get(f"/api/vehicles/{vid}/reminders").json()
    assert rems[0]["status"] == "overdue"

    # one-time reminder completes
    res = c.post(f"/api/reminders/{rid}/complete").json()
    assert res["done"] is True
    assert c.get(f"/api/vehicles/{vid}/reminders").json() == []

    # recurring reminder advances instead of completing
    r = c.post(
        f"/api/vehicles/{vid}/reminders",
        json={"title": "Oil change", "due_date": "2026-01-01", "recur_months": 6},
    )
    res = c.post(f"/api/reminders/{r.json()['id']}/complete").json()
    assert res["recurring"] is True
    assert c.get(f"/api/vehicles/{vid}/reminders").json()[0]["status"] == "upcoming"


def test_csv_export():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 12.5, 40)
    r = c.get(f"/api/vehicles/{vid}/export/fuel.csv")
    assert r.status_code == 200
    assert "date,odometer,quantity_gal" in r.text
    r = c.get(f"/api/vehicles/{vid}/export/service.csv")
    assert r.status_code == 200


def test_validation_errors():
    vid = _mk_vehicle()
    assert c.post(f"/api/vehicles/{vid}/fuel", json={"date": "2026-01-01", "odometer": 0, "quantity": 5, "cost": 5}).status_code == 422
    assert c.post(f"/api/vehicles/{vid}/fuel", json={"date": "01-01-2026", "odometer": 100, "quantity": 5, "cost": 5}).status_code == 422
    assert c.post(f"/api/vehicles/{vid}/fuel", json={"date": "2026-02-31", "odometer": 100, "quantity": 5, "cost": 5}).status_code == 422
    assert c.get("/api/vehicles/99999").status_code == 404


def test_name_only_vehicle():
    """Onboarding with just a name (blank year/make/model) must succeed."""
    r = c.post("/api/vehicles", json={"name": "  Minimal  "})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Minimal"
    assert r.json()["year"] is None


def test_patch_clears_optional_values():
    """Explicit null in a PATCH clears the stored value; omitted fields stay."""
    vid = _mk_vehicle()
    sid = c.post(f"/api/vehicles/{vid}/service", json={
        "date": "2026-01-15", "odometer": 5000, "description": "Brake check",
        "cost": 120.00, "notes": "front pads"}).json()["id"]
    # clear cost and notes, keep odometer
    r = c.patch(f"/api/service/{sid}", json={"cost": None, "notes": None})
    assert r.status_code == 200
    row = c.get(f"/api/vehicles/{vid}/service").json()[0]
    assert row["cost"] is None
    assert row["notes"] is None
    assert row["odometer"] == 5000
    # clearing odometer too
    c.patch(f"/api/service/{sid}", json={"odometer": None})
    row = c.get(f"/api/vehicles/{vid}/service").json()[0]
    assert row["odometer"] is None


def test_patch_validation():
    vid = _mk_vehicle()
    sid = c.post(f"/api/vehicles/{vid}/service", json={"date": "2026-01-15", "description": "x"}).json()["id"]
    assert c.patch(f"/api/service/{sid}", json={"cost": -5}).status_code == 422
    assert c.patch(f"/api/service/{sid}", json={"category": "bogus"}).status_code == 422
    assert c.patch(f"/api/service/{sid}", json={"date": "2026-02-30"}).status_code == 422


def test_recurring_complete_clears_stale_date():
    """A distance-only recurring reminder must not keep its expired date."""
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 10, 30)
    rid = c.post(f"/api/vehicles/{vid}/reminders", json={
        "title": "Rotate", "due_date": "2020-01-01", "recur_distance": 5000}).json()["id"]
    assert c.get(f"/api/vehicles/{vid}/reminders").json()[0]["status"] == "overdue"
    res = c.post(f"/api/reminders/{rid}/complete").json()
    assert res["recurring"] is True
    rem = c.get(f"/api/vehicles/{vid}/reminders").json()[0]
    assert rem["due_date"] is None          # stale date cleared
    assert rem["due_odometer"] == 15000     # advanced from latest odo
    assert rem["status"] == "upcoming"


def test_month_clamp():
    from app.main import reminder_months_ahead
    from datetime import date
    assert reminder_months_ahead(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert reminder_months_ahead(date(2026, 1, 31), 13) == date(2027, 2, 28)
    assert reminder_months_ahead(date(2026, 2, 28), 1) == date(2026, 3, 28)
    assert reminder_months_ahead(date(2024, 1, 31), 1) == date(2024, 2, 29)  # leap year


def test_accent_default_and_update():
    r = c.get("/api/settings")
    assert r.status_code == 200
    assert r.json()["accent"] == "blue"
    r = c.put("/api/settings", json={"accent": "violet"})
    assert r.status_code == 200 and r.json()["accent"] == "violet"
    assert c.get("/api/settings").json()["accent"] == "violet"
    assert c.put("/api/settings", json={"accent": "hotpink"}).status_code == 422
    assert c.get("/api/settings").json()["accent"] == "violet"
    c.put("/api/settings", json={"accent": "blue"})


def test_pwa_assets_served():
    r = c.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "manifest+json" in r.headers["content-type"]
    manifest = r.json()
    assert manifest["display"] == "standalone"
    for icon in manifest["icons"]:
        assert c.get("/" + icon["src"]).status_code == 200, icon["src"]
    # The default apple-touch-icon path referenced by index.html.
    assert c.get("/icons/blue-dark/apple-touch-icon.png").status_code == 200
    r = c.get("/sw.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_pdf_exports():
    vid = _mk_vehicle(name="PDF Car", currency="$")
    for i in range(90):
        _fuel(vid, f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", 10000 + i * 300, 12.5, 40)
    c.post(
        f"/api/vehicles/{vid}/service",
        json={"date": "2026-02-15", "odometer": 10500, "description": "Oil <change> & filter", "cost": 75.25, "notes": "line1\nline2"},
    )
    r = c.get(f"/api/vehicles/{vid}/export/fuel.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert "pdf_car_fuel.pdf" in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF")
    assert r.content.count(b"/Type /Page\n") + r.content.count(b"/Type /Page ") > 1
    r = c.get(f"/api/vehicles/{vid}/export/service.pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert c.get(f"/api/vehicles/{vid}/export/all.pdf").content.startswith(b"%PDF")
    assert c.get(f"/api/vehicles/{vid}/export/all.csv").text.startswith("type,date")
    empty = _mk_vehicle(name="Empty")
    assert c.get(f"/api/vehicles/{empty}/export/fuel.pdf").content.startswith(b"%PDF")
    assert c.get(f"/api/vehicles/{empty}/export/service.pdf").content.startswith(b"%PDF")
    assert c.get("/api/vehicles/99999/export/fuel.pdf").status_code == 404
    assert c.get("/api/vehicles/99999/export/service.pdf").status_code == 404


def test_csv_unchanged_by_pdf():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 12.5, 40)
    r = c.get(f"/api/vehicles/{vid}/export/fuel.csv")
    assert r.text.splitlines()[0] == "date,odometer,quantity_gal,cost,full_tank,station,notes"
    assert r.headers["content-type"].startswith("text/csv")


def test_pdf_export_uses_theme_icon():
    """PDF reports embed the same brand icon the app header uses (accent + theme)."""
    from app.main import _report_icon
    dark, light = _report_icon("blue", "dark"), _report_icon("blue", "light")
    assert dark and light and dark != light  # real, theme-specific icon assets
    vid = _mk_vehicle(name="Icon Car")
    r = c.get(f"/api/vehicles/{vid}/export/fuel.pdf?theme=dark")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    assert b"/Subtype /Image" in r.content  # icon asset embedded
    assert b"/SMask" in r.content  # rounded corners kept via the PNG's alpha
    # light theme (and the no-param default) embed the icon too, on every report type
    assert b"/Subtype /Image" in c.get(f"/api/vehicles/{vid}/export/all.pdf?theme=light").content
    assert b"/Subtype /Image" in c.get(f"/api/vehicles/{vid}/export/service.pdf").content
    # invalid theme value is rejected
    assert c.get(f"/api/vehicles/{vid}/export/fuel.pdf?theme=neon").status_code == 422


def _entries(vid):
    """Return a {odometer: entry} map for the fuel page — used by tests that
    need to look up an entry by its odometer reading."""
    return {e["odometer"]: e for e in c.get(f"/api/vehicles/{vid}/fuel").json()["entries"]}


def _run(vid, odometers):
    """Log a series of full fill-ups at the given odometer readings, one per
    month from 2026-01. Used by the suspect-interval tests to build a baseline."""
    for i, odo in enumerate(odometers):
        _fuel(vid, f"2026-{i + 1:02d}-01", odo, 10, 30)


def test_missed_fillup_restarts_tracking():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 10, 30)
    _fuel(vid, "2026-02-01", 10500, 10, 30)
    r = c.post(
        f"/api/vehicles/{vid}/fuel",
        json={"date": "2026-03-01", "odometer": 11500, "quantity": 10, "cost": 30, "missed_before": True},
    )
    assert r.status_code == 201, r.text
    _fuel(vid, "2026-04-01", 12000, 10, 30)
    by_odo = _entries(vid)
    assert by_odo[11500]["missed_before"] is True
    assert by_odo[11500].get("economy") is None
    assert by_odo[12000]["economy"] == 50.0
    assert _summary(vid)["avg_economy"] == 50.0


def test_missed_flag_can_be_edited():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 10, 30)
    fid = _fuel(vid, "2026-02-01", 11000, 10, 30)
    assert _summary(vid)["avg_economy"] == 100.0
    assert c.patch(f"/api/fuel/{fid}", json={"missed_before": True}).status_code == 200
    assert _summary(vid)["avg_economy"] is None
    assert c.patch(f"/api/fuel/{fid}", json={"missed_before": False}).status_code == 200
    assert _summary(vid)["avg_economy"] == 100.0


def test_suspect_economy_is_flagged():
    vid = _mk_vehicle()
    _run(vid, [10000 + 300 * i for i in range(7)] + [12800])
    entries = c.get(f"/api/vehicles/{vid}/fuel").json()["entries"]
    assert [e["odometer"] for e in entries if e["suspect"]] == [12800]


def test_one_outlier_does_not_flag_its_neighbours():
    vid = _mk_vehicle()
    _run(vid, [10000, 10300, 10600, 10900, 11900])
    entries = c.get(f"/api/vehicles/{vid}/fuel").json()["entries"]
    assert [e["odometer"] for e in entries if e["suspect"]] == [11900]


def test_normal_variation_is_not_flagged():
    vid = _mk_vehicle()
    _run(vid, [10000, 10250, 10600, 10950, 11350, 11700])
    assert not any(e["suspect"] for e in c.get(f"/api/vehicles/{vid}/fuel").json()["entries"])


def test_no_suspect_flags_with_few_intervals():
    vid = _mk_vehicle()
    _run(vid, [10000, 10300, 10600, 11600])
    assert not any(e["suspect"] for e in c.get(f"/api/vehicles/{vid}/fuel").json()["entries"])


def test_summary_has_no_economy_trend():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 10, 30)
    _fuel(vid, "2026-02-01", 10300, 10, 30)
    assert "economy_trend" not in _summary(vid)


def test_summary_month_range():
    vid = _mk_vehicle()
    _fuel(vid, "2026-01-01", 10000, 10, 30)
    assert len(_summary(vid)["monthly"]) == 12
    r = c.get(f"/api/vehicles/{vid}/summary?months=24")
    assert r.status_code == 200
    monthly = r.json()["monthly"]
    assert len(monthly) == 24
    assert monthly[0]["ym"] < monthly[11]["ym"] < monthly[-1]["ym"]
    assert len({mo["ym"] for mo in monthly}) == 24
    assert c.get(f"/api/vehicles/{vid}/summary?months=0").status_code == 422
    assert c.get(f"/api/vehicles/{vid}/summary?months=999").status_code == 422


def test_version_exposed():
    import re
    assert re.fullmatch(r"\d+\.\d+\.\d+", c.get("/api/settings").json()["version"])


def test_summary_monthly_distance():
    """Monthly distance = highest odometer in the month minus the highest before it."""
    from datetime import date

    today = date.today()

    def month_start(k):
        """Day-01 date string k months before the current month."""
        y, m = today.year, today.month - k
        while m <= 0:
            m += 12
            y -= 1
        return f"{y:04d}-{m:02d}-01"

    vid = _mk_vehicle()
    _fuel(vid, month_start(2), 5000, 10, 30)             # only reading: no distance
    _fuel(vid, month_start(1), 5300, 10, 30)             # 5300 - 5000
    _fuel(vid, month_start(0), 5600, 10, 30)             # two readings this month:
    _fuel(vid, month_start(0).replace("-01", "-15"), 5900, 10, 30)  # 5900 - 5300
    s = _summary(vid)
    monthly = {mo["ym"]: mo for mo in s["monthly"]}
    assert monthly[month_start(2)[:7]]["distance"] == 0
    assert monthly[month_start(1)[:7]]["distance"] == 300.0
    assert monthly[month_start(0)[:7]]["distance"] == 600.0


# ---------------------------------------------------------------------------
# EV (electric vehicle) support — fuel_type field, mi/kWh efficiency,
# charging sessions, exports, dashboards. Helpers above (_mk_vehicle, _fuel,
# _summary, _entries, _run) are reused; EV variants are defined here.
# ---------------------------------------------------------------------------

def _mk_ev(**kw) -> int:
    body = {"name": "Test EV", "fuel_type": "electric", "fuel_unit": "kwh", **kw}
    r = c.post("/api/vehicles", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _charge(vid, date, odo, kwh, cost, full=True):
    """Add a charging session (analogous to _fuel)."""
    r = c.post(
        f"/api/vehicles/{vid}/fuel",
        json={"date": date, "odometer": odo, "quantity": kwh, "cost": cost, "full_tank": full},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_ev_vehicle_created_with_fuel_type():
    vid = _mk_ev(name="Leaf")
    r = c.get(f"/api/vehicles/{vid}").json()
    assert r["fuel_type"] == "electric"
    assert r["fuel_unit"] == "kwh"


def test_gas_vehicle_default_fuel_type():
    vid = _mk_vehicle(name="Truck")
    r = c.get(f"/api/vehicles/{vid}").json()
    assert r["fuel_type"] == "gas"
    assert r["fuel_unit"] == "gal"


def test_fuel_type_validation_rejects_unknown():
    r = c.post("/api/vehicles", json={"name": "X", "fuel_type": "diesel"})
    assert r.status_code == 422


def test_electric_vehicle_rejects_gas_fuel_unit():
    """kwh is the only valid fuel_unit for an electric vehicle."""
    r = c.post("/api/vehicles", json={"name": "EV", "fuel_type": "electric", "fuel_unit": "gal"})
    assert r.status_code == 422


def test_gas_vehicle_rejects_kwh_fuel_unit():
    r = c.post("/api/vehicles", json={"name": "Gas", "fuel_type": "gas", "fuel_unit": "kwh"})
    assert r.status_code == 422


def test_electric_vehicle_accepts_kwh():
    r = c.post("/api/vehicles", json={"name": "EV", "fuel_type": "electric", "fuel_unit": "kwh"})
    assert r.status_code == 201
    assert r.json()["fuel_unit"] == "kwh"


def test_ev_first_charge_has_no_economy():
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 30, 9)
    s = _summary(vid)
    assert s["avg_economy"] is None
    # economy_label uses kWh for EVs
    assert s["economy_label"] == "mi/kWh"


def test_ev_efficiency_basic():
    """Two full charges: 200 mi on 50 kWh = 4.0 mi/kWh."""
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 30, 9)
    _charge(vid, "2026-02-01", 10200, 50, 15)
    s = _summary(vid)
    assert s["avg_economy"] == 4.0
    assert s["distance_logged"] == 200.0
    assert s["economy_label"] == "mi/kWh"


def test_ev_partial_charge_math():
    """Partial charge between two fulls: 10000 -> 10800 with partial 10 + full 10
    = 20 kWh over 800 mi = 40 mi/kWh."""
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 30, 9)
    _charge(vid, "2026-02-01", 10400, 10, 3, full=False)
    _charge(vid, "2026-03-01", 10800, 10, 3)
    s = _summary(vid)
    assert s["avg_economy"] == 40.0
    assert s["distance_logged"] == 800.0


def test_ev_kwh_stored_as_watt_hours():
    """1 kWh = 1000 Wh. The stored integer is 30500 for 30.5 kWh.
    We verify by checking the rounded quantity the API returns."""
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 30.5, 9.15)
    payload = c.get(f"/api/vehicles/{vid}/fuel").json()
    e = payload["entries"][0]
    assert abs(e["quantity"] - 30.5) < 0.001


def test_ev_unit_price_is_per_kwh():
    """Cost / kWh (cost per energy unit), not per gallon."""
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 25, 7.50)  # $0.30/kWh
    e = c.get(f"/api/vehicles/{vid}/fuel").json()["entries"][0]
    assert abs(e["unit_price"] - 0.30) < 0.001


def test_ev_cost_per_distance():
    """$0.30/kWh x 25 kWh (closing charge only) over 200 mi = $0.0375/mi."""
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 25, 7.50)  # baseline
    _charge(vid, "2026-02-01", 10200, 25, 7.50)  # interval: 200 mi, 25 kWh = $7.50
    s = _summary(vid)
    # 7.50 / 200 = 0.0375 -> 0.038 when quantized to 3 decimals
    assert abs(s["fuel_cost_per_distance"] - 0.038) < 0.001


def test_ev_summary_uses_kwh_label():
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 30, 9)
    s = _summary(vid)
    assert s["economy_label"] == "mi/kWh"
    # 'spend' structure stays the same shape; fuel = energy spend
    assert "fuel" in s["spend"]
    assert s["spend"]["fuel"] == 9.0


def test_ev_summary_monthly_efficiency_label():
    """Dashboard's monthly efficiency should be in mi/kWh for EVs."""
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 30, 9)
    _charge(vid, "2026-02-01", 10200, 40, 12)
    s = _summary(vid)
    months_with_eff = [mo for mo in s["monthly"] if mo.get("efficiency") is not None]
    assert len(months_with_eff) >= 1
    # 200 mi / 40 kWh = 5.0 mi/kWh
    assert any(abs(mo["efficiency"] - 5.0) < 0.01 for mo in months_with_eff)


def test_ev_km_distance_uses_km_kwh():
    """Metric EV: km/kWh economy label."""
    vid = _mk_ev(distance_unit="km", name="Metric EV")
    _charge(vid, "2026-01-01", 10000, 30, 9)  # 1000.0 km
    _charge(vid, "2026-02-01", 10500, 50, 15)  # 500 km / 50 kWh = 10 km/kWh
    s = _summary(vid)
    assert s["economy_label"] == "km/kWh"
    assert s["avg_economy"] == 10.0


def test_ev_recent_activity_label_is_charge():
    """Recent activity defaults to 'Charging session' for EVs."""
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 30, 9)
    s = _summary(vid)
    fuel_activity = [a for a in s["recent"] if a["type"] == "fuel"]
    assert fuel_activity, "expected a fuel/charge entry in recent activity"
    assert fuel_activity[0]["label"] == "Charging session"
    assert "kWh" in fuel_activity[0]["detail"]


def test_ev_csv_export_has_kwh_column():
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 25.5, 7.65)
    r = c.get(f"/api/vehicles/{vid}/export/fuel.csv")
    assert r.status_code == 200
    # The CSV header uses the vehicle's FUEL_UNIT_LABELS value (kWh, capital K-W-h).
    assert "date,odometer,quantity_kWh" in r.text
    lines = r.text.splitlines()
    # quantity should round-trip to 25.5
    assert lines[1].startswith("2026-01-01,10000.0,25.5,")


def test_ev_pdf_export_works():
    vid = _mk_ev(name="PDF EV", currency="$")
    for i in range(15):
        _charge(vid, f"2026-{1 + i // 28:02d}-{1 + i % 28:02d}", 10000 + i * 200, 30, 9)
    r = c.get(f"/api/vehicles/{vid}/export/fuel.pdf")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF")
    assert "pdf_ev_fuel.pdf" in r.headers["content-disposition"]
    # All-data PDF should also work for EVs
    assert c.get(f"/api/vehicles/{vid}/export/all.pdf").content.startswith(b"%PDF")
    assert c.get(f"/api/vehicles/{vid}/export/all.csv").text.startswith("type,date")


def test_ev_pdf_export_dark_theme():
    """PDF should embed the brand icon for an EV just like a gas vehicle."""
    vid = _mk_ev(name="EV Dark")
    _charge(vid, "2026-01-01", 10000, 30, 9)
    r = c.get(f"/api/vehicles/{vid}/export/fuel.pdf?theme=dark")
    assert r.status_code == 200
    assert r.content.startswith(b"%PDF")
    assert b"/Subtype /Image" in r.content


def test_fuel_type_not_patchable():
    """VehiclePatch doesn't accept fuel_type, so historical quantity_minor
    values can never be silently re-interpreted as a different unit."""
    vid = _mk_ev()
    r = c.patch(f"/api/vehicles/{vid}", json={"fuel_type": "gas"})
    assert r.status_code == 200
    # Field is ignored — stays electric.
    assert r.json()["fuel_type"] == "electric"


def test_ev_can_be_deleted():
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 30, 9)
    assert c.delete(f"/api/vehicles/{vid}").status_code == 200
    assert c.get(f"/api/vehicles/{vid}").status_code == 404


def test_mixed_garage_gas_and_ev_independent():
    """A gas vehicle and an EV in the same instance compute economy with
    their own units and don't cross-contaminate."""
    gas_vid = _mk_vehicle(name="Gas Car")
    ev_vid = _mk_ev(name="EV")

    _fuel(gas_vid, "2026-01-01", 10000, 10, 30)
    _fuel(gas_vid, "2026-02-01", 10400, 10, 30)  # 400 mi / 10 gal = 40 mpg

    _charge(ev_vid, "2026-01-01", 10000, 30, 9)
    _charge(ev_vid, "2026-02-01", 10200, 50, 15)  # 200 mi / 50 kWh = 4 mi/kWh

    gas_s = _summary(gas_vid)
    ev_s = _summary(ev_vid)
    assert gas_s["economy_label"] == "mi/gal"
    assert ev_s["economy_label"] == "mi/kWh"
    assert gas_s["avg_economy"] == 40.0
    assert ev_s["avg_economy"] == 4.0


def test_ev_suspect_efficiency_is_flagged():
    """Same suspect logic as for gas vehicles, but in mi/kWh."""
    vid = _mk_ev()
    # Six normal intervals at 5 mi/kWh (500 mi / 100 kWh each).
    for i in range(7):
        _charge(vid, f"2026-{i + 1:02d}-01", 10000 + i * 500, 100, 30)
    # Outlier: 500 mi on 20 kWh = 25 mi/kWh (5x the median)
    _charge(vid, "2026-08-01", 103500 + 500, 20, 6)
    entries = c.get(f"/api/vehicles/{vid}/fuel").json()["entries"]
    suspect_odometers = [e["odometer"] for e in entries if e["suspect"]]
    assert suspect_odometers == [103500 + 500]


def test_ev_missed_charge_restarts_tracking():
    """Marking a charge as 'missed_before' restarts efficiency tracking,
    just like the gas equivalent."""
    vid = _mk_ev()
    _charge(vid, "2026-01-01", 10000, 10, 3)
    _charge(vid, "2026-02-01", 10500, 10, 3)  # 500 mi / 10 kWh = 50 mi/kWh
    r = c.post(
        f"/api/vehicles/{vid}/fuel",
        json={"date": "2026-03-01", "odometer": 11500, "quantity": 10, "cost": 3, "missed_before": True},
    )
    assert r.status_code == 201, r.text
    _charge(vid, "2026-04-01", 12000, 10, 3)  # 500 mi / 10 kWh = 50 mi/kWh
    by_odo = _entries(vid)
    assert by_odo[11500]["missed_before"] is True
    assert by_odo[11500].get("economy") is None
    assert by_odo[12000]["economy"] == 50.0
    # distance-weighted avg across both intervals: (500+500)/(10+10) = 50 mi/kWh
    assert _summary(vid)["avg_economy"] == 50.0


# ---------------------------------------------------------------------------
# Quick-log QR code + deep-link (?add-fuel=1) tests
# ---------------------------------------------------------------------------

def test_qr_endpoint_returns_png_for_same_origin_url():
    """Same-origin URL returns a PNG that decodes to that exact URL."""
    import io
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except ImportError:
        import pytest
        pytest.skip('Pillow/pyzbar not available for QR decode')
    url = 'http://testserver/#/fuel?add-fuel=1'
    r = c.get('/api/qr.png', params={'url': url})
    assert r.status_code == 200
    assert r.headers['content-type'] == 'image/png'
    assert r.content.startswith(b'\x89PNG')
    # Cache header set so repeated Settings views don't regenerate the PNG
    assert 'max-age=3600' in r.headers.get('cache-control', '')
    img = Image.open(io.BytesIO(r.content))
    decoded = decode(img)
    assert len(decoded) == 1
    assert decoded[0].data.decode() == url


def test_qr_endpoint_rejects_cross_origin_url():
    """A URL on a different origin is rejected with 422 (no open-redirect / phishing)."""
    r = c.get('/api/qr.png', params={'url': 'https://evil.example.com/path'})
    assert r.status_code == 422
    # And never returns image content
    assert r.headers['content-type'] != 'image/png'


def test_qr_endpoint_requires_url_param():
    """Missing url query returns 422 (FastAPI Query validation)."""
    assert c.get('/api/qr.png').status_code == 422
    # Empty url is also rejected (min_length=1)
    assert c.get('/api/qr.png', params={'url': ''}).status_code == 422


def test_qr_endpoint_rejects_non_url_input():
    """A bare path with no scheme/netloc is rejected (can't tell it's same-origin)."""
    r = c.get('/api/qr.png', params={'url': '/#/fuel?add-fuel=1'})
    assert r.status_code == 422


def test_qr_endpoint_accepts_path_under_same_origin():
    """A same-origin URL with any path/query/fragment is fine — the QR encodes it verbatim."""
    import io
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except ImportError:
        import pytest
        pytest.skip('Pillow/pyzbar not available for QR decode')
    # TestServer's origin is http://testserver, so any URL with that scheme+netloc works.
    url = 'http://testserver/glovebox/#/fuel?add-fuel=1'
    r = c.get('/api/qr.png', params={'url': url})
    assert r.status_code == 200
    decoded = decode(Image.open(io.BytesIO(r.content)))
    assert decoded[0].data.decode() == url


def test_qr_png_is_rendered_at_high_enough_resolution():
    """The PNG should be big enough to scan reliably when printed at ~2 inches."""
    import io
    try:
        from PIL import Image
    except ImportError:
        import pytest
        pytest.skip('Pillow not available')
    r = c.get('/api/qr.png', params={'url': 'http://testserver/#/fuel?add-fuel=1'})
    img = Image.open(io.BytesIO(r.content))
    # box_size=8 with a ~10-module QR + 2-module border gives ~140+ px;
    # we just need it to be scannable from a phone. 100 px is the minimum.
    assert img.width >= 100 and img.height >= 100


def test_qr_endpoint_independent_of_vehicles():
    """The QR endpoint doesn't require any vehicle to exist (works on a fresh instance)."""
    r = c.get('/api/qr.png', params={'url': 'http://testserver/#/fuel?add-fuel=1'})
    assert r.status_code == 200


# The deep-link behavior (?add-fuel=1) is exercised by the JS route() and
# render() functions in static/js/app.js — see the browser smoke test in
# scripts/smoke_test_qr.py for an end-to-end verification that:
#   - visiting /#/fuel?add-fuel=1 opens the Add fill-up modal
#   - closing the modal strips ?add-fuel=1 from the URL
#   - refresh after close does NOT reopen the modal
#   - a normal visit to /#/fuel does NOT open the modal
#   - the Settings page shows the QR code <img src="/api/qr.png?...">


# ---------------------------------------------------------------------------
# Vehicle-specific QR code / deep-link (?add-fuel=1&vehicle=N) tests
# ---------------------------------------------------------------------------

def test_qr_endpoint_encodes_vehicle_specific_url():
    """The QR for vehicle N encodes #/fuel?add-fuel=1&vehicle=N — so each
    vehicle in a multi-vehicle garage can have its own QR."""
    import io
    try:
        from PIL import Image
        from pyzbar.pyzbar import decode
    except ImportError:
        import pytest
        pytest.skip('Pillow/pyzbar not available for QR decode')
    url = 'http://testserver/#/fuel?add-fuel=1&vehicle=42'
    r = c.get('/api/qr.png', params={'url': url})
    assert r.status_code == 200
    decoded = decode(Image.open(io.BytesIO(r.content)))
    assert decoded[0].data.decode() == url


def test_qr_endpoint_accepts_query_with_multiple_params():
    """The same-origin check should accept any same-origin URL — including
    one with multiple query params (add-fuel + vehicle)."""
    r = c.get('/api/qr.png', params={'url': 'http://testserver/#/fuel?add-fuel=1&vehicle=7'})
    assert r.status_code == 200
    assert r.headers['content-type'] == 'image/png'


def test_qr_endpoint_accepts_non_numeric_vehicle():
    """The backend doesn't validate the vehicle value — it just encodes
    whatever URL is given. The frontend is responsible for parsing the value."""
    r = c.get('/api/qr.png', params={'url': 'http://testserver/#/fuel?add-fuel=1&vehicle=abc'})
    assert r.status_code == 200


# The vehicle preselect behavior (route() parsing `vehicle` from the URL) is
# exercised by the browser smoke test — see scripts/smoke_test_vehicle_qr.py
# for an end-to-end verification that:
#   - visiting /#/fuel?add-fuel=1&vehicle=N preselects vehicle N
#   - the Add fill-up modal opens for vehicle N
#   - scanning the EV's QR doesn't accidentally open the gas car
#   - changing the dropdown strips `vehicle` from the URL
#   - the small QR icon in the Add fill-up modal opens an enlarged preview
#   - closing the preview leaves the underlying modal intact
