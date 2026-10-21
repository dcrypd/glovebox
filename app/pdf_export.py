"""Print-friendly, Glovebox-branded PDF reports (fuel log, record history, full export).

Pure rendering: callers pass plain dicts (and may pass the brand icon as PNG bytes),
so the CSV exports and DB layer stay untouched.
"""
from __future__ import annotations

import io
from datetime import date
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.utils import ImageReader
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Frame, PageTemplate, Paragraph,
                                Spacer, Table, TableStyle)

# Palette mirrors static/css/styles.css (light theme) so print matches the app.
# Each accent has three shades: the base color, a stronger one for dark-mode
# text on accent-soft backgrounds, and a soft tint for KPI / badge fills.
ACCENTS = {  # name: (accent, accent-strong, accent-soft)
    "amber": ("#a8690c", "#8a560a", "#f3e4c8"),
    "teal": ("#0d7d74", "#0a655e", "#d3ebe7"),
    "blue": ("#1d5fb8", "#164a93", "#d7e4f6"),
    "crimson": ("#b03535", "#8f2828", "#f5d9d9"),
    "violet": ("#7a46b8", "#613693", "#e6daf5"),
}
# Ink / muted / faint / rule / zebra mirror the CSS variables so the PDF reads
# like the app. Keep them in sync with static/css/styles.css.
INK = colors.HexColor("#28271f")
MUTED = colors.HexColor("#6e6c60")
FAINT = colors.HexColor("#a3a093")
RULE = colors.HexColor("#ddd9cf")
ZEBRA = colors.HexColor("#f4f3ef")

# Page geometry: margins, header (brand + report title + accent rule), footer
# (vehicle name + page X of Y). All in millimetres for readability.
MARGIN_X, MARGIN_TOP, MARGIN_BOTTOM = 16 * mm, 16 * mm, 16 * mm
HEADER_H, FOOTER_H = 22 * mm, 8 * mm


def _styles():
    """ParagraphStyle registry. Two flavours of header/body cell: "th/td" left-aligned,
    "thr/tdr" right-aligned (for numeric columns). Sizes are small so multi-month
    tables fit on a Letter page without scaling."""
    base = dict(fontName="Helvetica", textColor=INK)
    return {
        "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=INK),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=12, leading=15, textColor=INK,
                             keepWithNext=1),
        "sub": ParagraphStyle("sub", fontSize=9.5, leading=13, textColor=MUTED, fontName="Helvetica"),
        "th": ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=MUTED),
        "thr": ParagraphStyle("thr", fontName="Helvetica-Bold", fontSize=7.5, leading=9, textColor=MUTED,
                              alignment=TA_RIGHT),
        "td": ParagraphStyle("td", fontSize=8.5, leading=11, **base),
        "tdr": ParagraphStyle("tdr", fontSize=8.5, leading=11, alignment=TA_RIGHT, **base),
        "note": ParagraphStyle("note", fontSize=7.5, leading=9.5, textColor=MUTED, fontName="Helvetica"),
        "empty": ParagraphStyle("empty", fontSize=10, leading=14, textColor=MUTED, fontName="Helvetica"),
        "kl": ParagraphStyle("kl", fontName="Helvetica-Bold", fontSize=7, leading=9, textColor=MUTED),
        "kv": ParagraphStyle("kv", fontName="Helvetica-Bold", fontSize=13, leading=16, textColor=INK),
    }


def _p(text, style):
    """Paragraph helper: escapes the text (so user-entered descriptions/notes
    can't inject reportlab markup) and converts newlines to <br/> so multi-line
    notes render correctly."""
    return Paragraph(escape("" if text is None else str(text)).replace("\n", "<br/>"), style)


def _money(v, cur):
    """Format a number as `cur1,234.56` (cur is the currency symbol). Empty
    string for None so the cell renders blank instead of "None"."""
    return "" if v is None else f"{cur}{v:,.2f}"


def _num(v, d=1):
    """Format a number with `d` decimals and thousands separators. Empty
    string for None (e.g. economy on a baseline fill)."""
    return "" if v is None else f"{v:,.{d}f}"


def _fdate(iso):
    """ISO date -> "Jan 5, 2026" (the leading zero on day is dropped). Falls
    back to the raw input for non-date strings, so unexpected data still
    shows up rather than being hidden."""
    try:
        return date.fromisoformat(iso).strftime("%b %d, %Y").replace(" 0", " ")
    except (TypeError, ValueError):
        return iso or ""


