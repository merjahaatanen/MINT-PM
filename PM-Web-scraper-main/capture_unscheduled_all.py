"""
Explore + capture the DIVISION-WIDE Unscheduled Work Orders list
(/PME/Forms/WorkOrderUnshdAll).

WHY
---
The main scraper is equipment-centric: it only reads each machine's dashboard
grid. Work orders that are NOT attached to any equipment (facility / general
requests, e.g. WO 19878) never appear on an equipment dashboard, so they were
never scraped. Those orphan WOs only live on the division-wide list above.

This helper attaches to the debug Chrome (start_chrome_debug.bat), opens that
list, and:
  1. Prints every Kendo grid id on the page + its row count and field names,
     so we know the exact data shape.
  2. Dumps the grid's FULL dataSource (all rows, not just the visible/virtual
     ones) to  unscheduled_all_data.json  - Kendo keeps the whole dataset in
     memory, so this avoids fighting the virtual-scroll DOM.
  3. Mirrors the page HTML to  pages/unscheduled_all.html  for offline use.

Run (after start_chrome_debug.bat, logged in):
    python capture_unscheduled_all.py
Then send me the console output + unscheduled_all_data.json so I can wire the
records into the per-department / master files.
"""
import json
import os
import time

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager

BASE_URL = "https://circaweb.bobrick.com"
WOU_ALL = f"{BASE_URL}/PME/Forms/WorkOrderUnshdAll"
DEBUGGER_ADDRESS = os.environ.get("CHROME_DEBUG", "127.0.0.1:9222")
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
PAGES_DIR = os.path.join(OUTPUT_DIR, "pages")

# Pull every Kendo grid's dataSource on the page. Returns a JSON-serialisable
# dict: {gridId: {"total": N, "fields": [...], "rows": [...]}}. Reads the FULL
# dataset (dataSource.data()), which Kendo holds in memory regardless of the
# grid's virtual-scroll / paging state.
GRID_DUMP_JS = r"""
var out = {};
try {
  var jq = window.jQuery || window.$;
  document.querySelectorAll('[data-role=grid]').forEach(function (el) {
    var id = el.id || ('grid_' + Object.keys(out).length);
    try {
      var g = jq(el).data('kendoGrid');
      if (!g) { out[id] = {error: 'no kendoGrid widget'}; return; }
      var data = g.dataSource.data();
      var rows = [];
      for (var i = 0; i < data.length; i++) {
        var m = data[i].toJSON ? data[i].toJSON() : data[i];
        rows.push(m);
      }
      var fields = rows.length ? Object.keys(rows[0]) : [];
      out[id] = {total: g.dataSource.total(), count: rows.length,
                 fields: fields, rows: rows};
    } catch (e) { out[id] = {error: String(e)}; }
  });
} catch (e) { out['_error'] = String(e); }
return out;
"""


def main():
    opts = Options()
    opts.add_experimental_option("debuggerAddress", DEBUGGER_ADDRESS)
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()),
                              options=opts)
    print(f"Attached. Navigating to {WOU_ALL} ...")
    driver.get(WOU_ALL)

    # Wait for a Kendo grid to exist and finish its initial data bind.
    try:
        WebDriverWait(driver, 30).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "[data-role=grid]"))
        )
    except Exception:
        print("WARNING: no [data-role=grid] appeared within 30s.")
    # Give the dataSource time to load its first page/read.
    time.sleep(5)

    dump = driver.execute_script(GRID_DUMP_JS)

    print("\n=== Grids found ===")
    for gid, info in dump.items():
        if not isinstance(info, dict):
            continue
        if info.get("error"):
            print(f"  {gid}: ERROR {info['error']}")
            continue
        print(f"  {gid}: total={info.get('total')} loaded={info.get('count')}")
        print(f"     fields: {info.get('fields')}")
        rows = info.get("rows") or []
        if rows:
            print(f"     sample row: {json.dumps(rows[0])[:500]}")

    out_path = os.path.join(OUTPUT_DIR, "unscheduled_all_data.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2, default=str)
    print(f"\nSaved full grid dump -> {out_path}")

    # Mirror the page HTML too (best-effort).
    try:
        os.makedirs(PAGES_DIR, exist_ok=True)
        html = driver.execute_script(
            "return '<!DOCTYPE html>\\n' + document.documentElement.outerHTML;")
        with open(os.path.join(PAGES_DIR, "unscheduled_all.html"),
                  "w", encoding="utf-8") as f:
            f.write(html)
        print(f"Saved page HTML -> {os.path.join('pages', 'unscheduled_all.html')}")
    except Exception as e:
        print(f"(page snapshot failed: {e})")

    print("\nDone. Please send me the console output above + "
          "unscheduled_all_data.json.")


if __name__ == "__main__":
    main()
