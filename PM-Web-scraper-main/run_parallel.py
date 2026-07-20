"""
Parallel work-order scrape: one Chrome + one scraper per department.

WHY
---
A single scraper walks every machine one-by-one, opening each work-order
dialog. With 525 machines that is slow. The work splits cleanly by department,
so this launcher runs several scrapers at once - each driving its OWN Chrome
window on its OWN debugging port, scraping ONE department, and writing to its
OWN output file. When they all finish, the per-department files are merged back
into the master work_orders_unscheduled.* / work_orders_scheduled.* files.

HOW IT WORKS
------------
1. Your authenticated debug profile (the one start_chrome_debug.bat logs into)
   is copied once per department into a private folder under LOCALAPPDATA so
   each Chrome instance is already logged in. Heavy cache folders are skipped so
   the copies are small.
2. One Chrome is launched per department on ports 9222, 9223, ...
3. One `python scraper.py --department <d> --port <p> --out-suffix <slug>` is
   launched per department. Output (stdout) goes to logs/parallel_<slug>.log.
4. After all finish, the per-department JSON/CSV files are merged.

USAGE
-----
  1. Close ALL Chrome windows (very important - the profile copy needs them
     closed, and a running Chrome on the base profile blocks the copy).
  2. Make sure you have logged in at least once via start_chrome_debug.bat so
     the base debug profile has a valid session.
  3. Run:
        python run_parallel.py
     Options:
        --jobs N            cap how many run at the same time (default: all 9)
        --refresh-profiles  re-copy the login profile (do this if sessions have
                            expired or you logged in again)
        --keep-open         leave the Chrome windows open when finished
        --skip-swo-attachments / --scheduled-only   passed through to scraper.py

Note: 9 Chrome windows use a lot of RAM/CPU. If your machine struggles, use
`--jobs 3` (or similar) to run them in smaller waves.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request

import scraper as S  # reuse dataclasses + persist logic for the merge step

HERE = os.path.dirname(os.path.abspath(__file__))

# The 9 real departments (the "All Departments" option is intentionally excluded).
DEPARTMENTS = [
    "Maintenance",
    "Quality Assurance",
    "Soap Dispenser Assembly",
    "Toilet Partitions",
    "Machine Shop",
    "Shipping",
    "Mfg Engineering",
    "General",
    "Assembly",
]

BASE_PORT = 9222
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))
BASE_PROFILE = os.path.join(LOCALAPPDATA, "Google", "Chrome", "PM_Debug_Profile")
PARALLEL_PROFILES = os.path.join(LOCALAPPDATA, "Google", "Chrome", "PM_Parallel_Profiles")
LOG_DIR = os.path.join(HERE, "logs")

CHROME_CANDIDATES = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

# Large, regenerable folders we do NOT need to copy (keeps profile copies small
# and fast while still preserving the login cookies).
PROFILE_IGNORE = shutil.ignore_patterns(
    "Cache", "Code Cache", "GPUCache", "GraphiteDawnCache", "ShaderCache",
    "Service Worker", "Crashpad", "component_crx_cache", "extensions_crx_cache",
    "GrShaderCache", "DawnGraphiteCache", "DawnWebGPUCache", "Default Cache",
    "*.log", "Singleton*",
)

DASH_HOME = "https://circaweb.bobrick.com/PME/Forms/EquipmentAll"


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _rec_count(path: str) -> int:
    """Number of records in a work-order JSON file (0 if missing/unreadable)."""
    import json as _json
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
        return len(data) if isinstance(data, list) else 0
    except (OSError, ValueError):
        return 0


def _dept_paths(slug: str) -> list:
    return [os.path.join(HERE, f"work_orders_{kind}_{slug}.{ext}")
            for kind in ("unscheduled", "scheduled") for ext in ("json", "csv")]


def backup_dept_files(selected: list) -> dict:
    """Copy each selected department's per-dept files to .prescrape.bak and
    return {json_path: pre_scrape_record_count} for the shrink guard."""
    counts = {}
    for dept in selected:
        slug = slugify(dept)
        for p in _dept_paths(slug):
            if os.path.exists(p):
                shutil.copy2(p, p + ".prescrape.bak")
            if p.endswith(".json"):
                counts[p] = _rec_count(p)
    return counts


def guard_dept_files(selected: list, pre_counts: dict, progress=print) -> list:
    """Roll back any department whose fresh scrape shrank a substantial file by
    more than half - the signature of a throttled/expired session serving empty
    grids. Returns the list of rolled-back department slugs. Also removes the
    .prescrape.bak files it doesn't need."""
    rolled_back = []
    for dept in selected:
        slug = slugify(dept)
        for jp in (os.path.join(HERE, f"work_orders_unscheduled_{slug}.json"),
                   os.path.join(HERE, f"work_orders_scheduled_{slug}.json")):
            old = pre_counts.get(jp, 0)
            new = _rec_count(jp)
            if old >= 20 and new < 0.5 * old:
                # Restore json + csv from backup.
                base = jp[:-5]
                for ext in ("json", "csv"):
                    bak = f"{base}.{ext}.prescrape.bak"
                    if os.path.exists(bak):
                        shutil.copy2(bak, f"{base}.{ext}")
                progress(f"  GUARD: '{dept}' {os.path.basename(jp)} shrank "
                         f"{old} -> {new}; RESTORED previous data (likely a "
                         "session/throttling issue - scrape sequentially).")
                if slug not in rolled_back:
                    rolled_back.append(slug)
    # Clean up all backups for the selected departments.
    for dept in selected:
        for p in _dept_paths(slugify(dept)):
            bak = p + ".prescrape.bak"
            if os.path.exists(bak):
                try:
                    os.remove(bak)
                except OSError:
                    pass
    return rolled_back


