"""Build webapp-ready vendor/utility contact data from the two source CSVs.

Reads the "Telephone List" and "Equipment" CSV exports (BLA Vendor & Utilities
List) and writes a clean, de-duplicated vendor_contacts.json that the server
serves to the "Vendor & Utilities Contacts" tab. Re-run this whenever the CSVs
change.
"""
import csv
import json
import re
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parent  # the MINT+PM workspace root, where the CSVs live
TELEPHONE_CSV = ROOT / "VENDOR AND UTILITIES LIST 2019_SEAN COPY 1(Telephone List Updated).csv"
EQUIPMENT_CSV = ROOT / "VENDOR AND UTILITIES LIST 2019_SEAN COPY 1(Equipment).csv"
VENDOR_PM_JSON = HERE / "vendor_pm_list.json"
OUT = HERE / "vendor_contacts.json"

# Equipment-CSV company -> matched Toilet Partitions machine eq_id(s). Used to
# tag rows that belong to a specific machine (their contacts are also copied
# onto that machine's contact card).
MACHINE_MATCHES = {
    "voorwood": ["2063"],
    "black brothers": ["1584"],
    "stiles machinery": ["1361", "5273", "1877"],
    "akins machinery": ["1879"],
    "raymond west": ["5351"],
    "joos usa": ["1724"],
    "the service group": ["1503"],
}


def _clean(s):
    if s is None:
        return ""
    return " ".join(str(s).replace("\xa0", " ").split()).strip()


def _multiline(s):
    """Collapse internal whitespace per line but keep line breaks (for the
    multi-line account-number cells in the telephone list)."""
    if s is None:
        return ""
    lines = [" ".join(ln.split()) for ln in str(s).replace("\xa0", " ").splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _digits(s):
    return re.sub(r"\D", "", s or "")


def _match_company(company):
    key = (company or "").lower()
    for needle, eqs in MACHINE_MATCHES.items():
        if needle in key:
            return eqs
    return []


def parse_telephone():
    rows = []
    with open(TELEPHONE_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for cells in reader:
            if not cells or not _clean(cells[0]):
                continue
            c0 = _clean(cells[0])
            if c0.upper() in ("BLA VENDORS LIST", "COMPANY NAME"):
                continue
            cells = (cells + [""] * 7)[:7]
            rows.append({
                "company": _clean(cells[0]),
                "contact": _multiline(cells[1]),
                "address": _multiline(cells[2]),
                "phone": _clean(cells[3]),
                "cell": _multiline(cells[4]),
                "fax": _clean(cells[5]),
                "email": _multiline(cells[6]),
                "type": "Facility",
                "service_contract": "",
                "contract_type": "",
                "source": "telephone",
                "machine_eq": [],
            })
    return rows


def parse_equipment():
    rows = []
    with open(EQUIPMENT_CSV, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for i, cells in enumerate(reader):
            if i == 0 or not cells or not _clean(cells[0]):
                continue
            cells = (cells + [""] * 10)[:10]
            company = _clean(cells[0])
            rows.append({
                "company": company,
                "contact": _multiline(cells[1]),
                "address": _multiline(cells[2]),
                "phone": _clean(cells[3]),
                "cell": _clean(cells[4]),
                "fax": _clean(cells[5]),
                "email": _multiline(cells[6]),
                "type": _clean(cells[7]) or "TPF",
                "service_contract": _clean(cells[8]),
                "contract_type": _clean(cells[9]),
                "source": "equipment",
                "machine_eq": _match_company(company),
            })
    return rows


def parse_vendor_pm():
    """Contacts extracted from the saved PME Vendor PM List HTML page."""
    rows = []
    if not VENDOR_PM_JSON.exists():
        return rows
    try:
        data = json.loads(VENDOR_PM_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return rows
    for v in data.get("vendors", []):
        company = _clean(v.get("company"))
        if not company:
            continue
        rows.append({
            "company": company,
            "contact": _multiline(v.get("contact")),
            "address": _multiline(v.get("address")),
            "phone": _clean(v.get("phone")),
            "cell": _clean(v.get("cell")),
            "fax": _clean(v.get("fax")),
            "email": _multiline(v.get("email")),
            "type": _clean(v.get("type")) or "TPF",
            "service_contract": _clean(v.get("service_contract")),
            "contract_type": _clean(v.get("contract_type")),
            "source": "vendor_pm_list",
            "machine_eq": [],
        })
    return rows


def main():
    vendors = parse_telephone() + parse_equipment() + parse_vendor_pm()
    # De-duplicate rows that describe the same company+contact+phone (the two
    # CSVs overlap on a few facility vendors). Prefer the richer equipment row.
    seen = {}
    for v in vendors:
        sig = (v["company"].lower(), v["contact"].lower(), _digits(v["phone"]), _digits(v["cell"]))
        if sig in seen:
            existing = seen[sig]
            if v["source"] == "equipment" and existing["source"] == "telephone":
                seen[sig] = v  # equipment row carries type/contract info
        else:
            seen[sig] = v
    deduped = list(seen.values())
    deduped.sort(key=lambda r: r["company"].lower())
    OUT.write_text(json.dumps({
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vendors": deduped,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    machine_rows = [v for v in deduped if v["machine_eq"]]
    print(f"Wrote {len(deduped)} vendor contacts to {OUT.name} "
          f"({len(vendors) - len(deduped)} duplicates merged).")
    print(f"Machine-matched rows: {len(machine_rows)}")
    for v in machine_rows:
        print(f"  {v['company']} / {v['contact'][:30]} -> EQ {', '.join(v['machine_eq'])}")


if __name__ == "__main__":
    main()
