#!/usr/bin/env python3
"""
import_vendors.py
==================
Reads a saved PM "Vendors" grid HTML page (a full-page save of
/PME/Forms/VendorsAll with the Division dropdown set to the target division)
and inserts each vendor row into MINT's vendor_contacts table, tagged with
the given division_key.

Generic replacement for the one-time import_bed_vendors.py script - works for
any division (bed, bmc, bwec, kkp, bgd, cit, ...).

The grid only exposes: Mfg/Dist/Serv checkboxes, Company Name, Address,
City, State, Contact Person, Modified date. Phone/email/fax aren't shown
in the list view (only in the per-vendor edit dialog / tooltip), so those
columns are left blank here; they can be filled in later from MINT's
Contacts tab, or backfilled via scrape_vendor_details.py.

Usage:
    python import_vendors.py "BWEC Vendors list" --division-key bwec
"""

import argparse
import sys
from pathlib import Path

from bs4 import BeautifulSoup

import mint_store as store

HERE = Path(__file__).parent


def _clean(s) -> str:
    return " ".join(str(s or "").split()).strip()


def _vendor_type(mfg: bool, dist: bool, serv: bool) -> str:
    parts = []
    if mfg:
        parts.append("Manufacturer")
    if dist:
        parts.append("Distributor")
    if serv:
        parts.append("Service Provider")
    return " / ".join(parts)


def _extract_vendor_rows(soup: BeautifulSoup, division_key: str) -> list[dict]:
    """Return vendor dicts from the #gridVendors Kendo grid. The grid is
    split into a locked column table (action buttons) and a scrollable
    content table (the real data) -- we only need the content table."""
    grid = soup.find(id="gridVendors")
    if not grid:
        print("[warn] #gridVendors not found in HTML")
        return []

    content = grid.find("div", class_="k-grid-content")
    if not content:
        print("[warn] .k-grid-content not found inside #gridVendors")
        return []

    out = []
    for row in content.find_all("tr", class_="k-master-row"):
        cells = row.find_all("td", role="gridcell")
        if len(cells) < 9:
            continue

        def has_check(td):
            return td.find("i", class_="fa-check") is not None

        mfg = has_check(cells[0])
        dist = has_check(cells[1])
        serv = has_check(cells[2])
        company = _clean(cells[3].get_text(separator=" ", strip=True))
        address = _clean(cells[4].get_text(separator=" ", strip=True))
        city = _clean(cells[5].get_text(separator=" ", strip=True))
        state = _clean(cells[6].get_text(separator=" ", strip=True))
        contact = _clean(cells[7].get_text(separator=" ", strip=True))

        if not company:
            continue

        full_address = ", ".join(p for p in (address, city, state) if p)

        out.append({
            "company": company,
            "contact": contact if contact.upper() not in ("N/A", "TBD", "ANYONE") else "",
            "address": full_address,
            "type": _vendor_type(mfg, dist, serv),
            "source": f"PM Vendors ({division_key.upper()})",
            "division_key": division_key,
        })
    return out


def main():
    ap = argparse.ArgumentParser(description="Import a saved PM Vendors grid page into MINT")
    ap.add_argument("html_path", type=Path, help="Path to the saved Vendors page HTML")
    ap.add_argument("--division-key", required=True,
                     help="Division these vendors belong to (e.g. bed, bmc, bwec, kkp, bgd, cit)")
    args = ap.parse_args()

    division_key = args.division_key.strip().lower()
    html_path = args.html_path
    if not html_path.exists():
        print(f"[error] {html_path} not found")
        sys.exit(1)

    soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
    rows = _extract_vendor_rows(soup, division_key)
    print(f"Parsed {len(rows)} vendor rows from {html_path.name}")

    existing = store.list_vendor_contacts()
    existing_names = {
        (v.get("company") or "").strip().lower()
        for v in existing
        if (v.get("division_key") or "bla") == division_key
    }

    added = 0
    skipped = 0
    for r in rows:
        if r["company"].strip().lower() in existing_names:
            skipped += 1
            continue
        try:
            store.add_vendor_contact(r, author="import_vendors")
            added += 1
        except ValueError as e:
            print(f"[skip] {r['company']}: {e}")
            skipped += 1

    print(f"Added {added} new {division_key.upper()} vendor contacts "
          f"(skipped {skipped} already present or invalid).")


if __name__ == "__main__":
    main()
