import pandas as pd
import numpy as np
import sqlite3
import textwrap
from datetime import datetime
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from contextlib import closing
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from database import get_connection, log_error


def get_location_date_bounds(location_id: int = None) -> tuple[str, str]:
    """Returns (min_date, max_date) across orders and operating_calendar for a location (or all locations if None)."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    with closing(get_connection()) as conn:
        if location_id is None:
            row = conn.execute(
                """
                SELECT MIN(d) as min_d, MAX(d) as max_d FROM (
                    SELECT date(o.order_datetime) as d
                    FROM orders o
                    JOIN brands b ON o.brand_id = b.id
                    WHERE b.status = 'Active'
                    UNION ALL
                    SELECT oc.date as d
                    FROM operating_calendar oc
                    JOIN brands b ON oc.brand_id = b.id
                    WHERE b.status = 'Active'
                )
                """
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT MIN(d) as min_d, MAX(d) as max_d FROM (
                    SELECT date(o.order_datetime) as d
                    FROM orders o
                    JOIN brands b ON o.brand_id = b.id
                    WHERE o.location_id = ? AND b.status = 'Active'
                    UNION ALL
                    SELECT oc.date as d
                    FROM operating_calendar oc
                    JOIN brands b ON oc.brand_id = b.id
                    WHERE oc.location_id = ? AND b.status = 'Active'
                )
                """,
                (location_id, location_id),
            ).fetchone()
        if row and row["min_d"] and row["max_d"]:
            return str(row["min_d"]), str(row["max_d"])
    return today_str, today_str


