"""
PM Equipment Work Order Scraper (Live Browser Control)
=======================================================
Attaches to an already-running Chrome browser (started with remote debugging)
that is logged into the Bobrick PM system.

For every equipment record it:
  1. Opens the Equipment Dashboard (/PME/Forms/EquipmentDash/<id>)
  2. Iterates every row in the Unscheduled Work Orders grid
  3. Clicks "Edit Work Order" for each row
  4. Captures the following from the dialog:
       Work Order tab: Date Notified, Urgency, Problem, Status,
                       Material Cost, Labor Time, Work Performed By,
                       Downtime Hours, Completed Date/Time
       Comment tab:    full comment log (all entries, with timestamps)
       Attachments tab: filenames of all attachments

It does the same for the Scheduled (preventive-maintenance) Work Orders grid.

Results saved to:
  - work_orders_unscheduled.csv / .json
  - work_orders_scheduled.csv   / .json

Every page visited is also mirrored to ./pages/ (form state baked in, scripts
stripped) so the whole scrape can be replayed offline with --from-html.

--------------------------------------------------------
SETUP (do this once each session):
  1. Close ALL Chrome windows.
  2. Double-click  start_chrome_debug.bat
  3. Log in if needed; stay on the Equipment All page.
  4. Run:  python scraper.py
     Or for a quick test:  python scraper.py --limit 3

OFFLINE REPLAY (no website / login needed):
     python scraper.py --from-html
  Re-runs the SAME extraction against the captured ./pages/ HTML using a
  private headless Chrome. Ideal for testing scraper changes.
--------------------------------------------------------
"""

import csv
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, StaleElementReferenceException
)
from webdriver_manager.chrome import ChromeDriverManager

# Make console output UTF-8 safe (equipment names / problems may contain
# characters the default Windows cp1252 console cannot encode).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


BASE_URL     = "https://circaweb.bobrick.com"
EQUIP_ALL    = f"{BASE_URL}/PME/Forms/EquipmentAll"
DASH_URL     = f"{BASE_URL}/PME/Forms/EquipmentDash"
# Division-wide Unscheduled Work Orders list. Unlike the per-equipment
# dashboards, this page also lists work orders with NO equipment attached
# (facility / general requests), which the equipment-centric scrape can never
# reach. We use it to capture those "orphan" work orders.
WOU_ALL      = f"{BASE_URL}/PME/Forms/WorkOrderUnshdAll"
DEBUGGER     = "127.0.0.1:9222"
OUTPUT_DIR   = os.path.dirname(os.path.abspath(__file__))
SAVE_EVERY   = 25      # checkpoint to disk every N equipment records

# Where captured page HTML is mirrored. The structure preserves the site's
# hierarchy so the scraper can be re-run entirely offline against these files:
#   pages/
#     equipment_all.html
#     equipment/<equipment_id>/
#       dashboard.html          (both UWO + SWO grids, populated)
#       meta.json               (equipment id / eq_id / dept / name)
#       unscheduled/<wo_id>.html (Edit Work Order dialog snapshot)
#       scheduled/<wo_id>.html
PAGES_DIR = os.path.join(OUTPUT_DIR, "pages")

# --------------------------------------------------------------------------- #
# Headless login profile
# --------------------------------------------------------------------------- #
# A headless Chrome cannot show a login page, so it must start from a Chrome
# profile that ALREADY holds a valid PM session. `start_chrome_debug.bat` logs
# into BASE_PROFILE; for a headless run we work off a COPY of it (a profile dir
# can't be shared by two running Chromes). This mirrors run_parallel.py.
_LOCALAPPDATA   = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
BASE_PROFILE    = os.path.join(_LOCALAPPDATA, "Google", "Chrome", "PM_Debug_Profile")
HEADLESS_PROFILE = os.path.join(_LOCALAPPDATA, "Google", "Chrome", "PM_Headless_Profile")
# Large, regenerable folders we skip when copying so the copy stays small/fast
# while still preserving the login cookies.
_PROFILE_IGNORE = shutil.ignore_patterns(
    "Cache", "Code Cache", "GPUCache", "GraphiteDawnCache", "ShaderCache",
    "Service Worker", "Crashpad", "component_crx_cache", "extensions_crx_cache",
    "GrShaderCache", "DawnGraphiteCache", "DawnWebGPUCache", "Default Cache",
    "*.log", "Singleton*",
)


def ensure_login_profile(dest: str = HEADLESS_PROFILE, refresh: bool = False) -> str:
    """Copy the logged-in BASE_PROFILE to `dest` so a headless Chrome starts
    already authenticated. Returns the profile path (or "" if BASE_PROFILE is
    missing / the copy fails, in which case the caller should fall back)."""
    if not os.path.isdir(BASE_PROFILE):
        print(f"  [headless] base login profile not found: {BASE_PROFILE}")
        print("  [headless] run start_chrome_debug.bat once and log in first.")
        return ""
    if os.path.isdir(dest) and refresh:
        shutil.rmtree(dest, ignore_errors=True)
    # Reuse an existing good copy - avoids recopying (and the lock problem)
    # on every run.
    if os.path.isdir(dest):
        return dest
    print(f"  [headless] copying login profile -> {os.path.basename(dest)} ...")
    try:
        shutil.copytree(BASE_PROFILE, dest, ignore=_PROFILE_IGNORE,
                        dirs_exist_ok=True)
    except Exception as e:
        # The Cookies DB (the actual login session) is locked while the source
        # Chrome is running, so the copy would be logged-OUT even if it partly
        # succeeded. Clean up the partial copy and tell the user how to fix it.
        msg = str(e)
        if "WinError 32" in msg or "used by another process" in msg:
            print("  [headless] CANNOT copy the login profile because a Chrome "
                  "using it is still open.")
            print("  [headless] Fix: CLOSE every Chrome window started by "
                  "start_chrome_debug.bat, then re-run with --refresh-profile.")
        else:
            print(f"  [headless] profile copy failed: {msg}")
        shutil.rmtree(dest, ignore_errors=True)
        return ""
    return dest

# The Work Order status checkboxes carry a `value` code that maps to a status.
# The checkbox `title` attribute is UNRELIABLE (e.g. the value="P" / "Pending"
# checkbox is titled ">Facility Service"), so we map by value instead and only
# fall back to the title for unknown codes.
STATUS_BY_CODE = {
    "CC":  "Closed and Completed",
    "CWA": "Closed Without Action",
    "P":   "Pending",
    "F":   "Facility Service",
    "O":   "Open",
    "N":   "New",
}

# Injected before serializing a page so the saved HTML faithfully represents the
# *rendered* state. Form controls hold their current value as a DOM property,
# not necessarily as an attribute (e.g. <input> value, checkbox checked,
# <select> selected), so a raw page_source would drop them. We copy each live
# property into its attribute so an offline reload reads identical data.
REFLECT_STATE_JS = r"""
try {
  document.querySelectorAll('input, textarea, select').forEach(function (el) {
    try {
      var tag = el.tagName.toUpperCase();
      if (tag === 'SELECT') {
        Array.prototype.forEach.call(el.options, function (o) {
          if (o.selected) { o.setAttribute('selected', 'selected'); }
          else { o.removeAttribute('selected'); }
        });
      } else if (el.type === 'checkbox' || el.type === 'radio') {
        if (el.checked) { el.setAttribute('checked', 'checked'); }
        else { el.removeAttribute('checked'); }
      } else if (tag === 'TEXTAREA') {
        el.textContent = (el.value == null ? '' : el.value);
      } else {
        el.setAttribute('value', el.value == null ? '' : el.value);
      }
    } catch (e) {}
  });
} catch (e) {}
"""

# Strip <script> blocks from saved HTML so an offline reload renders the
# captured DOM statically (the page's own JS would otherwise re-run, re-init
# the Kendo widgets and wipe the values we just captured). Our own injected
# execute_script calls still work because they are evaluated by the browser
# runtime, independent of page scripts.
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)

_DOC_TEMPLATE = (
    "<!DOCTYPE html>\n<html><head><meta charset=\"utf-8\">\n"
    "<title>{title}</title></head>\n<body>\n{body}\n</body></html>\n"
)


def _strip_scripts(html: str) -> str:
    return _SCRIPT_RE.sub("", html or "")


