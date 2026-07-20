"""
Equipment Work-Order Trend Analyzer (Gemini)
============================================
Filters the unscheduled work orders in work_orders_unscheduled.json down to a
single machine and sends them to Google Gemini for trend / root-cause analysis.

Defaults to the "Rainbow" stacking system (equipment_id 1877), but any machine
can be analyzed via command-line arguments.

Setup
-----
1. pip install -r requirements.txt
2. Make sure GEMINI_API_KEY is available either as a Windows environment
   variable or in a local .env file:  GEMINI_API_KEY=your_key_here

Usage
-----
    python analyze_equipment.py                      # Rainbow machine (default)
    python analyze_equipment.py --keyword rainbow    # match on name/problem text
    python analyze_equipment.py --equipment-id 1877  # match on equipment_id
    python analyze_equipment.py --model gemini-2.5-pro

The Gemini analysis is printed to the console and saved to
<keyword>_analysis_<timestamp>.md in this folder.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH = os.path.join(OUTPUT_DIR, "work_orders_unscheduled.json")

# Fields that contribute to local statistics
NUMERIC_FIELDS = ("material_cost", "labor_time", "downtime_hours")


def load_work_orders(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def matches(record: dict, equipment_id: str | None, keyword: str | None) -> bool:
    """Return True if the record belongs to the target machine."""
    if equipment_id and (record.get("equipment_id") or "").strip() == equipment_id:
        return True
    if keyword:
        kw = keyword.lower()
        haystacks = (
            record.get("equipment_name", ""),
            record.get("equipment_eq_id", ""),
            record.get("problem", ""),
            record.get("comments", ""),
        )
        if any(kw in (h or "").lower() for h in haystacks):
            return True
    return False


def filter_records(records, equipment_id, keyword) -> list[dict]:
    return [r for r in records if matches(r, equipment_id, keyword)]


def _to_float(value) -> float:
    try:
        return float(str(value).replace("$", "").replace(",", "").strip())
    except (ValueError, TypeError):
        return 0.0


def _parse_date(value):
    for fmt in ("%m/%d/%Y", "%m/%d/%Y %I:%M %p"):
        try:
            return datetime.strptime(str(value).strip(), fmt)
        except (ValueError, TypeError):
            continue
    return None


def build_stats(records: list[dict]) -> dict:
    """Compute deterministic stats locally so Gemini gets accurate numbers."""
    stats = {"work_order_count": len(records)}

    for field in NUMERIC_FIELDS:
        stats[f"total_{field}"] = round(sum(_to_float(r.get(field)) for r in records), 2)

    dates = [d for d in (_parse_date(r.get("date_notified")) for r in records) if d]
    if dates:
        stats["date_range"] = {
            "earliest": min(dates).strftime("%m/%d/%Y"),
            "latest": max(dates).strftime("%m/%d/%Y"),
        }

    stats["urgency_breakdown"] = dict(
        Counter((r.get("urgency") or "Unspecified").strip() for r in records)
    )
    stats["status_breakdown"] = dict(
        Counter((r.get("status") or "Unspecified").strip() for r in records)
    )
    stats["equipment"] = sorted(
        {(r.get("equipment_name") or "").strip() for r in records if r.get("equipment_name")}
    )
    return stats


def _compact_records(records: list[dict]) -> list[dict]:
    return [
        {
            "wo_id": r.get("wo_id"),
            "date_notified": r.get("date_notified"),
            "urgency": r.get("urgency"),
            "problem": r.get("problem"),
            "status": r.get("status"),
            "material_cost": r.get("material_cost"),
            "labor_time": r.get("labor_time"),
            "downtime_hours": r.get("downtime_hours"),
            "work_performed_by": r.get("work_performed_by"),
            "completed_datetime": r.get("completed_datetime"),
            "comments": r.get("comments"),
        }
        for r in records
    ]


def build_prompt(records: list[dict], stats: dict, label: str) -> str:
    compact = [
        {
            "wo_id": r.get("wo_id"),
            "date_notified": r.get("date_notified"),
            "urgency": r.get("urgency"),
            "problem": r.get("problem"),
            "status": r.get("status"),
            "material_cost": r.get("material_cost"),
            "labor_time": r.get("labor_time"),
            "downtime_hours": r.get("downtime_hours"),
            "work_performed_by": r.get("work_performed_by"),
            "completed_datetime": r.get("completed_datetime"),
            "comments": r.get("comments"),
        }
        for r in records
    ]

    return f"""You are a reliability / maintenance engineer analyzing the unscheduled
