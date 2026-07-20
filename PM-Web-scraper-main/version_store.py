"""
Checklist version store (SQLite)
================================
Persistent, append-only version history for every per-machine troubleshooting
checklist. EVERY change to a checklist - whether AI-generated, AI-updated or
hand-edited by an operator - is recorded here as a new immutable version.

Each row stores:
  * eq_id           the machine the checklist belongs to
  * version_number  1-based, monotonically increasing per machine
  * content         the FULL Markdown of the checklist at that version
  * source          what created it (ai_generate / ai_update / manual_edit /
                    import / restore)
  * author          optional operator name (for manual edits / restores)
  * created_at      ISO-8601 local timestamp
  * prev_id / next_id   doubly-linked references to the neighbouring versions

Versions form a linked list per machine via prev_id / next_id so callers can
walk backwards/forwards. Identical, no-op saves are ignored so the history only
contains real changes.
"""

import os
import sqlite3
import threading
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
GUIDES_DIR = os.path.join(_HERE, "guides")
os.makedirs(GUIDES_DIR, exist_ok=True)
DB_PATH = os.path.join(GUIDES_DIR, "versions.db")

# Serialize writes so the per-machine version_number is assigned atomically
# even under Flask's threaded dev server.
_lock = threading.Lock()

# Human-readable labels for the UI (kept here so both API + docs agree).
SOURCE_LABELS = {
    "ai_generate": "AI generated",
    "ai_update": "AI update (new work orders)",
    "manual_edit": "Manual edit",
    "import": "Imported (pre-versioning)",
    "restore": "Restored version",
}


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _init() -> None:
    with _connect() as c:
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS versions (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                eq_id          TEXT    NOT NULL,
                version_number INTEGER NOT NULL,
                content        TEXT    NOT NULL,
                source         TEXT    NOT NULL,
                author         TEXT    NOT NULL DEFAULT '',
                created_at     TEXT    NOT NULL,
                prev_id        INTEGER REFERENCES versions(id),
                next_id        INTEGER REFERENCES versions(id),
                UNIQUE (eq_id, version_number)
            )
            """
        )
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_versions_eq "
            "ON versions (eq_id, version_number)"
        )


_init()


def _safe(eq_id) -> str:
    return str(eq_id).strip()


def count(eq_id) -> int:
    """Number of stored versions for a machine."""
    with _connect() as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM versions WHERE eq_id = ?", (_safe(eq_id),)
        ).fetchone()
    return int(row["n"]) if row else 0


def latest(eq_id) -> dict | None:
    """The most recent version row (highest version_number) or None."""
    with _connect() as c:
        row = c.execute(
            "SELECT * FROM versions WHERE eq_id = ? "
            "ORDER BY version_number DESC LIMIT 1",
            (_safe(eq_id),),
        ).fetchone()
    return dict(row) if row else None


def add_version(eq_id, content: str, source: str, author: str = "") -> dict | None:
    """Append a new version for a machine. Returns the created version dict, or
    None when the content is identical to the current latest version (no-op).

    prev_id/next_id are maintained so versions form a doubly-linked list.
    """
    eq_id = _safe(eq_id)
    content = content if content is not None else ""
    with _lock:
        with _connect() as c:
            prev = c.execute(
                "SELECT id, version_number, content FROM versions "
                "WHERE eq_id = ? ORDER BY version_number DESC LIMIT 1",
                (eq_id,),
            ).fetchone()

            # Skip no-op saves so history only holds real changes.
            if prev is not None and prev["content"] == content:
                return None

            version_number = (prev["version_number"] + 1) if prev else 1
            prev_id = prev["id"] if prev else None
            created_at = datetime.now().isoformat(timespec="seconds")

            cur = c.execute(
                "INSERT INTO versions "
                "(eq_id, version_number, content, source, author, created_at, prev_id, next_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                (eq_id, version_number, content, source, author or "", created_at, prev_id),
            )
            new_id = cur.lastrowid

            # Link the previous version forward to this one.
            if prev_id is not None:
                c.execute("UPDATE versions SET next_id = ? WHERE id = ?", (new_id, prev_id))

            row = c.execute("SELECT * FROM versions WHERE id = ?", (new_id,)).fetchone()
    return dict(row)


def _to_meta(row: sqlite3.Row) -> dict:
    """Version metadata WITHOUT the (potentially large) content body."""
    return {
        "id": row["id"],
        "eq_id": row["eq_id"],
        "version_number": row["version_number"],
        "source": row["source"],
        "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
        "author": row["author"],
        "created_at": row["created_at"],
        "prev_id": row["prev_id"],
        "next_id": row["next_id"],
        "size": len(row["content"] or ""),
    }


def list_versions(eq_id) -> list[dict]:
    """All versions for a machine, newest first, metadata only (no content)."""
    with _connect() as c:
        rows = c.execute(
            "SELECT * FROM versions WHERE eq_id = ? ORDER BY version_number DESC",
            (_safe(eq_id),),
        ).fetchall()
    return [_to_meta(r) for r in rows]


def get_version(eq_id, version_number: int) -> dict | None:
    """A single version INCLUDING its full content, or None if not found."""
    with _connect() as c:
        row = c.execute(
            "SELECT * FROM versions WHERE eq_id = ? AND version_number = ?",
            (_safe(eq_id), int(version_number)),
        ).fetchone()
    if row is None:
        return None
    meta = _to_meta(row)
    meta["content"] = row["content"]
    return meta
