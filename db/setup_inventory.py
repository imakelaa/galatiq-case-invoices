"""Creates the mock inventory SQLite database described in the README.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "inventory.db"

# (item, stock) — covers the items referenced across data/invoices/.
# SuperGizmo, MegaSprocket, and WidgetC are deliberately absent so they
# correctly trigger "unknown item" flags during validation.
SEED_DATA = [
    ("WidgetA", 15),
    ("WidgetB", 10),
    ("GadgetX", 5),
    ("FakeItem", 0),
]


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS inventory")
    cursor.execute("CREATE TABLE inventory (item TEXT PRIMARY KEY, stock INTEGER)")
    cursor.executemany("INSERT INTO inventory VALUES (?, ?)", SEED_DATA)

    conn.commit()
    conn.close()

    print(f"Created {DB_PATH} with {len(SEED_DATA)} inventory items.")


if __name__ == "__main__":
    main()
