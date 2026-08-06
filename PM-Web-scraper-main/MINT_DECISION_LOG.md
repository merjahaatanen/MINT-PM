\documentclass[11pt,a4paper]{article}

% ---------------------------------------------------------------------------
% MINT -- Design Decision Log
% Compiled from Merja's weekly update reports (Weeks 2-8, June-July).
% Copy this whole file into Overleaf as main.tex and compile with pdfLaTeX.
% ---------------------------------------------------------------------------

\usepackage[margin=2.5cm]{geometry}
\usepackage[T1]{fontenc}
\usepackage[utf8]{inputenc}
\usepackage{lmodern}
\usepackage{booktabs}
\usepackage{longtable}
\usepackage{enumitem}
\usepackage{xcolor}
\usepackage{hyperref}
\usepackage{parskip}

\hypersetup{
    colorlinks=true,
    linkcolor=blue!60!black,
    urlcolor=blue!60!black,
    pdftitle={MINT -- Design Decision Log},
    pdfauthor={Merja Haatanen}
}

\newcommand{\code}[1]{\texttt{#1}}
\newcommand{\decision}[1]{\textbf{Decision:} #1}

\title{\textbf{MINT}\\[4pt]
       \Large Design Decision Log\\[4pt]
       \normalsize From the PM Web Scraper to the BLA Maintenance Dashboard}
\author{Merja Haatanen --- Bobrick Washroom Equipment}
\date{\today}

\begin{document}

\maketitle
\tableofcontents
\newpage

% ===========================================================================
\section{Introduction}
% ===========================================================================

This document records the design and engineering decisions made during the
development of \textbf{MINT}, the BLA maintenance dashboard. The project began
as a task to ``collect data from PM for troubleshooting machines'' and evolved
over roughly eight weeks into a full maintenance-management platform: a web
scraper for the legacy PM system, an AI-assisted troubleshooting-checklist
generator, and a read/write dashboard covering work orders, trends, contacts,
machine information, calendars, and notifications.

Decisions are presented chronologically (as they were made week by week),
followed by a consolidated summary table. Each entry notes the context or
problem, the decision taken, and --- where relevant --- the alternatives that
were rejected.

% ===========================================================================
\section{Chronological Decision Record}
% ===========================================================================

% ---------------------------------------------------------------------------
\subsection{Weeks 2--3: From Manual Logging to an Automated Scraper}
% ---------------------------------------------------------------------------

\subsubsection{Manual data compilation is abandoned in favor of automation}
The task began as manually compiling PM work-order data into an Excel sheet
(the Rainbow machine alone yielded about ten issues before the approach
was reconsidered). Manual logging clearly would not scale to the whole
facility.

\decision{Automate the extraction using AI coding tools (initially Cursor,
then Devin). The manual Excel workflow was dropped entirely.}

\subsubsection{Scraping the website instead of accessing the database}
Direct access to the PM system's database was requested but could not be
obtained in the form required.

\decision{Build a \emph{web scraper} instead: upload the HTML from the PM
website and have the tooling generate a scraper plus a Python UI. The scraped
HTML pages are kept locally so extraction can be replayed offline.}

\subsubsection{AI-generated troubleshooting checklists}
Once the work-order history could be extracted, the question became how to
turn it into something operators could use at the machine.

\decision{Integrate an LLM (initially Google Gemini via an API key) to
generate a per-machine \emph{troubleshooting checklist} from that machine's
work-order history. Checklists are explicitly treated as drafts: ``checklists
aren't perfect and need to be edited and then looked at by leads and
maintenance.'' An example checklist was provided to steer the output format.}

\subsubsection{Serving the tool from a server rather than a local script}
\decision{Host the program on a server so that others can access it easily,
with a UI to browse all checklists per machine and edit them in place.}

\subsubsection{Early stakeholder-driven features}
Presentations to Eric and Sean in Week 3 produced immediate course
corrections.

\decision{Based on Sean's feedback: fix attachments so they can be opened
from the UI, extend the scraper to also capture \emph{scheduled} (preventive
maintenance) work orders --- not just unscheduled breakdowns --- and plan a
dashboard-centric UI.}

% ---------------------------------------------------------------------------
\subsection{Week 4: Full-Facility Scope and the Dashboard Hierarchy}
% ---------------------------------------------------------------------------

\subsubsection{Rebuilding the scraper}
Debugging of the first full scrape revealed enough problems that patching was
not worthwhile (``ruined the web scraper, started over'').

\decision{Rewrite the scraper from scratch and rescrape everything from all
departments.}

\subsubsection{Dashboard drill-down structure}
\decision{Structure the UI as a drill-down of dashboards: \emph{entire
division} $\rightarrow$ \emph{per department} $\rightarrow$ \emph{per
machine}, with a quick statistics view for each machine. Machines are
explicitly sorted apart from non-machine equipment/tools in TPF. Soap \&
Assembly is included alongside Toilet Partitions.}

\subsubsection{All nine departments included}
\decision{Extend coverage from the initial two departments to all nine
(adding assembly, general, maintenance, machine shop, shipping, quality
assurance, and mfg engineering). Two defects this exposed were fixed: the
scraper skipped departments with no unscheduled work orders, and the UI
silently omitted some stored TPF and S\&A data.}

\subsubsection{Nightly scraping instead of live database updates}
A meeting on intermittent updates concluded that the required kind of PM
database access simply was not obtainable.

\decision{Perform a \emph{full rescrape every night} instead of live/
incremental updates, and run the system on a company virtual machine so the
nightly job is always on. (The VM being down at the time delayed, but did not
change, this decision.)}

% ---------------------------------------------------------------------------
\subsection{Week 5: Deployment, Archival, and Weekly Views}
% ---------------------------------------------------------------------------

\subsubsection{VM deployment and shareability}
\decision{Move the server onto the virtual machine and make it shareable on
the network, so the tool is no longer tied to a personal computer.}

\subsubsection{Nightly checklist archives}
Since checklists are regenerated and hand-edited, edits must never be lost.

\decision{Archive every checklist every night before the rescrape, so edits
can always be recovered.}

\subsubsection{Weekly dashboards}
\decision{Add weekly schedule dashboards (last week / this week / next week)
so upcoming and recent scheduled maintenance is visible at a glance.}

\subsubsection{Headless scraping}
Data discrepancies appeared, traced to the scraping method and the overnight
update.

\decision{Switch to \emph{headless} scraping (no visible browser UI; HTML is
grabbed directly), which was refined the following week.}

% ---------------------------------------------------------------------------
\subsection{Week 6: Robustness, Access Control, and a Name}
% ---------------------------------------------------------------------------

\subsubsection{Root cause of scrape failures and rollback protection}
The discrepancies were diagnosed: too many scraper instances ran at once, and
a single missing element in one department's work order failed the whole
department.

\decision{Finish the headless-scraping setup and add \emph{rollback}: if a
scrape fails for any reason, the data automatically reverts to the previous
good version, so a failed run can never corrupt or wipe good data.}

\subsubsection{Password-protected checklist editing}
\decision{Require a shared password before anyone can edit a checklist, and
rework checklist editing to be more user-friendly and readable.}

\subsubsection{Weekly dashboard filters}
\decision{Add filters (unscheduled / scheduled / all) to the weekly
dashboards.}

\subsubsection{Spare parts: build over buy (Smartsheet)}
A meeting with Gabriel and James compared the existing spare-parts Smartsheet
against a mocked-up in-house tool.

\decision{Switch from Smartsheet to a spare-parts tool built into the same
platform (James added the spare-parts tab and a tutorial).}

\subsubsection{Human review workflow for checklists}
\decision{Track checklist review in a spreadsheet recording which checklists
have been edited and by whom, and roll editing out machine-by-machine with
manufacturing and maintenance, starting from a compiled list of TPF machines
in MINT order.}

\subsubsection{Naming}
\decision{The tool needed a real name; \textbf{MINT} was adopted (the weekly
reports switch from ``PM tool'' to ``MINT'' at this point).}

% ---------------------------------------------------------------------------
\subsection{Week 7: Trends, Versioning, Write Capability, and Notifications}
% ---------------------------------------------------------------------------

\subsubsection{Trends with AI summaries}
Feedback from Kieun suggested a visual aid for trends; a game plan was agreed
with Eric.

\decision{Add a \emph{Trends} tab (first for the Holzma, then all
``important'' TPF machines, then every TPF machine plus an overall TPF view)
plotting month-to-month statistics per year, with an AI-generated synopsis
for each month.}

\subsubsection{SQLite version control for checklists}
The question of how often edits should be saved was resolved
architecturally.

\decision{Build a version-control system (with James) in which \emph{every}
change to a checklist is saved as an immutable version in SQLite, viewable
directly in MINT. Checklist regeneration was refined to \emph{add to the
latest version} rather than start over.}

\subsubsection{The Company layer}
\decision{Create a top-level \emph{Company} page so other divisions beyond
BLA can be added later without restructuring.}

\subsubsection{MINT becomes read/write: creating work orders}
\decision{Allow users to create new scheduled and unscheduled work orders in
MINT itself (developed on a ``copy'' of MINT used as a mock-up before
merging). Key workflow rules:
\begin{itemize}[nosep]
    \item New unscheduled work orders are automatically \emph{Pending}.
    \item Labor time, material cost, and downtime become \emph{required}
          fields only once a work order is marked Closed \& Complete.
    \item Scheduled work orders get a \emph{rate of occurrence}: the next
          instance auto-populates on the cadence, with the ability to delete
          a single occurrence or stop the series entirely.
\end{itemize}}

\subsubsection{Tiered password protection}
\decision{Introduce password protection incrementally, starting with
checklist editing (Week 6) and extending to version history and other
sensitive actions.}

\subsubsection{PDF export}
\decision{Any work order can be exported to PDF (weekly schedules and monthly
calendars followed in Week 8).}

\subsubsection{E-mail notifications via SMTP app password}
Notifications were wanted for created/completed work orders and a weekly
look-ahead.

\decision{Implement e-mail notifications (with James) for: any work order
created, any work order closed, and a weekly e-mail of upcoming and past-due
scheduled work orders. Transport uses a Gmail account with an \emph{app
password} (no OAuth or admin consent needed), explicitly as an interim
solution --- the plan of record is to obtain an Outlook address and app
password from IT later.}

\subsubsection{Soft deletion (the ``inactive folder'')}
Deleting a machine deletes all of its statistics with it, which is too
dangerous to make irreversible.

\decision{Nothing is hard-deleted. Deleted items go into an \emph{inactive
folder} from which they can be restored at any time.}

\subsubsection{Equipment categorization}
\decision{Organize S\&A equipment into categories on MINT (started with
Michael).}

% ---------------------------------------------------------------------------
\subsection{Week 8: Branding, Usability, Contacts, and Machine Info}
% ---------------------------------------------------------------------------

\subsubsection{Branding and visual identity}
\decision{Create a MINT logo and change the site's color scheme; improve
checklist rendering (bolding, bullet points) for readability.}

\subsubsection{Filtering and search}
\decision{Add ``actualized'' work-order filters to the Trends tab and search
filters for browsing work orders.}

\subsubsection{Structured user feedback intake}
\decision{Work through the ``PM 2.0'' Excel sheet of user suggestions
item-by-item to decide what to implement, rather than reacting ad hoc.}

\subsubsection{Calendar with controlled vocabulary}
\decision{Add a calendar view with monthly export. Calendar events use a
\emph{dropdown-only} event type (tech visit, safety event, or vendor visit)
--- no free-text categories --- to keep the data consistent.}

\subsubsection{Production-stop color coding}
\decision{Highlight a machine in red when it has a pending work order of
urgency~1, giving an immediate visual production-stop indicator.}

\subsubsection{Machine Info tab and the longevity spreadsheet}
The machine-longevity Excel sheet was reorganized for TPF and cross-checked
with Sean (some machines existed in the sheet but not in PM).

\decision{Integrate the machine-longevity data plus machine information
scraped from PM into a new \emph{Machine Info} tab. Free-form PM notes are
sorted \emph{using AI} into user-friendly card layouts (contacts, purchase
info, settings, etc.), and editing of this tab is locked. (A code-based,
non-AI sorting attempt was tried and rejected after inconsistent phone-number
formats crashed the server.)}

\subsubsection{Centralized, editable contact list}
\decision{Merge Sean's contact list with contacts scraped from PM (including
scraped ``vendors'') into one editable contact list on the division page,
with per-machine contact lists where applicable. Contacts are filterable by
type, and new types can be added --- each new type automatically gains its own
filter.}

\subsubsection{Pareto charts and chart event markers}
\decision{Add Pareto charts everywhere trend charts already exist (TPF
department and per-machine), and allow users to add a \emph{vertical line} to
trend charts marking major events (e.g.\ a rebuild or vendor visit) so
changes in the data can be explained visually.}

\subsubsection{User profiles and work-order attribution}
\decision{Begin attaching names to work orders: each maintenance person and
Sean (supervisor) gets a user profile, laying the groundwork for assignment
and per-technician statistics.}

% ===========================================================================
\section{Consolidated Decision Summary}
% ===========================================================================

\begin{longtable}{@{}p{0.06\textwidth}p{0.32\textwidth}p{0.55\textwidth}@{}}
\toprule
\textbf{Week} & \textbf{Area} & \textbf{Decision} \\
\midrule
\endhead
2--3 & Data acquisition & Abandon manual Excel logging; automate with AI-built web scraper. \\
2--3 & Data acquisition & Scrape the PM website (no database access obtainable); keep local HTML copies. \\
2--3 & AI & Generate per-machine troubleshooting checklists with an LLM (Gemini first); human review required. \\
2--3 & Architecture & Serve via a shared server with a browsable/editable checklist UI. \\
3 & Scope & Capture scheduled (PM) work orders too; make attachments accessible; plan dashboards. \\
4 & Scraper & Rewrite the scraper from scratch after debugging failures. \\
4 & UI & Division $\rightarrow$ Department $\rightarrow$ Machine dashboard drill-down; machines separated from tools. \\
4 & Scope & Cover all nine departments. \\
4 & Updates & Nightly full rescrape (no live DB access possible); host on a VM. \\
5 & Operations & Deploy to the VM; make the server shareable on the LAN. \\
5 & Data safety & Archive all checklists nightly before rescraping. \\
5 & UI & Weekly (last/this/next week) schedule dashboards. \\
5--6 & Scraper & Switch to headless scraping; sequential rather than many parallel instances. \\
6 & Data safety & Rollback: failed scrapes revert to the previous good version. \\
6 & Security & Password gate on checklist editing. \\
6 & Build vs.\ buy & Replace the spare-parts Smartsheet with an in-house tab in the platform. \\
6 & Process & Checklist review tracked per machine and per editor; rollout with mfg + maintenance. \\
6 & Identity & Name the tool \textbf{MINT}. \\
7 & Analytics & Trends tab with monthly plots and AI month synopses; later extended to all TPF machines. \\
7 & Data safety & SQLite version control: every checklist change is an immutable, viewable version. \\
7 & Architecture & Company layer added so future divisions can be onboarded. \\
7 & Write path & Users create scheduled/unscheduled WOs in MINT; required fields enforced at completion; recurring PMs auto-populate with stop/delete controls. \\
7 & Notifications & SMTP e-mail via Gmail app password (interim; Outlook + IT app password planned); created/closed/weekly-digest flows. \\
7 & Data safety & Soft deletion: inactive folder with restore instead of hard delete. \\
7 & Security & Password protection extended (version history etc.). \\
7--8 & Export & PDF export for work orders, weekly schedules, and monthly calendars. \\
8 & UX & Logo, color scheme, readable checklist formatting, WO search filters. \\
8 & Process & Systematic triage of the ``PM 2.0'' user-suggestion sheet. \\
8 & UI & Calendar with controlled event types (tech visit / safety event / vendor visit). \\
8 & UI & Urgency-1 pending WOs highlight the machine in red (prod-stop indicator). \\
8 & Data & Machine Info tab merging longevity spreadsheet + scraped PM info; AI-based note sorting (code-based sorting rejected); editing locked. \\
8 & Data & Unified editable contact list (Sean's list + scraped PM/vendor contacts) with extensible type filters. \\
8 & Analytics & Pareto charts alongside trends; user-added vertical event markers on charts. \\
8 & Accountability & User profiles for maintenance staff and Sean; names attached to work orders. \\
\bottomrule
\end{longtable}

% ===========================================================================
\section{Open Items Carried Forward}
% ===========================================================================

At the close of the reporting period, the following MINT-related items were
explicitly noted as future work:

\begin{itemize}[nosep]
    \item Replace the interim Gmail app-password transport with an Outlook
          address and app password provisioned by IT.
    \item Continue refining user profiles and work-order name attribution
          (``still lots of refining to do'').
    \item Investigate the server crash caused by contact phone-number
          formats during code-based note sorting (pending James's return).
    \item Reconcile remaining discrepancies between the machine-longevity
          spreadsheet and PM (machines present in one but not the other).
    \item Continue working through the PM~2.0 suggestion sheet.
\end{itemize}

\end{document}