def find_chrome() -> str:
    for path in CHROME_CANDIDATES:
        if os.path.exists(path):
            return path
    sys.exit("ERROR: Could not find chrome.exe. Edit CHROME_CANDIDATES in "
             "run_parallel.py to point at your Chrome install.")


def ensure_base_profile():
    if not os.path.isdir(BASE_PROFILE):
        sys.exit(
            f"ERROR: Base debug profile not found at:\n  {BASE_PROFILE}\n"
            "Run start_chrome_debug.bat once and log in to create it.")


def prepare_profile(slug: str, refresh: bool) -> str:
    dest = os.path.join(PARALLEL_PROFILES, slug)
    if os.path.isdir(dest) and refresh:
        shutil.rmtree(dest, ignore_errors=True)
    if not os.path.isdir(dest):
        print(f"  copying login profile -> {slug} ...")
        shutil.copytree(BASE_PROFILE, dest, ignore=PROFILE_IGNORE,
                        dirs_exist_ok=True)
    return dest


def chrome_ready(port: int) -> bool:
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def launch_chrome(chrome: str, port: int, profile: str,
                  headless: bool = True) -> subprocess.Popen:
    # Launch on about:blank (lightweight); the scraper navigates itself. Each
    # instance gets its own throwaway profile dir, so they never collide.
    # Headless (default) uses far less RAM/GPU than a window, which is what let
    # 9 simultaneous Chromes crash chromedriver in headful mode.
    args = [chrome, f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}", "--no-first-run",
            "--no-default-browser-check", "--disable-session-crashed-bubble",
            "--disable-infobars", "--restore-last-session=false",
            "--no-startup-window=false"]
    if headless:
        args += ["--headless=new", "--disable-gpu", "--disable-dev-shm-usage",
                 "--window-size=1920,1080"]
    args.append("about:blank")
    return subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def chrome_page_count(port: int) -> int:
    """Number of real 'page' targets the Chrome on this port exposes."""
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json", timeout=2) as r:
            import json as _json
            targets = _json.loads(r.read().decode("utf-8", "replace"))
            return sum(1 for t in targets if t.get("type") == "page")
    except Exception:
        return -1


def wait_ready(port: int, timeout: int = 45) -> bool:
    """Wait until Chrome's debug port has at least one usable PAGE target.

    Just checking /json/version was not enough - a Chrome whose only window had
    closed still answered /json/version, which let the scraper attach to a dead
    browser ('Current URL: None'). Requiring a page target avoids that.
    """
    end = time.time() + timeout
    while time.time() < end:
        if chrome_page_count(port) >= 1:
            return True
        time.sleep(0.5)
    return False


