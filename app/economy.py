"""Fuel-economy / energy-efficiency computations.

All stored quantities are exact integers:
- odometer: tenths of the vehicle's distance unit (mi or km)
- fuel/energy: the integer minor unit of the vehicle's fuel_unit
  (millilitres for gal/l/gal_uk; watt-hours for kwh)
- money: cents (minor units)

Economy intervals follow the standard full-tank method:
a valid interval starts at a full tank/charge and ends at the next one.
Distance = difference in odometer readings; energy used = every fill recorded
after the starting full tank, partial fills included, plus the ending fill.
The first full fill is only a baseline and produces no economy figure.

A fill-up flagged ``missed_before`` means an earlier fill-up was never logged.
The open interval is discarded (its energy total would be too low and the
economy too high) and tracking restarts: a flagged full fill becomes the new
baseline.

Intervals whose economy is far from the median interval are flagged as
``suspect_ids`` so the UI can suggest a missed fill-up or a typo. The median
is used (not the mean) so one bad reading cannot drag the baseline and cause
its normal neighbours to be flagged too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, getcontext

getcontext().prec = 28

# An interval is "suspect" when its economy differs from the median interval
# economy by more than this fraction (needs enough intervals to judge).
# 40% catches one-off data-entry typos and missed fill-ups without flagging
# normal highway/city variation.
SUSPECT_THRESHOLD = Decimal("0.4")
# Below this many intervals we don't flag anything — the median is too
# noisy with so few samples to be a trustworthy baseline.
SUSPECT_MIN_INTERVALS = 4

# Minor units per fuel/energy unit (exact definitions). The stored integer
# column ``quantity_minor`` is multiplied by the vehicle's fuel_unit to
# produce a human-readable quantity, e.g. ``quantity_minor / QUANTITY_PER_UNIT['gal']``
# yields US gallons. For electric vehicles, ``kwh`` stores watt-hours, so
# 1 kWh = 1000 Wh.
QUANTITY_PER_UNIT: dict[str, Decimal] = {
    "gal": Decimal("3785.411784"),   # US liquid gallon → millilitres
    "l": Decimal("1000"),            # litre → millilitres
    "gal_uk": Decimal("4546.09"),     # Imperial gallon → millilitres
    "kwh": Decimal("1000"),           # kilowatt-hour → watt-hours
}

FUEL_UNIT_LABELS = {
    "gal": "gal",
    "l": "L",
    "gal_uk": "imp gal",
    "kwh": "kWh",
}


def quantity_from_minor(minor: int, unit: str) -> Decimal:
    """Convert a stored minor-unit integer to a Decimal quantity in the
    vehicle's unit (gallons, litres, kWh, ...). Quantized to 3 decimals,
    which is what the UI and CSV exports display."""
    return (Decimal(minor) / QUANTITY_PER_UNIT[unit]).quantize(Decimal("0.001"))


def economy_label(distance_unit: str, fuel_unit: str) -> str:
    """Build the human-readable label, e.g. "mi/gal" or "km/kWh". Used by
    the dashboard, fuel page, CSV/PDF exports, and recent-activity details."""
    du = "mi" if distance_unit == "mi" else "km"
    fu = FUEL_UNIT_LABELS[fuel_unit]
    return f"{du}/{fu}"


@dataclass
class Interval:
    """One full-tank-to-full-tank interval: the unit of economy computation.

    Distance is the odometer delta; ``quantity_minor`` is the *total* energy
    used between the two fulls (every fill after the start, partials included,
    plus the ending fill). The closing-entry id is kept so the API can attach
    the interval to its closing entry when serializing per-row economy.
    """
    start_entry_id: int
    end_entry_id: int
    distance_tenths: int
    quantity_minor: int
    fuel_cost_cents: int

    @property
    def distance(self) -> Decimal:
        # Stored as tenths so the math stays exact until the very last division.
        return Decimal(self.distance_tenths) / Decimal(10)

    def economy(self, fuel_unit: str) -> Decimal:
        """Distance per energy unit (e.g. mi/gal or mi/kWh). The same formula
        works for gas and EVs because QUANTITY_PER_UNIT knows about kWh."""
        return self.distance / (Decimal(self.quantity_minor) / QUANTITY_PER_UNIT[fuel_unit])


@dataclass
class FuelStats:
    """Aggregated results of compute_fuel_stats(): valid intervals, their
    totals, and the set of suspicious closing entries the UI should badge.

    ``closing`` maps the closing entry's id to its interval so the per-row
    serializer can decorate each entry with the interval's economy figure
    (and only the closing entry of each interval gets an economy value).
    """
    intervals: list[Interval] = field(default_factory=list)
    closing: dict[int, Interval] = field(default_factory=dict)
    total_distance_tenths: int = 0
    total_quantity_minor: int = 0
    total_fuel_cost_cents: int = 0
    # Ids of closing entries whose economy looks unusual (possible missed
    # fill-up or typo). The UI shows a "Check" badge on these.
    suspect_ids: set[int] = field(default_factory=set)

    @property
    def total_distance(self) -> Decimal:
        return Decimal(self.total_distance_tenths) / Decimal(10)

    def avg_economy(self, fuel_unit: str) -> Decimal | None:
        """Distance-weighted average economy across all valid intervals.
        This is *not* the mean of per-interval economies — it's total distance
        divided by total energy, so a 300-mile road trip isn't weighted
        equally with a short commute."""
        if self.total_quantity_minor <= 0:
            return None
        return self.total_distance / (Decimal(self.total_quantity_minor) / QUANTITY_PER_UNIT[fuel_unit])

    def cost_per_distance(self) -> Decimal | None:
        """Energy cost per distance unit across valid intervals, in currency units.
        None when there's no logged distance (e.g. only a baseline fill so far)."""
        if self.total_distance_tenths <= 0:
            return None
        return (Decimal(self.total_fuel_cost_cents) / Decimal(100)) / self.total_distance


