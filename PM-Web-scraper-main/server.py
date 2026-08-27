"""
BLA Maintenance Dashboard - Backend
===================================
Flask server powering a local web UI with a three-level drill-down:

    BLA Division  ->  Department  ->  Machine (EQ ID)  ->  4 tabs
        (dashboard)     (dashboard)     1. Dashboard
                        + machines      2. AI Troubleshooting Checklist (Ollama)
                                        3. Unscheduled work orders
                                        4. Scheduled work orders

Only two departments are surfaced: "Soap Dispenser Assembly" (Soap & Assembly)
and "Toilet Partitions".

It reuses the stats / prompt / Ollama logic from analyze_equipment.py (including
the Windows-TLS PowerShell transport, since the corporate network blocks the
OpenSSL-based TLS that Python would otherwise use).


Run:
    python server.py
Then open http://127.0.0.1:5000 in your browser.

Per-machine troubleshooting checklists are cached as Markdown in
./guides/<equipment_id>.md and only regenerated when explicitly requested.
"""

import json
import os
import re
import shutil
import threading
import time
from collections import Counter
from datetime import datetime, timedelta

from flask import Flask, jsonify, make_response, request, send_from_directory
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

import analyze_equipment as ae
import guide_engine as ge
import mint_store as store
import mint_email as emailer
import longevity_parser as lp
import nightly_update
from scraper import ScheduledWorkOrder, WorkOrderDetail

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
WEBAPP_DIR = os.path.join(OUTPUT_DIR, "webapp")
GUIDES_DIR = os.path.join(OUTPUT_DIR, "guides")
CLOSED_STATUS = "Closed and Completed"
# Local copies of the scraped PM "Equipment Summary" dashboards, one folder per
# numeric equipment id. These hold the ORIGINAL, un-edited notes used as the
# source of truth for AI note sorting.
EQUIPMENT_PAGES_DIR = os.path.join(OUTPUT_DIR, "pages", "equipment")

# Gemini model name (only used if LLM_PROVIDER=gemini). Checklists default to
# Ollama Cloud because OLLAMA_API_KEY is present in the .env file.
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# Ollama Cloud model. The default in analyze_equipment.py ("gemma4:31b") is not
# a real cloud model, so pin a valid one here unless the user overrides it.
ae.OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:120b")

# Shared password that gates checklist editing (generate / edit-save / update).
# Set EDIT_PASSWORD in the .env file. If it is left blank, editing stays OPEN
# (unprotected) so a fresh install isn't accidentally locked out.
EDIT_PASSWORD = os.environ.get("EDIT_PASSWORD", "").strip()


def _edit_ok(req) -> bool:
    """True if the request is allowed to modify checklists. When no password is
    configured, editing is open. Otherwise the caller must supply the shared
    password via the 'X-Edit-Password' header (or a 'password' JSON field)."""
    if not EDIT_PASSWORD:
        return True
    supplied = (req.headers.get("X-Edit-Password") or "").strip()
    if not supplied:
        body = req.get_json(silent=True) or {}
        supplied = (body.get("password") or "").strip()
    return supplied == EDIT_PASSWORD


# Separate admin password that gates sensitive work-order actions: deleting a
# work order and viewing a work order's version (change) history. Set
# ADMIN_PASSWORD in the .env file. If left blank, these actions stay OPEN so a
# fresh install isn't accidentally locked out.
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "").strip()


def _admin_ok(req) -> bool:
    """True if the request may perform an admin-gated action (delete WO / view
    history). Open when no ADMIN_PASSWORD is configured; otherwise the caller
    must supply it via the 'X-Admin-Password' header (or a 'password' JSON
    field)."""
    if not ADMIN_PASSWORD:
        return True
    supplied = (req.headers.get("X-Admin-Password") or "").strip()
    if not supplied:
        body = req.get_json(silent=True) or {}
        supplied = (body.get("password") or "").strip()
    return supplied == ADMIN_PASSWORD


# Separate Sean (supervisor) password that gates work-order assignment and the
# completed-work-order team dashboard. Override it with SEAN_PASSWORD in the
# environment; otherwise it falls back to this default so Sean's profile is
# ALWAYS password-protected.
SEAN_PASSWORD = os.environ.get("SEAN_PASSWORD", "").strip() or "sean"


def _sean_ok(req) -> bool:
    """True if the request may perform Sean-only actions (assign work orders to
    someone else, view completed-counts dashboard). Open when no SEAN_PASSWORD
    is configured; otherwise the caller must supply it via the 'X-Sean-Password'
    header (or a 'password' JSON field)."""
    if not SEAN_PASSWORD:
        return True
    supplied = (req.headers.get("X-Sean-Password") or "").strip()
    if not supplied:
        body = req.get_json(silent=True) or {}
        supplied = (body.get("password") or "").strip()
    return supplied == SEAN_PASSWORD


# Maintenance technicians who can take (self-assign) work orders.
# Loaded dynamically from the store by _load_technicians(); seeded with the
# original three on first run.
_TECHNICIANS: set[str] = set()
_TECH_ALIASES: dict[str, set[str]] = {}


def _load_technicians() -> None:
    """Reload the in-memory technician sets from the store."""
    global _TECHNICIANS, _TECH_ALIASES
    _TECHNICIANS = set()
    _TECH_ALIASES = {}
    for t in store.list_technicians(active_only=True):
        name = (t.get("name") or "").strip()
        if not name:
            continue
        name_l = name.lower()
        _TECHNICIANS.add(name_l)
        aliases = {name_l}
        for a in t.get("aliases") or []:
            a = (a or "").strip().lower()
            if a:
                aliases.add(a)
        _TECH_ALIASES[name] = aliases


_load_technicians()


def _is_technician(name: str) -> bool:
    return (name or "").strip().lower() in _TECHNICIANS


os.makedirs(GUIDES_DIR, exist_ok=True)

app = Flask(__name__, static_folder=WEBAPP_DIR, static_url_path="")

# Nightly scheduler state (one thread, started on server launch)
_NIGHTLY_THREAD = None
_NIGHTLY_STOP_EVENT = None
_NIGHTLY_LAST_STATUS = {"last_run": None, "last_summary": None, "running": False}

CORS(app)

# --------------------------------------------------------------------------- #
# Company / division / department configuration
# --------------------------------------------------------------------------- #
# The top "company" layer sits above divisions. Divisions (BLA, and later BED,
# etc.) sit above departments. Every scraped department currently belongs to the
# BLA division; user-added departments carry their own division_key (see the
# mint_store `departments` table).
COMPANY = {"key": "company", "name": os.environ.get("COMPANY_NAME", "Bobrick")}
DIVISION = {"key": "bla", "name": "BLA"}

# The scraped departments (frozen from the PM export). Manual departments are
# merged on top of these at reload time into the live DEPARTMENTS dict.
_STATIC_DEPARTMENTS = {
    "soap": {
        "key": "soap",
        "name": "Soap Dispenser Assembly",
        "label": "Soap & Assembly",
        "unscheduled": "work_orders_unscheduled_soap_dispenser_assembly.json",
        "scheduled": "work_orders_scheduled_soap_dispenser_assembly.json",
    },
    "toilet": {
        "key": "toilet",
        "name": "Toilet Partitions",
        "label": "Toilet Partitions",
        "unscheduled": "work_orders_unscheduled_toilet_partitions.json",
        "scheduled": "work_orders_scheduled_toilet_partitions.json",
    },
    "assembly": {
        "key": "assembly",
        "name": "Assembly",
        "label": "Assembly",
        "unscheduled": "work_orders_unscheduled_assembly.json",
        "scheduled": "work_orders_scheduled_assembly.json",
    },
    "general": {
        "key": "general",
        "name": "General",
        "label": "General",
        "unscheduled": "work_orders_unscheduled_general.json",
        "scheduled": "work_orders_scheduled_general.json",
    },
    "machine_shop": {
        "key": "machine_shop",
        "name": "Machine Shop",
        "label": "Machine Shop",
        "unscheduled": "work_orders_unscheduled_machine_shop.json",
        "scheduled": "work_orders_scheduled_machine_shop.json",
    },
    "maintenance": {
        "key": "maintenance",
        "name": "Maintenance",
        "label": "Maintenance",
        "unscheduled": "work_orders_unscheduled_maintenance.json",
        "scheduled": "work_orders_scheduled_maintenance.json",
    },
    "mfg_engineering": {
        "key": "mfg_engineering",
        "name": "Mfg Engineering",
        "label": "Mfg Engineering",
        "unscheduled": "work_orders_unscheduled_mfg_engineering.json",
        "scheduled": "work_orders_scheduled_mfg_engineering.json",
    },
    "quality_assurance": {
        "key": "quality_assurance",
        "name": "Quality Assurance",
        "label": "Quality Assurance",
        "unscheduled": "work_orders_unscheduled_quality_assurance.json",
        "scheduled": "work_orders_scheduled_quality_assurance.json",
    },
    "shipping": {
        "key": "shipping",
        "name": "Shipping",
        "label": "Shipping",
        # No work-order files were scraped for Shipping; equipment still shows.
        "unscheduled": "work_orders_unscheduled_shipping.json",
        "scheduled": "work_orders_scheduled_shipping.json",
    },
}

# Live department map (static + user-added). Rebuilt by reload_data(). Every
# scraped department maps to the BLA division; _DEPT_DIVISION tracks each
# department's division so the company layer can group them.
DEPARTMENTS = dict(_STATIC_DEPARTMENTS)
_DEPT_DIVISION = {k: "bla" for k in _STATIC_DEPARTMENTS}


def _rebuild_departments() -> None:
    """Merge user-added departments (from the SQLite store) on top of the frozen
    scraped departments. Manual departments have no scraped JSON files - their
    work orders come from the store and are injected in reload_data()."""
    global DEPARTMENTS, _DEPT_DIVISION
    merged = dict(_STATIC_DEPARTMENTS)
    div = {k: "bla" for k in _STATIC_DEPARTMENTS}
    for d in store.list_departments():
        key = d["key"]
        merged[key] = {
            "key": key,
            "name": d["name"],
            "label": d.get("label") or d["name"],
            "unscheduled": None,   # sourced from the store, not a file
            "scheduled": None,
            "manual": True,
        }
        div[key] = d.get("division_key") or "bla"
    # Drop soft-deleted (inactive) departments so they - and their machines and
    # work orders - disappear from every view until they are restored.
    for key in store.inactive_department_keys():
        merged.pop(key, None)
        div.pop(key, None)
    DEPARTMENTS = merged
    _DEPT_DIVISION = div


# --------------------------------------------------------------------------- #
# Machine grouping (within a department)
# --------------------------------------------------------------------------- #
# Ordered groups for the Toilet Partitions department. Names are the ACTUAL
# equipment_name strings as they appear in the scraped data (matched after
# light normalization), so minor spacing/quote differences still line up.
TOILET_GROUP_ORDER = [
    "Machines", "Vehicles", "General", "Equipment", "Gauges and Jigs",
    "Carts", "Tools",
]

TOILET_GROUPS = {
    "Machines": [
        '1/2 " Edge Finisher, Solid, Technolegno/ Universal 280',
        '3/4 " Edge Finisher, Solid, Technolegno/ Universal 280',
        'CNC Drilling Machine, Automatic Leveling Bar',
        'Chop Saw (corner guard pack out Lam.)',
        'Chop saw by Holzma Saw',
        'Drill Press (Stile Building Cell)',
        'Drilling Machine CNC, 1040 Laminate',
        'Edge Finisher Laminate',
        'Edgebander Homag 2520 Servo 6 Coil',
        'Evolve Double Head Drilling Machine',
        'Gannomat Index 330 Trend/PRO (Solid)',
        'Insert 1 Screwdriver, Auto Reverse, Lever',
        'Insert Screwdriver, 1080 -1',
        'Laminate Slitter',
        'Notching machine, 1540 Door',
        'O-Sama (Joos) Glue Spreader',
        'Pinch Roller (Heated)',
        'Router Station (Stile Building cell)',
        'Router, CNC, Anderson Stratos/Nest TC+D',
        "Saw, 10' Panel, Laminate Line",
        'Saw, Horizontal, Holzma',
        'Saw,Horizontal, Holz-Her',
        'Screwdriver, Insert, 1080-2',
        'Step Drill 1040 CNC Drilling Machine',
        'Step Drill 1080/1090 CNC Drilling Machine',
        'TLF Intellistore (Rainbow Stacking System)- TLF211',
        'Tenoner A 517 Single End',
        'VLM Storage Lift -Small Hardware',
    ],
    "Vehicles": [
        'Forklift # T20',
        'Forklift # T4',
        'Forklift # T5',
        'Scissor Lift #1 (small) Holz-Her Saw',
        'Scissor Lift Holz-Her Edge Bander',
        'Scissor Lift # 2 (large) Holz-Her Saw',
        'Scissor Lift 1/2" Edge Finisher',
        'Scissor Lift 3/4" solid Edge Finisher',
        'Scissor Lift Holzma Saw',
        'Sissors Lift, HolzHer Saw',
        'Stacker R-19',
    ],
    "General": [
        'Concrete floor',
        'Flamex spark detection and extinguishing system',
        'General Maintenance',
        'Laminate Cell',
        'Solid Cell',
        'TPF',
    ],
    "Equipment": [
        '1/2 Pop-up table made in house',
        '2 gallon glue tank with hand held glue nozzle gun',
        '3/4 Thomas return system',
        'Dust Collector, Donaldson Downflo Oval',
        'FRL - Filter, Regulator, Lubricator',
        'Meyer rotary airlock (dust collector)',
        'Panel Handler - 4ft',
        'Rework Station (Laminate Cell)',
        'Edge finisher Pop UP table',
        'Edgebander Pop up table',
        'Evolve cell Pop UP table',
        'FLIP TABLE',
        'POP UP Table, 3/4" Edge Finisher',
        'Return System, Thomas, 1/2" Solid Panels',
        'Return System, Thomas, 1040 Edgebander',
        'Return System, Thomas, Laminate Trimmer',
        'Return system,Thomas,Evolve cell',
        'Vacuum Lift (Anderson CNC)',
        'Vacuum Lift 1/2" panels packout',
        'Vacuum Lift 1080 Line packout',
        'Vacuum Lift Evolve Cell',
        'Vacuum Lift Holz-Her Saw',
        'Vacuum Lift Laminate Pack Out',
        'Vacuum Lift System (Glue Line )',
    ],
    "Gauges and Jigs": [
        'CNC Drill Setup Gage-1040',
        'CNC Drill Setup Gage-1080',
        'Cutout Jig B3471/B3571',
        'Cutout Jig B357, B347',
        'Drill Jig - OS Door Hinge',
        'Drill Jig - for Door Hinges ( 3 Hinges) Laminate',
        'Drill Jig - for Door Hinges Laminate',
        'Drill Jig - for Hinges Stile -I/S-O/S FC For Laminate',
        'Drill Jig - for Hinges for O/S Stile Hinges',
        'Drill Jig, 1080/ 1090 Leveling Bar',
        'Gage (Go/No Go), Drill Diameter, Laminate',
        'Gage (Go/No Go), Drill Diameter, Solid',
        'Jig, Drill, T-203040 ECOR T-Nut Drill, Laminate',
        'Laminate Drill Hole Depth Gage',
        'TPT CL 1005 ANDY',
        'TPT CL 1005 TENO',
    ],
    "Carts": [
        'Cart - TPF Finish Goods',
        'Drywall Carts 1-15',
        'Job Carts 1-6',
        'Materal Carts 1-2',
        'Pack out Carts 1-6',
    ],
    "Tools": [
        'Driver, Pulse Tool, 1080 Leveling Bar',
        'Driver, Pulse Tool, 1080 Leveling Bar,Desoutter model PTF022-T6500-S4Q',
        'Shaper, Single Spindle, Northfield',
        'Step Drill & Stop Phenolic Series Insert',
    ],
}

OTHER_GROUP = "Other"


def _norm_name(name: str) -> str:
    """Normalize an equipment name for tolerant matching."""
    s = (name or "").lower().strip()
    for ch in ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019"):
        s = s.replace(ch, "")
    return " ".join(s.split())


# group config per department key -> (ordered group names, {normalized name: group})
def _build_group_lookup(groups: dict) -> tuple[list, dict]:
    lookup = {}
    for grp, names in groups.items():
        for n in names:
            lookup[_norm_name(n)] = grp
    return list(groups.keys()), lookup


_DEPT_GROUPS = {
    "toilet": _build_group_lookup(TOILET_GROUPS),
}


def _group_for(dept_key: str, name: str) -> str | None:
    cfg = _DEPT_GROUPS.get(dept_key)
    if not cfg:
        return None
    return cfg[1].get(_norm_name(name), OTHER_GROUP)


def _group_order(dept_key: str) -> list:
    cfg = _DEPT_GROUPS.get(dept_key)
    if not cfg:
        return []
    return cfg[0] + [OTHER_GROUP]


def _load(filename: str) -> list[dict]:
    if not filename:
        return []
    path = os.path.join(OUTPUT_DIR, filename)
    if not os.path.exists(path):
        return []
    return ae.load_work_orders(path)


# In-memory caches. Populated by reload_data(), which is called at startup and
# whenever data changes (e.g. via /api/reload) so the frontend reflects fresh
# data WITHOUT a server restart. A lock guards swaps so requests never see
# half-loaded data.
_DATA_LOCK = threading.RLock()
_DEPT_DATA: dict[str, dict[str, list[dict]]] = {}
_WO_INDEX: dict[str, dict] = {}
_EQUIP_BY_KEY: dict[str, list[dict]] = {}
_LAST_RELOAD: datetime | None = None


# --------------------------------------------------------------------------- #
# Equipment master list (the authoritative set of machines per department).
# The dashboard's machine list is driven by this so that equipment with ZERO
# work orders still appears, matching the PM database equipment counts.
# --------------------------------------------------------------------------- #
EQUIPMENT_FILE = "equipment_data.json"

