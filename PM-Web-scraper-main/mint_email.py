"""
MINT email notifications
=========================
Three notification flows:

  1. New unscheduled work order   -> notify the recipient list immediately.
  2. Work order Closed & Completed -> notify when any WO (scheduled OR
                                     unscheduled) is marked complete.
  3. Weekly scheduled digest       -> scheduled work due this coming week PLUS
                                     any past-due scheduled work still pending.

Emails are sent as HTML (with a plain-text fallback) laid out like the MINT/UWO
notification format: a coloured header banner, label/value rows, and a
"Link to Item" button back to the work order in the MINT app.

Transport is plain SMTP, so it works with a self-serve **Gmail App Password**
(no OAuth / admin consent needed) or any other SMTP server (e.g. Office 365):
  * Gmail:  SMTP_HOST=smtp.gmail.com  SMTP_PORT=587  SMTP_USER=<you>@gmail.com
            SMTP_PASSWORD=<16-char app password from myaccount.google.com/apppasswords>

This is intentionally INERT until it is configured. Nothing is sent unless:
  * EMAIL_ENABLED=1 in the environment, AND
  * SMTP settings (host/from) are provided, AND
  * there is at least one recipient in mint_data/email_recipients.json.

So it is safe to ship now and simply "switch on" once the email list is
available - just drop the addresses into email_recipients.json (or POST them to
the /api/email/recipients endpoint) and set the SMTP env vars.

Environment variables (all optional; see .env.example):
    EMAIL_ENABLED       "1" to actually send; anything else = dry-run/log only
    SMTP_HOST           mail server host (e.g. smtp.gmail.com)
    SMTP_PORT           default 587
    SMTP_USER           login user (Gmail: your full address)
    SMTP_PASSWORD       login password (Gmail: the App Password)
    SMTP_FROM           From: address (defaults to SMTP_USER)
    SMTP_STARTTLS       "1" (default) to use STARTTLS
    MINT_BASE_URL       base URL of the MINT app for the "Link to Item" button
                        (e.g. http://mint.local:5000). Default http://localhost:5000
"""

