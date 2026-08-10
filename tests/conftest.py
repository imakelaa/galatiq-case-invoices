import sqlite3

import pytest

from agents import ledger

INVENTORY_SEED = [
    ("WidgetA", 15),
    ("WidgetB", 10),
    ("GadgetX", 5),
    ("FakeItem", 0),
]


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """A temp sqlite DB with the same schema as db/setup_inventory.py.

    Monkeypatches ledger.DB_PATH so ledger functions read/write this
    throwaway DB instead of the real db/inventory.db.
    """
    db_path = tmp_path / "test_inventory.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE inventory (item TEXT PRIMARY KEY, stock INTEGER)")
    conn.executemany("INSERT INTO inventory VALUES (?, ?)", INVENTORY_SEED)
    conn.execute(
        """
        CREATE TABLE payments (
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
    conn.execute(
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
            SUM(CASE WHEN amount >= {ledger.NEAR_THRESHOLD_LOW} AND amount < {ledger.NEAR_THRESHOLD_HIGH}
                     THEN 1 ELSE 0 END) AS near_threshold_count
        FROM payments
        WHERE vendor IS NOT NULL
        GROUP BY vendor_norm
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(ledger, "DB_PATH", db_path)
    return db_path