def _write_html(path: str, html: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """Strip HTML tags/entities from a grid cell value (e.g. the Comment column
    arrives as '<span ...>Name - date:</span>text')."""
    import html as _html
    s = _TAG_RE.sub("", s or "")
    return _html.unescape(s).replace("\xa0", " ").strip()


def _num(v) -> str:
    """Normalise a numeric grid value to a plain string ('' for empty)."""
    if v is None or v == "":
        return ""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (TypeError, ValueError):
        return str(v)


def _snapshot_full_page(driver, path: str):
    """Save the entire current document (scripts stripped) to `path`."""
    try:
        driver.execute_script(REFLECT_STATE_JS)
        html = driver.execute_script(
            "return '<!DOCTYPE html>\\n' + document.documentElement.outerHTML;"
        )
        _write_html(path, _strip_scripts(html))
    except Exception as e:
        print(f"    (snapshot failed for {os.path.basename(path)}: {e})")


def _snapshot_dialog(driver, path: str, title: str = "Work Order"):
    """Save just the open Work Order dialog window (scripts stripped) wrapped in
    a minimal HTML document, so it can be reloaded standalone offline."""
    try:
        driver.execute_script(REFLECT_STATE_JS)
        html = None
        for sel in ["div.k-window", "#WorkOrderWindow"]:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            visible = [e for e in els if e.is_displayed()]
            target = visible[-1] if visible else (els[-1] if els else None)
            if target is not None:
                html = target.get_attribute("outerHTML")
                if html:
                    break
        if not html:
            return
        doc = _DOC_TEMPLATE.format(title=title, body=_strip_scripts(html))
        _write_html(path, doc)
    except Exception as e:
        print(f"    (dialog snapshot failed for {os.path.basename(path)}: {e})")


def _file_url(path: str) -> str:
    """Convert a local path into a file:// URL Selenium can load."""
    return "file:///" + os.path.abspath(path).replace("\\", "/")


@dataclass
class WorkOrderDetail:
    # --- identifiers ---
    equipment_id:       str
    equipment_eq_id:    str
    equipment_name:     str
    department:         str
    wo_id:              str

    # --- Work Order tab ---
    date_notified:      str
    urgency:            str
    problem:            str
    status:             str
    material_cost:      str
    labor_time:         str
    work_performed_by:  str
    downtime_hours:     str
    completed_datetime: str

    # --- Comment tab ---
    comments:           str   # all entries joined; each prefixed with timestamp

    # --- Attachments tab ---
    attachments:        list = field(default_factory=list)  # [{"name", "url"}, ...]


@dataclass
class ScheduledWorkOrder:
    """A scheduled (preventive-maintenance) work order. These are triggered by
    a Maintenance Schedule and live in the dashboard's "Scheduled Work Orders"
    grid (#gridWOS). They reuse the same Edit dialog (WorkOrderWindow) as the
    unscheduled ones, but the key field is the Audit Item (the maintenance
    checklist) plus a Due Date rather than a free-text Problem/Urgency."""
    # --- identifiers ---
    equipment_id:       str
    equipment_eq_id:    str
    equipment_name:     str
    department:         str
    wo_id:              str

    # --- Work Order tab ---
    audit_item:         str   # the scheduled maintenance checklist text
    status:             str
    due_date:           str
    work_performed_by:  str
    labor_time:         str
    material_cost:      str
    downtime_hours:     str
    completed_datetime: str

    # --- Comment tab ---
    comments:           str

    # --- Attachments tab ---
    attachments:        list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------

def _safe_text(driver, css: str, attr: str = None) -> str:
    """Return text (or attribute) of the first matching element, or ''.

    For text mode, .text returns '' when the element isn't currently displayed
    (e.g. the dialog is still loading/animating), so we fall back to textContent
    which works regardless of visibility."""
    try:
        el = driver.find_element(By.CSS_SELECTOR, css)
        if attr:
            return (el.get_attribute(attr) or "").strip()
        txt = (el.text or "").strip()
        if not txt:
            txt = (el.get_attribute("textContent") or "").strip()
        return txt
    except (NoSuchElementException, StaleElementReferenceException):
        return ""


def _wait_for_dialog(driver, timeout: int = 10, require_visible: bool = True) -> bool:
    """Wait until the Work Order dialog exists.

    Live scraping waits for *visibility* (the dialog animates open). Offline
    replay loads a static snapshot where the dialog markup is present but the
    page's own show/hide JS never runs, so we only require *presence* there.
    """
    cond = (
        EC.visibility_of_element_located((By.ID, "WorkOrderWindow"))
        if require_visible
        else EC.presence_of_element_located((By.ID, "WorkOrderWindow"))
    )
    try:
        WebDriverWait(driver, timeout).until(cond)
        return True
    except TimeoutException:
        return False


def _close_dialog(driver):
    """Close the Work Order dialog."""
    try:
        btn = driver.find_element(By.ID, "btnCancelWorkOrder")
        driver.execute_script("arguments[0].click();", btn)
        time.sleep(0.4)
    except NoSuchElementException:
        pass


def _click_tab(driver, tab_id: str):
    """Click a tab inside the dialog by its element id."""
    try:
        tab = driver.find_element(By.ID, tab_id)
        driver.execute_script("arguments[0].click();", tab)
        time.sleep(0.3)
    except NoSuchElementException:
        pass


def _cell_text(cells, i: int) -> str:
    """Read a grid cell's text via textContent so it works even when the row
    is scrolled out of the Kendo grid's viewport. Selenium's .text returns ''
    for elements that aren't currently displayed, which silently dropped data
    for machines whose scheduled-WO grid was taller than the visible area."""
    if i < len(cells):
        try:
            return (cells[i].get_attribute("textContent") or "").strip()
        except StaleElementReferenceException:
            return ""
    return ""


# ---------------------------------------------------------------------------
# Dialog scraping
# ---------------------------------------------------------------------------

def _scrape_wo_tab(driver) -> dict:
    """Extract fields from the Work Order tab (read-only .old-uwo section)."""

    # Date Notified
    date_notified = _safe_text(driver, "#lblNotifiedDate")

    # Urgency - the displayed value is the .k-input span of the Kendo dropdown
    # widget that owns the ddlUrgencyWO2 listbox (a sibling of the hidden <select>).
    urgency = _safe_text(
        driver, "span.k-dropdown[aria-owns='ddlUrgencyWO2_listbox'] .k-input"
    )
    if not urgency or urgency.lower().startswith("select an"):
        # Fallback: read the selected option text from the hidden <select>
        try:
            urgency = driver.execute_script(
                "var s=document.getElementById('ddlUrgencyWO2');"
                "if(!s||s.selectedIndex<0) return '';"
                "return s.options[s.selectedIndex].text;"
            ) or ""
            urgency = urgency.strip()
        except Exception:
            urgency = ""
    if urgency.lower().startswith("select an"):
        urgency = ""

    # Problem - read-only label
    problem = _safe_text(driver, "#lblProblemWO")

    # Status - whichever StatusWO checkbox is checked. The status text is
    # derived from the checkbox `value` code (CC/CWA/P/...), NOT its `title`,
    # because the title is unreliable (e.g. the value="P"/"Pending" checkbox is
    # titled ">Facility Service"). Title is only a last-resort fallback for
    # unknown codes.
    status = ""
    try:
        checkboxes = driver.find_elements(
            By.CSS_SELECTOR, "input[name='StatusWO']"
        )
        for cb in checkboxes:
            checked = (
                cb.get_attribute("checked") in ("true", "checked")
                or cb.get_attribute("aria-checked") == "true"
            )
            if not checked:
                try:
                    checked = bool(driver.execute_script(
                        "return arguments[0].checked === true;", cb
                    ))
                except Exception:
                    checked = False
            if checked:
                code = (cb.get_attribute("value") or "").strip()
                title = (cb.get_attribute("title") or "").lstrip(">").strip()
                status = STATUS_BY_CODE.get(code, title)
                break
    except Exception:
        pass
    status = status.lstrip(">").strip()

    # Material Cost - hidden numerictextbox; value stored in aria-valuenow
    # The visible formatted input has aria-hidden=true, so use the hidden one's attribute
    material_cost = _safe_text(driver, "#tbMtrlCostWO", "aria-valuenow")

    # Labor Time
    labor_time = _safe_text(driver, "#tbLbrTimeWO", "aria-valuenow")

    # Work Performed By - a plain textbox input
    work_performed_by = _safe_text(driver, "#tbWorkPerformedByWO", "value")

    # Downtime Hours
    downtime = _safe_text(driver, "#tbDowntimeHours", "aria-valuenow")

    # Completed Date/Time
    completed = _safe_text(driver, "#dtCompletedWO", "value")

    return {
        "date_notified":      date_notified,
        "urgency":            urgency,
        "problem":            problem,
        "status":             status,
        "material_cost":      material_cost,
        "labor_time":         labor_time,
        "work_performed_by":  work_performed_by,
        "downtime_hours":     downtime,
        "completed_datetime": completed,
    }


def _scrape_comment_tab(driver) -> str:
    """Extract all comment log entries from the Comment tab."""
    _click_tab(driver, "wocomment-tab")
    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "divCommentsWO"))
        )
    except TimeoutException:
        pass

    entries = []
    try:
        rows = driver.find_elements(
            By.CSS_SELECTOR, "#divCommentsWO table tbody tr"
        )
        # Read via textContent (not .text) so entries are captured even when
        # the Comment tab pane is not the active/visible tab - e.g. during
        # offline replay where the tab-switching JS never runs.
        i = 0
        while i < len(rows):
            # Timestamp row: contains a <span class="pmpt-bob">
            ts_spans = rows[i].find_elements(By.CSS_SELECTOR, "span.pmpt-bob")
            if ts_spans:
                timestamp = (ts_spans[0].get_attribute("textContent") or "").strip()
                body = ""
                # Body is typically the FOLLOWING row, unless that row is itself
                # another timestamp (consecutive timestamps with empty bodies).
                if i + 1 < len(rows) and not rows[i + 1].find_elements(
                    By.CSS_SELECTOR, "span.pmpt-bob"
                ):
                    body = (rows[i + 1].get_attribute("textContent") or "").strip()
                    i += 2
                else:
                    i += 1
                entries.append(f"{timestamp} {body}".strip())
            else:
                # Standalone body row (some entries have body in same row)
                text = (rows[i].get_attribute("textContent") or "").strip()
                if text:
                    entries.append(text)
                i += 1
    except Exception:
        pass

    return " | ".join(entries)