def _draw_mark(c: Canvas, x, y, size, fill):
    """Legacy vector mark (accent square + arch stroke), only used as a fallback
    when the caller cannot supply the current brand icon as PNG bytes."""
    s = size / 32.0
    c.saveState()
    c.setFillColor(colors.HexColor(fill))
    c.roundRect(x, y, size, size, 7 * s, stroke=0, fill=1)
    c.setStrokeColor(colors.white)
    c.setLineWidth(2.4 * s)
    c.setLineCap(1)
    c.setLineJoin(1)

    def P(px, py):
        # Coordinate helper: the source SVG was drawn on a 32x32 grid;
        # this maps it into the destination rect so the mark scales.
        return x + px * s, y + (32 - py) * s
    p = c.beginPath()
    p.moveTo(*P(9, 20))
    p.curveTo(*P(11.2, 20), *P(11.2, 12), *P(14.5, 12))
    p.curveTo(*P(17.8, 12), *P(17.8, 20), *P(20, 20))
    c.drawPath(p, stroke=1, fill=0)
    c.restoreState()


def _make_page_decor(meta, accent, icon=None):
    """Build the per-page header/footer callback. Called once per report
    (not per page) — the returned `decor` is invoked by ReportLab on each
    page render. The header shows the brand mark, app name, report title,
    generation date, and an accent-coloured rule; the footer shows the
    vehicle name (top of the bottom margin) and the page-number is added
    separately by _NumberedCanvas."""
    acc = ACCENTS[accent][0]

    def decor(c: Canvas, doc):
        w, h = doc.pagesize
        c.saveState()
        top = h - MARGIN_TOP
        if icon:
            # Same brand asset the app header uses: icons/<accent>-<theme>/icon-192-rounded.png.
            # Rounded corners are baked into the PNG's alpha channel.
            c.drawImage(ImageReader(io.BytesIO(icon)), MARGIN_X, top - 11 * mm,
                        11 * mm, 11 * mm, mask="auto")
        else:
            _draw_mark(c, MARGIN_X, top - 11 * mm, 11 * mm, acc)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 15)
        c.drawString(MARGIN_X + 14 * mm, top - 6.5 * mm, "Glovebox")
        c.setFont("Helvetica", 8)
        c.setFillColor(MUTED)
        c.drawString(MARGIN_X + 14 * mm, top - 10.5 * mm, "Vehicle log")
        c.setFont("Helvetica-Bold", 9)
        c.setFillColor(INK)
        c.drawRightString(w - MARGIN_X, top - 5 * mm, meta["report"].upper())
        c.setFont("Helvetica", 8)
        c.setFillColor(MUTED)
        c.drawRightString(w - MARGIN_X, top - 9.5 * mm, f"Generated {_fdate(meta['generated'])}")
        c.setStrokeColor(colors.HexColor(acc))
        c.setLineWidth(1.6)
        c.line(MARGIN_X, top - 14 * mm, w - MARGIN_X, top - 14 * mm)
        c.setStrokeColor(RULE)
        c.setLineWidth(0.5)
        c.line(MARGIN_X, MARGIN_BOTTOM + 2 * mm, w - MARGIN_X, MARGIN_BOTTOM + 2 * mm)
        c.setFont("Helvetica", 7.5)
        c.setFillColor(FAINT)
        c.drawString(MARGIN_X, MARGIN_BOTTOM - 2 * mm, f"Glovebox  |  {meta['vehicle_name']}")
        c.restoreState()

    return decor


