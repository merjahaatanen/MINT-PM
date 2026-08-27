#!/usr/bin/env python3
"""Sync the inactive_items table from a source mint.db to a target mint.db.

This preserves all other tables in the target DB (work orders, spare parts,
nicknames, etc.) and only copies/replaces the inactive_items table.

Usage:
    python sync_inactive_items.py <source_mint.db> <target_mint.db>

Example (run on local, then copy target to VM):
    python sync_inactive_items.py mint_data/mint.db mint_data/mint.db.inactive_synced
"""
import sqlite3
import sys


def sync(source_path: str, target_path: str) -> None:
    src = sqlite3.connect(source_path)
    dst = sqlite3.connect(target_path)
    try:
        rows = src.execute(
            "SELECT item_type, item_key, division_key, dept_key, dept_label, "
            "eq_id, name, created_at, created_by FROM inactive_items"
        ).fetchall()

        dst.execute("DELETE FROM inactive_items")
        dst.executemany(
            "INSERT OR REPLACE INTO inactive_items "
            "(item_type, item_key, division_key, dept_key, dept_label, eq_id, name, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        dst.commit()
        print(f"Synced {len(rows)} inactive item(s) from {source_path} to {target_path}")
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    sync(sys.argv[1], sys.argv[2])