def _scrape_attachments_tab(driver) -> list:
    """Return a list of {"name", "url"} dicts for the Attachments tab.

    The download URL (the link's resolved absolute href, e.g.
    https://circaweb.bobrick.com/PME/Forms/DownloadAttachment/88?guid=...) is
    captured so the web app can link straight to the file in the PM system.
    """
    _click_tab(driver, "woattach-tab")
    try:
        WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.ID, "divAttchmentsWorkOrder"))
        )
    except TimeoutException:
        pass

    # The attachment links (<a class="get-attach">) are rendered into the
    # container asynchronously after the tab is shown. The container itself
    # exists immediately (even when empty), so we must POLL for the links
    # rather than reading once - otherwise we capture zero attachments.
    links = []
    end = time.time() + 5
    while time.time() < end:
        links = driver.find_elements(
            By.CSS_SELECTOR, "#divAttchmentsWorkOrder a.get-attach"
        )
        if links:
            break
        time.sleep(0.3)

    attachments = []
    for link in links:
        try:
            # Filename is the text content after the <span> icon
            # Use JS to get just the text node content
            name = driver.execute_script(
                "return Array.from(arguments[0].childNodes)"
                ".filter(n => n.nodeType === 3)"
                ".map(n => n.textContent.trim())"
                ".join('')",
                link
            ).strip()
            if not name:
                name = link.text.strip()

            url = _attachment_url(link)
            if name or url:
                attachments.append({"name": name or url, "url": url})
        except (NoSuchElementException, StaleElementReferenceException):
            continue

    return attachments


def _attachment_url(link) -> str:
    """Return a fully-qualified PM download URL for an attachment <a>.

    `get_attribute("href")` normally returns the browser-resolved absolute URL,
    but in some captures it has come back blank or relative ("/PME/..."), which
    means the link does nothing when clicked from the web app. We therefore (1)
    absolutise any relative href against BASE_URL and (2) fall back to rebuilding
    the URL from the link's data-id / data-uid (guid) attributes, which always
    point to /PME/Forms/DownloadAttachment/<id>?guid=<uid>.
    """
    href = (link.get_attribute("href") or "").strip()
    if href.startswith("/"):
        href = BASE_URL + href
    if href.startswith("http"):
        return href

    data_id = (link.get_attribute("data-id") or "").strip()
    guid = (link.get_attribute("data-uid") or "").strip()
    if data_id:
        url = f"{BASE_URL}/PME/Forms/DownloadAttachment/{data_id}"
        if guid:
            url += f"?guid={guid}"
        return url
    return href


def scrape_wo_dialog(driver, wo_id: str, require_visible: bool = True) -> dict:
    """
    Given an open Work Order dialog, scrape all three tabs and return a dict.
    Leaves dialog open on failure; always tries to return to Work Order tab.
    """
    if not _wait_for_dialog(driver, require_visible=require_visible):
        return {}

    # Work Order tab is active by default — scrape it first
    _click_tab(driver, "wohome-tab")
    time.sleep(0.3)
    data = _scrape_wo_tab(driver)

    # Comment tab
    data["comments"] = _scrape_comment_tab(driver)

    # Attachments tab
    data["attachments"] = _scrape_attachments_tab(driver)

    return data


def scrape_swo_dialog(driver, wo_id: str, require_visible: bool = True) -> dict:
    """Scrape an open SCHEDULED Work Order dialog.

    Scheduled work orders reuse the same WorkOrderWindow dialog as unscheduled
    ones (tabs wohome / wocomment / woattach), so we reuse the shared tab
    scrapers and additionally capture the two scheduled-only read-only labels:
      - Audit Item  (#lblAuditItemSWO) - the maintenance checklist text
      - Due Date    (#lblDueDtSWO)
    These labels live on the home tab, so they must be read while it is active
    (Selenium .text returns '' for hidden elements).
    """
    if not _wait_for_dialog(driver, require_visible=require_visible):
        return {}

    _click_tab(driver, "wohome-tab")
    time.sleep(0.3)
    data = _scrape_wo_tab(driver)

    # Scheduled-only fields (read while the home tab is visible)
    data["audit_item"] = (
        _safe_text(driver, "#lblAuditItemSWO")
        or _safe_text(driver, "#lblAuditItemSWO", "textContent")
    )
    data["due_date"] = (
        _safe_text(driver, "#lblDueDtSWO")
        or _safe_text(driver, "#lblDueDtSWO", "textContent")
    )

    data["comments"] = _scrape_comment_tab(driver)
    data["attachments"] = _scrape_attachments_tab(driver)

    return data


# ---------------------------------------------------------------------------
# Main scraper class
# ---------------------------------------------------------------------------