class _NumberedCanvas(Canvas):
    """Two-pass canvas so the footer can say 'Page X of Y'.

    ReportLab's Canvas.showPage() finalizes a page, but at that point we
    don't know how many pages will follow. This subclass buffers each page's
    state, and on save() replays them in order with the now-known total —
    letting us stamp "Page N of T" on every page at the bottom-right.
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._saved = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            w, _ = self._pagesize
            self.setFont("Helvetica", 7.5)
            self.setFillColor(FAINT)
            self.drawRightString(w - MARGIN_X, MARGIN_BOTTOM - 2 * mm, f"Page {self._pageNumber} of {total}")
            super().showPage()
        super().save()


def _vehicle_lines(v):
    title = " ".join(str(x) for x in (v.get("year"), v.get("make"), v.get("model")) if x)
    bits = [title] if title else []
    if v.get("plate"):
        bits.append(f"Plate {v['plate']}")
    return "  |  ".join(bits)


def _kpis(items, width, st, accent):
    """Build the KPI strip at the top of each report: a two-row table (label
    row + value row) with accent-soft background and thin white separators.
    `items` is a list of (label, value) tuples."""
    soft = ACCENTS[accent][2]
    cells = [[Paragraph(escape(k.upper()), st["kl"]) for k, _ in items],
             [Paragraph(escape(val), st["kv"]) for _, val in items]]
    t = Table(cells, colWidths=[width / len(items)] * len(items))
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(soft)),
        ("LEFTPADDING", (0, 0), (-1, -1), 8), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, 0), (-1, 0), 0),
        ("TOPPADDING", (0, 1), (-1, 1), 1), ("BOTTOMPADDING", (0, 1), (-1, 1), 7),
        ("LINEBEFORE", (1, 0), (-1, -1), 0.5, colors.white),
    ]))
    return t


def _table(headers, rows, widths, st, accent, right_cols):
    """Build a table with a styled header row (accent underline), zebra-striped
    data rows, and right-aligned numeric columns. `right_cols` is the set of
    column indices that should use the right-aligned style."""
    acc = ACCENTS[accent][0]
    head = [Paragraph(escape(h.upper()), st["thr"] if i in right_cols else st["th"])
            for i, h in enumerate(headers)]
    data = [head] + rows
    t = Table(data, colWidths=widths, repeatRows=1, splitByRow=1)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 3.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 3.5),
        ("LINEBELOW", (0, 0), (-1, 0), 1, colors.HexColor(acc)),
        ("LINEBELOW", (0, 1), (-1, -1), 0.25, RULE),
        ("TOPPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
    ]
    # Zebra striping on every other data row (index 2, 4, ... — row 0 is the
    # header, so the first data row is index 1 and the second is index 2).
    for i in range(2, len(data), 2):
        style.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
    t.setStyle(TableStyle(style))
    return t


def _build(story_fn, meta, accent, pagesize, icon=None):
    """Render a PDF. `story_fn` is called with (styles, content_width, accent)
    and returns the Platypus flowables (intro, KPIs, tables, etc.). The header /
    footer are drawn by `_make_page_decor` on every page via a PageTemplate,
    and the body is laid out in a Frame between the header and footer.
    Unknown accents fall back to blue."""
    accent = accent if accent in ACCENTS else "blue"
    buf = io.BytesIO()
    w, h = pagesize
    doc = BaseDocTemplate(buf, pagesize=pagesize, leftMargin=MARGIN_X, rightMargin=MARGIN_X,
                          topMargin=MARGIN_TOP, bottomMargin=MARGIN_BOTTOM,
                          title=f"{meta['vehicle_name']} - {meta['report']}", author="Glovebox",
                          subject=meta["report"])
    frame = Frame(MARGIN_X, MARGIN_BOTTOM + FOOTER_H - 4 * mm, w - 2 * MARGIN_X,
                  h - MARGIN_TOP - HEADER_H - MARGIN_BOTTOM - FOOTER_H + 4 * mm,
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0, id="body")
    doc.addPageTemplates([PageTemplate(id="p", frames=[frame],
                                       onPage=_make_page_decor(meta, accent, icon))])
    doc.build(story_fn(_styles(), w - 2 * MARGIN_X, accent), canvasmaker=_NumberedCanvas)
    return buf.getvalue()


def _intro(meta, st):
    """Standard report opening: report title, vehicle name, and the
    year/make/model/plate line if the vehicle has any of them."""
    out = [_p(meta["report"], st["title"]), Spacer(1, 1.5 * mm), _p(meta["vehicle_name"], st["sub"])]
    line = _vehicle_lines(meta["vehicle"])
    if line:
        out.append(_p(line, st["sub"]))
    out.append(Spacer(1, 4 * mm))
    return out


def _meta(report, vehicle, today):
    """Build the metadata dict passed to the page decorator and intro.
    `today` is injectable so tests can produce stable output."""
    return {"report": report, "vehicle_name": vehicle.get("name", ""), "vehicle": vehicle,
            "generated": (today or date.today()).isoformat()}


def _distance_unit(vehicle):
    return "km" if vehicle.get("distance_unit") == "km" else "mi"


def _is_electric(vehicle):
    return (vehicle or {}).get("fuel_type") == "electric"


def _fuel_terms(vehicle):
    """Vehicle-type-aware nouns/labels for the fuel/energy log.

    Centralizes the gas-vs-EV wording so the three PDF builders all use the
    same terms. Keys: count_label (KPI: "Fill-ups"/"Charges"), level_head
    (table column "Tank"/"Charge"), location_head ("Station / notes"/"Location / notes"),
    spend_label, empty_msg, note (the footnote under the table), log_heading
    (the section title in the all-data PDF).
    """
    if _is_electric(vehicle):
        return {
            "count_label": "Charges",
            "level_head": "Charge",
            "location_head": "Location / notes",
            "spend_label": "Total energy spend",
            "empty_msg": "No charging sessions have been logged for this vehicle yet.",
            "note": "Efficiency is calculated between consecutive full charges; partial charges roll into the next full charge.",
            "log_heading": "Charge log",
        }
    return {
        "count_label": "Fill-ups",
        "level_head": "Tank",
        "location_head": "Station / notes",
        "spend_label": "Total fuel spend",
        "empty_msg": "No fill-ups have been logged for this vehicle yet.",
        "note": "Economy is calculated between consecutive full tanks; partial fills roll into the next full tank.",
        "log_heading": "Fuel log",
    }


def _fuel_table(entries, summary, vehicle, width, st, accent):
    cur = vehicle.get("currency") or ""
    du = _distance_unit(vehicle)
    fu_lbl = summary.get("fuel_unit_label", "gal")
    econ_lbl = summary.get("economy_label", "")
    t = _fuel_terms(vehicle)
    heads = ["Date", f"Odometer ({du})", f"Qty ({fu_lbl})", "Cost", "Price/unit",
             f"Economy ({econ_lbl})", t["level_head"], t["location_head"]]
    fr = [0.12, 0.12, 0.10, 0.10, 0.10, 0.12, 0.08, 0.26]
    rows = []
    for e in entries:
        extra = " - ".join(x for x in (e.get("station"), e.get("notes")) if x)
        rows.append([_p(_fdate(e.get("date")), st["td"]), _p(_num(e.get("odometer"), 0), st["tdr"]),
                     _p(_num(e.get("quantity"), 3), st["tdr"]), _p(_money(e.get("cost"), cur), st["tdr"]),
                     _p(_money(e.get("unit_price"), cur), st["tdr"]),
                     _p(_num(e.get("economy"), 2), st["tdr"]),
                     _p("Full" if e.get("full_tank") else "Partial", st["td"]), _p(extra, st["td"])])
    return [_table(heads, rows, [width * f for f in fr], st, accent, {1, 2, 3, 4, 5}),
            Spacer(1, 3 * mm),
            _p(t["note"], st["note"])]


def _service_table(entries, vehicle, width, st, accent):
    cur = vehicle.get("currency") or ""
    du = _distance_unit(vehicle)
    heads = ["Date", f"Odometer ({du})", "Description", "Category", "Cost"]
    fr = [0.13, 0.14, 0.45, 0.14, 0.14]
    rows = []
    for e in entries:
        desc = [Paragraph(f"<b>{escape(e.get('description') or '')}</b>", st["td"])]
        if e.get("notes"):
            desc.append(_p(e["notes"], st["note"]))
        rows.append([_p(_fdate(e.get("date")), st["td"]), _p(_num(e.get("odometer"), 0), st["tdr"]),
                     desc, _p((e.get("category") or "").capitalize(), st["td"]),
                     _p(_money(e.get("cost"), cur), st["tdr"])])
    return [_table(heads, rows, [width * f for f in fr], st, accent, {1, 4})]


def fuel_pdf(vehicle: dict, entries: list[dict], summary: dict, accent: str = "blue",
             pagesize=LETTER, today: date | None = None, icon: bytes | None = None) -> bytes:
    """Build the Fuel log / Charge log PDF.

    `entries` are newest-first dicts as produced by main.fuel_payload()
    (date, odometer, quantity, cost, unit_price, economy, full_tank, station,
    notes). `summary` carries economy_label, avg_economy, distance_logged.
    `icon` is an optional PNG bytes of the brand icon; when None a vector
    fallback is drawn instead (see _draw_mark).
    """
    cur = vehicle.get("currency") or ""
    du = _distance_unit(vehicle)
    t = _fuel_terms(vehicle)
    meta = _meta(t["log_heading"], vehicle, today)

    def story(st, width, accent):
        s = _intro(meta, st)
        avg = summary.get("avg_economy")
        s.append(_kpis([(t["count_label"], f"{len(entries):,}"),
                        ("Avg economy", f"{_num(avg, 2)} {summary.get('economy_label', '')}" if avg is not None else "-"),
                        ("Distance logged", f"{_num(summary.get('distance_logged'), 0)} {du}"),
                        (t["spend_label"], _money(sum(e.get("cost") or 0 for e in entries), cur))],
                       width, st, accent))
        s.append(Spacer(1, 5 * mm))
        if not entries:
            s.append(_p(t["empty_msg"], st["empty"]))
        else:
            s.extend(_fuel_table(entries, summary, vehicle, width, st, accent))
        return s

    return _build(story, meta, accent, pagesize, icon)


def service_pdf(vehicle: dict, entries: list[dict], accent: str = "blue",
                pagesize=LETTER, today: date | None = None, icon: bytes | None = None) -> bytes:
    """Build the Record history PDF.

    `entries` are newest-first dicts with date, odometer, description,
    category, cost, notes — as produced by main.row_service(). Renamed from
    "Service history" to "Record history" when the in-app terminology was
    updated; the function name (and the internal `service_entries` parameter
    name in all_data_pdf) is kept as an internal identifier.
    """
    cur = vehicle.get("currency") or ""
    meta = _meta("Record history", vehicle, today)

    def story(st, width, accent):
        s = _intro(meta, st)
        s.append(_kpis([("Records", f"{len(entries):,}"),
                        ("Total spend", _money(sum(e.get("cost") or 0 for e in entries), cur)),
                        ("Latest record", _fdate(entries[0]["date"]) if entries else "-")], width, st, accent))
        s.append(Spacer(1, 5 * mm))
        if not entries:
            s.append(_p("No records have been logged for this vehicle yet.", st["empty"]))
        else:
            s.extend(_service_table(entries, vehicle, width, st, accent))
        return s

    return _build(story, meta, accent, pagesize, icon)


def all_data_pdf(vehicle: dict, fuel_entries: list[dict], fuel_summary: dict,
                 service_entries: list[dict], accent: str = "blue",
                 pagesize=LETTER, today: date | None = None, icon: bytes | None = None) -> bytes:
    """Build the combined "Complete vehicle data" PDF: records section first,
    then the fuel/energy log, with a CondPageBreak between so each starts on a
    fresh page when there's room. `fuel_summary` and `fuel_entries` mirror the
    fuel_pdf() args; `service_entries` mirrors service_pdf()."""
    cur = vehicle.get("currency") or ""
    du = _distance_unit(vehicle)
    t = _fuel_terms(vehicle)
    energy_word = "Energy" if t is not None and _is_electric(vehicle) else "Fuel"
    meta = _meta("Complete vehicle data", vehicle, today)

    def story(st, width, accent):
        fuel_total = sum(e.get("cost") or 0 for e in fuel_entries)
        svc_total = sum(e.get("cost") or 0 for e in service_entries)
        avg = fuel_summary.get("avg_economy")
        s = _intro(meta, st)
        s.append(_kpis([("Records", f"{len(service_entries):,}"),
                        (t["count_label"], f"{len(fuel_entries):,}"),
                        ("Avg economy", f"{_num(avg, 2)} {fuel_summary.get('economy_label', '')}" if avg is not None else "-"),
                        ("Total spend", _money(fuel_total + svc_total, cur))], width, st, accent))
        s.append(_p(f"Records {_money(svc_total, cur)}  |  {energy_word} {_money(fuel_total, cur)}  |  "
                    f"{_num(fuel_summary.get('distance_logged'), 0)} {du} logged", st["note"]))
        s.append(Spacer(1, 6 * mm))
        s.append(_p("Records", st["h2"]))
        s.append(Spacer(1, 2 * mm))
        if service_entries:
            s.extend(_service_table(service_entries, vehicle, width, st, accent))
        else:
            s.append(_p("No records have been logged for this vehicle yet.", st["empty"]))
        s.append(CondPageBreak(60 * mm))
        s.append(Spacer(1, 8 * mm))
        s.append(_p(t["log_heading"], st["h2"]))
        s.append(Spacer(1, 2 * mm))
        if fuel_entries:
            s.extend(_fuel_table(fuel_entries, fuel_summary, vehicle, width, st, accent))
        else:
            s.append(_p(t["empty_msg"], st["empty"]))
        return s

    return _build(story, meta, accent, pagesize, icon)
