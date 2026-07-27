"""
Import machine-longevity data from
"Machine and Equipment Longevity Tracking Tables(BLA).csv" into mint_store.

Matching rules (in order):
1. Look for an "ID <number>" in the CSV "Machine and Equipment" column.
   If that numeric EQ ID exists in the target department, use it.
2. Otherwise, if an Asset # is present and matches a known machine asset_num,
   use that machine's EQ ID.
3. Otherwise the row is reported as unmatched.
"""

import csv
import json
import os
import re

import mint_store as store

HERE = os.path.dirname(os.path.abspath(__file__))
CSV_PATH = os.path.join(HERE, "Machine and Equipment Longevity Tracking Tables(BLA).csv")
EQ_PATH = os.path.join(HERE, "equipment_data.json")

# Map CSV department strings to the keys used in MINT.
DEPT_MAP = {
    "Toilet Partitions Factory": "toilet",
    "Toilet Partitions": "toilet",
}

# Manual fixes for rows that could not be auto-matched.
MANUAL_ROW_EQID = {
    8: "811",
    31: "5273",
    42: "SKIP",
    52: "1969",
    93: "154",
}

# Rows that describe brand-new machines/tools to create in TPF.
MANUAL_ROW_NEW_MACHINE = {
    92: "press/clamp",
    95: "Glue Viscometer",
    96: "Dust Collector, HPL Edge Bander Standalone",
    97: "Dust Collector, CL Tenoner Standalone",
}

ERY_BUCKETS = [
    ("0-1", 15),
    ("1-2", 16),
    ("2-3", 17),
    ("3-4", 18),
    ("4-5", 19),
    ("5+", 20),
]


def norm(s: str) -> str:
    return " ".join((s or "").strip().split())


def clean(s: str) -> str:
    s = norm(s)
    return s if s else "N/A"


def _num_id(eq_id: str) -> str:
    m = re.search(r"(\d+)", eq_id or "")
    return m.group(1) if m else ""


def extract_id_numbers(text: str) -> list[str]:
    m = re.search(r"\bID\s*([\d\s/,-]+)", text or "", re.IGNORECASE)
    if not m:
        return []
    return re.findall(r"\d+", m.group(1))


def load_equipment():
    by_dept_eqid: dict[str, dict[str, dict]] = {}
    by_dept_asset: dict[str, dict[str, dict]] = {}

    if os.path.exists(EQ_PATH):
        with open(EQ_PATH, encoding="utf-8") as f:
            records = json.load(f)
        for e in records:
            key = DEPT_MAP.get(norm(e.get("dept") or ""))
            if not key:
                continue
            eq_num = _num_id(e.get("eq_id") or "")
            if eq_num:
                by_dept_eqid.setdefault(key, {})[eq_num] = e
            asset = (e.get("asset_num") or "").strip()
            if asset and asset.lower() != "n/a":
                by_dept_asset.setdefault(key, {})[asset] = e

    # Also match against user-added machines in the store.
    for m in store.list_machines():
        key = m.get("dept_key")
        eq_num = _num_id(m.get("eq_id") or "")
        if key and eq_num:
            rec = {"eq_id": m.get("eq_id"), "equipment_name": m.get("equipment_name"), "asset_num": m.get("asset_num")}
            by_dept_eqid.setdefault(key, {})[eq_num] = rec
        asset = (m.get("asset_num") or "").strip()
        if key and asset and asset.lower() != "n/a":
            by_dept_asset.setdefault(key, {})[asset] = rec

    return by_dept_eqid, by_dept_asset


def find_match(csv_name: str, asset_raw: str, dept_key: str,
               by_dept_eqid, by_dept_asset):
    eqid_map = by_dept_eqid.get(dept_key, {})
    asset_map = by_dept_asset.get(dept_key, {})

    for n in extract_id_numbers(csv_name):
        if n in eqid_map:
            return n, eqid_map[n]

    asset = (asset_raw or "").strip()
    if asset and asset.lower() != "n/a" and asset in asset_map:
        eq_num = _num_id(asset_map[asset].get("eq_id") or "")
        if eq_num:
            return eq_num, asset_map[asset]

    return None, None


