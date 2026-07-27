"""
MINT write store (SQLite)
=========================
Persistent store for everything MINT lets users CREATE or EDIT, layered on top
of the (frozen) scraped PM data. Nothing here is ever touched by a scrape, so
the "one final scrape" and all historical data stay intact while MINT becomes
the system of record going forward.

It holds:
  * divisions            company -> division layer (BLA seeded; BED etc. later)
  * departments          user-added departments (scraped ones live in server.py)
  * machines             user-added machines/equipment
  * work_orders          NEW work orders created in MINT (scheduled/unscheduled)
  * wo_overrides         field-level edits applied to ANY work order (scraped or
                         manual), stored as a JSON patch keyed by wo_id
  * solutions            append-only "Solution" log entries (unscheduled WOs)
  * attachments          uploaded file metadata (files live in attachments/)
  * audit_log            who created/changed what, and when

Every mutating call requires an `author` (the "your name" field) and records an
audit_log row so changes are always attributable.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "mint_data")
ATTACH_DIR = os.path.join(DATA_DIR, "attachments")
os.makedirs(ATTACH_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "mint.db")

# Serialize writes so ID assignment stays atomic under Flask's threaded server.
_lock = threading.Lock()

# Manual work orders get an "M-" prefix so they can never collide with the
# purely-numeric PM work-order numbers.
WO_PREFIX = "M-"
WO_START = 1000


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _init() -> None:
    with _connect() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS divisions (
                key        TEXT PRIMARY KEY,
                name       TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS departments (
                key          TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                label        TEXT NOT NULL,
                division_key TEXT NOT NULL DEFAULT 'bla',
                created_at   TEXT NOT NULL,
                created_by   TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS machines (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                eq_id          TEXT NOT NULL,
                equipment_name TEXT NOT NULL,
                dept_key       TEXT NOT NULL,
                make           TEXT NOT NULL DEFAULT '',
                model          TEXT NOT NULL DEFAULT '',
                vendor         TEXT NOT NULL DEFAULT '',
                asset_num      TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL,
                created_by     TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS work_orders (
                wo_id           TEXT PRIMARY KEY,
                wo_type         TEXT NOT NULL,
                department_key  TEXT NOT NULL,
                equipment_id    TEXT NOT NULL DEFAULT '',
                equipment_eq_id TEXT NOT NULL DEFAULT '',
                equipment_name  TEXT NOT NULL DEFAULT '',
                data_json       TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                created_by      TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS wo_overrides (
                wo_id      TEXT PRIMARY KEY,
                data_json  TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS solutions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                wo_id      TEXT NOT NULL,
                author     TEXT NOT NULL DEFAULT '',
                text       TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS attachments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                wo_id        TEXT NOT NULL,
                filename     TEXT NOT NULL,
                stored_name  TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT '',
                size         INTEGER NOT NULL DEFAULT 0,
                uploaded_by  TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS inactive_items (
                item_type    TEXT NOT NULL,
                item_key     TEXT NOT NULL,
                division_key TEXT NOT NULL DEFAULT '',
                dept_key     TEXT NOT NULL DEFAULT '',
                dept_label   TEXT NOT NULL DEFAULT '',
                eq_id        TEXT NOT NULL DEFAULT '',
                name         TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL,
                created_by   TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (item_type, item_key)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                wo_id      TEXT NOT NULL DEFAULT '',
                author     TEXT NOT NULL DEFAULT '',
                action     TEXT NOT NULL,
                detail     TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS calendar_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                date          TEXT NOT NULL,
                department_key TEXT NOT NULL DEFAULT '',
                equipment_id  TEXT NOT NULL DEFAULT '',
                title         TEXT NOT NULL,
                description   TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL,
                created_by    TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events (date);
            CREATE INDEX IF NOT EXISTS idx_calendar_events_dept ON calendar_events (department_key);

            CREATE INDEX IF NOT EXISTS idx_solutions_wo ON solutions (wo_id);
            CREATE INDEX IF NOT EXISTS idx_attachments_wo ON attachments (wo_id);
            CREATE INDEX IF NOT EXISTS idx_audit_wo ON audit_log (wo_id);

            CREATE TABLE IF NOT EXISTS machine_info (
                dept_key              TEXT NOT NULL,
                eq_id                 TEXT NOT NULL,
                division_key          TEXT NOT NULL DEFAULT 'bla',
                equipment_name        TEXT NOT NULL DEFAULT '',
                category              TEXT NOT NULL DEFAULT '',
                location_workcenter   TEXT NOT NULL DEFAULT '',
                type_capex            TEXT NOT NULL DEFAULT '',
                serial_no             TEXT NOT NULL DEFAULT '',
                asset_num             TEXT NOT NULL DEFAULT '',
                year_new              TEXT NOT NULL DEFAULT '',
                condition             TEXT NOT NULL DEFAULT '',
                service_status        TEXT NOT NULL DEFAULT '',
                as_of_year_month      TEXT NOT NULL DEFAULT '',
                replacement_cost      TEXT NOT NULL DEFAULT '',
                replacement_year      TEXT NOT NULL DEFAULT '',
                ery                   TEXT NOT NULL DEFAULT '',
                comments              TEXT NOT NULL DEFAULT '',
                summary_json          TEXT NOT NULL DEFAULT '',
                created_at            TEXT NOT NULL,
                created_by            TEXT NOT NULL DEFAULT '',
                updated_at            TEXT NOT NULL,
                updated_by            TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (dept_key, eq_id)
            );

            CREATE INDEX IF NOT EXISTS idx_machine_info_dept ON machine_info (dept_key);
            """
        )
        # Migration: add summary_json column if it exists from an older schema.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(machine_info)").fetchall()}
        if "summary_json" not in cols:
            c.execute("ALTER TABLE machine_info ADD COLUMN summary_json TEXT NOT NULL DEFAULT ''")
        # Seed the BLA division so the company layer always has something.
        c.execute(
            "INSERT OR IGNORE INTO divisions (key, name, created_at) VALUES (?, ?, ?)",
            ("bla", "BLA", _now()),
        )