(breakdown) work-order history for a single piece of factory equipment: {label}.

Below are pre-computed statistics followed by the full list of work orders as JSON.
Use the statistics for any totals; use the work-order text (problem + comments) to
understand failure modes.

=== PRE-COMPUTED STATISTICS ===
{json.dumps(stats, indent=2)}

=== WORK ORDERS (JSON) ===
{json.dumps(compact, indent=2)}

Produce a clear, well-structured Markdown report with these sections:

1. **Executive Summary** - 3-5 sentence overview of the machine's reliability.
2. **Recurring Failure Modes** - group the work orders into recurring problem
   categories (e.g. X-axis/drive issues, referencing errors, sensor faults).
   For each: how many times it occurred, example WO numbers, and likely root cause.
3. **Trends Over Time** - is the machine getting better or worse? Note any
   clustering of failures, escalating downtime, or recurring seasonality.
4. **Cost & Downtime Impact** - call out the most expensive / highest-downtime
   events and the cumulative impact.
5. **Root-Cause Hypotheses** - your best engineering judgment on underlying causes.
6. **Recommended Actions** - prioritized, concrete preventive-maintenance and
   spare-parts recommendations to reduce future breakdowns.

Be specific and cite WO numbers as evidence. Do not invent data that is not present.
"""


def _operator_edits_section(operator_edits: str) -> str:
    """A prompt block that forces the model to preserve manual operator edits.
    `operator_edits` is a pre-formatted list of instructions (see guide_engine).
    Returns an empty string when there are no recorded edits."""
    if not (operator_edits or "").strip():
        return ""
    return f"""

=== OPERATOR EDITS THAT MUST BE PRESERVED ===
A human reviewer has manually edited this machine's checklist over time. The
edits below are listed oldest-first; when two edits conflict, the LATER edit
wins. You MUST carry every one of these changes into your output, even when you
are rebuilding the checklist from scratch. Do not undo or contradict them.

{operator_edits}
=== END OPERATOR EDITS ===
"""


def build_troubleshoot_prompt(records: list[dict], stats: dict, label: str,
                              operator_edits: str = "") -> str:
    compact = _compact_records(records)
    edits_block = _operator_edits_section(operator_edits)

    return f"""You are a senior maintenance technician writing a practical
TROUBLESHOOTING CHECKLIST that machine OPERATORS (not engineers) will use to
diagnose and fix common issues on this equipment: {label}.

Base the checklist ENTIRELY on the real breakdown history below. Use the
'problem' text to identify symptoms operators report, and the 'comments' text to
extract the actual fixes that worked. Group similar work orders into common
problem categories ordered by how often they occur (most frequent first).

=== PRE-COMPUTED STATISTICS ===
{json.dumps(stats, indent=2)}

=== WORK ORDERS (JSON) ===
{json.dumps(compact, indent=2)}
{edits_block}
Produce the checklist as Markdown using EXACTLY this structure and order:

1. A single H1 title line: "# Operator Troubleshooting Checklist: {label}".
2. A "## Troubleshooting" section containing ONE Markdown table with EXACTLY
   these four columns (keep this header row verbatim):

   | Issue | Symptoms | Troubleshooting Steps | When to Call Maintenance |

3. A "## Work Orders & Parts Reference" section containing ONE Markdown table
   with EXACTLY these three columns (keep this header row verbatim):

   | Issue | Related Work Order #s | Parts Often Needed |

CRITICAL FORMATTING RULES for the tables:
- Group the work orders into common problem categories ("Issues"), MOST FREQUENT
  FIRST. Use the SAME Issue names (identical text) in both tables so they line up.
- Inside a table cell, put EACH symptom / step / criterion on its own line using
  a literal "<br>" between items, and start each line with "- " (e.g.
  "- Machine won't reference<br>- Makes jerking movements"). Do NOT use real line
  breaks inside a cell - use <br> only, so the Markdown table stays valid.