# Map the equipment-master department string -> our department key.
_DEPT_NAME_TO_KEY = {cfg["name"]: key for key, cfg in DEPARTMENTS.items()}


def _num_id(s: str) -> str:
    """Extract the numeric portion of an EQ ID (e.g. 'EQ ID 2082' -> '2082')."""
    m = re.search(r"(\d+)", s or "")
    return m.group(1) if m else ""


def _is_completed(status) -> bool:
    """A work order counts as done when its status mentions 'complete' or
    'closed' (e.g. 'Closed and Completed')."""
    s = (status or "").lower()
    return "complete" in s or "closed" in s


# Date formats people might type in the create/edit forms. We normalise all of
# them to the canonical "MM/DD/YYYY" the scraped data uses, so sorting (which
# relies on ae._parse_date / the frontend parser, both 4-digit-year only) puts
# manual work orders in the right place instead of dumping them at the bottom.
_DATE_INPUT_FORMATS = (
    "%m/%d/%y", "%m/%d/%y %I:%M %p", "%m/%d/%y %H:%M",
    "%m-%d-%Y", "%m-%d-%y",
    "%Y-%m-%d", "%Y-%m-%d %H:%M",
    "%m/%d/%Y %H:%M",
)


def _normalize_date(s: str) -> str:
    """Return a date string as canonical MM/DD/YYYY (keeping a time component if
    one was supplied). Blank stays blank; already-canonical values pass through;
    anything unrecognised is returned unchanged so we never lose data."""
    s = (s or "").strip()
    if not s or ae._parse_date(s):  # blank or already MM/DD/YYYY[ HH:MM AM]
        return s
    for fmt in _DATE_INPUT_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
        except ValueError:
            continue
        has_time = "%H:%M" in fmt or "%I:%M" in fmt
        return dt.strftime("%m/%d/%Y %I:%M %p") if has_time else dt.strftime("%m/%d/%Y")
    return s


def _num(v) -> float:
    """Parse a string/number to float; blanks or invalid values become 0.0."""
    try:
        return float(str(v).strip() or 0)
    except (TypeError, ValueError):
        return 0.0


def _load_equipment() -> dict[str, list[dict]]:
    path = os.path.join(OUTPUT_DIR, EQUIPMENT_FILE)
    by_key: dict[str, list[dict]] = {k: [] for k in DEPARTMENTS}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        name_to_key = {cfg["name"]: key for key, cfg in DEPARTMENTS.items()}
        for e in records:
            key = name_to_key.get((e.get("dept") or "").strip())
            if key is None:
                continue  # department not surfaced in the dashboard
            by_key[key].append(e)
    # Merge user-added machines from the store.
    for m in store.list_machines():
        key = m.get("dept_key")
        if key not in by_key:
            by_key.setdefault(key, [])
        by_key[key].append({
            "eq_id": m.get("eq_id"),
            "equipment_name": m.get("equipment_name"),
            "dept": DEPARTMENTS.get(key, {}).get("name", ""),
            "make": m.get("make", ""),
            "model": m.get("model", ""),
            "vendor": m.get("vendor", ""),
            "asset_num": m.get("asset_num", ""),
            "is_manual": True,
        })
    return by_key


# --------------------------------------------------------------------------- #
# Recurring scheduled work orders
# --------------------------------------------------------------------------- #
# Frequency label -> stepping rule. Month-based frequencies advance by calendar
# months (day-of-month clamped); shorter ones by fixed day counts.
_FREQUENCY_MONTHS = {
    "monthly": 1, "quarterly": 3, "semi-annually": 6, "annually": 12,
}
_FREQUENCY_DAYS = {"weekly": 7, "bi-weekly": 14}


def _parse_mdy(s: str):
    """Parse a MM/DD/YYYY (optionally with a trailing time) date. None if blank
    or unrecognised."""
    s = (s or "").strip().split()[0] if (s or "").strip() else ""
    if not s:
        return None
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _add_months(d: datetime, n: int) -> datetime:
    """Add n calendar months to d, clamping the day to the target month's last
    day (e.g. Jan 31 + 1 month -> Feb 28/29)."""
    import calendar
    m0 = d.month - 1 + n
    year = d.year + m0 // 12
    month = m0 % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return d.replace(year=year, month=month, day=day)


def _add_interval(d: datetime, frequency: str):
    """Advance a due date by one recurrence interval. None if the frequency is
    unknown (so we never loop forever on bad data)."""
    f = (frequency or "").strip().lower()
    if f in _FREQUENCY_DAYS:
        return d + timedelta(days=_FREQUENCY_DAYS[f])
    if f in _FREQUENCY_MONTHS:
        return _add_months(d, _FREQUENCY_MONTHS[f])
    return None


def _generate_recurring_occurrences() -> int:
    """Ensure every recurring scheduled-WO series has its due occurrences created
    through today PLUS the next upcoming one - independent of whether previous
    occurrences were completed. Returns how many new occurrences were created.

    Purely date-driven: occurrences auto-populate on a fixed cadence from the
    seed's first due date, so overdue-but-open PMs still spawn the next one."""
    try:
        recs = store.list_work_orders()
    except Exception as e:
        print(f"[recurring] could not read work orders: {e}", flush=True)
        return 0

    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    series: dict[str, list[dict]] = {}
    for r in recs:
        if r.get("wo_type") != "scheduled":
            continue
        if not (r.get("frequency") or "").strip():
            continue
        if r.get("recurrence_stopped"):
            continue  # series was explicitly stopped - never generate more
        sid = (r.get("series_id") or r.get("wo_id") or "").strip()
        if sid:
            series.setdefault(sid, []).append(r)

    created = 0
    for sid, occs in series.items():
        dated = [(o, _parse_mdy(o.get("due_date"))) for o in occs]
        dated = [(o, d) for o, d in dated if d]
        if not dated:
            continue
        template, max_due = max(dated, key=lambda x: x[1])
        frequency = (template.get("frequency") or "").strip()
        # Already have a future occurrence -> series is up to date.
        if max_due > today:
            continue
        cur = max_due
        guard = 0
        while cur <= today and guard < 200:
            nxt = _add_interval(cur, frequency)
            if nxt is None:
                break
            fields = {
                "equipment_id": template.get("equipment_id", ""),
                "equipment_eq_id": template.get("equipment_eq_id", ""),
                "equipment_name": template.get("equipment_name", ""),
                "department": template.get("department", ""),
                "department_key": template.get("department_key", ""),
                "wo_type": "scheduled",
                "status": "Pending",
                "audit_item": template.get("audit_item", ""),
                "due_date": nxt.strftime("%m/%d/%Y"),
                "frequency": frequency,
                "series_id": sid,
            }
            try:
                store.add_work_order(fields, author="system (recurring)")
                created += 1
            except Exception as e:
                print(f"[recurring] failed to create occurrence for {sid}: {e}",
                      flush=True)
                break
            cur = nxt
            guard += 1
    if created:
        print(f"[recurring] created {created} scheduled occurrence(s)", flush=True)
    return created


def _apply_nicknames(dept_data: dict) -> None:
    """Overlay admin-set nicknames onto every work order's equipment_name so the
    nickname shows everywhere a record is rendered (lists, modal, weekly/monthly
    schedules, exports). The original PM name is preserved as pm_equipment_name
    so nothing is lost and the overlay can be recomputed idempotently."""
    nick_map = store.list_nicknames("")  # {dept_key: {eq_id: nickname}}
    for dk, data in dept_data.items():
        nicks = nick_map.get(dk) or {}
        for kind in ("unscheduled", "scheduled"):
            for r in data.get(kind, []):
                pm = r.get("pm_equipment_name") or r.get("equipment_name") or ""
                r["pm_equipment_name"] = pm
                nick = (nicks.get(_num_id(r.get("equipment_id"))) or "").strip()
                r["equipment_name"] = nick or pm


def reload_data() -> datetime:
    """(Re)load every department's work orders + the equipment master into the
    in-memory caches, then atomically swap them in. Safe to call at any time.

    In addition to the frozen scraped JSON this now merges everything created in
    MINT itself (via mint_store): user-added departments/machines, manually
    created work orders, field-level edits (overrides), and per-WO solution /
    attachment counts."""
    global _DEPT_DATA, _WO_INDEX, _EQUIP_BY_KEY, _LAST_RELOAD

    _load_technicians()  # refresh technician list from store
    _rebuild_departments()  # fold in user-added departments first
    _generate_recurring_occurrences()  # auto-populate due recurring PM occurrences

    dept_data: dict[str, dict[str, list[dict]]] = {}
    for key, cfg in DEPARTMENTS.items():
        uns = _load(cfg.get("unscheduled"))
        sch = _load(cfg.get("scheduled"))
        for r in uns:
            r["wo_type"] = "unscheduled"
            r["department_key"] = key
        for r in sch:
            r["wo_type"] = "scheduled"
            r["department_key"] = key
        dept_data[key] = {"unscheduled": uns, "scheduled": sch}

    # Inject manually-created work orders into their departments.
    for rec in store.list_work_orders():
        # Self-heal date fields to the canonical MM/DD/YYYY the dashboard sorts
        # on, so manual WOs saved with a 2-digit year etc. don't sink to the
        # bottom of the list.
        for df in ("date_notified", "due_date", "completed_datetime"):
            if rec.get(df):
                rec[df] = _normalize_date(rec[df])
        key = rec.get("department_key")
        if key not in dept_data:
            dept_data.setdefault(key, {"unscheduled": [], "scheduled": []})
        kind = "scheduled" if rec.get("wo_type") == "scheduled" else "unscheduled"
        dept_data[key][kind].append(rec)

    wo_index: dict[str, dict] = {}
    for data in dept_data.values():
        for kind in ("unscheduled", "scheduled"):
            for r in data[kind]:
                wid = str(r.get("wo_id") or "").strip()
                if wid and wid not in wo_index:
                    wo_index[wid] = r

    # Apply field-level edits + tag solution/attachment counts on the shared
    # record objects (so both list and detail views see them).
    overrides = store.get_all_overrides()
    sol_counts = store.solution_counts()
    att_counts = store.attachment_counts()
    for wid, rec in wo_index.items():
        patch = overrides.get(wid)
        if patch:
            rec.update(patch)
        rec["_sol_count"] = sol_counts.get(wid, 0)
        rec["_att_count"] = att_counts.get(wid, 0) + len(rec.get("attachments") or [])

    equip = _load_equipment()

    # Hide soft-deleted (inactive) machines and their work orders. Matching is by
    # numeric EQ ID within the machine's department, so this works for both
    # scraped and user-added machines. Restoring simply removes the flag.
    inactive_machines = store.inactive_machine_map()
    if inactive_machines:
        for dk, ids in inactive_machines.items():
            if dk in equip:
                equip[dk] = [e for e in equip[dk]
                             if _num_id(e.get("eq_id")) not in ids]
            if dk in dept_data:
                for kind in ("unscheduled", "scheduled"):
                    dept_data[dk][kind] = [
                        r for r in dept_data[dk][kind]
                        if _num_id(r.get("equipment_id")) not in ids]

    _apply_nicknames(dept_data)

    with _DATA_LOCK:
        _DEPT_DATA = dept_data
        _WO_INDEX = wo_index
        _EQUIP_BY_KEY = equip
        _LAST_RELOAD = datetime.now()
    return _LAST_RELOAD


# Initial load at import time.
reload_data()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _totals(records: list[dict]) -> dict:
    """Sum the three numeric KPI fields across a list of records."""
    return {
        "labor_time": round(sum(ae._to_float(r.get("labor_time")) for r in records), 2),
        "material_cost": round(sum(ae._to_float(r.get("material_cost")) for r in records), 2),
        "downtime_hours": round(sum(ae._to_float(r.get("downtime_hours")) for r in records), 2),
    }


def _stats(unscheduled: list[dict], scheduled: list[dict]) -> dict:
    allr = unscheduled + scheduled
    t = _totals(allr)
    t["unscheduled_count"] = len(unscheduled)
    t["scheduled_count"] = len(scheduled)
    return t


def _compact_wo(r: dict) -> dict:
    """A lightweight work-order record for list views (drops heavy 'comments'
    and attachment payloads; keeps an attachment count and department label)."""
    return {
        "wo_id": r.get("wo_id"),
        "equipment_id": r.get("equipment_id"),
        "equipment_name": r.get("equipment_name"),
        "department_key": r.get("department_key"),
        "department_label": DEPARTMENTS.get(r.get("department_key"), {}).get("label"),
        "status": r.get("status"),
        "urgency": r.get("urgency"),
        "problem": r.get("problem"),
        "audit_item": r.get("audit_item"),
        "date_notified": r.get("date_notified"),
        "due_date": r.get("due_date"),
        "labor_time": r.get("labor_time"),
        "material_cost": r.get("material_cost"),
        "downtime_hours": r.get("downtime_hours"),
        "wo_type": r.get("wo_type"),
        "is_manual": bool(r.get("is_manual")),
        "assigned_to": r.get("assigned_to"),
        "attachment_count": r.get("_att_count", len(r.get("attachments") or [])),
        "solution_count": r.get("_sol_count", 0),
    }


def _sorted_desc(records: list[dict], date_field: str) -> list[dict]:
    """Compact + sort newest -> oldest by date_field. Undated records last."""
    def key(r):
        return ae._parse_date(r.get(date_field)) or datetime.min
    return [_compact_wo(r) for r in sorted(records, key=key, reverse=True)]


def _machine_groups(dept_key: str) -> dict[str, dict[str, list[dict]]]:
    """Group a department's work orders by equipment_id (the EQ ID)."""
    data = _DEPT_DATA.get(dept_key)
    if data is None:
        return {}
    groups: dict[str, dict[str, list[dict]]] = {}
    for kind in ("unscheduled", "scheduled"):
        for r in data[kind]:
            eq = (r.get("equipment_id") or "").strip()
            if not eq:
                continue
            groups.setdefault(eq, {"unscheduled": [], "scheduled": []})[kind].append(r)
    return groups


def _has_critical_wo(recs: dict) -> bool:
    """True if the machine has any pending/open work order with urgency level 1."""
    for kind in ("unscheduled", "scheduled"):
        for r in recs.get(kind, []):
            st = (r.get("status") or "").strip().lower()
            u = (r.get("urgency") or "").strip().lower()
            if (not st or "pending" in st or "open" in st or "progress" in st) and u.startswith("1"):
                return True
    return False


def _all_closed(recs: dict) -> bool:
    """True if the machine has at least one work order and every work order
    (scheduled or unscheduled) is marked Closed and Completed."""
    allr = recs.get("unscheduled", []) + recs.get("scheduled", [])
    if not allr:
        return False
    return all((r.get("status") or "").strip() == CLOSED_STATUS for r in allr)


def _machine_name(records: list[dict]) -> str:
    names = [(r.get("equipment_name") or "").strip() for r in records if r.get("equipment_name")]
    if not names:
        return "Unknown"
    return Counter(names).most_common(1)[0][0]


def _eq_label(records: list[dict], eq_id: str) -> str:
    for r in records:
        lbl = (r.get("equipment_eq_id") or "").strip()
        if lbl:
            return lbl
    return f"EQ ID {eq_id}"


def _dept_machines(dept_key: str) -> list[dict]:
    """Authoritative machine list for a department, driven by the equipment
    master. Each machine joins any matching work orders (by numeric EQ ID).
    Returns dicts of {eq_id, eq_label, name, group, has_guide, all_closed, recs}."""
    wo_groups = _machine_groups(dept_key)  # numeric equipment_id -> {unscheduled, scheduled}
    nicks = store.list_nicknames(dept_key)  # {eq_id: nickname} (admin-set display name)
    out = []
    seen = set()
    for e in _EQUIP_BY_KEY.get(dept_key, []):
        eq_id = _num_id(e.get("eq_id"))
        if not eq_id or eq_id in seen:
            continue
        seen.add(eq_id)
        pm_name = (e.get("equipment_name") or "").strip() or "Unknown"
        nickname = (nicks.get(eq_id) or "").strip()
        # The display name shown throughout MINT is the nickname when one is set;
        # the actual PM name is preserved separately as pm_name.
        name = nickname or pm_name
        recs = wo_groups.get(eq_id, {"unscheduled": [], "scheduled": []})
        out.append({
            "eq_id": eq_id,
            "eq_label": (e.get("eq_id") or f"EQ ID {eq_id}").strip(),
            "name": name,
            "pm_name": pm_name,
            "nickname": nickname,
            "group": _group_for(dept_key, pm_name),
            "has_guide": os.path.exists(_guide_path(eq_id)),
            "all_closed": _all_closed(recs),
            "recs": recs,
        })
    return out


def _guide_path(eq_id: str) -> str:
    safe = "".join(c for c in str(eq_id) if c.isalnum() or c in ("-", "_"))
    return os.path.join(GUIDES_DIR, f"{safe}.md")


