"""
Troubleshooting-checklist engine
================================
Single source of truth for creating, editing, archiving and regenerating the
per-machine operator troubleshooting checklists. Used by BOTH the Flask server
(interactive generate / edit / update) and the nightly job (bulk regeneration).

Manual-edit preservation
-------------------------
Every time an operator saves a hand-edited checklist we:
  1. Back up the previous file (guides/<eq>.<timestamp>.bak.md).
  2. Diff the previous vs new Markdown and append an entry to an append-only
     edit log at guides/edits/<eq>.json. Each entry records WHAT changed
     (added / removed lines) with a timestamp.

That edit log is rendered into an "OPERATOR EDITS THAT MUST BE PRESERVED" block
that is injected into EVERY future prompt - both the interactive regenerate and
the nightly rebuild - so manual edits survive even a full regeneration from
scratch. Editing an edit simply appends another log entry; because entries are
applied oldest-first with "later wins", the newest change always takes effect.

Files (all under ./guides):
  <eq>.md                    the live checklist (contains all baked-in edits)
  <eq>.<stamp>.bak.md        timestamped backups (created before any overwrite)
  baselines/<eq>.md          the last AI-generated version (pre human edits)
  edits/<eq>.json            append-only manual-edit log
  archive/<stamp>/<eq>.md    full nightly snapshot taken before each rescrape
"""

import difflib
import json
import os
import re
import shutil
from datetime import datetime

import analyze_equipment as ae
import version_store as vs

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
GUIDES_DIR = os.path.join(OUTPUT_DIR, "guides")
BASELINE_DIR = os.path.join(GUIDES_DIR, "baselines")
EDITS_DIR = os.path.join(GUIDES_DIR, "edits")
ARCHIVE_DIR = os.path.join(GUIDES_DIR, "archive")

for _d in (GUIDES_DIR, BASELINE_DIR, EDITS_DIR, ARCHIVE_DIR):
    os.makedirs(_d, exist_ok=True)

# Max number of added/removed lines to keep per edit entry when rendering the
# prompt block, so long edit histories don't blow up the prompt size.
_MAX_LINES_PER_EDIT = 40


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
def _safe(eq_id) -> str:
    return "".join(c for c in str(eq_id) if c.isalnum() or c in ("-", "_"))


def guide_path(eq_id) -> str:
    return os.path.join(GUIDES_DIR, f"{_safe(eq_id)}.md")


def baseline_path(eq_id) -> str:
    return os.path.join(BASELINE_DIR, f"{_safe(eq_id)}.md")


def edits_path(eq_id) -> str:
    return os.path.join(EDITS_DIR, f"{_safe(eq_id)}.json")


def guide_exists(eq_id) -> bool:
    return os.path.exists(guide_path(eq_id))


def read_guide(eq_id) -> str | None:
    p = guide_path(eq_id)
    if not os.path.exists(p):
        return None
    with open(p, encoding="utf-8") as f:
        return f.read()


def _write(path: str, text: str):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


# --------------------------------------------------------------------------- #
# Backups & archives
# --------------------------------------------------------------------------- #
def backup_guide(eq_id) -> str | None:
    """Copy the current guide to a timestamped .bak before it is overwritten."""
    path = guide_path(eq_id)
    if not os.path.exists(path):
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(GUIDES_DIR, f"{_safe(eq_id)}.{stamp}.bak.md")
    shutil.copy2(path, bak)
    return bak


def archive_all_guides(stamp: str | None = None) -> tuple[str, int]:
    """Snapshot every live checklist into guides/archive/<stamp>/ BEFORE a
    nightly rescrape. Returns (archive_dir, count)."""
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.join(ARCHIVE_DIR, stamp)
    os.makedirs(dest, exist_ok=True)
    count = 0
    for name in os.listdir(GUIDES_DIR):
        if not name.endswith(".md") or name.endswith(".bak.md"):
            continue
        src = os.path.join(GUIDES_DIR, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))
            count += 1
    # Also snapshot the edit logs so the archive is self-contained.
    if os.path.isdir(EDITS_DIR):
        edits_dest = os.path.join(dest, "edits")
        os.makedirs(edits_dest, exist_ok=True)
        for name in os.listdir(EDITS_DIR):
            if name.endswith(".json"):
                shutil.copy2(os.path.join(EDITS_DIR, name),
                             os.path.join(edits_dest, name))
    return dest, count


# --------------------------------------------------------------------------- #
# Manual-edit log
# --------------------------------------------------------------------------- #
def load_edits(eq_id) -> list[dict]:
    p = edits_path(eq_id)
    if not os.path.exists(p):
        return []
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _diff_summary(old_md: str, new_md: str) -> dict:
    """Return {added:[...], removed:[...]} of meaningful changed lines."""
    old_lines = (old_md or "").splitlines()
    new_lines = (new_md or "").splitlines()
    added, removed = [], []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        if line.startswith("+"):
            t = line[1:].strip()
            if t:
                added.append(t)
        elif line.startswith("-"):
            t = line[1:].strip()
            if t:
                removed.append(t)
    return {"added": added, "removed": removed}


