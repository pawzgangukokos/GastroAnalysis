import os
import re
import sys
import json
import platform
import contextlib
from datetime import datetime, timedelta
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

from database import init_db, backup_db, restore_db, get_connection, log_error, BACKUP_DIR
from importer import CSVImporter, UnrecognizedCSVFormatError, merge_brands, import_day_folder_recursive
import brand_service
from analytics import GastroAnalytics, get_location_date_bounds
from importer import import_payouts_recursive
from payout_analytics import get_loss_summary, get_payout_breakdown_by_brand_and_platform

OS_NAME = platform.system()
FONT_FAMILY = "SF Pro Text" if OS_NAME == "Darwin" else ("Segoe UI" if OS_NAME == "Windows" else "Inter")
DISPLAY_FONT = "SF Pro Display" if OS_NAME == "Darwin" else FONT_FAMILY

PALETTE = {
    "Light": {
        "canvas": "#F7F7F8", "sidebar": "#EFEFF1", "card": "#FFFFFF", "border": "#E5E7EB",
        "text": "#18181B", "muted": "#6B7280", "primary_bg": "#18181B", "primary_hover": "#27272A",
        "primary_fg": "#FFFFFF", "ghost_border": "#D1D5DB", "ghost_hover": "#E4E4E7",
        "nav_active": "#E4E4E7", "nav_hover": "#E8E8EB", "danger": "#B91C1C",
        "danger_hover": "#991B1B", "grid": "#E5E7EB"
    },
    "Dark": {
        "canvas": "#141517", "sidebar": "#18191C", "card": "#1C1D21", "border": "#2A2C32",
        "text": "#F4F4F5", "muted": "#9CA3AF", "primary_bg": "#F4F4F5", "primary_hover": "#E4E4E7",
        "primary_fg": "#18181B", "ghost_border": "#3F3F46", "ghost_hover": "#27272A",
        "nav_active": "#27272A", "nav_hover": "#202126", "danger": "#DC2626",
        "danger_hover": "#B91C1C", "grid": "#2A2C32"
    }
}
CHART_COLORS = ["#334155", "#D97706", "#0D9488", "#4F46E5", "#64748B", "#B45309"]
TIME_HHMM_REGEX = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

def safe_modal_grab(win: ctk.CTkToplevel):
    try:
        win.update_idletasks()
        win.wait_visibility()
        win.grab_set()
        win.focus_force()
    except tk.TclError:
        pass

def safe_action(func):
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            log_error(f"UI Action Error in {func.__name__}: {str(e)}")
            messagebox.showerror("Application Error", f"{str(e)}\n\nDetails were logged to error_log.txt.")
    return wrapper

def validate_date(date_str: str) -> bool:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return True
    except ValueError:
        return False

def validate_hhmm(time_str: str) -> bool:
    return bool(TIME_HHMM_REGEX.match(time_str.strip()))


class TopDishesDialog(ctk.CTkToplevel):
    def __init__(self, parent, theme_mode: str, dishes: list[tuple[str, int]], total_food_items: int = 0):
        super().__init__(parent)
        self.title("Top 10 Selling Dishes")
        self.geometry("640x460")
        self.transient(parent)
        t = PALETTE[theme_mode]
        self.configure(fg_color=t["canvas"])

        ctk.CTkLabel(
            self,
            text="Top 10 Selling Dishes (Selected Period)",
            font=ctk.CTkFont(family=DISPLAY_FONT, size=18, weight="bold"),
            text_color=t["text"]
        ).pack(anchor="w", padx=20, pady=(18, 6))

        card = ctk.CTkFrame(self, fg_color=t["card"], corner_radius=8)
        card.pack(fill="both", expand=True, padx=20, pady=(6, 14))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)

        if not dishes:
            total_db_items = 0
            try:
                with contextlib.closing(get_connection()) as conn:
                    row = conn.execute("SELECT COUNT(*) FROM order_items").fetchone()
                    total_db_items = int(row[0]) if row else 0
            except Exception:
                total_db_items = 0

            if total_db_items == 0:
                msg = (
                    "The dish items table (order_items) in the database is currently empty.\n\n"
                    "Please re-import your Wolt or POS CSV files via Settings & Import -> Import CSV "
                    "to extract and populate individual dish names."
                )
            else:
                msg = (
                    "No itemized dish names were found for the currently selected location, brand, or date range.\n\n"
                    "Note: Some platforms (such as Bolt) only export total item counts per order without "
                    "individual dish names, or there are no itemized orders in this date range."
                )

            ctk.CTkLabel(
                card,
                text=msg,
                font=ctk.CTkFont(family=FONT_FAMILY, size=13),
                text_color=t["muted"],
                justify="center",
                wraplength=520
            ).grid(row=0, column=0, padx=24, pady=40)
        else:
            total_listed = sum(q for _, q in dishes)
            denom = total_food_items if total_food_items > 0 else total_listed

            cols = ("Rank", "Dish Name", "Sold Qty", "Share %")
            tree = ttk.Treeview(card, columns=cols, show="headings", height=12)
            tree.heading("Rank", text="Rank")
            tree.heading("Dish Name", text="Dish Name")
            tree.heading("Sold Qty", text="Sold Qty")
            tree.heading("Share %", text="Share %")

            tree.column("Rank", width=65, anchor="center")
            tree.column("Dish Name", width=330, anchor="w")
            tree.column("Sold Qty", width=95, anchor="e")
            tree.column("Share %", width=95, anchor="e")

            vsb = ttk.Scrollbar(card, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=vsb.set)
            tree.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=12)
            vsb.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=12)

            for rank, (name, qty) in enumerate(dishes, 1):
                share_pct = (qty / denom * 100.0) if denom > 0 else 0.0
                tree.insert("", "end", values=(f"#{rank}", name, f"{qty:,}", f"{share_pct:.1f}%"))

        ctk.CTkButton(self, text="Close", width=110, command=self.destroy).pack(pady=(0, 16))
        safe_modal_grab(self)


class UserGuideDialog(ctk.CTkToplevel):
    def __init__(self, parent, theme_mode: str):
        super().__init__(parent)
        self.title("GastroAnalytics — User Guide")
        self.geometry("840x660")
        self.transient(parent)
        t = PALETTE[theme_mode]
        self.configure(fg_color=t["canvas"])
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            self,
            text="GastroAnalytics — User Guide",
            font=ctk.CTkFont(family=DISPLAY_FONT, size=20, weight="bold"),
            text_color=t["text"],
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 8))

        scroll = ctk.CTkScrollableFrame(self, fg_color=t["card"], corner_radius=8)
        scroll.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 12))
        scroll.grid_columnconfigure(0, weight=1)

        guide_text = """1. WHAT THIS APP DOES (OVERVIEW)
GastroAnalytics helps multi-brand kitchens and delivery operations track order volume, kitchen workload, and platform payout costs in one place:
- Dashboard: KPI cards (revenue, AOV, orders, food items, rejected orders/lost revenue, top dishes, busiest/quietest dates, best/worst weekday, ratings during rush vs. off-peak), 4 charts (daily trend, weekday averages, platform revenue share, meal shifts), 30-minute HR staffing heatmap (Orders / Items + suggested cooks), and PDF / .xlsx exports.
- Daily Breakdown: Per-day, per-brand table across all days in the database or a selected date range, with column sorting, status highlighting (Open, Closed, Disrupted, rejections), summary totals, and .xlsx / .csv export.
- Losses & Payouts: Deduplicated financial summary and per-restaurant/platform breakdown from Bolt Food and Wolt payout/invoice documents (gross sales incl. VAT, commissions & fees, ads & promos, merchant discounts, deductions & refunds, total costs, cost share %, and platform payout), with .xlsx / .csv export.
- Settings: All-in-one folder import, custom CSV/XLSX column mapper, restaurant/brand & alias management (including merging duplicate restaurants and toggling Active/Inactive status), continuous staffing rules, meal shifts, and item exclusion keywords.
- Backups & Safety: Automatic database backups on startup and before major changes/imports/merges/deletes, duplicate protection, error logging (error_log.txt), and safe backup restore with SQLite integrity checks.

2. STEP-BY-STEP WORKFLOW
Step A — Import Order & Payout Files:
1. Go to Settings -> Import and click "Import Folder".
2. Select a folder (subfolders are scanned recursively). PDFs and payout/settlement CSVs go to the payouts importer; order CSV/XLSX files (Wolt, Bolt, or matching custom templates) go to the orders importer.
3. For non-standard order CSV/XLSX exports, use "Custom Column Mapper" to map columns (such as Order Date/Time, Order ID, Venue/Brand Name, Items Count or Items Text, Sales Amount, Status, Customer Rating) and save a reusable template.
4. Duplicate orders, duplicate files (SHA-256), and duplicate report fingerprints are skipped automatically. Cancelled, rejected, refunded, or void orders are excluded from completed sales.

Step B — Manage Restaurants, Aliases & Merges:
1. Delivery platforms sometimes list the same venue under different names or formats.
2. In Settings -> Restaurants & Brands, you can:
   - Add locations ("+ Loc") or brands ("+ Brand"), or edit a restaurant's Name, Location, and Status ("Active" / "Inactive"). Inactive brands are excluded from active analytics views.
   - Review "Unmapped / Pending Names" (when "Auto-create new brands on import" is unchecked or unmapped items exist), assign them to an existing brand, create a new brand, defer them ("Later"), or click "Re-apply".
   - Use "Merge Restaurant" -> "Merge & Save Alias" to merge a duplicate/secondary restaurant entry into a target restaurant while moving its records and saving the source name/aliases for future imports.

Step C — Configure Shifts, Staffing Rules & Dish Exclusions:
1. Meal Shifts (Settings -> Meal Shifts): Define 24-hour HH:MM windows (e.g. Pre-Lunch, Lunch Rush, Afternoon, Dinner Rush, Late Night). Shifts containing "Rush" are used to compare Peak Rush vs. Off-Peak ratings.
2. Staffing Rules (Settings -> Staffing Rules): Edit continuous load thresholds (Orders/hr and Items/hr, starting at 0.0 without gaps or overlaps) to set suggested cooks and load labels (e.g. Quiet, Steady, Busy, Peak Rush) shown in the 30-minute heatmap.
3. Exclusions (Settings -> Exclusions): Enter comma-separated non-kitchen keywords (e.g. drinks, sauces, bottles, cutlery) so they are excluded when tallying dish names for the Top 10 Selling Dishes modal (click the Top Selling Dishes KPI card on the Dashboard).

Step D — Review Analytics, Finances & Exports:
1. Filter Dashboard, Daily Breakdown, or Losses & Payouts by date range, location, restaurant/brand, or platform.
2. Export reports to PDF or .xlsx from the Dashboard, or .xlsx / .csv from Daily Breakdown and Losses & Payouts.
3. Use "Restore Backup" in the sidebar if you ever need to roll back to an earlier snapshot.

3. FORMULAS & HOW NUMBERS ARE CALCULATED
- Completed Sales (Revenue):
  Sum of order totals for non-cancelled/non-rejected orders in the selected scope and period (for active brands).

- Average Order Value (AOV):
  AOV = Total Completed Sales / Number of Completed Orders
  (0.00 when Completed Orders = 0.)

- 5-Star Order Share (%):
  FiveStarPct = (Orders with Rating == 5.0 / Total Rated Orders) * 100

- Rejected / Cancelled Orders & Lost Revenue:
  Rejected Orders = Count of orders with status in (rejected, cancelled, canceled, refunded, void, odrzucone, odrzucony, anulowane, anulowany, zwrocone)
  Lost Revenue = Sum of order totals for those rejected/cancelled orders

- Total Costs & Cost Share (%):
  Total Costs = Commissions & Fees + Ads & Campaigns + Merchant Discounts + Deductions & Refunds + Other Adjustments
  Cost Share % = (Total Costs / Gross Sales Total) * 100

- Deduplication & Partial Period Overlap Ratio (Losses & Payouts):
  When multiple documents cover the same platform/brand/period window, overlapping sales_report figures are deduplicated against settlement/invoice figures so sales and costs are not double-counted.
  When filtering payouts by Date From / Date To, a report spanning [P_start, P_end] of length L_period = (P_end - P_start) + 1 day overlapping the filter by OverlapDays days is scaled by:
    OverlapRatio = OverlapDays / L_period
    ScaledValue = PeriodValue * OverlapRatio

4. WORKED EXAMPLES
Example 1 — Completed vs. Cancelled Orders & AOV:
- Order #1: EUR 20.00, 2 items, status = delivered, rating = 5
- Order #2: EUR 30.00, 3 items, status = delivered, rating = 4
- Order #3: EUR 25.00, 2 items, status = cancelled, rating = empty
Results: Completed Orders = 2; Total Food Items = 5; Total Food Sales = EUR 50.00; AOV = EUR 25.00; Rejected Orders = 1; Lost Revenue = EUR 25.00; 5-Star Share = 50.0%.

Example 2 — Partial Period Overlap on a 7-Day Payout:
- Report covers 2026-10-01 to 2026-10-07 (L_period = 7 days), Gross Sales = EUR 700.00, Total Costs = EUR 70.00, Payout = EUR 630.00.
- Filter is 2026-10-01 to 2026-10-03 -> OverlapDays = 3, OverlapRatio = 3 / 7.
- Scaled Gross Sales = EUR 300.00; Scaled Costs = EUR 30.00; Scaled Payout = EUR 270.00; Cost Share % = 10.0%.

Example 3 — Combined Brands in One Kitchen:
- In a single 30-minute slot, Brand A has 4 orders and Brand B has 6 orders.
- Viewing a single brand shows only that brand's load; selecting "All Brands Combined" / "All Active Restaurants" sums them (10 orders) for combined kitchen staffing load.

5. QUICK TROUBLESHOOTING
- Missing numbers or 0 values? Check the date range (YYYY-MM-DD), selected location/brand, whether the brand is marked Active, or whether orders were cancelled.
- Top 10 Selling Dishes empty? Bolt exports often provide item counts without dish names; itemized dish names come from order item strings in supported/mapped exports.
- Need data directory override? Set the GASTRO_APP_DIR environment variable (default is ~/Documents/GastroAnalytics with ~/.gastroanalytics fallback).
"""

        ctk.CTkLabel(
            scroll,
            text=guide_text,
            font=ctk.CTkFont(family=FONT_FAMILY, size=13),
            justify="left",
            anchor="nw",
            text_color=t["text"],
            wraplength=760,
        ).grid(row=0, column=0, sticky="nw", padx=16, pady=14)

        ctk.CTkButton(
            self,
            text="Close",
            width=110,
            fg_color=t["primary_bg"],
            hover_color=t["primary_hover"],
            text_color=t["primary_fg"],
            command=self.destroy,
        ).grid(row=2, column=0, pady=(0, 16))

        safe_modal_grab(self)
class RestoreBackupDialog(ctk.CTkToplevel):
    def __init__(self, parent, theme_mode: str, on_restored_callback):
        super().__init__(parent)
        self.title("Restore Backup")
        self.geometry("480x340")
        self.transient(parent)
        self.on_restored_callback = on_restored_callback
        t = PALETTE[theme_mode]
        self.configure(fg_color=t["canvas"])

        self.listbox = tk.Listbox(self, height=10, bg=t["card"], fg=t["text"])
        self.listbox.pack(fill="both", expand=True, padx=24, pady=24)
        backups = sorted(BACKUP_DIR.glob("*.db"), key=os.path.getmtime, reverse=True)
        self.backup_files = [b.name for b in backups]
        for fname in self.backup_files:
            self.listbox.insert(tk.END, fname)

        ctk.CTkButton(self, text="Restore Snapshot", command=self._do_restore, fg_color=t["danger"]).pack(pady=10)
        safe_modal_grab(self)

    @safe_action
    def _do_restore(self):
        sel = self.listbox.curselection()
        if sel and messagebox.askyesno("Confirm", "Restore selected database snapshot?"):
            restore_db(self.backup_files[sel[0]])
            self.destroy()
            self.on_restored_callback()
            messagebox.showinfo("Success", "Backup restored successfully.")