def _backup_guide(eq_id: str):
    """Copy the current guide to a timestamped .bak before it is overwritten,
    so any regenerate/update is reversible. Returns the backup path or None."""
    path = _guide_path(eq_id)
    if not os.path.exists(path):
        return None
    safe = "".join(c for c in str(eq_id) if c.isalnum() or c in ("-", "_"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(GUIDES_DIR, f"{safe}.{stamp}.bak.md")
    shutil.copy2(path, bak)
    return bak


def _new_records(records: list[dict], markdown: str) -> list[dict]:
    """Records whose work-order number is not already cited in the checklist."""
    cited = set(re.findall(r"\d{2,7}", markdown or ""))
    out = []
    for r in records:
        wo = str(r.get("wo_id", "") or "").strip()
        if wo and wo not in cited:
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# Static
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    # The whole app is a single self-contained index.html. Serve it with
    # no-cache headers so browsers always pick up UI changes on refresh
    # (avoids stale sorting / layout after an update).
    resp = send_from_directory(WEBAPP_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


# --------------------------------------------------------------------------- #
# Company (top layer): stats for the whole company + a card per division
# --------------------------------------------------------------------------- #
def _division_summary(div_key: str) -> dict:
    """Aggregate stats for every department belonging to a division."""
    uns: list[dict] = []
    sch: list[dict] = []
    dept_count = 0
    machine_count = 0
    for key in DEPARTMENTS:
        if _DEPT_DIVISION.get(key) != div_key:
            continue
        dept_count += 1
        data = _DEPT_DATA.get(key, {"unscheduled": [], "scheduled": []})
        uns += data["unscheduled"]
        sch += data["scheduled"]
        machine_count += len(_EQUIP_BY_KEY.get(key, []))
    stats = _stats(uns, sch)
    stats.update({"department_count": dept_count, "machine_count": machine_count})
    return stats


@app.route("/api/company")
def api_company():
    """Company-wide KPIs plus one card per division. Only divisions that have
    at least one department are surfaced (currently just BLA)."""
    divisions = []
    all_uns: list[dict] = []
    all_sch: list[dict] = []
    seen = {"bla"}
    div_defs = {"bla": DIVISION["name"]}
    for d in store.list_divisions():
        div_defs[d["key"]] = d["name"]
        seen.add(d["key"])
    for div_key, name in div_defs.items():
        summ = _division_summary(div_key)
        if summ["department_count"] == 0 and div_key != "bla":
            continue  # hide empty user-added divisions
        summ.update({"key": div_key, "name": name})
        divisions.append(summ)
        for key in DEPARTMENTS:
            if _DEPT_DIVISION.get(key) == div_key:
                data = _DEPT_DATA.get(key, {"unscheduled": [], "scheduled": []})
                all_uns += data["unscheduled"]
                all_sch += data["scheduled"]
    return jsonify({
        "key": COMPANY["key"],
        "name": COMPANY["name"],
        "divisions": divisions,
        "totals": _stats(all_uns, all_sch),
    })


# --------------------------------------------------------------------------- #
# Division
# --------------------------------------------------------------------------- #
@app.route("/api/division")
@app.route("/api/divisions/<div_key>")
def api_division(div_key="bla"):
    depts = []
    all_uns: list[dict] = []
    all_sch: list[dict] = []
    for key, cfg in DEPARTMENTS.items():
        if _DEPT_DIVISION.get(key) != div_key:
            continue
        data = _DEPT_DATA[key]
        uns, sch = data["unscheduled"], data["scheduled"]
        all_uns += uns
        all_sch += sch
        stats = _stats(uns, sch)
        stats.update({
            "key": key,
            "name": cfg["name"],
            "label": cfg["label"],
            "machine_count": len(_EQUIP_BY_KEY.get(key, [])),
            "is_manual": bool(cfg.get("manual")),
        })
        depts.append(stats)
    div_name = DIVISION["name"] if div_key == "bla" else next(
        (d["name"] for d in store.list_divisions() if d["key"] == div_key), div_key.upper())
    inactive_depts = [
        {"key": r["item_key"], "label": r.get("name") or r["item_key"]}
        for r in store.list_inactive("department")
        if (r.get("division_key") or "bla") == div_key
    ]
    return jsonify({
        "key": div_key,
        "name": div_name,
        "departments": depts,
        "inactive_departments": inactive_depts,
        "totals": _stats(all_uns, all_sch),
    })


@app.route("/api/divisions/<div_key>/longevity")
def api_division_longevity(div_key):
    """Division-wide estimated-replacement-year table from the Excel workbook.
    User edits are merged in from longevity_edits.json; the original Excel file
    remains the source of truth for unedited fields."""
    return jsonify({
        "division": div_key,
        "items": lp.by_division(div_key),
    })


@app.route("/api/longevity/<div_key>/<item_num>", methods=["POST"])
def api_update_longevity(div_key, item_num):
    """Update editable fields for a single longevity row.

    Body: {"author": ..., "updates": {"asset_num": ..., "estimated_replacement_year": ..., ...}}
    Persisted edits override Excel values; blank values remove the edit overlay.
    """
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    updates = body.get("updates") or {}
    if not isinstance(updates, dict):
        return jsonify({"error": "updates must be an object"}), 400
    # department/machine disambiguate rows whose Item# collides with another
    # row's (blank Item# cells fall back to a per-sheet row number that isn't
    # globally unique) so an edit never lands on the wrong row.
    department = (body.get("department") or "").strip()
    machine = (body.get("machine") or "").strip()
    row = lp.apply_edit(div_key, item_num, updates, department=department, machine=machine)
    if row is None:
        return jsonify({"error": "row not found"}), 404
    return jsonify({"ok": True, "item": row})


# --------------------------------------------------------------------------- #
# Vendor & Utilities contacts (BLA division tab)
# --------------------------------------------------------------------------- #
VENDOR_CONTACTS_FILE = os.path.join(OUTPUT_DIR, "vendor_contacts.json")


def _seed_vendor_contacts_if_empty() -> None:
    """On first run, seed the vendor_contacts table from the CSV-derived JSON
    (generated by build_vendor_contacts.py). Afterwards the DB is authoritative."""
    try:
        if store.count_vendor_contacts() > 0:
            return
        with open(VENDOR_CONTACTS_FILE, encoding="utf-8") as f:
            rows = (json.load(f) or {}).get("vendors", [])
        n = store.seed_vendor_contacts(rows)
        if n:
            print(f"[vendor-contacts] seeded {n} contacts from {os.path.basename(VENDOR_CONTACTS_FILE)}")
    except (OSError, ValueError, AttributeError) as e:
        print(f"[vendor-contacts] seed skipped: {e}")


# Seed the editable vendor list from the CSV-derived JSON on first run.
_seed_vendor_contacts_if_empty()


def _toilet_machine_list() -> list[dict]:
    """Numeric eq_id + display name (nickname, when set) for every Toilet
    Partitions machine, for the 'assign to machine' picker and the machine-name
    chips on the Vendor tab."""
    nicks = store.list_nicknames("toilet")
    seen, out = set(), []
    for e in _EQUIP_BY_KEY.get("toilet", []):
        eq_id = _num_id(e.get("eq_id"))
        if not eq_id or eq_id in seen:
            continue
        seen.add(eq_id)
        pm_name = (e.get("equipment_name") or "").strip()
        name = (nicks.get(eq_id) or "").strip() or pm_name or f"EQ {eq_id}"
        out.append({"eq_id": eq_id, "name": name})
    out.sort(key=lambda m: int(m["eq_id"]) if m["eq_id"].isdigit() else 0)
    return out


def _vendors_for_machine(eq_id: str) -> list[dict]:
    """Vendor contacts assigned to a given machine (shown read-only on its
    Machine Info contact card)."""
    eq_id = _num_id(eq_id)
    out = []
    for v in store.list_vendor_contacts():
        if eq_id in (v.get("machine_eq") or []):
            out.append(v)
    return out


def _machine_contacts(dept_key: str) -> list[dict]:
    """Every contact card stored on a department's machines, each tagged with
    its machine so they can be listed together on the Vendor & Utilities tab."""
    nicks = store.list_nicknames(dept_key)
    names = {_num_id(e.get("eq_id")): (e.get("equipment_name") or "").strip()
             for e in _EQUIP_BY_KEY.get(dept_key, [])}
    out = []
    for m in store.list_machine_info(dept_key) if hasattr(store, "list_machine_info") else []:
        try:
            summary = json.loads(m.get("summary_json") or "{}")
        except ValueError:
            continue
        eq_id = _num_id(m.get("eq_id"))
        pm_name = names.get(eq_id) or ""
        machine_name = (nicks.get(eq_id) or "").strip() or pm_name or f"EQ {eq_id}"
        for contact_index, c in enumerate(summary.get("contacts") or []):
            if not isinstance(c, dict):
                continue
            # Skip junk rows (e.g. a stray "phone: 3") - require a name, company,
            # email, or a phone/cell with at least 7 digits.
            phone_digits = re.sub(r"\D", "", (c.get("phone") or "") + (c.get("cell") or ""))
            if not (c.get("name") or c.get("company") or c.get("email") or len(phone_digits) >= 7):
                continue
            out.append({
                "eq_id": eq_id,
                "contact_index": contact_index,
                "machine_name": machine_name,
                "pm_equipment_name": pm_name,
                "name": c.get("name", ""), "role": c.get("role", ""),
                "company": c.get("company", ""), "phone": c.get("phone", ""),
                "cell": c.get("cell", ""), "fax": c.get("fax", ""),
                "secondary": c.get("secondary", ""), "email": c.get("email", ""),
            })
    out.sort(key=lambda r: ((r["company"] or r["name"] or "").lower(), r["machine_name"].lower()))
    return out


@app.route("/api/vendor-contacts")
def api_vendor_contacts():
    """Vendor & Utilities Contacts tab: the editable vendor list (DB), every
    contact card that lives on a Toilet Partitions machine, and the machine
    list used by the 'assign to machine' picker."""
    return jsonify({
        "vendors": store.list_vendor_contacts(),
        "machine_contacts": _machine_contacts("toilet"),
        "machines": _toilet_machine_list(),
        "types": store.list_vendor_contact_types(),
        "editable": True,
        "protected": bool(EDIT_PASSWORD),
    })


@app.route("/api/vendor-contact-types", methods=["POST"])
def api_create_vendor_contact_type():
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    try:
        name = store.add_vendor_contact_type(body.get("name"), author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"name": name}), 201


@app.route("/api/vendor-contact-types/<path:name>", methods=["DELETE"])
def api_delete_vendor_contact_type(name):
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    try:
        store.delete_vendor_contact_type(name, author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/vendor-contacts", methods=["POST"])
def api_create_vendor_contact():
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    try:
        vc = store.add_vendor_contact(body, author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(vc), 201


@app.route("/api/vendor-contacts/<int:vc_id>", methods=["PATCH", "PUT"])
def api_update_vendor_contact(vc_id):
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    try:
        vc = store.update_vendor_contact(vc_id, body, author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if vc is None:
        return jsonify({"error": "contact not found"}), 404
    return jsonify(vc)


@app.route("/api/vendor-contacts/<int:vc_id>", methods=["DELETE"])
def api_delete_vendor_contact(vc_id):
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    if not store.delete_vendor_contact(vc_id, author):
        return jsonify({"error": "contact not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/departments/<dept_key>/machines/<eq_id>/contacts/<int:contact_index>", methods=["PATCH", "DELETE"])
def api_update_listed_machine_contact(dept_key, eq_id, contact_index):
    """Edit a contact that originates on an individual machine card, from the
    centralized Vendor & Utilities Contacts list."""
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    eq_id = _num_id(eq_id)
    info = store.get_machine_info(dept_key, eq_id)
    if not info:
        return jsonify({"error": "machine information not found"}), 404
    try:
        summary = json.loads(info.get("summary_json") or "{}")
    except ValueError:
        summary = {}
    contacts = summary.get("contacts") or []
    if contact_index < 0 or contact_index >= len(contacts) or not isinstance(contacts[contact_index], dict):
        return jsonify({"error": "contact not found"}), 404
    if request.method == "DELETE":
        contacts.pop(contact_index)
    else:
        keys = ("name", "role", "company", "phone", "cell", "fax", "secondary", "email")
        contacts[contact_index] = {k: str(body.get(k, contacts[contact_index].get(k, "")) or "").strip() for k in keys}
    summary["contacts"] = contacts
    store.set_machine_info(dept_key, eq_id, {"summary_json": json.dumps(summary, ensure_ascii=False)}, author)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Department
# --------------------------------------------------------------------------- #
@app.route("/api/departments/<dept_key>")
def api_department(dept_key):
    cfg = DEPARTMENTS.get(dept_key)
    if not cfg:
        return jsonify({"error": "department not found"}), 404

    data = _DEPT_DATA[dept_key]

    machines = []
    for mc in _dept_machines(dept_key):
        uns, sch = mc["recs"]["unscheduled"], mc["recs"]["scheduled"]
        stats = _stats(uns, sch)
        stats.update({
            "eq_id": mc["eq_id"],
            "eq_label": mc["eq_label"],
            "name": mc["name"],
            "pm_name": mc["pm_name"],
            "nickname": mc["nickname"],
            "group": mc["group"],
            "has_guide": mc["has_guide"],
            "has_critical_wo": _has_critical_wo(mc["recs"]),
            "all_closed": mc["all_closed"],
        })
        machines.append(stats)

    # Sort machines by most unscheduled work orders (most troublesome first).
    machines.sort(key=lambda m: (-m["unscheduled_count"], m["name"]))

    # Only advertise groups that actually contain machines, preserving order.
    present = {m["group"] for m in machines if m.get("group")}
    groups = [g for g in _group_order(dept_key) if g in present]

    inactive_machines = [
        {"eq_id": r.get("eq_id"), "name": r.get("name") or f"EQ ID {r.get('eq_id')}"}
        for r in store.list_inactive("machine")
        if r.get("dept_key") == dept_key
    ]

    return jsonify({
        "key": dept_key,
        "name": cfg["name"],
        "label": cfg["label"],
        "stats": _stats(data["unscheduled"], data["scheduled"]),
        "machines": machines,
        "groups": groups,
        "inactive_machines": inactive_machines,
        "unscheduled": _sorted_desc(data["unscheduled"], "date_notified"),
        "scheduled": _sorted_desc(data["scheduled"], "due_date"),
    })


@app.route("/api/departments/<dept_key>/longevity")
def api_department_longevity(dept_key):
    """Department-level estimated-replacement-year table from the Excel workbook.
    Rows are filtered to the requested department; editable details still live on
    the per-machine Machine Info tab."""
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    division_key = _DEPT_DIVISION.get(dept_key, "bla")
    return jsonify({
        "department": dept_key,
        "division": division_key,
        "items": lp.by_department(dept_key, division_key),
    })


@app.route("/api/workorders")
def api_workorders():
    """All departments' work orders (compact, newest -> oldest). Powers the
    home-page 'Unscheduled' / 'Scheduled' tabs that span the whole division."""
    with _DATA_LOCK:
        uns, sch = [], []
        for key, cfg in DEPARTMENTS.items():
            data = _DEPT_DATA.get(key, {"unscheduled": [], "scheduled": []})
            uns.extend(data["unscheduled"])
            sch.extend(data["scheduled"])
        return jsonify({
            "stats": _stats(uns, sch),
            "unscheduled": _sorted_desc(uns, "date_notified"),
            "scheduled": _sorted_desc(sch, "due_date"),
        })


# --------------------------------------------------------------------------- #
# Floor plan (per-department machine layout)
# --------------------------------------------------------------------------- #
# Items are positioned on a fixed logical canvas; the frontend scales it to fit
# the viewport. Editing (move/add/delete) is gated by the ADMIN_PASSWORD.
FLOORPLAN_CANVAS = {"w": 960, "h": 820}

# Default seed layout for the Toilet Partitions (TPF) floor. Each row is
# (label, machine_name | None, x, y, w, h). `machine_name` is resolved to a live
# EQ ID at request time by matching the equipment master; label-only boxes
# (None) are aisles/benches or equipment not tracked in MINT. This is only used
# until an admin saves a layout, after which the stored layout takes over.
_TPF_FLOORPLAN_DEFAULT = [
    # Full-width divider between the upper machines (Evolve return) and the
    # lower row (Holz-Her). A thin, label-less, unlinked item renders as a line.
    ("", None, 0, 430, 960, 3),
    ("1/2\" Edge Finisher & return", '1/2 " Edge Finisher, Solid, Technolegno/ Universal 280', 225, 15, 110, 175),
    ("Tenoner & return system", 'Tenoner A 517 Single End', 345, 15, 225, 80),
    ("LBDM", 'CNC Drilling Machine, Automatic Leveling Bar', 580, 15, 60, 55),
    ("Gannomat", 'Gannomat Index 330 Trend/PRO (Solid)', 575, 75, 65, 175),
    ("Holzma", 'Saw, Horizontal, Holzma', 695, 95, 95, 175),
    ("Rainbow", 'TLF Intellistore (Rainbow Stacking System)- TLF211', 820, 115, 45, 530),
    ("3/4\" Inserts", None, 345, 190, 210, 65),
    ("VLM", 'VLM Storage Lift -Small Hardware', 125, 260, 65, 50),
    ("Anderson", 'Router, CNC, Anderson Stratos/Nest TC+D', 205, 310, 65, 175),
    ("Evolve station", 'Evolve Double Head Drilling Machine', 695, 285, 95, 45),
    ("Evolve return system", None, 695, 350, 95, 50),
    ("Gannomat", 'Drilling Machine CNC, 1040 Laminate', 205, 470, 55, 170),
    ("Glue spreader", 'O-Sama (Joos) Glue Spreader', 345, 465, 135, 80),
    ("Heat rollers", 'Pinch Roller (Heated)', 345, 575, 135, 55),
    ("Edge trimmer & return system", None, 205, 665, 145, 95),
    ("Edgebander", 'Edgebander Homag 2520 Servo 6 Coil', 520, 460, 55, 170),
    ("Laminate slitter", 'Laminate Slitter', 610, 520, 55, 45),
    ("Clamp", None, 520, 635, 75, 40),
    ("Notching machine", 'Notching machine, 1540 Door', 360, 700, 50, 55),
    ("Steel core slitter", None, 425, 700, 60, 55),
    ("Holz-Her", 'Saw,Horizontal, Holz-Her', 695, 460, 95, 175),
    ("Vertical saw", "Saw, 10' Panel, Laminate Line", 695, 650, 95, 45),
]

_DEFAULT_FLOORPLANS = {"toilet": _TPF_FLOORPLAN_DEFAULT}


def _eqid_by_name(dept_key: str, name: str) -> str:
    """Resolve a machine display name to its numeric EQ ID for a department,
    tolerant of quote/spacing differences. Returns '' if no match."""
    if not name:
        return ""
    target = _norm_name(name)
    for e in _EQUIP_BY_KEY.get(dept_key, []):
        if _norm_name(e.get("equipment_name")) == target:
            return _num_id(e.get("eq_id"))
    return ""


def _default_floorplan(dept_key: str) -> list[dict]:
    rows = _DEFAULT_FLOORPLANS.get(dept_key)
    if not rows:
        return []
    out = []
    for i, (label, mname, x, y, w, h) in enumerate(rows):
        out.append({
            "id": f"seed-{i}",
            "eq_id": _eqid_by_name(dept_key, mname) if mname else "",
            "label": label,
            "x": x, "y": y, "w": w, "h": h, "z": i,
        })
    return out


def _floorplan_payload(dept_key: str) -> dict:
    with _DATA_LOCK:
        stored = store.list_floorplan(dept_key)
        items = stored if stored else _default_floorplan(dept_key)
        # Attach live per-machine stats + critical-WO flag for linked items so
        # the sidebar quick view and red highlight need no extra requests.
        wo_groups = _machine_groups(dept_key)
        name_by_eq = {
            _num_id(e.get("eq_id")): (e.get("equipment_name") or "").strip()
            for e in _EQUIP_BY_KEY.get(dept_key, [])
        }
        enriched = []
        for it in items:
            eq = _num_id(it.get("eq_id")) if it.get("eq_id") else ""
            entry = {
                "id": it.get("id"),
                "eq_id": eq,
                "label": it.get("label") or "",
                "x": it.get("x"), "y": it.get("y"),
                "w": it.get("w"), "h": it.get("h"),
                "z": it.get("z", 0),
                "machine_name": name_by_eq.get(eq, ""),
            }
            if eq:
                recs = wo_groups.get(eq, {"unscheduled": [], "scheduled": []})
                st = _stats(recs["unscheduled"], recs["scheduled"])
                entry["stats"] = {
                    "unscheduled_count": st["unscheduled_count"],
                    "scheduled_count": st["scheduled_count"],
                    "downtime_hours": st["downtime_hours"],
                    "labor_time": st["labor_time"],
                    "material_cost": st["material_cost"],
                }
                entry["has_critical_wo"] = _has_critical_wo(recs)
                entry["all_closed"] = _all_closed(recs)
                entry["has_guide"] = os.path.exists(_guide_path(eq))
            enriched.append(entry)
    return {
        "dept_key": dept_key,
        "canvas": FLOORPLAN_CANVAS,
        "items": enriched,
        "seeded": not bool(stored),
        "protected": bool(ADMIN_PASSWORD),
    }


@app.route("/api/departments/<dept_key>/floorplan", methods=["GET"])
def api_floorplan(dept_key):
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    return jsonify(_floorplan_payload(dept_key))


@app.route("/api/departments/<dept_key>/floorplan", methods=["POST"])
def api_save_floorplan(dept_key):
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    if not _admin_ok(request):
        return jsonify({"error": "Admin password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    items = body.get("items")
    if not isinstance(items, list):
        return jsonify({"error": "items must be a list"}), 400
    try:
        store.save_floorplan(dept_key, items, author)
    except (ValueError, TypeError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(_floorplan_payload(dept_key))


# --------------------------------------------------------------------------- #
# Machine
# --------------------------------------------------------------------------- #
@app.route("/api/departments/<dept_key>/machines/<eq_id>")
def api_machine(dept_key, eq_id):
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404

    eq_id = _num_id(eq_id)
    master = next((e for e in _EQUIP_BY_KEY.get(dept_key, [])
                   if _num_id(e.get("eq_id")) == eq_id), None)
    recs = _machine_groups(dept_key).get(eq_id)

    if master is None and recs is None:
        return jsonify({"error": "machine not found"}), 404

    recs = recs or {"unscheduled": [], "scheduled": []}
    uns, sch = recs["unscheduled"], recs["scheduled"]
    combined = uns + sch

    if master is not None:
        pm_name = (master.get("equipment_name") or "").strip() or "Unknown"
        eq_label = (master.get("eq_id") or f"EQ ID {eq_id}").strip()
    else:
        pm_name = _machine_name(combined)
        eq_label = _eq_label(combined, eq_id)

    nickname = store.get_nickname(dept_key, eq_id)
    name = nickname or pm_name

    return jsonify({
        "machine": {
            "eq_id": eq_id,
            "eq_label": eq_label,
            "name": name,
            "pm_name": pm_name,
            "nickname": nickname,
            "department": DEPARTMENTS[dept_key]["name"],
            "department_key": dept_key,
            "group": _group_for(dept_key, pm_name),
            "make": (master or {}).get("make", ""),
            "model": (master or {}).get("model", ""),
            "vendor": (master or {}).get("vendor", ""),
            "asset_num": (master or {}).get("asset_num", ""),
            "has_guide": os.path.exists(_guide_path(eq_id)),
        },
        "info": store.get_machine_info(dept_key, eq_id),
        "stats": _stats(uns, sch),
        "unscheduled": uns,
        "scheduled": sch,
    })


@app.route("/api/departments/<dept_key>/machines/<eq_id>/export/<kind>")
def api_machine_export(dept_key, eq_id, kind):
    """Download the unscheduled or scheduled work orders for one machine as
    CSV (default) or JSON.  kind must be 'unscheduled' or 'scheduled'."""
    if kind not in ("unscheduled", "scheduled"):
        return jsonify({"error": "kind must be 'unscheduled' or 'scheduled'"}), 400
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404

    eq_id = _num_id(eq_id)
    recs = _machine_groups(dept_key).get(eq_id, {}).get(kind, [])
    if not recs:
        return jsonify({"error": f"no {kind} work orders for this machine"}), 404

    fmt = (request.args.get("format") or "csv").lower()
    machine_name = (_machine_name(recs) or f"EQ_{eq_id}").replace(" ", "_")
    safe_name = re.sub(r"[^\w\-]+", "_", machine_name).strip("_") or f"EQ_{eq_id}"
    filename_base = f"{dept_key}_{safe_name}_{kind}"

    if fmt == "json":
        response = make_response(json.dumps({kind: recs}, indent=2, default=str))
        response.headers.set("Content-Type", "application/json")
        response.headers.set(
            "Content-Disposition", f'attachment; filename="{filename_base}.json"'
        )
        return response

    if fmt != "csv":
        return jsonify({"error": "format must be 'csv' or 'json'"}), 400

    import csv
    import io

    dataclass_type = WorkOrderDetail if kind == "unscheduled" else ScheduledWorkOrder
    base_fields = list(dataclass_type.__dataclass_fields__)
    extra_keys = set()
    for r in recs:
        extra_keys.update(r.keys())
    extra_keys -= set(base_fields)
    fieldnames = base_fields + sorted(extra_keys)

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for r in recs:
        writer.writerow({
            k: (json.dumps(r.get(k)) if isinstance(r.get(k), (list, dict))
                else (r.get(k) if r.get(k) is not None else ""))
            for k in fieldnames
        })

    response = make_response(output.getvalue())
    response.headers.set("Content-Type", "text/csv")
    response.headers.set(
        "Content-Disposition", f'attachment; filename="{filename_base}.csv"'
    )
    return response


# --------------------------------------------------------------------------- #
# Machine information / longevity (Machine Info tab)
# --------------------------------------------------------------------------- #
@app.route("/api/departments/<dept_key>/machines/<eq_id>/info", methods=["GET"])
def api_machine_info(dept_key, eq_id):
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    eq_id = _num_id(eq_id)
    info = store.get_machine_info(dept_key, eq_id) or {}
    # Vendor contacts assigned to this machine are shown read-only on the card;
    # they live in the central vendor list (edited on the Vendor & Utilities tab).
    info = dict(info)
    info["assigned_vendors"] = _vendors_for_machine(eq_id)
    # Overlay the nickname-aware display name so the Machine Info card matches
    # the rest of the app, and keep the original PM name as pm_equipment_name.
    if not info.get("equipment_name"):
        master = next((e for e in _EQUIP_BY_KEY.get(dept_key, []) if _num_id(e.get("eq_id")) == eq_id), None)
        info["equipment_name"] = (master.get("equipment_name") or "").strip() if master else ""
    nick = (store.get_nickname(dept_key, eq_id) or "").strip()
    if info.get("equipment_name"):
        info["pm_equipment_name"] = info["equipment_name"]
    if nick:
        info["equipment_name"] = nick
    return jsonify(info)


@app.route("/api/departments/<dept_key>/machines/<eq_id>/info", methods=["PATCH", "POST"])
def api_update_machine_info(dept_key, eq_id):
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    eq_id = _num_id(eq_id)
    fields = {k: body.get(k) for k in store.MACHINE_INFO_FIELDS if k in body}
    try:
        info = store.set_machine_info(dept_key, eq_id, fields, author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(info)


# --------------------------------------------------------------------------- #
# Machine nickname (admin-gated display name; the PM equipment name is kept)
# --------------------------------------------------------------------------- #
@app.route("/api/departments/<dept_key>/machines/<eq_id>/nickname", methods=["POST"])
def api_set_machine_nickname(dept_key, eq_id):
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    if not _admin_ok(request):
        return jsonify({"error": "Admin password required"}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required"}), 400
    eq_id = _num_id(eq_id)
    try:
        nickname = store.set_nickname(dept_key, eq_id, body.get("nickname") or "", author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # Update the shared in-memory records so the new display name shows up
    # everywhere immediately (work orders, weekly/monthly schedules, exports)
    # without waiting for the next full reload.
    with _DATA_LOCK:
        data = _DEPT_DATA.get(dept_key)
        if data:
            for kind in ("unscheduled", "scheduled"):
                for r in data.get(kind, []):
                    if _num_id(r.get("equipment_id")) != eq_id:
                        continue
                    pm = r.get("pm_equipment_name") or r.get("equipment_name") or ""
                    r["pm_equipment_name"] = pm
                    r["equipment_name"] = nickname or pm
    return jsonify({"ok": True, "nickname": nickname})


# --------------------------------------------------------------------------- #
# AI note sorting (Ollama): turn messy PM notes into structured categories
# --------------------------------------------------------------------------- #
def _build_notes_prompt(comment: str, machine_settings: str) -> str:
    """Prompt asking the LLM to sort free-form machine notes into categories."""
    return (
        "You are a data-cleaning assistant for a maintenance app. You are given "
        "free-form notes about ONE machine, split into a COMMENT field and a "
        "MACHINE SETTINGS field. Sort every piece of information into the correct "
        "category and return STRICT JSON.\n\n"
        "Return ONLY a JSON object (no markdown fences, no explanation) with "
        "exactly these keys:\n"
        "{\n"
        '  "contacts": [ { "name": "", "role": "", "company": "", "phone": "", '
        '"cell": "", "fax": "", "secondary": "", "email": "" } ],\n'
        '  "logins": [ { "label": "", "login": "", "password": "" } ],\n'
        '  "original_cost": "",\n'
        '  "purchase_date": "",\n'
        '  "notes": "",\n'
        '  "machine_settings": ""\n'
        "}\n\n"
        "Rules:\n"
        "- contacts: people/vendors that have a phone, cell, fax, or email. Split "
        "the name, role, company and each phone type into the right field. A phone "
        "labeled p/phone -> phone, c/cell -> cell, f/fax -> fax, extra numbers -> "
        "secondary. NEVER put a login or password into a contact.\n"
        "- logins: any machine or screen credentials (operator, administrator, "
        "tech screen, system admin, etc.). 'label' is which login it is; 'login' "
        "is the username/name; 'password' is the password. If only a password is "
        "given, leave login blank.\n"
        "- original_cost: the machine's purchase/original cost if stated. Include a "
        "leading $ and thousands separators (e.g. $240,375). If several costs are "
        "listed, use the primary machine cost and leave the rest in notes.\n"
        "- purchase_date: the installation/purchase date if stated, kept as written.\n"
        "- notes: everything that is genuinely a free-form note (instructions, "
        "accounting names, other costs/expenses, misc). Remove the pieces you "
        "already moved into contacts/logins/cost/date. Keep it readable.\n"
        "- machine_settings: only true machine configuration text that is NOT a "
        "login. If none, use an empty string.\n"
        "- Do not invent data. Use \"\" or [] when unknown. Output must be valid "
        "JSON and nothing else.\n\n"
        f"COMMENT:\n{comment or '(none)'}\n\n"
        f"MACHINE SETTINGS:\n{machine_settings or '(none)'}\n"
    )


def _extract_json(text: str):
    """Best-effort extraction of a JSON object from an LLM response."""
    if not text:
        return None
    s = text.strip()
    # Strip ```json ... ``` fences if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", s, re.DOTALL | re.IGNORECASE)
    if fence:
        s = fence.group(1).strip()
    # Fall back to the first { ... last }.
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        return json.loads(s[start:end + 1])
    except json.JSONDecodeError:
        return None


def _normalize_parsed_notes(data: dict) -> dict:
    """Coerce the LLM output into the exact shape the frontend expects."""
    contact_keys = ("name", "role", "company", "phone", "cell", "fax", "secondary", "email")
    contacts = []
    for c in (data.get("contacts") or []):
        if not isinstance(c, dict):
            continue
        row = {k: str(c.get(k) or "").strip() for k in contact_keys}
        if any(row[k] for k in ("phone", "cell", "fax", "secondary", "email")):
            contacts.append(row)
    logins = []
    for l in (data.get("logins") or []):
        if not isinstance(l, dict):
            continue
        row = {k: str(l.get(k) or "").strip() for k in ("label", "login", "password")}
        if row["login"] or row["password"]:
            logins.append(row)
    return {
        "contacts": contacts,
        "logins": logins,
        "original_cost": str(data.get("original_cost") or "").strip(),
        "purchase_date": str(data.get("purchase_date") or "").strip(),
        "notes": str(data.get("notes") or "").strip(),
        "machine_settings": str(data.get("machine_settings") or "").strip(),
    }


def _original_pm_notes(eq_id):
    """Return (comment, machine_settings) parsed straight from the ORIGINAL
    scraped PM Equipment Summary (pages/equipment/<eq_id>/dashboard.html).

    This is the source of truth for AI note sorting so that re-sorting a machine
    can never degrade earlier, already-cleaned text. Returns (None, None) when
    there is no scraped page for this machine (e.g. a user-added machine)."""
    path = os.path.join(EQUIPMENT_PAGES_DIR, str(eq_id), "dashboard.html")
    if not os.path.exists(path):
        return None, None
    try:
        from import_equipment_summary import parse_equipment_summary
        with open(path, encoding="utf-8") as f:
            summary = parse_equipment_summary(f.read())
        return (summary.get("comment") or ""), (summary.get("machine_settings") or "")
    except Exception:  # noqa: BLE001 - fall back to submitted text on any error
        return None, None


@app.route("/api/departments/<dept_key>/machines/<eq_id>/parse-notes", methods=["POST"])
def api_parse_machine_notes(dept_key, eq_id):
    """Use the configured LLM (Ollama Cloud) to sort free-form PM notes into
    structured contacts / logins / purchase info / notes for the Machine Info
    tab. Gated by the shared edit password, like other machine-info edits.

    The notes are read from the ORIGINAL PM scrape when available, so re-sorting
    always works from pristine data instead of previously-cleaned text."""
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    eq_id = _num_id(eq_id)
    body = request.get_json(silent=True) or {}
    comment = (body.get("comment") or "").strip()
    machine_settings = (body.get("machine_settings") or "").strip()
    # Prefer the pristine PM scrape as the source of truth.
    source = "submitted"
    orig_c, orig_m = _original_pm_notes(eq_id)
    if orig_c is not None or orig_m is not None:
        comment = (orig_c or "").strip()
        machine_settings = (orig_m or "").strip()
        source = "pm_original"
    if not comment and not machine_settings:
        return jsonify({"error": "Nothing to sort - the notes are empty."}), 400
    prompt = _build_notes_prompt(comment, machine_settings)
    try:
        raw = ae.analyze(prompt, MODEL)  # may raise SystemExit on failure
    except SystemExit as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001 - surface any transport error as 502
        return jsonify({"error": f"AI request failed: {e}"}), 502
    data = _extract_json(raw)
    if data is None:
        return jsonify({"error": "Could not parse the AI response.",
                        "raw": (raw or "")[:2000]}), 502
    result = _normalize_parsed_notes(data)
    result["source"] = source
    return jsonify(result)


# --------------------------------------------------------------------------- #
# Machine monthly trends (Trends tab)
# --------------------------------------------------------------------------- #
# Metrics surfaced per month. Unscheduled work orders are bucketed by
# date_notified and scheduled by due_date (matching the rest of MINT).
_TREND_METRICS = [
    "material_cost", "downtime_hours", "labor_time",
    "unscheduled_count", "unscheduled_completed_count",
    "scheduled_count", "scheduled_completed_count",
]


def _empty_month(month: int) -> dict:
    m = {k: 0 for k in _TREND_METRICS}
    m["month"] = month
    return m


def _trends_from_sources(sources) -> list[dict]:
    """Bucket work orders into per-year, per-month metrics.

    `sources` is an iterable of (records, date_field, count_key,
    completed_count_key). Every record adds to material/downtime/labor and the
    count_key; records marked Closed and Completed additionally add to
    completed_count_key. Returns a list of {year, months:[12], totals} sorted
    oldest -> newest, including only years that have at least one work order.
    Every year lists all 12 months (empty months are zero-filled)."""
    years: dict[int, list[dict]] = {}
    for records, date_field, count_key, completed_count_key in sources:
        for r in records:
            dt = ae._parse_date(r.get(date_field))
            if not dt:
                continue
            months = years.setdefault(dt.year, [_empty_month(i) for i in range(1, 13)])
            cell = months[dt.month - 1]
            cell["material_cost"] += ae._to_float(r.get("material_cost"))
            cell["downtime_hours"] += ae._to_float(r.get("downtime_hours"))
            cell["labor_time"] += ae._to_float(r.get("labor_time"))
            cell[count_key] += 1
            if _is_completed(r.get("status")):
                cell[completed_count_key] += 1

    out = []
    for year in sorted(years):
        months = years[year]
        for m in months:
            m["material_cost"] = round(m["material_cost"], 2)
            m["downtime_hours"] = round(m["downtime_hours"], 2)
            m["labor_time"] = round(m["labor_time"], 2)
        totals = {k: round(sum(m[k] for m in months), 2) for k in _TREND_METRICS}
        out.append({"year": year, "months": months, "totals": totals})
    return out


def _machine_trends(dept_key: str, eq_id: str) -> list[dict]:
    """Aggregate a single machine's work orders into per-year, per-month
    metrics, tracking both all and completed (Closed and Completed) counts."""
    recs = _machine_groups(dept_key).get(eq_id) or {"unscheduled": [], "scheduled": []}
    return _trends_from_sources([
        (recs["unscheduled"], "date_notified", "unscheduled_count", "unscheduled_completed_count"),
        (recs["scheduled"], "due_date", "scheduled_count", "scheduled_completed_count"),
    ])


def _department_trends(dept_key: str, group: str | None = None) -> list[dict]:
    """Aggregate a department's work orders into per-year, per-month metrics,
    tracking both all and completed (Closed and Completed) counts.

    When `group` is given, only work orders whose equipment belongs to that
    machine group (per the department's group config) are included."""
    data = _DEPT_DATA.get(dept_key)
    if data is None:
        return []

    def keep(r) -> bool:
        if group is None:
            return True
        pm_name = (r.get("pm_equipment_name") or r.get("equipment_name") or "").strip()
        return _group_for(dept_key, pm_name) == group

    uns = [r for r in data["unscheduled"] if keep(r)]
    sch = [r for r in data["scheduled"] if keep(r)]
    return _trends_from_sources([
        (uns, "date_notified", "unscheduled_count", "unscheduled_completed_count"),
        (sch, "due_date", "scheduled_count", "scheduled_completed_count"),
    ])


def _technician_trends(tech: str) -> list[dict]:
    """Per-technician monthly trends. All WOs assigned to the tech count toward
    the 'All' columns; completed WOs credited to the tech (via assigned_to or
    work_performed_by aliases) count toward the 'Completed' columns."""
    aliases = _TECH_ALIASES.get(tech, {tech.lower()})

    def belongs(r: dict) -> bool:
        assigned = (r.get("assigned_to") or "").strip().lower()
        if assigned and assigned in aliases:
            return True
        return _is_completed(r.get("status")) and _wo_credited_to(r, tech)

    uns = [r for r in _WO_INDEX.values()
           if belongs(r) and (r.get("wo_type") or "").lower() == "unscheduled"]
    sch = [r for r in _WO_INDEX.values()
           if belongs(r) and (r.get("wo_type") or "").lower() != "unscheduled"]
    return _trends_from_sources([
        (uns, "date_notified", "unscheduled_count", "unscheduled_completed_count"),
        (sch, "due_date", "scheduled_count", "scheduled_completed_count"),
    ])


def _tech_schedule(tech: str) -> dict:
    """Build the personal dashboard for one technician: completed WOs credited
    to them, upcoming assigned WOs, and monthly trends. ISO dates are included
    so the frontend can bucket entries into weeks reliably."""

    def _iso(v):
        d = ae._parse_date(v)
        return d.strftime("%Y-%m-%d") if d else None

    aliases = _TECH_ALIASES.get(tech, {tech.lower()})
    out = {"name": tech, "completed": [], "upcoming": []}
    with _DATA_LOCK:
        for rec in _WO_INDEX.values():
            done = _is_completed(rec.get("status"))
            if done:
                if not _wo_credited_to(rec, tech):
                    continue
                out["completed"].append({
                    "wo_id": rec.get("wo_id"),
                    "wo_type": rec.get("wo_type"),
                    "equipment_name": rec.get("equipment_name"),
                    "department": rec.get("department"),
                    "problem": rec.get("problem") or rec.get("audit_item"),
                    "labor_time": _num(rec.get("labor_time")),
                    "completed_datetime": rec.get("completed_datetime"),
                    "completed_iso": _iso(rec.get("completed_datetime")),
                })
            else:
                assignee = (rec.get("assigned_to") or "").strip().lower()
                if assignee not in aliases:
                    continue
                due = rec.get("due_date") or rec.get("date_notified")
                out["upcoming"].append({
                    "wo_id": rec.get("wo_id"),
                    "wo_type": rec.get("wo_type"),
                    "equipment_name": rec.get("equipment_name"),
                    "department": rec.get("department"),
                    "problem": rec.get("problem") or rec.get("audit_item"),
                    "status": rec.get("status"),
                    "due_date": due,
                    "due_iso": _iso(due),
                })
        out["trends"] = _technician_trends(tech)
    out["completed"].sort(
        key=lambda w: ae._parse_date(w.get("completed_datetime")) or datetime.min,
        reverse=True)
    out["upcoming"].sort(
        key=lambda w: ae._parse_date(w.get("due_date")) or datetime.max)
    return out


@app.route("/api/departments/<dept_key>/machines/<eq_id>/trends")
def api_machine_trends(dept_key, eq_id):
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    eq_id = _num_id(eq_id)
    return jsonify({
        "eq_id": eq_id,
        "metrics": _TREND_METRICS,
        "years": _machine_trends(dept_key, eq_id),
    })


@app.route("/api/departments/<dept_key>/trends")
def api_department_trends(dept_key):
    """Department-wide monthly trends. An optional ?group=<name> query param
    limits the aggregate to work orders whose equipment falls in that machine
    group (e.g. Toilet Partitions' "Machines")."""
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    group = (request.args.get("group") or "").strip() or None
    return jsonify({
        "metrics": _TREND_METRICS,
        "group": group,
        "years": _department_trends(dept_key, group),
    })


def _department_machine_trends(dept_key: str, group: str | None = None) -> list[dict]:
    """Per-machine, per-year totals for a department (optionally restricted to
    a machine group), used by the department-level Pareto chart to compare
    machines against each other."""
    seen = set()
    out = []
    for e in _EQUIP_BY_KEY.get(dept_key, []):
        eq_id = _num_id(e.get("eq_id"))
        if not eq_id or eq_id in seen:
            continue
        seen.add(eq_id)
        name = (e.get("equipment_name") or "").strip() or "Unknown"
        if group is not None and _group_for(dept_key, name) != group:
            continue
        out.append({
            "eq_id": eq_id,
            "name": name,
            "years": _machine_trends(dept_key, eq_id),
        })
    return out


@app.route("/api/departments/<dept_key>/machines-trends")
def api_department_machine_trends(dept_key):
    """Per-machine yearly totals for a department, for the department-level
    Pareto chart (machines compared against each other). Optional ?group=
    restricts to a single machine group (e.g. Toilet Partitions' "Machines")."""
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    group = (request.args.get("group") or "").strip() or None
    return jsonify({
        "metrics": _TREND_METRICS,
        "group": group,
        "machines": _department_machine_trends(dept_key, group),
    })


_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]


def _tag_month_records(unscheduled: list[dict], scheduled: list[dict],
                       year: int, month: int) -> list[dict]:
    """Tag work orders that fall in a given year/month by kind. Unscheduled
    bucket by date_notified, scheduled by due_date."""
    out = []
    for records, date_field, kind in (
        (unscheduled, "date_notified", "unscheduled"),
        (scheduled, "due_date", "scheduled"),
    ):
        for r in records:
            dt = ae._parse_date(r.get(date_field))
            if dt and dt.year == year and dt.month == month:
                out.append({"kind": kind, "date_field": date_field, "rec": r})
    return out


def _month_records(dept_key: str, eq_id: str, year: int, month: int) -> list[dict]:
    """A single machine's work orders that fall in a given year/month."""
    recs = _machine_groups(dept_key).get(eq_id) or {"unscheduled": [], "scheduled": []}
    return _tag_month_records(recs["unscheduled"], recs["scheduled"], year, month)


def _department_month_records(dept_key: str, group: str | None,
                              year: int, month: int) -> list[dict]:
    """A department's work orders (optionally limited to one machine group)
    that fall in a given year/month."""
    data = _DEPT_DATA.get(dept_key)
    if data is None:
        return []

    def keep(r) -> bool:
        if group is None:
            return True
        pm_name = (r.get("pm_equipment_name") or r.get("equipment_name") or "").strip()
        return _group_for(dept_key, pm_name) == group

    uns = [r for r in data["unscheduled"] if keep(r)]
    sch = [r for r in data["scheduled"] if keep(r)]
    return _tag_month_records(uns, sch, year, month)


def _month_synopsis_payload(label: str, year: int, month: int,
                            tagged: list[dict]) -> dict:
    """Build the month-synopsis JSON payload (totals + work orders + LLM
    synopsis) shared by the machine and department endpoints."""
    totals = {
        "material_cost": round(sum(ae._to_float(t["rec"].get("material_cost")) for t in tagged), 2),
        "downtime_hours": round(sum(ae._to_float(t["rec"].get("downtime_hours")) for t in tagged), 2),
        "labor_time": round(sum(ae._to_float(t["rec"].get("labor_time")) for t in tagged), 2),
        "unscheduled_count": sum(1 for t in tagged if t["kind"] == "unscheduled"),
        "unscheduled_completed_count": sum(
            1 for t in tagged
            if t["kind"] == "unscheduled" and _is_completed(t["rec"].get("status"))),
        "scheduled_count": sum(1 for t in tagged if t["kind"] == "scheduled"),
        "scheduled_completed_count": sum(
            1 for t in tagged
            if t["kind"] == "scheduled" and _is_completed(t["rec"].get("status"))),
    }
    work_orders = [
        {
            "wo_id": t["rec"].get("wo_id"),
            "wo_type": t["kind"],
            "date": t["rec"].get(t["date_field"]),
            "problem": t["rec"].get("problem"),
            "material_cost": t["rec"].get("material_cost"),
            "downtime_hours": t["rec"].get("downtime_hours"),
            "labor_time": t["rec"].get("labor_time"),
            "status": t["rec"].get("status"),
        }
        for t in tagged
    ]

    base = {"year": year, "month": month, "month_name": _MONTH_NAMES[month],
            "totals": totals, "work_orders": work_orders}

    if not tagged:
        base["synopsis"] = f"No work orders were recorded for {_MONTH_NAMES[month]} {year}."
        return base

    prompt = _build_month_synopsis_prompt(label, year, month, tagged, totals)
    base["synopsis"] = ae.analyze(prompt, MODEL)  # may raise SystemExit
    return base


def _build_month_synopsis_prompt(label, year, month, tagged, totals) -> str:
    compact = [
        {
            "wo_id": t["rec"].get("wo_id"),
            "type": t["kind"],
            "date": t["rec"].get(t["date_field"]),
            "completed": t["rec"].get("completed_datetime"),
            "urgency": t["rec"].get("urgency"),
            "status": t["rec"].get("status"),
            "problem": t["rec"].get("problem"),
            "material_cost": t["rec"].get("material_cost"),
            "labor_time": t["rec"].get("labor_time"),
            "downtime_hours": t["rec"].get("downtime_hours"),
            "work_performed_by": t["rec"].get("work_performed_by"),
            "comments": t["rec"].get("comments"),
        }
        for t in tagged
    ]
    return f"""You are a maintenance analyst producing a data readout for
{label}, {_MONTH_NAMES[month]} {year}. Write like a report, not a story. State
only numbers and their documented causes.

Use the 'problem' text for the symptom and the 'comments' text for what was done
/ what parts were replaced.

=== MONTH TOTALS ===
{json.dumps(totals, indent=2)}

=== WORK ORDERS THIS MONTH (JSON) ===
{json.dumps(compact, indent=2)}

Write concise Markdown with EXACTLY this structure:

1. A **summary** of 1-2 sentences. It MUST begin with the counts and figures,
   e.g. "3 unscheduled and 2 scheduled work orders; $70 material cost, 8 h
   downtime, 4.5 h labor." Then, if warranted, one sentence naming the single
   biggest driver. Nothing else.
2. A "**Key drivers**" bulleted list. One bullet per notable work order, each
   led by the bare WO number, the cost and/or downtime figures, and a short
   plain reason (e.g. "WO 19912 - $4,188, 6 h: spindle bearing replaced after
   the saw seized.").

HARD RULES - the report is rejected if any are broken:
- NEVER characterize the month or the machine. Banned openings and phrases
  include "it was a ___ month", "mixed month", "modest", "quiet", "busy",
  "slow", "dramatic", "negligible", "overall", "on the whole".
- Do NOT mention the machine name in the summary; it is already in the header.
- The first word of the summary must be a number.
- Cite bare WO numbers and exact dollar/hour figures from the data only.
- Do NOT invent parts, costs, or causes not supported by the text.
- No preamble, no closing remarks, no adjectives beyond what the facts require.
"""


def _parse_year_month(args):
    """Parse & validate year/month query params. Returns (year, month) or
    raises ValueError with a message."""
    try:
        year = int(args.get("year", ""))
        month = int(args.get("month", ""))
    except (TypeError, ValueError):
        raise ValueError("numeric 'year' and 'month' query params required")
    if not (1 <= month <= 12):
        raise ValueError("month must be 1-12")
    return year, month


@app.route("/api/departments/<dept_key>/machines/<eq_id>/month-synopsis")
def api_month_synopsis(dept_key, eq_id):
    """LLM synopsis of what drove a specific month's stats for a machine."""
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    try:
        year, month = _parse_year_month(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    eq_id = _num_id(eq_id)
    tagged = _month_records(dept_key, eq_id, year, month)
    recs = _machine_groups(dept_key).get(eq_id) or {"unscheduled": [], "scheduled": []}
    label = f"{_machine_name(recs['unscheduled'] + recs['scheduled'])} ({_eq_label(recs['unscheduled'] + recs['scheduled'], eq_id)})"
    try:
        return jsonify(_month_synopsis_payload(label, year, month, tagged))
    except SystemExit as e:  # analyze() calls sys.exit on failure
        return jsonify({"error": str(e)}), 502


@app.route("/api/departments/<dept_key>/month-synopsis")
def api_department_month_synopsis(dept_key):
    """LLM synopsis of what drove a month's stats across a department (or one
    machine group via ?group=)."""
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    try:
        year, month = _parse_year_month(request.args)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    group = (request.args.get("group") or "").strip() or None
    tagged = _department_month_records(dept_key, group, year, month)
    scope = group or DEPARTMENTS[dept_key]["name"]
    label = f"{scope} - {DEPARTMENTS[dept_key]['name']}"
    try:
        return jsonify(_month_synopsis_payload(label, year, month, tagged))
    except SystemExit as e:  # analyze() calls sys.exit on failure
        return jsonify({"error": str(e)}), 502


# --------------------------------------------------------------------------- #
# Work order detail
# --------------------------------------------------------------------------- #
def _attachments_payload(wo_id: str, rec: dict | None = None) -> list[dict]:
    out = []
    seen_urls = set()
    for a in store.list_attachments(wo_id):
        item = {
            "id": a["id"], "filename": a["filename"], "size": a["size"],
            "content_type": a["content_type"], "uploaded_by": a["uploaded_by"],
            "created_at": a["created_at"], "url": f"/api/attachments/{a['id']}",
        }
        out.append(item)
        seen_urls.add(item["url"])

    # Merge legacy/scraped attachment links (e.g. from the PM system) so the
    # detail view shows them even though they are not files stored in MINT.
    for a in (rec or {}).get("attachments") or []:
        if not isinstance(a, dict):
            continue
        url = (a.get("url") or a.get("link") or "").strip()
        name = (a.get("name") or a.get("filename") or "Attachment").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        out.append({
            "filename": name,
            "url": url,
            "content_type": a.get("content_type") or "",
            "uploaded_by": a.get("uploaded_by") or "",
            "size": a.get("size") or 0,
        })
    return out


def _wo_detail(rec: dict) -> dict:
    """Full work-order payload for the detail modal: the record (minus internal
    bookkeeping keys) plus its solution log, attachments and audit trail."""
    wid = str(rec.get("wo_id") or "").strip()
    d = {k: v for k, v in rec.items() if not k.startswith("_")}
    d["is_manual"] = bool(rec.get("is_manual"))
    d["solutions"] = store.list_solutions(wid)
    d["attachments"] = _attachments_payload(wid, rec)
    # NOTE: the change/version history (audit) is intentionally NOT included
    # here. It's password-gated behind /api/workorder/<id>/audit so it is only
    # returned to callers who supply the admin password.
    return d


def _author_from(req) -> str:
    if req.form:
        return (req.form.get("author") or "").strip()
    body = req.get_json(silent=True) or {}
    return (body.get("author") or "").strip()


def _inject_wo_live(rec: dict) -> None:
    """Add a freshly-created work order to the in-memory caches under lock so it
    shows up immediately without a full reload."""
    key = rec.get("department_key")
    kind = "scheduled" if rec.get("wo_type") == "scheduled" else "unscheduled"
    rec.setdefault("_sol_count", 0)
    rec.setdefault("_att_count", len(rec.get("attachments") or []))
    with _DATA_LOCK:
        _DEPT_DATA.setdefault(key, {"unscheduled": [], "scheduled": []})[kind].append(rec)
        _WO_INDEX[str(rec.get("wo_id"))] = rec


@app.route("/api/workorder/<wo_id>")
def api_workorder(wo_id):
    rec = _WO_INDEX.get(str(wo_id).strip())
    if rec is None:
        # It may exist in the store but not yet be in the cache (edge case).
        rec = store.get_work_order(str(wo_id).strip())
        if rec is None:
            return jsonify({"error": "work order not found"}), 404
    return jsonify(_wo_detail(rec))


# --------------------------------------------------------------------------- #
# Create / edit work orders (MINT as system of record)
# --------------------------------------------------------------------------- #
@app.route("/api/workorders", methods=["POST"])
def api_create_workorder():
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400

    dept_key = (body.get("department_key") or "").strip()
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "unknown department"}), 400
    wo_type = (body.get("wo_type") or "unscheduled").strip().lower()
    if wo_type not in ("scheduled", "unscheduled"):
        return jsonify({"error": "wo_type must be 'scheduled' or 'unscheduled'"}), 400

    fields = {
        "equipment_id": _num_id(body.get("equipment_id") or body.get("equipment_eq_id") or ""),
        "equipment_eq_id": (body.get("equipment_eq_id") or "").strip(),
        "equipment_name": (body.get("equipment_name") or "").strip(),
        "department": DEPARTMENTS[dept_key]["name"],
        "department_key": dept_key,
        "wo_type": wo_type,
        "status": (body.get("status") or "Pending").strip(),
        "labor_time": str(body.get("labor_time") or "0"),
        "material_cost": str(body.get("material_cost") or "0"),
        "downtime_hours": str(body.get("downtime_hours") or "0"),
        "work_performed_by": (body.get("work_performed_by") or "").strip(),
        "completed_datetime": _normalize_date(body.get("completed_datetime") or ""),
    }
    if wo_type == "unscheduled":
        # Default the notified date to today so the new WO sorts to the top
        # (and isn't lost) if the reporter leaves it blank.
        fields["date_notified"] = _normalize_date(body.get("date_notified") or "") \
            or datetime.now().strftime("%m/%d/%Y")
        fields["urgency"] = (body.get("urgency") or "").strip()
        fields["problem"] = (body.get("problem") or "").strip()
    else:
        fields["due_date"] = _normalize_date(body.get("due_date") or "")
        fields["audit_item"] = (body.get("audit_item") or "").strip()
        fields["frequency"] = (body.get("frequency") or "").strip()

    try:
        created = store.add_work_order(fields, author=author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    rec = created["data"]
    rec["department_key"] = dept_key
    rec["is_manual"] = True
    _inject_wo_live(rec)

    # Optional initial solution note (unscheduled).
    first_solution = (body.get("solution") or "").strip()
    if wo_type == "unscheduled" and first_solution:
        store.add_solution(rec["wo_id"], first_solution, author=author)
        rec["_sol_count"] = rec.get("_sol_count", 0) + 1

    email_result = None
    if wo_type == "unscheduled":
        rec2 = dict(rec)
        rec2["created_by"] = author
        email_result = emailer.notify_new_unscheduled(
            rec2, dept_label=DEPARTMENTS[dept_key]["label"])

    # For a recurring scheduled WO, reload so the auto-generated next occurrence
    # is immediately visible on the dashboard.
    if wo_type == "scheduled" and fields.get("frequency"):
        reload_data()

    return jsonify({"ok": True, "wo_id": rec["wo_id"],
                    "work_order": _wo_detail(rec), "email": email_result})


@app.route("/api/workorder/<wo_id>", methods=["PATCH", "PUT"])
def api_edit_workorder(wo_id):
    wo_id = str(wo_id).strip()
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400

    rec = _WO_INDEX.get(wo_id)
    if rec is None:
        return jsonify({"error": "work order not found"}), 404

    editable = {k: v for k, v in (body.get("fields") or {}).items()
                if k in store.WO_FIELDS}
    if not editable:
        return jsonify({"error": "no editable fields supplied"}), 400

    # Assignment is a separate, access-controlled action; don't allow it to be
    # smuggled through the generic edit endpoint.
    if "assigned_to" in editable and not _sean_ok(request):
        return jsonify({"error": "Only Sean can assign work orders to someone else."}), 403

    # Keep dates canonical so edited WOs stay correctly sorted.
    for df in ("date_notified", "due_date", "completed_datetime"):
        if df in editable:
            editable[df] = _normalize_date(editable[df])

    prev_completed = _is_completed(rec.get("status"))
    store.set_override(wo_id, editable, author=author)
    with _DATA_LOCK:
        rec.update(editable)

    # Fire the "Closed & Completed" email when the status transitions into a
    # completed state (any WO type). Only on the transition, so re-saving an
    # already-completed WO doesn't re-notify.
    email_result = None
    if _is_completed(rec.get("status")) and not prev_completed:
        dept_key = rec.get("department_key")
        dept_label = DEPARTMENTS.get(dept_key, {}).get("label", "") if dept_key else ""
        email_result = emailer.notify_completed(rec, dept_label=dept_label)

    return jsonify({"ok": True, "work_order": _wo_detail(rec), "email": email_result})


@app.route("/api/workorder/<wo_id>", methods=["DELETE"])
def api_delete_workorder(wo_id):
    """Delete a manually-created work order. Scraped PM records cannot be
    deleted (they're the read-only system-of-record snapshot). Gated by the
    admin password."""
    if not _admin_ok(request):
        return jsonify({"error": "Enter the admin password to delete work orders."}), 401
    wo_id = str(wo_id).strip()
    try:
        deleted = store.delete_work_order(wo_id)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not deleted:
        return jsonify({"error": "work order not found"}), 404
    # Drop it from the in-memory caches so it disappears immediately.
    with _DATA_LOCK:
        _WO_INDEX.pop(wo_id, None)
        for data in _DEPT_DATA.values():
            for kind in ("unscheduled", "scheduled"):
                lst = data.get(kind)
                if lst:
                    lst[:] = [r for r in lst if str(r.get("wo_id")) != wo_id]
    return jsonify({"ok": True, "deleted": wo_id})


@app.route("/api/workorder/<wo_id>/stop-recurrence", methods=["POST"])
def api_stop_recurrence(wo_id):
    """Stop a recurring scheduled-WO series so no further occurrences are ever
    generated. Existing occurrences are kept. Gated by the admin password."""
    if not _admin_ok(request):
        return jsonify({"error": "Enter the admin password to stop recurrence."}), 401
    wo_id = str(wo_id).strip()
    rec = _WO_INDEX.get(wo_id) or store.get_work_order(wo_id)
    if rec is None:
        return jsonify({"error": "work order not found"}), 404
    if (rec.get("wo_type") != "scheduled") or not (rec.get("frequency") or "").strip():
        return jsonify({"error": "this work order is not part of a recurring series"}), 400
    series_id = (rec.get("series_id") or wo_id).strip()
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    flagged = store.stop_recurrence(series_id, author=author)
    reload_data()  # rebuild caches so the "stopped" state is reflected everywhere
    return jsonify({"ok": True, "series_id": series_id, "stopped": flagged})


@app.route("/api/workorder/<wo_id>/solutions", methods=["POST"])
def api_add_solution(wo_id):
    wo_id = str(wo_id).strip()
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    text = (body.get("text") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not text:
        return jsonify({"error": "Solution text is required."}), 400
    if wo_id not in _WO_INDEX and store.get_work_order(wo_id) is None:
        return jsonify({"error": "work order not found"}), 404
    entry = store.add_solution(wo_id, text, author=author)
    rec = _WO_INDEX.get(wo_id)
    if rec is not None:
        rec["_sol_count"] = rec.get("_sol_count", 0) + 1
    return jsonify({"ok": True, "solution": entry,
                    "solutions": store.list_solutions(wo_id)})


@app.route("/api/workorder/<wo_id>/attachments", methods=["GET"])
def api_list_attachments(wo_id):
    wo_id = str(wo_id).strip()
    rec = _WO_INDEX.get(wo_id) or store.get_work_order(wo_id)
    return jsonify({"attachments": _attachments_payload(wo_id, rec)})


@app.route("/api/workorder/<wo_id>/attachments", methods=["POST"])
def api_upload_attachment(wo_id):
    wo_id = str(wo_id).strip()
    author = (request.form.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    files = request.files.getlist("file") or request.files.getlist("files")
    if not files:
        return jsonify({"error": "no file uploaded"}), 400
    if wo_id not in _WO_INDEX and store.get_work_order(wo_id) is None:
        return jsonify({"error": "work order not found"}), 404
    saved = []
    for f in files:
        data = f.read()
        meta = store.add_attachment(wo_id, f.filename, data,
                                    content_type=f.mimetype or "", author=author)
        saved.append(meta)
    rec = _WO_INDEX.get(wo_id)
    if rec is not None:
        rec["_att_count"] = rec.get("_att_count", 0) + len(saved)
    return jsonify({"ok": True, "attachments": _attachments_payload(wo_id, rec)})


@app.route("/api/attachments/<int:att_id>")
def api_download_attachment(att_id):
    a = store.get_attachment(att_id)
    if a is None or not os.path.exists(a["path"]):
        return jsonify({"error": "attachment not found"}), 404
    return send_from_directory(
        store.ATTACH_DIR, a["stored_name"],
        as_attachment=False, download_name=a["filename"])


@app.route("/api/workorder/<wo_id>/audit")
def api_wo_audit(wo_id):
    if not _admin_ok(request):
        return jsonify({"error": "Enter the admin password to view version history."}), 401
    return jsonify({"audit": store.list_audit(str(wo_id).strip())})


# --------------------------------------------------------------------------- #
# Work-order assignment (Sean -> anyone, technician -> self only)
# --------------------------------------------------------------------------- #
@app.route("/api/workorder/<wo_id>/assign", methods=["POST"])
def api_assign_workorder(wo_id):
    wo_id = str(wo_id).strip()
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    assigned_to = (body.get("assigned_to") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400

    rec = _WO_INDEX.get(wo_id)
    if rec is None:
        return jsonify({"error": "work order not found"}), 404

    sean = _sean_ok(request)
    if not sean:
        if not _is_technician(author):
            return jsonify({"error": "Only Sean or a technician can assign work orders."}), 403
        if not assigned_to:
            return jsonify({"error": "Only Sean can unassign a work order."}), 403
        if assigned_to.lower() != author.lower():
            return jsonify({"error": "You can only assign a work order to yourself."}), 403

    fields = {"assigned_to": assigned_to}
    current_status = (rec.get("status") or "").strip()
    if assigned_to:
        if current_status.lower() == "pending":
            fields["status"] = "Assigned"
    else:
        if current_status.lower() == "assigned":
            fields["status"] = "Pending"

    store.set_override(wo_id, fields, author=author)
    with _DATA_LOCK:
        rec.update(fields)
    return jsonify({"ok": True, "work_order": _wo_detail(rec)})


def _wo_credited_to(rec: dict, tech: str) -> bool:
    """True if a completed work order should be credited to `tech`. We match the
    assignee first (the new assignment feature), then fall back to the free-text
    'work performed by' and 'helpers' fields so historical/scraped completions
    and helper credits still count. Technician aliases (e.g. Gabriel -> shinobi,
    Primo -> pu) are normalized."""
    aliases = _TECH_ALIASES.get(tech, {tech.lower()})
    assigned = (rec.get("assigned_to") or "").strip().lower()
    if assigned and assigned in aliases:
        return True

    def _matches(text: str) -> bool:
        text = (text or "").lower()
        if not text:
            return False
        # Token match so "Shinobi, Max, Sean" credits Gabriel/Max but "Maximus" does not.
        # Strip surrounding punctuation so "M." or "pu)" still match.
        tokens = re.split(r"[,/&+]|\band\b|\s", text)
        cleaned = {re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", t.strip()).lower()
                   for t in tokens if t.strip()}
        return bool(aliases & cleaned)

    return _matches(rec.get("work_performed_by")) or _matches(rec.get("helpers"))


@app.route("/api/completed-counts")
def api_completed_counts():
    """Completed-work-order stats for the three maintenance technicians only
    (Shinobi, Primo, Max). For each we return the count, the list of completed
    work orders and the total labor time. Only Sean can view this dashboard."""
    if not _sean_ok(request):
        return jsonify({"error": "Enter Sean's password to view team completion stats."}), 401

    def _num(v):
        try:
            return float(str(v).strip() or 0)
        except (TypeError, ValueError):
            return 0.0

    techs = list(_TECH_ALIASES.keys())
    stats = {t: {"name": t, "completed": 0, "total_labor_hours": 0.0, "work_orders": []} for t in techs}
    with _DATA_LOCK:
        for rec in _WO_INDEX.values():
            if not _is_completed(rec.get("status")):
                continue
            for t in techs:
                if not _wo_credited_to(rec, t):
                    continue
                labor = _num(rec.get("labor_time"))
                stats[t]["completed"] += 1
                stats[t]["total_labor_hours"] += labor
                stats[t]["work_orders"].append({
                    "wo_id": rec.get("wo_id"),
                    "equipment_name": rec.get("equipment_name"),
                    "department": rec.get("department"),
                    "problem": rec.get("problem") or rec.get("audit_item"),
                    "labor_time": rec.get("labor_time"),
                    "completed_datetime": rec.get("completed_datetime"),
                })
                break  # credit each WO to a single technician
    for t in techs:
        stats[t]["work_orders"].sort(
            key=lambda w: ae._parse_date(w.get("completed_datetime")) or datetime.min,
            reverse=True)
        stats[t]["total_labor_hours"] = round(stats[t]["total_labor_hours"], 2)
    return jsonify({"counts": [stats[t] for t in techs]})


@app.route("/api/team-stats")
def api_team_stats():
    """Per-technician dashboard data for Sean: every COMPLETED work order
    credited to each tech (for the weekly schedule), their UPCOMING
    (not-yet-completed) assigned work orders (for the 'next week' view), and
    per-year monthly TRENDS with all 7 metrics. Dates are also returned in ISO
    form so the frontend can bucket them into weeks/months reliably. Sean-only."""
    if not _sean_ok(request):
        return jsonify({"error": "Enter Sean's password to view team stats."}), 401
    techs = list(_TECH_ALIASES.keys())
    return jsonify({"techs": [_tech_schedule(t) for t in techs]})


@app.route("/api/my-schedule", methods=["POST"])
def api_my_schedule():
    """Personal weekly schedule + trends for the signed-in technician.
    Requires the technician's name in the request body; only registered
    technicians may view their own schedule."""
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not _is_technician(author):
        return jsonify({"error": "Only registered technicians can view a personal schedule."}), 403
    return jsonify({"tech": _tech_schedule(author)})


# --------------------------------------------------------------------------- #
# Technician management (Sean-only)
# --------------------------------------------------------------------------- #
@app.route("/api/technicians", methods=["GET"])
def api_list_technicians():
    """List all active technicians (names only; aliases are Sean-only)."""
    names = [t["name"] for t in store.list_technicians(active_only=True)]
    return jsonify({"technicians": names})


@app.route("/api/technicians", methods=["POST"])
def api_add_technician():
    """Add a new technician with aliases."""
    if not _sean_ok(request):
        return jsonify({"error": "Enter Sean's password to manage technicians."}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    name = (body.get("name") or "").strip()
    aliases = body.get("aliases") or []
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not name:
        return jsonify({"error": "Technician name is required."}), 400
    if not isinstance(aliases, list):
        return jsonify({"error": "aliases must be a list."}), 400
    aliases = [str(a).strip() for a in aliases if str(a).strip()]
    try:
        tech = store.add_technician(name, aliases=aliases, author=author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    _load_technicians()  # refresh in-memory sets immediately
    return jsonify({"ok": True, "technician": tech})


@app.route("/api/technicians/<name>", methods=["DELETE"])
def api_delete_technician(name):
    """Delete a technician."""
    if not _sean_ok(request):
        return jsonify({"error": "Enter Sean's password to manage technicians."}), 401
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not store.delete_technician(name, author=author):
        return jsonify({"error": "Technician not found."}), 404
    _load_technicians()  # refresh in-memory sets immediately
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Create departments / machines
# --------------------------------------------------------------------------- #
@app.route("/api/departments", methods=["POST"])
def api_create_department():
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    name = (body.get("name") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not name:
        return jsonify({"error": "Department name is required."}), 400
    division_key = (body.get("division_key") or "bla").strip().lower()
    try:
        dept = store.add_department(name, division_key=division_key, author=author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    reload_data()  # rebuild caches so the new department is fully wired in
    return jsonify({"ok": True, "department": dept})


@app.route("/api/machines", methods=["POST"])
def api_create_machine():
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    name = (body.get("equipment_name") or "").strip()
    dept_key = (body.get("dept_key") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not name or not dept_key:
        return jsonify({"error": "Machine name and department are required."}), 400
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "unknown department"}), 400
    try:
        m = store.add_machine(
            name, dept_key,
            eq_id=(body.get("eq_id") or "").strip(),
            make=(body.get("make") or "").strip(),
            model=(body.get("model") or "").strip(),
            vendor=(body.get("vendor") or "").strip(),
            asset_num=(body.get("asset_num") or "").strip(),
            author=author,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    reload_data()
    return jsonify({"ok": True, "machine": m})


# --------------------------------------------------------------------------- #
# Deactivate / restore departments & machines (soft delete)
# --------------------------------------------------------------------------- #
@app.route("/api/departments/<dept_key>/deactivate", methods=["POST"])
def api_deactivate_department(dept_key):
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    cfg = DEPARTMENTS.get(dept_key)
    if not cfg:
        return jsonify({"error": "department not found"}), 404
    store.set_inactive(
        "department", dept_key,
        division_key=_DEPT_DIVISION.get(dept_key, "bla"),
        dept_key=dept_key, dept_label=cfg.get("label") or cfg.get("name", ""),
        name=cfg.get("label") or cfg.get("name", ""), author=author,
    )
    reload_data()
    return jsonify({"ok": True})


@app.route("/api/departments/<dept_key>/restore", methods=["POST"])
def api_restore_department(dept_key):
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    store.restore_inactive("department", dept_key, author=author)
    reload_data()
    return jsonify({"ok": True})


@app.route("/api/machines/deactivate", methods=["POST"])
def api_deactivate_machine():
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    dept_key = (body.get("dept_key") or "").strip()
    eq_id = str(body.get("eq_id") or "").strip()
    name = (body.get("name") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not dept_key or not eq_id:
        return jsonify({"error": "dept_key and eq_id are required."}), 400
    cfg = DEPARTMENTS.get(dept_key)
    if not cfg:
        return jsonify({"error": "department not found"}), 404
    store.set_inactive(
        "machine", f"{dept_key}:{eq_id}",
        division_key=_DEPT_DIVISION.get(dept_key, "bla"),
        dept_key=dept_key, dept_label=cfg.get("label") or cfg.get("name", ""),
        eq_id=eq_id, name=name or f"EQ ID {eq_id}", author=author,
    )
    reload_data()
    return jsonify({"ok": True})


@app.route("/api/machines/restore", methods=["POST"])
def api_restore_machine():
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    dept_key = (body.get("dept_key") or "").strip()
    eq_id = str(body.get("eq_id") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not dept_key or not eq_id:
        return jsonify({"error": "dept_key and eq_id are required."}), 400
    store.restore_inactive("machine", f"{dept_key}:{eq_id}", author=author)
    reload_data()
    return jsonify({"ok": True})


@app.route("/api/inactive")
def api_inactive():
    """Everything that has been soft-deleted, for the global Inactive/Archive
    page: deactivated departments and machines, each restorable."""
    depts = [{
        "key": r["item_key"],
        "label": r.get("name") or r["item_key"],
        "division_key": r.get("division_key") or "bla",
        "created_at": r.get("created_at"),
        "created_by": r.get("created_by"),
    } for r in store.list_inactive("department")]
    machines = [{
        "eq_id": r.get("eq_id"),
        "name": r.get("name") or (f"EQ ID {r.get('eq_id')}"),
        "dept_key": r.get("dept_key"),
        "dept_label": r.get("dept_label") or r.get("dept_key"),
        "division_key": r.get("division_key") or "bla",
        "created_at": r.get("created_at"),
        "created_by": r.get("created_by"),
    } for r in store.list_inactive("machine")]
    return jsonify({"departments": depts, "machines": machines})


# --------------------------------------------------------------------------- #
# Email notifications (structure; inert until configured + recipients added)
# --------------------------------------------------------------------------- #
@app.route("/api/email/status")
def api_email_status():
    return jsonify(emailer.status())


@app.route("/api/email/recipients", methods=["GET", "POST"])
def api_email_recipients():
    if request.method == "GET":
        return jsonify({"recipients": emailer.get_recipients()})
    body = request.get_json(silent=True) or {}
    recips = body.get("recipients")
    if not isinstance(recips, list):
        return jsonify({"error": "'recipients' must be a list of email addresses"}), 400
    return jsonify({"recipients": emailer.set_recipients(recips)})


@app.route("/api/email/test", methods=["POST"])
def api_email_test():
    """Send a sample 'Closed & Completed' email to one address to verify SMTP.
    Body: {"address": "someone@example.com"}."""
    body = request.get_json(silent=True) or {}
    address = (body.get("address") or "").strip()
    if not address:
        return jsonify({"error": "'address' is required"}), 400
    result = emailer.send_test(address)
    return jsonify({"ok": bool(result.get("sent")), "email": result,
                    "status": emailer.status()})


@app.route("/api/email/weekly-digest", methods=["POST"])
def api_email_weekly_digest():
    """Manually trigger the weekly scheduled-maintenance digest."""
    due, overdue = _weekly_digest_buckets()
    result = emailer.send_weekly_digest(due, overdue)
    return jsonify({"ok": True, "due_count": len(due),
                    "overdue_count": len(overdue), "email": result})


def _weekly_digest_buckets():
    """Scheduled work orders due in the current week vs. overdue (past due and
    not completed). Used by the weekly digest email."""
    _, this_sun, next_sun, _ = _week_starts()
    today = datetime.now().date()
    due, overdue = [], []
    with _DATA_LOCK:
        for key in DEPARTMENTS:
            data = _DEPT_DATA.get(key, {"scheduled": []})
            for r in data.get("scheduled", []):
                d = ae._parse_date(r.get("due_date"))
                if not d:
                    continue
                d = d.date()
                if _is_completed(r.get("status")):
                    continue
                if this_sun <= d < next_sun:
                    due.append(r)
                elif d < today:
                    overdue.append(r)
    return due, overdue


@app.route("/api/reload", methods=["POST"])
def api_reload():
    """Re-read the work-order + equipment files into the in-memory caches
    WITHOUT restarting the server. Useful right after a scrape (including the
    equipment-less 'orphan' unscheduled WOs) so the dashboard reflects the new
    data immediately."""
    ts = reload_data()
    return jsonify({
        "status": "reloaded",
        "reloaded_at": ts.isoformat(),
        "work_orders_indexed": len(_WO_INDEX),
    })


# --------------------------------------------------------------------------- #
# Checklist edit authentication (shared password)
# --------------------------------------------------------------------------- #
@app.route("/api/edit-auth")
def api_edit_auth_status():
    """Tell the frontend whether checklist editing is password-protected so it
    can decide whether to show the unlock prompt."""
    return jsonify({"protected": bool(EDIT_PASSWORD)})


@app.route("/api/verify-edit-password", methods=["POST"])
def api_verify_edit_password():
    """Validate the shared edit password (used to 'unlock' the edit controls)."""
    if not EDIT_PASSWORD:
        return jsonify({"ok": True, "protected": False})
    body = request.get_json(silent=True) or {}
    supplied = (body.get("password") or "").strip()
    if supplied == EDIT_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Incorrect password"}), 401


# --------------------------------------------------------------------------- #
# Admin authentication (deleting work orders + viewing version history)
# --------------------------------------------------------------------------- #
@app.route("/api/admin-auth")
def api_admin_auth_status():
    """Tell the frontend whether admin actions (delete WO / view history) are
    password-protected."""
    return jsonify({"protected": bool(ADMIN_PASSWORD)})


@app.route("/api/verify-admin-password", methods=["POST"])
def api_verify_admin_password():
    """Validate the admin password (used to 'unlock' delete + history)."""
    if not ADMIN_PASSWORD:
        return jsonify({"ok": True, "protected": False})
    body = request.get_json(silent=True) or {}
    supplied = (body.get("password") or "").strip()
    if supplied == ADMIN_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Incorrect password"}), 401


# --------------------------------------------------------------------------- #
# Sean authentication (assignment + completed-work-order dashboard)
# --------------------------------------------------------------------------- #
@app.route("/api/sean-auth")
def api_sean_auth_status():
    """Tell the frontend whether Sean's supervisor features are password-protected."""
    return jsonify({"protected": bool(SEAN_PASSWORD)})


@app.route("/api/verify-sean-password", methods=["POST"])
def api_verify_sean_password():
    """Validate Sean's password (used to unlock assignment + team stats)."""
    if not SEAN_PASSWORD:
        return jsonify({"ok": True, "protected": False})
    body = request.get_json(silent=True) or {}
    supplied = (body.get("password") or "").strip()
    if supplied == SEAN_PASSWORD:
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "Incorrect password"}), 401


# --------------------------------------------------------------------------- #
# Per-machine troubleshooting checklist (Ollama)
# --------------------------------------------------------------------------- #
@app.route("/api/departments/<dept_key>/machines/<eq_id>/guide")
def api_get_guide(dept_key, eq_id):
    path = _guide_path(eq_id)
    if not os.path.exists(path):
        return jsonify({"exists": False, "markdown": None})
    with open(path, encoding="utf-8") as f:
        return jsonify({
            "exists": True,
            "markdown": f.read(),
            "generated_at": os.path.getmtime(path),
        })


@app.route("/api/departments/<dept_key>/machines/<eq_id>/guide", methods=["POST"])
def api_generate_guide(dept_key, eq_id):
    # A full rebuild ("regenerate") is a heavier, riskier action, so it is gated
    # by the ADMIN password. First-time generation only needs the edit password.
    regen = str(request.args.get("regenerate") or "").strip() not in ("", "0", "false")
    if regen:
        if not _admin_ok(request):
            return jsonify({"error": "Admin password required to regenerate the checklist."}), 401
    elif not _edit_ok(request):
        return jsonify({"error": "Editing is locked. Enter the shared edit password."}), 401
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    groups = _machine_groups(dept_key)
    recs = groups.get(eq_id)
    if recs is None:
        return jsonify({"error": "machine not found"}), 404

    # Checklist is built ENTIRELY from the machine's unscheduled (breakdown)
    # work orders, per the requirements. guide_engine injects any recorded
    # operator edits so they survive a full regeneration.
    unscheduled = recs["unscheduled"]
    if not unscheduled:
        return jsonify({"error": "no unscheduled work orders to analyze for this machine"}), 400

    label = f"{_machine_name(unscheduled)} ({_eq_label(unscheduled, eq_id)})"
    try:
        markdown = ge.generate_guide(eq_id, unscheduled, label, MODEL)
    except SystemExit as e:  # analyze() calls sys.exit on failure
        return jsonify({"error": str(e)}), 502

    return jsonify({
        "exists": True,
        "markdown": markdown,
        "generated_at": os.path.getmtime(ge.guide_path(eq_id)),
    })


@app.route("/api/departments/<dept_key>/machines/<eq_id>/guide", methods=["PUT"])
def api_save_guide(dept_key, eq_id):
    """Save operator-edited Markdown for a machine's checklist."""
    if not _edit_ok(request):
        return jsonify({"error": "Editing is locked. Enter the shared edit password."}), 401
    body = request.get_json(silent=True) or {}
    markdown = body.get("markdown")
    if markdown is None:
        return jsonify({"error": "missing 'markdown'"}), 400
    # save_edit backs up the previous version AND records the manual-edit diff
    # into the persistent edit log so it is injected into all future prompts.
    author = (body.get("author") or "").strip()
    result = ge.save_edit(eq_id, markdown, author=author)
    return jsonify({
        "exists": True,
        "markdown": result["markdown"],
        "generated_at": result["generated_at"],
    })


@app.route("/api/departments/<dept_key>/machines/<eq_id>/guide/update", methods=["POST"])
def api_update_guide(dept_key, eq_id):
    """MERGE newly reported unscheduled work orders into the existing
    (operator-edited) checklist via the LLM, PRESERVING the human edits. The
    previous guide is backed up first so the merge is reversible. This is an
    additions-only merge (it never rewrites existing rows), so it is intentionally
    NOT password protected."""
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    groups = _machine_groups(dept_key)
    recs = groups.get(eq_id)
    if recs is None:
        return jsonify({"error": "machine not found"}), 404

    if not ge.guide_exists(eq_id):
        return jsonify({"error": "no existing checklist to update; generate one first"}), 400

    unscheduled = recs["unscheduled"]
    label = f"{_machine_name(unscheduled)} ({_eq_label(unscheduled, eq_id)})"
    try:
        result = ge.update_guide(eq_id, unscheduled, label, MODEL)
    except SystemExit as e:  # analyze() calls sys.exit on failure
        return jsonify({"error": str(e)}), 502

    return jsonify({
        "exists": True,
        "markdown": result["markdown"],
        "updated": result["updated"],
        "new_count": result["new_count"],
        "generated_at": os.path.getmtime(ge.guide_path(eq_id)),
    })


# --------------------------------------------------------------------------- #
# Checklist version history (SQLite-backed, see version_store.py)
# --------------------------------------------------------------------------- #
@app.route("/api/departments/<dept_key>/machines/<eq_id>/guide/versions")
def api_guide_versions(dept_key, eq_id):
    """List every stored version of a machine's checklist, newest first."""
    if not _admin_ok(request):
        return jsonify({"error": "Enter the admin password to view version history."}), 401
    versions = ge.list_versions(eq_id)
    current = versions[0]["version_number"] if versions else None
    return jsonify({"eq_id": eq_id, "current_version": current, "versions": versions})


@app.route("/api/departments/<dept_key>/machines/<eq_id>/guide/versions/<int:version_number>")
def api_guide_version(dept_key, eq_id, version_number):
    """Full content of a single historical version."""
    if not _admin_ok(request):
        return jsonify({"error": "Enter the admin password to view version history."}), 401
    v = ge.get_version(eq_id, version_number)
    if v is None:
        return jsonify({"error": "version not found"}), 404
    return jsonify(v)


@app.route("/api/departments/<dept_key>/machines/<eq_id>/guide/versions/<int:version_number>/restore",
           methods=["POST"])
def api_restore_guide_version(dept_key, eq_id, version_number):
    """Restore an older version as the live checklist. This creates a NEW
    version (tagged 'restore') rather than mutating history."""
    if not _edit_ok(request):
        return jsonify({"error": "Editing is locked. Enter the shared edit password."}), 401
    v = ge.get_version(eq_id, version_number)
    if v is None:
        return jsonify({"error": "version not found"}), 404
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    result = ge.save_edit(eq_id, v["content"], author=author, source="restore")
    return jsonify({
        "exists": True,
        "markdown": result["markdown"],
        "generated_at": result["generated_at"],
        "restored_from": version_number,
    })


# --------------------------------------------------------------------------- #
# Weekly work-order dashboard (Sunday -> Sunday weeks)
# --------------------------------------------------------------------------- #
def _week_starts(today: datetime | None = None):
    """Return (last_sun, this_sun, next_sun, week_after) as date objects.
    Weeks run Sunday 00:00 -> next Sunday 00:00."""
    today = (today or datetime.now()).date()
    # Python weekday(): Mon=0 .. Sun=6. Days since the most recent Sunday:
    since_sun = (today.weekday() + 1) % 7
    this_sun = today - timedelta(days=since_sun)
    return (this_sun - timedelta(days=7), this_sun,
            this_sun + timedelta(days=7), this_sun + timedelta(days=14))


def _wo_date(rec: dict):
    """The date that places a work order in a week: due_date for scheduled,
    date_notified for unscheduled."""
    raw = rec.get("due_date") if rec.get("wo_type") == "scheduled" else rec.get("date_notified")
    d = ae._parse_date(raw)
    return d.date() if d else None


def _weekly_payload(offset: int = 0, predicate=None) -> dict:
    """Build the standard {weeks, departments} weekly dashboard payload,
    optionally filtering each work order through `predicate(rec)`."""
    last_sun, this_sun, next_sun, week_after = _week_starts()
    base = this_sun + timedelta(weeks=offset)
    bounds = {
        "last": (base - timedelta(days=7), base),
        "this": (base, base + timedelta(days=7)),
        "next": (base + timedelta(days=7), base + timedelta(days=14)),
    }
    weeks_meta = {
        name: {"start": start.strftime("%Y-%m-%d"),
               "end": (end - timedelta(days=1)).strftime("%Y-%m-%d")}
        for name, (start, end) in bounds.items()
    }

    def _compact(r: dict) -> dict:
        return {
            "wo_id": r.get("wo_id"),
            "equipment_id": r.get("equipment_id"),
            "equipment_eq_id": r.get("equipment_eq_id"),
            "equipment_name": r.get("equipment_name"),
            "department_key": r.get("department_key"),
            "wo_type": r.get("wo_type"),
            "status": r.get("status"),
            "urgency": r.get("urgency"),
            "due_date": r.get("due_date"),
            "date_notified": r.get("date_notified"),
            "problem": r.get("problem"),
            "audit_item": r.get("audit_item"),
            "work_performed_by": r.get("work_performed_by"),
            "assigned_to": r.get("assigned_to"),
        }

    with _DATA_LOCK:
        departments = []
        for key, cfg in DEPARTMENTS.items():
            data = _DEPT_DATA.get(key, {"unscheduled": [], "scheduled": []})
            dept_buckets = {name: {"scheduled": [], "unscheduled": []} for name in bounds}
            for kind in ("scheduled", "unscheduled"):
                for r in data[kind]:
                    if predicate is not None and not predicate(r):
                        continue
                    d = _wo_date(r)
                    if not d:
                        continue
                    for name, (start, end) in bounds.items():
                        if start <= d < end:
                            dept_buckets[name][kind].append(_compact(r))
                            break
            departments.append({
                "key": key,
                "label": cfg["label"],
                "weeks": dept_buckets,
            })

    return {"weeks": weeks_meta, "departments": departments}


@app.route("/api/weekly")
def api_weekly():
    """Per-department scheduled + unscheduled work orders bucketed into three
    consecutive Sunday->Sunday weeks. By default these are last/this/next week,
    but `offset` (in whole weeks from the current Sunday) shifts the view so the
    frontend can page through earlier/upcoming weeks just like the monthly
    calendar."""
    try:
        offset = int((request.args.get("offset") or "0").strip() or "0")
    except (TypeError, ValueError):
        offset = 0
    return jsonify(_weekly_payload(offset))


def _tech_wo_predicate(rec: dict, tech: str) -> bool:
    """True if a work order belongs on a technician's personal weekly view:
    completed WOs credited to the tech, or open WOs assigned to the tech."""
    aliases = _TECH_ALIASES.get(tech, {tech.lower()})
    if _is_completed(rec.get("status")):
        return _wo_credited_to(rec, tech)
    return (rec.get("assigned_to") or "").strip().lower() in aliases


@app.route("/api/my-weekly", methods=["POST"])
def api_my_weekly():
    """Weekly dashboard scoped to the signed-in technician. Accepts the tech's
    name as `author` and an optional `offset` (weeks from the current Sunday)."""
    body = request.get_json(silent=True) or {}
    author = (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not _is_technician(author):
        return jsonify({"error": "Only registered technicians can view a personal schedule."}), 403
    try:
        offset = int(str(body.get("offset") or "0").strip() or "0")
    except (TypeError, ValueError):
        offset = 0
    return jsonify(_weekly_payload(offset, lambda r: _tech_wo_predicate(r, author)))


@app.route("/api/team-weekly", methods=["GET"])
def api_team_weekly():
    """Weekly dashboard for every active technician. Sean-only."""
    if not _sean_ok(request):
        return jsonify({"error": "Enter Sean's password to view the team schedule."}), 401
    try:
        offset = int(str(request.args.get("offset") or "0").strip() or "0")
    except (TypeError, ValueError):
        offset = 0
    techs = list(_TECH_ALIASES.keys())
    return jsonify({
        "techs": [
            {
                "name": t,
                **_weekly_payload(offset, lambda r: _tech_wo_predicate(r, t)),
            }
            for t in techs
        ]
    })


# --------------------------------------------------------------------------- #
# Monthly calendar (whole-month grid, Sunday-aligned)
# --------------------------------------------------------------------------- #
@app.route("/api/monthly")
def api_monthly():
    """Scheduled (and unscheduled) work orders for a target month, returned as a
    flat list tagged with an ISO `date` for the frontend calendar grid. Recurring
    PM occurrences are PROJECTED forward within the visible grid even when they
    have not been materialized as records yet (the recurring generator only
    creates occurrences through the next upcoming one), so the calendar shows
    every upcoming occurrence in the month. Projected entries have wo_id=None and
    is_projected=True. The grid is Sunday-aligned and covers whole weeks so the
    frontend can render a clean month view."""
    now = datetime.now()

    def _int(name, default):
        try:
            return int(request.args.get(name) or default)
        except (TypeError, ValueError):
            return default

    year = _int("year", now.year)
    month = _int("month", now.month)
    if month < 1 or month > 12:
        year, month = now.year, now.month

    first = datetime(year, month, 1)
    next_first = datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    last = next_first - timedelta(days=1)
    # Sunday index for a date: (weekday()+1)%7 -> Sun=0 .. Sat=6.
    grid_start = first - timedelta(days=(first.weekday() + 1) % 7)
    grid_end = last + timedelta(days=(6 - (last.weekday() + 1) % 7))
    gs, ge_ = grid_start.date(), grid_end.date()

    def _compact(r, date_iso, projected=False):
        return {
            "wo_id": None if projected else r.get("wo_id"),
            "equipment_id": r.get("equipment_id"),
            "equipment_eq_id": r.get("equipment_eq_id"),
            "equipment_name": r.get("equipment_name"),
            "department_key": r.get("department_key"),
            "wo_type": "scheduled" if projected else r.get("wo_type"),
            "status": "Pending" if projected else r.get("status"),
            "urgency": r.get("urgency"),
            "due_date": r.get("due_date"),
            "date_notified": r.get("date_notified"),
            "problem": r.get("problem"),
            "audit_item": r.get("audit_item"),
            "frequency": r.get("frequency"),
            "date": date_iso,
            "is_projected": projected,
        }

    work_orders = []
    with _DATA_LOCK:
        departments = [{"key": k, "label": c["label"]} for k, c in DEPARTMENTS.items()]
        for key, cfg in DEPARTMENTS.items():
            data = _DEPT_DATA.get(key, {"unscheduled": [], "scheduled": []})

            # Unscheduled work: placed by date_notified (only past/current days).
            for r in data["unscheduled"]:
                d = ae._parse_date(r.get("date_notified"))
                if d and gs <= d.date() <= ge_:
                    work_orders.append(_compact(r, d.strftime("%Y-%m-%d")))

            # Scheduled work: emit real records in-window, then project future
            # recurring occurrences forward so the whole month is populated.
            series: dict[str, list[dict]] = {}
            for r in data["scheduled"]:
                sid = (r.get("series_id") or r.get("wo_id") or "").strip() or f"_{id(r)}"
                series.setdefault(sid, []).append(r)

            for sid, occs in series.items():
                real_dates = set()
                seed_date = None
                frequency = ""
                stopped = False
                for o in occs:
                    d = _parse_mdy(o.get("due_date"))
                    if not d:
                        continue
                    if seed_date is None or d < seed_date:
                        seed_date = d
                    if (o.get("frequency") or "").strip():
                        frequency = (o.get("frequency") or "").strip()
                    if o.get("recurrence_stopped"):
                        stopped = True
                    if gs <= d.date() <= ge_:
                        real_dates.add(d.date())
                        work_orders.append(_compact(o, d.strftime("%Y-%m-%d")))

                if not frequency or stopped or seed_date is None:
                    continue  # one-off scheduled WO or stopped series: no projection

                template = max(occs, key=lambda o: (_parse_mdy(o.get("due_date")) or datetime.min))
                cur = seed_date
                ff = 0
                while cur.date() < gs and ff < 100000:  # fast-forward to the window
                    nxt = _add_interval(cur, frequency)
                    if nxt is None:
                        break
                    cur = nxt
                    ff += 1
                cg = 0
                while cur.date() <= ge_ and cg < 400:
                    if gs <= cur.date() <= ge_ and cur.date() not in real_dates:
                        proj = dict(template)
                        proj["due_date"] = cur.strftime("%m/%d/%Y")
                        work_orders.append(_compact(proj, cur.strftime("%Y-%m-%d"), projected=True))
                    nxt = _add_interval(cur, frequency)
                    if nxt is None:
                        break
                    cur = nxt
                    cg += 1

    calendar_events = store.list_calendar_events(
        date_from=grid_start.strftime("%Y-%m-%d"),
        date_to=grid_end.strftime("%Y-%m-%d"),
    )
    events = [
        {
            "id": e["id"],
            "date": e["date"],
            "department_key": e["department_key"] or "",
            "equipment_id": e["equipment_id"] or "",
            "title": e["title"],
            "description": e["description"] or "",
            "created_by": e["created_by"] or "",
            "created_at": e["created_at"] or "",
            "is_event": True,
        }
        for e in calendar_events
    ]

    return jsonify({
        "year": year,
        "month": month,
        "month_label": first.strftime("%B %Y"),
        "grid_start": grid_start.strftime("%Y-%m-%d"),
        "grid_end": grid_end.strftime("%Y-%m-%d"),
        "today": now.strftime("%Y-%m-%d"),
        "departments": departments,
        "work_orders": work_orders,
        "events": events,
    })


# --------------------------------------------------------------------------- #
# Manual calendar events (non-work-order items)
# --------------------------------------------------------------------------- #
@app.route("/api/calendar-events", methods=["GET"])
def api_list_calendar_events():
    """List manual calendar events within an optional date range."""
    dept_key = (request.args.get("dept_key") or "").strip()
    date_from = (request.args.get("date_from") or "").strip()
    date_to = (request.args.get("date_to") or "").strip()
    rows = store.list_calendar_events(
        department_key=dept_key,
        date_from=date_from,
        date_to=date_to,
    )
    return jsonify({"events": rows})


@app.route("/api/calendar-events", methods=["POST"])
def api_create_calendar_event():
    """Create a manual calendar event. Requires the edit password."""
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = _author_from(request)
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    try:
        event = store.add_calendar_event(body, author=author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"event": event}), 201


@app.route("/api/calendar-events/<int:event_id>", methods=["DELETE"])
def api_delete_calendar_event(event_id):
    """Delete a manual calendar event. Requires the edit password."""
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = _author_from(request) or (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not store.delete_calendar_event(event_id, author=author):
        return jsonify({"error": "event not found"}), 404
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Chart events (global timeline markers shown as vertical lines on every chart)
# --------------------------------------------------------------------------- #
@app.route("/api/chart-events", methods=["GET"])
def api_list_chart_events():
    """Every global chart event. Shared across all trend charts."""
    return jsonify({"events": store.list_chart_events(), "protected": bool(EDIT_PASSWORD)})


@app.route("/api/chart-events", methods=["POST"])
def api_create_chart_event():
    """Create a global chart event. Requires the edit password."""
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = _author_from(request)
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    try:
        event = store.add_chart_event(body, author=author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"event": event}), 201


@app.route("/api/chart-events/<int:event_id>", methods=["DELETE"])
def api_delete_chart_event(event_id):
    """Delete a global chart event. Requires the edit password."""
    if not _edit_ok(request):
        return jsonify({"error": "Edit password required"}), 401
    body = request.get_json(silent=True) or {}
    author = _author_from(request) or (body.get("author") or "").strip()
    if not author:
        return jsonify({"error": "Your name is required."}), 400
    if not store.delete_chart_event(event_id, author=author):
        return jsonify({"error": "event not found"}), 404
    return jsonify({"ok": True})


def _weekly_digest_scheduler():
    """Background loop that sends the weekly scheduled-maintenance digest once a
    week (default Monday 07:00 local time). The digest lists scheduled work due
    the coming week PLUS any past-due scheduled work still pending.

    Controlled by env vars:
        WEEKLY_DIGEST_ENABLED  "0" to disable (default on)
        WEEKLY_DIGEST_DAY      0=Mon .. 6=Sun (default 0)
        WEEKLY_DIGEST_HOUR     0-23 local hour (default 7)

    Emails are inert unless SMTP is configured + recipients exist, so this is
    safe to run by default. A tiny state file prevents double-sends across
    restarts within the same day."""
    if os.environ.get("WEEKLY_DIGEST_ENABLED", "1").strip() == "0":
        print("[digest] weekly digest scheduler disabled (WEEKLY_DIGEST_ENABLED=0)")
        return
    try:
        day = int(os.environ.get("WEEKLY_DIGEST_DAY", "0") or 0)
        hour = int(os.environ.get("WEEKLY_DIGEST_HOUR", "7") or 7)
    except ValueError:
        day, hour = 0, 7
    state_path = os.path.join(emailer.DATA_DIR, "weekly_digest_state.json")

    def _last_sent() -> str:
        try:
            with open(state_path, encoding="utf-8") as f:
                return json.load(f).get("last_sent_date", "")
        except (OSError, json.JSONDecodeError):
            return ""

    def _mark(date_str: str):
        try:
            with open(state_path, "w", encoding="utf-8") as f:
                json.dump({"last_sent_date": date_str}, f)
        except OSError as e:
            print(f"[digest] could not persist state: {e}")

    print(f"[digest] weekly digest scheduler running (day={day}, hour={hour})")
    while True:
        now = datetime.now()
        today = now.date().isoformat()
        if now.weekday() == day and now.hour >= hour and _last_sent() != today:
            try:
                due, overdue = _weekly_digest_buckets()
                res = emailer.send_weekly_digest(due, overdue)
                print(f"[digest] weekly digest fired: due={len(due)} "
                      f"overdue={len(overdue)} sent={res.get('sent')}")
            except Exception as e:  # noqa: BLE001 - never kill the loop
                print(f"[digest] error sending weekly digest: {e}")
            _mark(today)
        time.sleep(300)  # check every 5 minutes


def _start_weekly_digest():
    t = threading.Thread(target=_weekly_digest_scheduler, daemon=True,
                         name="weekly-digest")
    t.start()


# --------------------------------------------------------------------------- #
# Nightly full rescrape scheduler
# --------------------------------------------------------------------------- #
def _nightly_scheduler(stop_event: threading.Event):
    """Daemon thread that runs nightly_update.run() once per day at the
    configured local time (default 02:00)."""
    if os.environ.get("NIGHTLY_ENABLED", "1").strip() == "0":
        print("[nightly] scheduler disabled (NIGHTLY_ENABLED=0)")
        return
    try:
        hour = int(os.environ.get("NIGHTLY_HOUR", "2") or 2)
        minute = int(os.environ.get("NIGHTLY_MINUTE", "0") or 0)
    except ValueError:
        hour, minute = 2, 0
    print(f"[nightly] scheduler running (daily at {hour:02d}:{minute:02d} local time)")

    while not stop_event.is_set():
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()

        if stop_event.wait(timeout=wait_seconds):
            break

        _NIGHTLY_LAST_STATUS["running"] = True
        print(f"[nightly] starting scheduled cycle at {datetime.now().isoformat()}")
        try:
            summary = nightly_update.run(reload_callback=reload_data)
            _NIGHTLY_LAST_STATUS["last_run"] = datetime.now().isoformat()
            _NIGHTLY_LAST_STATUS["last_summary"] = summary
            print(f"[nightly] scheduled cycle finished: {summary.get('status')}")
        except Exception as e:  # noqa: BLE001 - never kill the loop
            print(f"[nightly] scheduled cycle failed: {e}")
            _NIGHTLY_LAST_STATUS["last_run"] = datetime.now().isoformat()
            _NIGHTLY_LAST_STATUS["last_summary"] = {"status": "failed", "error": str(e)}
        finally:
            _NIGHTLY_LAST_STATUS["running"] = False


def _start_nightly():
    global _NIGHTLY_THREAD, _NIGHTLY_STOP_EVENT
    if _NIGHTLY_THREAD is not None and _NIGHTLY_THREAD.is_alive():
        return
    _NIGHTLY_STOP_EVENT = threading.Event()
    _NIGHTLY_THREAD = threading.Thread(
        target=_nightly_scheduler,
        args=(_NIGHTLY_STOP_EVENT,),
        daemon=True,
        name="nightly",
    )
    _NIGHTLY_THREAD.start()


# --------------------------------------------------------------------------- #
# Spare parts inventory
# --------------------------------------------------------------------------- #
@app.route("/api/spare-parts", methods=["GET"])
def api_spare_parts():
    """Global spare-parts list, newest first."""
    return jsonify({"parts": store.list_spare_parts()})


@app.route("/api/spare-parts/options", methods=["GET"])
def api_spare_part_options():
    """All departments + machines for the 'for what machine' dropdown, plus
    divisions for the division filter/selector. Uses _dept_machines so that any
    admin-set nicknames are the displayed name."""
    out = []
    for key, cfg in DEPARTMENTS.items():
        machines = []
        for m in _dept_machines(key):
            eq_id = _num_id(m.get("eq_id"))
            if not eq_id:
                continue
            machines.append({
                "eq_id": eq_id,
                "name": m.get("name") or m.get("pm_name") or "",
                "pm_name": m.get("pm_name") or "",
                "nickname": m.get("nickname") or "",
            })
        if machines:
            out.append({
                "dept_key": key,
                "dept_label": cfg.get("label") or cfg.get("name") or key,
                "division_key": _DEPT_DIVISION.get(key) or "bla",
                "machines": machines,
            })
    divisions = [{"key": d["key"], "name": d["name"]} for d in store.list_divisions()]
    if not any(d["key"] == "bla" for d in divisions):
        divisions.insert(0, {"key": "bla", "name": DIVISION["name"]})
    return jsonify({"departments": out, "divisions": divisions})


@app.route("/api/spare-parts", methods=["POST"])
def api_create_spare_part():
    if not _admin_ok(request):
        return jsonify({"error": "Admin password required"}), 401
    body = request.get_json(silent=True) or {}
    author = str(body.get("author") or "").strip()
    try:
        part = store.add_spare_part(body, author=author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(part), 201


@app.route("/api/spare-parts/<part_id>", methods=["GET"])
def api_get_spare_part(part_id):
    part = store.get_spare_part(part_id)
    if not part:
        return jsonify({"error": "spare part not found"}), 404
    return jsonify(part)


@app.route("/api/spare-parts/<part_id>", methods=["PATCH", "PUT"])
def api_update_spare_part(part_id):
    if not _admin_ok(request):
        return jsonify({"error": "Admin password required"}), 401
    body = request.get_json(silent=True) or {}
    author = str(body.get("author") or "").strip()
    try:
        part = store.update_spare_part(part_id, body, author=author)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not part:
        return jsonify({"error": "spare part not found"}), 404
    return jsonify(part)


@app.route("/api/spare-parts/<part_id>", methods=["DELETE"])
def api_delete_spare_part(part_id):
    if not _admin_ok(request):
        return jsonify({"error": "Admin password required"}), 401
    body = request.get_json(silent=True) or {}
    author = str(body.get("author") or "").strip()
    if not store.delete_spare_part(part_id, author=author):
        return jsonify({"error": "spare part not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/departments/<dept_key>/machines/<eq_id>/spare-parts", methods=["GET"])
def api_machine_spare_parts(dept_key, eq_id):
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    return jsonify({"parts": store.list_spare_parts_for_machine(dept_key, eq_id)})


# --------------------------------------------------------------------------- #
# Nightly scrape status / manual trigger
# --------------------------------------------------------------------------- #
@app.route("/api/nightly/status", methods=["GET"])
def api_nightly_status():
    """Return the last nightly run summary and whether a run is in progress."""
    payload = dict(_NIGHTLY_LAST_STATUS)
    payload["scheduled"] = os.environ.get("NIGHTLY_ENABLED", "1").strip() != "0"
    return jsonify(payload)


@app.route("/api/nightly/run", methods=["POST"])
def api_nightly_run():
    """Trigger a nightly cycle in a background thread."""
    def _run():
        _NIGHTLY_LAST_STATUS["running"] = True
        print(f"[nightly] manual run started at {datetime.now().isoformat()}")
        try:
            summary = nightly_update.run(reload_callback=reload_data)
            _NIGHTLY_LAST_STATUS["last_run"] = datetime.now().isoformat()
            _NIGHTLY_LAST_STATUS["last_summary"] = summary
            print(f"[nightly] manual run finished: {summary.get('status')}")
        except Exception as e:  # noqa: BLE001
            print(f"[nightly] manual run failed: {e}")
            _NIGHTLY_LAST_STATUS["last_run"] = datetime.now().isoformat()
            _NIGHTLY_LAST_STATUS["last_summary"] = {"status": "failed", "error": str(e)}
        finally:
            _NIGHTLY_LAST_STATUS["running"] = False

    if _NIGHTLY_LAST_STATUS.get("running"):
        return jsonify({"status": "already_running"}), 409
    threading.Thread(target=_run, daemon=True, name="nightly-manual").start()
    return jsonify({"status": "started"})


def _start_chrome():
    """Spin up the logged-in debug Chrome on startup and capture its port so the
    nightly scrape can attach to it. Best-effort: the server still runs if this
    fails (e.g. no desktop session)."""
    if os.environ.get("CHROME_AUTOSTART", "1").strip() == "0":
        print("[chrome] autostart disabled (CHROME_AUTOSTART=0)")
        return
    try:
        import chrome_session
        port = chrome_session.ensure_session(require_login=False)
        print(f"[chrome] debug Chrome ready on port {port} "
              f"(logged_in={chrome_session.is_logged_in(port)})")
        print("[chrome] If not logged in, sign into the PM site in that Chrome "
              "window - the session is reused for manual scrapes.")
    except Exception as e:
        print(f"[chrome] could not auto-start Chrome: {e}")
        print("[chrome] Launch it manually with: python chrome_session.py")


if __name__ == "__main__":
    host = os.environ.get("SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("SERVER_PORT", "5000"))
    print("=" * 60)
    print("BLA Maintenance Dashboard")
    print(f"  Local:   http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        print(f"  Network: http://<this-machine-ip>:{port}  (reachable on your LAN)")
    print(f"  Checklist provider: {'ollama' if os.environ.get('OLLAMA_API_KEY') else 'gemini'} "
          f"(model: {ae.OLLAMA_MODEL})")
    print("=" * 60)
    _start_chrome()
    _start_nightly()
    _start_weekly_digest()
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
