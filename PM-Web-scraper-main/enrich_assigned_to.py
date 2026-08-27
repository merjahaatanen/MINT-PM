#!/usr/bin/env python3
"""
enrich_assigned_to.py
======================
One-time enrichment script.

Reads the two saved PM "All Open Work Orders" tab HTML files:
  - "PM open unscheduled HTML"  -> extracts the "Owner" column
  - "PM Open Scheduled HTML"    -> extracts the "Assigned To (Skill)" column

Maps each work order ID to its scraped owner/skill value and writes an
`assigned_to` field into the existing per-department work_orders_*.json
(and corresponding .csv) files.

The server already expects an `assigned_to` field on work order records, so
after this runs a simple /api/reload (or server restart) will surface the
values in MINT.
"""

import csv
import json
import os
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

HERE = Path(__file__).parent

UNSCHEDULED_HTML = HERE / "PM open unscheduled HTML"
SCHEDULED_HTML = HERE / "PM Open Scheduled HTML"

# Map from the department names shown in the PM grids to the JSON/CSV file keys.
DEPT_KEY_BY_NAME = {
    "Toilet Partitions": "toilet_partitions",
    "Maintenance": "maintenance",
    "Soap Dispenser Assembly": "soap_dispenser_assembly",
    "Machine Shop": "machine_shop",
    "Shipping": "shipping",
    "Mfg Engineering": "mfg_engineering",
    "Quality Assurance": "quality_assurance",
    "General": "general",
    "Assembly": "assembly",
}


def _clean(s) -> str:
    return " ".join(str(s or "").split()).strip()


def _wo_id_from_onclick(onclick: str) -> str:
    m = re.search(r"\((\d+)", onclick or "")
    return m.group(1) if m else ""


def _extract_unscheduled_owner_rows(soup: BeautifulSoup) -> dict[str, tuple[str, str]]:
    """Return {wo_id: (department_name, owner)} from #gridWOUH."""
    out = {}
    grid = soup.find(id="gridWOUH")
    if not grid:
        print("[warn] #gridWOUH not found in unscheduled HTML")
        return out
    for row in grid.find_all("tr", class_="k-master-row"):
        link = row.find("a", class_="id-links")
        wo_id = _wo_id_from_onclick(link.get("onclick") if link else "")
        if not wo_id:
            continue
        cells = row.find_all("td", role="gridcell")
        # Columns: 0=ID, 1=Urgency, 2=Department, 3=EquipName, 4=Problem,
        #          5=Owner, 6=DateNotified, 7=Comment
        dept = _clean(_extract_cell_text(cells, 2))
        owner = _clean(_extract_cell_text(cells, 5))
        out[wo_id] = (dept, owner)
    return out


def _extract_scheduled_skill_rows(soup: BeautifulSoup) -> dict[str, tuple[str, str]]:
    """Return {wo_id: (department_name, assigned_to_skill)} from #gridWOSH."""
    out = {}
    grid = soup.find(id="gridWOSH")
    if not grid:
        print("[warn] #gridWOSH not found in scheduled HTML")
        return out
    for row in grid.find_all("tr", class_="k-master-row"):
        link = row.find("a", class_="id-links")
        wo_id = _wo_id_from_onclick(link.get("onclick") if link else "")
        if not wo_id:
            continue
        cells = row.find_all("td", role="gridcell")
        # Columns: 0=ID, 1=Department, 2=EquipName, 3=AuditItem,
        #          4=AssignedToSkill, 5=DueDate, 6=Comment
        dept = _clean(_extract_cell_text(cells, 1))
        skill = _clean(_extract_cell_text(cells, 4))
        out[wo_id] = (dept, skill)
    return out


def _extract_cell_text(cells, idx: int) -> str:
    if idx < len(cells):
        return cells[idx].get_text(separator=" ", strip=True)
    return ""


def _json_path(dept_key: str, kind: str) -> Path:
    return HERE / f"work_orders_{kind}_{dept_key}.json"


