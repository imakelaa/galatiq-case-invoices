"""Creates the mock inventory SQLite database described in the README.
"""

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from agents.ledger import (  # noqa: E402
    NEAR_THRESHOLD_HIGH,
    NEAR_THRESHOLD_LOW,
    normalize_invoice_number,
    normalize_vendor,
)

DB_PATH = Path(__file__).parent / "inventory.db"

# (item, stock) — covers the items referenced across data/invoices/.
# SuperGizmo, MegaSprocket, and WidgetC are deliberately absent so they
# correctly trigger "unknown item" flags during validation.
SEED_DATA = [
    ("WidgetA", 55),
    ("WidgetB", 50),
    ("GadgetX", 15),
    ("FakeItem", 0),
]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS inventory")
    cursor.execute("CREATE TABLE inventory (item TEXT PRIMARY KEY, stock INTEGER)")
    cursor.executemany("INSERT INTO inventory VALUES (?, ?)", SEED_DATA)

    # payments is a persistent ledger, not reseeded data -- use IF NOT
    # EXISTS so history survives repeated setup runs. Keyed on
    # (invoice_number, source_file): reprocessing the same file overwrites
    # its row, but a distinct file (e.g. an "_revised" resubmission) gets
    # its own row rather than being treated as a duplicate.
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_number TEXT,
            invoice_number_norm TEXT,
            source_file TEXT,
            vendor TEXT,
            vendor_norm TEXT,
            amount REAL,
            status TEXT CHECK(status IN ('paid', 'rejected')),
            invoice_date TEXT,
            processed_at TEXT,
            UNIQUE(invoice_number, source_file)
        )
        """
    )
    _ensure_normalized_columns(conn, cursor)

    # vendors is derived entirely from payments -- a view, not a table, so
    # agents have exactly one writable table (payments) and vendor stats
    # can never drift out of sync with the ledger. Grouped by vendor_norm so
    # formatting drift (case/punctuation/whitespace) in how a vendor name was
    # extracted doesn't fragment one vendor's history into multiple rows;
    # `vendor` in the view is a representative display string for the group.
    cursor.execute("DROP VIEW IF EXISTS vendors")
    cursor.execute(
        f"""
        CREATE VIEW vendors AS
        SELECT
            vendor_norm,
            MIN(vendor) AS vendor,
            MIN(processed_at) AS first_seen,
            MAX(processed_at) AS last_seen,
            COUNT(*) AS invoice_count,
            SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid_count,
            SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
            AVG(amount) AS avg_amount,
            SUM(CASE WHEN amount >= {NEAR_THRESHOLD_LOW} AND amount < {NEAR_THRESHOLD_HIGH}
                     THEN 1 ELSE 0 END) AS near_threshold_count
        FROM payments
        WHERE vendor IS NOT NULL
        GROUP BY vendor_norm
        """
    )

    conn.commit()
    conn.close()

    print(f"Created {DB_PATH} with {len(SEED_DATA)} inventory items (payments table ready, vendors view ready).")


def _ensure_normalized_columns(conn: sqlite3.Connection, cursor: sqlite3.Cursor) -> None:
    """Migrate a payments table created before invoice_number_norm/vendor_norm existed.

    CREATE TABLE IF NOT EXISTS is a no-op against an existing table, so a
    pre-existing db/inventory.db needs its columns added and its historical
    rows backfilled explicitly.
    """
    existing_cols = {row[1] for row in cursor.execute("PRAGMA table_info(payments)")}
    if "invoice_number_norm" not in existing_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN invoice_number_norm TEXT")
    if "vendor_norm" not in existing_cols:
        cursor.execute("ALTER TABLE payments ADD COLUMN vendor_norm TEXT")

    rows = cursor.execute(
        "SELECT id, invoice_number, vendor FROM payments "
        "WHERE invoice_number_norm IS NULL OR vendor_norm IS NULL"
    ).fetchall()
    for row_id, invoice_number, vendor in rows:
        cursor.execute(
            "UPDATE payments SET invoice_number_norm = ?, vendor_norm = ? WHERE id = ?",
            (normalize_invoice_number(invoice_number), normalize_vendor(vendor), row_id),
        )


if __name__ == "__main__":
    main()
