"""
Nightly full rescrape + checklist regeneration
===============================================
One cycle, run every night (scheduled in-process by server.py via APScheduler,
or manually: ``python nightly_update.py``):

  1. ARCHIVE every current checklist (guides/*.md) into guides/archive/<stamp>/.
  2. SNAPSHOT the current unscheduled work-order ids per machine (to detect
     what is NEW after the scrape).
  3. RESCRAPE all 9 departments sequentially, driving the ONE already-logged-in
     Chrome (port auto-detected via chrome_session). Every section / comment /
     attachment is captured for both scheduled and unscheduled work orders.
     Results are written to temp files first and only PROMOTED over the live
     per-department files when they look valid, so an expired login can never
     wipe good data.
  4. MERGE the per-department files into the master work_orders_*.json/.csv.
  5. Determine which machines gained NEW unscheduled work orders, and
     REGENERATE only those checklists - preserving all recorded manual edits.
  6. RELOAD the server's in-memory data so the frontend updates immediately.

Design choices confirmed with the user:
  - Single logged-in Chrome, scraped sequentially (robust for unattended 24/7).
  - Only machines with NEW work orders since the last capture are regenerated.
"""

import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime

import analyze_equipment as ae
import chrome_session
import guide_engine as ge
import run_parallel as rp

HERE = os.path.dirname(os.path.abspath(__file__))

# Per-department scrape timeout (seconds). A hung department is killed and
# flagged so the rest of the run continues. Override via env.
DEPT_TIMEOUT = int(os.environ.get("NIGHTLY_DEPT_TIMEOUT", str(4 * 3600)))


def _log(progress, msg):
    if progress:
        progress(msg)
    else:
        print(f"[nightly] {msg}", flush=True)


def _uns_file(slug: str) -> str:
    return os.path.join(HERE, f"work_orders_unscheduled_{slug}.json")


def _sch_file(slug: str) -> str:
    return os.path.join(HERE, f"work_orders_scheduled_{slug}.json")


def _load_json(path: str) -> list:
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _uns_ids_by_equipment(slug: str) -> dict[str, set]:
    """{equipment_id -> set(unscheduled wo_id)} for one department file."""
    out: dict[str, set] = {}
    for r in _load_json(_uns_file(slug)):
        eq = str(r.get("equipment_id") or "").strip()
        wo = str(r.get("wo_id") or "").strip()
        if eq and wo:
            out.setdefault(eq, set()).add(wo)
    return out


def _label_for(records: list[dict], eq_id: str) -> str:
    names = [(_r.get("equipment_name") or "").strip() for _r in records
             if _r.get("equipment_name")]
    name = Counter(names).most_common(1)[0][0] if names else "Unknown"
    eq_label = next((str(_r.get("equipment_eq_id")).strip() for _r in records
                     if _r.get("equipment_eq_id")), f"EQ ID {eq_id}")
    return f"{name} ({eq_label})"


def _scrape_department(dept: str, slug: str, port: int, progress) -> dict:
    """Scrape ONE department into temp files, then promote them over the live
    per-department files only if they look valid. Returns a per-dept result."""
    tmp_suffix = f"{slug}__nightly_tmp"
    tmp_uns = os.path.join(HERE, f"work_orders_unscheduled_{tmp_suffix}.json")
    tmp_sch = os.path.join(HERE, f"work_orders_scheduled_{tmp_suffix}.json")
    tmp_uns_csv = os.path.join(HERE, f"work_orders_unscheduled_{tmp_suffix}.csv")
    tmp_sch_csv = os.path.join(HERE, f"work_orders_scheduled_{tmp_suffix}.csv")
    for p in (tmp_uns, tmp_sch, tmp_uns_csv, tmp_sch_csv):
        try:
            os.remove(p)
        except OSError:
            pass

    cmd = [sys.executable, "-u", os.path.join(HERE, "scraper.py"),
           "--department", dept, "--port", str(port),
           "--out-suffix", tmp_suffix]
    log_path = os.path.join(HERE, "logs", f"nightly_{slug}.log")
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)

    _log(progress, f"scraping '{dept}' (log: logs/nightly_{slug}.log) ...")
    result = {"dept": dept, "slug": slug, "status": "ok",
              "unscheduled": 0, "scheduled": 0, "error": None, "promoted": False}
    with open(log_path, "w", encoding="utf-8") as logf:
        try:
            proc = subprocess.run(cmd, cwd=HERE, stdout=logf,
                                  stderr=subprocess.STDOUT, timeout=DEPT_TIMEOUT)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            result["status"] = "timeout"
            result["error"] = f"exceeded {DEPT_TIMEOUT}s"
            _log(progress, f"'{dept}' TIMED OUT after {DEPT_TIMEOUT}s")
            return result

    if rc != 0:
        result["status"] = "scraper_error"
        result["error"] = f"scraper exited {rc}"

    new_uns = _load_json(tmp_uns)
    new_sch = _load_json(tmp_sch)
    result["unscheduled"] = len(new_uns)
    result["scheduled"] = len(new_sch)

    old_total = len(_load_json(_uns_file(slug))) + len(_load_json(_sch_file(slug)))
    new_total = len(new_uns) + len(new_sch)

    # Promote only when the new scrape has data, OR the department legitimately
    # had none before. This prevents an expired login (0 rows everywhere) from
    # wiping good data.
    if new_total > 0 or old_total == 0:
        for src, dst in ((tmp_uns, _uns_file(slug)), (tmp_sch, _sch_file(slug))):
            if os.path.exists(src):
                os.replace(src, dst)
        result["promoted"] = True
    else:
        result["status"] = "kept_previous"
        result["error"] = (result["error"] or "") + \
            " scrape returned 0 rows but previous data existed; kept previous."
        _log(progress, f"'{dept}': 0 rows scraped but data existed - KEPT previous "
                       "(possible login/session issue)")

    # Clean up any leftover temp files.
    for p in (tmp_uns, tmp_sch, tmp_uns_csv, tmp_sch_csv):
        try:
            os.remove(p)
        except OSError:
            pass
    return result


