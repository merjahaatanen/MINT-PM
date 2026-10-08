#!/usr/bin/env python3
"""
scrape_division_vendors.py
===========================
Launches a scraper-owned headless Chrome (reusing the login-profile machinery
from scraper.py), opens the PM 'VendorsAll' grid, switches the Division
dropdown to the requested division, and saves the full page HTML. The saved
file can then be fed straight into import_vendors.py.

Usage:
    python scrape_division_vendors.py kkp
    python import_vendors.py "KKP Vendors list.html" --division-key kkp
"""
import argparse
import os
import sys
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import scraper as S

VENDORS_URL = "https://circaweb.bobrick.com/PME/Forms/VendorsAll"


def main():
    ap = argparse.ArgumentParser(description="Save a division's Vendors grid page")
    ap.add_argument("division", help="Division code, e.g. kkp, bed, bmc, bwec")
    ap.add_argument("--refresh-profile", action="store_true",
                     help="Re-copy the logged-in profile before scraping")
    ap.add_argument("--out", default=None,
                     help="Output HTML path (default: '<DIVISION> Vendors list.html')")
    args = ap.parse_args()

    division = args.division.strip().lower()
    out_path = args.out or os.path.join(
        S.OUTPUT_DIR, f"{division.upper()} Vendors list.html")

    sc = S.WorkOrderScraper()
    sc.headless = True
    sc.refresh_profile = args.refresh_profile
    sc.division = division
    sc.launch_headless()
    try:
        print(f"Navigating to {VENDORS_URL} ...")
        sc.driver.get(VENDORS_URL)
        try:
            WebDriverWait(sc.driver, 20).until(
                EC.presence_of_element_located((By.ID, "gridVendors"))
            )
        except Exception:
            print("  [warn] #gridVendors did not appear in time")
        sc._select_division_in_ui()
        time.sleep(2)
        S._snapshot_full_page(sc.driver, out_path)
        print(f"Saved -> {out_path}")
    finally:
        try:
            sc.driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()
