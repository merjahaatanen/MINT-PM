"""
Unscheduled Work Order Viewer
=============================
A simple Tkinter interface to browse the unscheduled work orders scraped by
scraper.py (work_orders_unscheduled.json / .csv).

Features
--------
- Work Orders tab: searchable / sortable table of every work order.
  Click a row to see the full detail (problem, comments, attachments, etc.)
- Repeated Problems tab: shows each distinct problem and how many times it
  occurred, sorted by frequency (most common first).
- Department filter so you can focus on, e.g., "Toilet Partitions".

Run:
    python work_order_viewer.py
"""

import csv
import json
import os
import re
import tkinter as tk
from collections import Counter
from tkinter import ttk, messagebox, filedialog

OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))
JSON_PATH  = os.path.join(OUTPUT_DIR, "work_orders_unscheduled.json")
CSV_PATH   = os.path.join(OUTPUT_DIR, "work_orders_unscheduled.csv")

# Columns shown in the main grid: (key, heading, width)
GRID_COLUMNS = [
    ("equipment_eq_id",    "EQ ID",        80),
    ("department",         "Department",   120),
    ("equipment_name",     "Equipment",    220),
    ("wo_id",              "WO #",          60),
    ("date_notified",      "Notified",      90),
    ("urgency",            "Urgency",       80),
    ("status",             "Status",       150),
    ("problem",            "Problem",      300),
    ("work_performed_by",  "Performed By", 130),
    ("labor_time",         "Labor",         60),
    ("material_cost",      "Material $",    80),
    ("downtime_hours",     "Downtime",      80),
    ("completed_datetime", "Completed",    110),
]