def record_edit(eq_id, old_md: str, new_md: str, author: str = "") -> dict | None:
    """Append a manual-edit entry to the log. No-op when nothing changed.
    Returns the recorded entry (or None)."""
    summary = _diff_summary(old_md, new_md)
    if not summary["added"] and not summary["removed"]:
        return None
    entry = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "author": author or "",
        "added": summary["added"],
        "removed": summary["removed"],
    }
    log = load_edits(eq_id)
    log.append(entry)
    _write(edits_path(eq_id), json.dumps(log, indent=2))
    return entry


def build_edits_block(eq_id) -> str:
    """Render the accumulated manual edits into a compact, oldest-first list of
    instructions for the LLM prompt. Empty string when there are no edits."""
    log = load_edits(eq_id)
    if not log:
        return ""
    lines = []
    for i, e in enumerate(log, 1):
        when = e.get("timestamp", "")
        who = f" by {e['author']}" if e.get("author") else ""
        lines.append(f"Edit #{i} ({when}{who}):")
        added = e.get("added", [])[:_MAX_LINES_PER_EDIT]
        removed = e.get("removed", [])[:_MAX_LINES_PER_EDIT]
        if added:
            lines.append("  KEEP / ADD these lines (operator added or changed them):")
            lines.extend(f"    + {ln}" for ln in added)
        if removed:
            lines.append("  The operator REMOVED these lines - do not reintroduce them:")
            lines.extend(f"    - {ln}" for ln in removed)
        lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Version history (SQLite, see version_store.py)
# --------------------------------------------------------------------------- #
def ensure_seeded(eq_id) -> None:
    """Make sure the current on-disk checklist exists as version 1 in the store.
    This backfills history for checklists that predate the version system so the
    UI always has at least the current state to show."""
    if vs.count(eq_id) == 0:
        current = read_guide(eq_id)
        if current is not None:
            vs.add_version(eq_id, current, source="import")


def record_version(eq_id, content: str, source: str, author: str = "") -> dict | None:
    """Seed legacy content (if needed) then append a new version. No-op saves
    (identical content) are ignored by the store."""
    ensure_seeded(eq_id)
    return vs.add_version(eq_id, content, source=source, author=author)


def list_versions(eq_id) -> list[dict]:
    ensure_seeded(eq_id)
    return vs.list_versions(eq_id)


def get_version(eq_id, version_number: int) -> dict | None:
    ensure_seeded(eq_id)
    return vs.get_version(eq_id, version_number)


# --------------------------------------------------------------------------- #
# New-record detection
# --------------------------------------------------------------------------- #
def new_records(records: list[dict], markdown: str) -> list[dict]:
    """Records whose work-order number is not already cited in the checklist."""
    cited = set(re.findall(r"\d{2,7}", markdown or ""))
    out = []
    for r in records:
        wo = str(r.get("wo_id", "") or "").strip()
        if wo and wo not in cited:
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# Generation / update
# --------------------------------------------------------------------------- #
def save_edit(eq_id, new_markdown: str, author: str = "", source: str = "manual_edit") -> dict:
    """Persist an operator-edited checklist: back up the old file, record the
    manual-edit diff into the log, save a new version, then write the new
    content. `source` lets callers tag restores distinctly from manual edits."""
    old = read_guide(eq_id) or ""
    backup_guide(eq_id)
    record_edit(eq_id, old, new_markdown, author=author)
    record_version(eq_id, new_markdown, source=source, author=author)
    _write(guide_path(eq_id), new_markdown)
    return {"markdown": new_markdown, "generated_at": os.path.getmtime(guide_path(eq_id))}


def generate_guide(eq_id, unscheduled: list[dict], label: str, model: str) -> str:
    """Build a checklist FROM SCRATCH from the machine's unscheduled work orders,
    injecting any recorded operator edits so they are preserved. Backs up and
    updates the baseline. Returns the Markdown."""
    stats = ae.build_stats(unscheduled)
    prompt = ae.build_troubleshoot_prompt(
        unscheduled, stats, label, operator_edits=build_edits_block(eq_id)
    )
    markdown = ae.analyze(prompt, model)
    backup_guide(eq_id)
    record_version(eq_id, markdown, source="ai_generate")
    _write(guide_path(eq_id), markdown)
    _write(baseline_path(eq_id), markdown)
    return markdown


def update_guide(eq_id, unscheduled: list[dict], label: str, model: str) -> dict:
    """Feed the LLM the LATEST checklist plus the newly reported unscheduled work
    orders and ask whether anything needs to be ADDED (additions only, never
    deletions). Manual edits are NOT re-injected into the prompt - the on-disk
    checklist is already the latest version, and full change history is kept in
    the version store. Falls back to a full generate when no checklist exists
    yet. Returns {markdown, updated, new_count}."""
    existing = read_guide(eq_id)
    if existing is None:
        md = generate_guide(eq_id, unscheduled, label, model)
        return {"markdown": md, "updated": True, "new_count": len(unscheduled),
                "generated": True}

    new = new_records(unscheduled, existing)
    if not new:
        return {"markdown": existing, "updated": False, "new_count": 0}

    prompt = ae.build_update_prompt(existing, new, label=label)
    markdown = ae.analyze(prompt, model)
    backup_guide(eq_id)
    record_version(eq_id, markdown, source="ai_update")
    _write(guide_path(eq_id), markdown)
    _write(baseline_path(eq_id), markdown)
    return {"markdown": markdown, "updated": True, "new_count": len(new)}