_init()


# --------------------------------------------------------------------------- #
# Divisions
# --------------------------------------------------------------------------- #
def list_divisions() -> list[dict]:
    with _connect() as c:
        rows = c.execute("SELECT * FROM divisions ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def add_division(key: str, name: str) -> dict:
    key = (key or "").strip().lower()
    name = (name or "").strip()
    if not key or not name:
        raise ValueError("division key and name are required")
    with _lock, _connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO divisions (key, name, created_at) VALUES (?, ?, ?)",
            (key, name, _now()),
        )
    return {"key": key, "name": name}


# --------------------------------------------------------------------------- #
# Departments (user-added)
# --------------------------------------------------------------------------- #
def list_departments() -> list[dict]:
    with _connect() as c:
        rows = c.execute("SELECT * FROM departments ORDER BY name").fetchall()
    return [dict(r) for r in rows]


def add_department(name: str, division_key: str = "bla", author: str = "",
                   label: str = "", key: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("department name is required")
    key = (key or "").strip().lower() or _slug(name)
    label = (label or "").strip() or name
    division_key = (division_key or "bla").strip().lower()
    with _lock, _connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO departments "
            "(key, name, label, division_key, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, name, label, division_key, _now(), author or ""),
        )
    _audit("", author, "add_department", f"{name} ({key})")
    return {"key": key, "name": name, "label": label, "division_key": division_key}


def _slug(name: str) -> str:
    s = "".join(ch if ch.isalnum() else "_" for ch in (name or "").lower())
    return "_".join(p for p in s.split("_") if p) or "dept"