class WorkOrderScraper:

    def __init__(self):
        self.driver = None
        self.records: List[WorkOrderDetail] = []
        self.scheduled_records: List[ScheduledWorkOrder] = []
        # When True, save() merges into the existing JSON/CSV instead of
        # overwriting it, so a partial scrape (single machine / department)
        # only replaces the work orders for the equipment ids it touched.
        self.partial = False
        self.scraped_ids: set = set()
        # Equipment ids whose grid never loaded this run (dashboard/grid never
        # settled after retries). Their existing data must be PRESERVED, not
        # overwritten with a false zero. Tracked per kind because a machine can
        # load one grid but not the other.
        self.failed_unscheduled_ids: set = set()
        self.failed_scheduled_ids: set = set()
        # When True, also open each scheduled WO's dialog to capture attachments
        # (slow). Core fields are always read from the grid regardless.
        self.swo_attachments = True
        # When True, skip unscheduled WOs entirely (only refresh the scheduled
        # grid data) and leave work_orders_unscheduled.* untouched.
        self.skip_unscheduled = False
        # When True, scrape ONLY the division-wide equipment-less ("orphan")
        # unscheduled work orders (facility / general requests) and merge them
        # into the per-department + master unscheduled files, leaving all
        # equipment-linked data untouched.
        self.orphans_only = False
        # When True, mirror every page the scraper visits to PAGES_DIR so the
        # whole scrape can later be replayed offline.
        self.capture_html = True
        self.pages_dir = PAGES_DIR
        # When True, read everything from previously captured HTML in
        # self.pages_dir instead of the live website.
        self.offline = False
        # When True (live scrape only), launch our OWN headless Chrome from a
        # logged-in profile copy instead of attaching to the user's debug
        # Chrome. Owning the browser avoids the 'invalid session id' crashes
        # that happen when a shared/attached Chrome tab is closed or navigated.
        self.headless = False
        # Optional explicit profile dir for the headless browser. When None a
        # copy of BASE_PROFILE (HEADLESS_PROFILE) is used.
        self.profile: Optional[str] = None
        self.refresh_profile = False
        # We launched our own (headless) browser and therefore own it; safe to
        # quit on finish. False when attached to the user's debug Chrome.
        self._owns_driver = False
        # debugger address to attach to (overridable for parallel instances,
        # each driving its own Chrome on a different port).
        self.debugger = DEBUGGER
        # When set (e.g. a department slug), output files are written as
        # work_orders_<kind>_<suffix>.json/.csv instead of the master names, so
        # parallel instances never write the same file. A merge step combines
        # them afterwards (see run_parallel.py).
        self.out_suffix = ""

    # ------------------------------------------------------------------
    # Capture-path helpers
    # ------------------------------------------------------------------
    def _equip_dir(self, eq_num) -> str:
        return os.path.join(self.pages_dir, "equipment", str(eq_num))

    def _write_meta(self, equipment: dict, equipment_name: str):
        if not self.capture_html:
            return
        eqdir = self._equip_dir(equipment["id"])
        os.makedirs(eqdir, exist_ok=True)
        meta = {
            "id":    str(equipment["id"]),
            "eq_id": equipment.get("eq_id", ""),
            "dept":  equipment.get("dept", ""),
            "name":  equipment_name or equipment.get("name", ""),
        }
        with open(os.path.join(eqdir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

    def attach(self):
        print(f"Attaching to Chrome at {self.debugger} ...")
        opts = Options()
        opts.add_experimental_option("debuggerAddress", self.debugger)
        svc = Service(ChromeDriverManager().install())
        try:
            self.driver = webdriver.Chrome(service=svc, options=opts)
        except Exception as e:
            print("\nERROR: Cannot attach to Chrome.")
            print("Run start_chrome_debug.bat first (close all other Chrome windows).\n")
            raise e
        print(f"Attached. Current URL: {self.driver.current_url}")

    def attach_offline(self):
        """Launch a private headless Chrome used only to load captured HTML
        files (file:// URLs). It never touches the live website, so it does not
        need the debug Chrome from start_chrome_debug.bat."""
        print("Launching headless Chrome for offline replay ...")
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--allow-file-access-from-files")
        svc = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=svc, options=opts)
        self._owns_driver = True
        print(f"Replaying captured pages from: {self.pages_dir}")

    def launch_headless(self):
        """Launch a scraper-OWNED headless Chrome against the LIVE site, started
        from a logged-in profile copy so it is already authenticated.

        Owning the browser (vs attaching to the shared debug Chrome) removes the
        'invalid session id' failures caused by a tab being closed/navigated out
        from under the driver, and uses far less RAM/GPU than a headful window.
        Falls back to attach() if no logged-in profile is available.
        """
        profile = self.profile or ensure_login_profile(refresh=self.refresh_profile)
        if not profile:
            print("  [headless] no logged-in profile available; "
                  "falling back to attaching to the debug Chrome.")
            self.attach()
            return
        print(f"Launching headless Chrome (profile: {os.path.basename(profile)}) ...")
        opts = Options()
        opts.add_argument("--headless=new")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--window-size=1920,1080")
        opts.add_argument(f"--user-data-dir={profile}")
        svc = Service(ChromeDriverManager().install())
        self.driver = webdriver.Chrome(service=svc, options=opts)
        self._owns_driver = True
        # Verify we actually landed on the app and not a login redirect.
        self.driver.get(EQUIP_ALL)
        try:
            WebDriverWait(self.driver, 20).until(
                lambda d: "EquipmentAll" in d.current_url or "Login" in d.current_url
                          or "login" in d.current_url.lower()
            )
        except TimeoutException:
            pass
        url = self.driver.current_url
        print(f"Headless Chrome ready. Current URL: {url}")
        if "login" in url.lower():
            print("  [headless] WARNING: redirected to a login page - the copied "
                  "profile's session has expired. Re-run start_chrome_debug.bat, "
                  "log in, then retry with --refresh-profile.")

    # ------------------------------------------------------------------
    def get_equipment_list(self) -> List[dict]:
        if self.offline:
            return self._equipment_list_offline()

        if "EquipmentAll" not in self.driver.current_url:
            print("Navigating to Equipment All ...")
            self.driver.get(EQUIP_ALL)
            time.sleep(3)

        ids = []
        try:
            WebDriverWait(self.driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "a.id-links"))
            )
            for link in self.driver.find_elements(By.CSS_SELECTOR, "a.id-links"):
                data_id = link.get_attribute("data-id")
                if data_id:
                    ids.append({"id": data_id, "eq_id": link.text.strip() or f"EQ ID {data_id}"})
            print(f"Found {len(ids)} equipment records on live page.")
            if self.capture_html:
                _snapshot_full_page(
                    self.driver, os.path.join(self.pages_dir, "equipment_all.html")
                )
        except TimeoutException:
            print("Could not read live page - falling back to equipment_data.csv")
            ids = self._ids_from_csv()

        seen, unique = set(), []
        for item in ids:
            if item["id"] not in seen:
                seen.add(item["id"])
                unique.append(item)
        return unique

    def _equipment_list_offline(self) -> List[dict]:
        """Build the equipment list from captured pages/equipment/<id>/meta.json."""
        base = os.path.join(self.pages_dir, "equipment")
        equipment = []
        if not os.path.isdir(base):
            print(f"No captured pages found at {base}. Run a live scrape first.")
            return []
        for name in sorted(os.listdir(base), key=lambda s: (len(s), s)):
            eqdir = os.path.join(base, name)
            if not os.path.isdir(eqdir):
                continue
            meta_path = os.path.join(eqdir, "meta.json")
            if os.path.exists(meta_path):
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
                equipment.append({
                    "id":    meta.get("id", name),
                    "eq_id": meta.get("eq_id", f"EQ ID {name}"),
                    "dept":  meta.get("dept", ""),
                    "name":  meta.get("name", ""),
                })
            else:
                equipment.append({"id": name, "eq_id": f"EQ ID {name}",
                                  "dept": "", "name": ""})
        print(f"Found {len(equipment)} equipment with captured pages.")
        return equipment

    def _ids_from_csv(self, department: Optional[str] = None) -> List[dict]:
        """Read equipment ids from equipment_data.csv, optionally filtered by department.

        If `department` is given, only rows whose `dept` column matches
        (case-insensitive) are returned.
        """
        path = os.path.join(OUTPUT_DIR, "equipment_data.csv")
        ids = []
        dept_filter = department.strip().lower() if department else None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    if dept_filter:
                        row_dept = (row.get("dept", "") or "").strip().lower()
                        if row_dept != dept_filter:
                            continue
                    eq_id = row.get("eq_id", "")
                    m = re.search(r"(\d+)", eq_id)
                    if m:
                        ids.append({
                            "id": m.group(1),
                            "eq_id": eq_id.strip(),
                            "dept": (row.get("dept", "") or "").strip(),
                            "name": (row.get("equipment_name", "") or "").strip(),
                        })
        return ids

    # ------------------------------------------------------------------
    # JS that returns the Kendo grid's authoritative record count, or -1 if the
    # widget isn't ready / jQuery-Kendo isn't available yet.
    _GRID_TOTAL_JS = (
        "try { var g = $(arguments[0]).data('kendoGrid');"
        " return (g && g.dataSource) ? g.dataSource.total() : -1; }"
        " catch (e) { return -1; }"
    )

    def _grid_total(self, grid_sel: str) -> int:
        """Authoritative row count straight from the Kendo grid widget."""
        try:
            t = self.driver.execute_script(self._GRID_TOTAL_JS, grid_sel)
            return int(t) if t is not None else -1
        except Exception:
            return -1

    def _load_grid_rows(self, grid_sel: str, timeout: int = 20):
        """Wait for a Kendo grid to finish loading and return (rows, confirmed_empty).

        These PM dashboards NEVER render a `.k-grid-norecords` placeholder for an
        empty grid - an empty grid just shows the header with no content rows,
        which is indistinguishable from a still-loading grid by DOM alone. That
        made every genuinely-empty machine burn the full timeout and get falsely
        flagged 'never loaded' (and every real retry wasted ~40s).

        The reliable signal is the Kendo widget's own `dataSource.total()`:
          * total > 0  -> wait for that many DOM rows to paint, then return them,
          * total == 0 -> confirmed empty (needs 2 stable reads to avoid a
                          pre-bind transient), so we stop immediately,
          * total == -1 (widget not ready / JS unavailable) -> fall back to the
                          old DOM row / `.k-grid-norecords` heuristic.
        Never trusts the grid while Kendo is actively (re)binding (loading mask).
        A timeout with no definitive state returns confirmed_empty=False so the
        caller can retry instead of recording a false zero.
        """
        row_sel   = f"{grid_sel} .k-grid-content tbody tr.k-master-row"
        norec_sel = f"{grid_sel} .k-grid-norecords"
        load_sel  = f"{grid_sel} .k-loading-mask, {grid_sel} .k-loading"
        end = time.time() + timeout
        empty_confirmations = 0
        while time.time() < end:
            # Do not trust the grid contents while Kendo is (re)binding.
            if self.driver.find_elements(By.CSS_SELECTOR, load_sel):
                empty_confirmations = 0
                time.sleep(0.3)
                continue

            total = self._grid_total(grid_sel)
            rows = self.driver.find_elements(By.CSS_SELECTOR, row_sel)

            if total > 0:
                # The widget knows there are records; return once the DOM rows
                # have painted (otherwise keep waiting for them).
                if rows:
                    return rows, False
                empty_confirmations = 0
            elif total == 0:
                # Authoritative empty. Require 2 stable reads so a pre-bind 0
                # (before the AJAX fetch runs) can't be mistaken for empty.
                empty_confirmations += 1
                if empty_confirmations >= 2:
                    return [], True
            else:
                # Widget not ready yet: fall back to the DOM heuristic.
                if rows:
                    return rows, False
                if self.driver.find_elements(By.CSS_SELECTOR, norec_sel):
                    empty_confirmations += 1
                    if empty_confirmations >= 3:
                        return [], True
                else:
                    empty_confirmations = 0
            time.sleep(0.3)
        # Timed out without a settled state. Return whatever is there, but flag
        # it as NOT a confirmed empty so the caller can retry the dashboard.
        rows = self.driver.find_elements(By.CSS_SELECTOR, row_sel)
        return rows, bool(rows)

    def _wait_for_wou_rows(self, timeout: int = 20):
        return self._load_grid_rows("#gridWOU", timeout)[0]

    def _wait_for_wos_rows(self, timeout: int = 20):
        return self._load_grid_rows("#gridWOS", timeout)[0]

    # ------------------------------------------------------------------
    def scrape_equipment(self, equipment: dict) -> List[WorkOrderDetail]:
        if self.offline:
            return self._scrape_equipment_offline(equipment)

        eq_num     = equipment["id"]
        eq_display = equipment["eq_id"]
        eq_dept    = equipment.get("dept", "")

        # Load the dashboard and read the unscheduled grid. If the grid never
        # settles into a definitive state (rows OR a persistent "no records"),
        # reload once before believing it is empty - a false zero here would be
        # written to the per-department file and permanently lose that machine's
        # work orders (the file is overwritten, not merged).
        equipment_name = ""
        rows, confirmed_empty = [], False
        for attempt in (1, 2):
            self.driver.get(f"{DASH_URL}/{eq_num}")
            try:
                WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.ID, "EqpDash"))
                )
            except TimeoutException:
                print(f"  [{eq_display}] dashboard timed out "
                      f"(try {attempt}) - retrying" if attempt == 1
                      else f"  [{eq_display}] dashboard timed out - skipping")
                continue

            # Activate Unscheduled Work Orders tab
            try:
                self.driver.execute_script(
                    "arguments[0].click();",
                    self.driver.find_element(By.ID, "eqpdashuwo-tab")
                )
            except NoSuchElementException:
                pass

            equipment_name = _safe_text(self.driver, "#lblEqpED")
            rows, confirmed_empty = self._load_grid_rows("#gridWOU")
            if rows or confirmed_empty:
                break
            if attempt == 1:
                print(f"  [{eq_display}] unscheduled grid did not settle - retrying")

        if not rows:
            if not confirmed_empty:
                print(f"  [{eq_display}] WARNING: unscheduled grid never loaded "
                      "- leaving prior data untouched")
                self.failed_unscheduled_ids.add(str(eq_num).strip())
                return []
            print(f"  [{eq_display}] 0 unscheduled work orders")
            return []

        # Collect wo_ids first (avoid stale refs from dialog opening/closing)
        wo_ids = []
        for row in rows:
            try:
                link = row.find_element(By.CSS_SELECTOR, "a.btn-info-bob")
                onclick = link.get_attribute("onclick") or ""
                m = re.search(r"\((\d+)", onclick)
                wo_ids.append(m.group(1) if m else "")
            except (NoSuchElementException, StaleElementReferenceException):
                wo_ids.append("")

        records = []
        for idx, wo_id in enumerate(wo_ids):
            if not wo_id:
                continue
            try:
                # Re-fetch row (DOM may have refreshed)
                current_rows = self._wait_for_wou_rows(timeout=5)
                if idx >= len(current_rows):
                    break
                row = current_rows[idx]

                # Read grid-level fields before opening dialog
                cells = row.find_elements(By.TAG_NAME, "td")
                grid_status  = cells[3].text.strip() if len(cells) > 3 else ""
                grid_wpb     = cells[4].text.strip() if len(cells) > 4 else ""
                grid_comment = cells[5].text.strip() if len(cells) > 5 else ""
                grid_labor   = cells[6].text.strip() if len(cells) > 6 else ""
                grid_matl    = cells[7].text.strip() if len(cells) > 7 else ""
                grid_down    = cells[8].text.strip() if len(cells) > 8 else ""
                grid_comp    = cells[9].text.strip() if len(cells) > 9 else ""

                # Click edit
                edit_btn = row.find_element(By.CSS_SELECTOR, "a.btn-info-bob")
                self.driver.execute_script("arguments[0].click();", edit_btn)

                # Scrape dialog
                dialog_data = scrape_wo_dialog(self.driver, wo_id)

                # Mirror the open dialog so it can be replayed offline.
                if self.capture_html and wo_id:
                    _snapshot_dialog(
                        self.driver,
                        os.path.join(self._equip_dir(eq_num),
                                     "unscheduled", f"{wo_id}.html"),
                        title=f"UWO {wo_id}",
                    )

                record = WorkOrderDetail(
                    equipment_id      = eq_num,
                    equipment_eq_id   = eq_display,
                    equipment_name    = equipment_name or equipment.get("name", ""),
                    department        = eq_dept,
                    wo_id             = wo_id,
                    date_notified     = dialog_data.get("date_notified", ""),
                    urgency           = dialog_data.get("urgency", ""),
                    problem           = dialog_data.get("problem", ""),
                    status            = dialog_data.get("status", "") or grid_status,
                    material_cost     = dialog_data.get("material_cost", "") or grid_matl,
                    labor_time        = dialog_data.get("labor_time", "") or grid_labor,
                    work_performed_by = dialog_data.get("work_performed_by", "") or grid_wpb,
                    downtime_hours    = dialog_data.get("downtime_hours", "") or grid_down,
                    completed_datetime= dialog_data.get("completed_datetime", "") or grid_comp,
                    comments          = dialog_data.get("comments", "") or grid_comment,
                    attachments       = dialog_data.get("attachments") or [],
                )
                records.append(record)

                _close_dialog(self.driver)
                time.sleep(0.3)

            except Exception as e:
                print(f"  [{eq_display}] WO {wo_id} error: {e}")
                _close_dialog(self.driver)
                continue

        print(f"  [{eq_display}] {equipment_name[:45]!r}: {len(records)} work orders scraped")
        return records

    # ------------------------------------------------------------------
    def scrape_scheduled(self, equipment: dict) -> List[ScheduledWorkOrder]:
        """Scrape every Scheduled Work Order (#gridWOS) for one machine.

        Captures ALL scheduled work orders regardless of status (Pending,
        in-progress, Closed & Completed). They share the WorkOrderWindow edit
        dialog with unscheduled WOs, plus an Audit Item and Due Date.
        """
        if self.offline:
            return self._scrape_scheduled_offline(equipment)

        eq_num     = equipment["id"]
        eq_display = equipment["eq_id"]
        eq_dept    = equipment.get("dept", "")

        # Reuse the dashboard if we're already on it (scrape_equipment just ran);
        # otherwise navigate to it. If the scheduled grid never settles, reload
        # once before believing it is empty (same false-zero hazard as the
        # unscheduled grid).
        equipment_name = ""
        rows, confirmed_empty = [], False
        for attempt in (1, 2):
            if f"{DASH_URL}/{eq_num}" not in self.driver.current_url or attempt == 2:
                self.driver.get(f"{DASH_URL}/{eq_num}")
                try:
                    WebDriverWait(self.driver, 15).until(
                        EC.presence_of_element_located((By.ID, "EqpDash"))
                    )
                except TimeoutException:
                    print(f"  [{eq_display}] dashboard timed out (scheduled) - "
                          f"{'retrying' if attempt == 1 else 'skipping'}")
                    continue

            # Activate Scheduled Work Orders tab
            try:
                self.driver.execute_script(
                    "arguments[0].click();",
                    self.driver.find_element(By.ID, "eqpdashswo-tab")
                )
            except NoSuchElementException:
                pass

            equipment_name = _safe_text(self.driver, "#lblEqpED")
            rows, confirmed_empty = self._load_grid_rows("#gridWOS")
            if rows or confirmed_empty:
                break
            if attempt == 1:
                print(f"  [{eq_display}] scheduled grid did not settle - retrying")

        # Mirror the whole dashboard (both UWO + SWO grids are now populated)
        # and record equipment metadata so an offline replay can find it.
        if self.capture_html:
            self._write_meta(equipment, equipment_name)
            _snapshot_full_page(
                self.driver,
                os.path.join(self._equip_dir(eq_num), "dashboard.html"),
            )

        if not rows:
            if not confirmed_empty:
                print(f"  [{eq_display}] WARNING: scheduled grid never loaded "
                      "- leaving prior data untouched")
                self.failed_scheduled_ids.add(str(eq_num).strip())
                return []
            print(f"  [{eq_display}] 0 scheduled work orders")
            return []

        # Collect wo_ids first (avoid stale refs from dialog opening/closing)
        wo_ids = []
        for row in rows:
            try:
                link = row.find_element(By.CSS_SELECTOR, "a.btn-info-bob")
                onclick = link.get_attribute("onclick") or ""
                m = re.search(r"\((\d+)", onclick)
                wo_ids.append(m.group(1) if m else "")
            except (NoSuchElementException, StaleElementReferenceException):
                wo_ids.append("")

        records = []
        for idx, wo_id in enumerate(wo_ids):
            if not wo_id:
                continue
            try:
                current_rows = self._wait_for_wos_rows(timeout=5)
                if idx >= len(current_rows):
                    break
                row = current_rows[idx]

                # Grid columns: 0 edit, 1 del, 2 EquipmentName, 3 AuditItem,
                # 4 Status, 5 DueDate, 6 WorkPerformedBy, 7 Comment,
                # 8 LaborTime, 9 MaterialCost, 10 DownTime, 11 CompletedDateTime
                # Read all core fields straight from the grid. textContent is
                # used (via _cell_text) so rows scrolled out of the Kendo grid
                # viewport still yield their text - the shared edit dialog uses
                # different field ids for scheduled WOs and returns nothing.
                cells = row.find_elements(By.TAG_NAME, "td")
                grid_audit  = _cell_text(cells, 3)
                grid_status = _cell_text(cells, 4)
                grid_due    = _cell_text(cells, 5)
                grid_wpb    = _cell_text(cells, 6)
                grid_comment= _cell_text(cells, 7)
                grid_labor  = _cell_text(cells, 8)
                grid_matl   = _cell_text(cells, 9)
                grid_down   = _cell_text(cells, 10)
                grid_comp   = _cell_text(cells, 11)

                # Attachments only live in the edit dialog, so open it only when
                # requested (it is the slow part of the scrape).
                dialog_data = {}
                if self.swo_attachments:
                    try:
                        edit_btn = row.find_element(By.CSS_SELECTOR, "a.btn-info-bob")
                        self.driver.execute_script("arguments[0].click();", edit_btn)
                        dialog_data = scrape_swo_dialog(self.driver, wo_id)
                        if self.capture_html and wo_id:
                            _snapshot_dialog(
                                self.driver,
                                os.path.join(self._equip_dir(eq_num),
                                             "scheduled", f"{wo_id}.html"),
                                title=f"SWO {wo_id}",
                            )
                    except Exception:
                        pass
                    finally:
                        _close_dialog(self.driver)
                        time.sleep(0.2)

                record = ScheduledWorkOrder(
                    equipment_id      = eq_num,
                    equipment_eq_id   = eq_display,
                    equipment_name    = equipment_name or equipment.get("name", ""),
                    department        = eq_dept,
                    wo_id             = wo_id,
                    audit_item        = grid_audit or dialog_data.get("audit_item", ""),
                    status            = grid_status or dialog_data.get("status", ""),
                    due_date          = grid_due or dialog_data.get("due_date", ""),
                    work_performed_by = grid_wpb or dialog_data.get("work_performed_by", ""),
                    labor_time        = grid_labor or dialog_data.get("labor_time", ""),
                    material_cost     = grid_matl or dialog_data.get("material_cost", ""),
                    downtime_hours    = grid_down or dialog_data.get("downtime_hours", ""),
                    completed_datetime= grid_comp or dialog_data.get("completed_datetime", ""),
                    comments          = dialog_data.get("comments", "") or grid_comment,
                    attachments       = dialog_data.get("attachments") or [],
                )
                records.append(record)

            except Exception as e:
                print(f"  [{eq_display}] SWO {wo_id} error: {e}")
                _close_dialog(self.driver)
                continue

        print(f"  [{eq_display}] {equipment_name[:45]!r}: {len(records)} scheduled work orders scraped")
        return records

    # ------------------------------------------------------------------
    # Division-wide "orphan" unscheduled work orders (no equipment attached)
    # ------------------------------------------------------------------
    # Read the whole #gridWOU dataSource on the WorkOrderUnshdAll page. Kendo
    # keeps the full dataset in memory, so this returns every row regardless of
    # the grid's virtual-scroll state (avoids scraping a virtualised DOM).
    _WOU_ALL_ROWS_JS = r"""
    var jq = window.jQuery || window.$;
    var el = document.getElementById('gridWOU');
    if (!el) return [];
    var g = jq(el).data('kendoGrid');
    if (!g) return [];
    var data = g.dataSource.data(), out = [];
    for (var i = 0; i < data.length; i++) {
      var m = data[i].toJSON ? data[i].toJSON() : data[i];
      out.push(m);
    }
    return out;
    """

    def scrape_orphan_unscheduled(self) -> List[WorkOrderDetail]:
        """Scrape unscheduled work orders that have NO equipment attached
        (facility / general requests) from the division-wide WorkOrderUnshdAll
        list. Equipment-linked WOs are already captured per-equipment, so only
        rows with a blank EquipmentName are kept. Each orphan's Problem text and
        owning Department come from its Edit dialog."""
        if self.offline:
            return self._scrape_orphan_unscheduled_offline()

        self.driver.get(WOU_ALL)
        try:
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "[data-role=grid]"))
            )
        except TimeoutException:
            print("  [orphans] WorkOrderUnshdAll grid never appeared - skipping")
            return []
        # Let the Kendo dataSource finish its initial read.
        time.sleep(3)
        for _ in range(20):
            rows = self.driver.execute_script(self._WOU_ALL_ROWS_JS) or []
            if rows:
                break
            time.sleep(0.5)

        if self.capture_html:
            _snapshot_full_page(
                self.driver, os.path.join(self.pages_dir, "unscheduled_all.html")
            )

        orphans = [r for r in rows if not str(r.get("EquipmentName") or "").strip()]
        print(f"  [orphans] {len(rows)} division WOs, "
              f"{len(orphans)} with no equipment attached")

        records = []
        for r in orphans:
            wo_id = str(r.get("ID") or "").strip()
            if not wo_id:
                continue
            dialog_data = {}
            department = ""
            try:
                self.driver.execute_script(
                    f"WorkOrderUnshdMgr.EditWorkOrderUnschd({wo_id});"
                )
                dialog_data = scrape_wo_dialog(self.driver, wo_id)
                department = (_safe_text(self.driver, "#lblDepartmentWO")
                              or _safe_text(self.driver, "#lblDepartmentWO", "textContent"))
                if self.capture_html:
                    _snapshot_dialog(
                        self.driver,
                        os.path.join(self.pages_dir, "unscheduled_all",
                                     f"{wo_id}.html"),
                        title=f"UWO {wo_id}",
                    )
            except Exception as e:
                print(f"  [orphans] WO {wo_id} error: {e}")
            finally:
                _close_dialog(self.driver)
                time.sleep(0.2)

            records.append(WorkOrderDetail(
                equipment_id      = "",
                equipment_eq_id   = "",
                equipment_name    = "",
                department        = department.strip(),
                wo_id             = wo_id,
                date_notified     = dialog_data.get("date_notified", ""),
                urgency           = dialog_data.get("urgency", ""),
                problem           = dialog_data.get("problem", ""),
                status            = dialog_data.get("status", "") or (r.get("WOStatusDsc") or ""),
                material_cost     = dialog_data.get("material_cost", "") or _num(r.get("MaterialCost")),
                labor_time        = dialog_data.get("labor_time", "") or _num(r.get("LaborTime")),
                work_performed_by = dialog_data.get("work_performed_by", "") or (r.get("WorkPerformedBy") or ""),
                downtime_hours    = dialog_data.get("downtime_hours", "") or _num(r.get("DownTime")),
                completed_datetime= dialog_data.get("completed_datetime", "") or (r.get("CompletedDateTime") or ""),
                comments          = dialog_data.get("comments", "") or _strip_html(r.get("Comment") or ""),
                attachments       = dialog_data.get("attachments") or [],
            ))

        print(f"  [orphans] scraped {len(records)} equipment-less work orders")
        return records

    # ------------------------------------------------------------------
    # Offline replay (reads from captured HTML instead of the live site)
    # ------------------------------------------------------------------
    def _load_file(self, path: str) -> bool:
        """Load a captured HTML file as a file:// page. Returns False if absent."""
        if not os.path.exists(path):
            return False
        self.driver.get(_file_url(path))
        return True

    def _grid_wo_ids(self, grid_sel: str):
        """Return [(wo_id, [cell elements]), ...] for the rows of a captured grid."""
        out = []
        rows = self.driver.find_elements(
            By.CSS_SELECTOR, f"{grid_sel} .k-grid-content tbody tr.k-master-row"
        )
        if not rows:
            # Some captures keep rows directly under the grid table tbody.
            rows = self.driver.find_elements(
                By.CSS_SELECTOR, f"{grid_sel} tbody tr.k-master-row"
            )
        for row in rows:
            wo_id = ""
            try:
                link = row.find_element(By.CSS_SELECTOR, "a.btn-info-bob")
                m = re.search(r"\((\d+)", link.get_attribute("onclick") or "")
                wo_id = m.group(1) if m else ""
            except (NoSuchElementException, StaleElementReferenceException):
                pass
            out.append((wo_id, row.find_elements(By.TAG_NAME, "td")))
        return out

    def _scrape_equipment_offline(self, equipment: dict) -> List[WorkOrderDetail]:
        eq_num     = equipment["id"]
        eq_display = equipment["eq_id"]
        eq_dept    = equipment.get("dept", "")
        eqdir      = self._equip_dir(eq_num)

        if not self._load_file(os.path.join(eqdir, "dashboard.html")):
            print(f"  [{eq_display}] no captured dashboard - skipping")
            return []

        equipment_name = _safe_text(self.driver, "#lblEqpED") or equipment.get("name", "")

        # Snapshot grid-level fields first; loading a dialog file replaces the page.
        grid = []
        for wo_id, cells in self._grid_wo_ids("#gridWOU"):
            grid.append((wo_id, {
                "status":  _cell_text(cells, 3),
                "wpb":     _cell_text(cells, 4),
                "comment": _cell_text(cells, 5),
                "labor":   _cell_text(cells, 6),
                "matl":    _cell_text(cells, 7),
                "down":    _cell_text(cells, 8),
                "comp":    _cell_text(cells, 9),
            }))

        records = []
        for wo_id, g in grid:
            if not wo_id:
                continue
            dialog_data = {}
            if self._load_file(os.path.join(eqdir, "unscheduled", f"{wo_id}.html")):
                dialog_data = scrape_wo_dialog(self.driver, wo_id, require_visible=False)
            records.append(WorkOrderDetail(
                equipment_id      = eq_num,
                equipment_eq_id   = eq_display,
                equipment_name    = equipment_name,
                department        = eq_dept,
                wo_id             = wo_id,
                date_notified     = dialog_data.get("date_notified", ""),
                urgency           = dialog_data.get("urgency", ""),
                problem           = dialog_data.get("problem", ""),
                status            = dialog_data.get("status", "") or g["status"],
                material_cost     = dialog_data.get("material_cost", "") or g["matl"],
                labor_time        = dialog_data.get("labor_time", "") or g["labor"],
                work_performed_by = dialog_data.get("work_performed_by", "") or g["wpb"],
                downtime_hours    = dialog_data.get("downtime_hours", "") or g["down"],
                completed_datetime= dialog_data.get("completed_datetime", "") or g["comp"],
                comments          = dialog_data.get("comments", "") or g["comment"],
                attachments       = dialog_data.get("attachments") or [],
            ))

        print(f"  [{eq_display}] {equipment_name[:45]!r}: {len(records)} work orders (offline)")
        return records

    def _scrape_scheduled_offline(self, equipment: dict) -> List[ScheduledWorkOrder]:
        eq_num     = equipment["id"]
        eq_display = equipment["eq_id"]
        eq_dept    = equipment.get("dept", "")
        eqdir      = self._equip_dir(eq_num)

        if not self._load_file(os.path.join(eqdir, "dashboard.html")):
            return []

        equipment_name = _safe_text(self.driver, "#lblEqpED") or equipment.get("name", "")

        grid = []
        for wo_id, cells in self._grid_wo_ids("#gridWOS"):
            grid.append((wo_id, {
                "audit":   _cell_text(cells, 3),
                "status":  _cell_text(cells, 4),
                "due":     _cell_text(cells, 5),
                "wpb":     _cell_text(cells, 6),
                "comment": _cell_text(cells, 7),
                "labor":   _cell_text(cells, 8),
                "matl":    _cell_text(cells, 9),
                "down":    _cell_text(cells, 10),
                "comp":    _cell_text(cells, 11),
            }))

        records = []
        for wo_id, g in grid:
            if not wo_id:
                continue
            dialog_data = {}
            if self._load_file(os.path.join(eqdir, "scheduled", f"{wo_id}.html")):
                dialog_data = scrape_swo_dialog(self.driver, wo_id, require_visible=False)
            records.append(ScheduledWorkOrder(
                equipment_id      = eq_num,
                equipment_eq_id   = eq_display,
                equipment_name    = equipment_name,
                department        = eq_dept,
                wo_id             = wo_id,
                audit_item        = g["audit"] or dialog_data.get("audit_item", ""),
                status            = g["status"] or dialog_data.get("status", ""),
                due_date          = g["due"] or dialog_data.get("due_date", ""),
                work_performed_by = g["wpb"] or dialog_data.get("work_performed_by", ""),
                labor_time        = g["labor"] or dialog_data.get("labor_time", ""),
                material_cost     = g["matl"] or dialog_data.get("material_cost", ""),
                downtime_hours    = g["down"] or dialog_data.get("downtime_hours", ""),
                completed_datetime= g["comp"] or dialog_data.get("completed_datetime", ""),
                comments          = dialog_data.get("comments", "") or g["comment"],
                attachments       = dialog_data.get("attachments") or [],
            ))

        print(f"  [{eq_display}] {equipment_name[:45]!r}: {len(records)} scheduled work orders (offline)")
        return records

    def _scrape_orphan_unscheduled_offline(self) -> List[WorkOrderDetail]:
        """Replay equipment-less work orders from the captured dialog snapshots
        in pages/unscheduled_all/<wo_id>.html."""
        odir = os.path.join(self.pages_dir, "unscheduled_all")
        if not os.path.isdir(odir):
            print("  [orphans] no captured unscheduled_all dialogs - skipping")
            return []
        records = []
        for fn in sorted(os.listdir(odir)):
            if not fn.endswith(".html"):
                continue
            wo_id = fn[:-5]
            if not self._load_file(os.path.join(odir, fn)):
                continue
            dialog_data = scrape_wo_dialog(self.driver, wo_id, require_visible=False)
            department = (_safe_text(self.driver, "#lblDepartmentWO")
                          or _safe_text(self.driver, "#lblDepartmentWO", "textContent"))
            records.append(WorkOrderDetail(
                equipment_id      = "",
                equipment_eq_id   = "",
                equipment_name    = "",
                department        = department.strip(),
                wo_id             = wo_id,
                date_notified     = dialog_data.get("date_notified", ""),
                urgency           = dialog_data.get("urgency", ""),
                problem           = dialog_data.get("problem", ""),
                status            = dialog_data.get("status", ""),
                material_cost     = dialog_data.get("material_cost", ""),
                labor_time        = dialog_data.get("labor_time", ""),
                work_performed_by = dialog_data.get("work_performed_by", ""),
                downtime_hours    = dialog_data.get("downtime_hours", ""),
                completed_datetime= dialog_data.get("completed_datetime", ""),
                comments          = dialog_data.get("comments", ""),
                attachments       = dialog_data.get("attachments") or [],
            ))
        print(f"  [orphans] {len(records)} equipment-less work orders (offline)")
        return records

    # ------------------------------------------------------------------
    def _persist(self, records, dataclass_type, json_name, csv_name, label,
                 failed_ids: Optional[set] = None):
        """Write records to JSON + CSV, merging on equipment_id when partial.

        `failed_ids` are equipment whose grid never loaded this run; their
        existing records are ALWAYS carried over (never overwritten with a false
        zero), even in overwrite mode, so a transient load failure can't wipe a
        machine's data.
        """
        csv_path  = os.path.join(OUTPUT_DIR, csv_name)
        json_path = os.path.join(OUTPUT_DIR, json_name)
        failed_ids = {str(i).strip() for i in (failed_ids or set())}

        fields = [f for f in dataclass_type.__dataclass_fields__]
        rows = [asdict(r) for r in records]

        def _read_existing():
            if os.path.exists(json_path):
                try:
                    with open(json_path, encoding="utf-8") as f:
                        return json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"  WARNING: could not read existing {json_path} ({e}).")
            return []

        if self.partial and self.scraped_ids:
            # Merge: keep every existing record EXCEPT the equipment ids we
            # just re-scraped (but DO keep ids that failed to load), then append
            # the freshly scraped records. Preserves other machines' data on a
            # single-machine scrape.
            existing = _read_existing()
            remove = self.scraped_ids - failed_ids
            kept = [r for r in existing
                    if str(r.get("equipment_id", "")).strip() not in remove]
            rows = kept + rows
            print(f"Merging {label}: kept {len(kept)} existing records, "
                  f"updated {len(records)} for {sorted(remove)}"
                  + (f", preserved failed {sorted(failed_ids & self.scraped_ids)}"
                     if failed_ids & self.scraped_ids else ""))
        elif failed_ids:
            # Overwrite mode (e.g. per-department parallel run): carry over the
            # prior records of any machine whose grid failed to load this run so
            # a transient failure doesn't drop it from the fresh file.
            have = {str(r.get("equipment_id", "")).strip() for r in rows}
            carried = [r for r in _read_existing()
                       if str(r.get("equipment_id", "")).strip() in failed_ids
                       and str(r.get("equipment_id", "")).strip() not in have]
            if carried:
                rows = rows + carried
                print(f"Preserved {len(carried)} {label} records for machines "
                      f"that failed to load: {sorted(failed_ids)}")

        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fields)
            writer.writeheader()
            for r in rows:
                writer.writerow({
                    k: (json.dumps(r.get(k)) if isinstance(r.get(k), (list, dict))
                        else r.get(k, ""))
                    for k in fields
                })

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)

        print(f"Saved {len(rows)} {label} records  ->  {csv_path}")

    def _out_name(self, base: str, ext: str) -> str:
        return f"{base}_{self.out_suffix}.{ext}" if self.out_suffix else f"{base}.{ext}"

    def save(self):
        if not self.skip_unscheduled:
            self._persist(self.records, WorkOrderDetail,
                          self._out_name("work_orders_unscheduled", "json"),
                          self._out_name("work_orders_unscheduled", "csv"),
                          "unscheduled", self.failed_unscheduled_ids)
        self._persist(self.scheduled_records, ScheduledWorkOrder,
                      self._out_name("work_orders_scheduled", "json"),
                      self._out_name("work_orders_scheduled", "csv"),
                      "scheduled", self.failed_scheduled_ids)

    def save_orphans(self, records: List[WorkOrderDetail]):
        """Route equipment-less work orders into the per-department unscheduled
        files (by their dialog Department) + the master, deduping by wo_id.

        These have no equipment_id, so the equipment-keyed merge in _persist
        does not apply. Every department file is rewritten to drop any stale
        copy of the scraped wo_ids first (so a WO can't linger in the wrong
        department or be duplicated), then this run's orphans are appended to
        their department's file and all of them to the master.
        """
        import glob

        def slugify(s: str) -> str:
            return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")

        fields = list(WorkOrderDetail.__dataclass_fields__)
        new_ids = {str(r.wo_id).strip() for r in records}
        new_by_slug: dict = {}
        for r in records:
            slug = slugify(r.department) or "general"
            new_by_slug.setdefault(slug, []).append(asdict(r))

        # Every existing per-department file, plus any new department slug.
        dept_slugs = set(new_by_slug)
        for p in glob.glob(os.path.join(OUTPUT_DIR, "work_orders_unscheduled_*.json")):
            dept_slugs.add(os.path.basename(p)[len("work_orders_unscheduled_"):-len(".json")])

        def _merge(json_path: str, add_rows: list) -> tuple:
            existing = []
            if os.path.exists(json_path):
                try:
                    with open(json_path, encoding="utf-8") as f:
                        existing = json.load(f)
                except (json.JSONDecodeError, OSError):
                    existing = []
            kept = [r for r in existing
                    if str(r.get("wo_id", "")).strip() not in new_ids]
            rows = kept + add_rows
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(rows, f, indent=2)
            with open(json_path[:-5] + ".csv", "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                for r in rows:
                    w.writerow({k: (json.dumps(r.get(k))
                                    if isinstance(r.get(k), (list, dict))
                                    else r.get(k, "")) for k in fields})
            return len(add_rows), len(rows)

        for slug in sorted(dept_slugs):
            add = new_by_slug.get(slug, [])
            path = os.path.join(OUTPUT_DIR, f"work_orders_unscheduled_{slug}.json")
            if not add and not os.path.exists(path):
                continue
            n_add, n_total = _merge(path, add)
            if n_add:
                print(f"  [orphans] {slug}: +{n_add} (now {n_total})")

        _merge(os.path.join(OUTPUT_DIR, "work_orders_unscheduled.json"),
               [asdict(r) for r in records])
        print(f"  [orphans] master updated (+{len(records)})")

    # ------------------------------------------------------------------
    def _quit_owned_driver(self):
        if self._owns_driver and self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass

    def run(self, limit: Optional[int] = None, department: Optional[str] = None,
            equipment_ids: Optional[List[str]] = None):
        if self.offline:
            # Offline replay never touches the live site and must not re-capture.
            self.capture_html = False
            self.attach_offline()
        elif self.headless:
            self.launch_headless()
        else:
            self.attach()

        # Orphan (equipment-less) unscheduled WOs are division-wide, not tied to
        # any equipment, so they are scraped in one pass instead of the machine
        # loop.
        if self.orphans_only:
            records = self.scrape_orphan_unscheduled()
            self.save_orphans(records)
            print("\nDone (unscheduled-all / orphans).")
            self._quit_owned_driver()
            return

        if self.offline:
            equipment_list = self.get_equipment_list()
        else:
            if department:
                equipment_list = self._ids_from_csv(department=department)
                print(f"Loaded {len(equipment_list)} '{department}' equipment from equipment_data.csv")
                if not equipment_list:
                    print("No equipment matched that department. "
                          "Run the equipment parser/scraper first to build equipment_data.csv.")
            else:
                equipment_list = self.get_equipment_list()

        if equipment_ids:
            wanted = {str(i).strip() for i in equipment_ids}
            equipment_list = [e for e in equipment_list if str(e["id"]).strip() in wanted]
            # Fall back to the CSV for any requested id missing from the live list.
            found = {str(e["id"]).strip() for e in equipment_list}
            for missing in wanted - found:
                for e in self._ids_from_csv():
                    if str(e["id"]).strip() == missing:
                        equipment_list.append(e)
                        break
            print(f"Filtered to {len(equipment_list)} equipment for ids {sorted(wanted)}")

        # A scrape that targets a subset (single machine or department) must
        # merge into the existing data instead of overwriting everything.
        if department or equipment_ids:
            self.partial = True

        # When writing to a dedicated per-department file (parallel runs), each
        # file holds exactly that department's records, so do NOT merge - the
        # separate run_parallel.py merge step combines them at the end.
        if self.out_suffix:
            self.partial = False

        if limit:
            equipment_list = equipment_list[:limit]

        total = len(equipment_list)
        print(f"\nScraping {total} equipment records...\n")

        for idx, equipment in enumerate(equipment_list, 1):
            print(f"[{idx}/{total}] {equipment['eq_id']}")
            # Record the id up front so a machine whose work orders are now all
            # closed gets its stale records removed on merge (even if it
            # returns zero rows this run).
            self.scraped_ids.add(str(equipment["id"]).strip())
            if not self.skip_unscheduled:
                try:
                    self.records.extend(self.scrape_equipment(equipment))
                except Exception as e:
                    print(f"  Error (unscheduled): {e}")
            try:
                self.scheduled_records.extend(self.scrape_scheduled(equipment))
            except Exception as e:
                print(f"  Error (scheduled): {e}")

            if idx % SAVE_EVERY == 0:
                self.save()

        self.save()
        print("\nDone.")
        # Quit only the headless browser we launched ourselves. Never quit the
        # user's attached debug Chrome - that would close their browser.
        if self._owns_driver and self.driver:
            try:
                self.driver.quit()
            except Exception:
                pass


# ---------------------------------------------------------------------------
def main():
    import argparse
    p = argparse.ArgumentParser(description="PM Unscheduled Work Order scraper")
    p.add_argument("--limit", type=int, default=None,
                   help="Scrape only first N equipment records (testing)")
    p.add_argument("--department", type=str, default=None,
                   help="Only scrape equipment in this department "
                        "(e.g. \"Toilet Partitions\"). Reads from equipment_data.csv.")
    p.add_argument("--equipment-id", dest="equipment_ids", action="append",
                   default=None, metavar="ID",
                   help="Only scrape this equipment id (e.g. 1877 = Rainbow). "
                        "Repeat the flag for several ids. Merges into the "
                        "existing data; other machines are preserved.")
    p.add_argument("--skip-swo-attachments", action="store_true",
                   help="Scheduled work orders: read all fields from the grid "
                        "only and DON'T open each dialog for attachments. Much "
                        "faster and fully reliable for core fields.")
    p.add_argument("--scheduled-only", action="store_true",
                   help="Only refresh scheduled work orders; leave the "
                        "unscheduled data (work_orders_unscheduled.*) untouched.")
    p.add_argument("--no-capture-html", action="store_true",
                   help="Live scrape: do NOT mirror visited pages to the "
                        f"'{os.path.basename(PAGES_DIR)}' folder.")
    p.add_argument("--from-html", dest="offline", action="store_true",
                   help="OFFLINE replay: rebuild the work-order data from the "
                        f"previously captured HTML in '{os.path.basename(PAGES_DIR)}' "
                        "instead of the live website. Launches its own headless "
                        "Chrome - no start_chrome_debug.bat needed.")
    p.add_argument("--pages-dir", type=str, default=None,
                   help="Override the capture/replay folder (default: "
                        f"{PAGES_DIR}).")
    p.add_argument("--port", type=int, default=9222,
                   help="Chrome remote-debugging port to attach to "
                        "(default 9222). Used by run_parallel.py to drive one "
                        "Chrome per department.")
    p.add_argument("--out-suffix", type=str, default=None,
                   help="Write results to work_orders_<kind>_<suffix>.json/.csv "
                        "instead of the master files, so parallel instances "
                        "don't clobber each other. Disables merging.")
    p.add_argument("--headless", action="store_true",
                   help="LIVE scrape in a scraper-owned headless Chrome started "
                        "from a logged-in profile copy (no start_chrome_debug.bat "
                        "window needed). More stable than attaching - avoids the "
                        "'invalid session id' crashes from a shared Chrome.")
    p.add_argument("--profile", type=str, default=None,
                   help="Explicit Chrome profile dir for --headless (default: a "
                        "copy of the logged-in PM_Debug_Profile).")
    p.add_argument("--refresh-profile", action="store_true",
                   help="Re-copy the login profile for --headless (do this after "
                        "logging in again / when the session has expired).")
    p.add_argument("--unscheduled-all", dest="orphans_only", action="store_true",
                   help="Scrape ONLY the division-wide equipment-less "
                        "(facility / general request) unscheduled work orders "
                        "from WorkOrderUnshdAll and merge them into the "
                        "per-department + master unscheduled files. Leaves all "
                        "equipment-linked data untouched.")
    args = p.parse_args()

    mode = "  [OFFLINE REPLAY]" if args.offline else ("  [HEADLESS]" if args.headless else "")
    print("=" * 64)
    print("PM Work Order Scraper" + mode)
    print("=" * 64)
    if not args.offline and not args.headless:
        print("Chrome must be running via start_chrome_debug.bat")
    elif args.headless:
        print("Headless mode: using a logged-in profile copy "
              "(log in once via start_chrome_debug.bat first).")
    print("=" * 64 + "\n")

    scraper = WorkOrderScraper()
    scraper.swo_attachments = not args.skip_swo_attachments
    scraper.skip_unscheduled = args.scheduled_only
    scraper.orphans_only = args.orphans_only
    scraper.offline = args.offline
    scraper.headless = args.headless
    scraper.profile = os.path.abspath(args.profile) if args.profile else None
    scraper.refresh_profile = args.refresh_profile
    scraper.capture_html = not args.no_capture_html
    scraper.debugger = f"127.0.0.1:{args.port}"
    if args.out_suffix:
        scraper.out_suffix = args.out_suffix
    if args.pages_dir:
        scraper.pages_dir = os.path.abspath(args.pages_dir)
    scraper.run(limit=args.limit, department=args.department,
                equipment_ids=args.equipment_ids)


if __name__ == "__main__":
    main()