def run_orphan_step(chrome: str, args) -> None:
    """Scrape the division-wide equipment-less ("orphan") unscheduled work
    orders and merge them into the per-department files. MUST run AFTER the
    department scrapes (which overwrite those files) and BEFORE merge() (which
    rebuilds the master from them), so orphans survive in both.
    """
    slug = "orphans"
    port = BASE_PORT + len(DEPARTMENTS)      # a port past the department range
    print("\n" + "=" * 64)
    print("Scraping equipment-less (orphan) unscheduled work orders ...")
    print("=" * 64)
    profile = prepare_profile(slug, args.refresh_profiles)
    cmd = [sys.executable, "-u", os.path.join(HERE, "scraper.py"),
           "--unscheduled-all"]
    cp = None
    if args.headful:
        for attempt in (1, 2):
            print(f"[orphans] launching Chrome on port {port} (try {attempt}) ...")
            cp = launch_chrome(chrome, port, profile, headless=False)
            if wait_ready(port, timeout=45):
                break
            try:
                cp.terminate()
            except Exception:
                pass
            time.sleep(2)
        else:
            print("[orphans] WARNING: Chrome never became ready; skipping orphans.")
            return
        cmd += ["--port", str(port)]
    else:
        # Headless owned-browser (default), same reliable model as the
        # per-department jobs.
        cmd += ["--headless", "--profile", profile]

    logf = open(os.path.join(LOG_DIR, "parallel_orphans.log"), "w", encoding="utf-8")
    print(f"[orphans] starting scraper "
          f"({'headful/attach' if args.headful else 'headless/owned'}; "
          f"log: logs/parallel_orphans.log)")
    proc = subprocess.Popen(cmd, cwd=HERE, stdout=logf, stderr=subprocess.STDOUT)
    proc.wait()
    logf.close()
    print(f"[orphans] scraper finished: "
          f"{'OK' if proc.returncode == 0 else f'FAILED (exit {proc.returncode})'}")
    if cp and not args.keep_open:
        try:
            cp.terminate()
        except Exception:
            pass


def merge(skip_unscheduled: bool):
    """Concatenate every per-department file into the master files."""
    print("\n" + "=" * 64)
    print("Merging per-department results ...")
    print("=" * 64)
    kinds = [("work_orders_scheduled", S.ScheduledWorkOrder)]
    if not skip_unscheduled:
        kinds.insert(0, ("work_orders_unscheduled", S.WorkOrderDetail))

    import csv
    import json
    for base, dataclass_type in kinds:
        rows = []
        for dept in DEPARTMENTS:
            part = os.path.join(HERE, f"{base}_{slugify(dept)}.json")
            if os.path.exists(part):
                try:
                    with open(part, encoding="utf-8") as f:
                        rows.extend(json.load(f))
                except (json.JSONDecodeError, OSError) as e:
                    print(f"  WARNING: could not read {part}: {e}")
        # de-dupe by (equipment_id, wo_id) in case a machine appears twice
        seen, deduped = set(), []
        for r in rows:
            key = (str(r.get("equipment_id", "")), str(r.get("wo_id", "")))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(r)
        rows = deduped

        fields = list(dataclass_type.__dataclass_fields__)
        json_path = os.path.join(HERE, f"{base}.json")
        csv_path = os.path.join(HERE, f"{base}.csv")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=2)
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for r in rows:
                w.writerow({
                    k: (json.dumps(r.get(k)) if isinstance(r.get(k), (list, dict))
                        else r.get(k, ""))
                    for k in fields
                })
        print(f"  {base}: merged {len(rows)} records -> {os.path.basename(json_path)}")