def row_to_fields(row: list[str]) -> dict:
    # Index mapping based on the CSV header.
    return {
        "category": clean(row[2]) if len(row) > 2 else "N/A",
        "equipment_name_csv": clean(row[3]) if len(row) > 3 else "N/A",
        "location_workcenter": clean(row[4]) if len(row) > 4 else "N/A",
        "type_capex": clean(row[5]) if len(row) > 5 else "N/A",
        "serial_no": clean(row[6]) if len(row) > 6 else "N/A",
        "asset_num": clean(row[7]) if len(row) > 7 else "N/A",
        "year_new": clean(row[9]) if len(row) > 9 else "N/A",
        "condition": clean(row[10]) if len(row) > 10 else "N/A",
        "service_status": clean(row[11]) if len(row) > 11 else "N/A",
        "as_of_year_month": clean(row[12]) if len(row) > 12 else "N/A",
        "replacement_cost": clean(row[13]) if len(row) > 13 else "N/A",
        "replacement_year": clean(row[14]) if len(row) > 14 else "N/A",
        "ery": pick_ery(row),
        "comments": clean(row[21]) if len(row) > 21 else "N/A",
    }


def pick_ery(row: list[str]) -> str:
    for label, idx in ERY_BUCKETS:
        val = (row[idx] if len(row) > idx else "").strip()
        if val and val.lower() in ("x", "✓", "yes", "true", "1"):
            return label
    return "N/A"


def find_or_create_manual_machine(dept_key: str, name: str, asset_num: str = "") -> dict:
    """Avoid duplicate user-added machines if the import script is re-run."""
    target = name.strip().lower()
    for m in store.list_machines():
        if m.get("dept_key") == dept_key and (m.get("equipment_name") or "").strip().lower() == target:
            return m
    return store.add_machine(
        equipment_name=name,
        dept_key=dept_key,
        asset_num=(asset_num or "").strip().lower() == "n/a" and "" or (asset_num or "").strip(),
        author="CSV import",
    )


def main():
    by_dept_eqid, by_dept_asset = load_equipment()

    matched = []
    unmatched = []
    skipped = []

    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for i, row in enumerate(reader, start=2):
            csv_name = (row[3] if len(row) > 3 else "").strip()
            if not csv_name:
                continue
            dept_label = (row[1] if len(row) > 1 else "").strip()
            dept_key = DEPT_MAP.get(dept_label)
            if not dept_key:
                skipped.append({"row": i, "name": csv_name, "reason": f"unmapped department '{dept_label}'"})
                continue

            manual_eqid = MANUAL_ROW_EQID.get(i)
            if manual_eqid == "SKIP":
                skipped.append({"row": i, "name": csv_name, "reason": "user requested skip"})
                continue

            if manual_eqid:
                eq_id = manual_eqid
                eq_rec = by_dept_eqid.get(dept_key, {}).get(eq_id, {})
            elif i in MANUAL_ROW_NEW_MACHINE:
                asset_raw = (row[7] if len(row) > 7 else "").strip()
                eq_rec = find_or_create_manual_machine(dept_key, MANUAL_ROW_NEW_MACHINE[i], asset_raw)
                eq_id = _num_id(eq_rec["eq_id"])
            else:
                asset_raw = row[7] if len(row) > 7 else ""
                eq_id, eq_rec = find_match(csv_name, asset_raw, dept_key, by_dept_eqid, by_dept_asset)
                if not eq_id:
                    unmatched.append({"row": i, "name": csv_name, "department": dept_label})
                    continue

            fields = row_to_fields(row)
            fields["division_key"] = "bla"
            fields["equipment_name"] = fields["equipment_name_csv"]
            del fields["equipment_name_csv"]

            store.set_machine_info(dept_key, eq_id, fields, author="CSV import")
            matched.append({
                "row": i,
                "name": csv_name,
                "eq_id": eq_id,
                "matched_to": eq_rec.get("equipment_name") or csv_name,
            })

    print(f"Imported: {len(matched)}")
    print(f"Unmatched: {len(unmatched)}")
    print(f"Skipped: {len(skipped)}")

    if unmatched:
        print("\nUnmatched rows:")
        for u in unmatched:
            print(f"  row {u['row']}: {u['name']} ({u['department']})")
    if skipped:
        print("\nSkipped rows:")
        for s in skipped:
            print(f"  row {s['row']}: {s['name']} - {s['reason']}")


if __name__ == "__main__":
    main()
