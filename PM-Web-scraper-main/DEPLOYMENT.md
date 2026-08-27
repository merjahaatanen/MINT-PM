# PM Dashboard - VM Deployment & Nightly Update Guide

This app runs as an always-on service on a Windows VM. It:

- Serves the maintenance dashboard on your LAN.
- Every night, rescrapes all 9 departments, regenerates checklists for machines
  that gained **new** work orders (preserving manual edits), archives every
  checklist first, and hot-reloads the frontend.
- Shows weekly (Sunday -> Sunday) schedules per department and overall.

---

## 1. One-time setup on the VM

```powershell
# From the project folder
pip install -r requirements.txt
```

Create your `.env` (copy from `.env.example`) with your real keys:

```
GEMINI_API_KEY=...
OLLAMA_API_KEY=...
```

> **Security:** `.env` is now git-ignored. Never commit it. The keys previously
> pushed to GitHub are exposed in git history and **must be rotated** (see
> section 6).

---

## 2. Copy the write-store database to the VM

The `mint_data/` folder is **git-ignored** because it contains the live SQLite
write store (`mint.db`) plus attachments and email state. `mint.db` holds all
user-created/edited data:

- Machine Info / longevity records
- Inactive (soft-deleted) departments and machines
- Manual work orders
- Work-order field overrides and solutions
- Vendor contacts
- Floor-plan layouts, machine nicknames, spare-parts inventory
- Calendar events and chart markers

A fresh clone or pull creates an empty `mint.db` automatically, so **after the
initial deployment you must copy your existing `mint_data/` from the source
machine to the VM**. The simplest way on a LAN share or OneDrive is:

```powershell
# Run from the source machine (adjust the VM path)
robocopy "C:\Users\<you>\Desktop\MINT+PM\PM-Web-scraper-main\mint_data" \
         "\\<vm-name>\C$\Users\<vm-user>\Desktop\MINT+PM\PM-Web-scraper-main\mint_data" \
         /E /COPY:DAT
```

Or copy the folder manually and paste it into `PM-Web-scraper-main\` on the VM
before starting the server. If you ever re-image the VM, repeat this step.

> **Note:** If Machine Info shows `N/A` for every field and the Inactive tab is
> empty on the VM, the `mint_data\mint.db` file is almost certainly missing or
> stale.

---

## 3. Chrome login session (required for scraping)

The nightly scrape drives ONE Chrome window that is logged into the PM site.
The remote-debugging **port is auto-detected** (it may differ per machine), so
you do not need to hard-code it.

- When you run `python server.py`, it **spins up Chrome automatically** using
  the profile at `%LOCALAPPDATA%\Google\Chrome\PM_Debug_Profile` and captures
  the port.
- The first time, **log into the PM site in that Chrome window** and leave it
  open. The session is reused every night.
- If Chrome lives in a non-standard path, set `CHROME_PATH` in `.env`.
- To launch/inspect Chrome manually:

```powershell
python chrome_session.py            # launches + prints the captured port
python chrome_session.py --status   # shows the running port + login state
```

If the session expires, the nightly job **skips the scrape** (it never
overwrites good data with a failed login) and reports it in the status.

---

## 4. Run the server (always-on)

```powershell
python server.py
```

You will see:

```
  Local:   http://127.0.0.1:5000
  Network: http://<this-machine-ip>:5000  (reachable on your LAN)
[chrome] debug Chrome ready on port <auto>
[nightly] scheduled daily at 02:00 local time
```

To keep it running 24/7, use **Task Scheduler** with a task that runs
`python server.py` "at startup" / "at log on", set to restart on failure. (You
chose the Python-only/APScheduler approach, so the single `server.py` process is
both the web server and the nightly scheduler - keep it alive.)

---

## 5. LAN access (same Wi-Fi)

The server binds to `0.0.0.0` by default, so it is reachable at
`http://<VM-IP>:5000`. Open the firewall port once (Admin PowerShell):

```powershell
New-NetFirewallRule -DisplayName "PM Dashboard" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5000
```

Find the VM's IP with `ipconfig` (IPv4 Address). Anyone on the same network
opens `http://<that-ip>:5000`.

---

## 6. Nightly update behavior

Configurable via `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `NIGHTLY_HOUR` / `NIGHTLY_MINUTE` | `2` / `0` | When the nightly job runs |
| `NIGHTLY_ENABLED` | `1` | `0` disables auto-scheduling |
| `NIGHTLY_DEPT_TIMEOUT` | `14400` | Per-department scrape timeout (seconds) |
| `SERVER_HOST` / `SERVER_PORT` | `0.0.0.0` / `5000` | Bind address/port |
| `CHROME_AUTOSTART` | `1` | `0` = don't auto-launch Chrome |
| `CHROME_DEBUG_PORT` | `0` | `0` = auto-pick + capture; or pin a port |

Each night, in order:

1. **Archive** every checklist into `guides/archive/<timestamp>/`.
2. **Rescrape** all departments sequentially (all sections, comments,
   attachments; scheduled + unscheduled). Results are written to temp files and
   only promoted if valid - an expired login cannot wipe good data.
3. **Merge** into the master `work_orders_*.json/.csv`.
4. **Regenerate** checklists only for machines with **new** unscheduled work
   orders, **preserving manual edits**.
5. **Reload** the frontend data (no restart needed).

Trigger a run on demand or check status:

```
POST /api/nightly/run       # start a cycle now (background)
GET  /api/nightly/status    # progress, last run, summary, errors
```

Or run the whole cycle from the command line:

```powershell
python nightly_update.py
```

---

## 7. Manual-edit preservation (how it works)

When someone edits a checklist in the UI and saves:

- The previous version is backed up (`guides/<eq>.<timestamp>.bak.md`).
- The change is diffed and appended to an **append-only edit log**
  (`guides/edits/<eq>.json`).
- That log is rendered into an "OPERATOR EDITS THAT MUST BE PRESERVED" block
  that is injected into **every** future prompt - so edits survive even a full
  regeneration. Editing an edit just appends another entry (latest wins), so
  edits-of-edits are handled automatically.

---

## 8. IMPORTANT: rotate leaked API keys

The earlier `git push` committed `.env` with real keys, and it is in the repo's
git history (public). **Rotate both keys now:**

- Gemini: Google AI Studio -> API keys -> revoke the old key, create a new one.
- Ollama Cloud: revoke/regenerate the key in your Ollama account.

Then put the new keys in `.env` (which is no longer tracked). To scrub the keys
from history entirely you'd need to rewrite history (e.g. `git filter-repo`) and
force-push - rotating the keys is the essential step regardless.
