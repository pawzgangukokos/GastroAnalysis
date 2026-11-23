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
                    "Please re-import your Wolt or POS CSV files via Data & Calendar -> Import CSV "
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
        self.geometry("780x620")
        self.transient(parent)
        self.configure(fg_color=PALETTE[theme_mode]["canvas"])
        
        scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=20, pady=20)
        
        guide_text = """
1. WHAT THIS APP DOES (SIMPLE OVERVIEW)
GastroAnalytics helps you understand how your kitchen and delivery channels perform:
- Orders & Kitchen Data: Shows completed orders, food items, revenue, ratings, busy hours, top dishes, and daily tables from delivery platforms (such as Bolt, Wolt, or POS exports).
- Payouts & Costs: Reads payout and invoice documents (PDF/CSV) so you can see gross sales, platform commissions, ads, discounts, refunds/deductions, net payout, and real cost share.
- Safety First: Importing the same file twice skips duplicates, and you can create or restore backups anytime.

2. STEP-BY-STEP WORKFLOW (FOR EVERYDAY USE)
Step A — Import Order Files:
1. Export order CSV files from Bolt, Wolt, or your POS/export folder.
2. Open "Data & Calendar" -> "Import CSV" (or folder import).
3. Duplicate orders are skipped automatically. Cancelled, rejected, refunded, or void orders do not count as completed sales.

Step B — Check the Operating Calendar:
1. Open "Data & Calendar" and review dates marked as Open, Closed, or Disrupted.
2. Mark days when the kitchen was closed or had major disruptions so daily averages and operating comparisons stay realistic.

Step C — Merge Duplicate Brand Names:
1. Delivery platforms sometimes spell the same brand in slightly different ways (for example, "Burger House" and "Burger House - Sliema").
2. Go to "Settings" -> "Restaurants & Brands" -> "Combine Duplicate Restaurant Names".
3. Pick the duplicate name as the source, choose your main target brand, and confirm. Existing orders stay safe and are reassigned cleanly.

Step D — Import Payouts & Review Finances:
1. Import payout/invoice PDFs or CSVs in the finances/payouts section.
2. Check brand mapping so each report links to the right restaurant brand.
3. Compare gross sales, total costs, cost share %, and expected/actual payouts across date ranges.

Step E — Export & Back Up:
1. Export daily tables or reports to CSV/Excel when sharing numbers or checking details.
2. Create a backup before big changes or end-of-week reviews. If anything ever goes wrong, restore from a valid backup safely.

3. FORMULAS & HOW NUMBERS ARE CALCULATED
All formulas below use straightforward arithmetic:

- Completed Sales (Revenue):
  Sum of order totals for non-cancelled/non-rejected orders in the selected period, location, and active brand(s).

- Average Order Value (AOV):
  AOV = Total Completed Sales / Number of Completed Orders
  (If Completed Orders = 0, AOV is N/A or 0.00.)

- 5-Star Order Share (%):
  FiveStarPct = (Number of Orders with Rating == 5 / Number of Rated Orders) * 100

- Rejected / Cancelled Orders & Lost Revenue:
  Rejected Orders = Count of orders with status in (rejected, cancelled, canceled, refunded, void)
  Lost Revenue = Sum of order totals across those rejected/cancelled orders

- Cost Share of Gross Sales (%):
  CostSharePct = (Total Costs and Losses Gross / Gross Sales Total) * 100

- Partial Period Overlap Ratio (when filtering payouts by Date From / Date To):
  Suppose a payout covers period [P_start, P_end] with L_period days (inclusive):
    L_period = (P_end - P_start) + 1 day
  Suppose your filter covers [F_start, F_end]. The overlapping days [O_start, O_end] are:
    O_start = max(P_start, F_start)
    O_end   = min(P_end, F_end)
    OverlapDays = max(0, (O_end - O_start) + 1 day)
    OverlapRatio = OverlapDays / L_period
  Scaled amount for the selected window:
    ScaledValue = PeriodValue * OverlapRatio

4. WORKED EXAMPLES (NUMBERS YOU CAN CHECK BY HAND)

Example 1 — Completed vs. Cancelled Orders & AOV:
Suppose on one day you have:
- Order #1: EUR 20.00, 2 items, status = delivered, rating = 5
- Order #2: EUR 30.00, 3 items, status = delivered, rating = 4
- Order #3: EUR 25.00, 2 items, status = cancelled, rating = empty
Result:
- Completed Orders = 2 (#1 and #2)
- Total Food Items = 2 + 3 = 5 (cancelled Order #3 items are excluded)
- Total Food Sales = 20.00 + 30.00 = EUR 50.00
- AOV = 50.00 / 2 = EUR 25.00
- Rejected Orders = 1; Lost Revenue = EUR 25.00
- 5-Star Order Share = (1 / 2) * 100 = 50.0% (out of 2 rated orders)

Example 2 — Partial Date Overlap on a Weekly Payout:
Suppose a weekly payout report covers 7 days: 2026-10-01 to 2026-10-07 (L_period = 7 days),
with Gross Sales = EUR 700.00 and Total Costs = EUR 70.00 (Payout = EUR 630.00).
You filter payouts from 2026-10-01 to 2026-10-03:
- Overlapping days = Oct 1, Oct 2, Oct 3 -> OverlapDays = 3 days
- OverlapRatio = 3 / 7
- Scaled Gross Sales = 700.00 * (3 / 7) = EUR 300.00
- Scaled Costs = 70.00 * (3 / 7) = EUR 30.00
- Scaled Payout = 630.00 * (3 / 7) = EUR 270.00
- Cost Share % = (30.00 / 300.00) * 100 = 10.0%

Example 3 — Multiple Virtual Brands in One Kitchen:
Suppose Brand A and Brand B cook in the same kitchen during the same 30-minute slot:
- Brand A has 4 orders; Brand B has 6 orders.
- Viewing a single brand shows only that brand's orders.
- Selecting "All Brands Combined" adds them together (4 + 6 = 10 orders in that slot) so kitchen/staffing load reflects the real combined volume.

5. QUICK TROUBLESHOOTING TIPS
- Numbers look lower than expected? Check the selected date range, active brand/location filter, and whether the day is marked Closed/Disrupted or orders were cancelled.
- Seeing a brand name twice? Merge the duplicate into the main brand under Settings -> Restaurants & Brands.
- Want to test safely? Keep a backup first and verify results against a known export file.
"""
        ctk.CTkLabel(scroll, text="Documentation", font=ctk.CTkFont(family=DISPLAY_FONT, size=20, weight="bold"), text_color=PALETTE[theme_mode]["text"]).pack(anchor="w", pady=(0, 10))
        ctk.CTkLabel(scroll, text=guide_text, justify="left", text_color=PALETTE[theme_mode]["text"], wraplength=700).pack(anchor="w")
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
        self.df = importer.sniff_csv(path)
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
            importer = CSVImporter()
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
        self._dirty_tabs = {"dashboard", "daily", "data", "finances", "settings"}

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
        sys.exit(0)

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
            ("data", "Data & Calendar"),
            ("finances", "Losses & Payouts"),
            ("settings", "Settings")
        ]
        for idx, (key, label) in enumerate(nav_items, 1):
            btn = ctk.CTkButton(self.sidebar, text=label, anchor="w", fg_color="transparent", text_color=t["text"], command=lambda k=key: self.show_tab(k))
            btn.grid(row=idx, column=0, padx=12, pady=2, sticky="ew")
            self.nav_buttons[key] = btn

        self.theme_btn = ctk.CTkButton(self.sidebar, text="Toggle Theme", command=self._toggle_theme, fg_color="transparent", text_color=t["text"], border_width=1, border_color=t["ghost_border"])
        self.theme_btn.grid(row=7, column=0, padx=14, pady=8, sticky="ew")

        self.guide_btn = ctk.CTkButton(self.sidebar, text="User Guide", command=lambda: UserGuideDialog(self, self.theme_mode), fg_color="transparent", text_color=t["text"], border_width=1, border_color=t["ghost_border"])
        self.guide_btn.grid(row=8, column=0, padx=14, pady=8, sticky="ew")

        self.restore_btn = ctk.CTkButton(self.sidebar, text="Restore Backup", command=lambda: RestoreBackupDialog(self, self.theme_mode, self._on_database_restored), fg_color="transparent", text_color=t["text"], border_width=1, border_color=t["ghost_border"])
        self.restore_btn.grid(row=9, column=0, padx=14, pady=(8, 20), sticky="ew")
        
        self.frames = {
            "dashboard": ctk.CTkFrame(self, fg_color="transparent"),
            "daily": ctk.CTkFrame(self, fg_color="transparent"),
            "data": ctk.CTkFrame(self, fg_color="transparent"),
            "finances": ctk.CTkFrame(self, fg_color="transparent"),
            "settings": ctk.CTkFrame(self, fg_color="transparent"),
        }
        self._build_dashboard_tab()
        self._build_daily_breakdown_tab()
        self._build_data_calendar_tab()
        self._build_finances_tab()
        self._build_settings_tab()

    def _toggle_theme(self):
        self._close_active_figures()
        self.theme_mode = "Dark" if self.theme_mode == "Light" else "Light"
        ctk.set_appearance_mode(self.theme_mode)
        for w in self.winfo_children():
            w.destroy()
        self._build_layout()
        self._populate_global_dropdowns()
        self.show_tab(self.current_tab)

    def mark_tabs_dirty(self, *tabs: str, clear_cache: bool = False):
        if clear_cache and hasattr(self, "invalidate_analytics_cache"):
            self.invalidate_analytics_cache()
        if not tabs:
            self._dirty_tabs = {"dashboard", "daily", "data", "finances", "settings"}
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
        elif tab_key == "data":
            self.refresh_calendar_table()
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
        ctk.CTkButton(top_bar, text="Export PDF", command=self.export_pdf_report, fg_color="transparent", border_width=1, border_color=t["ghost_border"], text_color=t["text"]).pack(side="right", padx=10)
        ctk.CTkButton(top_bar, text="Export .xlsx", command=self.export_excel_report, fg_color="transparent", border_width=1, border_color=t["ghost_border"], text_color=t["text"]).pack(side="right", padx=10)

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
            text="Export Daily CSV / Excel",
            command=self._export_daily_breakdown_file,
            fg_color="transparent",
            border_width=1,
            border_color=t["ghost_border"],
            text_color=t["text"]
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
    def _build_data_calendar_tab(self):
        t = self._t()
        parent = self.frames["data"]
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        cal_card = ctk.CTkFrame(parent, fg_color=t["card"])
        cal_card.grid(row=1, column=0, rowspan=2, sticky="nsew")
        cal_card.grid_columnconfigure(0, weight=1)
        cal_card.grid_rowconfigure(2, weight=1)
        
        cal_filter = ctk.CTkFrame(cal_card, fg_color="transparent")
        cal_filter.grid(row=0, column=0, sticky="ew", padx=14, pady=12)
        self.cal_location_cb = ctk.CTkComboBox(cal_filter, command=self._on_cal_location_change)
        self.cal_location_cb.pack(side="left", padx=4)
        self.cal_brand_cb = ctk.CTkComboBox(cal_filter)
        self.cal_brand_cb.pack(side="left", padx=4)
        
        self.cal_start_entry, self.cal_end_entry = ctk.CTkEntry(cal_filter, width=100), ctk.CTkEntry(cal_filter, width=100)
        self.cal_start_entry.insert(0, (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d"))
        self.cal_end_entry.insert(0, datetime.now().strftime("%Y-%m-%d"))
        self.cal_start_entry.pack(side="left", padx=4)
        self.cal_end_entry.pack(side="left", padx=4)
        ctk.CTkButton(cal_filter, text="Apply", command=self.refresh_calendar_table).pack(side="right")

        edit_bar = ctk.CTkFrame(cal_card, fg_color="transparent")
        edit_bar.grid(row=1, column=0, sticky="ew", padx=14, pady=10)
        
        self.cal_status_cb = ctk.CTkComboBox(edit_bar, values=["Open", "Closed", "Disrupted"], width=120)
        self.cal_status_cb.pack(side="left", padx=10)
        self.cal_open_entry = ctk.CTkEntry(edit_bar, width=62)
        self.cal_open_entry.insert(0, "10:00")
        self.cal_open_entry.pack(side="left", padx=4)
        self.cal_close_entry = ctk.CTkEntry(edit_bar, width=62)
        self.cal_close_entry.insert(0, "22:00")
        self.cal_close_entry.pack(side="left", padx=4)
        self.cal_notes_entry = ctk.CTkEntry(edit_bar, placeholder_text="Note", width=240)
        self.cal_notes_entry.pack(side="left", padx=8)
        ctk.CTkButton(edit_bar, text="Update Selected", command=self._apply_calendar_bulk_update).pack(side="right")

        cols = ("Date", "Weekday", "Status", "Hours", "Orders", "Notes")
        self.cal_tree = ttk.Treeview(cal_card, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            self.cal_tree.heading(c, text=c)
        self.cal_tree.grid(row=2, column=0, sticky="nsew", padx=14, pady=(0, 14))

    def _append_import_log(self, line: str):
        self.import_log_box.configure(state="normal")
        self.import_log_box.insert("end", line + "\n")
        self.import_log_box.see("end")
        self.import_log_box.configure(state="disabled")

    @safe_action
    def _one_click_import(self):
        filepaths = filedialog.askopenfilenames(title="Import CSV", filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")])
        if not filepaths:
            return

        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            importer = CSVImporter()
            for path in filepaths:
                try:
                    res = importer.auto_import_file(path)
                    self._append_import_log(f"{res['filename']}: Imported {res['inserted']} orders ({res['skipped']} duplicates skipped).")
                except UnrecognizedCSVFormatError as unrec:
                    self._append_import_log(f"{os.path.basename(path)}: Unrecognized format — opening mapper.")
                    self._open_custom_mapper(unrec.filepath)
        finally:
            self.configure(cursor="")

        self.invalidate_analytics_cache()
        self.mark_tabs_dirty()
        self._populate_global_dropdowns()
        self.refresh_calendar_table()

    def _open_custom_mapper(self, filepath):
        CustomMapperDialog(self, theme_mode=self.theme_mode, initial_filepath=filepath, on_imported_callback=self._on_custom_import_done)

    def _on_custom_import_done(self, res: dict):
        self._append_import_log(f"{res['filename']}: Imported {res['inserted']} orders ({res['skipped']} duplicates skipped).")
        self._populate_global_dropdowns()
        self.refresh_calendar_table()

    @safe_action
    def refresh_calendar_table(self):
        start_d, end_d = self.cal_start_entry.get().strip(), self.cal_end_entry.get().strip()
        if not validate_date(start_d) or not validate_date(end_d) or start_d > end_d:
            return
        
        self.cal_tree.delete(*self.cal_tree.get_children())
        loc_id, brand_id = self._get_selected_ids(self.cal_location_cb.get(), self.cal_brand_cb.get(), False)
        if not loc_id or not brand_id:
            return

        date_range = pd.date_range(start_d, end_d)
        with contextlib.closing(get_connection()) as conn:
            cal_rows = conn.execute("SELECT date, status, open_time, close_time, COALESCE(notes, '') as notes FROM operating_calendar WHERE location_id = ? AND brand_id = ? AND date BETWEEN ? AND ?", (loc_id, brand_id, start_d, end_d)).fetchall()
            ord_rows = conn.execute("SELECT date(order_datetime) as d, COUNT(*) as cnt FROM orders WHERE location_id = ? AND brand_id = ? AND LOWER(status) NOT IN ('rejected', 'cancelled', 'refunded', 'void') AND date(order_datetime) BETWEEN ? AND ? GROUP BY d", (loc_id, brand_id, start_d, end_d)).fetchall()
        
        cal_map = {r['date']: dict(r) for r in cal_rows}
        ord_map = {r['d']: r['cnt'] for r in ord_rows}
        for dt in date_range:
            d_str = dt.strftime("%Y-%m-%d")
            info = cal_map.get(d_str, {"status": "Open", "open_time": "10:00", "close_time": "22:00", "notes": ""})
            self.cal_tree.insert("", "end", iid=d_str, values=(d_str, dt.day_name(), info["status"], f"{info['open_time']}-{info['close_time']}", ord_map.get(d_str, 0), info["notes"]))

    @safe_action
    def _apply_calendar_bulk_update(self):
        selected = self.cal_tree.selection()
        if not selected:
            return
        loc_id, brand_id = self._get_selected_ids(self.cal_location_cb.get(), self.cal_brand_cb.get(), False)
        status = self.cal_status_cb.get()
        open_t, close_t = self.cal_open_entry.get().strip() or "10:00", self.cal_close_entry.get().strip() or "22:00"
        notes = self.cal_notes_entry.get().strip()

        with contextlib.closing(get_connection()) as conn:
            cur = conn.cursor()
            for d_str in selected:
                cur.execute("""
                    INSERT INTO operating_calendar (location_id, brand_id, date, status, open_time, close_time, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(location_id, brand_id, date) DO UPDATE SET
                        status = excluded.status, open_time = excluded.open_time, close_time = excluded.close_time, notes = excluded.notes
                """, (loc_id, brand_id, d_str, status, open_t, close_t, notes))
            conn.commit()
        self.invalidate_analytics_cache()
        self.mark_tabs_dirty()
        self.refresh_calendar_table()

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
            res = import_day_folder_recursive(folder)
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
        self.refresh_calendar_table()
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
        tab.grid_rowconfigure(3, weight=1)

        # Row 0: Locations (Groups) & Adding New Restaurants
        loc_brand_card = ctk.CTkFrame(tab, fg_color=t["canvas"])
        loc_brand_card.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 5))

        ctk.CTkLabel(loc_brand_card, text="New Location:", font=self.font_bold).pack(side="left", padx=(10, 4), pady=8)
        self.new_loc_entry = ctk.CTkEntry(loc_brand_card, placeholder_text="e.g. MSIDA", width=135)
        self.new_loc_entry.pack(side="left", padx=4, pady=8)
        ctk.CTkButton(loc_brand_card, text="Create Location", width=115, command=self._add_location).pack(side="left", padx=(4, 14), pady=8)

        ctk.CTkLabel(loc_brand_card, text="Add Restaurant:", font=self.font_bold).pack(side="left", padx=(4, 4), pady=8)
        self.new_brand_loc_cb = ctk.CTkComboBox(loc_brand_card, width=135)
        self.new_brand_loc_cb.pack(side="left", padx=4, pady=8)
        self.new_brand_name_entry = ctk.CTkEntry(loc_brand_card, placeholder_text="Restaurant Name", width=150)
        self.new_brand_name_entry.pack(side="left", padx=4, pady=8)
        ctk.CTkButton(loc_brand_card, text="Add", width=70, command=self._add_brand).pack(side="left", padx=4, pady=8)

        ctk.CTkButton(
            loc_brand_card,
            text="Delete Location",
            width=115,
            fg_color=t["danger"],
            hover_color=t["danger_hover"],
            command=self._delete_location
        ).pack(side="right", padx=(4, 10), pady=8)
        self.del_loc_cb = ctk.CTkComboBox(loc_brand_card, width=135)
        self.del_loc_cb.pack(side="right", padx=4, pady=8)

        # Row 1: Group / Move Selected Restaurants into a Location, Edit Name/Status, or Delete
        edit_brand_card = ctk.CTkFrame(tab, fg_color=t["canvas"])
        edit_brand_card.grid(row=1, column=0, sticky="ew", padx=10, pady=5)

        ctk.CTkLabel(edit_brand_card, text="Assign Selected to Location:", font=self.font_bold).pack(side="left", padx=(10, 4), pady=8)
        self.move_brand_loc_cb = ctk.CTkComboBox(edit_brand_card, width=140)
        self.move_brand_loc_cb.pack(side="left", padx=4, pady=8)
        ctk.CTkButton(edit_brand_card, text="Move Selected", width=110, command=self._move_selected_brands_to_location).pack(side="left", padx=(4, 16), pady=8)

        ctk.CTkLabel(edit_brand_card, text="Edit:", font=self.font_bold).pack(side="left", padx=(4, 4), pady=8)
        self.edit_brand_name_entry = ctk.CTkEntry(edit_brand_card, placeholder_text="Select row below", width=170)
        self.edit_brand_name_entry.pack(side="left", padx=4, pady=8)
        self.edit_brand_status_cb = ctk.CTkComboBox(edit_brand_card, values=["Active", "Inactive"], width=100)
        self.edit_brand_status_cb.pack(side="left", padx=4, pady=8)
        ctk.CTkButton(edit_brand_card, text="Save", width=75, command=self._update_selected_brand).pack(side="left", padx=4, pady=8)

        ctk.CTkButton(
            edit_brand_card,
            text="Delete Restaurant",
            width=130,
            fg_color=t["danger"],
            hover_color=t["danger_hover"],
            command=self._delete_brand
        ).pack(side="right", padx=10, pady=8)

        # Row 2: Simple Layman Wizard — Combine Duplicate Restaurant Names
        combine_card = ctk.CTkFrame(tab, fg_color=t["canvas"])
        combine_card.grid(row=2, column=0, sticky="ew", padx=10, pady=6)
        combine_card.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            combine_card,
            text="Combine Duplicate Restaurant Names",
            font=self.font_bold,
            text_color=t["text"]
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 2))

        ctk.CTkLabel(
            combine_card,
            text="If Wolt or Bolt spells your restaurant name differently (e.g. 'Burger House' and 'Burger House - City Center'), select the duplicate name below and merge it into your main brand. All past and future orders will automatically combine.",
            font=self.font_small,
            text_color=t["muted"],
            justify="left",
            wraplength=980
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

        combine_row = ctk.CTkFrame(combine_card, fg_color="transparent")
        combine_row.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 10))

        self.combine_dup_cb = ctk.CTkComboBox(combine_row, width=260)
        self.combine_dup_cb.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(combine_row, text="Merge into:", font=self.font_bold).pack(side="left", padx=6)

        self.combine_main_cb = ctk.CTkComboBox(combine_row, width=260)
        self.combine_main_cb.pack(side="left", padx=8)

        ctk.CTkButton(
            combine_row,
            text="Combine Names",
            width=140,
            command=self._combine_duplicate_names
        ).pack(side="left", padx=10)

        # Row 3: Brands Table (supports Ctrl/Shift multi-select to group multiple restaurants into one location)
        cols = ("ID", "Location", "Brand", "Status")
        self.brands_tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            self.brands_tree.heading(c, text=c)
        self.brands_tree.grid(row=3, column=0, sticky="nsew", padx=10, pady=10)
        self.brands_tree.bind("<<TreeviewSelect>>", self._on_brand_tree_select)

    def _on_brand_tree_select(self, _event=None):
        sel = self.brands_tree.selection()
        if not sel:
            return
        vals = self.brands_tree.item(sel[0], "values")
        self.edit_brand_name_entry.delete(0, "end")
        self.edit_brand_name_entry.insert(0, vals[2])
        self.edit_brand_status_cb.set(vals[3])

    @safe_action
    def _add_location(self):
        name = self.new_loc_entry.get().strip()
        if not name:
            messagebox.showwarning("Missing Name", "Enter a location name (e.g. MSIDA).")
            return
        with contextlib.closing(get_connection()) as conn:
            conn.execute("INSERT INTO locations (name) VALUES (?)", (name,))
            conn.commit()
        self.new_loc_entry.delete(0, "end")
        self._populate_global_dropdowns()
        self.refresh_all_settings()
        if hasattr(self, 'new_brand_loc_cb'):
            self.new_brand_loc_cb.set(name)
        if hasattr(self, 'move_brand_loc_cb'):
            self.move_brand_loc_cb.set(name)
        messagebox.showinfo("Location Created", f"Location '{name}' is ready. You can now add or move restaurants into '{name}'.")

    @safe_action
    def _delete_location(self):
        loc_name = self.del_loc_cb.get().strip()
        loc_id = self.location_map.get(loc_name)
        if not loc_id:
            messagebox.showwarning("No Selection", "Please select a valid location to delete.")
            return

        if not messagebox.askyesno(
            "Confirm Delete",
            f"Are you sure you want to delete location '{loc_name}'?\n\nThis will permanently delete all associated restaurants, orders, and calendar records for this location."
        ):
            return

        backup_db()
        with contextlib.closing(get_connection()) as conn:
            conn.execute("DELETE FROM order_items WHERE location_id = ?", (loc_id,))
            conn.execute("DELETE FROM locations WHERE id = ?", (loc_id,))
            conn.commit()

        self._populate_global_dropdowns()
        self.refresh_all_settings()
        messagebox.showinfo("Deleted", f"Location '{loc_name}' and all its data have been removed.")

    @safe_action
    def _add_brand(self):
        loc_name = self.new_brand_loc_cb.get().strip()
        brand_name = self.new_brand_name_entry.get().strip()
        loc_id = self.location_map.get(loc_name)
        if not loc_id or not brand_name:
            messagebox.showwarning("Missing Fields", "Select a valid location and enter a restaurant name.")
            return
        with contextlib.closing(get_connection()) as conn:
            conn.execute("INSERT INTO brands (location_id, name, status) VALUES (?, ?, 'Active')", (loc_id, brand_name))
            conn.commit()
        self.new_brand_name_entry.delete(0, "end")
        self._populate_global_dropdowns()
        self.refresh_all_settings()
        messagebox.showinfo("Restaurant Added", f"'{brand_name}' added to '{loc_name}'.")

    @safe_action
    def _move_selected_brands_to_location(self):
        sel = self.brands_tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select one or more restaurants in the table below (hold Ctrl/Cmd to select multiple).")
            return
        target_loc_name = self.move_brand_loc_cb.get().strip()
        target_loc_id = self.location_map.get(target_loc_name)
        if not target_loc_id:
            messagebox.showwarning("Invalid Location", "Select a valid target location.")
            return

        brand_ids = [int(self.brands_tree.item(item_id, "values")[0]) for item_id in sel]
        backup_db()
        with contextlib.closing(get_connection()) as conn:
            cur = conn.cursor()
            for bid in brand_ids:
                cur.execute("UPDATE brands SET location_id = ? WHERE id = ?", (target_loc_id, bid))
                cur.execute("UPDATE orders SET location_id = ? WHERE brand_id = ?", (target_loc_id, bid))
                cur.execute("UPDATE order_items SET location_id = ? WHERE brand_id = ?", (target_loc_id, bid))
                cur.execute("UPDATE platform_aliases SET location_id = ? WHERE brand_id = ?", (target_loc_id, bid))
                cur.execute("UPDATE operating_calendar SET location_id = ? WHERE brand_id = ?", (target_loc_id, bid))
            conn.commit()

        self._populate_global_dropdowns()
        self.refresh_all_settings()
        messagebox.showinfo("Grouped", f"Moved {len(brand_ids)} restaurant(s) to '{target_loc_name}'.")

    @safe_action
    def _delete_brand(self):
        sel = self.brands_tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Please select a restaurant in the table below to delete.")
            return
        vals = self.brands_tree.item(sel[0], "values")
        brand_id = int(vals[0])
        brand_name = vals[2]

        if not messagebox.askyesno(
            "Confirm Delete",
            f"Are you sure you want to delete restaurant '{brand_name}'?\n\nThis will permanently remove its orders, dish records, and calendar settings."
        ):
            return

        backup_db()
        with contextlib.closing(get_connection()) as conn:
            conn.execute("DELETE FROM order_items WHERE brand_id = ?", (brand_id,))
            conn.execute("DELETE FROM brands WHERE id = ?", (brand_id,))
            conn.commit()

        self.edit_brand_name_entry.delete(0, "end")
        self._populate_global_dropdowns()
        self.refresh_all_settings()
        messagebox.showinfo("Deleted", f"Restaurant '{brand_name}' has been deleted.")

    @safe_action
    def _update_selected_brand(self):
        sel = self.brands_tree.selection()
        if not sel:
            messagebox.showwarning("No Selection", "Select a restaurant row in the table first.")
            return
        vals = self.brands_tree.item(sel[0], "values")
        brand_id = int(vals[0])
        new_name = self.edit_brand_name_entry.get().strip()
        new_status = self.edit_brand_status_cb.get()
        if not new_name:
            messagebox.showwarning("Invalid Name", "Restaurant name cannot be empty.")
            return
        with contextlib.closing(get_connection()) as conn:
            conn.execute("UPDATE brands SET name = ?, status = ? WHERE id = ?", (new_name, new_status, brand_id))
            conn.commit()
        self._populate_global_dropdowns()
        self.refresh_all_settings()
        messagebox.showinfo("Saved", "Restaurant details updated.")

    @safe_action
    def _combine_duplicate_names(self):
        dup_label = self.combine_dup_cb.get().strip()
        main_label = self.combine_main_cb.get().strip()
        if dup_label not in self.duplicate_choice_map or main_label not in self.brand_merge_map:
            messagebox.showwarning("Selection Needed", "Please select both a duplicate name and a main brand.")
            return

        dup_info = self.duplicate_choice_map[dup_label]
        target_brand_id = self.brand_merge_map[main_label]
        source_brand_id = dup_info["brand_id"]

        if dup_info["kind"] == "brand" and source_brand_id == target_brand_id:
            messagebox.showwarning("Same Brand", "Please choose two different restaurant names to combine.")
            return

        if not messagebox.askyesno(
            "Combine Names",
            f"Combine '{dup_label}' into '{main_label}'?\n\nAll past and future orders will appear under '{main_label}'."
        ):
            return

        moved_orders = 0
        if dup_info["kind"] == "alias":
            backup_db()
            with contextlib.closing(get_connection()) as conn:
                cur = conn.cursor()
                target_row = cur.execute("SELECT location_id FROM brands WHERE id = ?", (target_brand_id,)).fetchone()
                if not target_row:
                    return
                target_loc_id = target_row["location_id"]
                venue_name = dup_info["csv_venue_name"]
                plat = dup_info["platform"]

                cur.execute(
                    "UPDATE platform_aliases SET brand_id = ?, location_id = ? WHERE id = ?",
                    (target_brand_id, target_loc_id, dup_info["alias_id"])
                )

                matching_orders = cur.execute(
                    "SELECT order_id, platform, brand_id FROM orders WHERE source_venue_name = ? AND platform = ?",
                    (venue_name, plat)
                ).fetchall()

                cur.execute("""
                    UPDATE OR IGNORE orders
                    SET brand_id = ?, location_id = ?
                    WHERE source_venue_name = ? AND platform = ?
                """, (target_brand_id, target_loc_id, venue_name, plat))
                moved_orders = cur.rowcount

                cur.execute("""
                    DELETE FROM orders
                    WHERE source_venue_name = ? AND platform = ? AND brand_id != ?
                """, (venue_name, plat, target_brand_id))

                for mo in matching_orders:
                    cur.execute("""
                        UPDATE OR IGNORE order_items
                        SET brand_id = ?, location_id = ?
                        WHERE order_id = ? AND platform = ? AND brand_id = ?
                    """, (target_brand_id, target_loc_id, mo["order_id"], mo["platform"], mo["brand_id"]))
                    cur.execute("""
                        DELETE FROM order_items
                        WHERE order_id = ? AND platform = ? AND brand_id = ? AND brand_id != ?
                    """, (mo["order_id"], mo["platform"], mo["brand_id"], target_brand_id))

                if source_brand_id != target_brand_id:
                    rem_orders = cur.execute("SELECT COUNT(*) FROM orders WHERE brand_id = ?", (source_brand_id,)).fetchone()[0]
                    rem_aliases = cur.execute("SELECT COUNT(*) FROM platform_aliases WHERE brand_id = ?", (source_brand_id,)).fetchone()[0]
                    if rem_orders == 0 and rem_aliases == 0:
                        cur.execute("DELETE FROM operating_calendar WHERE brand_id = ?", (source_brand_id,))
                        cur.execute("DELETE FROM brands WHERE id = ?", (source_brand_id,))

                conn.commit()
        else:
            moved_orders = merge_brands(source_brand_id, target_brand_id)

        self.refresh_all_settings()
        self._populate_global_dropdowns()
        messagebox.showinfo("Names Combined", f"Successfully combined into '{main_label}' ({moved_orders} past orders updated).")

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
        self.hr_tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.hr_tree.heading(c, text=c)
        self.hr_tree.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 12))
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
        self.shifts_tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        for c in cols:
            self.shifts_tree.heading(c, text=c)
        self.shifts_tree.grid(row=1, column=0, sticky="nsew", padx=12, pady=(6, 12))
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
            for cb_attr in ('new_brand_loc_cb', 'move_brand_loc_cb', 'del_loc_cb'):
                if hasattr(self, cb_attr):
                    cb_widget = getattr(self, cb_attr)
                    cb_widget.configure(values=loc_names if loc_names else [""])
                    if loc_names:
                        if cb_widget.get() not in loc_names:
                            cb_widget.set(loc_names[0])
                    else:
                        cb_widget.set("")

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

    def _populate_global_dropdowns(self):
        with contextlib.closing(get_connection()) as conn:
            locs = conn.execute("SELECT id, name FROM locations ORDER BY name").fetchall()
        self.location_map = {r['name']: r['id'] for r in locs}
        single_loc_names = list(self.location_map.keys()) if locs else ["No Locations"]
        multi_loc_names = (["All Locations (All Active)"] + list(self.location_map.keys())) if locs else ["No Locations"]

        self.dash_location_cb.configure(values=multi_loc_names)
        self.daily_location_cb.configure(values=multi_loc_names)
        self.cal_location_cb.configure(values=single_loc_names)

        if self.dash_location_cb.get() not in multi_loc_names:
            self.dash_location_cb.set(multi_loc_names[0])
        if self.daily_location_cb.get() not in multi_loc_names:
            self.daily_location_cb.set(multi_loc_names[0])
        if self.cal_location_cb.get() not in single_loc_names:
            self.cal_location_cb.set(single_loc_names[0])

        self._on_dash_location_change(self.dash_location_cb.get())
        self._on_daily_location_change(self.daily_location_cb.get())
        self._on_cal_location_change(self.cal_location_cb.get())

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
        self.dash_brand_cb.configure(values=[all_label] + [b['display_name'] for b in brands])
        self.dash_brand_cb.set(all_label)

    def _on_cal_location_change(self, loc_name: str):
        brands = self._get_brands_for_location(loc_name)
        vals = [b['name'] for b in brands] if brands else ["No Active Brands"]
        self.cal_brand_cb.configure(values=vals)
        self.cal_brand_cb.set(vals[0])


    def _build_finances_tab(self):
        parent = self.frames["finances"]
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        top_bar = ctk.CTkFrame(parent)
        top_bar.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))

        ctk.CTkLabel(top_bar, text="From (YYYY-MM-DD):").pack(side="left", padx=(10, 4), pady=8)
        self.fin_date_from = ctk.CTkEntry(top_bar, width=110)
        self.fin_date_from.pack(side="left", padx=(0, 10), pady=8)

        ctk.CTkLabel(top_bar, text="To:").pack(side="left", padx=(0, 4), pady=8)
        self.fin_date_to = ctk.CTkEntry(top_bar, width=110)
        self.fin_date_to.pack(side="left", padx=(0, 10), pady=8)

        ctk.CTkLabel(top_bar, text="Platform:").pack(side="left", padx=(0, 4), pady=8)
        self.fin_platform_var = ctk.StringVar(value="All")
        self.fin_platform_menu = ctk.CTkOptionMenu(
            top_bar,
            values=["All", "Bolt Food", "Wolt"],
            variable=self.fin_platform_var,
            width=120,
            command=lambda _v: self.refresh_finances_tab(),
        )
        self.fin_platform_menu.pack(side="left", padx=(0, 10), pady=8)

        ctk.CTkButton(top_bar, text="Apply", width=80, command=self.refresh_finances_tab).pack(
            side="left", padx=(0, 8), pady=8
        )


        cards_frame = ctk.CTkFrame(parent)
        cards_frame.grid(row=1, column=0, sticky="ew", padx=16, pady=(0, 10))
        for c in range(3):
            cards_frame.grid_columnconfigure(c, weight=1)

        self.fin_cards = {}
        card_specs = [
            ("gross_sales", "Gross Sales (incl. VAT)"),
            ("service_fees", "Service Fees & Commissions"),
            ("total_losses", "Total Costs & Losses"),
        ]
        for idx, (key, title) in enumerate(card_specs):
            box = ctk.CTkFrame(cards_frame)
            box.grid(row=0, column=idx, sticky="nsew", padx=6, pady=8)
            ctk.CTkLabel(box, text=title, font=ctk.CTkFont(size=12, weight="bold")).pack(
                anchor="w", padx=10, pady=(8, 2)
            )
            val_lbl = ctk.CTkLabel(box, text="0.00 EUR", font=ctk.CTkFont(size=18, weight="bold"))
            val_lbl.pack(anchor="w", padx=10, pady=(0, 2))
            sub_lbl = ctk.CTkLabel(box, text="0.00% of sales", font=ctk.CTkFont(size=11))
            sub_lbl.pack(anchor="w", padx=10, pady=(0, 8))
            self.fin_cards[key] = (val_lbl, sub_lbl)

        table_frame = ctk.CTkFrame(parent)
        table_frame.grid(row=2, column=0, sticky="nsew", padx=16, pady=(0, 14))
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_rowconfigure(0, weight=1)

        cols = ("brand", "platform", "reports", "sales", "ads", "comm_fees", "other", "losses", "share", "payout")
        self.fin_tree = ttk.Treeview(table_frame, columns=cols, show="headings", height=12)
        headings = {
            "brand": "Brand",
            "platform": "Platform",
            "reports": "Reports",
            "sales": "Gross Sales",
            "ads": "Ads Loss",
            "comm_fees": "Fees & Comm.",
            "other": "Other Costs",
            "losses": "Total Losses",
            "share": "Loss %",
            "payout": "Payout",
        }
        for col, text in headings.items():
            self.fin_tree.heading(col, text=text)
            self.fin_tree.column(col, width=105, anchor="e" if col not in ("brand", "platform") else "w")

        self.fin_tree.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)

    @safe_action
    def _import_payout_folder_recursive(self):
        folder = filedialog.askdirectory(title="Select folder with Bolt Food / Wolt payout reports")
        if not folder:
            return
        res = import_payouts_recursive(folder, create_backup=True)
        self.refresh_finances_tab()
        messagebox.showinfo(
            "Payout Import Summary",
            f"Found: {res['found']}\nImported: {res['imported']}\n"
            f"Duplicates skipped: {res['skipped_duplicates']}\n"
            f"Warnings: {res['warnings_count']}\nErrors: {res['errors_count']}",
        )

    @safe_action
    def _import_day_folder_all(self):
        folder = filedialog.askdirectory(title="Select day folder with sales/orders and payout reports")
        if not folder:
            return
        res = import_day_folder_recursive(folder)
        p_res = res.get("payouts", {}) if isinstance(res, dict) else {}
        o_res = res.get("orders", {}) if isinstance(res, dict) else {}
        p_errs = p_res.get("errors", []) or []
        o_errs = o_res.get("errors", []) or []
        self.refresh_finances_tab()
        messagebox.showinfo(
            "Day Folder Import Summary",
            f"Folder: {res.get('folder', folder)}\n"
            f"Payouts/Invoices imported: {p_res.get('imported', 0)} "
            f"(duplicates: {p_res.get('skipped_duplicates', p_res.get('duplicates', 0))})\n"
            f"Orders/Sales files imported: {o_res.get('imported', 0)} "
            f"(duplicates: {o_res.get('duplicates', 0)})\n"
            f"Errors: {len(p_errs) + len(o_errs)}",
        )

    @safe_action
    def refresh_finances_tab(self, force: bool = False):
        if not hasattr(self, "fin_cards"):
            return
        d_from = self.fin_date_from.get().strip() or None
        d_to = self.fin_date_to.get().strip() or None
        plat = self.fin_platform_var.get()
        fin_key = (d_from, d_to, plat, self.theme_mode)
        if (
            not force
            and getattr(self, "_last_finances_key", None) == fin_key
            and hasattr(self, "fin_tree")
            and len(self.fin_tree.get_children()) > 0
        ):
            return

        summary = get_loss_summary(date_from=d_from, date_to=d_to, platform=plat)
        sales = summary["gross_sales_total"]

        self.fin_cards["gross_sales"][0].configure(text=f"{sales:,.2f} EUR")
        self.fin_cards["gross_sales"][1].configure(text=f"Reports: {summary['reports_count']}")

        self.fin_cards["service_fees"][0].configure(text=f"{summary['service_fees_loss_gross']:,.2f} EUR")
        self.fin_cards["service_fees"][1].configure(text=f"{summary['service_fees_loss_pct']:.2f}% of sales")

        self.fin_cards["total_losses"][0].configure(text=f"{summary['total_losses_gross']:,.2f} EUR")
        self.fin_cards["total_losses"][1].configure(text=f"{summary['total_loss_pct']:.2f}% of sales")

        existing_rows = self.fin_tree.get_children()
        if existing_rows:
            self.fin_tree.delete(*existing_rows)
        self._last_finances_key = fin_key

        df = get_payout_breakdown_by_brand_and_platform(date_from=d_from, date_to=d_to, platform=plat)
        for _, r in df.iterrows():
            other_c = round(
                float(r["merchant_discounts_gross"])
                + float(r["deductions_refunds_gross"])
                + float(r["other_adjustments_gross"]),
                2,
            )
            self.fin_tree.insert(
                "",
                "end",
                values=(
                    r["brand_name"],
                    r["platform"],
                    int(r["reports_count"]),
                    f"{float(r['gross_sales_total']):,.2f}",
                    f"{float(r['ads_campaigns_gross']):,.2f}",
                    f"{float(r['commissions_fees_gross']):,.2f}",
                    f"{other_c:,.2f}",
                    f"{float(r['total_costs_gross']):,.2f}",
                    f"{float(r['cost_share_pct']):.2f}%",
                    f"{float(r['payout_amount_gross']):,.2f}",
                ),
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