- In the "Troubleshooting Steps" cell, write them as ordered actions the operator
  can safely try, based on fixes that actually worked in the comments.
- In "Related Work Order #s", list the bare WO numbers separated by commas
  (e.g. "2321, 3424, 7931"). Add a short parenthetical note only if a WO required
  specialist repair.
- In "Parts Often Needed", list part names with their part numbers in parentheses
  if present in the history; otherwise write "None operator-replaceable".

GENERAL RULES:
- Keep language simple, direct, and action-oriented for shop-floor operators.
- Only include steps that are SAFE for an operator (no electrical/mechanical
  repairs that require a technician) - route those to Maintenance.
- Do NOT invent error codes, parts, or steps that are not supported by the data.
- Do NOT include a "Safety First" section, safety notes, disclaimers, warnings,
  or any preamble before the tables. Start with the title and go straight to the
  "## Troubleshooting" table.
- Output ONLY the Markdown described above (title and the two tables).
"""


def build_update_prompt(existing_markdown: str, new_records: list[dict],
                        stats: dict = None, label: str = "",
                        operator_edits: str = "") -> str:
    """Prompt that feeds the model the LATEST checklist plus the new unscheduled
    work orders and asks whether anything needs to be ADDED. Additions only -
    never deletions. Manual edits are NOT re-injected: the on-disk checklist is
    already the latest version, and full edit history lives in version_store.
    `stats` / `operator_edits` are accepted for backwards-compatibility and
    intentionally ignored."""
    compact = _compact_records(new_records)

    return f"""You are a senior maintenance technician maintaining an operator
TROUBLESHOOTING CHECKLIST for this equipment: {label}.

You are given (1) the CURRENT checklist Markdown (its latest version) and
(2) NEW unscheduled work orders collected in the most recent scrape.

=== CURRENT CHECKLIST ===
{existing_markdown}

=== NEW UNSCHEDULED WORK ORDERS (JSON) ===
{json.dumps(compact, indent=2)}

Based on these new unscheduled work orders, does anything new need to be added to the checklist? If so, make those changes. Do not make any deletions.

RULES:
- ADDITIONS ONLY. Never delete, remove, reword, or reorder existing rows, Issue
  names, symptoms, steps, or work-order numbers. Preserve all current content
  exactly as written.
- If a new work order fits an EXISTING Issue, append the new symptom/step text to
  that row and add its WO number to that row's "Related Work Order #s".
- If a new work order is a genuinely NEW failure mode not covered by any existing
  Issue, add a NEW row to BOTH tables using the SAME Issue name in each.
- If nothing new needs to be added, output the current checklist unchanged.
- Keep the EXACT same two-table structure and column headers as the current
  checklist. Inside a table cell, put each item on its own line using a literal
  "<br>" and start each line with "- ". Never use real line breaks in a cell.
- In "Related Work Order #s", list bare WO numbers separated by commas.
- In "Parts Often Needed", list parts with part numbers in parentheses when
  present in the data; otherwise write "None operator-replaceable".
- Only include steps that are SAFE for an operator; route repairs to Maintenance.
- Do NOT invent error codes, parts, or steps not supported by the data.
- Do NOT add a "Safety First" section, disclaimers, warnings, or any preamble.
  Start with the existing H1 title.
- Output ONLY the full updated Markdown checklist (title + the two tables).
"""


# PowerShell script that performs the Gemini REST call via the Windows TLS
# stack (Schannel). This is required because the corporate network resets
# OpenSSL-based TLS connections used by Python/curl, while the native Windows
# stack is allowed through.
_PS_SCRIPT = r"""
param([Parameter(Mandatory=$true)][string]$BodyPath,
      [Parameter(Mandatory=$true)][string]$Model)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$key = $env:GEMINI_API_KEY
