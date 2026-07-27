"""Import PM equipment-summary dashboards into MINT machine_info.

Scrapes the local pages/equipment/<id>/dashboard.html files (captured from the
PM Equipment Dashboard -> Equipment Summary tab) for Toilet Partitions and
stores the parsed summary as JSON on each machine's info record.
"""

import json
import os
import re
from pathlib import Path
from typing import Optional

from bs4 import BeautifulSoup

import mint_store as store

HERE = Path(__file__).parent
EQ_PATH = HERE / "equipment_data.json"
PAGES_DIR = HERE / "pages" / "equipment"


def _norm(s: Optional[str]) -> str:
    if not s:
        return ""
    return " ".join(s.replace("\xa0", " ").replace("&nbsp;", " ").split())


def _multiline(el) -> str:
    if not el:
        return ""
    text = el.get_text("\n").replace("\xa0", " ").replace("&nbsp;", " ")
    lines = [line.strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _checked(el) -> bool:
    if not el:
        return False
    return bool(el.find("i", class_=lambda x: x and "fa-check" in x))


def parse_equipment_summary(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    def by_id(id_: str) -> str:
        return _norm(soup.find(id=id_).get_text()) if soup.find(id=id_) else ""

    summary = {
        "division": by_id("lblDivED"),
        "department": by_id("lblDeptED"),
        "equipment": by_id("lblEqpED"),
        "make": by_id("lblMakeED"),
        "model": by_id("lblModelED"),
        "serial_no": by_id("lblSerialED"),
        "asset_no": by_id("lblAssetNoED"),
        "function": by_id("lblFunED"),
        "po_no": by_id("lblPONoED"),
        "original_cost": by_id("lblOrigCostED"),
        "purchase_date": by_id("lblPurchDtED"),
        "warranty_end_date": by_id("lblWarrantyED"),
        "volts": by_id("lblVoltsED"),
        "amps": by_id("lblAmpsED"),
        "ph": by_id("lblPhED"),
        "branch_dust_collection": by_id("lblBranchDustED"),
        "replacement_name": by_id("lblReplNameED"),
        "replacement_year": by_id("lblReplYearED"),
        "replacement_cost": by_id("lblReplCostED"),
        "manufacturer": by_id("lblMfgED"),
        "distributor_a": by_id("lblDistAED"),
        "distributor_b": by_id("lblDistBED"),
        "distributor_c": by_id("lblDistCED"),
        "service_a": by_id("lblServAED"),
        "service_b": by_id("lblServBED"),
        "service_c": by_id("lblServCED"),
        "comment": _multiline(soup.find(id="lblCommentED")),
        "machine_settings": _multiline(soup.find(id="lblMachSetED")),
    }

    # Utility checkboxes: the label text ("Air:") is followed by a data label
    # containing a FontAwesome checkmark when the box is checked.
    util_names = ["Air", "Water", "Draine", "Gas", "Battery", "Propane", "Network"]
    utilities = {}
    for name in util_names:
        key = name.lower().replace(" ", "_")
        # labels may contain trailing colon/spaces
        label = soup.find(string=re.compile(rf"\b{re.escape(name)}\s*:?"))
        if label:
            parent = label.find_parent("label")
            if parent:
                sibling = parent.find_next_sibling("label")
                utilities[key] = _checked(sibling) if sibling else False
                continue
        utilities[key] = False
    summary["utilities"] = utilities

    # Critical / Obsolete status text (usually blank / yes)
    for label_text, key in [("Critical?", "critical"), ("Obsolete?", "obsolete")]:
        label = soup.find(string=re.compile(rf"\b{re.escape(label_text)}\s*:?"))
        val = ""
        if label:
            parent = label.find_parent("label")
            if parent:
                sibling = parent.find_next_sibling("label")
                val = _norm(sibling.get_text()) if sibling else ""
        summary[key] = val

    return summary


def _load_tpf_eq_ids() -> list[tuple[str, str]]:
    """Return (numeric_eq_id, equipment_name) for Toilet Partitions machines."""
    out = []
    if not EQ_PATH.exists():
        return out
    with open(EQ_PATH, encoding="utf-8") as f:
        records = json.load(f)
    name_to_key = {"Toilet Partitions": "toilet"}
    for r in records:
        dept = (r.get("dept") or "").strip()
        if name_to_key.get(dept) != "toilet":
            continue
        eq_id = (r.get("eq_id") or "").strip()
        m = re.search(r"\d+", eq_id)
        if m:
            out.append((m.group(), r.get("equipment_name") or ""))
    # Also include user-added TPF machines
    for m in store.list_machines():
        if m.get("dept_key") == "toilet":
            eq_id = (m.get("eq_id") or "").strip()
            numeric = re.search(r"\d+", eq_id)
            if numeric:
                out.append((numeric.group(), m.get("equipment_name") or ""))
    return out


def main():
    imported = 0
    skipped = 0
    rows = []

    for eq_id, name in _load_tpf_eq_ids():
        path = PAGES_DIR / eq_id / "dashboard.html"
        if not path.exists():
            skipped += 1
            rows.append({"eq_id": eq_id, "name": name, "status": "no dashboard.html"})
            continue
        try:
            summary = parse_equipment_summary(path.read_text(encoding="utf-8"))
            store.set_machine_info(
                "toilet",
                eq_id,
                {"summary_json": json.dumps(summary, ensure_ascii=False)},
                author="PM summary import",
            )
            imported += 1
            rows.append({"eq_id": eq_id, "name": name, "status": "imported"})
        except Exception as e:
            skipped += 1
            rows.append({"eq_id": eq_id, "name": name, "status": f"error: {e}"})

    print(f"Imported: {imported}")
    print(f"Skipped: {skipped}")
    for r in rows:
        if r["status"] != "imported":
            print(f"  EQ {r['eq_id']} ({r['name']}): {r['status']}")


if __name__ == "__main__":
    main()
