"""Ledger: tracks payment history and vendor patterns in db/inventory.db.

Read by the approval agent (duplicate + structuring checks) and written by
the payment agent (recording outcomes), so vendor patterns accumulate across
runs of the pipeline.
"""

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional, TypedDict

DB_PATH = Path(__file__).parent.parent / "db" / "inventory.db"

# A vendor whose invoices repeatedly land just under the $10K approval
# scrutiny threshold may be "structuring" -- splitting/sizing invoices to
# dodge extra review.
NEAR_THRESHOLD_LOW = 8_000
NEAR_THRESHOLD_HIGH = 10_000
STRUCTURING_MIN_OCCURRENCES = 2


def normalize_invoice_number(value: Optional[str]) -> Optional[str]:
    """Canonical form used for duplicate-detection matching only.

    Collapses whitespace/underscores to a single hyphen and uppercases, so
    "INV 1012", "inv_1012", and "INV-1012" are recognized as the same
    invoice. Does not invent a missing "INV-" prefix or otherwise guess at
    content -- that would risk masking a genuinely different invoice number,
    it only normalizes formatting.
    """
    if not value:
        return None
    collapsed = re.sub(r"[\s_]+", "-", value.strip())
    collapsed = re.sub(r"-+", "-", collapsed)
    return collapsed.upper()


def normalize_vendor(value: Optional[str]) -> Optional[str]:
    """Canonical form used for vendor grouping/lookup only (raw vendor is still stored for display)."""
    if not value:
        return None
    return re.sub(r"[^a-z0-9]", "", value.lower())


def is_duplicate(invoice_number: Optional[str]) -> bool:
    """True if this invoice_number has already been paid."""
    if not invoice_number:
        return False
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM payments WHERE invoice_number_norm = ? AND status = 'paid' LIMIT 1",
        (normalize_invoice_number(invoice_number),),
    ).fetchone()
    conn.close()
    return row is not None


def get_paid_record(invoice_number: Optional[str]) -> Optional[dict]:
    """The prior paid submission for this invoice_number, if any (source_file + amount)."""
    if not invoice_number:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT source_file, amount FROM payments WHERE invoice_number_norm = ? AND status = 'paid' LIMIT 1",
        (normalize_invoice_number(invoice_number),),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    return {"source_file": row[0], "amount": row[1]}


class VendorStats(TypedDict):
    invoice_count: int
    paid_count: int
    rejected_count: int
    avg_amount: float
    near_threshold_count: int


def get_vendor_stats(vendor: Optional[str]) -> Optional[VendorStats]:
    """Historical stats for a vendor, or None if we have no prior invoices from them.

    Reads the `vendors` view, which is computed live from `payments` -- there
    is no separate vendors table to keep in sync.
    """
    if not vendor:
        return None
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT invoice_count, paid_count, rejected_count, avg_amount, near_threshold_count "
        "FROM vendors WHERE vendor_norm = ?",
        (normalize_vendor(vendor),),
    ).fetchone()
    conn.close()
    if row is None:
        return None
    keys = ("invoice_count", "paid_count", "rejected_count", "avg_amount", "near_threshold_count")
    return dict(zip(keys, row))  # type: ignore[return-value]


def is_structuring_pattern(vendor: Optional[str]) -> bool:
    """True if this vendor has a history of invoices sized just under the $10K threshold."""
    stats = get_vendor_stats(vendor)
    return bool(stats and stats["near_threshold_count"] >= STRUCTURING_MIN_OCCURRENCES)


def decrement_stock(items: list[tuple[str, int]]) -> None:
    """Decrement inventory.stock for each shipped line item.

    Allowed to go negative rather than clamped at zero: a negative balance
    is a visible signal that more was shipped than was on hand (e.g. an
    invoice approved despite an insufficient_stock flag), not an error to
    hide. Items not present in the inventory table are silently skipped --
    validation already flags those as unknown_item.
    """
    conn = sqlite3.connect(DB_PATH)
    conn.executemany(
        "UPDATE inventory SET stock = stock - ? WHERE item = ?",
        [(quantity, item) for item, quantity in items],
    )
    conn.commit()
    conn.close()


def record_payment(
    invoice_number: Optional[str],
    source_file: str,
    vendor: Optional[str],
    amount: Optional[float],
    invoice_date: Optional[str],
    status: Literal["paid", "rejected"],
) -> None:
    """Upsert a row into the payments ledger -- the only table agents write to.

    Keyed on (invoice_number, source_file): reprocessing the same source
    file overwrites its existing row (no duplicate accumulation on reruns),
    while a different file for the same invoice_number (e.g. a "_revised"
    resubmission) gets its own row, since that's a distinct submission, not
    a rerun. Vendor stats are read via the `vendors` view -- nothing else
    to update here.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        INSERT INTO payments (
            invoice_number, invoice_number_norm, source_file, vendor, vendor_norm,
            amount, status, invoice_date, processed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(invoice_number, source_file) DO UPDATE SET
            invoice_number_norm = excluded.invoice_number_norm,
            vendor = excluded.vendor,
            vendor_norm = excluded.vendor_norm,
            amount = excluded.amount,
            status = excluded.status,
            invoice_date = excluded.invoice_date,
            processed_at = excluded.processed_at
        """,
        (
            invoice_number,
            normalize_invoice_number(invoice_number),
            source_file,
            vendor,
            normalize_vendor(vendor),
            amount,
            status,
            invoice_date,
            now,
        ),
    )
    conn.commit()
    conn.close()
