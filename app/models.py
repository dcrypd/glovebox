"""Request/response schemas and unit conversions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, Field, field_validator

from .economy import QUANTITY_PER_UNIT

DISTANCE_UNITS = {"mi", "km"}
FUEL_UNITS = {"gal", "l", "gal_uk", "kwh"}
SERVICE_CATEGORIES = {"service", "repair", "upgrade", "tax", "other"}

# A vehicle's fuel_type constrains which fuel_unit values are valid. The two
# are validated together (see VehicleIn.model_post_init) so a mismatch like
# fuel_type=gas + fuel_unit=kwh is rejected at the API boundary instead of
# silently producing nonsense economy numbers later.
FUEL_TYPES = {"gas", "electric"}
FUEL_UNITS_BY_TYPE = {
    "gas": {"gal", "l", "gal_uk"},
    "electric": {"kwh"},
}


def to_cents(value: float | int | str | None) -> int | None:
    # Decimal(str(value)) avoids float representation surprises like
    # 0.1 + 0.2 = 0.30000000000000004. None/empty pass through as None so
    # PATCH can clear a cost by sending null.
    if value is None or value == "":
        return None
    try:
        return int((Decimal(str(value)) * 100).quantize(Decimal("1")))
    except InvalidOperation:
        raise ValueError("invalid amount")


def to_minor_units(value: float | int | str, unit: str) -> int:
    """Convert a human-readable quantity (gal, L, kWh, ...) to its integer
    minor unit (millilitres for liquids, watt-hours for electricity)."""
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid quantity")
    return int((d * QUANTITY_PER_UNIT[unit]).quantize(Decimal("1")))


def to_tenths(value: float | int | str) -> int:
    # Odometer readings are stored as integer tenths of the vehicle's
    # distance unit, so 45000.3 mi becomes 450003 tenths. Keeping the math
    # exact avoids 0.1 + 0.2 drift over many edits.
    try:
        d = Decimal(str(value))
    except InvalidOperation:
        raise ValueError("invalid odometer")
    return int((d * 10).quantize(Decimal("1")))


def valid_date(v: str | None) -> str | None:
    """Require a real calendar date, not just the YYYY-MM-DD shape.

    Pydantic's date handling would accept some invalid strings; we go one
    step further and reject e.g. 2026-02-30 with a 422, matching what a
    user expects from a date picker. None / empty pass through so PATCH
    can leave the field unchanged.
    """
    if v is None or v == "":
        return v
    try:
        date.fromisoformat(v)
    except ValueError:
        raise ValueError("invalid calendar date")
    return v


def check_category(v: str) -> str:
    if v not in SERVICE_CATEGORIES:
        raise ValueError("invalid category")
    return v


def _check_fuel_type(v: str) -> str:
    if v not in FUEL_TYPES:
        raise ValueError("fuel_type must be gas or electric")
    return v


def _check_fuel_unit_for_type(unit: str, fuel_type: str) -> str:
    allowed = FUEL_UNITS_BY_TYPE.get(fuel_type, set())
    if unit not in allowed:
        raise ValueError(f"fuel_unit {unit!r} is not valid for fuel_type {fuel_type!r}")
    return unit


class VehicleIn(BaseModel):
    """POST /api/vehicles body. Fuel type, distance unit, and fuel unit are
    set at creation and immutable afterward (see VehiclePatch) — they decide
    how every historical quantity_minor value is interpreted, so changing
    them later would silently rewrite history."""
    name: str = Field(min_length=1, max_length=80)
    year: int | None = None
    make: str | None = None
    model: str | None = None
    plate: str | None = None
    fuel_type: str = "gas"
    distance_unit: str = "mi"
    fuel_unit: str = "gal"
    currency: str = "$"

    @field_validator("fuel_type")
    @classmethod
    def _ft(cls, v: str) -> str:
        return _check_fuel_type(v)

    @field_validator("distance_unit")
    @classmethod
    def _du(cls, v: str) -> str:
        if v not in DISTANCE_UNITS:
            raise ValueError("distance_unit must be mi or km")
        return v

    @field_validator("fuel_unit")
    @classmethod
    def _fu(cls, v: str) -> str:
        if v not in FUEL_UNITS:
            raise ValueError("fuel_unit must be gal, l, gal_uk or kwh")
        return v

    @field_validator("year")
    @classmethod
    def _year(cls, v: int | None) -> int | None:
        if v is not None and not (1900 <= v <= 2100):
            raise ValueError("year must be between 1900 and 2100")
        return v

    def model_post_init(self, __context) -> None:
        # Cross-field check: the fuel_unit must match the fuel_type. We
        # validate this after both fields have been parsed so a wrong
        # combination (e.g. fuel_type=gas, fuel_unit=kwh) is rejected
        # with a clear message rather than silently accepted.
        _check_fuel_unit_for_type(self.fuel_unit, self.fuel_type)


class VehiclePatch(BaseModel):
    """PATCH /api/vehicles/{vid} body. Only the rename-able fields are here;
    fuel_type / distance_unit / fuel_unit are intentionally NOT patchable
    (they decide how every historical quantity_minor is interpreted, so
    changing them after entries exist would silently rewrite history).
    Omitted fields stay unchanged on the server; explicit null clears a
    value where the field is nullable (year/make/model/plate)."""
    name: str | None = Field(default=None, min_length=1, max_length=80)
    year: int | None = None
    make: str | None = None
    model: str | None = None
    plate: str | None = None
    currency: str | None = None
    archived: bool | None = None

    @field_validator("year")
    @classmethod
    def _year(cls, v: int | None) -> int | None:
        if v is not None and not (1900 <= v <= 2100):
            raise ValueError("year must be between 1900 and 2100")
        return v


class FuelIn(BaseModel):
    """POST /api/vehicles/{vid}/fuel body. The same shape is used for both
    gas fill-ups and EV charges — `quantity` is gallons/litres/gal_uk/kWh
    depending on the vehicle's fuel_unit. The backend converts to the
    integer minor unit before storing."""
    date: str
    odometer: float = Field(gt=0)
    quantity: float = Field(gt=0)
    cost: float = Field(default=0, ge=0)
    full_tank: bool = True
    missed_before: bool = False
    station: str | None = None
    notes: str | None = None

    _vdate = field_validator("date")(valid_date)


class FuelPatch(BaseModel):
    """PATCH /api/fuel/{fid} body. Omitted fields stay unchanged; the route
    handler re-reads the vehicle for its fuel_unit so quantity conversions
    use the right unit."""
    date: str | None = None
    odometer: float | None = Field(default=None, gt=0)
    quantity: float | None = Field(default=None, gt=0)
    cost: float | None = Field(default=None, ge=0)
    full_tank: bool | None = None
    missed_before: bool | None = None
    station: str | None = None
    notes: str | None = None

    _vdate = field_validator("date")(valid_date)


class ServiceIn(BaseModel):
    date: str
    odometer: float | None = Field(default=None, gt=0)
    description: str = Field(min_length=1, max_length=200)
    category: str = "service"
    cost: float | None = Field(default=None, ge=0)
    notes: str | None = None

    _vdate = field_validator("date")(valid_date)
    _vcat = field_validator("category")(check_category)


class ServicePatch(BaseModel):
    date: str | None = None
    odometer: float | None = Field(default=None, gt=0)
    description: str | None = Field(default=None, min_length=1, max_length=200)
    category: str | None = None
    cost: float | None = Field(default=None, ge=0)
    notes: str | None = None

    _vdate = field_validator("date")(valid_date)
    _vcat = field_validator("category")(check_category)


class ReminderIn(BaseModel):
    """POST /api/vehicles/{vid}/reminders body. Either or both of due_date /
    due_odometer may be set; whichever comes first triggers the reminder.
    recur_distance / recur_months are optional recurring triggers that
    advance the reminder instead of completing it."""
    title: str = Field(min_length=1, max_length=200)
    notes: str | None = None
    due_date: str | None = None
    due_odometer: float | None = Field(default=None, gt=0)
    recur_distance: float | None = Field(default=None, gt=0)
    recur_months: int | None = Field(default=None, ge=1, le=120)

    _vdate = field_validator("due_date")(valid_date)


class ReminderPatch(BaseModel):
    """PATCH /api/reminders/{rid} body. `done` is patchable here, but the
    preferred path for completing a reminder is POST /api/reminders/{rid}/complete
    — that route handles recurring-reminder advancement (this one doesn't)."""
    title: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str | None = None
    due_date: str | None = None
    due_odometer: float | None = Field(default=None, gt=0)
    recur_distance: float | None = Field(default=None, gt=0)
    recur_months: int | None = Field(default=None, ge=1, le=120)
    done: bool | None = None

    _vdate = field_validator("due_date")(valid_date)


class SettingsIn(BaseModel):
    """PUT /api/settings body. Currently only the accent color; the key/value
    settings table can hold other instance-wide settings without a schema
    change (see db.py)."""
    accent: str | None = None
