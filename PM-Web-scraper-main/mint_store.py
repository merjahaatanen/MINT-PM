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
import re
import sqlite3
import threading
import uuid
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "mint_data")
ATTACH_DIR = os.path.join(DATA_DIR, "attachments")
SEED_DIR = os.path.join(_HERE, "seed_data")
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

            CREATE TABLE IF NOT EXISTS chart_events (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                date       TEXT NOT NULL,
                title      TEXT NOT NULL,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_chart_events_date ON chart_events (date);

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

            CREATE TABLE IF NOT EXISTS technicians (
                name       TEXT PRIMARY KEY COLLATE NOCASE,
                aliases    TEXT NOT NULL DEFAULT '[]',
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS roles (
                name       TEXT PRIMARY KEY COLLATE NOCASE,
                aliases    TEXT NOT NULL DEFAULT '[]',
                active     INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS vendor_contact_types (
                name       TEXT PRIMARY KEY COLLATE NOCASE,
                created_at TEXT NOT NULL,
                created_by TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS vendor_contacts (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                company          TEXT NOT NULL DEFAULT '',
                contact          TEXT NOT NULL DEFAULT '',
                address          TEXT NOT NULL DEFAULT '',
                phone            TEXT NOT NULL DEFAULT '',
                cell             TEXT NOT NULL DEFAULT '',
                fax              TEXT NOT NULL DEFAULT '',
                email            TEXT NOT NULL DEFAULT '',
                type             TEXT NOT NULL DEFAULT '',
                service_contract TEXT NOT NULL DEFAULT '',
                contract_type    TEXT NOT NULL DEFAULT '',
                machine_eq       TEXT NOT NULL DEFAULT '[]',
                source           TEXT NOT NULL DEFAULT '',
                created_at       TEXT NOT NULL,
                created_by       TEXT NOT NULL DEFAULT '',
                updated_at       TEXT NOT NULL,
                updated_by       TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS floorplan_items (
                id         TEXT PRIMARY KEY,
                dept_key   TEXT NOT NULL,
                eq_id      TEXT NOT NULL DEFAULT '',
                label      TEXT NOT NULL DEFAULT '',
                x          REAL NOT NULL DEFAULT 0,
                y          REAL NOT NULL DEFAULT 0,
                w          REAL NOT NULL DEFAULT 100,
                h          REAL NOT NULL DEFAULT 60,
                z          INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_floorplan_dept ON floorplan_items (dept_key);

            CREATE TABLE IF NOT EXISTS machine_nicknames (
                dept_key   TEXT NOT NULL,
                eq_id      TEXT NOT NULL,
                nickname   TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (dept_key, eq_id)
            );

            CREATE TABLE IF NOT EXISTS spare_parts (
                id              TEXT PRIMARY KEY,
                description     TEXT NOT NULL,
                digital_id      TEXT NOT NULL DEFAULT '',
                picture         TEXT NOT NULL DEFAULT '',
                division_key    TEXT NOT NULL DEFAULT 'bla',
                machine_dept_key TEXT NOT NULL DEFAULT '',
                machine_eq_id   TEXT NOT NULL DEFAULT '',
                machine_name    TEXT NOT NULL DEFAULT '',
                part_type       TEXT NOT NULL DEFAULT '',
                location        TEXT NOT NULL DEFAULT '',
                quantity        INTEGER NOT NULL DEFAULT 0,
                condition       TEXT NOT NULL DEFAULT '',
                brand           TEXT NOT NULL DEFAULT '',
                is_part_of_set  INTEGER NOT NULL DEFAULT 0,
                set_info        TEXT NOT NULL DEFAULT '',
                buy_direct      INTEGER NOT NULL DEFAULT 0,
                where_to_buy    TEXT NOT NULL DEFAULT '',
                purchase_link   TEXT NOT NULL DEFAULT '',
                price_new       TEXT NOT NULL DEFAULT '',
                barcode_qr      TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                created_by      TEXT NOT NULL DEFAULT '',
                updated_at      TEXT NOT NULL,
                updated_by      TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_spare_parts_machine ON spare_parts (machine_dept_key, machine_eq_id);
            CREATE INDEX IF NOT EXISTS idx_spare_parts_type ON spare_parts (part_type);

            CREATE TABLE IF NOT EXISTS spare_part_machines (
                part_id    TEXT NOT NULL,
                dept_key   TEXT NOT NULL,
                eq_id      TEXT NOT NULL,
                name       TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (part_id, dept_key, eq_id)
            );

            CREATE INDEX IF NOT EXISTS idx_spm_part ON spare_part_machines (part_id);
            CREATE INDEX IF NOT EXISTS idx_spm_machine ON spare_part_machines (dept_key, eq_id);

            CREATE TABLE IF NOT EXISTS view_profiles (
                key        TEXT PRIMARY KEY,
                label      TEXT NOT NULL,
                features   TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL DEFAULT ''
            );
            """
        )
        # Migration: add summary_json column if it exists from an older schema.
        cols = {r["name"] for r in c.execute("PRAGMA table_info(machine_info)").fetchall()}
        if "summary_json" not in cols:
            c.execute("ALTER TABLE machine_info ADD COLUMN summary_json TEXT NOT NULL DEFAULT ''")
        # Migration: add division_key to spare_parts if it exists from an older schema.
        spare_cols = {r["name"] for r in c.execute("PRAGMA table_info(spare_parts)").fetchall()}
        if "division_key" not in spare_cols:
            c.execute("ALTER TABLE spare_parts ADD COLUMN division_key TEXT NOT NULL DEFAULT 'bla'")
        # Migration: add view_key to roles (which saved view profile a role sees).
        role_cols = {r["name"] for r in c.execute("PRAGMA table_info(roles)").fetchall()}
        if "view_key" not in role_cols:
            c.execute("ALTER TABLE roles ADD COLUMN view_key TEXT NOT NULL DEFAULT ''")
        type_count = c.execute("SELECT COUNT(*) AS n FROM vendor_contact_types").fetchone()["n"]
        if not type_count:
            c.execute(
                "INSERT INTO vendor_contact_types (name, created_at, created_by) VALUES (?, ?, ?)",
                ("TPF", _now(), "system"),
            )
            c.execute(
                "INSERT INTO vendor_contact_types (name, created_at, created_by) VALUES (?, ?, ?)",
                ("Facility", _now(), "system"),
            )
        # Seed the BLA division so the company layer always has something.
        c.execute(
            "INSERT OR IGNORE INTO divisions (key, name, created_at) VALUES (?, ?, ?)",
            ("bla", "BLA", _now()),
        )


def _seed_data() -> None:
    """One-time import of committed seed data when the local SQLite tables are
    empty. This keeps the repo self-contained: a fresh clone creates the DB,
    then immediately restores the shared floor plan, spare-parts inventory,
    and machine nicknames."""
    if not os.path.isdir(SEED_DIR):
        return

    with _lock, _connect() as c:
        # Machine nicknames
        nickname_count = c.execute("SELECT COUNT(*) FROM machine_nicknames").fetchone()[0]
        if nickname_count == 0:
            path = os.path.join(SEED_DIR, "machine_nicknames.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    nicks = json.load(f)
                for n in nicks:
                    c.execute(
                        "INSERT OR IGNORE INTO machine_nicknames (dept_key, eq_id, nickname, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (
                            str(n.get("dept_key") or ""),
                            str(n.get("eq_id") or ""),
                            str(n.get("nickname") or ""),
                            str(n.get("updated_at") or _now()),
                            str(n.get("updated_by") or "seed"),
                        ),
                    )

        # Floor plan layout
        floorplan_count = c.execute("SELECT COUNT(*) FROM floorplan_items").fetchone()[0]
        if floorplan_count == 0:
            path = os.path.join(SEED_DIR, "floorplan_items.json")
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    items = json.load(f)
                for it in items:
                    c.execute(
                        "INSERT INTO floorplan_items (id, dept_key, eq_id, label, x, y, w, h, z, updated_at, updated_by) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            str(it.get("id") or uuid.uuid4().hex),
                            str(it.get("dept_key") or ""),
                            str(it.get("eq_id") or ""),
                            str(it.get("label") or ""),
                            float(it.get("x") or 0),
                            float(it.get("y") or 0),
                            float(it.get("w") or 100),
                            float(it.get("h") or 60),
                            int(it.get("z") or 0),
                            str(it.get("updated_at") or _now()),
                            str(it.get("updated_by") or "seed"),
                        ),
                    )

        # Spare parts inventory
        parts_count = c.execute("SELECT COUNT(*) FROM spare_parts").fetchone()[0]
        if parts_count == 0:
            parts_path = os.path.join(SEED_DIR, "spare_parts.json")
            machines_path = os.path.join(SEED_DIR, "spare_part_machines.json")
            if os.path.exists(parts_path):
                with open(parts_path, "r", encoding="utf-8") as f:
                    parts = json.load(f)
                cols = list(SPARE_PART_FIELDS) + ["machine_dept_key", "machine_eq_id", "machine_name",
                                                   "created_at", "created_by", "updated_at", "updated_by"]
                for p in parts:
                    values = [
                        str(p.get("id") or uuid.uuid4().hex),
                        *[p.get(k, "") for k in SPARE_PART_FIELDS],
                        str(p.get("machine_dept_key") or ""),
                        str(p.get("machine_eq_id") or ""),
                        str(p.get("machine_name") or ""),
                        str(p.get("created_at") or _now()),
                        str(p.get("created_by") or "seed"),
                        str(p.get("updated_at") or _now()),
                        str(p.get("updated_by") or "seed"),
                    ]
                    c.execute(
                        f"INSERT INTO spare_parts (id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                        values,
                    )
                if os.path.exists(machines_path):
                    with open(machines_path, "r", encoding="utf-8") as f:
                        machines = json.load(f)
                    for m in machines:
                        c.execute(
                            "INSERT OR IGNORE INTO spare_part_machines (part_id, dept_key, eq_id, name) VALUES (?, ?, ?, ?)",
                            (
                                str(m.get("part_id") or ""),
                                str(m.get("dept_key") or ""),
                                str(m.get("eq_id") or ""),
                                str(m.get("name") or ""),
                            ),
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


def list_machine_info(dept_key: str = "") -> list[dict]:
    """Return all machine_info rows, optionally filtered to one department."""
    with _connect() as c:
        if (dept_key or "").strip():
            rows = c.execute(
                "SELECT * FROM machine_info WHERE dept_key = ?", (dept_key.strip(),)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM machine_info").fetchall()
    return [dict(r) for r in rows]


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
# Machine nicknames (admin-set display name; PM name is preserved untouched)
# --------------------------------------------------------------------------- #
def list_nicknames(dept_key: str = "") -> dict:
    """Return {eq_id: nickname} for one department, or {dept_key: {eq_id:
    nickname}} for every department when dept_key is blank."""
    with _connect() as c:
        if (dept_key or "").strip():
            rows = c.execute(
                "SELECT eq_id, nickname FROM machine_nicknames WHERE dept_key = ?",
                (dept_key.strip(),),
            ).fetchall()
            return {r["eq_id"]: r["nickname"] for r in rows if (r["nickname"] or "").strip()}
        rows = c.execute("SELECT dept_key, eq_id, nickname FROM machine_nicknames").fetchall()
        out: dict = {}
        for r in rows:
            if (r["nickname"] or "").strip():
                out.setdefault(r["dept_key"], {})[r["eq_id"]] = r["nickname"]
        return out


def get_nickname(dept_key: str, eq_id: str) -> str:
    dept_key = (dept_key or "").strip()
    eq_id = (eq_id or "").strip()
    if not dept_key or not eq_id:
        return ""
    with _connect() as c:
        row = c.execute(
            "SELECT nickname FROM machine_nicknames WHERE dept_key = ? AND eq_id = ?",
            (dept_key, eq_id),
        ).fetchone()
    return (row["nickname"] if row else "") or ""


def set_nickname(dept_key: str, eq_id: str, nickname: str, author: str = "") -> str:
    """Set (or clear, when blank) the display nickname for a machine. The PM
    equipment name is never modified. Returns the stored nickname."""
    dept_key = (dept_key or "").strip()
    eq_id = (eq_id or "").strip()
    nickname = (nickname or "").strip()
    if not dept_key or not eq_id:
        raise ValueError("dept_key and eq_id are required")
    now = _now()
    with _lock, _connect() as c:
        if nickname:
            c.execute(
                """INSERT INTO machine_nicknames
                (dept_key, eq_id, nickname, updated_at, updated_by)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(dept_key, eq_id) DO UPDATE SET
                    nickname = excluded.nickname,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by""",
                (dept_key, eq_id, nickname, now, author or ""),
            )
        else:
            c.execute(
                "DELETE FROM machine_nicknames WHERE dept_key = ? AND eq_id = ?",
                (dept_key, eq_id),
            )
    _audit("", author, "set_nickname", f"{dept_key}:{eq_id} -> {nickname or '(cleared)'}")
    return nickname


# --------------------------------------------------------------------------- #
# Vendor & Utilities contacts (BLA vendor list)
# --------------------------------------------------------------------------- #
VENDOR_CONTACT_FIELDS = (
    "company", "contact", "address", "phone", "cell", "fax", "email",
    "type", "service_contract", "contract_type", "machine_eq", "source",
)


def _vendor_row_to_dict(row) -> dict:
    d = dict(row)
    try:
        eqs = json.loads(d.get("machine_eq") or "[]")
    except (ValueError, TypeError):
        eqs = []
    d["machine_eq"] = [str(e).strip() for e in eqs if str(e).strip()]
    return d


def _clean_vendor_fields(fields: dict) -> dict:
    """Normalize a vendor-contact payload to the stored columns. machine_eq is
    accepted as a list (or comma string) and stored as a JSON list of strings."""
    out = {}
    for k in VENDOR_CONTACT_FIELDS:
        if k == "machine_eq":
            raw = fields.get("machine_eq")
            if isinstance(raw, str):
                raw = [p for p in raw.replace(",", " ").split() if p]
            elif not isinstance(raw, (list, tuple)):
                raw = []
            out["machine_eq"] = json.dumps([str(e).strip() for e in raw if str(e).strip()])
        else:
            out[k] = str(fields.get(k) or "").strip()
    return out


def list_vendor_contact_types() -> list[str]:
    with _connect() as c:
        rows = c.execute("SELECT name FROM vendor_contact_types ORDER BY name COLLATE NOCASE").fetchall()
    return [r["name"] for r in rows]


def add_vendor_contact_type(name: str, author: str = "") -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("type name is required")
    with _lock, _connect() as c:
        c.execute(
            "INSERT OR IGNORE INTO vendor_contact_types (name, created_at, created_by) VALUES (?, ?, ?)",
            (name, _now(), author or ""),
        )
    _audit("", author, "add_vendor_contact_type", name)
    return name


def delete_vendor_contact_type(name: str, author: str = "") -> None:
    name = (name or "").strip()
    if not name:
        raise ValueError("type name is required")
    with _lock, _connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM vendor_contacts WHERE type = ? COLLATE NOCASE", (name,)
        ).fetchone()
        if row["n"]:
            raise ValueError(f"{row['n']} contact(s) still use '{name}'. Reassign them before deleting this type.")
        cur = c.execute("DELETE FROM vendor_contact_types WHERE name = ? COLLATE NOCASE", (name,))
        if not cur.rowcount:
            raise ValueError("type not found")
    _audit("", author, "delete_vendor_contact_type", name)


def list_vendor_contacts() -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM vendor_contacts ORDER BY company COLLATE NOCASE, contact COLLATE NOCASE"
        ).fetchall()
    return [_vendor_row_to_dict(r) for r in rows]


def get_vendor_contact(vc_id) -> dict | None:
    with _connect() as c:
        row = c.execute("SELECT * FROM vendor_contacts WHERE id = ?", (vc_id,)).fetchone()
    return _vendor_row_to_dict(row) if row else None


def add_vendor_contact(fields: dict, author: str = "") -> dict:
    data = _clean_vendor_fields(fields or {})
    if not data["company"] and not data["contact"]:
        raise ValueError("a company or contact name is required")
    now = _now()
    cols = list(VENDOR_CONTACT_FIELDS)
    placeholders = ", ".join("?" for _ in cols)
    with _lock, _connect() as c:
        cur = c.execute(
            f"INSERT INTO vendor_contacts ({', '.join(cols)}, created_at, created_by, updated_at, updated_by) "
            f"VALUES ({placeholders}, ?, ?, ?, ?)",
            tuple(data[k] for k in cols) + (now, author or "", now, author or ""),
        )
        vc_id = cur.lastrowid
    _audit("", author, "add_vendor_contact", f"{data['company']} / {data['contact']}")
    return get_vendor_contact(vc_id)


def update_vendor_contact(vc_id, fields: dict, author: str = "") -> dict | None:
    existing = get_vendor_contact(vc_id)
    if not existing:
        return None
    incoming = fields or {}
    merged = {}
    for k in VENDOR_CONTACT_FIELDS:
        merged[k] = incoming[k] if k in incoming else (
            existing[k] if k != "machine_eq" else existing["machine_eq"])
    data = _clean_vendor_fields(merged)
    with _lock, _connect() as c:
        c.execute(
            "UPDATE vendor_contacts SET "
            + ", ".join(f"{k} = ?" for k in VENDOR_CONTACT_FIELDS)
            + ", updated_at = ?, updated_by = ? WHERE id = ?",
            tuple(data[k] for k in VENDOR_CONTACT_FIELDS) + (_now(), author or "", vc_id),
        )
    _audit("", author, "edit_vendor_contact", f"#{vc_id} {data['company']}")
    return get_vendor_contact(vc_id)


def delete_vendor_contact(vc_id, author: str = "") -> bool:
    existing = get_vendor_contact(vc_id)
    if not existing:
        return False
    with _lock, _connect() as c:
        c.execute("DELETE FROM vendor_contacts WHERE id = ?", (vc_id,))
    _audit("", author, "delete_vendor_contact", f"#{vc_id} {existing.get('company')}")
    return True


def count_vendor_contacts() -> int:
    with _connect() as c:
        return int(c.execute("SELECT COUNT(*) AS n FROM vendor_contacts").fetchone()["n"])


def seed_vendor_contacts(rows: list[dict]) -> int:
    """One-time seed of the vendor list (only runs when the table is empty)."""
    if count_vendor_contacts() > 0:
        return 0
    n = 0
    for r in rows or []:
        try:
            add_vendor_contact(r, author="import")
            n += 1
        except ValueError:
            continue
    return n


# --------------------------------------------------------------------------- #
# Work orders (created in MINT)
# --------------------------------------------------------------------------- #
# Fields carried on a work-order record (mirrors the scraped JSON schema).
WO_FIELDS = (
    "equipment_id", "equipment_eq_id", "equipment_name", "department",
    "wo_id", "wo_type", "date_notified", "due_date", "urgency", "problem",
    "audit_item", "status", "material_cost", "labor_time", "work_performed_by",
    "helpers", "downtime_hours", "completed_datetime", "completion_comments",
    "frequency", "series_id", "recurrence_stopped", "assigned_to", "owner",
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


# --------------------------------------------------------------------------- #
# Chart events (global timeline markers drawn as vertical lines on every chart)
# --------------------------------------------------------------------------- #
def list_chart_events() -> list[dict]:
    """Every global chart event (major dated events like 'changed the glue'),
    oldest first."""
    with _connect() as c:
        rows = c.execute(
            "SELECT id, date, title, created_at, created_by FROM chart_events "
            "ORDER BY date ASC, id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_chart_event(event_id: int) -> dict | None:
    with _connect() as c:
        r = c.execute(
            "SELECT id, date, title, created_at, created_by FROM chart_events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
    return dict(r) if r else None


def add_chart_event(event: dict, author: str = "") -> dict:
    date = (event.get("date") or "").strip()
    title = (event.get("title") or "").strip()
    if not date or not title:
        raise ValueError("event date and title are required")
    with _lock, _connect() as c:
        cur = c.execute(
            "INSERT INTO chart_events (date, title, created_at, created_by) VALUES (?, ?, ?, ?)",
            (date, title, _now(), author or ""),
        )
        new_id = cur.lastrowid
    _audit("", author, "add_chart_event", f"{date}: {title}")
    return get_chart_event(new_id)


def delete_chart_event(event_id: int, author: str = "") -> bool:
    event = get_chart_event(event_id)
    if not event:
        return False
    with _lock, _connect() as c:
        c.execute("DELETE FROM chart_events WHERE id = ?", (int(event_id),))
    _audit("", author, "delete_chart_event", f"{event['date']}: {event['title']}")
    return True


# --------------------------------------------------------------------------- #
# Technicians (maintenance workers tracked in team stats)
# --------------------------------------------------------------------------- #
def list_technicians(active_only: bool = True) -> list[dict]:
    query = "SELECT name, aliases, active, created_at, created_by FROM technicians"
    params = ()
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name COLLATE NOCASE"
    with _connect() as c:
        rows = c.execute(query, params).fetchall()
    out = []
    for r in rows:
        data = dict(r)
        try:
            data["aliases"] = json.loads(data.get("aliases") or "[]")
        except (json.JSONDecodeError, TypeError):
            data["aliases"] = []
        out.append(data)
    return out


def get_technician(name: str) -> dict | None:
    name = (name or "").strip()
    if not name:
        return None
    with _connect() as c:
        r = c.execute(
            "SELECT name, aliases, active, created_at, created_by FROM technicians "
            "WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
    if r is None:
        return None
    data = dict(r)
    try:
        data["aliases"] = json.loads(data.get("aliases") or "[]")
    except (json.JSONDecodeError, TypeError):
        data["aliases"] = []
    return data


def add_technician(name: str, aliases: list[str] | None = None, author: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("technician name is required")
    aliases = sorted({(a or "").strip().lower() for a in (aliases or []) if (a or "").strip()})
    with _lock, _connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO technicians (name, aliases, active, created_at, created_by) "
            "VALUES (?, ?, 1, ?, ?)",
            (name, json.dumps(aliases), _now(), author or ""),
        )
    _audit("", author, "add_technician", name)
    return get_technician(name)


def delete_technician(name: str, author: str = "") -> bool:
    name = (name or "").strip()
    if not name:
        return False
    with _lock, _connect() as c:
        row = c.execute(
            "DELETE FROM technicians WHERE name = ? COLLATE NOCASE",
            (name,),
        ).rowcount
    if row:
        _audit("", author, "delete_technician", name)
        return True
    return False


def seed_technicians() -> None:
    """Seed the default technicians if the table is empty."""
    with _connect() as c:
        count = c.execute("SELECT COUNT(*) AS n FROM technicians").fetchone()["n"]
    if count:
        return
    defaults = [
        ("Shinobi", ["gabriel", "shinobi", "gabe", "gabriel shinobi", "g shinobi"]),
        ("Primo", ["primo", "pu", "primo uy"]),
        ("Max", ["max", "maksim", "maksim bushka"]),
    ]
    for name, aliases in defaults:
        add_technician(name, aliases, author="system")


seed_technicians()


def _ensure_technician(name: str, aliases: list[str]) -> None:
    """Add a technician if one by this name doesn't already exist. Used to
    backfill new default technicians without clobbering existing entries
    (unlike seed_technicians(), which only ever runs once on an empty table)."""
    if get_technician(name) is None:
        add_technician(name, aliases, author="system")


# "Maintenance" and "Maintenance Mechanic" are scraped assigned-to/skill
# values that both map to the in-house maintenance team, so they're merged
# into a single technician (sign in + My Schedule + self-assign) rather than
# tracked as separate identities or roles.
_ensure_technician("Maintenance", ["maintenance", "maintenance mechanic"])


# --------------------------------------------------------------------------- #
# Roles (shared assigned-to identities, e.g. "QA Technician", "Mechanic")
# --------------------------------------------------------------------------- #
# Distinct from technicians: a role is a shared identity multiple people can
# sign in as to see/complete work orders assigned to that skill/team, without
# implying a specific tracked individual (no per-person team stats).
def list_roles(active_only: bool = True) -> list[dict]:
    query = "SELECT name, aliases, view_key, active, created_at, created_by FROM roles"
    params = ()
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY name COLLATE NOCASE"
    with _connect() as c:
        rows = c.execute(query, params).fetchall()
    out = []
    for r in rows:
        data = dict(r)
        try:
            data["aliases"] = json.loads(data.get("aliases") or "[]")
        except (json.JSONDecodeError, TypeError):
            data["aliases"] = []
        out.append(data)
    return out


def get_role(name: str) -> dict | None:
    name = (name or "").strip()
    if not name:
        return None
    with _connect() as c:
        r = c.execute(
            "SELECT name, aliases, view_key, active, created_at, created_by FROM roles "
            "WHERE name = ? COLLATE NOCASE",
            (name,),
        ).fetchone()
    if r is None:
        return None
    data = dict(r)
    try:
        data["aliases"] = json.loads(data.get("aliases") or "[]")
    except (json.JSONDecodeError, TypeError):
        data["aliases"] = []
    return data


def add_role(name: str, aliases: list[str] | None = None, view_key: str | None = None, author: str = "") -> dict:
    name = (name or "").strip()
    if not name:
        raise ValueError("role name is required")
    aliases = sorted({(a or "").strip().lower() for a in (aliases or []) if (a or "").strip()})
    existing = get_role(name)
    if view_key is None:
        view_key = existing["view_key"] if existing else ""
    with _lock, _connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO roles (name, aliases, view_key, active, created_at, created_by) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            (name, json.dumps(aliases), view_key,
             existing["created_at"] if existing else _now(),
             existing["created_by"] if existing else (author or "")),
        )
    _audit("", author, "add_role", name)
    return get_role(name)


def set_role_view(name: str, view_key: str, author: str = "") -> dict | None:
    """Assign a saved view profile (by key, or '' for full access) to a role."""
    name = (name or "").strip()
    if not name:
        return None
    with _lock, _connect() as c:
        cur = c.execute(
            "UPDATE roles SET view_key = ? WHERE name = ? COLLATE NOCASE",
            ((view_key or "").strip(), name),
        )
        if not cur.rowcount:
            return None
    _audit("", author, "set_role_view", f"{name} -> {view_key or '(full)'}")
    return get_role(name)


def delete_role(name: str, author: str = "") -> bool:
    name = (name or "").strip()
    if not name:
        return False
    with _lock, _connect() as c:
        row = c.execute(
            "DELETE FROM roles WHERE name = ? COLLATE NOCASE",
            (name,),
        ).rowcount
    if row:
        _audit("", author, "delete_role", name)
        return True
    return False


def seed_roles() -> None:
    """Seed the default roles if the table is empty."""
    with _connect() as c:
        count = c.execute("SELECT COUNT(*) AS n FROM roles").fetchone()["n"]
    if count:
        return
    defaults = [
        ("QA Technician", ["qa technician"]),
        ("Outside Service", ["outside service"]),
        ("Technician", ["technician"]),
        ("Supervisor", ["supervisor"]),
        ("Mechanic", ["mechanic"]),
        ("Lead", ["lead"]),
        ("Operator", ["operator"]),
        ("Sr. Mechanic", ["sr. mechanic", "sr mechanic", "senior mechanic"]),
        ("M/E", ["m/e", "me"]),
        ("Material Handler", ["material handler"]),
    ]
    for name, aliases in defaults:
        add_role(name, aliases, author="system")


seed_roles()


# --------------------------------------------------------------------------- #
# View profiles ("MINT master" configurable role views, e.g. Operator)
# --------------------------------------------------------------------------- #
# A view profile is a named, checkable subset of app features. Anyone can
# switch their session to "view as" a saved profile (see server.py
# api_view_profiles*); creating/editing/deleting profiles is master-gated.
def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


def list_view_profiles() -> list[dict]:
    with _connect() as c:
        rows = c.execute(
            "SELECT key, label, features, created_at, updated_at, updated_by "
            "FROM view_profiles ORDER BY label COLLATE NOCASE"
        ).fetchall()
    out = []
    for r in rows:
        data = dict(r)
        try:
            data["features"] = json.loads(data.get("features") or "[]")
        except (json.JSONDecodeError, TypeError):
            data["features"] = []
        out.append(data)
    return out


def get_view_profile(key: str) -> dict | None:
    key = (key or "").strip()
    if not key:
        return None
    with _connect() as c:
        r = c.execute(
            "SELECT key, label, features, created_at, updated_at, updated_by "
            "FROM view_profiles WHERE key = ?",
            (key,),
        ).fetchone()
    if r is None:
        return None
    data = dict(r)
    try:
        data["features"] = json.loads(data.get("features") or "[]")
    except (json.JSONDecodeError, TypeError):
        data["features"] = []
    return data


def save_view_profile(label: str, features: list[str], author: str = "", key: str = "") -> dict:
    """Create (key blank) or update (key given) a view profile."""
    label = (label or "").strip()
    if not label:
        raise ValueError("label is required")
    features = sorted({(f or "").strip() for f in (features or []) if (f or "").strip()})
    key = (key or "").strip() or _slugify(label)
    now = _now()
    with _lock, _connect() as c:
        existing = c.execute("SELECT key FROM view_profiles WHERE key = ?", (key,)).fetchone()
        if existing:
            c.execute(
                "UPDATE view_profiles SET label = ?, features = ?, updated_at = ?, updated_by = ? WHERE key = ?",
                (label, json.dumps(features), now, author or "", key),
            )
        else:
            c.execute(
                "INSERT INTO view_profiles (key, label, features, created_at, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, label, json.dumps(features), now, now, author or ""),
            )
    _audit("", author, "save_view_profile", key)
    return get_view_profile(key)


def delete_view_profile(key: str, author: str = "") -> bool:
    key = (key or "").strip()
    if not key:
        return False
    with _lock, _connect() as c:
        row = c.execute("DELETE FROM view_profiles WHERE key = ?", (key,)).rowcount
    if row:
        _audit("", author, "delete_view_profile", key)
        return True
    return False


# --------------------------------------------------------------------------- #
# Floor plan (per-department machine layout)
# --------------------------------------------------------------------------- #
# A department's floor plan is a flat list of rectangular items positioned on a
# fixed logical canvas (see server.py FLOORPLAN_CANVAS). Each item may link to a
# machine via eq_id (numeric portion) so the UI can show live stats / highlight
# critical work orders, or be a label-only box (eq_id == "") for aisles, benches
# and equipment not tracked in MINT. Layout edits are admin-gated in server.py.
def list_floorplan(dept_key: str) -> list[dict]:
    dept_key = (dept_key or "").strip()
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM floorplan_items WHERE dept_key = ? ORDER BY z, id",
            (dept_key,),
        ).fetchall()
    return [dict(r) for r in rows]


def save_floorplan(dept_key: str, items: list[dict], author: str = "") -> list[dict]:
    """Replace the entire floor-plan layout for a department in one atomic swap.
    `items` is the full desired set; anything not present is removed."""
    dept_key = (dept_key or "").strip()
    if not dept_key:
        raise ValueError("dept_key is required")
    now = _now()
    clean: list[tuple] = []
    for it in items or []:
        iid = str(it.get("id") or uuid.uuid4().hex)
        clean.append((
            iid, dept_key,
            str(it.get("eq_id") or "").strip(),
            str(it.get("label") or "").strip(),
            float(it.get("x") or 0), float(it.get("y") or 0),
            float(it.get("w") or 100), float(it.get("h") or 60),
            int(it.get("z") or 0), now, author or "",
        ))
    with _lock, _connect() as c:
        c.execute("DELETE FROM floorplan_items WHERE dept_key = ?", (dept_key,))
        if clean:
            c.executemany(
                "INSERT INTO floorplan_items "
                "(id, dept_key, eq_id, label, x, y, w, h, z, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                clean,
            )
    _audit("", author, "save_floorplan", f"{dept_key}: {len(clean)} items")
    return list_floorplan(dept_key)


# --------------------------------------------------------------------------- #
# Spare parts inventory
# --------------------------------------------------------------------------- #
SPARE_PART_FIELDS = (
    "description", "digital_id", "picture", "division_key", "part_type", "location",
    "quantity", "condition", "brand", "is_part_of_set", "set_info",
    "buy_direct", "where_to_buy", "purchase_link", "price_new", "barcode_qr",
)


def _division_names(c) -> dict:
    return {row["key"]: row["name"] for row in c.execute("SELECT key, name FROM divisions").fetchall()}


def _row_to_spare_part(r: sqlite3.Row, div_names: dict | None = None) -> dict:
    d = dict(r)
    d["quantity"] = int(d.get("quantity") or 0)
    d["is_part_of_set"] = bool(int(d.get("is_part_of_set") or 0))
    d["buy_direct"] = bool(int(d.get("buy_direct") or 0))
    d["machines"] = []
    key = d.get("division_key") or "bla"
    d["division_label"] = (div_names or {}).get(key, key)
    return d


def _clean_spare_part(fields: dict) -> dict:
    """Normalize a spare-part payload to the stored columns."""
    out = {}
    for k in SPARE_PART_FIELDS:
        if k == "quantity":
            try:
                out[k] = int(fields.get(k) or 0)
            except (ValueError, TypeError):
                out[k] = 0
        elif k in ("is_part_of_set", "buy_direct"):
            v = fields.get(k)
            out[k] = 1 if v in (1, "1", True, "true", "yes", "on") else 0
        elif k == "division_key":
            v = str(fields.get(k) or "").strip()
            out[k] = v if v else "bla"
        else:
            out[k] = str(fields.get(k) or "").strip()
    return out


def _clean_machines(fields: dict) -> list[dict]:
    """Normalize the list of machines a spare part applies to."""
    raw = fields.get("machines") if isinstance(fields.get("machines"), list) else None
    if raw is None:
        # Legacy single-machine support
        dept = str(fields.get("machine_dept_key") or "").strip()
        eq = str(fields.get("machine_eq_id") or "").strip()
        name = str(fields.get("machine_name") or "").strip()
        if dept and eq:
            return [{"dept_key": dept, "eq_id": eq, "name": name}]
        return []
    out = []
    for m in raw:
        dept = str(m.get("dept_key") or "").strip()
        eq = str(m.get("eq_id") or "").strip()
        if dept and eq:
            out.append({"dept_key": dept, "eq_id": eq, "name": str(m.get("name") or "").strip()})
    return out


def _attach_machines(c, parts: list[dict]) -> list[dict]:
    """Populate the machines list for each part."""
    if not parts:
        return parts
    ids = [p["id"] for p in parts]
    placeholders = ", ".join("?" for _ in ids)
    rows = c.execute(
        f"SELECT * FROM spare_part_machines WHERE part_id IN ({placeholders})",
        ids,
    ).fetchall()
    by_id = {p["id"]: p["machines"] for p in parts}
    for r in rows:
        by_id[r["part_id"]].append({
            "dept_key": r["dept_key"],
            "eq_id": r["eq_id"],
            "name": r["name"],
        })
    return parts


def list_spare_parts() -> list[dict]:
    with _connect() as c:
        div_names = _division_names(c)
        rows = c.execute(
            f"SELECT * FROM spare_parts ORDER BY updated_at DESC"
        ).fetchall()
        parts = [_row_to_spare_part(r, div_names) for r in rows]
        _attach_machines(c, parts)
    return parts


def list_spare_parts_for_machine(dept_key: str, eq_id: str) -> list[dict]:
    with _connect() as c:
        div_names = _division_names(c)
        rows = c.execute(
            "SELECT p.* FROM spare_parts p "
            "JOIN spare_part_machines m ON p.id = m.part_id "
            "WHERE m.dept_key = ? AND m.eq_id = ? "
            "ORDER BY p.updated_at DESC",
            (str(dept_key or ""), str(eq_id or "")),
        ).fetchall()
        parts = [_row_to_spare_part(r, div_names) for r in rows]
        _attach_machines(c, parts)
    return parts


def get_spare_part(part_id: str) -> dict | None:
    part_id = str(part_id or "").strip()
    if not part_id:
        return None
    with _connect() as c:
        div_names = _division_names(c)
        row = c.execute("SELECT * FROM spare_parts WHERE id = ?", (part_id,)).fetchone()
        if not row:
            return None
        part = _row_to_spare_part(row, div_names)
        _attach_machines(c, [part])
    return part


def add_spare_part(fields: dict, author: str = "") -> dict:
    data = _clean_spare_part(fields or {})
    if not data["description"]:
        raise ValueError("part description is required")
    machines = _clean_machines(fields or {})
    part_id = str(uuid.uuid4().hex)
    now = _now()
    cols = list(SPARE_PART_FIELDS)
    placeholders = ", ".join("?" for _ in cols)
    with _lock, _connect() as c:
        c.execute(
            f"INSERT INTO spare_parts (id, {', '.join(cols)}, created_at, created_by, updated_at, updated_by) "
            f"VALUES (?, {placeholders}, ?, ?, ?, ?)",
            (part_id, *(data[k] for k in cols), now, author or "", now, author or ""),
        )
        for m in machines:
            c.execute(
                "INSERT INTO spare_part_machines (part_id, dept_key, eq_id, name) VALUES (?, ?, ?, ?)",
                (part_id, m["dept_key"], m["eq_id"], m["name"]),
            )
    _audit("", author, "add_spare_part", f"{part_id}: {data['description']}")
    return get_spare_part(part_id)


def update_spare_part(part_id: str, fields: dict, author: str = "") -> dict | None:
    existing = get_spare_part(part_id)
    if not existing:
        return None
    incoming = _clean_spare_part(fields or {})
    merged = {k: incoming[k] if k in fields else existing.get(k, incoming[k]) for k in SPARE_PART_FIELDS}
    if not merged["description"]:
        raise ValueError("part description is required")
    cols = list(SPARE_PART_FIELDS)
    set_clause = ", ".join(f"{k} = ?" for k in cols)
    machines = _clean_machines(fields or {})
    with _lock, _connect() as c:
        c.execute(
            f"UPDATE spare_parts SET {set_clause}, updated_at = ?, updated_by = ? WHERE id = ?",
            (*(merged[k] for k in cols), _now(), author or "", str(part_id)),
        )
        c.execute("DELETE FROM spare_part_machines WHERE part_id = ?", (str(part_id),))
        for m in machines:
            c.execute(
                "INSERT INTO spare_part_machines (part_id, dept_key, eq_id, name) VALUES (?, ?, ?, ?)",
                (str(part_id), m["dept_key"], m["eq_id"], m["name"]),
            )
    _audit("", author, "edit_spare_part", f"{part_id}: {merged['description']}")
    return get_spare_part(part_id)


def delete_spare_part(part_id: str, author: str = "") -> bool:
    existing = get_spare_part(part_id)
    if not existing:
        return False
    with _lock, _connect() as c:
        c.execute("DELETE FROM spare_part_machines WHERE part_id = ?", (str(part_id),))
        c.execute("DELETE FROM spare_parts WHERE id = ?", (str(part_id),))
    _audit("", author, "delete_spare_part", f"{part_id}: {existing['description']}")
    return True


_seed_data()
