"""
PM Equipment Data Viewer
A simple GUI application to view, search, and sort equipment data.
"""

import csv
import json
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from typing import List, Dict, Any, Optional


class EquipmentViewer:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("PM Equipment Data Viewer")
        self.root.geometry("1400x800")
        
        # Data storage
        self.all_records: List[Dict[str, str]] = []
        self.filtered_records: List[Dict[str, str]] = []
        self.sort_column: str = ""
        self.sort_reverse: bool = False
        
        # Column configuration
        self.columns = [
            ("eq_id", "EQ ID", 80),
            ("dept", "Dept", 100),
            ("dept_num", "Dept#", 50),
            ("equipment_name", "Equipment Name", 250),
            ("asset_num", "Asset#", 80),
            ("make", "Make", 150),
            ("model", "Model", 150),
            ("vendor", "Vendor", 150),
            ("wo_qty", "WO Qty", 60),
            ("modified", "Modified", 150)
        ]
        
        self._setup_ui()
        self._load_default_data()
    
    def _setup_ui(self):
        """Setup the user interface."""
        # Top frame for controls
        control_frame = ttk.Frame(self.root, padding="10")
        control_frame.pack(fill=tk.X)
        
        # File controls
        ttk.Button(control_frame, text="Load CSV", command=self._load_csv).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="Load JSON", command=self._load_json).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="Parse HTML", command=self._parse_html).pack(side=tk.LEFT, padx=5)
        
        ttk.Separator(control_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        
        # Search controls
        ttk.Label(control_frame, text="Search:").pack(side=tk.LEFT, padx=5)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(control_frame, textvariable=self.search_var, width=30)
        self.search_entry.pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="Search", command=self._apply_search).pack(side=tk.LEFT, padx=5)
        ttk.Button(control_frame, text="Clear", command=self._clear_search).pack(side=tk.LEFT, padx=5)
        
        ttk.Separator(control_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        
        # Filter by department
        ttk.Label(control_frame, text="Filter Dept:").pack(side=tk.LEFT, padx=5)
        self.dept_filter_var = tk.StringVar(value="All")
        self.dept_combo = ttk.Combobox(control_frame, textvariable=self.dept_filter_var, width=15, state="readonly")
        self.dept_combo.pack(side=tk.LEFT, padx=5)
        self.dept_combo.bind("<<ComboboxSelected>>", lambda e: self._apply_filters())
        
        ttk.Separator(control_frame, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=10)
        
        # Stats label
        self.stats_label = ttk.Label(control_frame, text="Records: 0")
        self.stats_label.pack(side=tk.LEFT, padx=10)
        
        # Export button
        ttk.Button(control_frame, text="Export Filtered CSV", command=self._export_filtered).pack(side=tk.RIGHT, padx=5)
        
        # Treeview frame
        tree_frame = ttk.Frame(self.root)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        
        # Create Treeview
        self.tree = ttk.Treeview(
            tree_frame,
            columns=[col[0] for col in self.columns],
            show="headings",
            selectmode="browse"
        )
        
        # Configure columns
        for col_id, col_name, col_width in self.columns:
            self.tree.heading(col_id, text=col_name, command=lambda c=col_id: self._sort_by(c))
            self.tree.column(col_id, width=col_width, anchor=tk.W)
        
        # Scrollbars
        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient=tk.HORIZONTAL, command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        
        # Grid layout for tree and scrollbars
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tree_frame.grid_rowconfigure(0, weight=1)
        tree_frame.grid_columnconfigure(0, weight=1)
        
        # Bind double-click to show details
        self.tree.bind("<Double-1>", self._show_details)
        
        # Bind Enter key in search box
        self.search_entry.bind("<Return>", lambda e: self._apply_search())
        
        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)
    
    def _load_default_data(self):
        """Try to load default data file."""
        import os
        default_csv = r"c:\Users\merja.haatanen\OneDrive - Bobrick Washroom Equipment\Desktop\PM Web Scraper\equipment_data.csv"
        if os.path.exists(default_csv):
            self._load_csv_file(default_csv)
    
    def _parse_html(self):
        """Parse the HTML file directly."""
        import os
        from parse_html import EquipmentHTMLParser
        
        html_file = r"c:\Users\merja.haatanen\OneDrive - Bobrick Washroom Equipment\Desktop\PM Web Scraper\Equipment All HTML"
        
        if not os.path.exists(html_file):
            messagebox.showerror("Error", f"HTML file not found:\n{html_file}")
            return
        
        try:
            parser = EquipmentHTMLParser(html_file)
            parser.parse()
            
            if parser.records:
                self.all_records = [record.__dict__ for record in parser.records]
                self.filtered_records = self.all_records.copy()
                
                output_dir = r"c:\Users\merja.haatanen\OneDrive - Bobrick Washroom Equipment\Desktop\PM Web Scraper"
                parser.save_to_csv(os.path.join(output_dir, "equipment_data.csv"))
                parser.save_to_json(os.path.join(output_dir, "equipment_data.json"))
                
                self._update_dept_filter()
                self._refresh_tree()
                self._update_stats()
                self.status_var.set(f"Parsed {len(self.all_records)} records from HTML")
            else:
                messagebox.showwarning("Warning", "No records found in HTML file")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to parse HTML:\n{e}")
    
    def _load_csv(self):
        """Open dialog to load CSV file."""
        filename = filedialog.askopenfilename(
            title="Select CSV file",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=r"c:\Users\merja.haatanen\OneDrive - Bobrick Washroom Equipment\Desktop\PM Web Scraper"
        )
        if filename:
            self._load_csv_file(filename)
    
    def _load_csv_file(self, filename: str):
        """Load data from CSV file."""
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                self.all_records = list(reader)
                self.filtered_records = self.all_records.copy()
            
            self._update_dept_filter()
            self._refresh_tree()
            self._update_stats()
            self.status_var.set(f"Loaded {len(self.all_records)} records from {filename}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load CSV: {e}")
    
    def _load_json(self):
        """Open dialog to load JSON file."""
        filename = filedialog.askopenfilename(
            title="Select JSON file",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
            initialdir=r"c:\Users\merja.haatanen\OneDrive - Bobrick Washroom Equipment\Desktop\PM Web Scraper"
        )
        if filename:
            self._load_json_file(filename)
    
    def _load_json_file(self, filename: str):
        """Load data from JSON file."""
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                self.all_records = json.load(f)
                self.filtered_records = self.all_records.copy()
            
            self._update_dept_filter()
            self._refresh_tree()
            self._update_stats()
            self.status_var.set(f"Loaded {len(self.all_records)} records from {filename}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load JSON: {e}")
    
    def _update_dept_filter(self):
        """Update department filter dropdown with unique departments."""
        depts = set(r.get("dept", "") for r in self.all_records if r.get("dept"))
        dept_list = ["All"] + sorted(depts)
        self.dept_combo['values'] = dept_list
        self.dept_filter_var.set("All")
    
    def _apply_search(self):
        """Apply search filter."""
        search_term = self.search_var.get().lower()
        self._apply_filters(search_term)
    
    def _clear_search(self):
        """Clear search and filters."""
        self.search_var.set("")
        self.dept_filter_var.set("All")
        self.filtered_records = self.all_records.copy()
        self._refresh_tree()
        self._update_stats()
        self.status_var.set("Filters cleared")
    
    def _apply_filters(self, search_term: str = ""):
        """Apply all active filters."""
        if not search_term:
            search_term = self.search_var.get().lower()
        
        dept_filter = self.dept_filter_var.get()
        
        self.filtered_records = []
        for record in self.all_records:
            # Department filter
            if dept_filter != "All" and record.get("dept") != dept_filter:
                continue
            
            # Search filter
            if search_term:
                found = False
                for value in record.values():
                    if search_term in str(value).lower():
                        found = True
                        break
                if not found:
                    continue
            
            self.filtered_records.append(record)
        
        self._refresh_tree()
        self._update_stats()
        self.status_var.set(f"Showing {len(self.filtered_records)} of {len(self.all_records)} records")
    
    def _sort_by(self, col: str):
        """Sort records by column."""
        if self.sort_column == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_column = col
            self.sort_reverse = False
        
        # Sort the filtered records
        try:
            # Try numeric sort first
            self.filtered_records.sort(
                key=lambda x: float(x.get(col, 0) or 0) if x.get(col, 0) and x.get(col, 0).replace('.','',1).isdigit() else str(x.get(col, "")).lower(),
                reverse=self.sort_reverse
            )
        except:
            # Fall back to string sort
            self.filtered_records.sort(
                key=lambda x: str(x.get(col, "")).lower(),
                reverse=self.sort_reverse
            )
        
        self._refresh_tree()
        
        # Update heading to show sort direction
        for col_id, col_name, _ in self.columns:
            if col_id == col:
                direction = " ▼" if self.sort_reverse else " ▲"
                self.tree.heading(col_id, text=col_name + direction)
            else:
                self.tree.heading(col_id, text=col_name)
    
    def _refresh_tree(self):
        """Refresh treeview with current filtered records."""
        # Clear existing items
        for item in self.tree.get_children():
            self.tree.delete(item)
        
        # Add records
        for record in self.filtered_records:
            values = [record.get(col[0], "") for col in self.columns]
            self.tree.insert("", tk.END, values=values)
    
    def _update_stats(self):
        """Update statistics display."""
        self.stats_label.config(text=f"Showing: {len(self.filtered_records)} / Total: {len(self.all_records)}")
    
    def _show_details(self, event):
        """Show details dialog for selected record."""
        selected = self.tree.selection()
        if not selected:
            return
        
        item = self.tree.item(selected[0])
        values = item['values']
        
        # Create details dialog
        dialog = tk.Toplevel(self.root)
        dialog.title("Equipment Details")
        dialog.geometry("500x400")
        dialog.transient(self.root)
        dialog.grab_set()
        
        # Create scrolled text for details
        text = tk.Text(dialog, wrap=tk.WORD, padx=10, pady=10)
        text.pack(fill=tk.BOTH, expand=True)
        
        scrollbar = ttk.Scrollbar(text, command=text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text.configure(yscrollcommand=scrollbar.set)
        
        # Format details
        details = []
        for i, (col_id, col_name, _) in enumerate(self.columns):
            details.append(f"{col_name}:")
            details.append(f"  {values[i] if i < len(values) else 'N/A'}")
            details.append("")
        
        text.insert(tk.END, "\n".join(details))
        text.configure(state=tk.DISABLED)
        
        # Close button
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=10)
    
    def _export_filtered(self):
        """Export filtered records to CSV."""
        if not self.filtered_records:
            messagebox.showwarning("Warning", "No records to export")
            return
        
        filename = filedialog.asksaveasfilename(
            title="Export Filtered Data",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")],
            initialdir=r"c:\Users\merja.haatanen\OneDrive - Bobrick Washroom Equipment\Desktop\PM Web Scraper",
            initialfile="filtered_equipment_data.csv"
        )
        
        if not filename:
            return
        
        try:
            with open(filename, 'w', newline='', encoding='utf-8') as f:
                if self.filtered_records:
                    fieldnames = self.filtered_records[0].keys()
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()
                    writer.writerows(self.filtered_records)
            
            self.status_var.set(f"Exported {len(self.filtered_records)} records to {filename}")
            messagebox.showinfo("Success", f"Exported {len(self.filtered_records)} records to:\n{filename}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to export: {e}")


def main():
    root = tk.Tk()
    app = EquipmentViewer(root)
    root.mainloop()


if __name__ == "__main__":
    main()