def _norm_problem(text: str) -> str:
    """Normalize a problem string for grouping (lowercase, collapse whitespace)."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


class WorkOrderViewer(tk.Tk):

    def __init__(self):
        super().__init__()
        self.title("Unscheduled Work Order Viewer")
        self.geometry("1300x720")

        self.all_records = []      # list[dict]
        self.filtered    = []      # list[dict] currently displayed
        self.sort_state  = {}      # column -> ascending bool

        self._build_ui()
        self._load_data()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        # ---- top control bar ----
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")

        ttk.Label(bar, text="Department:").pack(side="left")
        self.dept_var = tk.StringVar(value="All")
        self.dept_combo = ttk.Combobox(bar, textvariable=self.dept_var,
                                       state="readonly", width=22)
        self.dept_combo.pack(side="left", padx=(4, 16))
        self.dept_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filters())

        ttk.Label(bar, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        search_entry = ttk.Entry(bar, textvariable=self.search_var, width=30)
        search_entry.pack(side="left", padx=(4, 16))
        self.search_var.trace_add("write", lambda *_: self._apply_filters())

        ttk.Button(bar, text="Reload", command=self._load_data).pack(side="left")
        ttk.Button(bar, text="Open file...", command=self._open_file).pack(side="left", padx=6)

        self.count_label = ttk.Label(bar, text="")
        self.count_label.pack(side="right")

        # ---- notebook with two tabs ----
        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        self._build_workorders_tab()
        self._build_problems_tab()

    def _build_workorders_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Work Orders")

        # split: table on top, detail panel below
        paned = ttk.PanedWindow(frame, orient="vertical")
        paned.pack(fill="both", expand=True)

        table_frame = ttk.Frame(paned)
        paned.add(table_frame, weight=3)

        cols = [c[0] for c in GRID_COLUMNS]
        self.tree = ttk.Treeview(table_frame, columns=cols, show="headings",
                                 selectmode="browse")
        for key, heading, width in GRID_COLUMNS:
            self.tree.heading(key, text=heading,
                              command=lambda k=key: self._sort_by(k))
            anchor = "center" if key in ("wo_id", "labor_time", "material_cost",
                                         "downtime_hours") else "w"
            self.tree.column(key, width=width, anchor=anchor, stretch=False)

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # detail panel
        detail_frame = ttk.LabelFrame(paned, text="Work Order Detail", padding=6)
        paned.add(detail_frame, weight=1)
        self.detail = tk.Text(detail_frame, wrap="word", height=10,
                              font=("Consolas", 10))
        dsb = ttk.Scrollbar(detail_frame, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=dsb.set, state="disabled")
        self.detail.pack(side="left", fill="both", expand=True)
        dsb.pack(side="right", fill="y")

    def _build_problems_tab(self):
        frame = ttk.Frame(self.nb)
        self.nb.add(frame, text="Repeated Problems")

        info = ttk.Label(frame, padding=(0, 6),
                         text="Distinct problems and how often they occur "
                              "(respects the Department filter above).")
        info.pack(anchor="w")

        cols = ("count", "problem", "equipment")
        self.prob_tree = ttk.Treeview(frame, columns=cols, show="headings")
        self.prob_tree.heading("count", text="Times",
                               command=lambda: self._sort_problems("count"))
        self.prob_tree.heading("problem", text="Problem",
                               command=lambda: self._sort_problems("problem"))
        self.prob_tree.heading("equipment", text="Equipment Affected",
                               command=lambda: self._sort_problems("equipment"))
        self.prob_tree.column("count", width=70, anchor="center", stretch=False)
        self.prob_tree.column("problem", width=600, anchor="w")
        self.prob_tree.column("equipment", width=400, anchor="w")

        pvsb = ttk.Scrollbar(frame, orient="vertical", command=self.prob_tree.yview)
        self.prob_tree.configure(yscrollcommand=pvsb.set)
        self.prob_tree.pack(side="left", fill="both", expand=True)
        pvsb.pack(side="right", fill="y")

        self._prob_sort_state = {}

    # --------------------------------------------------------------- data
    def _open_file(self):
        path = filedialog.askopenfilename(
            title="Open work order data",
            filetypes=[("Data files", "*.json *.csv"), ("All files", "*.*")],
            initialdir=OUTPUT_DIR,
        )
        if path:
            self._load_data(path)

    def _load_data(self, path: str = None):
        records = []
        try:
            if path and path.lower().endswith(".csv"):
                records = self._read_csv(path)
            elif path and path.lower().endswith(".json"):
                records = self._read_json(path)
            elif os.path.exists(JSON_PATH):
                records = self._read_json(JSON_PATH)
            elif os.path.exists(CSV_PATH):
                records = self._read_csv(CSV_PATH)
            else:
                messagebox.showwarning(
                    "No data",
                    "Could not find work_orders_unscheduled.json or .csv.\n"
                    "Run scraper.py first."
                )
                return
        except Exception as e:
            messagebox.showerror("Load error", str(e))
            return

        self.all_records = records
        # populate department dropdown
        depts = sorted({(r.get("department") or "").strip()
                        for r in records if (r.get("department") or "").strip()})
        self.dept_combo["values"] = ["All"] + depts
        if self.dept_var.get() not in (["All"] + depts):
            self.dept_var.set("All")

        self._apply_filters()

    @staticmethod
    def _read_json(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _read_csv(path):
        with open(path, encoding="utf-8") as f:
            return list(csv.DictReader(f))

    # ------------------------------------------------------------ filters
    def _apply_filters(self):
        dept = self.dept_var.get()
        query = self.search_var.get().strip().lower()

        rows = self.all_records
        if dept and dept != "All":
            rows = [r for r in rows if (r.get("department") or "").strip() == dept]
        if query:
            rows = [r for r in rows
                    if any(query in str(v).lower() for v in r.values())]

        self.filtered = rows
        self._refresh_grid()
        self._refresh_problems()
        self.count_label.config(text=f"{len(rows)} work orders")

    def _refresh_grid(self):
        self.tree.delete(*self.tree.get_children())
        for i, r in enumerate(self.filtered):
            values = [r.get(key, "") for key, _, _ in GRID_COLUMNS]
            self.tree.insert("", "end", iid=str(i), values=values)

    def _refresh_problems(self):
        self.prob_tree.delete(*self.prob_tree.get_children())

        groups = {}   # normalized -> {"display":..., "count":..., "equip":set()}
        for r in self.filtered:
            problem = (r.get("problem") or "").strip()
            if not problem:
                continue
            key = _norm_problem(problem)
            g = groups.setdefault(key, {"display": problem, "count": 0, "equip": set()})
            g["count"] += 1
            eq = (r.get("equipment_name") or r.get("equipment_eq_id") or "").strip()
            if eq:
                g["equip"].add(eq)

        # sort by count desc by default
        ordered = sorted(groups.values(), key=lambda g: g["count"], reverse=True)
        self._problem_rows = ordered
        for g in ordered:
            self.prob_tree.insert(
                "", "end",
                values=(g["count"], g["display"], ", ".join(sorted(g["equip"])))
            )

    # ------------------------------------------------------------- sorting
    def _sort_by(self, key):
        asc = self.sort_state.get(key, True)
        self.sort_state[key] = not asc

        def sort_key(r):
            v = r.get(key, "")
            # try numeric sort where sensible
            try:
                return (0, float(str(v).replace("$", "").replace(",", "")))
            except (ValueError, TypeError):
                return (1, str(v).lower())

        self.filtered.sort(key=sort_key, reverse=not asc)
        self._refresh_grid()

    def _sort_problems(self, col):
        asc = self._prob_sort_state.get(col, True)
        self._prob_sort_state[col] = not asc
        rows = getattr(self, "_problem_rows", [])
        if col == "count":
            rows.sort(key=lambda g: g["count"], reverse=not asc)
        elif col == "problem":
            rows.sort(key=lambda g: g["display"].lower(), reverse=not asc)
        else:
            rows.sort(key=lambda g: ", ".join(sorted(g["equip"])).lower(),
                      reverse=not asc)
        self.prob_tree.delete(*self.prob_tree.get_children())
        for g in rows:
            self.prob_tree.insert(
                "", "end",
                values=(g["count"], g["display"], ", ".join(sorted(g["equip"])))
            )

    # -------------------------------------------------------------- detail
    def _on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        try:
            r = self.filtered[int(sel[0])]
        except (IndexError, ValueError):
            return

        lines = [
            f"Equipment : {r.get('equipment_eq_id','')}  -  {r.get('equipment_name','')}",
            f"Department: {r.get('department','')}",
            f"WO #      : {r.get('wo_id','')}",
            f"Notified  : {r.get('date_notified','')}     Urgency: {r.get('urgency','')}",
            f"Status    : {r.get('status','')}",
            f"Performed : {r.get('work_performed_by','')}",
            f"Labor (h) : {r.get('labor_time','')}     Material $: {r.get('material_cost','')}"
            f"     Downtime (h): {r.get('downtime_hours','')}",
            f"Completed : {r.get('completed_datetime','')}",
            "",
            "PROBLEM:",
            f"  {r.get('problem','') or '(none)'}",
            "",
            "COMMENTS:",
            f"  {r.get('comments','') or '(none)'}",
            "",
            "ATTACHMENTS:",
            f"  {r.get('attachments','') or '(none)'}",
        ]
        self.detail.config(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(lines))
        self.detail.config(state="disabled")


if __name__ == "__main__":
    WorkOrderViewer().mainloop()
