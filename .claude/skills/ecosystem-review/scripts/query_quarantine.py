"""Fetch unreviewed quarantine items from Master.db."""
import json
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parents[4] / "Master.db"

def main():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM quarantine WHERE reviewed = 0 ORDER BY created_at DESC"
    ).fetchall()
    if not rows:
        print("No unreviewed quarantine items.")
        return
    print(f"Found {len(rows)} unreviewed items:\n")
    for r in rows:
        data = json.loads(r["raw_data"]) if r["raw_data"] else {}
        print(f"  ID: {r['id']}  |  Type: {r['entity_type']}  |  Module: {r['source_module']}")
        print(f"  Reason: {r['rejection_reason']}")
        print(f"  Created: {r['created_at']}")
        name = data.get("company_name") or data.get("investor_name") or data.get("headline") or "(no name)"
        print(f"  Name: {name}")
        print()
    conn.close()

if __name__ == "__main__":
    main()