# --------------------------------------------------------------------------- #
# Machines (user-added)
# --------------------------------------------------------------------------- #
def list_machines() -> list[dict]:
    with _connect() as c:
        rows = c.execute("SELECT * FROM machines ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def add_machine(equipment_name: str, dept_key: str, eq_id: str = "",
                make: str = "", model: str = "", vendor: str = "",
                asset_num: str = "", author: str = "") -> dict:
    equipment_name = (equipment_name or "").strip()
    dept_key = (dept_key or "").strip()
    if not equipment_name or not dept_key:
        raise ValueError("equipment_name and dept_key are required")
    eq_id = (eq_id or "").strip()
    with _lock, _connect() as c:
        if not eq_id:
            # Allocate a synthetic EQ ID that won't clash with PM numeric ids.
            row = c.execute(
                "SELECT COUNT(*) AS n FROM machines").fetchone()
            eq_id = f"M{9000 + int(row['n']) + 1}"
        cur = c.execute(
            "INSERT INTO machines "
            "(eq_id, equipment_name, dept_key, make, model, vendor, asset_num, "
            " created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (eq_id, equipment_name, dept_key, make, model, vendor, asset_num,
             _now(), author or ""),
        )
        new_id = cur.lastrowid
    _audit("", author, "add_machine", f"{equipment_name} ({eq_id}) -> {dept_key}")
    return {
        "id": new_id, "eq_id": eq_id, "equipment_name": equipment_name,
        "dept_key": dept_key, "make": make, "model": model,
        "vendor": vendor, "asset_num": asset_num,
    }


# --------------------------------------------------------------------------- #
# Inactive items (soft-deleted departments / machines)
# --------------------------------------------------------------------------- #
# Instead of hard-deleting a department or machine, MINT flags it "inactive".
# Inactive items (and their work orders) are hidden everywhere but can be
# restored at any time. This registry works for BOTH frozen scraped items and
# user-added ones, since it is keyed independently of the source data.
def list_inactive(item_type: str = "") -> list[dict]:
    with _connect() as c:
        if item_type:
            rows = c.execute(
                "SELECT * FROM inactive_items WHERE item_type = ? ORDER BY created_at DESC",
                (item_type,),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM inactive_items ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def set_inactive(item_type: str, item_key: str, division_key: str = "",
                 dept_key: str = "", dept_label: str = "", eq_id: str = "",
                 name: str = "", author: str = "") -> dict:
    item_type = (item_type or "").strip()
    item_key = (item_key or "").strip()
    if not item_type or not item_key:
        raise ValueError("item_type and item_key are required")
    with _lock, _connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO inactive_items "
            "(item_type, item_key, division_key, dept_key, dept_label, eq_id, "
            " name, created_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (item_type, item_key, division_key, dept_key, dept_label, eq_id,
             name, _now(), author or ""),
        )
    _audit("", author, f"deactivate_{item_type}", f"{name or item_key} ({item_key})")
    return {"item_type": item_type, "item_key": item_key, "name": name}


def restore_inactive(item_type: str, item_key: str, author: str = "") -> None:
    item_type = (item_type or "").strip()
    item_key = (item_key or "").strip()
    with _lock, _connect() as c:
        c.execute(
            "DELETE FROM inactive_items WHERE item_type = ? AND item_key = ?",
            (item_type, item_key),
        )
    _audit("", author, f"restore_{item_type}", item_key)


def inactive_department_keys() -> set[str]:
    return {r["item_key"] for r in list_inactive("department")}


def inactive_machine_map() -> dict[str, set[str]]:
    """dept_key -> {eq_id, ...} for every machine flagged inactive."""
    out: dict[str, set[str]] = {}
    for r in list_inactive("machine"):
        dk = r.get("dept_key") or ""
        out.setdefault(dk, set()).add(str(r.get("eq_id") or ""))
    return out


# --------------------------------------------------------------------------- #
# Machine information / longevity (editable layer)
# --------------------------------------------------------------------------- #
MACHINE_INFO_FIELDS = (
    "division_key", "equipment_name", "category", "location_workcenter",
    "type_capex", "serial_no", "asset_num", "year_new", "condition",
    "service_status", "as_of_year_month", "replacement_cost",
    "replacement_year", "ery", "comments", "summary_json",
)


