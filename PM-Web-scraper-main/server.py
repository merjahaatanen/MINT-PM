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
import traceback
from collections import Counter
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import analyze_equipment as ae
import guide_engine as ge
import mint_store as store
import mint_email as emailer

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
WEBAPP_DIR = os.path.join(OUTPUT_DIR, "webapp")
GUIDES_DIR = os.path.join(OUTPUT_DIR, "guides")

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


os.makedirs(GUIDES_DIR, exist_ok=True)

app = Flask(__name__, static_folder=WEBAPP_DIR, static_url_path="")
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
    DEPARTMENTS = merged
    _DEPT_DIVISION = div


# --------------------------------------------------------------------------- #
# Machine grouping (within a department)
# --------------------------------------------------------------------------- #
# Ordered groups for the Toilet Partitions department. Names are the ACTUAL
# equipment_name strings as they appear in the scraped data (matched after
# light normalization), so minor spacing/quote differences still line up.
TOILET_GROUP_ORDER = [
    "Machines", "Vehicles", "General", "Equipment", "Gages and Jigs",
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
    "Gages and Jigs": [
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


# In-memory caches. Populated by reload_data(), which is called once at startup
# AND by the nightly job so the frontend reflects a fresh scrape WITHOUT needing
# a server restart. A lock guards swaps so requests never see half-loaded data.
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


def reload_data() -> datetime:
    """(Re)load every department's work orders + the equipment master into the
    in-memory caches, then atomically swap them in. Safe to call at any time.

    In addition to the frozen scraped JSON this now merges everything created in
    MINT itself (via mint_store): user-added departments/machines, manually
    created work orders, field-level edits (overrides), and per-WO solution /
    attachment counts."""
    global _DEPT_DATA, _WO_INDEX, _EQUIP_BY_KEY, _LAST_RELOAD

    _rebuild_departments()  # fold in user-added departments first

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
    Returns dicts of {eq_id, eq_label, name, group, has_guide, recs}."""
    wo_groups = _machine_groups(dept_key)  # numeric equipment_id -> {unscheduled, scheduled}
    out = []
    seen = set()
    for e in _EQUIP_BY_KEY.get(dept_key, []):
        eq_id = _num_id(e.get("eq_id"))
        if not eq_id or eq_id in seen:
            continue
        seen.add(eq_id)
        name = (e.get("equipment_name") or "").strip() or "Unknown"
        recs = wo_groups.get(eq_id, {"unscheduled": [], "scheduled": []})
        out.append({
            "eq_id": eq_id,
            "eq_label": (e.get("eq_id") or f"EQ ID {eq_id}").strip(),
            "name": name,
            "group": _group_for(dept_key, name),
            "has_guide": os.path.exists(_guide_path(eq_id)),
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
    return jsonify({
        "key": div_key,
        "name": div_name,
        "departments": depts,
        "totals": _stats(all_uns, all_sch),
    })


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
            "group": mc["group"],
            "has_guide": mc["has_guide"],
        })
        machines.append(stats)

    # Sort machines by most unscheduled work orders (most troublesome first).
    machines.sort(key=lambda m: (-m["unscheduled_count"], m["name"]))

    # Only advertise groups that actually contain machines, preserving order.
    present = {m["group"] for m in machines if m.get("group")}
    groups = [g for g in _group_order(dept_key) if g in present]

    return jsonify({
        "key": dept_key,
        "name": cfg["name"],
        "label": cfg["label"],
        "stats": _stats(data["unscheduled"], data["scheduled"]),
        "machines": machines,
        "groups": groups,
        "unscheduled": _sorted_desc(data["unscheduled"], "date_notified"),
        "scheduled": _sorted_desc(data["scheduled"], "due_date"),
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
        name = (master.get("equipment_name") or "").strip() or "Unknown"
        eq_label = (master.get("eq_id") or f"EQ ID {eq_id}").strip()
    else:
        name = _machine_name(combined)
        eq_label = _eq_label(combined, eq_id)

    return jsonify({
        "machine": {
            "eq_id": eq_id,
            "eq_label": eq_label,
            "name": name,
            "department": DEPARTMENTS[dept_key]["name"],
            "department_key": dept_key,
            "group": _group_for(dept_key, name),
            "make": (master or {}).get("make", ""),
            "model": (master or {}).get("model", ""),
            "vendor": (master or {}).get("vendor", ""),
            "asset_num": (master or {}).get("asset_num", ""),
            "has_guide": os.path.exists(_guide_path(eq_id)),
        },
        "stats": _stats(uns, sch),
        "unscheduled": uns,
        "scheduled": sch,
    })


# --------------------------------------------------------------------------- #
# Machine monthly trends (Trends tab)
# --------------------------------------------------------------------------- #
# Metrics surfaced per month. Unscheduled work orders are bucketed by
# date_notified and scheduled by due_date (matching the rest of MINT).
_TREND_METRICS = [
    "material_cost", "downtime_hours", "labor_time",
    "unscheduled_count", "scheduled_count",
]


def _empty_month(month: int) -> dict:
    m = {k: 0 for k in _TREND_METRICS}
    m["month"] = month
    return m


def _machine_trends(dept_key: str, eq_id: str) -> list[dict]:
    """Aggregate a machine's work orders into per-year, per-month metrics.

    Returns a list of {year, months:[12], totals} sorted oldest -> newest,
    including only years that have at least one work order. Every year lists
    all 12 months (empty months are zero-filled)."""
    recs = _machine_groups(dept_key).get(eq_id) or {"unscheduled": [], "scheduled": []}

    # (records, date_field, kind) so we bucket each list by the right date.
    sources = [
        (recs["unscheduled"], "date_notified", "unscheduled_count"),
        (recs["scheduled"], "due_date", "scheduled_count"),
    ]

    years: dict[int, list[dict]] = {}
    for records, date_field, count_key in sources:
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


_MONTH_NAMES = ["", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"]


def _month_records(dept_key: str, eq_id: str, year: int, month: int) -> list[dict]:
    """The machine's work orders that fall in a given year/month, tagged with
    their kind. Unscheduled bucket by date_notified, scheduled by due_date."""
    recs = _machine_groups(dept_key).get(eq_id) or {"unscheduled": [], "scheduled": []}
    out = []
    for records, date_field, kind in (
        (recs["unscheduled"], "date_notified", "unscheduled"),
        (recs["scheduled"], "due_date", "scheduled"),
    ):
        for r in records:
            dt = ae._parse_date(r.get(date_field))
            if dt and dt.year == year and dt.month == month:
                out.append({"kind": kind, "date_field": date_field, "rec": r})
    return out


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
    return f"""You are a reliability / maintenance analyst. In a SHORT synopsis,
explain what drove the maintenance numbers for {label} during {_MONTH_NAMES[month]} {year}.

Focus on the biggest cost and downtime drivers - for example a breakdown that
required an expensive part, or a repair that took the machine down for a long
time. Use the 'problem' text for the symptom and the 'comments' text for what was
actually done / what parts were replaced.

=== MONTH TOTALS ===
{json.dumps(totals, indent=2)}

=== WORK ORDERS THIS MONTH (JSON) ===
{json.dumps(compact, indent=2)}

Write concise Markdown with EXACTLY this structure:

1. A 2-3 sentence **summary** of what happened this month and why the stats look
   the way they do (call out whether it was a quiet month or driven by one/two
   big events).
2. A "**Key drivers**" bulleted list. For each notable event give the WO number,
   the cost and/or downtime, and a one-line plain-English reason
   (e.g. "WO 19912 - $4,188 spindle bearing replacement after the saw seized").

RULES:
- Be specific and cite bare WO numbers and dollar/hour figures from the data.
- Do NOT invent parts, costs, or causes that are not supported by the text.
- If material cost, downtime, or labor was essentially zero, say the month was
  routine rather than manufacturing a dramatic cause.
- Keep it tight - no preamble, headings, or disclaimers beyond what is asked.
"""


@app.route("/api/departments/<dept_key>/machines/<eq_id>/month-synopsis")
def api_month_synopsis(dept_key, eq_id):
    """LLM synopsis of what drove a specific month's stats for a machine."""
    if dept_key not in DEPARTMENTS:
        return jsonify({"error": "department not found"}), 404
    try:
        year = int(request.args.get("year", ""))
        month = int(request.args.get("month", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "numeric 'year' and 'month' query params required"}), 400
    if not (1 <= month <= 12):
        return jsonify({"error": "month must be 1-12"}), 400

    eq_id = _num_id(eq_id)
    tagged = _month_records(dept_key, eq_id, year, month)

    totals = {
        "material_cost": round(sum(ae._to_float(t["rec"].get("material_cost")) for t in tagged), 2),
        "downtime_hours": round(sum(ae._to_float(t["rec"].get("downtime_hours")) for t in tagged), 2),
        "labor_time": round(sum(ae._to_float(t["rec"].get("labor_time")) for t in tagged), 2),
        "unscheduled_count": sum(1 for t in tagged if t["kind"] == "unscheduled"),
        "scheduled_count": sum(1 for t in tagged if t["kind"] == "scheduled"),
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
        return jsonify(base)

    recs = _machine_groups(dept_key).get(eq_id) or {"unscheduled": [], "scheduled": []}
    label = f"{_machine_name(recs['unscheduled'] + recs['scheduled'])} ({_eq_label(recs['unscheduled'] + recs['scheduled'], eq_id)})"
    prompt = _build_month_synopsis_prompt(label, year, month, tagged, totals)
    try:
        base["synopsis"] = ae.analyze(prompt, MODEL)
    except SystemExit as e:  # analyze() calls sys.exit on failure
        return jsonify({"error": str(e)}), 502
    return jsonify(base)


# --------------------------------------------------------------------------- #
# Work order detail
# --------------------------------------------------------------------------- #
def _attachments_payload(wo_id: str, rec: dict | None = None) -> list[dict]:
    out = []
    for a in store.list_attachments(wo_id):
        out.append({
            "id": a["id"], "filename": a["filename"], "size": a["size"],
            "content_type": a["content_type"], "uploaded_by": a["uploaded_by"],
            "created_at": a["created_at"], "url": f"/api/attachments/{a['id']}",
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

    # Keep dates canonical so edited WOs stay correctly sorted.
    for df in ("date_notified", "due_date", "completed_datetime"):
        if df in editable:
            editable[df] = _normalize_date(editable[df])

    store.set_override(wo_id, editable, author=author)
    with _DATA_LOCK:
        rec.update(editable)
    return jsonify({"ok": True, "work_order": _wo_detail(rec)})


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
    return jsonify({"attachments": _attachments_payload(str(wo_id).strip())})


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
    return jsonify({"ok": True, "attachments": _attachments_payload(wo_id)})


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


@app.route("/api/email/weekly-digest", methods=["POST"])
def api_email_weekly_digest():
    """Manually trigger the weekly scheduled-maintenance digest. (The scheduled
    automatic send is wired in _start_scheduler once recipients exist.)"""
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
                status = (r.get("status") or "").lower()
                completed = "complete" in status or "closed" in status
                if completed:
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
    if not _edit_ok(request):
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
    previous guide is backed up first so the merge is reversible."""
    if not _edit_ok(request):
        return jsonify({"error": "Editing is locked. Enter the shared edit password."}), 401
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
    versions = ge.list_versions(eq_id)
    current = versions[0]["version_number"] if versions else None
    return jsonify({"eq_id": eq_id, "current_version": current, "versions": versions})


@app.route("/api/departments/<dept_key>/machines/<eq_id>/guide/versions/<int:version_number>")
def api_guide_version(dept_key, eq_id, version_number):
    """Full content of a single historical version."""
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


@app.route("/api/weekly")
def api_weekly():
    """Per-department scheduled + unscheduled work orders bucketed into last
    week, this week and next week (Sunday->Sunday). The frontend combines the
    departments for the overall view. Future weeks naturally contain scheduled
    work orders only (unscheduled breakdowns are reported as they happen)."""
    last_sun, this_sun, next_sun, week_after = _week_starts()
    bounds = {
        "last": (last_sun, this_sun),
        "this": (this_sun, next_sun),
        "next": (next_sun, week_after),
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
        }

    with _DATA_LOCK:
        departments = []
        for key, cfg in DEPARTMENTS.items():
            data = _DEPT_DATA.get(key, {"unscheduled": [], "scheduled": []})
            dept_buckets = {name: {"scheduled": [], "unscheduled": []} for name in bounds}
            for kind in ("scheduled", "unscheduled"):
                for r in data[kind]:
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

    return jsonify({"weeks": weeks_meta, "departments": departments})


# --------------------------------------------------------------------------- #
# Nightly rescrape + regeneration (scheduled in-process via APScheduler)
# --------------------------------------------------------------------------- #
_NIGHTLY = {
    "running": False,
    "last_run": None,
    "last_status": None,     # "success" | "error" | "skipped"
    "last_summary": None,
    "last_error": None,
    "progress": None,
    "history": [],           # recent run summaries
}
_NIGHTLY_LOCK = threading.Lock()


def _nightly_progress(msg: str):
    _NIGHTLY["progress"] = msg
    print(f"[nightly] {msg}", flush=True)


def _run_nightly(reason: str = "scheduled"):
    """Execute one nightly cycle. Guarded so only one runs at a time."""
    if not _NIGHTLY_LOCK.acquire(blocking=False):
        _nightly_progress("a nightly run is already in progress; skipping")
        return
    _NIGHTLY["running"] = True
    _NIGHTLY["last_error"] = None
    started = datetime.now()
    try:
        import nightly_update
        summary = nightly_update.run(
            model=MODEL,
            progress=_nightly_progress,
            reload_callback=reload_data,
        )
        _NIGHTLY["last_status"] = summary.get("status", "success")
        _NIGHTLY["last_summary"] = summary
    except Exception as e:
        _NIGHTLY["last_status"] = "error"
        _NIGHTLY["last_error"] = f"{e}\n{traceback.format_exc()}"
        _nightly_progress(f"ERROR: {e}")
    finally:
        _NIGHTLY["last_run"] = started.isoformat(timespec="seconds")
        _NIGHTLY["running"] = False
        _NIGHTLY["progress"] = None
        _NIGHTLY["history"] = ([{
            "run": _NIGHTLY["last_run"],
            "status": _NIGHTLY["last_status"],
            "reason": reason,
            "summary": _NIGHTLY["last_summary"],
        }] + _NIGHTLY["history"])[:20]
        _NIGHTLY_LOCK.release()


@app.route("/api/nightly/status")
def api_nightly_status():
    return jsonify({k: v for k, v in _NIGHTLY.items()})


@app.route("/api/nightly/run", methods=["POST"])
def api_nightly_run():
    """Manually trigger a nightly cycle (runs in a background thread)."""
    if _NIGHTLY["running"]:
        return jsonify({"error": "a nightly run is already in progress"}), 409
    threading.Thread(target=_run_nightly, kwargs={"reason": "manual"},
                     daemon=True).start()
    return jsonify({"started": True})


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
              "window - the session is reused for every nightly scrape.")
    except Exception as e:
        print(f"[chrome] could not auto-start Chrome: {e}")
        print("[chrome] Launch it manually with: python chrome_session.py")


def _start_scheduler():
    """Schedule the nightly job in-process. Uses APScheduler if available."""
    if os.environ.get("NIGHTLY_ENABLED", "1").strip() == "0":
        print("[nightly] scheduler disabled (NIGHTLY_ENABLED=0)")
        return
    hour = int(os.environ.get("NIGHTLY_HOUR", "2"))
    minute = int(os.environ.get("NIGHTLY_MINUTE", "0"))
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        print("[nightly] APScheduler not installed; run 'pip install -r "
              "requirements.txt'. Nightly auto-run is OFF (manual trigger still "
              "works via POST /api/nightly/run).")
        return
    sched = BackgroundScheduler(daemon=True)
    sched.add_job(lambda: _run_nightly("scheduled"),
                  CronTrigger(hour=hour, minute=minute),
                  id="nightly_update", replace_existing=True,
                  misfire_grace_time=3600, coalesce=True)
    sched.start()
    print(f"[nightly] scheduled daily at {hour:02d}:{minute:02d} local time")


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
    _start_scheduler()
    # use_reloader=False so APScheduler doesn't start twice under the reloader.
    app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