def run(model: str = None, progress=None, reload_callback=None) -> dict:
    """Execute one nightly cycle. Returns a summary dict."""
    model = model or os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    started = datetime.now()
    stamp = started.strftime("%Y%m%d_%H%M%S")
    summary = {
        "status": "success",
        "started": started.isoformat(timespec="seconds"),
        "finished": None,
        "archive_dir": None,
        "archived_guides": 0,
        "departments": [],
        "machines_with_new_wos": 0,
        "regenerated": [],
        "regen_errors": [],
        "notes": [],
    }

    # --- 0. Ensure the logged-in Chrome session (auto-capture port) --------- #
    _log(progress, "ensuring logged-in Chrome session ...")
    try:
        port = chrome_session.ensure_session(require_login=True)
    except SystemExit as e:
        summary["status"] = "skipped"
        summary["notes"].append(f"Chrome not available: {e}")
        _log(progress, f"SKIPPED - {e}")
        summary["finished"] = datetime.now().isoformat(timespec="seconds")
        return summary

    if not chrome_session.is_logged_in(port):
        summary["status"] = "skipped"
        summary["notes"].append(
            "Chrome is not logged into the PM site; scrape skipped to avoid "
            "overwriting good data. Log in on the VM's Chrome window.")
        _log(progress, "SKIPPED - Chrome not logged in")
        summary["finished"] = datetime.now().isoformat(timespec="seconds")
        return summary

    _log(progress, f"using Chrome on port {port}")

    # --- 1. Archive current checklists -------------------------------------- #
    archive_dir, n_arch = ge.archive_all_guides(stamp)
    summary["archive_dir"] = os.path.relpath(archive_dir, HERE)
    summary["archived_guides"] = n_arch
    _log(progress, f"archived {n_arch} checklists -> {summary['archive_dir']}")

    # --- 2. Snapshot pre-scrape unscheduled WO ids per machine -------------- #
    before: dict[str, set] = {}
    for dept in rp.DEPARTMENTS:
        slug = rp.slugify(dept)
        for eq, ids in _uns_ids_by_equipment(slug).items():
            before.setdefault(eq, set()).update(ids)

    # --- 3. Rescrape all departments sequentially --------------------------- #
    for dept in rp.DEPARTMENTS:
        slug = rp.slugify(dept)
        res = _scrape_department(dept, slug, port, progress)
        summary["departments"].append(res)
        # Re-check auth between departments; bail early if the session dropped.
        if not chrome_session.is_logged_in(port):
            summary["notes"].append(
                f"Login appears to have expired after '{dept}'. Remaining "
                "departments were skipped to protect existing data.")
            _log(progress, "login lost mid-run; stopping further scraping")
            break

    # --- 4. Merge per-department files into the master files ---------------- #
    try:
        rp.merge(skip_unscheduled=False)
        _log(progress, "merged per-department files into master files")
    except Exception as e:
        summary["notes"].append(f"merge failed: {e}")
        _log(progress, f"merge failed: {e}")

    # --- 5. Detect machines with NEW unscheduled WOs & regenerate ----------- #
    after: dict[str, set] = {}
    uns_records_by_eq: dict[str, list] = {}
    for dept in rp.DEPARTMENTS:
        slug = rp.slugify(dept)
        for r in _load_json(_uns_file(slug)):
            eq = str(r.get("equipment_id") or "").strip()
            wo = str(r.get("wo_id") or "").strip()
            if not eq:
                continue
            after.setdefault(eq, set())
            if wo:
                after[eq].add(wo)
            uns_records_by_eq.setdefault(eq, []).append(r)

    changed = [eq for eq, ids in after.items() if ids - before.get(eq, set())]
    summary["machines_with_new_wos"] = len(changed)
    _log(progress, f"{len(changed)} machine(s) have new work orders since last capture")

    for eq in changed:
        recs = uns_records_by_eq.get(eq, [])
        if not recs:
            continue
        label = _label_for(recs, eq)
        try:
            _log(progress, f"regenerating checklist for {label} ...")
            result = ge.update_guide(eq, recs, label, model)
            summary["regenerated"].append({
                "eq_id": eq, "label": label,
                "updated": result.get("updated"),
                "new_count": result.get("new_count"),
            })
        except SystemExit as e:
            summary["regen_errors"].append({"eq_id": eq, "error": str(e)})
            _log(progress, f"regen FAILED for {eq}: {e}")
        except Exception as e:
            summary["regen_errors"].append({"eq_id": eq, "error": str(e)})
            _log(progress, f"regen FAILED for {eq}: {e}")

    # --- 6. Reload the server's in-memory data ------------------------------ #
    if reload_callback:
        try:
            reload_callback()
            _log(progress, "server data reloaded")
        except Exception as e:
            summary["notes"].append(f"reload failed: {e}")

    if summary["regen_errors"]:
        summary["status"] = "success_with_errors"
    summary["finished"] = datetime.now().isoformat(timespec="seconds")
    _log(progress, f"nightly cycle finished: {summary['status']}")
    return summary


def main():
    print("=" * 60)
    print("Nightly full rescrape + checklist regeneration")
    print("=" * 60)
    summary = run()
    print("\n" + json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