class GastroAnalytics:
    def __init__(self, start_date: str, end_date: str, location_id: int = None, brand_id: int = None):
        self.start_date = start_date
        self.end_date = end_date
        self.location_id = location_id
        self.brand_id = brand_id
        
        self.calendar_df = pd.DataFrame()
        self.open_dates = pd.Series(dtype=str)
        self._load_data()

    def _load_data(self):
        start_bound = f"{self.start_date} 00:00:00"
        end_bound = f"{self.end_date} 23:59:59.999999"

        with closing(get_connection()) as conn:
            # 1. Operating Calendar
            cal_query = """
                SELECT oc.date, oc.status, oc.brand_id
                FROM operating_calendar oc
                JOIN brands b ON oc.brand_id = b.id
                WHERE b.status = 'Active' AND oc.date BETWEEN ? AND ?
            """
            cal_params = [self.start_date, self.end_date]
            if self.location_id is not None:
                cal_query += " AND oc.location_id = ?"
                cal_params.append(self.location_id)
            if self.brand_id:
                cal_query += " AND oc.brand_id = ?"
                cal_params.append(self.brand_id)

            self.calendar_df = pd.read_sql_query(cal_query, conn, params=tuple(cal_params))

            if not self.calendar_df.empty:
                if self.brand_id:
                    open_days = self.calendar_df[self.calendar_df['status'] == 'Open']['date'].unique().tolist()
                    closed_days = self.calendar_df[self.calendar_df['status'].isin(['Closed', 'Disrupted'])]['date'].unique().tolist()
                else:
                    open_mask = self.calendar_df['status'] == 'Open'
                    open_days = self.calendar_df[open_mask]['date'].unique().tolist()
                    closed_mask = self.calendar_df['status'].isin(['Closed', 'Disrupted'])
                    closed_set = set(open_days)
                    closed_days = [d for d in self.calendar_df[closed_mask]['date'].unique() if d not in closed_set]
            else:
                open_days = []
                closed_days = []

            # 2. Load ALL Orders
            ord_query = """
                SELECT o.order_id, o.brand_id, o.platform, o.order_datetime, o.status,
                       o.items_count, o.total_sales, o.rating, b.name as brand_name
                FROM orders o
                JOIN brands b ON o.brand_id = b.id
                WHERE b.status = 'Active'
                  AND date(o.order_datetime) BETWEEN ? AND ?
            """
            ord_params = [self.start_date, self.end_date]
            if self.location_id is not None:
                ord_query += " AND o.location_id = ?"
                ord_params.append(self.location_id)
            if self.brand_id:
                ord_query += " AND o.brand_id = ?"
                ord_params.append(self.brand_id)

            raw_df = pd.read_sql_query(ord_query, conn, params=tuple(ord_params))

            if not raw_df.empty:
                dt = pd.to_datetime(raw_df['order_datetime'], format='mixed')
                raw_df['order_datetime'] = dt
                raw_df['date'] = dt.dt.strftime('%Y-%m-%d')
                raw_df['time_str'] = dt.dt.strftime('%H:%M')
                raw_df['hour'] = dt.dt.hour
                raw_df['weekday'] = dt.dt.day_name()
                raw_df['slot_30m'] = dt.dt.floor('30min').dt.strftime('%H:%M')

                rej_mask = raw_df['status'].str.lower().isin(['rejected', 'cancelled', 'canceled', 'refunded', 'void', 'odrzucone', 'odrzucony', 'anulowane', 'anulowany', 'zwrocone'])
                self.rejected_df = raw_df.loc[rej_mask].copy()
                completed_df = raw_df.loc[~rej_mask]

                completed_dates = completed_df['date'].unique().tolist()
                if not open_days and not closed_days:
                    open_days = completed_dates
                else:
                    open_days = sorted(set(open_days) | set(completed_dates))

                self.open_dates = pd.Series(open_days, dtype=str)
                open_set = set(open_days)
                self.orders_df = completed_df.copy()
            else:
                self.rejected_df = pd.DataFrame()
                self.orders_df = pd.DataFrame()
                self.open_dates = pd.Series(dtype=str)
                open_set = set()

            # 3. Load Order Items directly from order_items (full-key rejected-order exclusion)
            rej_pairs_query = """
                SELECT DISTINCT o.location_id, o.brand_id, o.platform, o.order_id
                FROM orders o
                JOIN brands b ON o.brand_id = b.id
                WHERE LOWER(TRIM(COALESCE(b.status, 'active'))) = 'active'
                  AND LOWER(TRIM(COALESCE(o.status, ''))) IN ('rejected', 'cancelled', 'canceled', 'refunded', 'void', 'odrzucone', 'odrzucony', 'anulowane', 'anulowany', 'zwrocone')
                  AND date(o.order_datetime) BETWEEN ? AND ?
            """
            rej_params = [self.start_date, self.end_date]
            if self.location_id is not None:
                rej_params.append(self.location_id)
                rej_pairs_query += " AND o.location_id = ?"
            if self.brand_id:
                rej_params.append(self.brand_id)
                rej_pairs_query += " AND o.brand_id = ?"

            rej_rows = conn.execute(rej_pairs_query, tuple(rej_params)).fetchall()
            rejected_keys = {(r[0], r[1], r[2], r[3]) for r in rej_rows}

            items_query = """
                SELECT oi.location_id, oi.brand_id, oi.platform, oi.order_id, oi.item_name, oi.quantity, oi.order_datetime
                FROM order_items oi
                JOIN brands b ON oi.brand_id = b.id
                WHERE b.status = 'Active'
                  AND date(oi.order_datetime) BETWEEN ? AND ?
            """
            items_params = [self.start_date, self.end_date]
            if self.location_id is not None:
                items_query += " AND oi.location_id = ?"
                items_params.append(self.location_id)
            if self.brand_id:
                items_query += " AND oi.brand_id = ?"
                items_params.append(self.brand_id)

            raw_dishes_df = pd.read_sql_query(items_query, conn, params=tuple(items_params))
            if not raw_dishes_df.empty:
                if rejected_keys:
                    keep_mask = [
                        (lid, bid, plat, oid) not in rejected_keys
                        for lid, bid, plat, oid in zip(
                            raw_dishes_df['location_id'],
                            raw_dishes_df['brand_id'],
                            raw_dishes_df['platform'],
                            raw_dishes_df['order_id'],
                        )
                    ]
                    raw_dishes_df = raw_dishes_df.loc[keep_mask]
                if not raw_dishes_df.empty:
                    raw_dishes_df = raw_dishes_df.copy()
                    raw_dishes_df['date'] = pd.to_datetime(raw_dishes_df['order_datetime'], format='mixed').dt.strftime('%Y-%m-%d')
                    if open_set:
                        self.dishes_df = raw_dishes_df.loc[raw_dishes_df['date'].isin(open_set)].copy()
                    else:
                        self.dishes_df = raw_dishes_df.iloc[0:0].copy()
                else:
                    self.dishes_df = pd.DataFrame(columns=['item_name', 'quantity', 'order_datetime', 'date'])
            else:
                self.dishes_df = raw_dishes_df

            # 4. Shifts & Staffing Rules
            self.shifts_df = pd.read_sql_query("SELECT name, start_time, end_time FROM meal_shifts ORDER BY start_time", conn)
            self.rules_df = pd.read_sql_query("SELECT min_load, max_load, metric, suggested_cooks, label FROM staffing_rules ORDER BY metric, min_load", conn)

    def get_top_dishes_list(self, limit: int = 10) -> list[tuple[str, int]]:
        """Returns sorted [(dish_name, sold_qty), ...] descending, or [] if no dish names exist."""
        if self.dishes_df.empty:
            return []
        cleaned = self.dishes_df.dropna(subset=['item_name']).copy()
        cleaned['item_name'] = cleaned['item_name'].astype(str).str.strip()
        cleaned = cleaned[cleaned['item_name'] != '']
        if cleaned.empty:
            return []
        top_df = cleaned.groupby('item_name')['quantity'].sum().sort_values(ascending=False).head(limit)
        return [(str(name), int(qty)) for name, qty in top_df.items()]

    def get_top_dishes(self, limit: int = 10) -> list[tuple[str, int]]:
        return self.get_top_dishes_list(limit=limit)

    def get_daily_breakdown(self, all_days_in_db: bool = False) -> pd.DataFrame:
        """
        Returns a per-day, per-brand summary table with columns:
        Date, Weekday, Restaurant / Brand, Status, Completed Orders, Food Items,
        Revenue (€), Avg Order Value (€), Avg Rating, Rejected Orders, Lost Revenue (€)
        """
        columns = [
            "Date", "Weekday", "Restaurant / Brand", "Status",
            "Completed Orders", "Food Items", "Revenue (€)",
            "Avg Order Value (€)", "Avg Rating", "Rejected Orders", "Lost Revenue (€)"
        ]
        with closing(get_connection()) as conn:
            brand_query = """
                SELECT b.id, b.name, l.name as loc_name
                FROM brands b
                JOIN locations l ON b.location_id = l.id
                WHERE b.status = 'Active'
            """
            brand_params = []
            if self.location_id is not None:
                brand_query += " AND b.location_id = ?"
                brand_params.append(self.location_id)
            if self.brand_id:
                brand_query += " AND b.id = ?"
                brand_params.append(self.brand_id)
            brands_df = pd.read_sql_query(brand_query, conn, params=tuple(brand_params))
            if brands_df.empty:
                return pd.DataFrame(columns=columns)
            if self.location_id is None:
                brand_name_map = {
                    int(r['id']): f"{r['name']} ({r['loc_name']})"
                    for _, r in brands_df.iterrows()
                }
            else:
                brand_name_map = dict(zip(brands_df['id'], brands_df['name']))

            ord_query = """
                SELECT o.brand_id, b.name as brand_name, date(o.order_datetime) as date,
                       o.status, o.items_count, o.total_sales, o.rating
                FROM orders o
                JOIN brands b ON o.brand_id = b.id
                WHERE b.status = 'Active'
            """
            ord_params = []
            if self.location_id is not None:
                ord_query += " AND o.location_id = ?"
                ord_params.append(self.location_id)
            if self.brand_id:
                ord_query += " AND o.brand_id = ?"
                ord_params.append(self.brand_id)
            if not all_days_in_db:
                ord_query += " AND date(o.order_datetime) BETWEEN ? AND ?"
                ord_params.extend([self.start_date, self.end_date])

            orders_raw = pd.read_sql_query(ord_query, conn, params=tuple(ord_params))

            cal_query = """
                SELECT oc.brand_id, oc.date, oc.status
                FROM operating_calendar oc
                JOIN brands b ON oc.brand_id = b.id
                WHERE b.status = 'Active'
            """
            cal_params = []
            if self.location_id is not None:
                cal_query += " AND oc.location_id = ?"
                cal_params.append(self.location_id)
            if self.brand_id:
                cal_query += " AND oc.brand_id = ?"
                cal_params.append(self.brand_id)
            if not all_days_in_db:
                cal_query += " AND oc.date BETWEEN ? AND ?"
                cal_params.extend([self.start_date, self.end_date])

            cal_raw = pd.read_sql_query(cal_query, conn, params=tuple(cal_params))

        cal_status_map = {}
        if not cal_raw.empty:
            for _, crow in cal_raw.iterrows():
                cal_status_map[(int(crow['brand_id']), str(crow['date']))] = str(crow['status'])

        pairs = set()
        if not orders_raw.empty:
            for _, orow in orders_raw[['brand_id', 'date']].drop_duplicates().iterrows():
                pairs.add((int(orow['brand_id']), str(orow['date'])))
        if not cal_raw.empty:
            for _, crow in cal_raw[['brand_id', 'date']].drop_duplicates().iterrows():
                pairs.add((int(crow['brand_id']), str(crow['date'])))

        if not pairs:
            return pd.DataFrame(columns=columns)

        if not orders_raw.empty:
            rejected_statuses = {
                "rejected", "cancelled", "canceled", "refunded", "void",
                "odrzucone", "odrzucony", "anulowane", "anulowany", "zwrocone",
            }
            rej_mask = orders_raw['status'].astype(str).str.strip().str.lower().isin(rejected_statuses)
            comp_df = orders_raw[~rej_mask].copy()
            rej_df = orders_raw[rej_mask].copy()
        else:
            comp_df = pd.DataFrame()
            rej_df = pd.DataFrame()

        comp_grp = {}
        if not comp_df.empty:
            for (bid, d_str), grp in comp_df.groupby(['brand_id', 'date']):
                rated = grp['rating'].dropna()
                items_num = pd.to_numeric(grp['items_count'], errors='coerce').fillna(0)
                sales_num = pd.to_numeric(grp['total_sales'], errors='coerce').fillna(0.0)
                comp_grp[(int(bid), str(d_str))] = {
                    "orders": int(len(grp)),
                    "items": int(items_num.sum()),
                    "revenue": float(sales_num.sum()),
                    "rating": round(float(rated.mean()), 2) if not rated.empty else None
                }

        rej_grp = {}
        if not rej_df.empty:
            for (bid, d_str), grp in rej_df.groupby(['brand_id', 'date']):
                rej_grp[(int(bid), str(d_str))] = {
                    "rej_orders": int(len(grp)),
                    "lost_rev": float(grp['total_sales'].sum())
                }

        weekday_cache = {}
        rows = []
        for bid, d_str in sorted(pairs, key=lambda x: (x[1], brand_name_map.get(x[0], "")), reverse=True):
            b_name = brand_name_map.get(bid, f"Brand #{bid}")
            status = cal_status_map.get((bid, d_str), "Open")
            c_info = comp_grp.get((bid, d_str), {"orders": 0, "items": 0, "revenue": 0.0, "rating": None})
            r_info = rej_grp.get((bid, d_str), {"rej_orders": 0, "lost_rev": 0.0})

            comp_orders = c_info["orders"]
            rev = round(c_info["revenue"], 2)
            aov = round(rev / comp_orders, 2) if comp_orders > 0 else 0.00
            rating_disp = f"{c_info['rating']:.2f}" if c_info["rating"] is not None else "—"
            if d_str not in weekday_cache:
                weekday_cache[d_str] = datetime.strptime(d_str, "%Y-%m-%d").strftime("%A")
            weekday_str = weekday_cache[d_str]

            rows.append({
                "Date": d_str,
                "Weekday": weekday_str,
                "Restaurant / Brand": b_name,
                "Status": status,
                "Completed Orders": comp_orders,
                "Food Items": c_info["items"],
                "Revenue (€)": rev,
                "Avg Order Value (€)": aov,
                "Avg Rating": rating_disp,
                "Rejected Orders": r_info["rej_orders"],
                "Lost Revenue (€)": round(r_info["lost_rev"], 2)
            })

        return pd.DataFrame(rows, columns=columns)

    def get_kpis(self) -> dict:
        rej_count = len(self.rejected_df)
        lost_rev = round(float(self.rejected_df['total_sales'].sum()), 2) if rej_count > 0 else 0.0
        if rej_count > 0:
            peak_rej_hr = int(self.rejected_df['hour'].value_counts().idxmax())
            peak_rej_label = f"{str(peak_rej_hr).zfill(2)}:00-{str((peak_rej_hr + 1) % 24).zfill(2)}:00"
        else:
            peak_rej_label = "None"

        if self.orders_df.empty or self.open_dates.empty:
            base = self._empty_kpis()
            base["Rejected Orders"] = rej_count
            base["Lost Revenue"] = lost_rev
            base["Peak Rejection Hour"] = peak_rej_label
            return base

        total_orders = len(self.orders_df)
        total_items = int(self.orders_df['items_count'].sum())
        total_sales = float(self.orders_df['total_sales'].sum())
        avg_order_value = total_sales / total_orders if total_orders > 0 else 0.0

        daily_grp = self.orders_df.groupby('date').agg(
            orders=('order_id', 'count'),
            items=('items_count', 'sum')
        ).reindex(self.open_dates, fill_value=0)

        max_orders = daily_grp['orders'].max()
        min_orders = daily_grp['orders'].min()
        busiest_dates = daily_grp[daily_grp['orders'] == max_orders].index.tolist()
        quietest_dates = daily_grp[daily_grp['orders'] == min_orders].index.tolist()

        open_days_dt = pd.to_datetime(self.open_dates)
        weekday_open_counts = open_days_dt.dt.day_name().value_counts()
        weekday_orders = self.orders_df.groupby('weekday')['order_id'].count()

        avg_by_weekday = {}
        for day in ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']:
            open_count = weekday_open_counts.get(day, 0)
            orders = weekday_orders.get(day, 0)
            avg_by_weekday[day] = orders / open_count if open_count > 0 else 0

        best_weekday = max(avg_by_weekday, key=avg_by_weekday.get) if avg_by_weekday else "N/A"
        worst_weekday = min(avg_by_weekday, key=avg_by_weekday.get) if avg_by_weekday else "N/A"

        rated_orders = self.orders_df.dropna(subset=['rating'])
        overall_rating = float(rated_orders['rating'].mean()) if not rated_orders.empty else 0.0
        total_rated_count = len(rated_orders)
        five_star_pct = (len(rated_orders[rated_orders['rating'] == 5.0]) / total_rated_count * 100) if total_rated_count > 0 else 0.0

        rush_rating, offpeak_rating = self._calculate_rush_metrics(rated_orders, 'rating')

        top_dishes_list = self.get_top_dishes_list(10)
        top_dishes_str = " | ".join([f"{name} ({qty})" for name, qty in top_dishes_list]) if top_dishes_list else "No dish data"

        return {
            "Total Orders": total_orders,
            "Total Food Items": total_items,
            "Total Food Sales (€)": round(total_sales, 2),
            "Average Order Value (€)": round(avg_order_value, 2),
            "Busiest Date(s)": f"{', '.join(busiest_dates)} ({int(max_orders)} orders)",
            "Quietest Trading Date(s)": f"{', '.join(quietest_dates)} ({int(min_orders)} orders)",
            "Best Weekday (Avg)": best_weekday,
            "Worst Weekday (Avg)": worst_weekday,
            "Overall Average Rating": round(overall_rating, 2) if total_rated_count > 0 else "N/A",
            "Total Rated Orders": total_rated_count,
            "% 5-Star Orders": round(five_star_pct, 1),
            "Rating - Peak Rush": round(rush_rating, 2) if rush_rating else "N/A",
            "Rating - Off-Peak": round(offpeak_rating, 2) if offpeak_rating else "N/A",
            "Quality Drop Indicator": round(offpeak_rating - rush_rating, 2) if rush_rating and offpeak_rating else "N/A",
            "Rejected Orders": rej_count,
            "Lost Revenue": lost_rev,
            "Peak Rejection Hour": peak_rej_label,
            "Top Selling Dishes": top_dishes_str,
            "Top Dishes List": top_dishes_list
        }

    def _calculate_rush_metrics(self, df: pd.DataFrame, column: str):
        if df.empty or self.shifts_df.empty:
            return None, None

        rush_shifts = self.shifts_df[self.shifts_df['name'].str.contains('Rush', case=False)]
        if rush_shifts.empty:
            return None, None

        rush_windows = [
            (str(shift['start_time']), str(shift['end_time']))
            for _, shift in rush_shifts.iterrows()
        ]

        def is_rush(time_val):
            time_str = time_val if isinstance(time_val, str) else time_val.strftime('%H:%M')
            for start_t, end_t in rush_windows:
                if start_t <= end_t:
                    if start_t <= time_str <= end_t:
                        return True
                else:
                    if time_str >= start_t or time_str <= end_t:
                        return True
            return False

        df = df.copy()
        time_col = 'time_str' if 'time_str' in df.columns else 'time'
        df['is_rush'] = df[time_col].apply(is_rush)

        rush_avg = df[df['is_rush'] == True][column].mean()
        offpeak_avg = df[df['is_rush'] == False][column].mean()

        return (rush_avg if not pd.isna(rush_avg) else None,
                offpeak_avg if not pd.isna(offpeak_avg) else None)

    def get_shift_for_time(self, time_str: str) -> str:
        if self.shifts_df.empty:
            return ""
        for _, shift in self.shifts_df.iterrows():
            st, et = shift['start_time'], shift['end_time']
            if st <= et:
                if st <= time_str <= et:
                    return shift['name']
            else:
                if time_str >= st or time_str <= et:
                    return shift['name']
        return "Off-Hours"

    def get_hr_heatmap(self, metric: str = 'Orders') -> pd.DataFrame:
        days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        times = [f"{str(h).zfill(2)}:{str(m).zfill(2)}" for h in range(24) for m in (0, 30)]

        matrix = pd.DataFrame(index=times, columns=days).fillna(0.0)
        if self.orders_df.empty:
            return matrix

        open_days_dt = pd.to_datetime(self.open_dates)
        weekday_open_counts = open_days_dt.dt.day_name().value_counts().to_dict()

        target_col = 'order_id' if metric == 'Orders' else 'items_count'
        agg_func = 'count' if metric == 'Orders' else 'sum'

        grouped = self.orders_df.groupby(['slot_30m', 'weekday'])[target_col].agg(agg_func).reset_index()

        for _, row in grouped.iterrows():
            slot, day, val = row['slot_30m'], row['weekday'], row[target_col]
            open_count = weekday_open_counts.get(day, 0)
            avg_per_slot = val / open_count if open_count > 0 else 0
            matrix.at[slot, day] = round(avg_per_slot, 2)

        return matrix

    def apply_staffing_rules(self, avg_per_slot: float, metric: str) -> dict:
        hourly_rate = round(avg_per_slot * 2, 4)
        rule_metric = 'Orders/hr' if metric == 'Orders' else 'Items/hr'

        applicable_rules = self.rules_df[self.rules_df['metric'] == rule_metric].sort_values('min_load').reset_index(drop=True)
        total_rules = len(applicable_rules)
        for idx, rule in applicable_rules.iterrows():
            min_l = float(rule['min_load'])
            max_l = float(rule['max_load'])
            is_last = (idx == total_rules - 1)
            if (min_l <= hourly_rate < max_l) or (is_last and hourly_rate >= min_l):
                return {"cooks": int(rule['suggested_cooks']), "label": rule['label']}
        return {"cooks": 0, "label": "No Data"}

    def get_chart_data_daily(self) -> pd.DataFrame:
        if self.orders_df.empty:
            return pd.DataFrame()
        return self.orders_df.groupby(['date', 'brand_name']).size().unstack(fill_value=0).sort_index()

    def get_chart_data_weekday(self) -> pd.DataFrame:
        days_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        short_days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
        if self.orders_df.empty or self.open_dates.empty:
            return pd.DataFrame()

        open_dt = pd.to_datetime(self.open_dates)
        wd_open = open_dt.dt.day_name().value_counts().to_dict()
        wd_ord = self.orders_df.groupby('weekday')['order_id'].count().to_dict()
        wd_itm = self.orders_df.groupby('weekday')['items_count'].sum().to_dict()

        avg_o = [wd_ord.get(d, 0) / wd_open[d] if wd_open.get(d, 0) > 0 else 0.0 for d in days_order]
        avg_i = [wd_itm.get(d, 0) / wd_open[d] if wd_open.get(d, 0) > 0 else 0.0 for d in days_order]

        return pd.DataFrame({
            "day": short_days,
            "avg_orders": avg_o,
            "avg_items": avg_i
        })

    def get_chart_data_platform(self) -> pd.DataFrame:
        if self.orders_df.empty:
            return pd.DataFrame()
        grp = self.orders_df.groupby('platform').agg(
            orders=('order_id', 'count'),
            revenue=('total_sales', 'sum')
        ).reset_index().sort_values('revenue', ascending=False)
        total_rev = grp['revenue'].sum()
        grp['pct'] = (grp['revenue'] / total_rev * 100.0) if total_rev > 0 else 0.0
        return grp

    def get_chart_data_shifts(self) -> pd.DataFrame:
        if self.orders_df.empty or self.shifts_df.empty:
            return pd.DataFrame()

        df = self.orders_df.copy()
        df['time_str'] = df['order_datetime'].dt.strftime('%H:%M')
        df['shift'] = df['time_str'].apply(self.get_shift_for_time)

        shift_order = self.shifts_df['name'].tolist()
        rows = []
        for s_name in shift_order:
            sub = df[df['shift'] == s_name]
            rows.append({
                "shift": s_name,
                "orders": len(sub),
                "items": int(sub['items_count'].sum()) if not sub.empty else 0
            })
        return pd.DataFrame(rows)

    def export_to_pdf(self, filepath: str, location_name: str, brand_name: str):
        kpis = self.get_kpis()
        daily_df = self.get_chart_data_daily()
        wd_df = self.get_chart_data_weekday()
        plat_df = self.get_chart_data_platform()
        shift_df = self.get_chart_data_shifts()
        hm_matrix = self.get_hr_heatmap(metric='Orders')

        active_slots = [s for s in hm_matrix.index if ("08:00" <= s <= "23:30") or (hm_matrix.loc[s].sum() > 0)]
        hm_matrix = hm_matrix.loc[active_slots]
        days_order = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        short_days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']

        def wrap_txt(val, width=34):
            return "\n".join(textwrap.wrap(str(val), width=width)) if val else ""

        prev_parse_math = plt.rcParams.get("text.parse_math", True)
        plt.rcParams["text.parse_math"] = False
        try:
            with PdfPages(filepath) as pdf:
                # =============================================================
                # PAGE 1: EXECUTIVE SUMMARY & KPI TABLE
                # =============================================================
                fig1 = plt.figure(figsize=(11.69, 8.27), dpi=150)  # A4 Landscape
                fig1.patch.set_facecolor("#FFFFFF")

                gs1 = fig1.add_gridspec(
                    3, 2,
                    height_ratios=[0.55, 1.85, 2.25],
                    left=0.06, right=0.94, top=0.93, bottom=0.11,
                    hspace=0.52, wspace=0.26
                )

                # Header
                ax_hdr = fig1.add_subplot(gs1[0, :])
                ax_hdr.axis("off")
                ax_hdr.text(0.0, 0.72, "GastroAnalytics — Executive Operations Summary", fontsize=16, fontweight="bold", color="#18181B")
                ax_hdr.text(0.0, 0.20, f"Location: {location_name}   |   Brand: {brand_name}   |   Period: {self.start_date} to {self.end_date}", fontsize=10, color="#4B5563")
                ax_hdr.text(1.0, 0.20, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", fontsize=9, color="#6B7280", ha="right")
                ax_hdr.plot([0, 1], [0.02, 0.02], color="#D1D5DB", linewidth=1.2, transform=ax_hdr.transAxes)

                # KPI Table (without Top Selling Dishes)
                ax_kpi = fig1.add_subplot(gs1[1, :])
                ax_kpi.axis("off")
                ax_kpi.set_title("Key Performance Indicators", loc="left", fontsize=11, fontweight="bold", color="#18181B", pad=8)

                rush_r, off_r, drop = kpis["Rating - Peak Rush"], kpis["Rating - Off-Peak"], kpis["Quality Drop Indicator"]
                rating_summary = (
                    f"{kpis['Overall Average Rating']}/5.0 (Rush: {rush_r} vs Off: {off_r}, -{drop})"
                    if rush_r != "N/A" and drop != "N/A"
                    else f"{kpis['Overall Average Rating']}/5.0"
                )

                kpi_rows = [
                    ["Total Revenue", f"€{kpis['Total Food Sales (€)']:,.2f}", "Average Order Value", f"€{kpis['Average Order Value (€)']:,.2f}"],
                    ["Total Orders", f"{kpis['Total Orders']:,}", "Total Food Items", f"{kpis['Total Food Items']:,}"],
                    ["Busiest Day(s)", wrap_txt(kpis['Busiest Date(s)'], 34), "Quietest Day(s)", wrap_txt(kpis['Quietest Trading Date(s)'], 34)],
                    ["Best Weekday (Avg)", str(kpis['Best Weekday (Avg)']), "Worst Weekday (Avg)", str(kpis['Worst Weekday (Avg)'])],
                    ["Customer Rating", wrap_txt(rating_summary, 36), "Lost Revenue & Rejections", f"€{kpis['Lost Revenue']:,.2f} ({kpis['Rejected Orders']} rejected | Peak: {kpis['Peak Rejection Hour']})"]
                ]

                kpi_tbl = ax_kpi.table(
                    cellText=kpi_rows,
                    colWidths=[0.19, 0.31, 0.20, 0.30],
                    bbox=[0.0, 0.0, 1.0, 0.94],
                    cellLoc="left"
                )
                kpi_tbl.auto_set_font_size(False)
                kpi_tbl.set_fontsize(8.5)
                for (r, c), cell in kpi_tbl.get_celld().items():
                    cell.set_edgecolor("#D1D5DB")
                    cell.set_linewidth(0.75)
                    cell.PAD = 0.03
                    if c in (0, 2):
                        cell.set_facecolor("#F3F4F6")
                        cell.get_text().set_weight("bold")
                        cell.get_text().set_color("#374151")
                    else:
                        cell.set_facecolor("#FFFFFF")
                        cell.get_text().set_color("#111827")

                # Chart 1: Daily Orders Trend
                ax_tr = fig1.add_subplot(gs1[2, 0])
                ax_tr.spines["top"].set_visible(False)
                ax_tr.spines["right"].set_visible(False)
                ax_tr.yaxis.grid(True, color="#E5E7EB", linewidth=0.7)
                ax_tr.set_title("Daily Orders Trend", loc="left", fontsize=10.5, fontweight="bold", pad=8)
                ax_tr.set_ylabel("Orders", fontsize=8.5, color="#374151")
                ax_tr.tick_params(labelsize=8)
                if not daily_df.empty:
                    palette = ["#334155", "#D97706", "#0D9488", "#4F46E5"]
                    x_vals = np.arange(len(daily_df.index))
                    for idx, col in enumerate(daily_df.columns):
                        ax_tr.plot(x_vals, daily_df[col].values, marker="o", markersize=3, linewidth=1.5, color=palette[idx % len(palette)], label=col[:22])
                    ax_tr.legend(frameon=False, fontsize=7.5, loc="upper left")
                    step = max(1, len(daily_df.index) // 6)
                    ticks = list(range(0, len(daily_df.index), step))
                    ax_tr.set_xticks(ticks)
                    ax_tr.set_xticklabels([daily_df.index[i] for i in ticks], rotation=25, ha="right")

                # Chart 2: Weekday Averages
                ax_wd = fig1.add_subplot(gs1[2, 1])
                ax_wd.spines["top"].set_visible(False)
                ax_wd.spines["right"].set_visible(False)
                ax_wd.yaxis.grid(True, color="#E5E7EB", linewidth=0.7)
                ax_wd.set_title("Average Orders & Items by Weekday", loc="left", fontsize=10.5, fontweight="bold", pad=8)
                ax_wd.set_ylabel("Avg Count / Open Day", fontsize=8.5, color="#374151")
                ax_wd.tick_params(labelsize=8)
                if not wd_df.empty:
                    x = np.arange(len(short_days))
                    w = 0.36
                    ax_wd.bar(x - w / 2, wd_df["avg_orders"], w, color="#334155", label="Avg Orders")
                    ax_wd.bar(x + w / 2, wd_df["avg_items"], w, color="#0D9488", label="Avg Food Items")
                    ax_wd.set_xticks(x)
                    ax_wd.set_xticklabels(short_days)
                    ax_wd.legend(frameon=False, fontsize=7.5, loc="upper left")

                pdf.savefig(fig1)
                plt.close(fig1)

                # =============================================================
                # PAGE 2: PLATFORM SHARE & MEAL SHIFT BREAKDOWN
                # =============================================================
                fig2 = plt.figure(figsize=(11.69, 8.27), dpi=150)
                fig2.patch.set_facecolor("#FFFFFF")
                gs2 = fig2.add_gridspec(2, 2, height_ratios=[0.35, 2.5], left=0.06, right=0.94, top=0.93, bottom=0.12, wspace=0.28)

                ax_hdr2 = fig2.add_subplot(gs2[0, :])
                ax_hdr2.axis("off")
                ax_hdr2.text(0.0, 0.55, "Channel & Shift Performance Breakdown", fontsize=14, fontweight="bold", color="#18181B")
                ax_hdr2.plot([0, 1], [0.05, 0.05], color="#D1D5DB", linewidth=1.0, transform=ax_hdr2.transAxes)

                # Platform Donut
                ax_pl = fig2.add_subplot(gs2[1, 0])
                ax_pl.set_title("Platform Revenue Share (€ & %)", loc="left", fontsize=11, fontweight="bold", pad=10)
                if not plat_df.empty and plat_df["revenue"].sum() > 0:
                    colors = ["#334155", "#D97706", "#0D9488", "#4F46E5"]
                    wedges, _ = ax_pl.pie(
                        plat_df["revenue"],
                        colors=[colors[i % len(colors)] for i in range(len(plat_df))],
                        startangle=90,
                        wedgeprops=dict(width=0.40, edgecolor="#FFFFFF", linewidth=2)
                    )
                    ax_pl.axis("equal")
                    ax_pl.text(0, 0, f"€{plat_df['revenue'].sum():,.0f}\nTotal", ha="center", va="center", fontsize=10, fontweight="bold")
                    leg_labels = [f"{r['platform']}: €{r['revenue']:,.0f} ({r['pct']:.1f}%)" for _, r in plat_df.iterrows()]
                    ax_pl.legend(wedges, leg_labels, frameon=False, fontsize=8.5, loc="upper center", bbox_to_anchor=(0.5, -0.02))
                else:
                    ax_pl.axis("off")
                    ax_pl.text(0.5, 0.5, "No Platform Data", ha="center", va="center", color="#6B7280")

                # Shift Bar Chart
                ax_sh = fig2.add_subplot(gs2[1, 1])
                ax_sh.spines["top"].set_visible(False)
                ax_sh.spines["right"].set_visible(False)
                ax_sh.yaxis.grid(True, color="#E5E7EB", linewidth=0.7)
                ax_sh.set_title("Orders & Food Items by Meal Shift", loc="left", fontsize=11, fontweight="bold", pad=10)
                ax_sh.set_ylabel("Total Count", fontsize=9, color="#374151")
                if not shift_df.empty and shift_df["orders"].sum() > 0:
                    x = np.arange(len(shift_df["shift"]))
                    w = 0.36
                    ax_sh.bar(x - w / 2, shift_df["orders"], w, color="#4F46E5", label="Orders")
                    ax_sh.bar(x + w / 2, shift_df["items"], w, color="#D97706", label="Food Items")
                    ax_sh.set_xticks(x)
                    ax_sh.set_xticklabels(shift_df["shift"], rotation=20, ha="right", fontsize=8.5)
                    ax_sh.legend(frameon=False, fontsize=8.5, loc="upper left")
                else:
                    ax_sh.text(0.5, 0.5, "No Shift Data", ha="center", va="center", color="#6B7280")

                pdf.savefig(fig2)
                plt.close(fig2)

                # =============================================================
                # PAGE 3: 30-MINUTE HEATMAP (TALLER PAGE TO PREVENT OVERLAP)
                # =============================================================
                num_slots = max(len(hm_matrix), 1)
                page_height = max(11.69, num_slots * 0.34 + 1.8)
                fig3 = plt.figure(figsize=(11.69, page_height), dpi=150)
                fig3.patch.set_facecolor("#FFFFFF")
                ax_hm = fig3.add_axes([0.04, 0.03, 0.92, 0.94])
                ax_hm.axis("off")

                ax_hm.text(0.0, 0.985, "30-Minute Kitchen Workload & Suggested Staffing Schedule", fontsize=14, fontweight="bold", color="#18181B", transform=ax_hm.transAxes)
                ax_hm.text(0.0, 0.968, "Format: Avg Orders / 30m  |  Suggested Cooks [Load Level]", fontsize=9.5, color="#4B5563", transform=ax_hm.transAxes)

                if not hm_matrix.empty:
                    col_labels = ["Time Slot / Shift"] + short_days
                    cell_texts, cell_colors = [], []
                    max_v = hm_matrix.values.max() if hm_matrix.values.max() > 0 else 1.0

                    for slot in hm_matrix.index:
                        h, m = map(int, slot.split(":"))
                        end_h, end_m = (h, 30) if m == 0 else ((h + 1) % 24, 0)
                        shift_name = self.get_shift_for_time(slot)
                        slot_range = f"{slot}-{str(end_h).zfill(2)}:{str(end_m).zfill(2)} ({shift_name})"

                        row_t, row_c = [slot_range], ["#F3F4F6"]
                        for d in days_order:
                            val = float(hm_matrix.at[slot, d])
                            rule_res = self.apply_staffing_rules(val, 'Orders')
                            cooks = rule_res['cooks']
                            lbl = rule_res['label']
                            row_t.append(f"{val:.1f} ord | {cooks}c [{lbl}]")
                            if val <= 0:
                                row_c.append("#FFFFFF")
                            else:
                                ratio = val / max_v
                                row_c.append("#FEF9C3" if ratio < 0.30 else "#FDE68A" if ratio < 0.65 else "#FECACA")

                        cell_texts.append(row_t)
                        cell_colors.append(row_c)

                    hm_tbl = ax_hm.table(
                        cellText=cell_texts,
                        colLabels=col_labels,
                        cellColours=cell_colors,
                        colColours=["#E5E7EB"] * 8,
                        colWidths=[0.20] + [0.114] * 7,
                        bbox=[0.0, 0.0, 1.0, 0.95],
                        cellLoc="center"
                    )
                    hm_tbl.auto_set_font_size(False)
                    hm_tbl.set_fontsize(7.5)
                    for (r, c), cell in hm_tbl.get_celld().items():
                        cell.set_edgecolor("#D1D5DB")
                        cell.set_linewidth(0.5)
                        if r == 0:
                            cell.get_text().set_weight("bold")
                            cell.get_text().set_color("#111827")
                        elif c == 0:
                            cell.get_text().set_weight("bold")
                            cell.get_text().set_horizontalalignment("left")

                pdf.savefig(fig3)
                plt.close(fig3)

        except Exception as e:
            log_error(f"PDF Export Failed: {str(e)}")
            raise
        finally:
            plt.rcParams["text.parse_math"] = prev_parse_math

    def export_to_excel(self, filepath: str):
        if self.orders_df.empty and self.rejected_df.empty:
            raise ValueError("No data available to export for the selected range.")
        try:
            with pd.ExcelWriter(filepath, engine='openpyxl') as writer:
                # 1. Summary KPIs (excluding Top Selling Dishes)
                kpis = self.get_kpis()
                excluded_keys = {"Top Dishes List", "Top Selling Dishes"}
                clean_kpis = {k: v for k, v in kpis.items() if k not in excluded_keys}
                kpi_df = pd.DataFrame(list(clean_kpis.items()), columns=['Metric', 'Value'])
                kpi_df['Value'] = kpi_df['Value'].astype(str)
                kpi_df.to_excel(writer, sheet_name='Summary KPIs', index=False)

                # 2. Daily Breakdown
                daily_breakdown_df = self.get_daily_breakdown(all_days_in_db=False)
                if not daily_breakdown_df.empty:
                    daily_breakdown_df.to_excel(writer, sheet_name='Daily Breakdown', index=False)

                # 3. HR Heatmaps
                hm_orders = self.get_hr_heatmap(metric='Orders').reset_index().rename(columns={'index': 'Time Slot'})
                hm_orders.to_excel(writer, sheet_name='HR Heatmap (Orders)', index=False)

                hm_items = self.get_hr_heatmap(metric='Items').reset_index().rename(columns={'index': 'Time Slot'})
                hm_items.to_excel(writer, sheet_name='HR Heatmap (Items)', index=False)

                for sheet_name in writer.book.sheetnames:
                    ws_s = writer.book[sheet_name]
                    for r_cells in ws_s.iter_rows(min_row=2, max_row=ws_s.max_row, min_col=1, max_col=ws_s.max_column):
                        for c_cell in r_cells:
                            if isinstance(c_cell.value, str) and c_cell.value.startswith(("=", "+", "-", "@", "\t", "\r")):
                                c_cell.value = "'" + c_cell.value

                # =============================================================
                # APPLY READABLE EXCEL STYLING & AUTO-FIT COLUMNS
                # =============================================================
                header_fill = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
                header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
                zebra_fill = PatternFill(start_color="F9FAFB", end_color="F9FAFB", fill_type="solid")
                body_font = Font(name="Calibri", size=10, color="111827")
                thin_border = Border(
                    left=Side(style="thin", color="E5E7EB"),
                    right=Side(style="thin", color="E5E7EB"),
                    top=Side(style="thin", color="E5E7EB"),
                    bottom=Side(style="thin", color="E5E7EB"),
                )

                for sheet_name, ws in writer.sheets.items():
                    ws.freeze_panes = "A2"
                    ws.views.sheetView[0].showGridLines = True

                    # Style header row
                    ws.row_dimensions[1].height = 26
                    header_names = {}
                    for col_idx, cell in enumerate(ws[1], 1):
                        cell.fill = header_fill
                        cell.font = header_font
                        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
                        cell.border = thin_border
                        header_names[col_idx] = str(cell.value or "")

                    # Style data rows
                    for row_idx in range(2, ws.max_row + 1):
                        max_lines = 1
                        for col_idx in range(1, ws.max_column + 1):
                            cell = ws.cell(row=row_idx, column=col_idx)
                            col_title = header_names.get(col_idx, "")
                            cell.font = body_font
                            cell.border = thin_border
                            if row_idx % 2 == 1:
                                cell.fill = zebra_fill

                            # Currency & number formatting
                            if "(€)" in col_title and isinstance(cell.value, (int, float)):
                                cell.number_format = '€#,##0.00'
                                cell.alignment = Alignment(horizontal="right", vertical="center")
                            elif isinstance(cell.value, int):
                                cell.number_format = '#,##0'
                                cell.alignment = Alignment(horizontal="right", vertical="center")
                            elif isinstance(cell.value, float):
                                cell.number_format = '#,##0.00'
                                cell.alignment = Alignment(horizontal="right", vertical="center")
                            else:
                                val_str = str(cell.value or "")
                                if len(val_str) > 55:
                                    max_lines = max(max_lines, (len(val_str) // 55) + 1)
                                cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

                        ws.row_dimensions[row_idx].height = max(20, max_lines * 16)

                    # Auto-fit column widths with reasonable max cap
                    for col_idx in range(1, ws.max_column + 1):
                        col_letter = get_column_letter(col_idx)
                        max_len = len(header_names.get(col_idx, ""))
                        for row_idx in range(2, ws.max_row + 1):
                            val = ws.cell(row=row_idx, column=col_idx).value
                            if val is not None:
                                max_len = max(max_len, min(len(str(val)), 60))
                        ws.column_dimensions[col_letter].width = max(max_len + 5, 13)

        except Exception as e:
            log_error(f"Excel Export Failed: {str(e)}")
            raise

    def _empty_kpis(self) -> dict:
        return {
            "Total Orders": 0, "Total Food Items": 0, "Total Food Sales (€)": 0.0,
            "Average Order Value (€)": 0.0, "Busiest Date(s)": "N/A", "Quietest Trading Date(s)": "N/A",
            "Best Weekday (Avg)": "N/A", "Worst Weekday (Avg)": "N/A", "Overall Average Rating": "N/A",
            "Total Rated Orders": 0, "% 5-Star Orders": "N/A", "Rating - Peak Rush": "N/A", 
            "Rating - Off-Peak": "N/A", "Quality Drop Indicator": "N/A", "Rejected Orders": 0,
            "Lost Revenue": 0.0, "Peak Rejection Hour": "None",
            "Top Selling Dishes": "No dish data", "Top Dishes List": []
        }
