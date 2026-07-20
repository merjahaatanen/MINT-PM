"""
MINT email notifications
=========================
Structure for the two notification flows requested:

  1. New unscheduled work order  -> notify the recipient list immediately.
  2. Weekly scheduled digest     -> what scheduled work is due this week or is
                                    overdue.

This is intentionally INERT until it is configured. Nothing is sent unless:
  * EMAIL_ENABLED=1 in the environment, AND
  * SMTP settings (host/from) are provided, AND
  * there is at least one recipient in mint_data/email_recipients.json.

So it is safe to ship now and simply "switch on" once the PM email list is
available - just drop the addresses into email_recipients.json (or POST them to
the /api/email/recipients endpoint) and set the SMTP env vars.

Environment variables (all optional; see .env.example):
    EMAIL_ENABLED       "1" to actually send; anything else = dry-run/log only
    SMTP_HOST           mail server host (e.g. smtp.office365.com)
    SMTP_PORT           default 587
    SMTP_USER           login user (optional if the server allows anonymous)
    SMTP_PASSWORD       login password
    SMTP_FROM           From: address (defaults to SMTP_USER)
    SMTP_STARTTLS       "1" (default) to use STARTTLS
"""

import json
import os
import smtplib
import ssl
from email.message import EmailMessage

_HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(_HERE, "mint_data")
os.makedirs(DATA_DIR, exist_ok=True)
RECIPIENTS_FILE = os.path.join(DATA_DIR, "email_recipients.json")


# --------------------------------------------------------------------------- #
# Recipient list (persisted JSON so it survives restarts)
# --------------------------------------------------------------------------- #
def get_recipients() -> list[str]:
    if not os.path.exists(RECIPIENTS_FILE):
        return []
    try:
        with open(RECIPIENTS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [str(a).strip() for a in data if str(a).strip()]
    except (json.JSONDecodeError, OSError):
        return []


def set_recipients(addresses: list[str]) -> list[str]:
    clean = sorted({str(a).strip() for a in (addresses or []) if str(a).strip()})
    with open(RECIPIENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(clean, f, indent=2)
    return clean


# --------------------------------------------------------------------------- #
# Config / status
# --------------------------------------------------------------------------- #
def _cfg() -> dict:
    return {
        "enabled": os.environ.get("EMAIL_ENABLED", "0").strip() == "1",
        "host": os.environ.get("SMTP_HOST", "").strip(),
        "port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "user": os.environ.get("SMTP_USER", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "from": (os.environ.get("SMTP_FROM", "") or os.environ.get("SMTP_USER", "")).strip(),
        "starttls": os.environ.get("SMTP_STARTTLS", "1").strip() != "0",
    }


def status() -> dict:
    cfg = _cfg()
    recips = get_recipients()
    return {
        "enabled": cfg["enabled"],
        "configured": bool(cfg["host"] and cfg["from"]),
        "recipient_count": len(recips),
        "recipients": recips,
        "active": bool(cfg["enabled"] and cfg["host"] and cfg["from"] and recips),
    }


# --------------------------------------------------------------------------- #
# Low-level send
# --------------------------------------------------------------------------- #
def _send(subject: str, body: str, recipients: list[str]) -> dict:
    """Send one plain-text email to the recipient list. When email is not fully
    configured/enabled this becomes a no-op that just reports what WOULD be sent
    (so the app works today and can be switched on later)."""
    cfg = _cfg()
    result = {"subject": subject, "recipients": recipients}
    if not recipients:
        result.update(sent=False, reason="no recipients configured")
        return result
    if not (cfg["enabled"] and cfg["host"] and cfg["from"]):
        result.update(sent=False, reason="email disabled or SMTP not configured (dry-run)")
        print(f"[email] DRY-RUN would send '{subject}' to {len(recipients)} recipient(s)")
        return result

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["from"]
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=30) as s:
            if cfg["starttls"]:
                s.starttls(context=ssl.create_default_context())
            if cfg["user"]:
                s.login(cfg["user"], cfg["password"])
            s.send_message(msg)
        result.update(sent=True)
        print(f"[email] sent '{subject}' to {len(recipients)} recipient(s)")
    except Exception as e:  # noqa: BLE001 - report, never crash the request
        result.update(sent=False, reason=f"SMTP error: {e}")
        print(f"[email] ERROR sending '{subject}': {e}")
    return result


# --------------------------------------------------------------------------- #
# High-level notifications
# --------------------------------------------------------------------------- #
def notify_new_unscheduled(wo: dict, dept_label: str = "") -> dict:
    """Called when a new unscheduled work order is created."""
    wo_id = wo.get("wo_id", "?")
    subject = f"[MINT] New unscheduled work order {wo_id}"
    body = (
        f"A new unscheduled work order was created in MINT.\n\n"
        f"Work Order: {wo_id}\n"
        f"Machine:    {wo.get('equipment_name', '')} ({wo.get('equipment_eq_id', '')})\n"
        f"Department: {dept_label or wo.get('department', '')}\n"
        f"Urgency:    {wo.get('urgency', '')}\n"
        f"Reported:   {wo.get('date_notified', '')}\n"
        f"Created by: {wo.get('created_by', '')}\n\n"
        f"Problem:\n{wo.get('problem', '')}\n"
    )
    return _send(subject, body, get_recipients())


def _fmt_wo_line(r: dict) -> str:
    return (f"  - {r.get('wo_id','?')}  {r.get('equipment_name','')} "
            f"({r.get('equipment_eq_id','')}) - due {r.get('due_date','')} "
            f"[{r.get('status','')}]")


def send_weekly_digest(due_this_week: list[dict], overdue: list[dict]) -> dict:
    """Weekly summary of scheduled work orders due this week and overdue ones."""
    subject = "[MINT] Weekly scheduled maintenance digest"
    lines = ["Scheduled maintenance summary\n"]
    lines.append(f"Due this week ({len(due_this_week)}):")
    lines += [_fmt_wo_line(r) for r in due_this_week] or ["  (none)"]
    lines.append("")
    lines.append(f"Overdue ({len(overdue)}):")
    lines += [_fmt_wo_line(r) for r in overdue] or ["  (none)"]
    return _send(subject, "\n".join(lines), get_recipients())