def main():
    ap = argparse.ArgumentParser(description="Parallel per-department WO scrape")
    ap.add_argument("--jobs", type=int, default=1,
                    help="Max concurrent departments (default: 1 = sequential). "
                         "The PM site throttles concurrent sessions and starts "
                         "serving EMPTY grids, which silently wipes data - so "
                         "sequential is the safe default. Raise at your own risk.")
    ap.add_argument("--refresh-profiles", action="store_true",
                    help="Re-copy the login profile for each department")
    ap.add_argument("--keep-open", action="store_true",
                    help="Leave the Chrome windows open when finished")
    ap.add_argument("--skip-swo-attachments", action="store_true",
                    help="Pass through: read scheduled WO fields from the grid "
                         "only (no dialog) - much faster")
    ap.add_argument("--scheduled-only", action="store_true",
                    help="Pass through: only refresh scheduled work orders")
    ap.add_argument("--merge-only", action="store_true",
                    help="Skip scraping; just merge existing per-department files")
    ap.add_argument("--skip-orphans", action="store_true",
                    help="Do NOT scrape the division-wide equipment-less "
                         "(facility / general) unscheduled work orders.")
    ap.add_argument("--headful", action="store_true",
                    help="Launch VISIBLE Chrome windows instead of headless. "
                         "Headless is the default (uses far less RAM/GPU and is "
                         "much more stable when running many at once). Requires "
                         "the login profile to already be authenticated.")
    ap.add_argument("--departments", type=str, default=None,
                    help="Comma-separated subset of departments to scrape "
                         "(default: all 9). Useful for re-running ones that "
                         "failed. Names must match exactly, e.g. "
                         "\"Machine Shop,Assembly\".")
    args = ap.parse_args()

    selected = DEPARTMENTS
    if args.departments:
        wanted = [d.strip() for d in args.departments.split(",") if d.strip()]
        unknown = [d for d in wanted if d not in DEPARTMENTS]
        if unknown:
            sys.exit(f"ERROR: unknown department(s): {unknown}\n"
                     f"Valid: {DEPARTMENTS}")
        selected = wanted

    os.makedirs(LOG_DIR, exist_ok=True)

    if args.merge_only:
        merge(args.scheduled_only)
        return

    ensure_base_profile()
    chrome = find_chrome()
    os.makedirs(PARALLEL_PROFILES, exist_ok=True)

    # Snapshot current per-department files so a throttled/empty scrape can be
    # rolled back instead of destroying good data.
    pre_counts = backup_dept_files(selected)

    print("=" * 64)
    print("PARALLEL PM WORK ORDER SCRAPE")
    print("=" * 64)
    print(f"Departments : {len(selected)}  ({', '.join(selected)})")
    print(f"Max parallel: {args.jobs}")
    print(f"Browser     : {'HEADFUL (visible windows)' if args.headful else 'headless'}")
    print(f"Profiles    : {PARALLEL_PROFILES}")
    print(f"Logs        : {LOG_DIR}")
    print("\nMake sure ALL Chrome windows are closed before continuing.")
    print("=" * 64 + "\n")

    # Warm the chromedriver cache once so the 9 child scrapers don't race to
    # download it simultaneously.
    try:
        from webdriver_manager.chrome import ChromeDriverManager
        print("Ensuring chromedriver is downloaded ...")
        ChromeDriverManager().install()
    except Exception as e:
        print(f"  (could not pre-install chromedriver: {e})")

    # Build the work queue: (dept, port, slug). Port index keys off the master
    # DEPARTMENTS list so a given department always uses the same port.
    jobs = []
    for dept in selected:
        port = BASE_PORT + DEPARTMENTS.index(dept)
        jobs.append((dept, port, slugify(dept)))

    chrome_procs = {}   # slug -> Popen
    scraper_procs = {}  # slug -> (Popen, logfile handle, dept)
    pending = list(jobs)
    done = []

    def launch_job(job):
        dept, port, slug = job
        profile = prepare_profile(slug, args.refresh_profiles)
        cmd = [sys.executable, "-u", os.path.join(HERE, "scraper.py"),
               "--department", dept, "--out-suffix", slug]

        if args.headful:
            # Legacy path: WE launch a visible Chrome and the scraper attaches to
            # it over a remote-debugging port. Verify a usable page target first.
            for attempt in (1, 2):
                print(f"[{slug}] launching Chrome on port {port} (try {attempt}) ...")
                chrome_procs[slug] = launch_chrome(chrome, port, profile,
                                                   headless=False)
                if wait_ready(port, timeout=45):
                    break
                print(f"[{slug}] Chrome on port {port} had no page target; "
                      "relaunching ...")
                cp = chrome_procs.pop(slug, None)
                if cp:
                    try:
                        cp.terminate()
                    except Exception:
                        pass
                time.sleep(2)
            else:
                print(f"[{slug}] WARNING: Chrome never became ready; "
                      "scraper will likely fail.")
            cmd += ["--port", str(port)]
        else:
            # Headless (default): the scraper OWNS its own headless Chrome,
            # started from this department's logged-in profile copy. No
            # remote-debugging port / attach - that combination proved flaky
            # (Chrome exposed no page target, chromedriver crashed). Owning the
            # browser is what made the single-department headless runs reliable.
            cmd += ["--headless", "--profile", profile]

        if args.skip_swo_attachments:
            cmd.append("--skip-swo-attachments")
        if args.scheduled_only:
            cmd.append("--scheduled-only")
        logf = open(os.path.join(LOG_DIR, f"parallel_{slug}.log"),
                    "w", encoding="utf-8")
        print(f"[{slug}] starting scraper for '{dept}' "
              f"({'headful/attach' if args.headful else 'headless/owned'}; "
              f"log: logs/parallel_{slug}.log)")
        proc = subprocess.Popen(cmd, cwd=HERE, stdout=logf,
                                stderr=subprocess.STDOUT)
        scraper_procs[slug] = (proc, logf, dept)

    # Launch initial wave
    while pending and len(scraper_procs) < args.jobs:
        launch_job(pending.pop(0))

    # Supervise: as scrapers finish, close their Chrome and start the next job.
    while scraper_procs:
        time.sleep(2)
        for slug in list(scraper_procs):
            proc, logf, dept = scraper_procs[slug]
            if proc.poll() is None:
                continue
            # finished
            rc = proc.returncode
            logf.close()
            status = "OK" if rc == 0 else f"FAILED (exit {rc})"
            print(f"[{slug}] scraper finished: {status}")
            done.append((slug, dept, rc))
            del scraper_procs[slug]
            # close that department's Chrome unless asked to keep open
            cp = chrome_procs.pop(slug, None)
            if cp and not args.keep_open:
                try:
                    cp.terminate()
                except Exception:
                    pass
            # start next pending job, if any
            if pending and len(scraper_procs) < args.jobs:
                launch_job(pending.pop(0))

    print("\nAll department scrapers finished.")

    # Roll back any department whose data shrank by more than half - the
    # signature of a throttled/expired session serving empty grids.
    rolled_back = guard_dept_files(selected, pre_counts)
    if rolled_back:
        print(f"  GUARD: restored previous data for {len(rolled_back)} "
              f"department(s): {', '.join(rolled_back)}")

    ok = [d for _, d, rc in done if rc == 0]
    bad = [d for _, d, rc in done if rc != 0]
    print(f"  Succeeded ({len(ok)}): {', '.join(ok) if ok else '-'}")
    if bad:
        print(f"  FAILED ({len(bad)}): {', '.join(bad)}  "
              "(check logs/parallel_<dept>.log)")

    # Fold in equipment-less (orphan) unscheduled WOs BEFORE merging so they
    # land in both the per-department files and the rebuilt master. Only makes
    # sense for a full, unscheduled-inclusive run.
    if (not args.skip_orphans and not args.scheduled_only
            and selected == DEPARTMENTS):
        run_orphan_step(chrome, args)
    elif not args.skip_orphans and not args.scheduled_only:
        print("\n[orphans] skipped (partial department run; run a full scrape "
              "or 'python scraper.py --unscheduled-all' to refresh orphans).")

    merge(args.scheduled_only)
    print("\nDone. Master files updated: work_orders_unscheduled.* / "
          "work_orders_scheduled.*")
    if args.keep_open:
        print("Chrome windows left open (--keep-open).")


if __name__ == "__main__":
    main()