def compute_fuel_stats(
    entries: list[dict],
    fuel_unit: str,
) -> FuelStats:
    """Compute interval statistics for a vehicle's fuel/charge entries.

    Walks the chronologically-sorted entries once, accumulating energy/cost
    between full tanks and emitting an Interval when the next full tank is
    reached. The anchor (the last full tank) moves forward after each
    interval so the next one starts cleanly. Edits, backdated entries, and
    deletions all trigger a full recompute — callers don't need to incrementally
    patch the stats.

    ``entries`` must be dicts with keys: id, odometer_tenths, quantity_minor,
    cost_cents, full_tank — sorted chronologically (date, then insertion id).
    """
    stats = FuelStats()
    anchor_id: int | None = None   # entry id of the most recent full tank
    anchor_odo: int | None = None   # odometer at that anchor
    energy_since = 0                # energy accumulated since the anchor
    cost_since = 0                  # cost accumulated since the anchor

    for e in entries:
        # ``missed_before`` discards the in-flight interval (the energy total
        # would be too low and the economy too high) and restarts tracking
        # here. The flagged full fill becomes the new baseline anchor.
        if e.get("missed_before"):
            anchor_id = anchor_odo = None
            energy_since = 0
            cost_since = 0

        # Accumulate energy/cost for every fill after the anchor — partials
        # roll into the next full tank, just like the closing fill does.
        if anchor_id is not None:
            energy_since += e["quantity_minor"]
            cost_since += e["cost_cents"]

        # Only full tanks can close an interval. Partial fills just
        # contribute their energy to the running totals above.
        if not e["full_tank"]:
            continue

        # Emit an interval only if the odometer moved forward; a backwards
        # reading (e.g. a typo or a vehicle swap) skips the interval but the
        # anchor still moves forward so tracking continues from here.
        if anchor_id is not None and e["odometer_tenths"] > anchor_odo:
            interval = Interval(
                start_entry_id=anchor_id,
                end_entry_id=e["id"],
                distance_tenths=e["odometer_tenths"] - anchor_odo,
                quantity_minor=energy_since,
                fuel_cost_cents=cost_since,
            )
            stats.intervals.append(interval)
            stats.closing[e["id"]] = interval
            stats.total_distance_tenths += interval.distance_tenths
            stats.total_quantity_minor += interval.quantity_minor
            stats.total_fuel_cost_cents += interval.fuel_cost_cents

        # This full tank becomes the anchor for the next interval.
        anchor_id = e["id"]
        anchor_odo = e["odometer_tenths"]
        energy_since = 0
        cost_since = 0

    _flag_suspect_intervals(stats, fuel_unit)
    return stats


def _flag_suspect_intervals(stats: FuelStats, fuel_unit: str) -> None:
    """Mark intervals whose economy is far from the median interval economy.

    Uses the median (not the mean) so a single bad reading can't drag the
    baseline and cause its normal neighbours to be flagged too — the median
    is robust to a single outlier, while the mean would shift toward it.
    """
    if len(stats.intervals) < SUSPECT_MIN_INTERVALS:
        return
    econs = [iv.economy(fuel_unit) for iv in stats.intervals]
    values = sorted(econs)
    mid = len(values) // 2
    # Median of an even-length list is the average of the two middle values.
    median = values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2
    if median <= 0:
        return
    for iv, econ in zip(stats.intervals, econs):
        if abs(econ - median) / median > SUSPECT_THRESHOLD:
            # Flag the closing entry — that's the row the UI shows next to
            # the economy figure, so the badge appears in the right place.
            stats.suspect_ids.add(iv.end_entry_id)