import html as _html
import json
import os
import smtplib
import ssl
from email.message import EmailMessage

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

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
    # Gmail-friendly: GMAIL_ADDRESS + GMAIL_APP_PASSWORD are accepted as
    # shortcuts for the generic SMTP_* vars, and the Gmail host/port are used
    # by default when a Gmail app password is supplied. Generic SMTP_* still
    # wins if explicitly set (so Office 365 / other servers keep working).
    gmail_pw = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    gmail_addr = os.environ.get("GMAIL_ADDRESS", "").strip()

    password = os.environ.get("SMTP_PASSWORD", "") or gmail_pw
    user = (os.environ.get("SMTP_USER", "").strip() or gmail_addr)
    frm = (os.environ.get("SMTP_FROM", "").strip() or user)
    host = (os.environ.get("SMTP_HOST", "").strip()
            or ("smtp.gmail.com" if gmail_pw else ""))

    # If credentials are present, default to ON (the operator clearly configured
    # email); they can still force dry-run with EMAIL_ENABLED=0.
    has_creds = bool(password and host and frm)
    enabled_default = "1" if has_creds else "0"
    enabled = os.environ.get("EMAIL_ENABLED", enabled_default).strip() == "1"

    return {
        "enabled": enabled,
        "host": host,
        "port": int(os.environ.get("SMTP_PORT", "587") or 587),
        "user": user,
        "password": password,
        "from": frm,
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
def _send(subject: str, text_body: str, recipients: list[str],
          html_body: str = "") -> dict:
    """Send one email (HTML with a plain-text fallback) to the recipient list.
    When email is not fully configured/enabled this becomes a no-op that just
    reports what WOULD be sent (so the app works today and can be switched on
    later)."""
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
    msg.set_content(text_body)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

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
# HTML rendering (mirrors the MINT/UWO notification layout)
# --------------------------------------------------------------------------- #
BANNER_COLOR = "#1c9aad"   # teal header bar, matches the MINT notification style


def _base_url() -> str:
    return (os.environ.get("MINT_BASE_URL", "") or "http://localhost:5000").rstrip("/")


def _item_link(wo_id: str) -> str:
    """Deep link that opens the work order modal in the MINT app."""
    return f"{_base_url()}/?wo={_html.escape(str(wo_id), quote=True)}"


def _esc(v) -> str:
    return _html.escape("" if v is None else str(v))


def _fmt_money(v) -> str:
    s = str(v or "").strip().lstrip("$").replace(",", "")
    try:
        return f"${float(s):,.2f}"
    except (TypeError, ValueError):
        return _esc(v) if v else "$0.00"


def _rows_html(rows: list[tuple[str, str]]) -> str:
    """Render label/value rows. `rows` is a list of (label, value); values are
    already-escaped strings (or plain text that we escape here)."""
    out = []
    for label, value in rows:
        out.append(
            '<tr>'
            f'<td style="padding:6px 16px 6px 0;vertical-align:top;font-weight:bold;'
            f'color:#111;white-space:nowrap;">{_esc(label)}</td>'
            f'<td style="padding:6px 0;vertical-align:top;color:#222;">{value}</td>'
            '</tr>'
        )
    return "\n".join(out)


def _shell(title: str, sections_html: str, wo_id: str) -> str:
    """Wrap the header banner + body sections + Link to Item button."""
    link = _item_link(wo_id)
    return f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f6f8;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#f4f6f8;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="640" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border:1px solid #e2e8f0;font-family:Arial,Helvetica,sans-serif;">
        <tr>
          <td style="background:{BANNER_COLOR};color:#ffffff;font-size:16px;font-weight:bold;
                     padding:14px 20px;">{_esc(title)}</td>
        </tr>
        <tr><td style="padding:20px 24px;">
          {sections_html}
          <div style="margin-top:24px;font-size:14px;color:#333;">
            Link to Item:
            <a href="{link}"
               style="display:inline-block;margin-left:8px;padding:6px 14px;background:{BANNER_COLOR};
                      color:#ffffff;text-decoration:none;font-weight:bold;border-radius:3px;">
              WO ID {_esc(wo_id)}</a>
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def _divider() -> str:
    return '<hr style="border:none;border-top:1px solid #d9dee4;margin:18px 0;">'


# --------------------------------------------------------------------------- #
# High-level notifications
# --------------------------------------------------------------------------- #
def notify_new_unscheduled(wo: dict, dept_label: str = "") -> dict:
    """Called when a new unscheduled work order is created."""
    wo_id = wo.get("wo_id", "?")
    dept = dept_label or wo.get("department", "")
    subject = f"[MINT] New unscheduled work order {wo_id} - {wo.get('equipment_name', '')}"

    rows = [
        ("Department:", _esc(dept)),
        ("Reported By:", _esc(wo.get("created_by", ""))),
        ("Equipment Affected:", _esc(wo.get("equipment_name", ""))),
        ("Problem:", _esc(wo.get("problem", ""))),
        ("Urgency:", _esc(wo.get("urgency", ""))),
        ("Date Reported:", _esc(wo.get("date_notified", ""))),
    ]
    sections = ('<table role="presentation" cellpadding="0" cellspacing="0" '
                f'style="font-size:14px;width:100%;">{_rows_html(rows)}</table>')
    title = ("This is a notification to inform you that a new unscheduled "
             "work order has been created")
    html_body = _shell(title, sections, wo_id)

    text_body = (
        f"A new unscheduled work order was created in MINT.\n\n"
        f"Work Order: {wo_id}\n"
        f"Department: {dept}\n"
        f"Reported By: {wo.get('created_by', '')}\n"
        f"Equipment Affected: {wo.get('equipment_name', '')} ({wo.get('equipment_eq_id', '')})\n"
        f"Urgency: {wo.get('urgency', '')}\n"
        f"Date Reported: {wo.get('date_notified', '')}\n\n"
        f"Problem:\n{wo.get('problem', '')}\n\n"
        f"Link to item: {_item_link(wo_id)}\n"
    )
    return _send(subject, text_body, get_recipients(), html_body)


def notify_completed(wo: dict, dept_label: str = "") -> dict:
    """Called when any work order (scheduled OR unscheduled) is marked
    Closed & Completed. Mirrors the UWO 'Closed and Completed' email layout."""
    wo_id = wo.get("wo_id", "?")
    dept = dept_label or wo.get("department", "")
    subject = (f"[MINT] Work order {wo_id} Closed and Completed - "
               f"{wo.get('equipment_name', '')}")

    top_rows = [
        ("Department:", _esc(dept)),
        ("Reported By:", _esc(wo.get("reported_by") or wo.get("created_by", ""))),
        ("Equipment Affected:", _esc(wo.get("equipment_name", ""))),
        ("Problem:", _esc(wo.get("problem", ""))),
        ("Urgency:", _esc(wo.get("urgency", ""))),
        ("Owner:", _esc(wo.get("owner", ""))),
    ]
    comment = (wo.get("completion_comments") or wo.get("comments")
               or wo.get("solution") or "")
    bottom_rows = [
        ("Comment:", _esc(comment)),
        ("Material Cost:", _esc(_fmt_money(wo.get("material_cost")))),
        ("Labor Time:", _esc(wo.get("labor_time", "") or "0")),
        ("Work Performed By:", _esc(wo.get("work_performed_by", ""))),
        ("Completed By:", _esc(wo.get("completed_by", ""))),
        ("Completed Date:", _esc(wo.get("completed_datetime", ""))),
        ("Down Time Hours:", _esc(wo.get("downtime_hours", "") or "0")),
    ]
    sections = (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="font-size:14px;width:100%;">{_rows_html(top_rows)}</table>'
        f'{_divider()}'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="font-size:14px;width:100%;">{_rows_html(bottom_rows)}</table>'
    )
    title = ("This is notification to inform you that the request to service "
             "the equipment below is Closed and Completed")
    html_body = _shell(title, sections, wo_id)

    text_body = (
        f"Work order {wo_id} is Closed and Completed.\n\n"
        f"Department: {dept}\n"
        f"Equipment Affected: {wo.get('equipment_name', '')}\n"
        f"Problem: {wo.get('problem', '')}\n"
        f"Comment: {comment}\n"
        f"Material Cost: {_fmt_money(wo.get('material_cost'))}\n"
        f"Labor Time: {wo.get('labor_time', '') or '0'}\n"
        f"Work Performed By: {wo.get('work_performed_by', '')}\n"
        f"Completed Date: {wo.get('completed_datetime', '')}\n"
        f"Down Time Hours: {wo.get('downtime_hours', '') or '0'}\n\n"
        f"Link to item: {_item_link(wo_id)}\n"
    )
    return _send(subject, text_body, get_recipients(), html_body)


def _fmt_wo_line(r: dict) -> str:
    return (f"  - {r.get('wo_id','?')}  {r.get('equipment_name','')} "
            f"({r.get('equipment_eq_id','')}) - due {r.get('due_date','')} "
            f"[{r.get('status','')}]")


def _digest_table(records: list[dict]) -> str:
    if not records:
        return '<p style="margin:6px 0 0;color:#777;font-size:14px;">(none)</p>'
    header = (
        '<tr style="background:#eef2f5;">'
        '<th align="left" style="padding:6px 10px;font-size:13px;">WO</th>'
        '<th align="left" style="padding:6px 10px;font-size:13px;">Equipment</th>'
        '<th align="left" style="padding:6px 10px;font-size:13px;">Department</th>'
        '<th align="left" style="padding:6px 10px;font-size:13px;">Due</th>'
        '<th align="left" style="padding:6px 10px;font-size:13px;">Status</th>'
        '</tr>'
    )
    rows = []
    for r in records:
        wid = r.get("wo_id", "?")
        rows.append(
            '<tr style="border-top:1px solid #e2e8f0;">'
            f'<td style="padding:6px 10px;font-size:13px;">'
            f'<a href="{_item_link(wid)}" style="color:{BANNER_COLOR};text-decoration:none;'
            f'font-weight:bold;">{_esc(wid)}</a></td>'
            f'<td style="padding:6px 10px;font-size:13px;">{_esc(r.get("equipment_name",""))}</td>'
            f'<td style="padding:6px 10px;font-size:13px;">{_esc(r.get("department",""))}</td>'
            f'<td style="padding:6px 10px;font-size:13px;">{_esc(r.get("due_date",""))}</td>'
            f'<td style="padding:6px 10px;font-size:13px;">{_esc(r.get("status",""))}</td>'
            '</tr>'
        )
    return ('<table role="presentation" cellpadding="0" cellspacing="0" width="100%" '
            f'style="border-collapse:collapse;margin-top:6px;">{header}{"".join(rows)}</table>')


def send_weekly_digest(due_this_week: list[dict], overdue: list[dict]) -> dict:
    """Weekly summary: scheduled work due this week PLUS past-due scheduled work
    that is still pending."""
    subject = "[MINT] Weekly scheduled maintenance summary"

    sections = f"""\
<h3 style="margin:0 0 4px;font-size:15px;color:#111;">Due this coming week ({len(due_this_week)})</h3>
{_digest_table(due_this_week)}
{_divider()}
<h3 style="margin:0 0 4px;font-size:15px;color:#b91c1c;">Past due &amp; still pending ({len(overdue)})</h3>
{_digest_table(overdue)}"""

    link = _base_url()
    html_body = f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f6f8;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
         style="background:#f4f6f8;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="680" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border:1px solid #e2e8f0;font-family:Arial,Helvetica,sans-serif;">
        <tr><td style="background:{BANNER_COLOR};color:#ffffff;font-size:16px;font-weight:bold;
                       padding:14px 20px;">Weekly Scheduled Maintenance Summary</td></tr>
        <tr><td style="padding:20px 24px;">
          {sections}
          <div style="margin-top:24px;font-size:14px;">
            <a href="{link}" style="display:inline-block;padding:6px 14px;background:{BANNER_COLOR};
               color:#ffffff;text-decoration:none;font-weight:bold;border-radius:3px;">Open MINT</a>
          </div>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

    lines = ["Scheduled maintenance summary\n"]
    lines.append(f"Due this coming week ({len(due_this_week)}):")
    lines += [_fmt_wo_line(r) for r in due_this_week] or ["  (none)"]
    lines.append("")
    lines.append(f"Past due & still pending ({len(overdue)}):")
    lines += [_fmt_wo_line(r) for r in overdue] or ["  (none)"]
    lines.append("")
    lines.append(f"Open MINT: {link}")
    return _send(subject, "\n".join(lines), get_recipients(), html_body)


# --------------------------------------------------------------------------- #
# Test / verification
# --------------------------------------------------------------------------- #
def send_test(address: str) -> dict:
    """Send a sample 'Closed & Completed' email to a single address to verify
    SMTP is configured. Bypasses the saved recipient list; sends only to the
    supplied address."""
    sample = {
        "wo_id": "20078",
        "department": "General",
        "created_by": "Erik Beltran",
        "equipment_name": "General Maintenace Requests",
        "problem": ("I was provided with an outdated standing desk, this desk "
                    "does not hold by itself when raised to the top."),
        "urgency": "6. General Maint",
        "owner": "Maintenance",
        "comments": "replaced sit/stand rewired cables.",
        "material_cost": "0",
        "labor_time": "1.00",
        "work_performed_by": "Shinobi",
        "completed_datetime": "07/13/2026 1:30 AM",
        "downtime_hours": "0.00",
    }
    # Reuse the completed-email renderer but force the single test recipient.
    wo_id = sample["wo_id"]
    top_rows = [
        ("Department:", _esc(sample["department"])),
        ("Reported By:", _esc(sample["created_by"])),
        ("Equipment Affected:", _esc(sample["equipment_name"])),
        ("Problem:", _esc(sample["problem"])),
        ("Urgency:", _esc(sample["urgency"])),
        ("Owner:", _esc(sample["owner"])),
    ]
    bottom_rows = [
        ("Comment:", _esc(sample["comments"])),
        ("Material Cost:", _esc(_fmt_money(sample["material_cost"]))),
        ("Labor Time:", _esc(sample["labor_time"])),
        ("Work Performed By:", _esc(sample["work_performed_by"])),
        ("Completed By:", ""),
        ("Completed Date:", _esc(sample["completed_datetime"])),
        ("Down Time Hours:", _esc(sample["downtime_hours"])),
    ]
    sections = (
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="font-size:14px;width:100%;">{_rows_html(top_rows)}</table>'
        f'{_divider()}'
        '<table role="presentation" cellpadding="0" cellspacing="0" '
        f'style="font-size:14px;width:100%;">{_rows_html(bottom_rows)}</table>'
    )
    title = ("This is notification to inform you that the request to service "
             "the equipment below is Closed and Completed")
    html_body = _shell(title, sections, wo_id)
    text_body = ("MINT email test - sample 'Closed and Completed' notification.\n"
                 f"Link to item: {_item_link(wo_id)}\n")
    return _send("[MINT] Test email - Closed and Completed sample",
                 text_body, [address], html_body)


if __name__ == "__main__":
    import sys
    to = sys.argv[1] if len(sys.argv) > 1 else ""
    print("Config status:", json.dumps(status(), indent=2))
    if to:
        print(f"Sending test email to {to} ...")
        print(json.dumps(send_test(to), indent=2))
    else:
        print("Pass a recipient address to send a test, e.g.:")
        print("  python mint_email.py you@example.com")