def _csv_path(dept_key: str, kind: str) -> Path:
    return HERE / f"work_orders_{kind}_{dept_key}.csv"


def _update_json(path: Path, assignments: dict[str, str]) -> int:
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        rows = json.load(f)
    changed = 0
    for r in rows:
        wo_id = str(r.get("wo_id") or "").strip()
        # Ensure the key always exists so downstream CSV generation has a
        # consistent schema across all rows.
        old_val = r.get("assigned_to", "")
        new_val = assignments.get(wo_id, old_val)
        if old_val != new_val:
            r["assigned_to"] = new_val
            changed += 1
        elif "assigned_to" not in r:
            r["assigned_to"] = ""
    if changed or any("assigned_to" not in r for r in rows):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)
    return changed


def _regen_csv(json_path: Path, csv_path: Path) -> bool:
    """Rewrite the CSV from the JSON using the historical column order plus
    the `assigned_to` field inserted right after `work_performed_by`."""
    if not json_path.exists():
        return False
    with open(json_path, encoding="utf-8") as f:
        rows = json.load(f)
    if not rows:
        return False

    # Preserve the legacy column order and inject assigned_to in a sensible spot.
    base_order = [
        "equipment_id", "equipment_eq_id", "equipment_name", "department",
        "wo_id", "date_notified", "urgency", "problem", "status",
        "material_cost", "labor_time", "work_performed_by", "assigned_to",
        "downtime_hours", "completed_datetime", "comments", "attachments",
    ]
    # If the JSON has audit_item (scheduled records) it goes before status.
    keys = set(rows[0].keys())
    for r in rows[1:]:
        keys.update(r.keys())
    ordered = [k for k in base_order if k in keys]
    ordered += [k for k in sorted(keys) if k not in ordered]

    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=ordered, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
    return True


def main():
    if not UNSCHEDULED_HTML.exists():
        print(f"[error] {UNSCHEDULED_HTML.name} not found")
        sys.exit(1)
    if not SCHEDULED_HTML.exists():
        print(f"[error] {SCHEDULED_HTML.name} not found")
        sys.exit(1)

    unsch_soup = BeautifulSoup(
        UNSCHEDULED_HTML.read_text(encoding="utf-8"), "html.parser"
    )
    sch_soup = BeautifulSoup(
        SCHEDULED_HTML.read_text(encoding="utf-8"), "html.parser"
    )

    # Build per-department assignment maps.
    unassigned: dict[str, dict[str, str]] = {k: {} for k in DEPT_KEY_BY_NAME.values()}
    for wo_id, (dept_name, owner) in _extract_unscheduled_owner_rows(unsch_soup).items():
        dept_key = DEPT_KEY_BY_NAME.get(dept_name)
        if dept_key:
            unassigned[dept_key][wo_id] = owner

    schassigned: dict[str, dict[str, str]] = {k: {} for k in DEPT_KEY_BY_NAME.values()}
    for wo_id, (dept_name, skill) in _extract_scheduled_skill_rows(sch_soup).items():
        dept_key = DEPT_KEY_BY_NAME.get(dept_name)
        if dept_key:
            schassigned[dept_key][wo_id] = skill

    total_changed = 0
    for dept_key in DEPT_KEY_BY_NAME.values():
        for kind, source in (("unscheduled", unassigned), ("scheduled", schassigned)):
            jpath = _json_path(dept_key, kind)
            cpath = _csv_path(dept_key, kind)
            changed = _update_json(jpath, source.get(dept_key, {}))
            if changed:
                _regen_csv(jpath, cpath)
                print(f"[{kind}] {dept_key}: updated {changed} records -> {jpath.name}")
                total_changed += changed

    print(f"\nTotal records enriched with assigned_to: {total_changed}")
    print("Run /api/reload in MINT (or restart the server) to load the new values.")


if __name__ == "__main__":
    main()