class CustomMapperDialog(ctk.CTkToplevel):
    def __init__(self, parent, theme_mode: str, initial_filepath: str, on_imported_callback):
        super().__init__(parent)
        self.title("Custom Column Mapper")
        self.geometry("820x540")
        self.transient(parent)
        self.filepath = initial_filepath
        self.on_imported_callback = on_imported_callback
        self.t = PALETTE[theme_mode]
        self.configure(fg_color=self.t["canvas"])
        self.mapper_dropdowns = {}
        self._build_ui()
        if self.filepath:
            self._load_file(self.filepath)
        safe_modal_grab(self)

    def _build_ui(self):
        top = ctk.CTkFrame(self, fg_color=self.t["card"])
        top.pack(fill="x", padx=24, pady=20)
        self.template_name_entry = ctk.CTkEntry(top, placeholder_text="e.g. Custom POS Export", width=240)
        self.template_name_entry.pack(side="left", padx=10, pady=10)

        grid_card = ctk.CTkFrame(self, fg_color=self.t["card"])
        grid_card.pack(fill="x", padx=24, pady=10)

        fields = [
            "Order Date/Time", "Order ID", "Venue/Brand Name",
            "Items Count or Items Text", "Sales Amount (Gross)", "Sales Amount (Net)",
            "Status", "Customer Rating"
        ]
        for idx, field in enumerate(fields):
            r, c = divmod(idx, 2)
            ctk.CTkLabel(grid_card, text=field).grid(row=r, column=c*2, padx=10, pady=5, sticky="w")
            cb = ctk.CTkComboBox(grid_card, values=["(Ignore)"])
            cb.grid(row=r, column=c*2+1, padx=10, pady=5)
            self.mapper_dropdowns[field] = cb

        ctk.CTkButton(self, text="Save & Import", command=self._save_and_import).pack(pady=20)

    @safe_action
    def _load_file(self, path: str):
        importer = CSVImporter()
        self.df = importer.sniff_table(path)
        cols = ["(Ignore)"] + list(self.df.columns)
        for cb in self.mapper_dropdowns.values():
            cb.configure(values=cols)
            cb.set("(Ignore)")

    @safe_action
    def _save_and_import(self):
        if not self.filepath:
            path = filedialog.askopenfilename(title="Select CSV", filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")])
            if not path:
                return
            self.filepath = path

        mapping = {val: k for k, cb in self.mapper_dropdowns.items() if (val := cb.get()) != "(Ignore)"}
        if "Order Date/Time" not in mapping.values() or "Order ID" not in mapping.values():
            messagebox.showwarning("Missing", "Map Order ID and Date/Time.")
            return

        tmpl_name = self.template_name_entry.get().strip() or f"Custom_{datetime.now().strftime('%Y%m%d')}"
        with contextlib.closing(get_connection()) as conn:
            conn.execute("INSERT OR REPLACE INTO csv_templates (template_name, mapping_json) VALUES (?, ?)", (tmpl_name, json.dumps(mapping)))
            conn.commit()

        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            importer = CSVImporter(auto_create_brands=brand_service.get_auto_create_brands_setting())
            res = importer.auto_import_file(self.filepath, override_mapping=mapping, override_platform=tmpl_name)
        finally:
            self.configure(cursor="")
        self.destroy()
        self.on_imported_callback(res)


def _format_kpi_date_weekday(raw_val) -> str:
    """Formats date string with short weekday name, e.g. '2026-10-24 (Sat)'."""
    import re
    from datetime import datetime

    txt = str(raw_val or "").strip()
    if not txt or txt.upper() == "N/A":
        return txt or "N/A"
    m = re.search(r"(\d{4}-\d{2}-\d{2})", txt)
    if not m:
        return txt[:24]
    dt_str = m.group(1)
    try:
        wd = datetime.strptime(dt_str, "%Y-%m-%d").strftime("%a")
        return f"{dt_str} ({wd})"
    except ValueError:
        return dt_str

class GastroAnalyticsApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        init_db()
        backup_db()
        self.theme_mode = "Light"
        ctk.set_appearance_mode("Light")
        self.title("GastroAnalytics")
        self.geometry("1440x900")
        self.protocol("WM_DELETE_WINDOW", self._on_app_close)

        self.active_figures = []
        self.top_dishes_win = None
        self.current_top_dishes = []
        self.current_total_food_items = 0
        self.current_daily_df = pd.DataFrame()
        self.daily_sort_reverse = {}
        self.brand_merge_map = {}
        self.duplicate_choice_map = {}
        self.location_map = {}
        self._analytics_cache = {}
        self._last_dashboard_key = None
        self._dirty_tabs = {"dashboard", "daily", "finances", "settings"}

        self.font_body = ctk.CTkFont(family=FONT_FAMILY, size=13)
        self.font_bold = ctk.CTkFont(family=FONT_FAMILY, size=13, weight="bold")
        self.font_small = ctk.CTkFont(family=FONT_FAMILY, size=12)
        self.font_title = ctk.CTkFont(family=DISPLAY_FONT, size=22, weight="bold")
        self.font_section = ctk.CTkFont(family=DISPLAY_FONT, size=16, weight="bold")
        self.font_kpi = ctk.CTkFont(family=DISPLAY_FONT, size=18, weight="bold")

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_layout()
        self._populate_global_dropdowns()
        self.show_tab("dashboard", defer_refresh=True)
        self.after(20, self._deferred_initial_refresh)

    def _on_app_close(self):
        self._close_active_figures()
        try:
            plt.close("all")
        except Exception:
            pass
        try:
            for after_id in self.tk.call("after", "info"):
                try:
                    self.after_cancel(after_id)
                except Exception:
                    pass
        except Exception:
            pass
        try:
            self.quit()
            self.destroy()
        except Exception:
            pass

    def _t(self) -> dict:
        return PALETTE[self.theme_mode]

    def _configure_ttk_styles(self):
        t = self._t()
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Treeview",
            background=t["card"],
            foreground=t["text"],
            fieldbackground=t["card"],
            bordercolor=t["border"],
            lightcolor=t["border"],
            darkcolor=t["border"],
            rowheight=28,
            font=(FONT_FAMILY, 10),
        )
        style.configure(
            "Treeview.Heading",
            background=t["sidebar"],
            foreground=t["text"],
            bordercolor=t["border"],
            relief="flat",
            font=(FONT_FAMILY, 10, "bold"),
        )
        style.map(
            "Treeview",
            background=[("selected", "#3B82F6" if self.theme_mode == "Dark" else "#DBEAFE")],
            foreground=[("selected", "#FFFFFF" if self.theme_mode == "Dark" else "#18181B")],
        )
        style.map(
            "Treeview.Heading",
            background=[("active", t["nav_hover"])],
        )

    def _close_active_figures(self):
        for fig in self.active_figures:
            try:
                plt.close(fig)
            except Exception:
                pass
        self.active_figures.clear()

    def _bind_click_recursive(self, widget, callback):
        """Recursively binds <Button-1> and hand2 cursor to a container and all nested children."""
        try:
            widget.configure(cursor="hand2")
        except Exception:
            pass
        widget.bind("<Button-1>", callback)
        for child in widget.winfo_children():
            self._bind_click_recursive(child, callback)

    def _build_layout(self):
        self._configure_ttk_styles()
        t = self._t()
        self.configure(fg_color=t["canvas"])
        self.sidebar = ctk.CTkFrame(self, width=210, corner_radius=0, fg_color=t["sidebar"])
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_rowconfigure(6, weight=1)

        ctk.CTkLabel(self.sidebar, text="GastroAnalytics", font=self.font_title).grid(row=0, column=0, padx=20, pady=24, sticky="w")

        self.nav_buttons = {}
        nav_items = [
            ("dashboard", "Dashboard"),
            ("daily", "Daily Breakdown"),
            ("finances", "Losses & Payouts"),
            ("settings", "Settings")
        ]
        for idx, (key, label) in enumerate(nav_items, 1):
            btn = ctk.CTkButton(self.sidebar, text=label, anchor="w", fg_color="transparent", text_color=t["text"], command=lambda k=key: self.show_tab(k))
            btn.grid(row=idx, column=0, padx=12, pady=2, sticky="ew")
            self.nav_buttons[key] = btn


        self.guide_btn = ctk.CTkButton(self.sidebar, text="User Guide", command=lambda: UserGuideDialog(self, self.theme_mode), fg_color="transparent", text_color=t["text"], border_width=1, border_color=t["ghost_border"])
        self.guide_btn.grid(row=8, column=0, padx=14, pady=8, sticky="ew")

        self.restore_btn = ctk.CTkButton(self.sidebar, text="Restore Backup", command=lambda: RestoreBackupDialog(self, self.theme_mode, self._on_database_restored), fg_color="transparent", text_color=t["text"], border_width=1, border_color=t["ghost_border"])
        self.restore_btn.grid(row=9, column=0, padx=14, pady=(8, 20), sticky="ew")

        self.frames = {
            "dashboard": ctk.CTkFrame(self, fg_color="transparent"),
            "daily": ctk.CTkFrame(self, fg_color="transparent"),
            "finances": ctk.CTkFrame(self, fg_color="transparent"),
            "settings": ctk.CTkFrame(self, fg_color="transparent"),
        }
        self._build_dashboard_tab()
        self._build_daily_breakdown_tab()
        self._build_finances_tab()
        self._build_settings_tab()


    def mark_tabs_dirty(self, *tabs: str, clear_cache: bool = False):
        if clear_cache and hasattr(self, "invalidate_analytics_cache"):
            self.invalidate_analytics_cache()
        if not tabs:
            self._dirty_tabs = {"dashboard", "daily", "finances", "settings"}
        else:
            self._dirty_tabs.update(tabs)

    def _deferred_initial_refresh(self):
        if not self.winfo_exists():
            return
        if getattr(self, "current_tab", "dashboard") == "dashboard" and "dashboard" in getattr(self, "_dirty_tabs", set()):
            self._dirty_tabs.discard("dashboard")
            self.refresh_dashboard(force=True)

    def show_tab(self, tab_key: str, force: bool = False, defer_refresh: bool = False):
        self.current_tab = tab_key
        t = self._t()
        for key, btn in self.nav_buttons.items():
            if key == tab_key:
                btn.configure(fg_color=t["nav_active"], font=self.font_bold)
            else:
                btn.configure(fg_color="transparent", font=self.font_body)

        for key, frame in self.frames.items():
            if key == tab_key:
                frame.grid(row=0, column=1, sticky="nsew", padx=24, pady=20)
            else:
                frame.grid_forget()

        should_refresh = force or (tab_key in getattr(self, "_dirty_tabs", set()))
        if not should_refresh:
            return

        deferred = False
        if tab_key == "dashboard":
            if not defer_refresh:
                self.refresh_dashboard(force=force)
            else:
                deferred = True
        elif tab_key == "daily":
            self.refresh_daily_breakdown()
        elif tab_key == "finances":
            self.refresh_finances_tab()
        elif tab_key == "settings":
            self.refresh_all_settings()

        if not deferred:
            self._dirty_tabs.discard(tab_key)

    def _on_database_restored(self):
        self.invalidate_analytics_cache()
        self.mark_tabs_dirty()
        self._populate_global_dropdowns()
        self.show_tab(self.current_tab)

    def _open_top_dishes_modal(self, _event=None):
        if getattr(self, "top_dishes_win", None) is not None and self.top_dishes_win.winfo_exists():
            self.top_dishes_win.focus_force()
            return
        self.top_dishes_win = TopDishesDialog(
            self,
            theme_mode=self.theme_mode,
            dishes=self.current_top_dishes,
            total_food_items=self.current_total_food_items
        )

    # =========================================================================
    # DASHBOARD TAB
    # =========================================================================
    def _build_dashboard_tab(self):
        t = self._t()
        parent = self.frames["dashboard"]
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        # Filters
        top_bar = ctk.CTkFrame(parent, fg_color=t["card"])
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        self.dash_location_cb = ctk.CTkComboBox(top_bar, command=self._on_dash_location_change)
        self.dash_location_cb.pack(side="left", padx=10, pady=10)
        self.dash_brand_cb = ctk.CTkComboBox(top_bar, values=["All Brands Combined"])
        self.dash_brand_cb.pack(side="left", padx=10, pady=10)

        self.dash_start_entry = ctk.CTkEntry(top_bar, width=105)
        self.dash_start_entry.insert(0, (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d"))
        self.dash_start_entry.pack(side="left", padx=5, pady=10)

        self.dash_end_entry = ctk.CTkEntry(top_bar, width=105)
        self.dash_end_entry.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self.dash_end_entry.pack(side="left", padx=5, pady=10)

        ctk.CTkButton(top_bar, text="Apply", command=self.refresh_dashboard).pack(side="left", padx=10)
        ctk.CTkButton(top_bar, text="Export PDF", height=30, command=self.export_pdf_report, fg_color="transparent", hover_color=t["ghost_hover"], border_width=1, border_color=t["ghost_border"], text_color=t["text"]).pack(side="right", padx=(4, 12), pady=10)
        ctk.CTkButton(top_bar, text="Export .xlsx", height=30, command=self.export_excel_report, fg_color="transparent", hover_color=t["ghost_hover"], border_width=1, border_color=t["ghost_border"], text_color=t["text"]).pack(side="right", padx=4, pady=10)

        # Content
        self.dash_scroll = ctk.CTkScrollableFrame(parent, fg_color="transparent")
        self.dash_scroll.grid(row=1, column=0, sticky="nsew")
        self.dash_scroll.grid_columnconfigure(0, weight=1)

        # KPI Grid (4 columns x 2 rows = 8 symmetric cards)
        kpi_grid = ctk.CTkFrame(self.dash_scroll, fg_color="transparent")
        kpi_grid.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        for c in range(4):
            kpi_grid.grid_columnconfigure(c, weight=1, uniform="kpi8")

        self.kpi_cards = {}
        kpi_specs = [
            ("revenue", "Total Revenue & AOV"),
            ("volume", "Total Orders & Kitchen Items"),
            ("lost", "Rejected Orders / Lost Rev"),
            ("top_dishes", "Top Selling Dishes (Click for Top 10)"),
            ("busiest", "Busiest & Quietest Dates"),
            ("weekday", "Best Performing Weekday"),
            ("worst_weekday", "Quietest Weekday"),
            ("rating", "Customer Rating & Rush Impact")
        ]

        for idx, (key, title) in enumerate(kpi_specs):
            r, c = divmod(idx, 4)
            card = ctk.CTkFrame(kpi_grid, fg_color=t["card"], corner_radius=8)
            card.grid(row=r, column=c, padx=6, pady=6, sticky="nsew")
            lbl_title = ctk.CTkLabel(card, text=title, font=self.font_small, text_color=t["muted"])
            lbl_title.pack(anchor="w", padx=12, pady=(10, 0))
            lbl_main = ctk.CTkLabel(card, text="—", font=self.font_kpi, text_color=t["text"])
            lbl_sub = ctk.CTkLabel(card, text="—", font=self.font_small, text_color=t["muted"])
            lbl_main.pack(anchor="w", padx=12, pady=(0, 0))
            lbl_sub.pack(anchor="w", padx=12, pady=(0, 10))
            self.kpi_cards[key] = (lbl_main, lbl_sub)

            if key == "top_dishes":
                self.top_dishes_card_widget = card
                self._bind_click_recursive(card, self._open_top_dishes_modal)

        # Chart Grid (2x2)
        charts_grid = ctk.CTkFrame(self.dash_scroll, fg_color="transparent")
        charts_grid.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        charts_grid.grid_columnconfigure(0, weight=1, uniform="chart2x2")
        charts_grid.grid_columnconfigure(1, weight=1, uniform="chart2x2")
        self.chart_boxes = {key: ctk.CTkFrame(charts_grid, fg_color=t["card"], height=300) for key in ("trend", "weekday", "share", "shift")}
        self.chart_boxes["trend"].grid(row=0, column=0, padx=6, pady=6, sticky="nsew")
        self.chart_boxes["weekday"].grid(row=0, column=1, padx=6, pady=6, sticky="nsew")
        self.chart_boxes["share"].grid(row=1, column=0, padx=6, pady=6, sticky="nsew")
        self.chart_boxes["shift"].grid(row=1, column=1, padx=6, pady=6, sticky="nsew")

        # Heatmap
        hm_card = ctk.CTkFrame(self.dash_scroll, fg_color=t["card"])
        hm_card.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        hm_card.grid_columnconfigure(0, weight=1)

        hm_header = ctk.CTkFrame(hm_card, fg_color="transparent")
        hm_header.grid(row=0, column=0, sticky="ew", padx=16, pady=10)
        ctk.CTkLabel(hm_header, text="30-Minute HR Staffing Heatmap", font=self.font_section).pack(side="left")
        self.hm_metric_seg = ctk.CTkSegmentedButton(hm_header, values=["Orders", "Items"], command=lambda _: self._render_integrated_heatmap())
        self.hm_metric_seg.set("Orders")
        self.hm_metric_seg.pack(side="right")
        self.hm_grid_container = ctk.CTkFrame(hm_card, fg_color="transparent")
        self.hm_grid_container.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 16))

    def invalidate_analytics_cache(self):
        self._analytics_cache.clear()
        self._last_dashboard_key = None

    def _get_cached_analytics(self, start_d, end_d, loc_id, brand_id) -> GastroAnalytics:
        key = (start_d, end_d, loc_id, brand_id)
        engine = self._analytics_cache.get(key)
        if engine is None:
            engine = GastroAnalytics(start_d, end_d, loc_id, brand_id)
            if len(self._analytics_cache) >= 16:
                oldest_key = next(iter(self._analytics_cache))
                del self._analytics_cache[oldest_key]
            self._analytics_cache[key] = engine
        return engine

    @safe_action
    def refresh_dashboard(self, force: bool = False):
        start_d, end_d = self.dash_start_entry.get().strip(), self.dash_end_entry.get().strip()
        if not validate_date(start_d) or not validate_date(end_d) or start_d > end_d:
            messagebox.showwarning("Invalid Date", "Check YYYY-MM-DD format and ensure Start <= End.")
            return

        loc_name = self.dash_location_cb.get()
        if loc_name != "All Locations (All Active)" and loc_name not in self.location_map:
            messagebox.showwarning("Unknown Location", f"Location '{loc_name}' is not available.")
            return
        loc_id, brand_id = self._get_selected_ids(loc_name, self.dash_brand_cb.get())

        render_key = (start_d, end_d, loc_id, brand_id, self.theme_mode)
        if (
            not force
            and self._last_dashboard_key == render_key
            and getattr(self, "active_engine", None) is not None
            and self.active_figures
        ):
            return
        self.active_engine = self._get_cached_analytics(start_d, end_d, loc_id, brand_id)
        kpis = self.active_engine.get_kpis()

        self.kpi_cards["revenue"][0].configure(text=f"€{kpis['Total Food Sales (€)']:,.2f}")
        self.kpi_cards["revenue"][1].configure(text=f"Avg Order: €{kpis['Average Order Value (€)']:,.2f}")
        self.kpi_cards["volume"][0].configure(text=f"{kpis['Total Orders']:,} orders")
        self.kpi_cards["volume"][1].configure(text=f"{kpis['Total Food Items']:,} items")
        self.kpi_cards["lost"][0].configure(text=f"{kpis['Rejected Orders']} (€{kpis['Lost Revenue']:,.2f})")
        self.kpi_cards["lost"][1].configure(text=f"Peak: {kpis['Peak Rejection Hour']}")

        self.current_top_dishes = self.active_engine.get_top_dishes_list(10)
        self.current_total_food_items = int(kpis.get('Total Food Items', 0))
        if self.current_top_dishes:
            top1_name, top1_qty = self.current_top_dishes[0]
            sub_items = [f"#{i+2} {n[:14]} ({q})" for i, (n, q) in enumerate(self.current_top_dishes[1:3])]
            self.kpi_cards["top_dishes"][0].configure(text=f"#1 {top1_name[:20]} ({top1_qty})")
            self.kpi_cards["top_dishes"][1].configure(text=" | ".join(sub_items) if sub_items else "Click to view Top 10")
        else:
            self.kpi_cards["top_dishes"][0].configure(text="No dish names")
            self.kpi_cards["top_dishes"][1].configure(text="Click for details")

        self.kpi_cards["busiest"][0].configure(text=_format_kpi_date_weekday(kpis['Busiest Date(s)']))
        self.kpi_cards["busiest"][1].configure(text=f"Quietest: {_format_kpi_date_weekday(kpis['Quietest Trading Date(s)'])}")
        self.kpi_cards["weekday"][0].configure(text=kpis['Best Weekday (Avg)'])
        self.kpi_cards["weekday"][1].configure(text="Highest average volume")
        self.kpi_cards["worst_weekday"][0].configure(text=kpis['Worst Weekday (Avg)'])
        self.kpi_cards["worst_weekday"][1].configure(text="Lowest average volume")

        self.kpi_cards["rating"][0].configure(text=f"{kpis['Overall Average Rating']} / 5.0")
        self.kpi_cards["rating"][1].configure(text=f"Rush {kpis['Rating - Peak Rush']} vs Off {kpis['Rating - Off-Peak']}")

        self._render_charts(self.active_engine)
        self._render_integrated_heatmap()
        self._last_dashboard_key = (start_d, end_d, loc_id, brand_id, self.theme_mode)

    def _render_charts(self, engine: GastroAnalytics):
        self._close_active_figures()
        t = self._t()

        def clear(c):
            for w in c.winfo_children():
                w.destroy()

        def style_ax(ax, title: str, ylabel: str = ""):
            ax.set_facecolor(t["card"])
            ax.set_title(title, loc="left", fontsize=10.5, fontweight="bold", color=t["text"], pad=8)
            if ylabel:
                ax.set_ylabel(ylabel, fontsize=8.5, color=t["muted"])
            ax.spines["top"].set_visible(False)
            ax.spines["right"].set_visible(False)
            ax.spines["left"].set_color(t["border"])
            ax.spines["bottom"].set_color(t["border"])
            ax.tick_params(colors=t["muted"], labelsize=8)
            ax.grid(True, axis="y", color=t["grid"], alpha=0.45, linewidth=0.7)

        def make_fig(c):
            clear(c)
            fig, ax = plt.subplots(figsize=(5.4, 2.85), dpi=100)
            fig.patch.set_facecolor(t["card"])
            self.active_figures.append(fig)
            return fig, ax

        # 1. Top-left: Daily Orders Trend by Brand
        fig1, ax1 = make_fig(self.chart_boxes["trend"])
        style_ax(ax1, "Daily Orders Trend", ylabel="Orders")
        daily_df = engine.get_chart_data_daily()
        if not daily_df.empty:
            x_idx = np.arange(len(daily_df.index))
            for idx, col in enumerate(daily_df.columns):
                ax1.plot(
                    x_idx,
                    daily_df[col].values,
                    label=col,
                    color=CHART_COLORS[idx % len(CHART_COLORS)],
                    marker="o",
                    markersize=3.5,
                    linewidth=1.6
                )
            ax1.xaxis.set_major_locator(MaxNLocator(nbins=7, integer=True))
            ticks = [int(tk_val) for tk_val in ax1.get_xticks() if 0 <= int(tk_val) < len(daily_df.index)]
            ax1.set_xticks(ticks)
            ax1.set_xticklabels([daily_df.index[i] for i in ticks], rotation=30, ha="right")
            leg = ax1.legend(frameon=False, fontsize=8, loc="upper left")
            for text in leg.get_texts():
                text.set_color(t["text"])
        else:
            ax1.text(0.5, 0.5, "No Daily Order Data", ha="center", va="center", color=t["muted"], transform=ax1.transAxes)
        fig1.tight_layout()
        FigureCanvasTkAgg(fig1, self.chart_boxes["trend"]).get_tk_widget().pack(fill="both", expand=True)

        # 2. Top-right: Average Orders & Items by Weekday
        fig2, ax2 = make_fig(self.chart_boxes["weekday"])
        style_ax(ax2, "Average Orders & Items by Weekday", ylabel="Avg Count / Open Day")
        wd_df = engine.get_chart_data_weekday()
        if not wd_df.empty:
            x = np.arange(len(wd_df["day"]))
            w = 0.36
            ax2.bar(x - w / 2, wd_df["avg_orders"], width=w, color="#334155", label="Avg Orders")
            ax2.bar(x + w / 2, wd_df["avg_items"], width=w, color="#0D9488", label="Avg Items")
            ax2.set_xticks(x)
            ax2.set_xticklabels(wd_df["day"])
            leg = ax2.legend(frameon=False, fontsize=8, loc="upper left")
            for text in leg.get_texts():
                text.set_color(t["text"])
        else:
            ax2.text(0.5, 0.5, "No Weekday Data", ha="center", va="center", color=t["muted"], transform=ax2.transAxes)
        fig2.tight_layout()
        FigureCanvasTkAgg(fig2, self.chart_boxes["weekday"]).get_tk_widget().pack(fill="both", expand=True)

        # 3. Bottom-left: Platform Share (Donut Chart with Revenue € & %)
        fig3, ax3 = make_fig(self.chart_boxes["share"])
        ax3.set_facecolor(t["card"])
        ax3.set_title("Platform Revenue Share (€ & %)", loc="left", fontsize=10.5, fontweight="bold", color=t["text"], pad=8)
        plat_df = engine.get_chart_data_platform()
        if not plat_df.empty and plat_df["revenue"].sum() > 0:
            colors = [CHART_COLORS[i % len(CHART_COLORS)] for i in range(len(plat_df))]
            wedges, _ = ax3.pie(
                plat_df["revenue"],
                colors=colors,
                startangle=90,
                wedgeprops=dict(width=0.42, edgecolor=t["card"], linewidth=2)
            )
            ax3.axis("equal")
            total_rev = plat_df["revenue"].sum()
            ax3.text(0, 0, f"€{total_rev:,.0f}\nTotal", ha="center", va="center", fontsize=9, fontweight="bold", color=t["text"])
            legend_labels = [
                f"{row['platform']}: €{row['revenue']:,.0f} ({row['pct']:.1f}%)"
                for _, row in plat_df.iterrows()
            ]
            leg = ax3.legend(wedges, legend_labels, frameon=False, fontsize=8, loc="center left", bbox_to_anchor=(0.82, 0.5))
            for text in leg.get_texts():
                text.set_color(t["text"])
        else:
            ax3.axis("off")
            ax3.text(0.5, 0.5, "No Platform Revenue Data", ha="center", va="center", color=t["muted"], transform=ax3.transAxes)
        fig3.tight_layout()
        FigureCanvasTkAgg(fig3, self.chart_boxes["share"]).get_tk_widget().pack(fill="both", expand=True)

        # 4. Bottom-right: Orders & Items by Meal Shift
        fig4, ax4 = make_fig(self.chart_boxes["shift"])
        style_ax(ax4, "Orders & Items by Meal Shift", ylabel="Count")
        shift_df = engine.get_chart_data_shifts()
        if not shift_df.empty and shift_df["orders"].sum() > 0:
            x = np.arange(len(shift_df["shift"]))
            w = 0.36
            ax4.bar(x - w / 2, shift_df["orders"], width=w, color="#4F46E5", label="Orders")
            ax4.bar(x + w / 2, shift_df["items"], width=w, color="#D97706", label="Food Items")
            ax4.set_xticks(x)
            ax4.set_xticklabels(shift_df["shift"], rotation=20, ha="right")
            leg = ax4.legend(frameon=False, fontsize=8, loc="upper left")
            for text in leg.get_texts():
                text.set_color(t["text"])
        else:
            ax4.text(0.5, 0.5, "No Meal Shift Data", ha="center", va="center", color=t["muted"], transform=ax4.transAxes)
        fig4.tight_layout()
        FigureCanvasTkAgg(fig4, self.chart_boxes["shift"]).get_tk_widget().pack(fill="both", expand=True)

    def _render_integrated_heatmap(self):
        t = self._t()
        if not hasattr(self, "active_engine") or not self.active_engine:
            for w in self.hm_grid_container.winfo_children():
                w.destroy()
            self._hm_slot_labels = []
            self._hm_cell_widgets = {}
            self._hm_headers_built = False
            return

        metric = self.hm_metric_seg.get()
        matrix = self.active_engine.get_hr_heatmap(metric=metric)
        active_slots = [idx for idx in matrix.index if ("08:00" <= idx <= "23:30") or (matrix.loc[idx].sum() > 0)]
        matrix = matrix.loc[active_slots]
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

        if not getattr(self, "_hm_headers_built", False) or not self.hm_grid_container.winfo_children():
            for w in self.hm_grid_container.winfo_children():
                w.destroy()
            for c_idx in range(8):
                self.hm_grid_container.grid_columnconfigure(c_idx, weight=1, uniform="hm")
            ctk.CTkLabel(self.hm_grid_container, text="Slot", text_color=t["muted"]).grid(row=0, column=0)
            for i, d in enumerate(days, 1):
                ctk.CTkLabel(self.hm_grid_container, text=d[:3]).grid(row=0, column=i)
            self._hm_slot_labels = []
            self._hm_cell_widgets = {}
            self._hm_headers_built = True

        target_rows = len(matrix.index)
        if len(self._hm_slot_labels) != target_rows:
            for lbl in self._hm_slot_labels:
                lbl.destroy()
            for row_cells in self._hm_cell_widgets.values():
                for cell in row_cells:
                    cell.destroy()
            self._hm_slot_labels = []
            self._hm_cell_widgets = {}

            for r_idx in range(1, target_rows + 1):
                slot_lbl = ctk.CTkLabel(self.hm_grid_container, text="", font=self.font_small)
                slot_lbl.grid(row=r_idx, column=0, pady=2)
                self._hm_slot_labels.append(slot_lbl)

                row_widgets = []
                for c_idx in range(1, len(days) + 1):
                    cell_lbl = ctk.CTkLabel(
                        self.hm_grid_container,
                        text="",
                        corner_radius=5,
                    )
                    cell_lbl.grid(row=r_idx, column=c_idx, padx=2, pady=2, sticky="nsew")
                    row_widgets.append(cell_lbl)
                self._hm_cell_widgets[r_idx] = row_widgets

        max_v = float(matrix.values.max()) if matrix.size > 0 and float(matrix.values.max()) > 0 else 1.0
        for r_idx, slot in enumerate(matrix.index, 1):
            h, m = map(int, slot.split(":"))
            end_h, end_m = (h, 30) if m == 0 else ((h + 1) % 24, 0)
            slot_label = f"{slot}-{str(end_h).zfill(2)}:{str(end_m).zfill(2)}"
            self._hm_slot_labels[r_idx - 1].configure(text=slot_label)

            row_widgets = self._hm_cell_widgets[r_idx]
            for c_idx, day in enumerate(days, 1):
                val = float(matrix.at[slot, day])
                cooks = self.active_engine.apply_staffing_rules(val, metric)["cooks"]
                if val == 0:
                    bg_hex, txt_hex = "#F3F4F6", "#6B7280"
                elif val / max_v > 0.6:
                    bg_hex, txt_hex = "#FECACA", "#18181B"
                else:
                    bg_hex, txt_hex = "#FEF9C3", "#18181B"
                row_widgets[c_idx - 1].configure(
                    text=f"{val:.1f}|{cooks}C",
                    fg_color=bg_hex,
                    text_color=txt_hex,
                )

    @safe_action
    def export_excel_report(self):
        start_d, end_d = self.dash_start_entry.get().strip(), self.dash_end_entry.get().strip()
        if not validate_date(start_d) or not validate_date(end_d) or start_d > end_d:
            messagebox.showwarning("Invalid Date", "Check YYYY-MM-DD format and ensure Start <= End before exporting.")
            return

        loc_name = self.dash_location_cb.get()
        if loc_name != "All Locations (All Active)" and loc_name not in self.location_map:
            return
        loc_id, brand_id = self._get_selected_ids(loc_name, self.dash_brand_cb.get())

        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialfile=f"GastroAnalytics_{datetime.now().strftime('%Y%m%d')}.xlsx"
        )
        if not path:
            return

        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            GastroAnalytics(start_d, end_d, loc_id, brand_id).export_to_excel(path)
            messagebox.showinfo("Export Complete", f"Excel report saved to:\n{path}")
        finally:
            self.configure(cursor="")

    @safe_action
    def export_pdf_report(self):
        start_d, end_d = self.dash_start_entry.get().strip(), self.dash_end_entry.get().strip()
        if not validate_date(start_d) or not validate_date(end_d) or start_d > end_d:
            messagebox.showwarning("Invalid Date", "Check YYYY-MM-DD format and ensure Start <= End before exporting.")
            return

        loc_name = self.dash_location_cb.get()
        if loc_name != "All Locations (All Active)" and loc_name not in self.location_map:
            return
        loc_id, brand_id = self._get_selected_ids(loc_name, self.dash_brand_cb.get())

        path = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
            initialfile=f"GastroAnalytics_{datetime.now().strftime('%Y%m%d')}.pdf"
        )
        if not path:
            return

        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            GastroAnalytics(start_d, end_d, loc_id, brand_id).export_to_pdf(path, self.dash_location_cb.get(), self.dash_brand_cb.get())
            messagebox.showinfo("Export Complete", f"PDF report saved to:\n{path}")
        finally:
            self.configure(cursor="")

    # =========================================================================
    # DAILY BREAKDOWN TAB
    # =========================================================================
    def _build_daily_breakdown_tab(self):
        t = self._t()
        parent = self.frames["daily"]
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        # Row 0: Filter Bar
        filter_bar = ctk.CTkFrame(parent, fg_color=t["card"])
        filter_bar.grid(row=0, column=0, sticky="ew", pady=(0, 12))

        self.daily_location_cb = ctk.CTkComboBox(filter_bar, command=self._on_daily_location_change, width=165)
        self.daily_location_cb.pack(side="left", padx=(12, 6), pady=10)

        self.daily_brand_cb = ctk.CTkComboBox(filter_bar, values=["All Brands Combined"], width=185)
        self.daily_brand_cb.pack(side="left", padx=6, pady=10)

        self.daily_scope_seg = ctk.CTkSegmentedButton(
            filter_bar,
            values=["All Days in Database", "Selected Date Range"],
            command=self._on_daily_scope_change
        )
        self.daily_scope_seg.set("All Days in Database")
        self.daily_scope_seg.pack(side="left", padx=8, pady=10)

        self.daily_start_entry = ctk.CTkEntry(filter_bar, width=102)
        self.daily_start_entry.pack(side="left", padx=4, pady=10)

        self.daily_end_entry = ctk.CTkEntry(filter_bar, width=102)
        self.daily_end_entry.pack(side="left", padx=4, pady=10)

        ctk.CTkButton(filter_bar, text="Apply", width=80, command=self.refresh_daily_breakdown).pack(side="left", padx=8, pady=10)

        ctk.CTkButton(
            filter_bar,
            text="Export .xlsx / .csv",
            height=30,
            command=self._export_daily_breakdown_file,
            fg_color="transparent",
            hover_color=t["ghost_hover"],
            border_width=1,
            border_color=t["ghost_border"],
            text_color=t["text"],
        ).pack(side="right", padx=12, pady=10)

        # Row 1: Summary Strip
        summary_bar = ctk.CTkFrame(parent, fg_color=t["card"])
        summary_bar.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        self.daily_summary_lbl = ctk.CTkLabel(
            summary_bar,
            text="Visible Rows: 0  |  Unique Dates: 0  |  Completed Orders: 0  |  Food Items: 0  |  Total Revenue: €0.00  |  Lost Revenue: €0.00",
            font=self.font_bold,
            text_color=t["text"]
        )
        self.daily_summary_lbl.pack(side="left", padx=16, pady=10)

        # Row 2: Sortable Treeview Table with Vertical & Horizontal Scrollbars
        tbl_card = ctk.CTkFrame(parent, fg_color=t["card"])
        tbl_card.grid(row=2, column=0, sticky="nsew")
        tbl_card.grid_columnconfigure(0, weight=1)
        tbl_card.grid_rowconfigure(0, weight=1)

        cols = (
            "Date", "Weekday", "Restaurant / Brand", "Status",
            "Completed Orders", "Food Items", "Revenue (€)",
            "Avg Order Value (€)", "Avg Rating", "Rejected Orders", "Lost Revenue (€)"
        )
        self.daily_tree = ttk.Treeview(tbl_card, columns=cols, show="headings", selectmode="extended")

        col_widths = {
            "Date": 105,
            "Weekday": 100,
            "Restaurant / Brand": 175,
            "Status": 95,
            "Completed Orders": 125,
            "Food Items": 95,
            "Revenue (€)": 110,
            "Avg Order Value (€)": 135,
            "Avg Rating": 90,
            "Rejected Orders": 115,
            "Lost Revenue (€)": 120
        }
        numeric_cols = {
            "Completed Orders", "Food Items", "Revenue (€)",
            "Avg Order Value (€)", "Avg Rating", "Rejected Orders", "Lost Revenue (€)"
        }

        for c in cols:
            self.daily_sort_reverse[c] = False
            self.daily_tree.heading(c, text=c, command=lambda col_name=c: self._sort_daily_tree(col_name))
            anchor = "e" if c in numeric_cols else ("center" if c in ("Date", "Status") else "w")
            self.daily_tree.column(c, width=col_widths[c], minwidth=80, anchor=anchor)

        if self.theme_mode == "Dark":
            self.daily_tree.tag_configure("closed_day", background="#27272A", foreground="#9CA3AF")
            self.daily_tree.tag_configure("disrupted_day", background="#451A03", foreground="#FDE68A")
            self.daily_tree.tag_configure("has_rejections", background="#450A0A", foreground="#FECACA")
        else:
            self.daily_tree.tag_configure("closed_day", background="#F3F4F6", foreground="#6B7280")
            self.daily_tree.tag_configure("disrupted_day", background="#FEF3C7", foreground="#92400E")
            self.daily_tree.tag_configure("has_rejections", background="#FEF2F2", foreground="#991B1B")

        vsb = ttk.Scrollbar(tbl_card, orient="vertical", command=self.daily_tree.yview)
        hsb = ttk.Scrollbar(tbl_card, orient="horizontal", command=self.daily_tree.xview)
        self.daily_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        self.daily_tree.grid(row=0, column=0, sticky="nsew", padx=(12, 0), pady=(12, 0))
        vsb.grid(row=0, column=1, sticky="ns", padx=(0, 8), pady=(12, 0))
        hsb.grid(row=1, column=0, sticky="ew", padx=(12, 0), pady=(0, 8))







    # =========================================================================
    # DATA & CALENDAR TAB
    # =========================================================================


    # =========================================================================
    # SETTINGS TAB (ZERO-MAINTENANCE & LAYMAN-FRIENDLY)
    # =========================================================================




















    # --- Staffing Rules CRUD ---



    # --- Meal Shifts CRUD ---





    # --- Exclusions CRUD ---








    def _on_daily_location_change(self, loc_name: str):
        brands = self._get_brands_for_location(loc_name)
        all_label = "All Active Restaurants" if loc_name == "All Locations (All Active)" else "All Brands Combined"
        self.daily_brand_cb.configure(values=[all_label] + [b['display_name'] for b in brands])
        self.daily_brand_cb.set(all_label)
        if loc_name == "All Locations (All Active)" or loc_name in self.location_map:
            loc_id = self.location_map.get(loc_name)
            min_d, max_d = get_location_date_bounds(loc_id)
            self.daily_start_entry.delete(0, "end")
            self.daily_start_entry.insert(0, min_d)
            self.daily_end_entry.delete(0, "end")
            self.daily_end_entry.insert(0, max_d)

    def _on_daily_scope_change(self, choice: str):
        if choice == "All Days in Database":
            loc_name = self.daily_location_cb.get()
            if loc_name == "All Locations (All Active)" or loc_name in self.location_map:
                loc_id = self.location_map.get(loc_name)
                min_d, max_d = get_location_date_bounds(loc_id)
                self.daily_start_entry.delete(0, "end")
                self.daily_start_entry.insert(0, min_d)
                self.daily_end_entry.delete(0, "end")
                self.daily_end_entry.insert(0, max_d)
        self.refresh_daily_breakdown()

    @safe_action
    def refresh_daily_breakdown(self, force: bool = False):
        loc_name = self.daily_location_cb.get()
        if loc_name != "All Locations (All Active)" and loc_name not in self.location_map:
            messagebox.showwarning("Unknown Location", f"Location '{loc_name}' is not available.")
            return
        loc_id, brand_id = self._get_selected_ids(loc_name, self.daily_brand_cb.get())

        all_days_mode = (self.daily_scope_seg.get() == "All Days in Database")
        if all_days_mode:
            min_d, max_d = get_location_date_bounds(loc_id)
            if self.daily_start_entry.get().strip() != str(min_d):
                self.daily_start_entry.delete(0, "end")
                self.daily_start_entry.insert(0, min_d)
            if self.daily_end_entry.get().strip() != str(max_d):
                self.daily_end_entry.delete(0, "end")
                self.daily_end_entry.insert(0, max_d)
            start_d, end_d = min_d, max_d
        else:
            start_d = self.daily_start_entry.get().strip()
            end_d = self.daily_end_entry.get().strip()
            if not validate_date(start_d) or not validate_date(end_d) or start_d > end_d:
                messagebox.showwarning("Invalid Date", "Check YYYY-MM-DD format and ensure Start <= End.")
                return

        daily_key = (start_d, end_d, loc_id, brand_id, all_days_mode, self.theme_mode)
        if (
            not force
            and getattr(self, "_last_daily_key", None) == daily_key
            and hasattr(self, "current_daily_df")
            and not self.current_daily_df.empty
        ):
            return

        engine = self._get_cached_analytics(start_d, end_d, loc_id, brand_id)
        df = engine.get_daily_breakdown(all_days_in_db=all_days_mode)
        self.current_daily_df = df

        self._render_daily_tree_rows(df)
        self._last_daily_key = daily_key

    def _render_daily_tree_rows(self, df: pd.DataFrame):
        self.daily_tree.delete(*self.daily_tree.get_children())
        if df.empty:
            self.daily_summary_lbl.configure(
                text="Visible Rows: 0  |  Unique Dates: 0  |  Completed Orders: 0  |  Food Items: 0  |  Total Revenue: €0.00  |  Lost Revenue: €0.00"
            )
            return

        for _, row in df.iterrows():
            status_val = str(row["Status"])
            rej_val = int(row["Rejected Orders"])
            tag = ()
            if status_val == "Closed":
                tag = ("closed_day",)
            elif status_val == "Disrupted":
                tag = ("disrupted_day",)
            elif rej_val > 0:
                tag = ("has_rejections",)

            vals = (
                row["Date"],
                row["Weekday"],
                row["Restaurant / Brand"],
                status_val,
                f"{int(row['Completed Orders']):,}",
                f"{int(row['Food Items']):,}",
                f"€{float(row['Revenue (€)']):,.2f}",
                f"€{float(row['Avg Order Value (€)']):,.2f}",
                row["Avg Rating"],
                f"{rej_val:,}",
                f"€{float(row['Lost Revenue (€)']):,.2f}"
            )
            self.daily_tree.insert("", "end", values=vals, tags=tag)

        unique_dates = df["Date"].nunique()
        total_orders = int(df["Completed Orders"].sum())
        total_items = int(df["Food Items"].sum())
        total_rev = float(df["Revenue (€)"].sum())
        total_lost = float(df["Lost Revenue (€)"].sum())

        self.daily_summary_lbl.configure(
            text=(
                f"Visible Rows: {len(df):,}  |  Unique Dates: {unique_dates:,}  |  "
                f"Completed Orders: {total_orders:,}  |  Food Items: {total_items:,}  |  "
                f"Total Revenue: €{total_rev:,.2f}  |  Lost Revenue: €{total_lost:,.2f}"
            )
        )

    def _sort_daily_tree(self, col_name: str):
        if self.current_daily_df.empty or col_name not in self.current_daily_df.columns:
            return
        reverse = not self.daily_sort_reverse.get(col_name, False)
        self.daily_sort_reverse[col_name] = reverse

        df = self.current_daily_df.copy()
        if col_name == "Avg Rating":
            df["_sort_key"] = pd.to_numeric(df["Avg Rating"], errors="coerce").fillna(-1.0)
            df = df.sort_values(by="_sort_key", ascending=not reverse).drop(columns=["_sort_key"])
        else:
            df = df.sort_values(by=col_name, ascending=not reverse)

        self.current_daily_df = df
        self._render_daily_tree_rows(df)

    @safe_action
    def _export_daily_breakdown_file(self):
        if self.current_daily_df.empty:
            messagebox.showwarning("No Data", "There is no daily breakdown data to export.")
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx"), ("CSV File", "*.csv")],
            initialfile=f"Daily_Breakdown_{datetime.now().strftime('%Y%m%d')}.xlsx"
        )
        if not path:
            return

        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            if path.lower().endswith(".csv"):
                self.current_daily_df.to_csv(path, index=False, encoding="utf-8-sig")
            else:
                with pd.ExcelWriter(path, engine="openpyxl") as writer:
                    self.current_daily_df.to_excel(writer, sheet_name="Daily Breakdown", index=False)
            messagebox.showinfo("Export Complete", f"Daily breakdown saved to:\n{path}")
        finally:
            self.configure(cursor="")

    # =========================================================================
    # DATA & CALENDAR TAB
    # =========================================================================

    def _append_import_log(self, line: str):
        box = getattr(self, "import_log_box", None)
        if box is None:
            return
        try:
            if not box.winfo_exists():
                return
            box.configure(state="normal")
            box.insert("end", line + chr(10))
            box.see("end")
            box.configure(state="disabled")
        except Exception:
            pass

    # =========================================================================
    # SETTINGS TAB (ZERO-MAINTENANCE & LAYMAN-FRIENDLY)
    # =========================================================================
    def _build_settings_tab(self):
        t = self._t()
        parent = self.frames["settings"]
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)
        self.set_tabs = ctk.CTkTabview(parent, fg_color=t["card"])
        self.set_tabs.grid(row=0, column=0, sticky="nsew")
        for tab_name in ["Import", "Restaurants & Brands", "Staffing Rules", "Meal Shifts", "Exclusions"]:
            self.set_tabs.add(tab_name)

        self._build_settings_import_subtab(self.set_tabs.tab("Import"))
        self._build_settings_entities_subtab(self.set_tabs.tab("Restaurants & Brands"))
        self._build_settings_hr_subtab(self.set_tabs.tab("Staffing Rules"))
        self._build_settings_shifts_subtab(self.set_tabs.tab("Meal Shifts"))
        self._build_settings_exclusions_subtab(self.set_tabs.tab("Exclusions"))


    def _build_settings_import_subtab(self, tab):
        t = self._t()
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        top_card = ctk.CTkFrame(tab, fg_color=t["canvas"])
        top_card.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))

        ctk.CTkLabel(
            top_card,
            text="All-in-One Folder Import (Orders CSV/XLSX + Payouts/Invoices PDF/CSV):",
            font=self.font_bold,
            text_color=t["text"],
        ).pack(side="left", padx=(12, 10), pady=12)

        ctk.CTkButton(
            top_card,
            text="Import Folder",
            width=150,
            command=self._import_all_from_folder,
        ).pack(side="left", padx=6, pady=12)

        ctk.CTkButton(
            top_card,
            text="Custom Column Mapper",
            width=165,
            fg_color="transparent",
            border_width=1,
            border_color=t["ghost_border"],
            text_color=t["text"],
            command=lambda: self._open_custom_mapper(None),
        ).pack(side="left", padx=6, pady=12)

        self.import_log_box = ctk.CTkTextbox(tab, state="disabled")
        self.import_log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 12))

    @safe_action
    def _import_all_from_folder(self):
        folder = filedialog.askdirectory(title="Select folder containing orders and payout/invoice files")
        if not folder:
            return
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            res = import_day_folder_recursive(
                folder, auto_create_brands=brand_service.get_auto_create_brands_setting()
            )
        finally:
            self.configure(cursor="")

        p_res = res.get("payouts", {}) if isinstance(res, dict) else {}
        o_res = res.get("orders", {}) if isinstance(res, dict) else {}
        p_imp = int(p_res.get("imported", 0) or 0)
        p_dup = int(p_res.get("skipped_duplicates", p_res.get("duplicates", 0)) or 0)
        p_err = len(p_res.get("errors", []) or [])
        o_imp = int(o_res.get("imported", 0) or 0)
        o_dup = int(o_res.get("duplicates", 0) or 0)
        o_err = len(o_res.get("errors", []) or [])

        log_line = (
            f"[{os.path.basename(folder)}] Orders: +{o_imp} ({o_dup} dup, {o_err} err) | "
            f"Payouts/Invoices: +{p_imp} ({p_dup} dup, {p_err} err)"
        )
        if hasattr(self, "import_log_box"):
            self._append_import_log(log_line)

        self.invalidate_analytics_cache()
        self.mark_tabs_dirty()
        self._populate_global_dropdowns()
        self.refresh_finances_tab(force=True)

        messagebox.showinfo(
            "Folder Import Complete",
            f"Folder: {res.get('folder', folder)}\n"
            f"Orders imported: {o_imp} (duplicates: {o_dup}, errors: {o_err})\n"
            f"Payouts/Invoices imported: {p_imp} (duplicates: {p_dup}, errors: {p_err})",
        )

    def _build_settings_entities_subtab(self, tab):
        t = self._t()
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(2, weight=1)

        # Row 0: Quick create (Location & Brand) + Delete controls
        top_bar = ctk.CTkFrame(tab, fg_color=t["canvas"])
        top_bar.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))

        ctk.CTkLabel(top_bar, text="Location:", font=self.font_bold).pack(side="left", padx=(10, 4), pady=8)
        self.new_loc_entry = ctk.CTkEntry(top_bar, placeholder_text="e.g. Sliema", width=130)
        self.new_loc_entry.pack(side="left", padx=4, pady=8)
        ctk.CTkButton(top_bar, text="+ Loc", width=65, command=self._add_location).pack(side="left", padx=(4, 14), pady=8)

        ctk.CTkLabel(top_bar, text="Brand:", font=self.font_bold).pack(side="left", padx=(4, 4), pady=8)
        self.new_brand_loc_cb = ctk.CTkComboBox(top_bar, width=130)
        self.new_brand_loc_cb.pack(side="left", padx=4, pady=8)
        self.new_brand_name_entry = ctk.CTkEntry(top_bar, placeholder_text="Brand name", width=160)
        self.new_brand_name_entry.pack(side="left", padx=4, pady=8)
        ctk.CTkButton(top_bar, text="+ Brand", width=75, command=self._add_brand).pack(side="left", padx=4, pady=8)

        ctk.CTkButton(
            top_bar,
            text="Delete Brand",
            width=105,
            fg_color=t["danger"],
            hover_color=t["danger_hover"],
            command=self._delete_brand,
        ).pack(side="right", padx=(4, 10), pady=8)

        self.del_loc_cb = ctk.CTkComboBox(top_bar, width=120)
        self.del_loc_cb.pack(side="right", padx=2, pady=8)
        ctk.CTkButton(
            top_bar,
            text="Del Loc",
            width=70,
            fg_color=t["danger"],
            hover_color=t["danger_hover"],
            command=self._delete_location,
        ).pack(side="right", padx=(8, 2), pady=8)

        # Row 1: Aliases + Selected Restaurant Settings (Active/Inactive + Location)
        alias_card = ctk.CTkFrame(tab, fg_color=t["canvas"])
        alias_card.grid(row=1, column=0, sticky="ew", padx=10, pady=5)
        alias_card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(alias_card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))
        ctk.CTkLabel(head, text="Unmapped / Pending Names", font=self.font_bold).pack(side="left")

        self.auto_create_brand_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(
            head,
            text="Auto-create new brands on import",
            variable=self.auto_create_brand_var,
            command=self._toggle_auto_create_brands,
        ).pack(side="left", padx=14)

        ctk.CTkButton(head, text="Re-apply", width=85, command=self._reapply_aliases_action).pack(side="right")

        u_cols = ("Name", "Platform", "Orders", "Orders EUR", "Reports", "Reports EUR", "Status")
        u_wrap = ctk.CTkFrame(alias_card, fg_color="transparent")
        u_wrap.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 6))
        u_wrap.grid_columnconfigure(0, weight=1)
        self.unmapped_tree = ttk.Treeview(u_wrap, columns=u_cols, show="headings", height=3, selectmode="browse")
        for c in u_cols:
            self.unmapped_tree.heading(c, text=c)
            self.unmapped_tree.column(c, width=190 if c == "Name" else 95, anchor="w")
        u_vsb = ttk.Scrollbar(u_wrap, orient="vertical", command=self.unmapped_tree.yview)
        self.unmapped_tree.configure(yscrollcommand=u_vsb.set)
        self.unmapped_tree.grid(row=0, column=0, sticky="ew")
        u_vsb.grid(row=0, column=1, sticky="ns")
        self.unmapped_tree.bind("<<TreeviewSelect>>", self._on_unmapped_tree_select)

        act_row = ctk.CTkFrame(alias_card, fg_color="transparent")
        act_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 6))
        self.alias_raw_name_entry = ctk.CTkEntry(act_row, placeholder_text="Alias name", width=180)
        self.alias_raw_name_entry.pack(side="left", padx=(0, 6))
        self.alias_target_brand_cb = ctk.CTkComboBox(act_row, width=210)
        self.alias_target_brand_cb.pack(side="left", padx=4)
        ctk.CTkButton(act_row, text="Assign", width=80, command=self._assign_selected_alias).pack(side="left", padx=4)
        ctk.CTkButton(act_row, text="Later", width=65, command=self._defer_selected_unmapped).pack(side="left", padx=4)

        self.new_alias_brand_entry = ctk.CTkEntry(act_row, placeholder_text="New brand...", width=130)
        self.new_alias_brand_entry.pack(side="left", padx=(12, 4))
        ctk.CTkButton(act_row, text="+ Create & Map", width=110, command=self._create_brand_for_alias).pack(side="left", padx=4)

        rem_row = ctk.CTkFrame(alias_card, fg_color="transparent")
        rem_row.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 6))
        ctk.CTkLabel(rem_row, text="Active Alias:", font=self.font_bold).pack(side="left", padx=(0, 6))
        self.active_alias_cb = ctk.CTkComboBox(rem_row, width=380)
        self.active_alias_cb.pack(side="left", padx=4)
        ctk.CTkButton(
            rem_row,
            text="Remove Alias",
            width=105,
            fg_color=t["danger"],
            hover_color=t["danger_hover"],
            command=self._remove_selected_alias,
        ).pack(side="left", padx=6)

        # Row 3b: Merge Two Restaurants Into One (e.g. Wolt + Bolt same venue)
        merge_row = ctk.CTkFrame(alias_card, fg_color=t["card"], corner_radius=6)
        merge_row.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 6))

        ctk.CTkLabel(merge_row, text="Merge Restaurant:", font=self.font_bold).pack(side="left", padx=(10, 4), pady=6)
        self.merge_source_cb = ctk.CTkComboBox(merge_row, width=210)
        self.merge_source_cb.pack(side="left", padx=4, pady=6)

        ctk.CTkLabel(merge_row, text="Into Target:", font=self.font_bold).pack(side="left", padx=(8, 4), pady=6)
        self.merge_target_cb = ctk.CTkComboBox(merge_row, width=210)
        self.merge_target_cb.pack(side="left", padx=4, pady=6)

        ctk.CTkButton(
            merge_row,
            text="Merge & Save Alias",
            width=145,
            command=self._merge_selected_restaurants,
        ).pack(side="right", padx=(4, 10), pady=6)

        # Row 4: Per-Restaurant Settings (Name, Location link, Active/Inactive status)
        brand_cfg_row = ctk.CTkFrame(alias_card, fg_color=t["card"], corner_radius=6)
        brand_cfg_row.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 8))

        ctk.CTkLabel(brand_cfg_row, text="Restaurant Settings:", font=self.font_bold).pack(side="left", padx=(10, 6), pady=6)
        self.edit_brand_id_var = tk.StringVar(value="")

        self.edit_brand_name_entry = ctk.CTkEntry(brand_cfg_row, placeholder_text="Restaurant name", width=165)
        self.edit_brand_name_entry.pack(side="left", padx=4, pady=6)

        ctk.CTkLabel(brand_cfg_row, text="Location:").pack(side="left", padx=(8, 2), pady=6)
        self.edit_brand_loc_cb = ctk.CTkComboBox(brand_cfg_row, width=140)
        self.edit_brand_loc_cb.pack(side="left", padx=4, pady=6)

        ctk.CTkLabel(brand_cfg_row, text="Status:").pack(side="left", padx=(8, 2), pady=6)
        self.edit_brand_status_cb = ctk.CTkComboBox(brand_cfg_row, values=["Active", "Inactive"], width=105)
        self.edit_brand_status_cb.set("Active")
        self.edit_brand_status_cb.pack(side="left", padx=4, pady=6)

        ctk.CTkButton(
            brand_cfg_row,
            text="Save Restaurant",
            width=125,
            command=self._save_selected_brand_settings,
        ).pack(side="right", padx=(4, 10), pady=6)

        ctk.CTkButton(
            brand_cfg_row,
            text="Toggle Active/Inactive",
            width=145,
            fg_color="transparent",
            border_width=1,
            border_color=t["ghost_border"],
            text_color=t["text"],
            command=self._toggle_selected_brand_status,
        ).pack(side="right", padx=4, pady=6)

        # Row 2: Brands list
        b_cols = ("ID", "Location", "Brand", "Status")
        b_wrap = ctk.CTkFrame(tab, fg_color="transparent")
        b_wrap.grid(row=2, column=0, sticky="nsew", padx=10, pady=(4, 10))
        b_wrap.grid_columnconfigure(0, weight=1)
        b_wrap.grid_rowconfigure(0, weight=1)
        self.brands_tree = ttk.Treeview(b_wrap, columns=b_cols, show="headings", selectmode="browse")
        for c in b_cols:
            self.brands_tree.heading(c, text=c)
        b_vsb = ttk.Scrollbar(b_wrap, orient="vertical", command=self.brands_tree.yview)
        self.brands_tree.configure(yscrollcommand=b_vsb.set)
        self.brands_tree.grid(row=0, column=0, sticky="nsew")
        b_vsb.grid(row=0, column=1, sticky="ns")
        self.brands_tree.bind("<<TreeviewSelect>>", self._on_brand_tree_select)
    def _refresh_after_brand_change(self):
        if hasattr(self, "invalidate_analytics_cache"):
            self.invalidate_analytics_cache()
        if hasattr(self, "mark_tabs_dirty"):
            self.mark_tabs_dirty()
        self._last_dashboard_key = None
        if hasattr(self, "_last_daily_key"):
            self._last_daily_key = None
        self._populate_global_dropdowns()
        self.refresh_all_settings()
        for fn_name in ("refresh_dashboard", "refresh_daily_breakdown", "refresh_finances_tab"):
            fn = getattr(self, fn_name, None)
            if callable(fn):
                try:
                    fn(force=True)
                except TypeError:
                    fn()

    @safe_action
    def _merge_selected_restaurants(self):
        src_label = self.merge_source_cb.get().strip() if hasattr(self, "merge_source_cb") else ""
        tgt_label = self.merge_target_cb.get().strip() if hasattr(self, "merge_target_cb") else ""
        src_id = getattr(self, "brand_merge_map", {}).get(src_label)
        tgt_id = getattr(self, "brand_merge_map", {}).get(tgt_label)
        if not src_id or not tgt_id:
            messagebox.showwarning("Missing Selection", "Choose both source and target restaurants.")
            return
        if int(src_id) == int(tgt_id):
            messagebox.showwarning("Same Restaurant", "Choose two different restaurants to merge.")
            return
        if not messagebox.askyesno(
            "Confirm Merge",
            f"Merge '{src_label}' INTO '{tgt_label}'?\n\n"
            "All orders, items, payout reports and costs will move to the target restaurant, "
            "the source name will be saved as an alias for automatic future imports, and the duplicate entry will be removed.",
        ):
            return
        res = brand_service.merge_restaurants_into_target(int(src_id), int(tgt_id))
        self._refresh_after_brand_change()
        messagebox.showinfo(
            "Merged",
            f"Merged '{res['source_name']}' into '{res['target_name']}' "
            f"(moved orders: {res['moved_orders']}, saved aliases: {res['saved_aliases']}).",
        )

    def _on_brand_tree_select(self, _event=None):
        sel = self.brands_tree.selection()
        if not sel:
            return
        vals = self.brands_tree.item(sel[0], "values")
        if not vals:
            return
        self.edit_brand_id_var.set(str(vals[0]))
        self.edit_brand_name_entry.delete(0, "end")
        self.edit_brand_name_entry.insert(0, str(vals[2]))
        loc_vals = self.edit_brand_loc_cb.cget("values") or []
        if str(vals[1]) in loc_vals:
            self.edit_brand_loc_cb.set(str(vals[1]))
        status_val = "Active" if str(vals[3]).strip().lower() == "active" else "Inactive"
        self.edit_brand_status_cb.set(status_val)

    @safe_action
    def _save_selected_brand_settings(self):
        brand_id_str = self.edit_brand_id_var.get().strip()
        if not brand_id_str:
            sel = self.brands_tree.selection()
            if sel:
                brand_id_str = str(self.brands_tree.item(sel[0], "values")[0])
        if not brand_id_str:
            messagebox.showwarning("No Selection", "Select a restaurant/brand from the table first.")
            return

        loc_name = self.edit_brand_loc_cb.get().strip()
        loc_id = self.location_map.get(loc_name)
        if not loc_id:
            messagebox.showwarning("Missing Location", "Select a valid location.")
            return

        new_name = self.edit_brand_name_entry.get().strip()
        status_val = self.edit_brand_status_cb.get().strip() or "Active"
        updated = brand_service.update_brand_settings(
            brand_id=int(brand_id_str),
            name=new_name,
            location_id=int(loc_id),
            status=status_val,
        )
        self._refresh_after_brand_change()
        messagebox.showinfo(
            "Saved",
            f"Updated '{updated['name']}' -> Location: {updated['location_name']} ({updated['status']}).",
        )

    @safe_action
    def _toggle_selected_brand_status(self):
        brand_id_str = self.edit_brand_id_var.get().strip()
        if not brand_id_str:
            sel = self.brands_tree.selection()
            if sel:
                brand_id_str = str(self.brands_tree.item(sel[0], "values")[0])
        if not brand_id_str:
            messagebox.showwarning("No Selection", "Select a restaurant/brand from the table first.")
            return

        current_status = self.edit_brand_status_cb.get().strip()
        next_status = "Inactive" if current_status.lower() == "active" else "Active"
        new_st = brand_service.set_brand_status(int(brand_id_str), next_status)
        self.edit_brand_status_cb.set(new_st)
        self._refresh_after_brand_change()
        messagebox.showinfo("Status Updated", f"Restaurant status changed to {new_st}.")

    def _on_unmapped_tree_select(self, _event=None):
        sel = self.unmapped_tree.selection()
        if not sel:
            return
        vals = self.unmapped_tree.item(sel[0], "values")
        if vals and hasattr(self, "alias_raw_name_entry"):
            self.alias_raw_name_entry.delete(0, "end")
            self.alias_raw_name_entry.insert(0, vals[0])

    @safe_action
    def _toggle_auto_create_brands(self):
        brand_service.set_auto_create_brands_setting(bool(self.auto_create_brand_var.get()))

    @safe_action
    def _reapply_aliases_action(self):
        stats = brand_service.reapply_all_brand_aliases()
        self._refresh_after_brand_change()
        messagebox.showinfo(
            "Re-applied",
            f"Orders: {stats.get('updated_orders', 0)}, "
            f"Reports: {stats.get('updated_reports', 0)}, "
            f"Cost items: {stats.get('updated_cost_items', 0)}."
        )

    @safe_action
    def _assign_selected_alias(self):
        raw_name = self.alias_raw_name_entry.get().strip()
        target_label = self.alias_target_brand_cb.get().strip()
        target_id = getattr(self, "alias_brand_id_map", {}).get(target_label)
        if not raw_name or not target_id:
            messagebox.showwarning("Missing Selection", "Select or enter an alias name and pick a brand.")
            return
        platform = ""
        sel = self.unmapped_tree.selection()
        if sel:
            vals = self.unmapped_tree.item(sel[0], "values")
            if vals and vals[0] == raw_name:
                platform = str(vals[1] or "")
        res = brand_service.assign_alias_to_brand(raw_name, int(target_id), platform=platform, reapply=True)
        self._refresh_after_brand_change()
        messagebox.showinfo("Assigned", f"'{raw_name}' -> '{res['brand_name']}'.")

    @safe_action
    def _create_brand_for_alias(self):
        new_brand = self.new_alias_brand_entry.get().strip()
        raw_name = self.alias_raw_name_entry.get().strip() or new_brand
        if not new_brand:
            messagebox.showwarning("Missing Name", "Enter a new brand name.")
            return
        platform = ""
        sel = self.unmapped_tree.selection()
        if sel:
            vals = self.unmapped_tree.item(sel[0], "values")
            if vals and vals[0] == raw_name:
                platform = str(vals[1] or "")
        res = brand_service.create_brand_and_assign_alias(new_brand, raw_name=raw_name, platform=platform)
        self.new_alias_brand_entry.delete(0, "end")
        self._refresh_after_brand_change()
        messagebox.showinfo("Created", f"Created '{res['brand_name']}' and mapped '{raw_name}'.")

    @safe_action
    def _defer_selected_unmapped(self):
        raw_name = self.alias_raw_name_entry.get().strip()
        if not raw_name:
            messagebox.showwarning("No Selection", "Select a name to assign later.")
            return
        platform = ""
        sel = self.unmapped_tree.selection()
        if sel:
            vals = self.unmapped_tree.item(sel[0], "values")
            if vals and vals[0] == raw_name:
                platform = str(vals[1] or "")
        brand_service.defer_unmapped_brand(raw_name, platform=platform)
        self.refresh_all_settings()

    @safe_action
    def _remove_selected_alias(self):
        label = self.active_alias_cb.get().strip()
        alias_id = getattr(self, "active_alias_id_map", {}).get(label)
        if not alias_id:
            messagebox.showwarning("No Selection", "Select an alias to remove.")
            return
        brand_service.delete_brand_alias(int(alias_id), unmap_matching_records=True)
        self._refresh_after_brand_change()
        messagebox.showinfo("Removed", f"Removed '{label}'.")

    @safe_action
    def _add_location(self):
        name = self.new_loc_entry.get().strip()
        if not name:
            messagebox.showwarning("Missing Name", "Enter a location name.")
            return
        with contextlib.closing(get_connection()) as conn:
            conn.execute("INSERT INTO locations (name) VALUES (?)", (name,))
            conn.commit()
        self.new_loc_entry.delete(0, "end")
        self._populate_global_dropdowns()
        self.refresh_all_settings()

    @safe_action
    def _delete_location(self):
        loc_name = self.del_loc_cb.get().strip()
        loc_id = self.location_map.get(loc_name)
        if not loc_id:
            messagebox.showwarning("No Selection", "Select a location to delete.")
            return
        if not messagebox.askyesno("Confirm Delete", f"Delete location '{loc_name}' and its data?"):
            return
        backup_db()
        with contextlib.closing(get_connection()) as conn:
            conn.execute("DELETE FROM order_items WHERE location_id = ?", (loc_id,))
            conn.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
            conn.commit()
        self._populate_global_dropdowns()
        self.refresh_all_settings()

    @safe_action
    def _add_brand(self):
        loc_name = self.new_brand_loc_cb.get().strip()
        brand_name = self.new_brand_name_entry.get().strip()
        loc_id = self.location_map.get(loc_name)
        if not loc_id or not brand_name:
            messagebox.showwarning("Missing Fields", "Select a location and enter a brand name.")
            return
        with contextlib.closing(get_connection()) as conn:
            conn.execute("INSERT INTO brands (location_id, name, status) VALUES (?, ?, 'Active')", (loc_id, brand_name))
            conn.commit()
        self.new_brand_name_entry.delete(0, "end")
        self._populate_global_dropdowns()
        self.refresh_all_settings()

    @safe_action
    def _delete_brand(self):
        sel = self.brands_tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select a brand row to delete.")
            return
        vals = self.brands_tree.item(sel[0], "values")
        brand_id = int(vals[0])
        brand_name = vals[2]
        if not messagebox.askyesno("Confirm Delete", f"Delete brand '{brand_name}'?"):
            return
        backup_db()
        with contextlib.closing(get_connection()) as conn:
            conn.execute("DELETE FROM order_items WHERE brand_id = ?", (brand_id,))
            conn.execute("DELETE FROM brands WHERE id = ?", (brand_id,))
            conn.commit()
        self._populate_global_dropdowns()
        self.refresh_all_settings()

    # --- Staffing Rules CRUD ---
    def _build_settings_hr_subtab(self, tab):
        t = self._t()
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        form = ctk.CTkFrame(tab, fg_color=t["canvas"])
        form.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))

        ctk.CTkLabel(form, text="Edit Selected Rule:", font=self.font_bold).pack(side="left", padx=(10, 8), pady=10)
        self.hr_metric_lbl = ctk.CTkLabel(form, text="[Metric]", font=self.font_bold, text_color=t["muted"])
        self.hr_metric_lbl.pack(side="left", padx=6)

        ctk.CTkLabel(form, text="Min:").pack(side="left", padx=(8, 2))
        self.hr_min_entry = ctk.CTkEntry(form, width=70)
        self.hr_min_entry.pack(side="left", padx=4)

        ctk.CTkLabel(form, text="Max:").pack(side="left", padx=(8, 2))
        self.hr_max_entry = ctk.CTkEntry(form, width=75)
        self.hr_max_entry.pack(side="left", padx=4)

        ctk.CTkLabel(form, text="Cooks:").pack(side="left", padx=(8, 2))
        self.hr_cooks_entry = ctk.CTkEntry(form, width=55)
        self.hr_cooks_entry.pack(side="left", padx=4)

        ctk.CTkLabel(form, text="Label:").pack(side="left", padx=(8, 2))
        self.hr_label_entry = ctk.CTkEntry(form, width=130)
        self.hr_label_entry.pack(side="left", padx=4)

        ctk.CTkButton(form, text="Save Rule", width=110, command=self._save_staffing_rule).pack(side="right", padx=10, pady=10)

        cols = ("ID", "Min Load", "Max Load", "Metric", "Suggested Cooks", "Label")
        hr_tbl_wrap = ctk.CTkFrame(tab, fg_color="transparent")
        hr_tbl_wrap.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 12))
        hr_tbl_wrap.grid_columnconfigure(0, weight=1)
        hr_tbl_wrap.grid_rowconfigure(0, weight=1)
        self.hr_tree = ttk.Treeview(hr_tbl_wrap, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.hr_tree.heading(c, text=c)
        hr_vsb = ttk.Scrollbar(hr_tbl_wrap, orient="vertical", command=self.hr_tree.yview)
        self.hr_tree.configure(yscrollcommand=hr_vsb.set)
        self.hr_tree.grid(row=0, column=0, sticky="nsew")
        hr_vsb.grid(row=0, column=1, sticky="ns")
        self.hr_tree.bind("<<TreeviewSelect>>", self._on_hr_tree_select)

    def _on_hr_tree_select(self, _event=None):
        sel = self.hr_tree.selection()
        if not sel:
            return
        vals = self.hr_tree.item(sel[0], "values")
        self.hr_min_entry.delete(0, "end")
        self.hr_min_entry.insert(0, vals[1])
        self.hr_max_entry.delete(0, "end")
        self.hr_max_entry.insert(0, vals[2])
        self.hr_metric_lbl.configure(text=f"[{vals[3]}]")
        self.hr_cooks_entry.delete(0, "end")
        self.hr_cooks_entry.insert(0, vals[4])
        self.hr_label_entry.delete(0, "end")
        self.hr_label_entry.insert(0, vals[5])

    @safe_action
    def _save_staffing_rule(self):
        sel = self.hr_tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select a staffing rule row to edit.")
            return
        vals = self.hr_tree.item(sel[0], "values")
        rule_id = int(vals[0])
        metric = vals[3]

        try:
            min_v = float(self.hr_min_entry.get().strip())
            max_v = float(self.hr_max_entry.get().strip())
            cooks_v = int(self.hr_cooks_entry.get().strip())
        except ValueError:
            messagebox.showwarning("Invalid Input", "Min Load and Max Load must be numbers, and Cooks must be an integer.")
            return

        label_v = self.hr_label_entry.get().strip()
        if min_v < 0 or max_v <= min_v or cooks_v < 1 or not label_v:
            messagebox.showwarning("Validation Error", "Ensure 0 <= Min < Max, Cooks >= 1, and Label is not empty.")
            return

        with contextlib.closing(get_connection()) as conn:
            other_rules = conn.execute(
                "SELECT id, min_load, max_load FROM staffing_rules WHERE metric = ? AND id != ? ORDER BY min_load",
                (metric, rule_id)
            ).fetchall()

            proposed = sorted(
                [(float(r['min_load']), float(r['max_load'])) for r in other_rules] + [(min_v, max_v)],
                key=lambda x: x[0]
            )
            if proposed[0][0] != 0.0:
                messagebox.showwarning("Continuity Error", f"The lowest threshold for {metric} must start at 0.0.")
                return
            for idx in range(len(proposed) - 1):
                curr_max = proposed[idx][1]
                next_min = proposed[idx + 1][0]
                if abs(curr_max - next_min) > 1e-6:
                    messagebox.showwarning(
                        "Continuity Error",
                        f"Thresholds for {metric} must be continuous without gaps or overlaps.\n"
                        f"Found {curr_max} followed by {next_min}."
                    )
                    return

            conn.execute("""
                UPDATE staffing_rules
                SET min_load = ?, max_load = ?, suggested_cooks = ?, label = ?
                WHERE id = ?
            """, (min_v, max_v, cooks_v, label_v, rule_id))
            conn.commit()

        self.invalidate_analytics_cache()
        self.mark_tabs_dirty()
        self.refresh_all_settings()
        messagebox.showinfo("Rule Saved", "Staffing rule updated successfully.")

    # --- Meal Shifts CRUD ---
    def _build_settings_shifts_subtab(self, tab):
        t = self._t()
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        form = ctk.CTkFrame(tab, fg_color=t["canvas"])
        form.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))

        ctk.CTkLabel(form, text="Shift Name:", font=self.font_bold).pack(side="left", padx=(10, 4), pady=10)
        self.shift_name_entry = ctk.CTkEntry(form, placeholder_text="e.g. Lunch Rush", width=160)
        self.shift_name_entry.pack(side="left", padx=4)

        ctk.CTkLabel(form, text="Start (HH:MM):").pack(side="left", padx=(8, 4))
        self.shift_start_entry = ctk.CTkEntry(form, placeholder_text="11:30", width=75)
        self.shift_start_entry.pack(side="left", padx=4)

        ctk.CTkLabel(form, text="End (HH:MM):").pack(side="left", padx=(8, 4))
        self.shift_end_entry = ctk.CTkEntry(form, placeholder_text="14:30", width=75)
        self.shift_end_entry.pack(side="left", padx=4)

        ctk.CTkButton(form, text="Delete Shift", width=105, fg_color=t["danger"], command=self._delete_meal_shift).pack(side="right", padx=(6, 10), pady=10)
        ctk.CTkButton(form, text="Update Selected", width=125, command=self._update_meal_shift).pack(side="right", padx=6, pady=10)
        ctk.CTkButton(form, text="Add Shift", width=95, command=self._add_meal_shift).pack(side="right", padx=6, pady=10)

        cols = ("ID", "Shift Name", "Start Time", "End Time")
        shifts_tbl_wrap = ctk.CTkFrame(tab, fg_color="transparent")
        shifts_tbl_wrap.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 12))
        shifts_tbl_wrap.grid_columnconfigure(0, weight=1)
        shifts_tbl_wrap.grid_rowconfigure(0, weight=1)
        self.shifts_tree = ttk.Treeview(shifts_tbl_wrap, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.shifts_tree.heading(c, text=c)
        shifts_vsb = ttk.Scrollbar(shifts_tbl_wrap, orient="vertical", command=self.shifts_tree.yview)
        self.shifts_tree.configure(yscrollcommand=shifts_vsb.set)
        self.shifts_tree.grid(row=0, column=0, sticky="nsew")
        shifts_vsb.grid(row=0, column=1, sticky="ns")
        self.shifts_tree.bind("<<TreeviewSelect>>", self._on_shift_tree_select)

    def _on_shift_tree_select(self, _event=None):
        sel = self.shifts_tree.selection()
        if not sel:
            return
        vals = self.shifts_tree.item(sel[0], "values")
        self.shift_name_entry.delete(0, "end")
        self.shift_name_entry.insert(0, vals[1])
        self.shift_start_entry.delete(0, "end")
        self.shift_start_entry.insert(0, vals[2])
        self.shift_end_entry.delete(0, "end")
        self.shift_end_entry.insert(0, vals[3])

    @safe_action
    def _add_meal_shift(self):
        name = self.shift_name_entry.get().strip()
        st = self.shift_start_entry.get().strip()
        et = self.shift_end_entry.get().strip()
        if not name or not validate_hhmm(st) or not validate_hhmm(et):
            messagebox.showwarning("Invalid Shift", "Enter a shift name and valid 24-hour times in HH:MM format (e.g. 11:30).")
            return
        with contextlib.closing(get_connection()) as conn:
            conn.execute("INSERT INTO meal_shifts (name, start_time, end_time) VALUES (?, ?, ?)", (name, st, et))
            conn.commit()
        self.refresh_all_settings()
        messagebox.showinfo("Shift Added", f"Meal shift '{name}' added.")

    @safe_action
    def _update_meal_shift(self):
        sel = self.shifts_tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select a meal shift row to update.")
            return
        shift_id = int(self.shifts_tree.item(sel[0], "values")[0])
        name = self.shift_name_entry.get().strip()
        st = self.shift_start_entry.get().strip()
        et = self.shift_end_entry.get().strip()
        if not name or not validate_hhmm(st) or not validate_hhmm(et):
            messagebox.showwarning("Invalid Shift", "Enter a shift name and valid 24-hour times in HH:MM format (e.g. 11:30).")
            return
        with contextlib.closing(get_connection()) as conn:
            conn.execute("UPDATE meal_shifts SET name = ?, start_time = ?, end_time = ? WHERE id = ?", (name, st, et, shift_id))
            conn.commit()
        self.refresh_all_settings()
        messagebox.showinfo("Shift Updated", f"Meal shift '{name}' updated.")

    @safe_action
    def _delete_meal_shift(self):
        sel = self.shifts_tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select a meal shift row to delete.")
            return
        vals = self.shifts_tree.item(sel[0], "values")
        if messagebox.askyesno("Confirm Delete", f"Delete meal shift '{vals[1]}'?"):
            with contextlib.closing(get_connection()) as conn:
                conn.execute("DELETE FROM meal_shifts WHERE id = ?", (int(vals[0]),))
                conn.commit()
            self.refresh_all_settings()

    # --- Exclusions CRUD ---
    def _build_settings_exclusions_subtab(self, tab):
        t = self._t()
        tab.grid_columnconfigure(0, weight=1)
        tab.grid_rowconfigure(1, weight=1)

        hdr = ctk.CTkFrame(tab, fg_color="transparent")
        hdr.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 4))
        ctk.CTkLabel(
            hdr,
            text="Excluded Non-Kitchen Keywords (comma-separated, e.g. drinks, sauces, cutlery):",
            font=self.font_bold,
            text_color=t["text"]
        ).pack(side="left")
        ctk.CTkButton(hdr, text="Save Exclusions", width=140, command=self._save_exclusions).pack(side="right")

        self.excl_textbox = ctk.CTkTextbox(tab, height=220)
        self.excl_textbox.grid(row=1, column=0, sticky="nsew", padx=14, pady=(6, 16))

    @safe_action
    def _save_exclusions(self):
        raw_text = self.excl_textbox.get("1.0", "end").strip()
        keywords = sorted({kw.strip().lower() for kw in raw_text.split(",") if kw.strip()})
        with contextlib.closing(get_connection()) as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM exclusion_keywords")
            if keywords:
                cur.executemany("INSERT INTO exclusion_keywords (keyword) VALUES (?)", [(k,) for k in keywords])
            conn.commit()
        self.invalidate_analytics_cache()
        self.mark_tabs_dirty()
        self.refresh_all_settings()
        messagebox.showinfo("Exclusions Saved", f"Saved {len(keywords)} exclusion keywords.")

    @safe_action
    def refresh_all_settings(self):
        with contextlib.closing(get_connection()) as conn:
            self.brands_tree.delete(*self.brands_tree.get_children())
            self.brand_merge_map = {}
            self.duplicate_choice_map = {}
            main_labels = []
            dup_labels = []

            for r in conn.execute("SELECT b.id, l.name as loc, b.name as brand, b.status FROM brands b JOIN locations l ON b.location_id = l.id ORDER BY l.name, b.name"):
                self.brands_tree.insert("", "end", values=(r['id'], r['loc'], r['brand'], r['status']))
                label = f"{r['brand']} ({r['loc']})"
                self.brand_merge_map[label] = r['id']
                main_labels.append(label)

                self.duplicate_choice_map[label] = {
                    "kind": "brand",
                    "brand_id": r['id']
                }
                dup_labels.append(label)

            for r in conn.execute("SELECT id, csv_venue_name, platform, brand_id FROM platform_aliases ORDER BY csv_venue_name"):
                alias_label = f"{r['csv_venue_name']} [{r['platform']}]"
                if alias_label not in self.duplicate_choice_map:
                    self.duplicate_choice_map[alias_label] = {
                        "kind": "alias",
                        "alias_id": r['id'],
                        "csv_venue_name": r['csv_venue_name'],
                        "platform": r['platform'],
                        "brand_id": r['brand_id']
                    }
                    dup_labels.append(alias_label)

            loc_names = list(self.location_map.keys())
            for cb_attr in ('new_brand_loc_cb', 'edit_brand_loc_cb', 'move_brand_loc_cb', 'del_loc_cb'):
                if hasattr(self, cb_attr):
                    cb_widget = getattr(self, cb_attr)
                    cb_widget.configure(values=loc_names if loc_names else [""])
                    if loc_names:
                        if cb_widget.get() not in loc_names:
                            cb_widget.set(loc_names[0])
                    else:
                        cb_widget.set("")

            if hasattr(self, 'merge_source_cb') and hasattr(self, 'merge_target_cb'):
                self.merge_source_cb.configure(values=main_labels if main_labels else [""])
                self.merge_target_cb.configure(values=main_labels if main_labels else [""])
                if len(main_labels) >= 2:
                    if self.merge_source_cb.get() not in main_labels:
                        self.merge_source_cb.set(main_labels[0])
                    if self.merge_target_cb.get() not in main_labels or self.merge_target_cb.get() == self.merge_source_cb.get():
                        self.merge_target_cb.set(main_labels[1])
                elif main_labels:
                    self.merge_source_cb.set(main_labels[0])
                    self.merge_target_cb.set(main_labels[0])

            if hasattr(self, 'combine_dup_cb'):
                self.combine_dup_cb.configure(values=dup_labels if dup_labels else [""])
                if dup_labels:
                    self.combine_dup_cb.set(dup_labels[0])
                self.combine_main_cb.configure(values=main_labels if main_labels else [""])
                if main_labels:
                    self.combine_main_cb.set(main_labels[-1])

            self.hr_tree.delete(*self.hr_tree.get_children())
            for r in conn.execute("SELECT * FROM staffing_rules ORDER BY metric, min_load"):
                self.hr_tree.insert("", "end", values=(r['id'], r['min_load'], r['max_load'], r['metric'], r['suggested_cooks'], r['label']))

            self.shifts_tree.delete(*self.shifts_tree.get_children())
            for r in conn.execute("SELECT * FROM meal_shifts ORDER BY start_time"):
                self.shifts_tree.insert("", "end", values=(r['id'], r['name'], r['start_time'], r['end_time']))

            kw_rows = conn.execute("SELECT keyword FROM exclusion_keywords ORDER BY keyword").fetchall()
            self.excl_textbox.delete("1.0", "end")
            self.excl_textbox.insert("1.0", ", ".join([r['keyword'] for r in kw_rows]))

        if hasattr(self, "unmapped_tree"):
            snap = brand_service.get_brand_management_snapshot()
            if hasattr(self, "auto_create_brand_var"):
                self.auto_create_brand_var.set(bool(snap.get("auto_create_unknown_brands", True)))

            self.unmapped_tree.delete(*self.unmapped_tree.get_children())
            for cand in snap.get("unmapped_candidates", []):
                d_range = f"{cand.get('date_min') or '?'}..{cand.get('date_max') or '?'}"
                self.unmapped_tree.insert(
                    "",
                    "end",
                    values=(
                        cand.get("raw_name", ""),
                        cand.get("platform", ""),
                        cand.get("orders_count", 0),
                        f"{float(cand.get('orders_sales', 0.0)):.2f}",
                        cand.get("reports_count", 0),
                        f"{float(cand.get('reports_gross_sales', 0.0)):.2f}",
                        cand.get("status", "pending"),
                    ),
                )

            self.alias_brand_id_map = {}
            brand_choices = []
            for b in snap.get("brands", []):
                b_label = f"{b['name']} ({b['location_name']})"
                self.alias_brand_id_map[b_label] = int(b["id"])
                brand_choices.append(b_label)
            if hasattr(self, "alias_target_brand_cb"):
                self.alias_target_brand_cb.configure(values=brand_choices if brand_choices else [""])
                if brand_choices and self.alias_target_brand_cb.get() not in brand_choices:
                    self.alias_target_brand_cb.set(brand_choices[0])

            self.active_alias_id_map = {}
            alias_choices = []
            for a in snap.get("aliases", []):
                plat_part = f" [{a['platform']}]" if a.get("platform") else ""
                a_label = f"#{a['id']}: {a['alias_name']}{plat_part} -> {a['brand_name']}"
                self.active_alias_id_map[a_label] = int(a["id"])
                alias_choices.append(a_label)
            if hasattr(self, "active_alias_cb"):
                self.active_alias_cb.configure(values=alias_choices if alias_choices else [""])
                if alias_choices and self.active_alias_cb.get() not in alias_choices:
                    self.active_alias_cb.set(alias_choices[0])

    def _populate_global_dropdowns(self):
        with contextlib.closing(get_connection()) as conn:
            locs = conn.execute("SELECT id, name FROM locations ORDER BY name").fetchall()
        self.location_map = {r['name']: r['id'] for r in locs}
        single_loc_names = list(self.location_map.keys()) if locs else ["No Locations"]
        multi_loc_names = (["All Locations (All Active)"] + list(self.location_map.keys())) if locs else ["No Locations"]

        self.dash_location_cb.configure(values=multi_loc_names)
        self.daily_location_cb.configure(values=multi_loc_names)

        if self.dash_location_cb.get() not in multi_loc_names:
            self.dash_location_cb.set(multi_loc_names[0])
        if self.daily_location_cb.get() not in multi_loc_names:
            self.daily_location_cb.set(multi_loc_names[0])

        self._on_dash_location_change(self.dash_location_cb.get())
        self._on_daily_location_change(self.daily_location_cb.get())

    def _get_brands_for_location(self, loc_name: str):
        with contextlib.closing(get_connection()) as conn:
            if loc_name == "All Locations (All Active)":
                rows = conn.execute(
                    """
                    SELECT b.id, b.name, l.name as loc_name
                    FROM brands b
                    JOIN locations l ON b.location_id = l.id
                    WHERE b.status = 'Active'
                    ORDER BY b.name, l.name
                    """
                ).fetchall()
                return [
                    {"id": r["id"], "name": r["name"], "display_name": f"{r['name']} ({r['loc_name']})"}
                    for r in rows
                ]
            loc_id = self.location_map.get(loc_name)
            if not loc_id:
                return []
            rows = conn.execute(
                "SELECT id, name FROM brands WHERE location_id = ? AND status = 'Active' ORDER BY name",
                (loc_id,)
            ).fetchall()
            return [
                {"id": r["id"], "name": r["name"], "display_name": r["name"]}
                for r in rows
            ]

    def _on_dash_location_change(self, loc_name: str):
        brands = self._get_brands_for_location(loc_name)
        all_label = "All Active Restaurants" if loc_name == "All Locations (All Active)" else "All Brands Combined"
        prev_brand = self.dash_brand_cb.get()
        vals = [all_label] + [b['display_name'] for b in brands]
        self.dash_brand_cb.configure(values=vals)
        if prev_brand in vals:
            self.dash_brand_cb.set(prev_brand)
        elif vals:
            self.dash_brand_cb.set(vals[0])

        brands = self._get_brands_for_location(loc_name)
        vals = [b['name'] for b in brands] if brands else ["No Active Brands"]


    def _build_finances_tab(self):
        t = self._t()
        parent = self.frames["finances"]
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(3, weight=1)

        # Karta filtrów na górze
        filter_card = ctk.CTkFrame(parent, fg_color=t["card"], corner_radius=8)
        filter_card.grid(row=0, column=0, padx=6, pady=(6, 10), sticky="ew")

        ctk.CTkLabel(filter_card, text="From:", font=self.font_small, text_color=t["muted"]).pack(
            side="left", padx=(12, 4), pady=10
        )
        self.fin_date_from = ctk.CTkEntry(filter_card, width=105)
        self.fin_date_from.pack(side="left", padx=(0, 8), pady=10)

        ctk.CTkLabel(filter_card, text="To:", font=self.font_small, text_color=t["muted"]).pack(
            side="left", padx=(0, 4), pady=10
        )
        self.fin_date_to = ctk.CTkEntry(filter_card, width=105)
        self.fin_date_to.pack(side="left", padx=(0, 10), pady=10)

        ctk.CTkLabel(filter_card, text="Platform:", font=self.font_small, text_color=t["muted"]).pack(
            side="left", padx=(0, 4), pady=10
        )
        self.fin_platform_var = ctk.StringVar(value="All")
        self.fin_platform_menu = ctk.CTkOptionMenu(
            filter_card,
            values=["All", "Bolt Food", "Wolt"],
            variable=self.fin_platform_var,
            width=115,
            command=lambda _v: self.refresh_finances_tab(force=True),
        )
        self.fin_platform_menu.pack(side="left", padx=(0, 10), pady=10)

        ctk.CTkLabel(filter_card, text="Location:", font=self.font_small, text_color=t["muted"]).pack(
            side="left", padx=(0, 4), pady=10
        )
        self.fin_location_var = ctk.StringVar(value="All")
        self.fin_location_menu = ctk.CTkOptionMenu(
            filter_card,
            values=["All"],
            variable=self.fin_location_var,
            width=130,
            command=self._on_fin_location_changed,
        )
        self.fin_location_menu.pack(side="left", padx=(0, 10), pady=10)

        ctk.CTkLabel(filter_card, text="Restaurant:", font=self.font_small, text_color=t["muted"]).pack(
            side="left", padx=(0, 4), pady=10
        )
        self.fin_brand_var = ctk.StringVar(value="All")
        self.fin_brand_menu = ctk.CTkOptionMenu(
            filter_card,
            values=["All"],
            variable=self.fin_brand_var,
            width=175,
            command=lambda _v: self.refresh_finances_tab(force=True),
        )
        self.fin_brand_menu.pack(side="left", padx=(0, 10), pady=10)

        ctk.CTkButton(
            filter_card,
            text="Apply",
            width=80,
            command=lambda: self.refresh_finances_tab(force=True),
        ).pack(side="left", padx=(0, 6), pady=10)

        ctk.CTkButton(
            filter_card,
            text="Reset",
            width=75,
            fg_color="transparent",
            border_width=1,
            border_color=t["ghost_border"],
            text_color=t["text"],
            command=self._reset_finances_filters,
        ).pack(side="left", padx=(0, 12), pady=10)

        ctk.CTkButton(
            filter_card,
            text="Export .xlsx / .csv",
            height=30,
            fg_color="transparent",
            hover_color=t["ghost_hover"],
            border_width=1,
            border_color=t["ghost_border"],
            text_color=t["text"],
            command=self._export_finances_file,
        ).pack(side="right", padx=12, pady=10)

        # Wiersz 1: Główne kafelki metryk (dokładny styl KPI)
        cards_frame = ctk.CTkFrame(parent, fg_color="transparent")
        cards_frame.grid(row=1, column=0, sticky="ew", pady=(0, 4))
        for c in range(3):
            cards_frame.grid_columnconfigure(c, weight=1, uniform="fin_cards_top")

        self.fin_cards = {}
        top_card_specs = [
            ("gross_sales", "Gross Sales (incl. VAT)"),
            ("service_fees", "Commissions & Fees (incl. VAT)"),
            ("total_losses", "Total Costs = Fees + Ads + Disc./Ded."),
        ]
        for idx, (key, title) in enumerate(top_card_specs):
            card = ctk.CTkFrame(cards_frame, fg_color=t["card"], corner_radius=8)
            card.grid(row=0, column=idx, padx=6, pady=6, sticky="nsew")
            lbl_title = ctk.CTkLabel(card, text=title, font=self.font_small, text_color=t["muted"])
            lbl_title.pack(anchor="w", padx=12, pady=(10, 0))
            lbl_main = ctk.CTkLabel(card, text="0.00 EUR", font=self.font_kpi, text_color=t["text"])
            lbl_sub = ctk.CTkLabel(card, text="0.00% of sales", font=self.font_small, text_color=t["muted"])
            lbl_main.pack(anchor="w", padx=12, pady=(0, 0))
            lbl_sub.pack(anchor="w", padx=12, pady=(0, 10))
            self.fin_cards[key] = (lbl_main, lbl_sub)

        # Wiersz 2: Czytelne kafelki sum (Ads, Discounts/Refunds, Net Payout)
        totals_frame = ctk.CTkFrame(parent, fg_color="transparent")
        totals_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        for c in range(3):
            totals_frame.grid_columnconfigure(c, weight=1, uniform="fin_cards_bottom")

        bottom_card_specs = [
            ("ads_total", "Total Ads & Promos"),
            ("other_total", "Total Discounts & Refunds"),
            ("payout_total", "Total Platform Payout"),
        ]
        for idx, (key, title) in enumerate(bottom_card_specs):
            card = ctk.CTkFrame(totals_frame, fg_color=t["card"], corner_radius=8)
            card.grid(row=0, column=idx, padx=6, pady=4, sticky="nsew")
            lbl_title = ctk.CTkLabel(card, text=title, font=self.font_small, text_color=t["muted"])
            lbl_title.pack(anchor="w", padx=12, pady=(10, 0))
            lbl_main = ctk.CTkLabel(card, text="0.00 EUR", font=self.font_kpi, text_color=t["text"])
            lbl_sub = ctk.CTkLabel(card, text="0.00% of sales", font=self.font_small, text_color=t["muted"])
            lbl_main.pack(anchor="w", padx=12, pady=(0, 0))
            lbl_sub.pack(anchor="w", padx=12, pady=(0, 10))
            self.fin_cards[key] = (lbl_main, lbl_sub)

        # Karta tabeli o tym samym kształcie co pozostałe karty
        table_card = ctk.CTkFrame(parent, fg_color=t["card"], corner_radius=8)
        table_card.grid(row=3, column=0, padx=6, pady=(0, 12), sticky="nsew")
        table_card.grid_columnconfigure(0, weight=1)
        table_card.grid_rowconfigure(0, weight=1)

        cols = ("brand", "platform", "reports", "sales", "ads", "comm_fees", "other", "losses", "share", "payout")
        self.fin_tree = ttk.Treeview(table_card, columns=cols, show="headings", height=11)
        headings = {
            "brand": "Restaurant",
            "platform": "Platform",
            "reports": "Docs",
            "sales": "Gross Sales (incl. VAT)",
            "ads": "Ads & Promos",
            "comm_fees": "Commissions & Fees",
            "other": "Discounts & Refunds",
            "losses": "Total Costs (All)",
            "share": "Costs % of Sales",
            "payout": "Platform Payout",
        }
        widths = {
            "brand": 185,
            "platform": 95,
            "reports": 60,
            "sales": 135,
            "ads": 105,
            "comm_fees": 135,
            "other": 135,
            "losses": 115,
            "share": 105,
            "payout": 125,
        }
        for c in cols:
            self.fin_tree.heading(c, text=headings[c], anchor="center")
            self.fin_tree.column(c, width=widths[c], anchor="center")

        self.fin_tree.grid(row=0, column=0, sticky="nsew", padx=12, pady=12)

    def _export_finances_file(self):
        if not hasattr(self, "fin_tree"):
            return
        rows = [self.fin_tree.item(iid, "values") for iid in self.fin_tree.get_children()]
        if not rows:
            messagebox.showwarning("No Data", "There is no losses and payouts data to export.")
            return

        headers = [
            "Restaurant",
            "Platform",
            "Docs",
            "Gross Sales (incl. VAT)",
            "Ads & Promos",
            "Commissions & Fees",
            "Discounts & Refunds",
            "Total Costs (All)",
            "Costs % of Sales",
            "Platform Payout",
        ]
        df_export = pd.DataFrame(rows, columns=headers)

        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel Workbook", "*.xlsx"), ("CSV File", "*.csv")],
            initialfile=f"Losses_Payouts_{datetime.now().strftime('%Y%m%d')}.xlsx",
        )
        if not path:
            return

        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            if path.lower().endswith(".csv"):
                df_export.to_csv(path, index=False, encoding="utf-8-sig")
            else:
                with pd.ExcelWriter(path, engine="openpyxl") as writer:
                    df_export.to_excel(writer, sheet_name="Losses & Payouts", index=False)
            messagebox.showinfo("Export Complete", f"Losses & payouts report saved to:\n{path}")
        finally:
            self.configure(cursor="")

    def _sync_finances_filter_options(self):
        if not hasattr(self, "fin_location_menu") or not hasattr(self, "fin_brand_menu"):
            return

        locs = []
        try:
            conn = get_connection()
            try:
                rows = conn.execute("SELECT id, name FROM locations ORDER BY name COLLATE NOCASE").fetchall()
                for r in rows:
                    locs.append({"id": int(r["id"]), "name": str(r["name"])})
            finally:
                conn.close()
        except Exception:
            locs = []

        brands = []
        try:
            conn = get_connection()
            try:
                rows = conn.execute("SELECT id, name, location_id FROM brands ORDER BY name COLLATE NOCASE").fetchall()
                for r in rows:
                    brands.append({
                        "id": int(r["id"]),
                        "name": str(r["name"]),
                        "location_id": r["location_id"],
                    })
            finally:
                conn.close()
        except Exception:
            brands = []

        self._fin_loc_map = {"All": None}
        loc_names = ["All"]
        for loc in locs:
            nm = str(loc.get("name") or "").strip()
            if nm and nm not in self._fin_loc_map:
                self._fin_loc_map[nm] = int(loc["id"])
                loc_names.append(nm)

        cur_loc = self.fin_location_var.get() if hasattr(self, "fin_location_var") else "All"
        if cur_loc not in self._fin_loc_map:
            cur_loc = "All"
            self.fin_location_var.set("All")
        self.fin_location_menu.configure(values=loc_names)
        self.fin_location_var.set(cur_loc)

        sel_loc_id = self._fin_loc_map.get(cur_loc)
        self._fin_brand_map = {"All": None}
        brand_names = ["All"]
        for br in brands:
            b_loc = br.get("location_id")
            if sel_loc_id is not None and (b_loc is None or int(b_loc) != int(sel_loc_id)):
                continue
            bname = str(br.get("name") or "").strip()
            if bname and bname not in self._fin_brand_map:
                self._fin_brand_map[bname] = int(br["id"])
                brand_names.append(bname)

        cur_brand = self.fin_brand_var.get() if hasattr(self, "fin_brand_var") else "All"
        if cur_brand not in self._fin_brand_map:
            cur_brand = "All"
            self.fin_brand_var.set("All")
        self.fin_brand_menu.configure(values=brand_names)
        self.fin_brand_var.set(cur_brand)

    def _on_fin_location_changed(self, _val=None):
        if hasattr(self, "fin_brand_var"):
            self.fin_brand_var.set("All")
        self._sync_finances_filter_options()
        self.refresh_finances_tab(force=True)

    def _reset_finances_filters(self):
        self.fin_date_from.delete(0, "end")
        self.fin_date_to.delete(0, "end")
        self.fin_platform_var.set("All")
        self.fin_location_var.set("All")
        self.fin_brand_var.set("All")
        self._sync_finances_filter_options()
        self.refresh_finances_tab(force=True)

    @safe_action
    def refresh_finances_tab(self, force: bool = False):
        if not hasattr(self, "fin_cards"):
            return
        self._sync_finances_filter_options()
        d_from = self.fin_date_from.get().strip() or None
        d_to = self.fin_date_to.get().strip() or None
        plat = self.fin_platform_var.get()
        loc_name = self.fin_location_var.get() if hasattr(self, "fin_location_var") else "All"
        brand_name = self.fin_brand_var.get() if hasattr(self, "fin_brand_var") else "All"
        brand_id = getattr(self, "_fin_brand_map", {}).get(brand_name)

        fin_key = (d_from, d_to, plat, loc_name, brand_name, self.theme_mode)
        if (
            not force
            and getattr(self, "_last_finances_key", None) == fin_key
            and hasattr(self, "fin_tree")
            and len(self.fin_tree.get_children()) > 0
        ):
            return

        existing_rows = self.fin_tree.get_children()
        if existing_rows:
            self.fin_tree.delete(*existing_rows)
        self._last_finances_key = fin_key

        df = get_payout_breakdown_by_brand_and_platform(
            date_from=d_from,
            date_to=d_to,
            brand_id=brand_id,
            platform=plat,
        )
        allowed_brands = None
        if brand_id is None and loc_name != "All":
            sel_loc_id = getattr(self, "_fin_loc_map", {}).get(loc_name)
            if sel_loc_id is not None:
                conn = get_connection()
                try:
                    rows = conn.execute(
                        "SELECT id FROM brands WHERE location_id = ?",
                        (int(sel_loc_id),),
                    ).fetchall()
                    allowed_brands = {int(r["id"]) for r in rows}
                finally:
                    conn.close()

        tot_docs = 0
        tot_sales = 0.0
        tot_ads = 0.0
        tot_comm = 0.0
        tot_other = 0.0
        tot_costs = 0.0
        tot_payout = 0.0

        for _, r in df.iterrows():
            if allowed_brands is not None and int(r["brand_id"]) not in allowed_brands:
                continue
            docs_cnt = int(r["reports_count"] or 0)
            row_sales = float(r["gross_sales_total"] or 0.0)
            row_ads = float(r["ads_campaigns_gross"] or 0.0)
            row_comm = float(r["commissions_fees_gross"] or 0.0)
            other_c = round(
                float(r["merchant_discounts_gross"] or 0.0)
                + float(r["deductions_refunds_gross"] or 0.0)
                + float(r["other_adjustments_gross"] or 0.0),
                2,
            )
            row_costs = round(row_comm + row_ads + other_c, 2)
            row_payout = float(r["payout_amount_gross"] or 0.0)
            row_pct = round((row_costs / row_sales) * 100.0, 2) if row_sales > 0 else 0.0

            tot_docs += docs_cnt
            tot_sales += row_sales
            tot_ads += row_ads
            tot_comm += row_comm
            tot_other += other_c
            tot_costs += row_costs
            tot_payout += row_payout

            self.fin_tree.insert(
                "",
                "end",
                values=(
                    r["brand_name"],
                    r["platform"],
                    docs_cnt,
                    f"{row_sales:,.2f}",
                    f"{row_ads:,.2f}",
                    f"{row_comm:,.2f}",
                    f"{other_c:,.2f}",
                    f"{row_costs:,.2f}",
                    f"{row_pct:.2f}%",
                    f"{row_payout:,.2f}",
                ),
            )

        tot_sales = round(tot_sales, 2)
        tot_ads = round(tot_ads, 2)
        tot_comm = round(tot_comm, 2)
        tot_other = round(tot_other, 2)
        tot_costs = round(tot_costs, 2)
        tot_payout = round(tot_payout, 2)

        if "ads_total" in self.fin_cards:
            ads_pct = (tot_ads / tot_sales) * 100.0 if tot_sales > 0 else 0.0
            v_lbl, s_lbl = self.fin_cards["ads_total"]
            v_lbl.configure(text=f"{tot_ads:,.2f} EUR")
            s_lbl.configure(text=f"{ads_pct:.2f}% of sales")

        if "other_total" in self.fin_cards:
            oth_pct = (tot_other / tot_sales) * 100.0 if tot_sales > 0 else 0.0
            v_lbl, s_lbl = self.fin_cards["other_total"]
            v_lbl.configure(text=f"{tot_other:,.2f} EUR")
            s_lbl.configure(text=f"{oth_pct:.2f}% of sales")

        if "payout_total" in self.fin_cards:
            pay_pct = (tot_payout / tot_sales) * 100.0 if tot_sales > 0 else 0.0
            v_lbl, s_lbl = self.fin_cards["payout_total"]
            v_lbl.configure(text=f"{tot_payout:,.2f} EUR")
            s_lbl.configure(text=f"{pay_pct:.2f}% of sales ({tot_docs} docs)")
        tot_ads = round(tot_ads, 2)
        tot_comm = round(tot_comm, 2)
        tot_other = round(tot_other, 2)
        tot_costs = round(tot_costs, 2)
        tot_payout = round(tot_payout, 2)

        comm_pct = round((tot_comm / tot_sales) * 100.0, 2) if tot_sales > 0 else 0.0
        total_pct = round((tot_costs / tot_sales) * 100.0, 2) if tot_sales > 0 else 0.0

        self.fin_cards["gross_sales"][0].configure(text=f"{tot_sales:,.2f} EUR")
        self.fin_cards["gross_sales"][1].configure(text=f"Docs: {tot_docs}")

        self.fin_cards["service_fees"][0].configure(text=f"{tot_comm:,.2f} EUR")
        self.fin_cards["service_fees"][1].configure(text=f"{comm_pct:.2f}% of gross sales")

        self.fin_cards["total_losses"][0].configure(text=f"{tot_costs:,.2f} EUR")
        self.fin_cards["total_losses"][1].configure(
            text=(
                f"{total_pct:.2f}% of sales | Platform Payout: {tot_payout:,.2f} EUR "
                f"(Ads: {tot_ads:,.2f}, Disc./Ref.: {tot_other:,.2f})"
            )
        )

    def _get_selected_ids(self, loc_name: str, brand_name: str, allow_all_brands: bool = True):
        if loc_name == "All Locations (All Active)":
            if allow_all_brands and brand_name in ("All Active Restaurants", "All Brands Combined"):
                return None, None
            brands = self._get_brands_for_location(loc_name)
            for b in brands:
                if b["display_name"] == brand_name or b["name"] == brand_name:
                    return None, b["id"]
            return None, None

        loc_id = self.location_map.get(loc_name)
        if not loc_id:
            return None, None
        if allow_all_brands and brand_name in ("All Brands Combined", "All Active Restaurants"):
            return loc_id, None
        with contextlib.closing(get_connection()) as conn:
            row = conn.execute(
                "SELECT id FROM brands WHERE location_id = ? AND name = ? AND status = 'Active'",
                (loc_id, brand_name)
            ).fetchone()
        return loc_id, (row['id'] if row else None)

if __name__ == "__main__":
    try:
        app = GastroAnalyticsApp()
        app.mainloop()
    except Exception as fatal_err:
        log_error(f"Fatal Startup/Mainloop Error: {str(fatal_err)}")
    finally:
        try:
            plt.close("all")
        except Exception:
            pass
        sys.exit(0)
