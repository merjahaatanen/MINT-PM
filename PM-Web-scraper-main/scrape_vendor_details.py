#!/usr/bin/env python3
"""
scrape_vendor_details.py
========================
Scrapes the Bobrick PM 'VendorsAll' page and captures the rich tooltip that
appears when the green binocular icon is clicked. The tooltip contains phone,
fax, email, web page, full address, and contact person.

The script merges those details into:
  - vendor_pm_list.json   (raw source)
  - vendor_contacts.json  (regenerated via build_vendor_contacts.py)
  - mint_data/mint.db     (live contacts are updated)

SETUP (do once per session):
  1. Close all Chrome windows.
  2. Double-click start_chrome_debug.bat
  3. Log into the PM site if needed and navigate to Vendors.
  4. Run:  python scrape_vendor_details.py

QUICK TEST:
  python scrape_vendor_details.py --test-id 1

OFFLINE (parse an already-saved HTML page):
  python scrape_vendor_details.py --from-html "Vendor contacts PM"
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import TimeoutException, WebDriverException
from webdriver_manager.chrome import ChromeDriverManager

import chrome_session
import mint_store

HERE = Path(__file__).parent
VENDORS_URL = "https://circaweb.bobrick.com/PME/Forms/VendorsAll"
DEFAULT_TOOLTIP_URL = "/PME/Forms/GetVendorToolTip"
VENDOR_PM_LIST = HERE / "vendor_pm_list.json"
VENDOR_TOOLTIPS = HERE / "vendor_pm_tooltips.json"


def _clean(s: Any) -> str:
    if s is None:
        return ""
    return " ".join(str(s).replace("\xa0", " ").split()).strip()


def _normalize_company(name: str) -> str:
    """Strip punctuation/common suffixes so 'Donaldson Company Inc.' and
    'Donaldson Company Inc' match."""
    n = re.sub(r"[^\w\s&]", "", name.lower())
    n = re.sub(r"\s+", " ", n).strip()
    for suffix in ("inc", "llc", "ltd", "corp", "co", "company"):
        n = re.sub(rf"\b{suffix}\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _build_address(tip: dict) -> str:
    lines = [_clean(tip.get(f"Address {i}")) for i in (1, 2, 3)]
    lines = [ln for ln in lines if ln]
    csz = [_clean(tip.get(k)) for k in ("City", "State", "Zip")]
    csz = [p for p in csz if p]
    if csz:
        lines.append(" ".join(csz))
    return "\n".join(lines)


def _company_from_tip(tip: dict) -> str:
    name = _clean(tip.get("Name"))
    if name:
        return name
    wp = _clean(tip.get("Web Page"))
    if wp and not wp.startswith("http") and not wp.startswith("www."):
        return wp
    email = _clean(tip.get("Email"))
    if email and "@" in email:
        local = email.split("@")[0]
        return local.replace(".", " ").replace("_", " ").title()
    return ""


def _get_driver(port: int):
    options = Options()
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=options)


def _wait_for_grid(driver, timeout: int = 30):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_all_elements_located(
                (By.CSS_SELECTOR, "#gridVendors .k-grid-content tr[role='row']")
            )
        )
    except TimeoutException:
        # Some pages put rows inside a single scrollable table
        WebDriverWait(driver, timeout).until(
            EC.presence_of_all_elements_located(
                (By.CSS_SELECTOR, "#gridVendors tr[role='row']")
            )
        )


def _read_vendor_ids(driver) -> list[str]:
    ids = []
    for el in driver.find_elements(By.CSS_SELECTOR, "#gridVendors .vendtooltip"):
        vid = el.get_attribute("data-id")
        if vid:
            ids.append(vid)
    # Deduplicate while preserving order
    seen = set()
    out = []
    for vid in ids:
        if vid not in seen:
            seen.add(vid)
            out.append(vid)
    return out


def _fetch_tooltip_text(driver, vendor_id: str, tooltip_url: str = DEFAULT_TOOLTIP_URL,
                        method: str = "GET", param_name: str = "id") -> str:
    script = """
    var vendorId = arguments[0];
    var url = arguments[1];
    var method = arguments[2];
    var paramName = arguments[3];
    var callback = arguments[arguments.length - 1];
    var fullUrl = url + '?' + encodeURIComponent(paramName) + '=' + encodeURIComponent(vendorId);
    var opts = { credentials: 'same-origin' };
    var promise;
    if (method === 'POST') {
        opts.method = 'POST';
        opts.headers = { 'Content-Type': 'application/x-www-form-urlencoded' };
        opts.body = encodeURIComponent(paramName) + '=' + encodeURIComponent(vendorId);
        promise = fetch(url, opts);
    } else {
        promise = fetch(fullUrl, opts);
    }
    promise.then(function(r) { return r.text(); })
      .then(function(t) { callback(t); })
      .catch(function(e) { callback('ERROR:' + e); });
    """
    return driver.execute_async_script(script, vendor_id, tooltip_url, method, param_name)


def _parse_tooltip(text: str) -> dict[str, str]:
    text = text.strip()
    # Live endpoint returns JSON; saved pages contain an HTML tooltip table.
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {}
        key_map = {
            "CompanyName": "Name",
            "ContactPerson": "Contact Person",
            "Phone": "Phone",
            "Fax": "Fax",
            "Email": "Email",
            "WebPageURL": "Web Page",
            "Address1": "Address 1",
            "Address2": "Address 2",
            "Address3": "Address 3",
            "City": "City",
            "State": "State",
            "Zip": "Zip",
            "Remarks": "Remark",
        }
        out = {}
        for src, dst in key_map.items():
            val = data.get(src)
            if val is not None:
                out[dst] = _clean(val)
        return out

    soup = BeautifulSoup(text, "html.parser")
    table = soup.find("table", class_="ttptable")
    if not table:
        return {}
    out = {}
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        key = _clean(cells[0].get_text())
        val = _clean(cells[1].get_text())
        # strip trailing colon if any
        key = key.rstrip(":").strip()
        out[key] = val
    return out


def _scrape_online(args) -> list[dict]:
    port = chrome_session.find_running_port()
    if not port:
        print("ERROR: No logged-in debug Chrome found.")
        print("Run start_chrome_debug.bat, log into the PM site, then try again.")
        sys.exit(1)

    print(f"[chrome] attaching to debug port {port}")
    driver = _get_driver(port)
    try:
        print(f"[navigate] {VENDORS_URL}")
        driver.get(VENDORS_URL)
        _wait_for_grid(driver, timeout=args.timeout)
        time.sleep(1)  # let Kendo finish rendering

        vendor_ids = _read_vendor_ids(driver)
        print(f"[grid] found {len(vendor_ids)} vendor rows")
        if args.limit:
            vendor_ids = vendor_ids[:args.limit]

        if args.test_id:
            vendor_ids = [str(args.test_id)]

        tooltips = []
        for i, vid in enumerate(vendor_ids, 1):
            print(f"[{i}/{len(vendor_ids)}] fetching tooltip for vendor id {vid}")
            text = _fetch_tooltip_text(
                driver, vid, tooltip_url=args.tooltip_url,
                method=args.method, param_name=args.param_name,
            )
            if text.startswith("ERROR:") or not text.strip():
                if text.startswith("ERROR:"):
                    print(f"  GET failed ({text}), trying POST...")
                    text = _fetch_tooltip_text(
                        driver, vid, tooltip_url=args.tooltip_url,
                        method="POST", param_name=args.param_name,
                    )
                if text.startswith("ERROR:") or not text.strip():
                    print(f"  skipping vendor {vid}: {text}")
                    continue
            tip = _parse_tooltip(text)
            if not tip:
                print(f"  warning: no tooltip table for vendor {vid}")
                continue
            tip["_vendor_id"] = vid
            tooltips.append(tip)
            if args.test_id:
                print(json.dumps(tip, indent=2))
                return []
            time.sleep(0.2)
        return tooltips
    finally:
        driver.quit()


def _parse_saved_html(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(text, "html.parser")
    tooltips = []
    for div in soup.find_all("div", class_="k-tooltip-content"):
        tip = _parse_tooltip(str(div))
        if tip:
            tooltips.append(tip)
    return tooltips


def _update_vendor_pm_list(tooltips: list[dict]) -> int:
    if not VENDOR_PM_LIST.exists():
        print(f"[merge] {VENDOR_PM_LIST.name} not found, creating it")
        pm = {"vendors": []}
    else:
        pm = json.loads(VENDOR_PM_LIST.read_text(encoding="utf-8"))

    by_norm = {_normalize_company(v["company"]): v for v in pm["vendors"]}
    updated = 0
    added = 0
    for tip in tooltips:
        name = _company_from_tip(tip)
        if not name:
            continue
        key = _normalize_company(name)
        if key in by_norm:
            v = by_norm[key]
        else:
            v = {
                "company": name,
                "contact": "",
                "address": "",
                "phone": "",
                "cell": "",
                "fax": "",
                "email": "",
                "type": "TPF",
                "service_contract": "",
                "contract_type": "",
                "source": "vendor_pm_list",
                "machine_eq": [],
            }
            pm["vendors"].append(v)
            by_norm[key] = v
            added += 1

        # Fill/overwrite from tooltip
        v["contact"] = _clean(tip.get("Contact Person")) or v["contact"]
        v["phone"] = _clean(tip.get("Phone")) or v["phone"]
        v["fax"] = _clean(tip.get("Fax")) or v["fax"]
        v["email"] = _clean(tip.get("Email")) or v["email"]
        # If there's a web page but no email, store it in email so it survives into MINT
        if not v["email"] and _clean(tip.get("Web Page")):
            v["email"] = _clean(tip.get("Web Page"))
        addr = _build_address(tip)
        if addr:
            v["address"] = addr
        updated += 1

    pm["vendors"].sort(key=lambda r: r["company"].lower())
    VENDOR_PM_LIST.write_text(json.dumps(pm, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[merge] updated {updated} records, added {added} new records to {VENDOR_PM_LIST.name}")
    return updated


def _regenerate_vendor_contacts():
    print("[regenerate] running build_vendor_contacts.py ...")
    subprocess.run([sys.executable, str(HERE / "build_vendor_contacts.py")], check=True)


def _update_db(tooltips: list[dict]) -> int:
    existing = mint_store.list_vendor_contacts()
    existing_by_norm = {}
    for c in existing:
        norm = _normalize_company(c["company"])
        existing_by_norm.setdefault(norm, []).append(c)

    updated = 0
    for tip in tooltips:
        name = _company_from_tip(tip)
        if not name:
            continue
        tip_contact = _clean(tip.get("Contact Person"))
        norm = _normalize_company(name)
        matches = existing_by_norm.get(norm, [])
        if not matches:
            # Not in DB yet; add it now
            fields = {
                "company": name,
                "contact": tip_contact,
                "address": _build_address(tip),
                "phone": _clean(tip.get("Phone")),
                "cell": "",
                "fax": _clean(tip.get("Fax")),
                "email": _clean(tip.get("Email")) or _clean(tip.get("Web Page")),
                "type": "TPF",
                "service_contract": "",
                "contract_type": "",
                "machine_eq": [],
            }
            mint_store.add_vendor_contact(fields, author="Merja Haatanen")
            updated += 1
            continue

        for c in matches:
            fields = dict(c)
            fields["address"] = _build_address(tip) or c["address"]
            fields["phone"] = _clean(tip.get("Phone")) or c["phone"]
            fields["fax"] = _clean(tip.get("Fax")) or c["fax"]
            email = _clean(tip.get("Email")) or _clean(tip.get("Web Page")) or c["email"]
            fields["email"] = email
            # Update contact name only if it makes sense
            if tip_contact:
                if not c["contact"] or c["contact"].lower().strip() == tip_contact.lower().strip():
                    fields["contact"] = tip_contact
            mint_store.update_vendor_contact(c["id"], fields, author="Merja Haatanen")
            updated += 1
    return updated


def main():
    ap = argparse.ArgumentParser(description="Scrape vendor tooltip details from PM Vendors page")
    ap.add_argument("--from-html", type=Path, metavar="PATH",
                    help="Parse an already-saved HTML page instead of driving the browser")
    ap.add_argument("--output", type=Path, default=VENDOR_TOOLTIPS,
                    help="Where to write the raw tooltip JSON")
    ap.add_argument("--limit", type=int, default=0,
                    help="Only scrape the first N vendors (useful for testing)")
    ap.add_argument("--timeout", type=int, default=30,
                    help="Seconds to wait for the grid to load")
    ap.add_argument("--tooltip-url", default=DEFAULT_TOOLTIP_URL,
                    help="Relative URL of the tooltip endpoint")
    ap.add_argument("--method", default="GET", choices=["GET", "POST"],
                    help="HTTP method for the tooltip endpoint")
    ap.add_argument("--param-name", default="id",
                    help="Query/body parameter name for the vendor id")
    ap.add_argument("--test-id", type=int, default=0,
                    help="Fetch and print one tooltip, then exit")
    ap.add_argument("--no-update-db", action="store_true",
                    help="Skip updating the live SQLite database")
    args = ap.parse_args()

    if args.from_html:
        print(f"[offline] parsing {args.from_html}")
        tooltips = _parse_saved_html(args.from_html)
    else:
        tooltips = _scrape_online(args)

    if not tooltips and not args.test_id:
        print("No tooltips captured.")
        sys.exit(1)

    args.output.write_text(
        json.dumps({"vendors": tooltips}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[save] wrote {len(tooltips)} tooltips to {args.output}")

    _update_vendor_pm_list(tooltips)
    _regenerate_vendor_contacts()

    if not args.no_update_db:
        n = _update_db(tooltips)
        print(f"[db] updated/added {n} contact record(s)")

    print("done")


if __name__ == "__main__":
    main()