if (-not $key) { $key = $env:GOOGLE_API_KEY }
if (-not $key) { Write-Output '__GEMINI_ERROR__GEMINI_API_KEY not set'; exit 1 }
$body = [System.Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -Path $BodyPath))
$uri = "https://generativelanguage.googleapis.com/v1beta/models/$Model`:generateContent?key=$key"
try {
    $resp = Invoke-WebRequest -Uri $uri -Method Post -ContentType 'application/json; charset=utf-8' -Headers @{ "Accept-Encoding" = "identity" } -Body $body -TimeoutSec 300 -UseBasicParsing
    [System.Console]::Out.Write([System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray()))
} catch {
    $msg = $_.ErrorDetails.Message
    if (-not $msg) { $msg = $_.Exception.Message }
    Write-Output "__GEMINI_ERROR__$msg"
    exit 1
}
"""


# PowerShell script for any OpenAI-compatible chat endpoint (e.g. Ollama Cloud)
# via the Windows TLS stack, for the same corporate-network reason as above.
# The API key is read from the OLLAMA_API_KEY env var and sent as a Bearer token.
_PS_SCRIPT_OPENAI = r"""
param([Parameter(Mandatory=$true)][string]$BodyPath,
      [Parameter(Mandatory=$true)][string]$Uri)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$key = $env:OLLAMA_API_KEY
