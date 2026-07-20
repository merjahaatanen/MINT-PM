"""
Helper: open an equipment dashboard, click the first "Edit Work Order" button
in the Unscheduled Work Orders tab, and dump the resulting dialog HTML to a file.

Run AFTER starting Chrome with start_chrome_debug.bat:
    python dump_edit_wo.py            # uses equipment 1877 by default
    python dump_edit_wo.py 5273       # use a specific equipment id
"""

import os
import sys
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "https://circaweb.bobrick.com"
DASH_URL = f"{BASE_URL}/PME/Forms/EquipmentDash"
DEBUGGER_ADDRESS = "127.0.0.1:9222"
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def main():
    eq_id = sys.argv[1] if len(sys.argv) > 1 else "1877"

    options = Options()
    options.add_experimental_option("debuggerAddress", DEBUGGER_ADDRESS)
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    print(f"Opening dashboard for equipment {eq_id} ...")
    driver.get(f"{DASH_URL}/{eq_id}")

    # Ensure the Unscheduled Work Orders tab is active
    WebDriverWait(driver, 15).until(EC.presence_of_element_located((By.ID, "EqpDash")))
    try:
        tab = driver.find_element(By.ID, "eqpdashuwo-tab")
        driver.execute_script("arguments[0].click();", tab)
    except Exception:
        pass

    # Wait for at least one unscheduled work-order row, then click its edit button
    print("Waiting for work order rows ...")
    WebDriverWait(driver, 15).until(
        EC.presence_of_element_located(
            (By.CSS_SELECTOR, "#gridWOU .k-grid-content tbody tr.k-master-row")
        )
    )
    first_row = driver.find_element(
        By.CSS_SELECTOR, "#gridWOU .k-grid-content tbody tr.k-master-row"
    )
    edit_btn = first_row.find_element(By.CSS_SELECTOR, "a.btn-info-bob")
    print(f"Clicking edit button: {edit_btn.get_attribute('onclick')}")
    driver.execute_script("arguments[0].click();", edit_btn)

    # Give the dialog time to open / load via AJAX
    time.sleep(3)

    # Try to capture just the dialog window; fall back to whole body
    html = None
    for sel in ["div.k-window", "#PMEFormWOU", ".k-window-content"]:
        try:
            elems = driver.find_elements(By.CSS_SELECTOR, sel)
            visible = [e for e in elems if e.is_displayed()]
            if visible:
                html = visible[-1].get_attribute("outerHTML")
                print(f"Captured dialog using selector: {sel}")
                break
        except Exception:
            continue

    if not html:
        print("Could not isolate dialog - dumping full page body instead.")
        html = driver.find_element(By.TAG_NAME, "body").get_attribute("outerHTML")

    out_path = os.path.join(OUTPUT_DIR, "edit_wo_dialog.html")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nSaved dialog HTML to: {out_path}")
    print(f"HTML length: {len(html):,} characters")


if __name__ == "__main__":
    main()
