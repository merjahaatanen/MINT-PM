\documentclass[11pt,a4paper]{article}

% ---------------------------------------------------------------------------
% MINT + PM Web Scraper -- Technical Design Document
% Copy this whole file into Overleaf as main.tex and compile with pdfLaTeX.
% ---------------------------------------------------------------------------

\usepackage[margin=2.5cm]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{graphicx}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{array}
\usepackage{enumitem}
\usepackage{xcolor}
\usepackage{listings}
\usepackage{hyperref}
\usepackage{tabularx}
\usepackage{parskip}

\hypersetup{
    colorlinks=true,
    linkcolor=blue!60!black,
    urlcolor=blue!60!black,
    pdftitle={MINT + PM Web Scraper -- Technical Design Document},
    pdfauthor={Bobrick Washroom Equipment -- Maintenance Engineering}
}

\definecolor{codebg}{RGB}{245,245,245}
\lstset{
    basicstyle=\ttfamily\small,
    backgroundcolor=\color{codebg},
    frame=single,
    framerule=0pt,
    breaklines=true,
    columns=fullflexible,
    keepspaces=true,
    showstringspaces=false
}

\newcommand{\code}[1]{\texttt{#1}}

\title{\textbf{MINT + PM Web Scraper}\\[4pt]
       \Large Technical Design Document\\[4pt]
       \normalsize BLA Maintenance Dashboard \& Preventive-Maintenance Data Platform}
\author{Bobrick Washroom Equipment --- Maintenance Engineering}
\date{\today}

\begin{document}

\maketitle
\tableofcontents
\newpage

% ===========================================================================
\section{Introduction}
% ===========================================================================

\subsection{Purpose}
This document describes the architecture, data model, components, APIs, and
operational procedures of the \textbf{MINT} application (Maintenance
INTelligence) and its companion \textbf{PM Web Scraper}. Together they form a
self-hosted maintenance-management platform for the Bobrick BLA facility that:

\begin{itemize}[nosep]
    \item Scrapes historical work-order data from the legacy Bobrick PM system
          (\code{circaweb.bobrick.com/PME}) using a live, logged-in Chrome
          browser session.
    \item Serves a modern single-page web dashboard with a four-level
          drill-down: \emph{Company} $\rightarrow$ \emph{Division} $\rightarrow$
          \emph{Department} $\rightarrow$ \emph{Machine}.
    \item Acts as the \emph{system of record going forward}: users create and
          edit work orders, machines, departments, vendor contacts and machine
          information directly in MINT, layered on top of the frozen scraped
          snapshot.
    \item Generates and maintains AI-assisted per-machine troubleshooting
          checklists (LLM-backed, with full version history and manual-edit
          preservation).
    \item Sends e-mail notifications for new unscheduled work orders,
          completed work orders, and a weekly scheduled-maintenance digest.
\end{itemize}

\subsection{Scope}
The system runs as a single Python/Flask process on a Windows VM, reachable on
the local LAN. All state is stored on the local file system (JSON snapshots,
SQLite databases, Markdown checklists, uploaded attachments). No external
services are required except optional LLM providers (Ollama Cloud or Google
Gemini) and an optional SMTP server for notifications.

\subsection{Terminology}
\begin{description}[nosep]
    \item[PM system] The legacy Bobrick preventive-maintenance web application
          being scraped.
    \item[MINT] The new dashboard application (this project).
    \item[WO] Work order. \emph{Unscheduled} (breakdown/repair) or
          \emph{Scheduled} (preventive maintenance).
    \item[EQ ID] Equipment identifier. Machines are addressed by the numeric
          portion of their equipment ID.
    \item[Machine / Equipment] Used interchangeably throughout the codebase.
    \item[Checklist / Guide] The per-machine AI troubleshooting checklist
          (Markdown, stored under \code{guides/}).
\end{description}

% ===========================================================================
\section{System Overview}
% ===========================================================================

\subsection{High-Level Architecture}
The platform consists of five logical layers:

\begin{enumerate}[nosep]
    \item \textbf{Acquisition layer} --- \code{scraper.py},
          \code{chrome\_session.py}, \code{run\_parallel.py},
          \code{nightly\_update.py}. Drives a real, already-logged-in Chrome
          browser via the DevTools remote-debugging protocol (Selenium attach)
          to extract every work order, comment, and attachment from the PM
          system. Output is written to per-department JSON/CSV snapshot files.
    \item \textbf{Storage layer} --- Frozen scraped JSON snapshots (read-only),
          plus two SQLite databases: \code{mint\_data/mint.db}
          (\code{mint\_store.py}) for all user-created/edited data and
          \code{guides/versions.db} (\code{version\_store.py}) for checklist
          version history. Uploaded files live in
          \code{mint\_data/attachments/}.
    \item \textbf{Application layer} --- \code{server.py}, a Flask application
          ($\sim$3{,}200 lines) that merges the frozen snapshot with the live
          store at load time, exposes a JSON REST API, and hosts background
          schedulers (weekly e-mail digest, recurring-PM generation, Chrome
          session bootstrap).
    \item \textbf{Intelligence layer} --- \code{analyze\_equipment.py},
          \code{guide\_engine.py}. LLM-backed trend analysis, monthly
          synopses, note parsing, and troubleshooting-checklist generation
          via Ollama Cloud (default) or Google Gemini.
    \item \textbf{Presentation layer} --- \code{webapp/index.html}, a single
          self-contained single-page application built with React (via
          \code{htm} + Babel standalone), Tailwind CSS, \code{marked} for
          Markdown rendering and \code{jsPDF} for PDF export. All frontend
          dependencies are vendored locally in \code{webapp/vendor/} so the
          app works with no internet access.
\end{enumerate}

\subsection{Data-Flow Summary}
\begin{lstlisting}
 Legacy PM site (circaweb.bobrick.com/PME)
        |  (logged-in Chrome, DevTools port auto-detected)
        v
 scraper.py / run_parallel.py / nightly_update.py
        |  writes
        v
 work_orders_{unscheduled|scheduled}_<dept>.json/.csv   (frozen snapshot)
        |                                 mint_data/mint.db (user writes)
        |                                 guides/*.md + versions.db
        v                                 /
 server.py  reload_data(): merge snapshot + store, apply overrides,
        |   hide inactive items, index by wo_id
        v
 JSON REST API (/api/...)  <---->  webapp/index.html (React SPA)
        |
        v
 mint_email.py (SMTP notifications, inert until configured)
\end{lstlisting}

\subsection{Key Design Principles}
\begin{description}[nosep]
    \item[Frozen snapshot + write layer] Scraped PM data is never mutated.
          All user changes live in SQLite and are merged over the snapshot at
          load time. Field-level edits to \emph{any} WO (scraped or manual)
          are stored as JSON patches in \code{wo\_overrides}.
    \item[Attributability] Every mutating API call requires an \code{author}
          (``Your name'' field) and writes an \code{audit\_log} row.
    \item[Soft deletion] Departments and machines are never hard-deleted;
          they are flagged in \code{inactive\_items} and can be restored.
    \item[Manual-edit preservation] Operator edits to AI checklists are
          diffed into an append-only edit log that is re-injected into every
          future LLM prompt, so edits survive full regeneration.
    \item[Fail-safe scraping] Nightly rescrape writes to temp files and only
          promotes them over live data when the result looks valid; an
          expired login can never wipe good data.
    \item[Offline-first frontend] All JS/CSS dependencies are vendored; the
          app requires no CDN or internet connection to render.
\end{description}

% ===========================================================================
\section{Repository Layout}
% ===========================================================================

\begin{longtable}{@{}>{\ttfamily}p{0.42\textwidth}p{0.53\textwidth}@{}}
\toprule
\textbf{Path} & \textbf{Description} \\
\midrule
\endhead
server.py & Flask backend, API, background schedulers ($\sim$3{,}200 lines). \\
mint\_store.py & SQLite write store for all user-created data. \\
mint\_email.py & SMTP notification module (inert until configured). \\
version\_store.py & Append-only checklist version history (SQLite). \\
guide\_engine.py & Checklist create/edit/archive/regenerate engine. \\
analyze\_equipment.py & LLM trend analysis, stats, prompt building, transport. \\
scraper.py & Live-browser PM work-order scraper ($\sim$1{,}900 lines). \\
chrome\_session.py & Persistent logged-in debug-Chrome session manager. \\
run\_parallel.py & Multi-department scrape orchestration. \\
nightly\_update.py & Full rescrape + checklist regeneration cycle. \\
build\_vendor\_contacts.py & Builds the vendor contact list from source CSVs. \\
scrape\_vendor\_details.py & Scrapes vendor detail pages. \\
import\_equipment\_summary.py & Imports scraped equipment-summary pages. \\
import\_machine\_longevity.py & Imports the machine-longevity CSV into machine\_info. \\
capture\_unscheduled\_all.py & Captures the site-wide unscheduled-WO listing. \\
parse\_html.py & HTML parsing helpers for captured pages. \\
equipment\_viewer.py / work\_order\_viewer.py & Standalone CLI data viewers. \\
webapp/index.html & The entire SPA frontend (single file). \\
webapp/vendor/ & Vendored react, react-dom, htm, babel, tailwind, marked, jspdf. \\
guides/ & Live checklists, baselines/, edits/, archive/, versions.db. \\
mint\_data/ & mint.db, attachments/, email\_recipients.json (git-ignored). \\
pages/ & Offline mirror of every scraped PM page ($\sim$8{,}000 files). \\
work\_orders\_*.json/.csv & Frozen scraped snapshots (master + per department). \\
equipment\_data.json/.csv & Scraped equipment master list. \\
start\_chrome\_debug.bat & Manual launcher for the debug Chrome profile. \\
requirements.txt & Python dependencies. \\
DEPLOYMENT.md & VM deployment and nightly-update guide. \\
.env & Local secrets/config (git-ignored; see \S\ref{sec:config}). \\
\bottomrule
\end{longtable}

% ===========================================================================
\section{Acquisition Layer (Scraping)}
% ===========================================================================

\subsection{Chrome Session Management (\code{chrome\_session.py})}
The scraper attaches to a real Chrome browser that a human has logged into the
PM site, so no credentials are stored in code. Because the remote-debugging
port is not fixed on the VM:

\begin{enumerate}[nosep]
    \item Chrome is launched once with a dedicated profile
          (\code{\%LOCALAPPDATA\%\textbackslash Google\textbackslash
          Chrome\textbackslash PM\_Debug\_Profile}) and
          \code{--remote-debugging-port=0}, letting Chrome pick a free port.
    \item The chosen port is read from the \code{DevToolsActivePort} file that
          Chrome writes into the profile directory (the official discovery
          mechanism), then verified by hitting the debug endpoint.
    \item The window is left open so a human can re-login if the session
          expires; the same session is reused for every scrape.
\end{enumerate}

Standalone usage: \code{python chrome\_session.py} (launch and print port) or
\code{python chrome\_session.py --status} (report port and login state).
Overrides: \code{CHROME\_PATH}, \code{CHROME\_PROFILE},
\code{CHROME\_DEBUG\_PORT} (0 = auto).

\subsection{Work-Order Scraper (\code{scraper.py})}
For every equipment record, the scraper:
\begin{enumerate}[nosep]
    \item Opens the Equipment Dashboard
          (\code{/PME/Forms/EquipmentDash/<id>}).
    \item Iterates every row of the \emph{Unscheduled Work Orders} grid,
          clicking ``Edit Work Order'' on each.
    \item Captures from the dialog: date notified, urgency, problem, status,
          material cost, labor time, work performed by, downtime hours,
          completed date/time; the full comment log with timestamps; and all
          attachment filenames.
    \item Repeats the same for the \emph{Scheduled} (PM) grid.
\end{enumerate}

Results are written to \code{work\_orders\_unscheduled.csv/.json} and
\code{work\_orders\_scheduled.csv/.json}. Every visited page is also mirrored
into \code{./pages/} (form state baked in, scripts stripped), enabling a full
\textbf{offline replay} with \code{python scraper.py --from-html} against a
private headless Chrome --- ideal for testing scraper changes without touching
the live site. A \code{--limit N} flag supports quick smoke tests.

\subsection{Nightly Update Cycle (\code{nightly\_update.py})}
One cycle (runnable manually via \code{python nightly\_update.py}) performs,
in order:

\begin{enumerate}[nosep]
    \item \textbf{Archive} every current checklist into
          \code{guides/archive/<timestamp>/}.
    \item \textbf{Snapshot} current unscheduled WO IDs per machine (to detect
          what is new after the scrape).
    \item \textbf{Rescrape} all nine departments \emph{sequentially} through
          the single logged-in Chrome. Results go to temp files first and are
          only \emph{promoted} over live per-department files when valid.
          A per-department timeout (\code{NIGHTLY\_DEPT\_TIMEOUT}, default
          4\,h) kills hung departments so the run continues.
    \item \textbf{Merge} per-department files into the master
          \code{work\_orders\_*.json/.csv}.
    \item \textbf{Regenerate} checklists only for machines that gained new
          unscheduled WOs, preserving all manual edits (\S\ref{sec:guides}).
    \item \textbf{Reload} the server's in-memory data so the frontend updates
          immediately (via a reload callback).
\end{enumerate}

% ===========================================================================
\section{Storage Layer}
% ===========================================================================

\subsection{Frozen Scraped Snapshots}
Scraped data lives in flat JSON/CSV files per department, e.g.
\code{work\_orders\_unscheduled\_toilet\_partitions.json}. These files are
treated as read-only historical truth; the application never rewrites them
outside the nightly promotion step. The equipment master lives in
\code{equipment\_data.json/.csv}.

\subsection{MINT Write Store (\code{mint\_store.py}, SQLite)}
\label{sec:store}
Database: \code{mint\_data/mint.db}. A module-level \code{threading.Lock}
serializes writes so ID assignment stays atomic under Flask's threaded server.
Manual work orders receive the prefix \code{M-} (starting at 1000) so they can
never collide with the purely numeric PM WO numbers.

\begin{longtable}{@{}>{\ttfamily}p{0.24\textwidth}p{0.71\textwidth}@{}}
\toprule
\textbf{Table} & \textbf{Purpose / key columns} \\
\midrule
\endhead
divisions & Company $\rightarrow$ division layer. \code{key} (PK), \code{name}. The \code{bla} division is seeded at init. \\
departments & User-added departments: \code{key} (PK), \code{name}, \code{label}, \code{division\_key} (default \code{bla}), audit columns. Scraped departments are configured statically in \code{server.py}. \\
machines & User-added machines: \code{eq\_id}, \code{equipment\_name}, \code{dept\_key}, \code{make}, \code{model}, \code{vendor}, \code{asset\_num}. \\
work\_orders & Manually created WOs: \code{wo\_id} (PK, \code{M-} prefixed), \code{wo\_type}, \code{department\_key}, equipment fields, and the full record as \code{data\_json}. \\
wo\_overrides & Field-level edits applied to \emph{any} WO (scraped or manual): \code{wo\_id} (PK) $\rightarrow$ JSON patch, \code{updated\_at/by}. \\
solutions & Append-only ``Solution'' log entries per WO (used instead of comments for unscheduled WOs). \\
attachments & Uploaded file metadata; the bytes live in \code{mint\_data/attachments/} under a generated \code{stored\_name}. \\
inactive\_items & Soft-delete flags. PK is (\code{item\_type}, \code{item\_key}); for departments \code{item\_key} = dept key, for machines \code{item\_key} = \code{"<dept\_key>:<eq\_id>"}. \\
audit\_log & Who did what and when: \code{wo\_id}, \code{author}, \code{action}, \code{detail}, \code{created\_at}. Written by every mutation. \\
calendar\_events & User calendar entries (date, department, equipment, title, description); indexed by date and department. \\
chart\_events & Global timeline markers rendered as vertical lines on every trend chart. \\
machine\_info & Editable machine information / longevity data, PK (\code{dept\_key}, \code{eq\_id}): category, location/workcenter, serial number, asset number, year new, condition, service status, replacement cost/year, ERY, comments, plus an LLM-parsed \code{summary\_json}. \\
technicians & Technician roster with JSON \code{aliases} and an \code{active} flag; used for assignment and team stats. \\
vendor\_contacts & Editable Vendor \& Utilities list: company, contact, address, phone/cell/fax, email, type, service-contract fields, and \code{machine\_eq} (JSON list of linked EQ IDs). \\
vendor\_contact\_types & Contact-type lookup (seeded with \code{TPF} and \code{Facility}); deletion is blocked while contacts still reference the type. \\
\bottomrule
\end{longtable}

Schema creation is idempotent (\code{CREATE TABLE IF NOT EXISTS}) and includes
a lightweight migration step (e.g.\ adding \code{summary\_json} to older
\code{machine\_info} schemas).

\subsection{Checklist Version Store (\code{version\_store.py}, SQLite)}
Database: \code{guides/versions.db}. Every change to a checklist --- AI
generated, AI updated, or hand-edited --- is recorded as a new immutable
version containing the \emph{full} Markdown. Rows store \code{eq\_id},
1-based \code{version\_number}, \code{content}, \code{source} (one of
\code{ai\_generate}, \code{ai\_update}, \code{manual\_edit}, \code{import},
\code{restore}), \code{author}, timestamp, and doubly-linked
\code{prev\_id}/\code{next\_id} references so callers can walk the history.
No-op saves are ignored.

% ===========================================================================
\section{Application Layer (\code{server.py})}
% ===========================================================================

\subsection{Startup Sequence}
Running \code{python server.py}:
\begin{enumerate}[nosep]
    \item Loads \code{.env} via \code{python-dotenv}.
    \item Imports the analysis, guide, store, and e-mail modules; performs the
          initial \code{reload\_data()} at import time.
    \item On \code{\_\_main\_\_}: prints access URLs, best-effort launches the
          debug Chrome (\code{CHROME\_AUTOSTART=1} default), starts the weekly
          digest scheduler thread, and runs Flask threaded on
          \code{SERVER\_HOST:SERVER\_PORT} (default \code{0.0.0.0:5000},
          reloader disabled).
\end{enumerate}

\subsection{Data Merge (\code{reload\_data()})}
The core of the read path. It atomically rebuilds three in-memory caches
(\code{\_DEPT\_DATA}, \code{\_WO\_INDEX}, \code{\_EQUIP\_BY\_KEY}) under a
lock:

\begin{enumerate}[nosep]
    \item Refreshes the technician list and rebuilds the live
          \code{DEPARTMENTS} map (static scraped departments + user-added
          ones, minus inactive ones).
    \item Auto-generates due occurrences for recurring scheduled-WO series
          (\S\ref{sec:recurring}).
    \item Loads each department's scheduled/unscheduled JSON, tagging records
          with \code{wo\_type} and \code{department\_key}.
    \item Injects manually created WOs from the store, normalizing date fields
          to canonical \code{MM/DD/YYYY} so they sort correctly.
    \item Builds a global \code{wo\_id} index, applies \code{wo\_overrides}
          patches, and tags per-WO solution/attachment counts.
    \item Filters out soft-deleted machines and their WOs (matched by numeric
          EQ ID within the department), then swaps all caches in atomically.
\end{enumerate}

\subsection{Recurring Scheduled Work Orders}
\label{sec:recurring}
Scheduled WOs with a \code{frequency} form a series (keyed by
\code{series\_id}). Occurrence generation is purely \emph{date-driven}: on
every reload, occurrences are created on a fixed cadence from the seed's first
due date through today plus the next upcoming one, independent of completion
status (so an overdue-but-open PM still spawns its successor). A series can be
permanently stopped via \code{POST /api/workorder/<id>/stop-recurrence}
(admin-gated); the \code{recurrence\_stopped} flag halts all future
generation while keeping existing occurrences.

\subsection{Access Control}
Three independent shared passwords, all supplied via header or JSON body and
all optional (a blank password leaves that gate \emph{open} so a fresh install
is never locked out):

\begin{longtable}{@{}p{0.16\textwidth}>{\ttfamily}p{0.25\textwidth}p{0.51\textwidth}@{}}
\toprule
\textbf{Gate} & \textbf{Env / header} & \textbf{Protects} \\
\midrule
Edit & EDIT\_PASSWORD / X-Edit-Password & Checklist generate/edit/update, vendor-contact and chart-event mutations. \\
Admin & ADMIN\_PASSWORD / X-Admin-Password & Deleting work orders, viewing WO change history, stopping recurrence. \\
Sean (supervisor) & SEAN\_PASSWORD / X-Sean-Password & Work-order assignment and the completed-WO team dashboard. Defaults to \code{"sean"} so it is always protected. \\
\bottomrule
\end{longtable}

In addition, every mutation requires an \code{author} name, persisted to the
audit log (the frontend remembers it in \code{localStorage}).

\subsection{API Reference}
All endpoints return JSON. Read endpoints are unauthenticated on the LAN.

\subsubsection{Navigation and Dashboards}
\begin{longtable}{@{}p{0.10\textwidth}>{\ttfamily\small}p{0.50\textwidth}p{0.33\textwidth}@{}}
\toprule
\textbf{Method} & \textbf{Path} & \textbf{Description} \\
\midrule
\endhead
GET & / & Serves the SPA (\code{webapp/index.html}) with no-cache headers. \\
GET & /api/company & Company KPIs + one card per division. \\
GET & /api/division, /api/divisions/<div\_key> & Division dashboard: departments, KPIs, inactive departments. \\
GET & /api/departments/<dept\_key> & Department dashboard: machines, KPIs, inactive machines. \\
GET & /api/departments/<dk>/machines/<eq\_id> & Machine detail (dashboard tab data). \\
GET & /api/workorders & All departments' WOs, compact, newest first. \\
GET & /api/workorder/<wo\_id> & Full work-order detail. \\
GET & /api/weekly, /api/monthly & Weekly (Sun--Sun) and monthly schedule views. \\
GET & /api/completed-counts, /api/team-stats & Completed-WO counts and per-technician stats. \\
GET & /api/inactive & All soft-deleted departments/machines. \\
\bottomrule
\end{longtable}

\subsubsection{Work-Order Lifecycle}
\begin{longtable}{@{}p{0.13\textwidth}>{\ttfamily\small}p{0.47\textwidth}p{0.33\textwidth}@{}}
\toprule
\textbf{Method} & \textbf{Path} & \textbf{Description} \\
\midrule
\endhead
POST & /api/workorders & Create a WO (scheduled or unscheduled); optional initial solution; fires the new-unscheduled e-mail. \\
PATCH/PUT & /api/workorder/<wo\_id> & Field-level edit of any WO (stored as override); fires the ``Closed \& Completed'' e-mail on status transition. \\
DELETE & /api/workorder/<wo\_id> & Delete a \emph{manual} WO (admin-gated; scraped WOs cannot be deleted). \\
POST & /api/workorder/<wo\_id>/stop-recurrence & Stop a recurring series (admin-gated). \\
POST & /api/workorder/<wo\_id>/solutions & Append a solution-log entry. \\
GET/POST & /api/workorder/<wo\_id>/attachments & List / upload attachments. \\
GET & /api/attachments/<att\_id> & Download an attachment. \\
GET & /api/workorder/<wo\_id>/audit & WO change history (admin-gated). \\
POST & /api/workorder/<wo\_id>/assign & Assign to a technician (Sean-gated). \\
\bottomrule
\end{longtable}

\subsubsection{Structure Management (Departments / Machines / Technicians)}
\begin{longtable}{@{}p{0.13\textwidth}>{\ttfamily\small}p{0.47\textwidth}p{0.33\textwidth}@{}}
\toprule
\textbf{Method} & \textbf{Path} & \textbf{Description} \\
\midrule
\endhead
POST & /api/departments & Create a department. \\
POST & /api/machines & Create a machine. \\
POST & /api/departments/<dk>/deactivate | restore & Soft-delete / restore a department. \\
POST & /api/machines/deactivate | restore & Soft-delete / restore a machine (body: dept\_key, eq\_id, name, author). \\
GET/POST & /api/technicians & List / add technicians. \\
DELETE & /api/technicians/<name> & Remove a technician. \\
\bottomrule
\end{longtable}

\subsubsection{Machine Information and Vendor Contacts}
\begin{longtable}{@{}p{0.15\textwidth}>{\ttfamily\small}p{0.47\textwidth}p{0.31\textwidth}@{}}
\toprule
\textbf{Method} & \textbf{Path} & \textbf{Description} \\
\midrule
\endhead
GET, PATCH/POST & /api/departments/<dk>/machines/<eq>/info & Read / update the editable Machine Info record. \\
POST & /api/departments/<dk>/machines/<eq>/parse-notes & LLM sorts free-form PM notes into structured contacts / logins / purchase info. \\
GET & /api/vendor-contacts & Combined vendor list: DB contacts + machine-card contacts. \\
POST, PATCH, DELETE & /api/vendor-contacts[/<id>] & Create / edit / delete vendor contacts (edit-gated). \\
POST, DELETE & /api/vendor-contact-types[/<name>] & Manage contact types (edit-gated; delete blocked while in use). \\
PATCH/DELETE & /api/departments/<dk>/machines/<eq>/contacts/<idx> & Edit a contact that lives on a machine card. \\
\bottomrule
\end{longtable}

\subsubsection{Analytics and AI}
\begin{longtable}{@{}p{0.10\textwidth}>{\ttfamily\small}p{0.52\textwidth}p{0.31\textwidth}@{}}
\toprule
\textbf{Method} & \textbf{Path} & \textbf{Description} \\
\midrule
\endhead
GET & /api/departments/<dk>/machines/<eq>/trends & Per-machine monthly KPI trends. \\
GET & /api/departments/<dk>/trends & Department monthly trends (optional ?group=). \\
GET & /api/departments/<dk>/machines-trends & Per-machine yearly totals (Pareto chart). \\
GET & /api/departments/<dk>[/machines/<eq>]/month-synopsis & LLM synopsis of what drove a month's stats. \\
GET/POST/PUT & /api/departments/<dk>/machines/<eq>/guide & Read / generate / save-edit the checklist (edit-gated for writes). \\
POST & \ldots/guide/update & AI update with new work orders. \\
GET & \ldots/guide/versions[/<n>] & Version history / a specific version. \\
POST & \ldots/guide/versions/<n>/restore & Restore an old checklist version. \\
GET/POST/DELETE & /api/calendar-events[/<id>] & Calendar entries. \\
GET/POST/DELETE & /api/chart-events[/<id>] & Global chart timeline markers (edit-gated). \\
\bottomrule
\end{longtable}

\subsubsection{System and E-mail}
\begin{longtable}{@{}p{0.13\textwidth}>{\ttfamily\small}p{0.47\textwidth}p{0.33\textwidth}@{}}
\toprule
\textbf{Method} & \textbf{Path} & \textbf{Description} \\
\midrule
\endhead
POST & /api/reload & Force \code{reload\_data()}. \\
GET & /api/edit-auth, /api/admin-auth, /api/sean-auth & Report whether each gate is password-protected. \\
POST & /api/verify-\{edit|admin|sean\}-password & Verify a supplied password. \\
GET & /api/email/status & E-mail configuration status. \\
GET/POST & /api/email/recipients & Read / replace the recipient list. \\
POST & /api/email/test & Send a sample e-mail to one address. \\
POST & /api/email/weekly-digest & Manually trigger the weekly digest. \\
\bottomrule
\end{longtable}

% ===========================================================================
\section{Intelligence Layer (LLM Integration)}
% ===========================================================================

\subsection{Providers and Transport (\code{analyze\_equipment.py})}
Two providers are supported, selected by environment:
\begin{itemize}[nosep]
    \item \textbf{Ollama Cloud} (default when \code{OLLAMA\_API\_KEY} is set);
          model pinned by \code{server.py} to \code{gpt-oss:120b} unless
          overridden with \code{OLLAMA\_MODEL}.
    \item \textbf{Google Gemini} (\code{GEMINI\_API\_KEY};
          \code{GEMINI\_MODEL}, default \code{gemini-2.5-flash}).
\end{itemize}
Because the corporate network blocks Python/OpenSSL TLS, HTTPS calls are made
through a \emph{Windows-native PowerShell transport} (Windows TLS stack)
instead of the standard Python HTTP stack. The module also computes local
statistics (material cost, labor time, downtime hours) and builds prompts, and
can be used standalone:
\code{python analyze\_equipment.py --equipment-id 1877}.

\subsection{Troubleshooting-Checklist Engine (\code{guide\_engine.py})}
\label{sec:guides}
Single source of truth for creating, editing, archiving, and regenerating the
per-machine checklists, used by both the Flask server (interactive) and the
nightly job (bulk). File layout under \code{guides/}:

\begin{longtable}{@{}>{\ttfamily}p{0.36\textwidth}p{0.59\textwidth}@{}}
\toprule
\textbf{File} & \textbf{Meaning} \\
\midrule
<eq>.md & The live checklist (all edits baked in). \\
<eq>.<stamp>.bak.md & Timestamped backup created before any overwrite. \\
baselines/<eq>.md & Last AI-generated version (pre human edits). \\
edits/<eq>.json & Append-only manual-edit log (diffs). \\
archive/<stamp>/<eq>.md & Full nightly snapshot before each rescrape. \\
versions.db & Immutable full-content version history. \\
\bottomrule
\end{longtable}

\textbf{Manual-edit preservation.} When an operator saves a hand-edited
checklist, the previous file is backed up, the old/new Markdown is diffed, and
the added/removed lines are appended to the edit log. That log is rendered
into an \emph{``OPERATOR EDITS THAT MUST BE PRESERVED''} block injected into
every future prompt (interactive regenerate and nightly rebuild alike).
Entries are applied oldest-first with ``later wins,'' so edits-of-edits are
handled automatically. Per-entry rendering is capped
(\code{\_MAX\_LINES\_PER\_EDIT} = 40 lines) to bound prompt size.

% ===========================================================================
\section{E-mail Notifications (\code{mint\_email.py})}
% ===========================================================================

Three flows, each rendered as HTML with a plain-text fallback (colored header
banner, label/value rows, and a ``Link to Item'' button back into MINT):
\begin{enumerate}[nosep]
    \item \textbf{New unscheduled WO} --- sent immediately on creation.
    \item \textbf{Closed \& Completed} --- sent when any WO transitions into a
          completed status (transition-only; re-saving does not re-notify).
    \item \textbf{Weekly digest} --- scheduled work due this coming week plus
          any past-due scheduled work still pending. Sent by a daemon thread
          (default Monday 07:00; \code{WEEKLY\_DIGEST\_DAY/HOUR/ENABLED}),
          with a small state file preventing double sends across restarts.
\end{enumerate}

Transport is plain SMTP, so a self-serve Gmail App Password works (no
OAuth/admin consent), as does Office~365 or any SMTP server. The module is
\textbf{intentionally inert} until: credentials are configured, \emph{and}
\code{mint\_data/email\_recipients.json} contains at least one address. When
credentials are present, sending defaults to \emph{on} but can be forced into
dry-run with \code{EMAIL\_ENABLED=0}. Gmail shortcuts
\code{GMAIL\_ADDRESS}/\code{GMAIL\_APP\_PASSWORD} are accepted in place of the
generic \code{SMTP\_*} variables.

% ===========================================================================
\section{Presentation Layer (Frontend)}
% ===========================================================================

The entire UI is a single self-contained file, \code{webapp/index.html}
($\sim$330\,KB), served by Flask with no-cache headers so refreshes always
pick up changes. Technology choices:

\begin{itemize}[nosep]
    \item \textbf{React} with \textbf{htm} tagged templates and \textbf{Babel
          standalone} for in-browser JSX-free compilation --- no build step.
    \item \textbf{Tailwind CSS} (vendored runtime build) for styling.
    \item \textbf{marked} to render the Markdown checklists; \textbf{jsPDF}
          for client-side PDF export.
    \item All libraries vendored in \code{webapp/vendor/}: works fully
          offline on the LAN.
\end{itemize}

Client-side routing uses a \code{route.view} state with views
\code{company} $\rightarrow$ \code{division} $\rightarrow$
\code{department} $\rightarrow$ \code{machine}, plus an \code{inactive} view
listing soft-deleted items with restore actions. The machine page has four
tabs: Dashboard, AI Troubleshooting Checklist, Unscheduled WOs, and Scheduled
WOs, plus Machine Info and trends/Pareto charts at machine and department
level. Deactivation uses a reusable confirm-with-name modal; the user's name
is remembered in \code{localStorage} and attached to every mutation.

% ===========================================================================
\section{Configuration Reference}
\label{sec:config}
% ===========================================================================

All configuration is via environment variables, typically in a git-ignored
\code{.env} file loaded with \code{python-dotenv}.

\begin{longtable}{@{}>{\ttfamily}p{0.30\textwidth}>{\ttfamily}p{0.14\textwidth}p{0.48\textwidth}@{}}
\toprule
\textbf{Variable} & \textbf{Default} & \textbf{Meaning} \\
\midrule
\endhead
SERVER\_HOST / SERVER\_PORT & 0.0.0.0 / 5000 & Bind address and port. \\
EDIT\_PASSWORD & (blank) & Checklist/vendor/chart-event edit gate; blank = open. \\
ADMIN\_PASSWORD & (blank) & Delete-WO / history / stop-recurrence gate; blank = open. \\
SEAN\_PASSWORD & sean & Assignment + team-dashboard gate (always protected). \\
OLLAMA\_API\_KEY / OLLAMA\_MODEL & -- / gpt-oss:120b & Ollama Cloud checklist provider. \\
GEMINI\_API\_KEY / GEMINI\_MODEL & -- / gemini-2.5-flash & Gemini provider (fallback). \\
CHROME\_AUTOSTART & 1 & 0 = do not auto-launch debug Chrome. \\
CHROME\_DEBUG\_PORT & 0 & 0 = auto-pick and capture; or pin a port. \\
CHROME\_PATH / CHROME\_PROFILE & auto & Non-standard Chrome binary / profile path. \\
NIGHTLY\_DEPT\_TIMEOUT & 14400 & Per-department scrape timeout (seconds). \\
EMAIL\_ENABLED & auto & 1/0; defaults on when SMTP credentials exist. \\
SMTP\_HOST / SMTP\_PORT & -- / 587 & Mail server. \\
SMTP\_USER / SMTP\_PASSWORD / SMTP\_FROM & -- & Credentials; FROM defaults to USER. \\
SMTP\_STARTTLS & 1 & 0 disables STARTTLS. \\
GMAIL\_ADDRESS / GMAIL\_APP\_PASSWORD & -- & Gmail shortcuts for the SMTP\_* vars. \\
MINT\_BASE\_URL & http://localhost:5000 & Base URL used in e-mail ``Link to Item'' buttons. \\
WEEKLY\_DIGEST\_ENABLED / DAY / HOUR & 1 / 0 / 7 & Weekly digest schedule (0 = Monday). \\
\bottomrule
\end{longtable}

% ===========================================================================
\section{Setup and Deployment}
% ===========================================================================

\subsection{Prerequisites}
\begin{itemize}[nosep]
    \item Windows VM (the LLM transport and Chrome management are
          Windows-specific), Python 3.10+ (uses \code{list[dict]} syntax),
          Google Chrome installed.
    \item Python dependencies (\code{requirements.txt}): \code{selenium},
          \code{webdriver-manager}, \code{beautifulsoup4}, \code{lxml},
          \code{google-genai}, \code{python-dotenv}, \code{flask},
          \code{flask-cors}, \code{APScheduler}.
\end{itemize}

\subsection{One-Time Setup}
\begin{lstlisting}
# From the project folder
pip install -r requirements.txt

# Create .env (copy from .env.example) with real keys:
#   GEMINI_API_KEY=...
#   OLLAMA_API_KEY=...
#   EDIT_PASSWORD=... ADMIN_PASSWORD=... (optional)
\end{lstlisting}
\code{.env} is git-ignored and must never be committed. (Keys once pushed to
git history must be rotated.)

\subsection{Chrome Login Session (required for scraping)}
\begin{lstlisting}
python server.py          # auto-launches debug Chrome, captures the port
# First time: log into the PM site in that Chrome window; leave it open.

python chrome_session.py            # manual launch + port
python chrome_session.py --status   # running port + login state
\end{lstlisting}
If the session expires, scrapes are skipped (never overwriting good data) and
the failure is reported.

\subsection{Running the Server}
\begin{lstlisting}
python server.py
#   Local:   http://127.0.0.1:5000
#   Network: http://<vm-ip>:5000   (LAN)
\end{lstlisting}
For 24/7 operation, register a Windows Task Scheduler task that runs
\code{python server.py} at startup/logon with restart-on-failure --- the
single process is both the web server and the scheduler.

For local development/testing without Chrome or the nightly machinery:
\begin{lstlisting}
$env:CHROME_AUTOSTART="0"; $env:NIGHTLY_ENABLED="0"; python server.py
\end{lstlisting}

\subsection{LAN Access}
The server binds \code{0.0.0.0}; open the firewall once (Admin PowerShell):
\begin{lstlisting}
New-NetFirewallRule -DisplayName "PM Dashboard" -Direction Inbound `
    -Action Allow -Protocol TCP -LocalPort 5000
\end{lstlisting}

\subsection{Manual Data Operations}
\begin{lstlisting}
python scraper.py --limit 3      # quick scrape smoke test
python scraper.py --from-html    # offline replay from ./pages/
python nightly_update.py         # full rescrape + regeneration cycle
python analyze_equipment.py --equipment-id 1877   # ad-hoc LLM analysis
curl -X POST http://localhost:5000/api/reload      # hot-reload data
\end{lstlisting}

\subsection{Operational Gotchas}
\begin{itemize}[nosep]
    \item \textbf{Stale server process:} a previously started
          \code{server.py} can hold port 5000 (\code{SO\_REUSEADDR}) so a new
          instance silently fails to bind while the old code keeps answering.
          Find and stop the old \code{python.exe} running \code{server.py}
          before restarting.
    \item \textbf{Backups:} \code{backup\_pre\_merge\_<stamp>/} folders hold
          pre-merge snapshots; \code{guides/archive/} holds nightly checklist
          snapshots.
    \item \textbf{Attachments and DBs} live under \code{mint\_data/} and
          \code{guides/} --- include both in any backup strategy.
\end{itemize}

% ===========================================================================
\section{Security Considerations}
% ===========================================================================

\begin{itemize}[nosep]
    \item The application is designed for a \textbf{trusted LAN}: reads are
          unauthenticated; writes are gated by shared passwords transmitted in
          headers over plain HTTP. It must not be exposed to the internet
          without a reverse proxy adding TLS and real authentication.
    \item Scraping reuses an interactive Chrome login --- no PM credentials
          are stored anywhere in the codebase.
    \item Secrets live only in the git-ignored \code{.env}. API keys that were
          historically committed must be treated as leaked and rotated.
    \item Every mutation is attributable via the mandatory author name and the
          \code{audit\_log} table; scraped records are immutable (edits are
          overlay patches) and manual WOs are the only deletable records.
\end{itemize}

% ===========================================================================
\section{Known Limitations and Future Work}
% ===========================================================================

\begin{itemize}[nosep]
    \item Single-process, single-VM deployment; SQLite + in-memory caches
          limit horizontal scaling (acceptable for a facility-level tool).
    \item The Windows-PowerShell TLS transport ties LLM calls to Windows.
    \item Author identity is honor-system (a typed name), not authenticated
          accounts.
    \item Only the BLA division is populated; the company layer is designed
          so additional divisions (e.g.\ BED) can be added later.
    \item Frontend is one large HTML file; a build pipeline (Vite + proper
          module splitting) would improve maintainability.
    \item E-mail digest scheduling uses a polling daemon thread; migrating to
          APScheduler jobs (already a dependency) would be cleaner.
\end{itemize}

\end{document}
