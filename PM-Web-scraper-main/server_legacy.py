"""
PM Troubleshooting Web App - Backend
====================================
A small Flask server that powers a local web UI for browsing machines, their
AI-generated troubleshooting guides, and the work orders used to build them.

It reuses the filtering / stats / Gemini logic from analyze_equipment.py
(including the Windows-TLS PowerShell transport, since the corporate network
blocks OpenSSL-based TLS used by Python directly).

Run:
    python server.py
Then open http://127.0.0.1:5000 in your browser.

Generated guides are cached as Markdown in ./guides/<equipment_id>.md so they
are only regenerated when you ask for it.
"""

import os
import re
import shutil
from collections import Counter
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

import analyze_equipment as ae

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
WEBAPP_DIR = os.path.join(OUTPUT_DIR, "webapp")
GUIDES_DIR = os.path.join(OUTPUT_DIR, "guides")
MODEL = "gemini-2.5-flash"

os.makedirs(GUIDES_DIR, exist_ok=True)

app = Flask(__name__, static_folder=WEBAPP_DIR, static_url_path="")
CORS(app)

# Loaded once at startup; reload by restarting the server.
_RECORDS = ae.load_work_orders(ae.JSON_PATH)


def _machine_index():
    """Group all work orders by equipment_id."""
    groups = {}
    for r in _RECORDS:
        eq_id = (r.get("equipment_id") or "").strip()
        if not eq_id:
            continue
        groups.setdefault(eq_id, []).append(r)
    return groups


def _machine_summary(eq_id, records):
    names = Counter((r.get("equipment_name") or "").strip()
                    for r in records if (r.get("equipment_name") or "").strip())
    depts = Counter((r.get("department") or "").strip()
                    for r in records if (r.get("department") or "").strip())
    return {
        "id": eq_id,
        "eq_id": (records[0].get("equipment_eq_id") or f"EQ ID {eq_id}").strip(),
        "name": names.most_common(1)[0][0] if names else f"Equipment {eq_id}",
        "department": depts.most_common(1)[0][0] if depts else "",
        "work_order_count": len(records),
        "has_guide": os.path.exists(_guide_path(eq_id)),
    }


def _guide_path(eq_id):
    safe = "".join(c for c in eq_id if c.isalnum() or c in ("-", "_"))
    return os.path.join(GUIDES_DIR, f"{safe}.md")


def _backup_guide(eq_id):
    """Copy the current guide to a timestamped .bak before it is overwritten,
    so any regenerate/update is reversible. Returns the backup path or None."""
    path = _guide_path(eq_id)
    if not os.path.exists(path):
        return None
    safe = "".join(c for c in eq_id if c.isalnum() or c in ("-", "_"))
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = os.path.join(GUIDES_DIR, f"{safe}.{stamp}.bak.md")
    shutil.copy2(path, bak)
    return bak


def _new_records(records, markdown):
    """Records whose work-order number is not already cited in the checklist."""
    cited = set(re.findall(r"\d{2,7}", markdown or ""))
    out = []
    for r in records:
        wo = str(r.get("wo_id", "") or "").strip()
        if wo and wo not in cited:
            out.append(r)
    return out


@app.route("/")
def index():
    return send_from_directory(WEBAPP_DIR, "index.html")


@app.route("/api/machines")
def api_machines():
    groups = _machine_index()
    machines = [_machine_summary(eq_id, recs) for eq_id, recs in groups.items()]
    machines.sort(key=lambda m: m["work_order_count"], reverse=True)
    return jsonify(machines)


@app.route("/api/machines/<eq_id>/workorders")
def api_workorders(eq_id):
    groups = _machine_index()
    records = groups.get(eq_id)
    if records is None:
        return jsonify({"error": "machine not found"}), 404
    stats = ae.build_stats(records)
    return jsonify({
        "machine": _machine_summary(eq_id, records),
        "stats": stats,
        "work_orders": ae._compact_records(records),
    })


@app.route("/api/machines/<eq_id>/guide", methods=["GET"])
def api_get_guide(eq_id):
    path = _guide_path(eq_id)
    if not os.path.exists(path):
        return jsonify({"exists": False, "markdown": None})
    with open(path, encoding="utf-8") as f:
        return jsonify({"exists": True, "markdown": f.read(),
                        "generated_at": os.path.getmtime(path)})


@app.route("/api/machines/<eq_id>/guide", methods=["PUT"])
def api_save_guide(eq_id):
    """Save operator-edited Markdown for a machine's guide."""
    body = request.get_json(silent=True) or {}
    markdown = body.get("markdown")
    if markdown is None:
        return jsonify({"error": "missing 'markdown'"}), 400
    path = _guide_path(eq_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    return jsonify({"exists": True, "markdown": markdown,
                    "generated_at": os.path.getmtime(path)})


@app.route("/api/workorder/<wo_id>")
def api_workorder(wo_id):
    """Return the full original work order record by work-order number."""
    for r in _RECORDS:
        if str(r.get("wo_id")).strip() == str(wo_id).strip():
            return jsonify(r)
    return jsonify({"error": "work order not found"}), 404


@app.route("/api/machines/<eq_id>/guide", methods=["POST"])
def api_generate_guide(eq_id):
    groups = _machine_index()
    records = groups.get(eq_id)
    if records is None:
        return jsonify({"error": "machine not found"}), 404

    summary = _machine_summary(eq_id, records)
    label = summary["name"]
    stats = ae.build_stats(records)
    prompt = ae.build_troubleshoot_prompt(records, stats, label)

    try:
        markdown = ae.analyze(prompt, MODEL)
    except SystemExit as e:  # analyze() calls sys.exit on failure
        return jsonify({"error": str(e)}), 502

    # Back up any existing (possibly hand-edited) guide before overwriting.
    _backup_guide(eq_id)

    path = _guide_path(eq_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)

    return jsonify({"exists": True, "markdown": markdown,
                    "generated_at": os.path.getmtime(path)})


@app.route("/api/machines/<eq_id>/guide/update", methods=["POST"])
def api_update_guide(eq_id):
    """MERGE newly reported work orders into the existing (operator-edited)
    checklist via Gemini, preserving the human edits. The previous guide is
    backed up first so the merge is reversible."""
    groups = _machine_index()
    records = groups.get(eq_id)
    if records is None:
        return jsonify({"error": "machine not found"}), 404

    path = _guide_path(eq_id)
    if not os.path.exists(path):
        return jsonify({"error": "no existing guide to update; "
                                 "generate one first"}), 400

    with open(path, encoding="utf-8") as f:
        existing = f.read()

    new_recs = _new_records(records, existing)
    if not new_recs:
        return jsonify({"exists": True, "markdown": existing, "updated": False,
                        "new_count": 0,
                        "generated_at": os.path.getmtime(path)})

    summary = _machine_summary(eq_id, records)
    label = summary["name"]
    stats = ae.build_stats(records)
    prompt = ae.build_update_prompt(existing, new_recs, stats, label)

    try:
        markdown = ae.analyze(prompt, MODEL)
    except SystemExit as e:  # analyze() calls sys.exit on failure
        return jsonify({"error": str(e)}), 502

    backup = _backup_guide(eq_id)
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)

    return jsonify({"exists": True, "markdown": markdown, "updated": True,
                    "new_count": len(new_recs),
                    "backup": os.path.basename(backup) if backup else None,
                    "generated_at": os.path.getmtime(path)})


if __name__ == "__main__":
    print("PM Troubleshooting web app running at http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, threaded=True, debug=False)