if (-not $key) { Write-Output '__GEMINI_ERROR__OLLAMA_API_KEY not set'; exit 1 }
$body = [System.Text.Encoding]::UTF8.GetBytes((Get-Content -Raw -Path $BodyPath))
$headers = @{ "Authorization" = "Bearer $key"; "Accept-Encoding" = "identity" }
try {
    $resp = Invoke-WebRequest -Uri $Uri -Method Post -Headers $headers -ContentType 'application/json; charset=utf-8' -Body $body -TimeoutSec 300 -UseBasicParsing
    [System.Console]::Out.Write([System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray()))
} catch {
    $msg = $_.ErrorDetails.Message
    if (-not $msg) { $msg = $_.Exception.Message }
    Write-Output "__GEMINI_ERROR__$msg"
    exit 1
}
"""

# Endpoint + default model for the Ollama Cloud OpenAI-compatible API.
OLLAMA_URL = os.environ.get("OLLAMA_URL", "https://ollama.com/v1/chat/completions")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gemma4:31b")


def analyze(prompt: str, model: str) -> str:
    """Dispatch to the configured LLM provider.

    Provider is chosen by the LLM_PROVIDER env var ("ollama" or "gemini").
    If unset, it defaults to "ollama" when OLLAMA_API_KEY is present, else
    "gemini". The `model` argument is used for Gemini; the Ollama model comes
    from OLLAMA_MODEL so the two providers can have different model names.
    """
    provider = os.environ.get(
        "LLM_PROVIDER",
        "ollama" if os.environ.get("OLLAMA_API_KEY") else "gemini",
    ).strip().lower()
    if provider == "ollama":
        return _analyze_ollama(prompt, OLLAMA_MODEL)
    return _analyze_gemini(prompt, model)


def _analyze_ollama(prompt: str, model: str) -> str:
    api_key = os.environ.get("OLLAMA_API_KEY")
    if not api_key:
        sys.exit(
            "ERROR: OLLAMA_API_KEY not found. Set it as an environment variable "
            "or add it to a .env file (OLLAMA_API_KEY=...)."
        )

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }

    body_fd, body_path = tempfile.mkstemp(suffix=".json")
    script_fd, script_path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(body_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with os.fdopen(script_fd, "w", encoding="utf-8") as f:
            f.write(_PS_SCRIPT_OPENAI)

        env = dict(os.environ, OLLAMA_API_KEY=api_key)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script_path, "-BodyPath", body_path, "-Uri", OLLAMA_URL],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        )
    finally:
        for p in (body_path, script_path):
            try:
                os.remove(p)
            except OSError:
                pass

    out = (result.stdout or "").strip()
    if "__GEMINI_ERROR__" in out:
        sys.exit("ERROR: Ollama API call failed:\n" + out.split("__GEMINI_ERROR__", 1)[1])
    if result.returncode != 0 or not out:
        sys.exit(f"ERROR: Ollama request failed (exit {result.returncode}).\n"
                 f"{result.stderr.strip()}")

    try:
        data = json.loads(out)
        return data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as e:
        sys.exit(f"ERROR: Could not parse Ollama response ({e}).\n{out[:2000]}")


def _analyze_gemini(prompt: str, model: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        sys.exit(
            "ERROR: GEMINI_API_KEY not found. Set it as an environment variable "
            "or add it to a .env file (GEMINI_API_KEY=...)."
        )

    payload = {"contents": [{"parts": [{"text": prompt}]}]}

    body_fd, body_path = tempfile.mkstemp(suffix=".json")
    script_fd, script_path = tempfile.mkstemp(suffix=".ps1")
    try:
        with os.fdopen(body_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        with os.fdopen(script_fd, "w", encoding="utf-8") as f:
            f.write(_PS_SCRIPT)

        env = dict(os.environ, GEMINI_API_KEY=api_key)
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-File", script_path, "-BodyPath", body_path, "-Model", model],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
        )
    finally:
        for p in (body_path, script_path):
            try:
                os.remove(p)
            except OSError:
                pass

    out = (result.stdout or "").strip()
    if "__GEMINI_ERROR__" in out:
        sys.exit("ERROR: Gemini API call failed:\n" + out.split("__GEMINI_ERROR__", 1)[1])
    if result.returncode != 0 or not out:
        sys.exit(f"ERROR: Gemini request failed (exit {result.returncode}).\n"
                 f"{result.stderr.strip()}")

    try:
        data = json.loads(out)
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts)
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        sys.exit(f"ERROR: Could not parse Gemini response ({e}).\n{out[:2000]}")


def main():
    parser = argparse.ArgumentParser(description="Analyze unscheduled work orders with Gemini.")
    parser.add_argument("--equipment-id", default="1877",
                        help="equipment_id to filter on (default 1877 = Rainbow).")
    parser.add_argument("--keyword", default="rainbow",
                        help="Keyword matched against name/problem/comments (default 'rainbow').")
    parser.add_argument("--no-keyword", action="store_true",
                        help="Disable keyword matching; filter by equipment-id only.")
    parser.add_argument("--mode", choices=["trends", "troubleshoot"], default="trends",
                        help="'trends' = reliability/cost report (default); "
                             "'troubleshoot' = operator troubleshooting checklist.")
    parser.add_argument("--model", default="gemini-2.5-flash",
                        help="Gemini model name (default gemini-2.5-flash).")
    parser.add_argument("--json", dest="json_path", default=JSON_PATH,
                        help="Path to the work_orders_unscheduled.json file.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Filter and print stats only; skip the Gemini call.")
    args = parser.parse_args()

    keyword = None if args.no_keyword else args.keyword

    print(f"Loading work orders from {args.json_path} ...")
    records = load_work_orders(args.json_path)
    print(f"  {len(records)} total work orders loaded.")

    filtered = filter_records(records, args.equipment_id, keyword)
    label = keyword or f"equipment_id {args.equipment_id}"
    print(f"  {len(filtered)} work orders matched '{label}'.")

    if not filtered:
        sys.exit("No matching work orders found - adjust --equipment-id / --keyword.")

    stats = build_stats(filtered)
    print("\n=== Local statistics ===")
    print(json.dumps(stats, indent=2))

    if args.mode == "troubleshoot":
        prompt = build_troubleshoot_prompt(filtered, stats, label)
    else:
        prompt = build_prompt(filtered, stats, label)

    if args.dry_run:
        print("\n--dry-run set: skipping Gemini call.")
        return

    print(f"\nSending {len(filtered)} work orders to Gemini ({args.model}) ...\n")
    report = analyze(prompt, args.model)

    print(report)

    slug = (keyword or args.equipment_id).replace(" ", "_").lower()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    kind = "troubleshooting_checklist" if args.mode == "troubleshoot" else "analysis"
    title = ("Operator Troubleshooting Checklist" if args.mode == "troubleshoot"
             else "Unscheduled Work Order Analysis")
    out_path = os.path.join(OUTPUT_DIR, f"{slug}_{kind}_{stamp}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}: {label}\n\n")
        f.write(f"_Generated {datetime.now():%Y-%m-%d %H:%M} using {args.model} "
                f"from {len(filtered)} work orders_\n\n")
        f.write(report)
    print(f"\nReport saved to {out_path}")


if __name__ == "__main__":
    main()