def get_machine_info(dept_key: str, eq_id: str) -> dict | None:
    dept_key = (dept_key or "").strip()
    eq_id = (eq_id or "").strip()
    if not dept_key or not eq_id:
        return None
    with _connect() as c:
        row = c.execute(
            "SELECT * FROM machine_info WHERE dept_key = ? AND eq_id = ?",
            (dept_key, eq_id),
        ).fetchone()
    return dict(row) if row else None


def set_machine_info(dept_key: str, eq_id: str, fields: dict,
                     author: str = "") -> dict:
    """Create or update the editable machine information for a single machine.
    Unknown keys are ignored."""
    dept_key = (dept_key or "").strip()
    eq_id = (eq_id or "").strip()
    if not dept_key or not eq_id:
        raise ValueError("dept_key and eq_id are required")
    now = _now()
    existing = get_machine_info(dept_key, eq_id) or {}
    created_at = existing.get("created_at") or now
    created_by = existing.get("created_by") or author or ""
    provided = {k: str(v or "").strip() for k, v in (fields or {}).items() if k in MACHINE_INFO_FIELDS}
    data = {k: provided.get(k, existing.get(k, "")) for k in MACHINE_INFO_FIELDS}
    data["division_key"] = provided.get("division_key") or existing.get("division_key") or "bla"
    data["equipment_name"] = provided.get("equipment_name") or existing.get("equipment_name") or ""
    with _lock, _connect() as c:
        c.execute(
            """INSERT OR REPLACE INTO machine_info
            (dept_key, eq_id, division_key, equipment_name, category,
             location_workcenter, type_capex, serial_no, asset_num, year_new,
             condition, service_status, as_of_year_month, replacement_cost,
             replacement_year, ery, comments, summary_json, created_at, created_by,
             updated_at, updated_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dept_key, eq_id, data["division_key"], data["equipment_name"],
                data["category"], data["location_workcenter"], data["type_capex"],
                data["serial_no"], data["asset_num"], data["year_new"],
                data["condition"], data["service_status"], data["as_of_year_month"],
                data["replacement_cost"], data["replacement_year"], data["ery"],
                data["comments"], data["summary_json"], created_at, created_by, now, author or "",
            ),
        )
    _audit("", author, "edit_machine_info", f"{dept_key}:{eq_id}")
    return get_machine_info(dept_key, eq_id)


# --------------------------------------------------------------------------- #
# Work orders (created in MINT)
# --------------------------------------------------------------------------- #
# Fields carried on a work-order record (mirrors the scraped JSON schema).
WO_FIELDS = (
    "equipment_id", "equipment_eq_id", "equipment_name", "department",
    "wo_id", "wo_type", "date_notified", "due_date", "urgency", "problem",
    "audit_item", "status", "material_cost", "labor_time", "work_performed_by",
    "downtime_hours", "completed_datetime", "completion_comments",
    "frequency", "series_id", "recurrence_stopped",
)


def _next_wo_id(c: sqlite3.Connection) -> str:
    rows = c.execute("SELECT wo_id FROM work_orders WHERE wo_id LIKE 'M-%'").fetchall()
    mx = WO_START
    for r in rows:
        try:
            n = int(str(r["wo_id"]).split("-", 1)[1])
            mx = max(mx, n)
        except (ValueError, IndexError):
            continue
    return f"{WO_PREFIX}{mx + 1}"


def add_work_order(fields: dict, author: str = "") -> dict:
    """Create a new work order. `fields` uses the scraped JSON keys. Returns the
    stored record (with its generated wo_id)."""
    data = {k: (fields.get(k) or "") for k in WO_FIELDS}
    wo_type = (data.get("wo_type") or "unscheduled").strip().lower()
    if wo_type not in ("scheduled", "unscheduled"):
        raise ValueError("wo_type must be 'scheduled' or 'unscheduled'")
    dept_key = (fields.get("department_key") or "").strip()
    if not dept_key:
        raise ValueError("department_key is required")
    with _lock, _connect() as c:
        wo_id = _next_wo_id(c)
        data["wo_id"] = wo_id
        data["wo_type"] = wo_type
        # A recurring scheduled WO is the seed of its own series: link every
        # occurrence back to this id so the recurrence engine can group them.
        if data.get("frequency") and not data.get("series_id"):
            data["series_id"] = wo_id
        data["attachments"] = []
        c.execute(
            "INSERT INTO work_orders "
            "(wo_id, wo_type, department_key, equipment_id, equipment_eq_id, "
            " equipment_name, data_json, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (wo_id, wo_type, dept_key, data.get("equipment_id", ""),
             data.get("equipment_eq_id", ""), data.get("equipment_name", ""),
             json.dumps(data), _now(), author or ""),
        )
    _audit(wo_id, author, "create_work_order", f"{wo_type} WO for {data.get('equipment_name','')}")
    return {"wo_id": wo_id, "wo_type": wo_type, "department_key": dept_key, "data": data}


def list_work_orders() -> list[dict]:
    """Every manually-created work order, as full records ready to merge into
    the in-memory caches (department_key + wo_type tagged)."""
    with _connect() as c:
        rows = c.execute("SELECT * FROM work_orders").fetchall()
    out = []
    for r in rows:
        data = json.loads(r["data_json"])
        data["wo_id"] = r["wo_id"]
        data["wo_type"] = r["wo_type"]
        data["department_key"] = r["department_key"]
        data["is_manual"] = True
        out.append(data)
    return out


def get_work_order(wo_id: str) -> dict | None:
    with _connect() as c:
        r = c.execute("SELECT * FROM work_orders WHERE wo_id = ?",
                      (str(wo_id).strip(),)).fetchone()
    if r is None:
        return None
    data = json.loads(r["data_json"])
    data["wo_id"] = r["wo_id"]
    data["wo_type"] = r["wo_type"]
    data["department_key"] = r["department_key"]
    data["is_manual"] = True
    return data


def delete_work_order(wo_id: str, author: str = "") -> bool:
    """Permanently delete a MANUALLY-created work order and everything attached
    to it (solutions, attachments + their files, overrides, audit trail).

    Only manual ("M-"-prefixed) work orders can be deleted - scraped PM records
    are read-only. Returns True if a row was removed."""
    wo_id = str(wo_id).strip()
    if not wo_id.startswith(WO_PREFIX):
        raise ValueError("only manually-created work orders can be deleted")
    with _lock, _connect() as c:
        row = c.execute("SELECT 1 FROM work_orders WHERE wo_id = ?", (wo_id,)).fetchone()
        if row is None:
            return False
        # Remove attachment files from disk first.
        for a in c.execute("SELECT stored_name FROM attachments WHERE wo_id = ?",
                            (wo_id,)).fetchall():
            try:
                os.remove(os.path.join(ATTACH_DIR, a["stored_name"]))
            except OSError:
                pass
        c.execute("DELETE FROM attachments WHERE wo_id = ?", (wo_id,))
        c.execute("DELETE FROM solutions WHERE wo_id = ?", (wo_id,))
        c.execute("DELETE FROM wo_overrides WHERE wo_id = ?", (wo_id,))
        c.execute("DELETE FROM audit_log WHERE wo_id = ?", (wo_id,))
        c.execute("DELETE FROM work_orders WHERE wo_id = ?", (wo_id,))
    return True


def stop_recurrence(series_id: str, author: str = "") -> int:
    """Permanently stop a recurring scheduled-WO series: flag every occurrence in
    the series so the recurrence engine no longer generates future ones. Existing
    occurrences are left untouched. Returns how many rows were flagged.

    A row belongs to the series if its stored series_id matches, or (for legacy
    seeds saved without a series_id) if its own wo_id matches."""
    series_id = str(series_id).strip()
    if not series_id:
        return 0
    flagged = 0
    with _lock, _connect() as c:
        rows = c.execute("SELECT wo_id, data_json FROM work_orders "
                         "WHERE wo_type = 'scheduled'").fetchall()
        for r in rows:
            data = json.loads(r["data_json"])
            sid = (data.get("series_id") or r["wo_id"] or "").strip()
            if sid != series_id or data.get("recurrence_stopped"):
                continue
            data["recurrence_stopped"] = True
            c.execute("UPDATE work_orders SET data_json = ? WHERE wo_id = ?",
                      (json.dumps(data), r["wo_id"]))
            flagged += 1
    if flagged:
        _audit(series_id, author, "stop_recurrence",
               f"stopped recurring series ({flagged} occurrence(s))")
    return flagged


# --------------------------------------------------------------------------- #
# Field-level edits (overrides) for ANY work order (scraped or manual)
# --------------------------------------------------------------------------- #
def get_override(wo_id: str) -> dict:
    with _connect() as c:
        r = c.execute("SELECT data_json FROM wo_overrides WHERE wo_id = ?",
                      (str(wo_id).strip(),)).fetchone()
    return json.loads(r["data_json"]) if r else {}


def get_all_overrides() -> dict[str, dict]:
    with _connect() as c:
        rows = c.execute("SELECT wo_id, data_json FROM wo_overrides").fetchall()
    return {r["wo_id"]: json.loads(r["data_json"]) for r in rows}


def set_override(wo_id: str, fields: dict, author: str = "") -> dict:
    """Merge `fields` into the stored override patch for a work order, and, if
    the WO is a manual one, also fold the change into its base record so the
    edit survives even if overrides are ever cleared."""
    wo_id = str(wo_id).strip()
    clean = {k: v for k, v in (fields or {}).items() if k in WO_FIELDS}
    if not clean:
        return get_override(wo_id)
    with _lock, _connect() as c:
        cur = get_override(wo_id)
        cur.update(clean)
        c.execute(
            "INSERT OR REPLACE INTO wo_overrides (wo_id, data_json, updated_at, updated_by) "
            "VALUES (?, ?, ?, ?)",
            (wo_id, json.dumps(cur), _now(), author or ""),
        )
        # Fold into the base manual record too, if present.
        base = c.execute("SELECT data_json FROM work_orders WHERE wo_id = ?",
                         (wo_id,)).fetchone()
        if base is not None:
            data = json.loads(base["data_json"])
            data.update(clean)
            c.execute("UPDATE work_orders SET data_json = ? WHERE wo_id = ?",
                      (json.dumps(data), wo_id))
    changed = ", ".join(sorted(clean.keys()))
    _audit(wo_id, author, "edit_work_order", f"changed: {changed}")
    return cur


# --------------------------------------------------------------------------- #
# Solution log (append-only) - the "Solution" section of unscheduled WOs
# --------------------------------------------------------------------------- #
def list_solutions(wo_id: str) -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT id, author, text, created_at FROM solutions "
            "WHERE wo_id = ? ORDER BY id",
            (str(wo_id).strip(),),
        ).fetchall()
    return [dict(r) for r in rows]


def add_solution(wo_id: str, text: str, author: str = "") -> dict:
    wo_id = str(wo_id).strip()
    text = (text or "").strip()
    if not text:
        raise ValueError("solution text is required")
    with _lock, _connect() as c:
        cur = c.execute(
            "INSERT INTO solutions (wo_id, author, text, created_at) VALUES (?, ?, ?, ?)",
            (wo_id, author or "", text, _now()),
        )
        new_id = cur.lastrowid
        row = c.execute("SELECT id, author, text, created_at FROM solutions WHERE id = ?",
                        (new_id,)).fetchone()
    _audit(wo_id, author, "add_solution", text[:120])
    return dict(row)


def solution_counts() -> dict[str, int]:
    with _connect() as c:
        rows = c.execute(
            "SELECT wo_id, COUNT(*) AS n FROM solutions GROUP BY wo_id").fetchall()
    return {r["wo_id"]: int(r["n"]) for r in rows}


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #
def add_attachment(wo_id: str, filename: str, file_bytes: bytes,
                   content_type: str = "", author: str = "") -> dict:
    wo_id = str(wo_id).strip()
    filename = os.path.basename(filename or "file")
    ext = os.path.splitext(filename)[1]
    stored_name = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(ATTACH_DIR, stored_name)
    with open(path, "wb") as f:
        f.write(file_bytes)
    size = len(file_bytes)
    with _lock, _connect() as c:
        cur = c.execute(
            "INSERT INTO attachments "
            "(wo_id, filename, stored_name, content_type, size, uploaded_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (wo_id, filename, stored_name, content_type or "", size, author or "", _now()),
        )
        new_id = cur.lastrowid
    _audit(wo_id, author, "add_attachment", filename)
    return {"id": new_id, "wo_id": wo_id, "filename": filename,
            "stored_name": stored_name, "content_type": content_type,
            "size": size, "uploaded_by": author or ""}


def list_attachments(wo_id: str) -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT id, wo_id, filename, stored_name, content_type, size, "
            "uploaded_by, created_at FROM attachments WHERE wo_id = ? ORDER BY id",
            (str(wo_id).strip(),),
        ).fetchall()
    return [dict(r) for r in rows]


def attachment_counts() -> dict[str, int]:
    with _connect() as c:
        rows = c.execute(
            "SELECT wo_id, COUNT(*) AS n FROM attachments GROUP BY wo_id").fetchall()
    return {r["wo_id"]: int(r["n"]) for r in rows}


def get_attachment(att_id: int) -> dict | None:
    with _connect() as c:
        r = c.execute("SELECT * FROM attachments WHERE id = ?", (int(att_id),)).fetchone()
    if r is None:
        return None
    d = dict(r)
    d["path"] = os.path.join(ATTACH_DIR, d["stored_name"])
    return d


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #
def _audit(wo_id: str, author: str, action: str, detail: str = "") -> None:
    with _connect() as c:
        c.execute(
            "INSERT INTO audit_log (wo_id, author, action, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(wo_id or ""), author or "", action, detail or "", _now()),
        )


def list_audit(wo_id: str) -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT id, wo_id, author, action, detail, created_at FROM audit_log "
            "WHERE wo_id = ? ORDER BY id DESC",
            (str(wo_id).strip(),),
        ).fetchall()
    return [dict(r) for r in rows]


def recent_audit(limit: int = 100) -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT id, wo_id, author, action, detail, created_at FROM audit_log "
            "ORDER BY id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Calendar events (manual, non-work-order events)
# --------------------------------------------------------------------------- #
def list_calendar_events(department_key: str = "", date_from: str = "", date_to: str = "") -> list[dict]:
    query = "SELECT id, date, department_key, equipment_id, title, description, created_at, created_by FROM calendar_events WHERE 1=1"
    params = []
    if department_key:
        query += " AND department_key = ?"
        params.append(department_key)
    if date_from:
        query += " AND date >= ?"
        params.append(date_from)
    if date_to:
        query += " AND date <= ?"
        params.append(date_to)
    query += " ORDER BY date, id"
    with _connect() as c:
        rows = c.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_calendar_event(event_id: int) -> dict | None:
    with _connect() as c:
        r = c.execute(
            "SELECT id, date, department_key, equipment_id, title, description, created_at, created_by "
            "FROM calendar_events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
    return dict(r) if r else None


def add_calendar_event(event: dict, author: str = "") -> dict:
    date = (event.get("date") or "").strip()
    title = (event.get("title") or "").strip()
    if not date or not title:
        raise ValueError("event date and title are required")
    with _lock, _connect() as c:
        cur = c.execute(
            "INSERT INTO calendar_events (date, department_key, equipment_id, title, description, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                date,
                (event.get("department_key") or "").strip(),
                (event.get("equipment_id") or "").strip(),
                title,
                (event.get("description") or "").strip(),
                _now(),
                author or "",
            ),
        )
        new_id = cur.lastrowid
    _audit("", author, "add_calendar_event", f"{date}: {title}")
    return get_calendar_event(new_id)


def delete_calendar_event(event_id: int, author: str = "") -> bool:
    event = get_calendar_event(event_id)
    if not event:
        return False
    with _lock, _connect() as c:
        c.execute("DELETE FROM calendar_events WHERE id = ?", (int(event_id),))
    _audit("", author, "delete_calendar_event", f"{event['date']}: {event['title']}")
    return True
