"""CSV export and date parsing for invoice metadata."""
from __future__ import annotations

import csv
import io
from datetime import date

from .invoice import Invoice


def parse_date(text: str) -> date:
    """Accepts ISO dates (2024-03-05) and day-first dates (05/03/2024 = 5 March 2024)."""
    text = text.strip()
    if "-" in text:
        y, m, d = text.split("-")
        return date(int(y), int(m), int(d))
    a, b, y = text.split("/")
    return date(int(y), int(b), int(a))


def to_csv(invoice: Invoice) -> str:
    """One row per line, then summary rows. Fields containing commas or quotes are quoted."""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["sku", "description", "quantity", "unit_price", "line_total"])
    for line in invoice.lines:
        w.writerow([line.sku, line.description, line.quantity, str(line.unit_price),
                    str(line.total())])
    w.writerow(["", "subtotal", "", "", str(invoice.subtotal())])
    w.writerow(["", "discount", "", "", str(invoice.discount())])
    w.writerow(["", "tax", "", "", str(invoice.tax())])
    w.writerow(["", "total", "", "", str(invoice.total())])
    return out.getvalue()
