import os
import re
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Iterable, Union
import pandas as pd
from contextlib import closing
from database import get_connection, log_error, backup_db
from payout_parser import (
    ParsedPayoutReport,
    compute_file_sha256,
    normalize_brand_alias,
    parse_money,
    parse_pdf_payout_report,
    is_payout_csv,
    parse_csv_payout_report,
)


class UnrecognizedCSVFormatError(Exception):
    def __init__(self, filepath: str, columns: List[str]):
        self.filepath = filepath
        self.columns = columns
        super().__init__(f"Unrecognized CSV format in {os.path.basename(filepath)}")


class CSVImporter:
    def __init__(self):
        self.exclusions = self._load_exclusions()
        self.aliases = self._load_aliases()

    def _load_exclusions(self) -> List[str]:
        with closing(get_connection()) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT keyword FROM exclusion_keywords")
            return [
                str(row['keyword']).strip().lower()
                for row in cursor.fetchall()
                if row['keyword'] and str(row['keyword']).strip()
            ]

    def _load_aliases(self) -> Dict[str, dict]:
        with closing(get_connection()) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT csv_venue_name, location_id, brand_id, platform FROM platform_aliases")
            return {row['csv_venue_name']: dict(row) for row in cursor.fetchall()}

    @staticmethod
    def sniff_csv(filepath: str) -> pd.DataFrame:
        raw_head = Path(filepath).read_bytes()[:4]
        if raw_head.startswith((b"\xff\xfe", b"\xfe\xff")):
            encodings = ['utf-16', 'utf-8-sig', 'utf-8', 'cp1250', 'latin1']
        else:
            encodings = ['utf-8-sig', 'utf-8', 'cp1250', 'utf-16', 'latin1']
        delimiters = [',', ';', '\t']

        for enc in encodings:
            for sep in delimiters:
                try:
                    df = pd.read_csv(filepath, encoding=enc, sep=sep, dtype=str)
                    if len(df.columns) > 1:
                        df.columns = [str(c).strip() for c in df.columns]
                        return df
                except Exception:
                    continue
        raise ValueError(f"Unable to parse '{os.path.basename(filepath)}'. Check encoding or delimiter.")

    def detect_schema(self, columns: List[str]) -> Optional[Tuple[str, Dict[str, str]]]:
        col_set = set(columns)
        lower_map = {c.lower(): c for c in columns}

        # 1. Wolt Schema
        if "order placed" in lower_map and "review score" in lower_map:
            mapping = {}
            spec = {
                "order placed": "Order Date/Time",
                "order number": "Order ID",
                "venue": "Venue/Brand Name",
                "items": "Items Count or Items Text",
                "price": "Sales Amount (Gross)",
                "delivery status": "Status",
                "review score": "Customer Rating",
            }
            for k, internal in spec.items():
                if k in lower_map:
                    mapping[lower_map[k]] = internal
            return "Wolt", mapping

        # 2. Bolt Schema
        if "provider name" in lower_map and "order reference id" in lower_map:
            mapping = {}
            sales_net = lower_map.get("order total net")
            sales_gross = lower_map.get("order total gross")
            spec = {
                "order create datetime": "Order Date/Time",
                "order reference id": "Order ID",
                "provider name": "Venue/Brand Name",
                "order items no": "Items Count or Items Text",
                "finished order status": "Status",
                "rating": "Customer Rating",
            }
            for k, internal in spec.items():
                if k in lower_map:
                    mapping[lower_map[k]] = internal

            if sales_net:
                mapping[sales_net] = "Sales Amount (Net)"
            elif sales_gross:
                mapping[sales_gross] = "Sales Amount (Gross)"

            return "Bolt", mapping

        # 3. Custom Templates
        with closing(get_connection()) as conn:
            cursor = conn.cursor()
            rows = cursor.execute("SELECT template_name, mapping_json FROM csv_templates").fetchall()
            for row in rows:
                saved_map = json.loads(row['mapping_json'])
                mapped_csv_cols = set(saved_map.keys())
                if mapped_csv_cols and mapped_csv_cols.issubset(col_set):
                    return row['template_name'], saved_map

        return None

    def _resolve_or_create_venue(self, venue_raw: str, platform_name: str) -> Tuple[int, int]:
        if venue_raw in self.aliases:
            entry = self.aliases[venue_raw]
            return entry['location_id'], entry['brand_id']

        with closing(get_connection()) as conn:
            cur = conn.cursor()
            existing_brand = cur.execute(
                "SELECT id, location_id FROM brands WHERE LOWER(name) = LOWER(?)",
                (venue_raw,)
            ).fetchone()

            if existing_brand:
                loc_id = existing_brand['location_id']
                brand_id = existing_brand['id']
            else:
                default_loc = cur.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()
                if default_loc:
                    loc_id = default_loc['id']
                else:
                    cur.execute("INSERT INTO locations (name) VALUES (?)", ("Main Kitchen",))
                    loc_id = cur.lastrowid

                cur.execute(
                    "INSERT INTO brands (location_id, name, status) VALUES (?, ?, 'Active')",
                    (loc_id, venue_raw)
                )
                brand_id = cur.lastrowid

            cur.execute("""
                INSERT OR REPLACE INTO platform_aliases (csv_venue_name, platform, location_id, brand_id)
                VALUES (?, ?, ?, ?)
            """, (venue_raw, platform_name, loc_id, brand_id))

            norm_alias = normalize_brand_alias(venue_raw)
            if norm_alias:
                cur.execute("""
                    INSERT OR IGNORE INTO brand_aliases (brand_id, platform, alias_name, normalized_alias)
                    VALUES (?, ?, ?, ?)
                """, (brand_id, platform_name, venue_raw, norm_alias))

            conn.commit()

        self.aliases[venue_raw] = {
            "csv_venue_name": venue_raw,
            "platform": platform_name,
            "location_id": loc_id,
            "brand_id": brand_id
        }
        return loc_id, brand_id

    def parse_items_detailed(self, item_string: str) -> Tuple[int, List[Tuple[str, int]]]:
        if pd.isna(item_string) or not str(item_string).strip():
            return 0, []

        cleaned = str(item_string).strip()
        if cleaned.isdigit():
            return int(cleaned), []

        try:
            as_float = float(cleaned)
            if as_float.is_integer():
                return int(as_float), []
        except ValueError:
            pass

        # Decision: Exclusion keywords filter non-kitchen/packaging items out of
        # sold dish aggregations (order_items / top dishes), while keeping
        # order-level items_count consistent with ordered quantities.
        total_items = 0
        dish_totals: Dict[str, int] = {}
        pattern = re.compile(r'(\d+)\s*[xX]\s*(.+?)(?=(?:,\s*\d+\s*[xX]\s)|$)')
        matches = pattern.findall(cleaned)

        for qty_str, raw_desc in matches:
            qty = int(qty_str)
            total_items += qty

            desc_lower = raw_desc.lower()
            if any(excl and excl in desc_lower for excl in self.exclusions):
                continue
            dish_name = re.sub(r'\s+\d+(?:[.,]\d+)?\s*(?:EUR|€|USD|\$|GBP|£).*$', '', raw_desc, flags=re.IGNORECASE)
            dish_name = re.sub(r',\s*\d+\s*(?:g|kg|ml|l|pcs)\b.*$', '', dish_name, flags=re.IGNORECASE)
            dish_name = dish_name.strip(" ,-")

            if dish_name:
                dish_totals[dish_name] = dish_totals.get(dish_name, 0) + qty

        return total_items, list(dish_totals.items())

    def normalize_rating(self, rating_val) -> Optional[float]:
        if pd.isna(rating_val) or str(rating_val).strip() == '':
            return None
        val_str = str(rating_val).strip().replace(',', '.')
        match = re.search(r'(\d+(\.\d+)?)', val_str)
        if match:
            score = float(match.group(1))
            if 1.0 <= score <= 5.0:
                return score
        return None

    def auto_import_file(
        self,
        filepath: str,
        override_mapping: Optional[Dict[str, str]] = None,
        override_platform: Optional[str] = None
    ) -> dict:
        df = self.sniff_csv(filepath)

        # Route payout/cost CSVs to PayoutImporter instead of orders table
        if not override_mapping and is_payout_csv(df):
            payout_imp = PayoutImporter()
            summary = payout_imp.import_files([filepath])
            return {
                "filename": os.path.basename(filepath),
                "platform": "Payout CSV",
                "inserted": summary["imported"],
                "skipped": summary["skipped_duplicates"],
                "brands_count": 1 if summary["imported"] > 0 else 0,
                "payout_summary": summary,
            }

        if override_mapping:
            mapping = override_mapping
            platform_name = override_platform or "Custom CSV"
        else:
            detected = self.detect_schema(list(df.columns))
            if not detected:
                raise UnrecognizedCSVFormatError(filepath, list(df.columns))
            platform_name, mapping = detected

        backup_db()
        col_map = {v: k for k, v in mapping.items()}
        records_to_insert = []
        dish_records_to_insert = []
        brands_seen = set()

        for _, row in df.iterrows():
            order_id = str(row.get(col_map.get('Order ID', ''), '')).strip()
            if not order_id or order_id.lower() == 'nan':
                continue

            raw_date = str(row.get(col_map.get('Order Date/Time', ''), '')).strip()
            if not raw_date or raw_date.lower() == 'nan':
                continue
            try:
                dt_obj = pd.to_datetime(raw_date, format='ISO8601') if raw_date[:4].isdigit() and raw_date[4:5] == '-' else pd.to_datetime(raw_date, dayfirst=True)
                if pd.isna(dt_obj):
                    continue
                order_dt = dt_obj.strftime('%Y-%m-%d %H:%M:%S')
            except Exception:
                continue

            status_col = col_map.get('Status')
            status = str(row.get(status_col, 'delivered')).strip().lower() if status_col else 'delivered'
            if status in (
                'refunded', 'void', 'canceled', 'cancelled', 'rejected',
                'odrzucone', 'odrzucony', 'anulowane', 'anulowany', 'zwrocone'
            ):
                status = 'cancelled'

            venue_col = col_map.get('Venue/Brand Name')
            venue_raw = str(row.get(venue_col, '')).strip() if venue_col else ""
            if not venue_raw or venue_raw.lower() == 'nan':
                venue_raw = os.path.splitext(os.path.basename(filepath))[0]

            loc_id, brand_id = self._resolve_or_create_venue(venue_raw, platform_name)
            brands_seen.add(brand_id)

            sales_col = col_map.get('Sales Amount (Net)') or col_map.get('Sales Amount (Gross)') or col_map.get('Sales Amount')
            parsed_sales = parse_money(row.get(sales_col)) if sales_col else None
            total_sales = float(parsed_sales) if parsed_sales is not None else 0.0

            if total_sales < 0:
                status = 'cancelled'
                total_sales = abs(total_sales)

            items_raw = row.get(col_map.get('Items Count or Items Text', ''))
            items_count, parsed_dishes = self.parse_items_detailed(items_raw)
            rating = self.normalize_rating(row.get(col_map.get('Customer Rating', '')))

            records_to_insert.append((
                loc_id, brand_id, platform_name, order_id, order_dt,
                status, items_count, total_sales, rating, venue_raw
            ))

            if status != 'cancelled' and parsed_dishes:
                for dish_name, qty in parsed_dishes:
                    dish_records_to_insert.append((
                        loc_id, brand_id, platform_name, order_id, order_dt, dish_name, qty
                    ))

        inserted = 0
        skipped = 0
        if records_to_insert:
            with closing(get_connection()) as conn:
                cursor = conn.cursor()
                cursor.executemany("""
                    INSERT OR IGNORE INTO orders
                    (location_id, brand_id, platform, order_id, order_datetime, status, items_count, total_sales, rating, source_venue_name)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, records_to_insert)
                inserted = cursor.rowcount
                skipped = len(records_to_insert) - inserted

                if dish_records_to_insert:
                    cursor.executemany("""
                        INSERT OR IGNORE INTO order_items
                        (location_id, brand_id, platform, order_id, order_datetime, item_name, quantity)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, dish_records_to_insert)
                conn.commit()

        return {
            "filename": os.path.basename(filepath),
            "platform": platform_name,
            "inserted": inserted,
            "skipped": skipped,
            "brands_count": len(brands_seen)
        }


# =============================================================================
# STAGE 1: PAYOUT & COST IMPORTER (RECURSIVE, TRANSACTIONAL, IDEMPOTENT)
# =============================================================================

class PayoutImporter:
    """
    Imports Bolt Food & Wolt payout/invoice/sales PDFs and payout CSVs into
    `payout_reports` and `payout_cost_items` with:
    - fast `file_sha256` deduplication prior to heavy PDF parsing,
    - secondary `report_fingerprint` deduplication,
    - per-file atomic transactions (rollback on single-file failure without aborting batch),
    - automatic brand resolution/registration via `brand_aliases` and `brands`.
    """

    SUPPORTED_EXTENSIONS = {".pdf", ".csv"}

    def __init__(self, auto_create_brands: bool = True):
        self.auto_create_brands = auto_create_brands
        self._csv_helper = CSVImporter()

    def discover_files(self, paths: Iterable[Union[str, Path]]) -> List[str]:
        """Recursively collects all supported files (.pdf, .csv) from files and directories."""
        discovered: List[str] = []
        seen_paths = set()

        for raw_p in paths:
            p = Path(raw_p)
            if not p.exists():
                continue
            if p.is_file():
                if p.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                    abs_p = str(p.resolve())
                    if abs_p not in seen_paths:
                        seen_paths.add(abs_p)
                        discovered.append(abs_p)
            elif p.is_dir():
                for child in sorted(p.rglob("*")):
                    if child.is_file() and child.suffix.lower() in self.SUPPORTED_EXTENSIONS:
                        abs_p = str(child.resolve())
                        if abs_p not in seen_paths:
                            seen_paths.add(abs_p)
                            discovered.append(abs_p)

        return discovered

    def _resolve_or_register_brand(
        self,
        cur,
        raw_restaurant_name: str,
        platform: str,
    ) -> Tuple[Optional[int], bool, Optional[str]]:
        """
        Resolves restaurant name to `brand_id` via `brand_aliases` -> `brands` -> `platform_aliases`.
        If not found and `auto_create_brands=True`, creates a new brand + alias and returns a notice.
        """
        clean_name = (raw_restaurant_name or "").strip()
        if not clean_name:
            return None, False, "Missing restaurant name; imported with brand_id=NULL."

        norm = normalize_brand_alias(clean_name)

        # 1. Check brand_aliases
        if norm:
            row = cur.execute(
                "SELECT brand_id FROM brand_aliases WHERE normalized_alias = ?",
                (norm,),
            ).fetchone()
            if row:
                return int(row["brand_id"]), False, None

        # 2. Check existing brands (exact case-insensitive or normalized match)
        brands = cur.execute("SELECT id, name FROM brands").fetchall()
        for b in brands:
            if b["name"].strip().lower() == clean_name.lower() or (norm and normalize_brand_alias(b["name"]) == norm):
                brand_id = int(b["id"])
                if norm:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO brand_aliases (brand_id, platform, alias_name, normalized_alias)
                        VALUES (?, ?, ?, ?)
                        """,
                        (brand_id, platform, clean_name, norm),
                    )
                return brand_id, False, None

        # 3. Check legacy platform_aliases
        p_aliases = cur.execute("SELECT csv_venue_name, brand_id FROM platform_aliases").fetchall()
        for pa in p_aliases:
            if pa["csv_venue_name"].strip().lower() == clean_name.lower() or (
                norm and normalize_brand_alias(pa["csv_venue_name"]) == norm
            ):
                brand_id = int(pa["brand_id"])
                if norm:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO brand_aliases (brand_id, platform, alias_name, normalized_alias)
                        VALUES (?, ?, ?, ?)
                        """,
                        (brand_id, platform, clean_name, norm),
                    )
                return brand_id, False, None

        if not self.auto_create_brands:
            return None, False, f"Unmapped restaurant '{clean_name}' (brand_id=NULL)."

        # 4. Create brand under default/first location & register alias
        loc = cur.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()
        if loc:
            loc_id = int(loc["id"])
        else:
            cur.execute("INSERT INTO locations (name) VALUES (?)", ("Main Kitchen",))
            loc_id = int(cur.lastrowid)

        cur.execute(
            "INSERT INTO brands (location_id, name, status) VALUES (?, ?, 'Active')",
            (loc_id, clean_name),
        )
        brand_id = int(cur.lastrowid)

        if norm:
            cur.execute(
                """
                INSERT OR IGNORE INTO brand_aliases (brand_id, platform, alias_name, normalized_alias)
                VALUES (?, ?, ?, ?)
                """,
                (brand_id, platform, clean_name, norm),
            )
        cur.execute(
            """
            INSERT OR IGNORE INTO platform_aliases (csv_venue_name, platform, location_id, brand_id)
            VALUES (?, ?, ?, ?)
            """,
            (clean_name, platform, loc_id, brand_id),
        )

        return brand_id, True, f"Registered new brand '{clean_name}' (brand_id={brand_id})."

    def import_files(self, paths: Iterable[Union[str, Path]], create_backup: bool = False) -> dict:
        """
        Recursively scans `paths` (directories and/or file paths) and imports all payout reports.
        Returns a structured summary dictionary.
        """
        files = self.discover_files(paths)
        summary = {
            "found": len(files),
            "imported": 0,
            "skipped_duplicates": 0,
            "skipped_non_payout_csv": 0,
            "warnings_count": 0,
            "errors_count": 0,
            "new_brands_created": [],
            "warnings": [],
            "errors": [],
            "imported_files": [],
        }

        if not files:
            return summary

        if create_backup:
            backup_db()

        for filepath in files:
            fname = os.path.basename(filepath)
            ext = os.path.splitext(fname)[1].lower()

            try:
                # STEP 1: Fast SHA-256 check BEFORE heavy PDF parsing
                file_sha256 = compute_file_sha256(filepath)
                with closing(get_connection()) as conn:
                    dup_sha = conn.execute(
                        "SELECT id FROM payout_reports WHERE file_sha256 = ?",
                        (file_sha256,),
                    ).fetchone()
                if dup_sha:
                    summary["skipped_duplicates"] += 1
                    continue

                # STEP 2: Parse document (PDF or payout CSV)
                if ext == ".pdf":
                    parsed: ParsedPayoutReport = parse_pdf_payout_report(filepath)
                elif ext == ".csv":
                    df = self._csv_helper.sniff_csv(filepath)
                    if not is_payout_csv(df):
                        summary["skipped_non_payout_csv"] += 1
                        continue
                    parsed = parse_csv_payout_report(df, filepath)
                else:
                    continue

                fingerprint = parsed.build_fingerprint()

                # STEP 3: Per-file atomic transaction
                with closing(get_connection()) as conn:
                    cur = conn.cursor()
                    try:
                        # Check secondary fingerprint deduplication
                        dup_fp = cur.execute(
                            "SELECT id, source_file_name FROM payout_reports WHERE report_fingerprint = ?",
                            (fingerprint,),
                        ).fetchone()
                        if dup_fp:
                            conn.rollback()
                            summary["skipped_duplicates"] += 1
                            continue

                        brand_id, created_brand, brand_msg = self._resolve_or_register_brand(
                            cur, parsed.raw_restaurant_name, parsed.platform
                        )
                        if created_brand:
                            summary["new_brands_created"].append(parsed.raw_restaurant_name)

                        file_warnings = list(parsed.parse_warnings)
                        if brand_msg:
                            file_warnings.append(brand_msg)

                        parse_status = "partial" if file_warnings else parsed.parse_status
                        warnings_text = " | ".join(file_warnings) if file_warnings else None
                        imported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                        cur.execute(
                            """
                            INSERT INTO payout_reports (
                                brand_id, raw_restaurant_name, platform, document_type, document_number,
                                period_start, period_end, currency,
                                gross_sales_report, gross_sales_net_of_vat, vat_amount,
                                total_commissions_fees_gross, total_ads_campaigns_gross,
                                total_merchant_discounts_gross, total_deductions_refunds_gross,
                                total_other_adjustments_gross, total_costs_and_losses_gross,
                                payout_amount_gross, source_file_name, source_file_path,
                                file_sha256, report_fingerprint, parse_status, parse_warnings, imported_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                brand_id,
                                parsed.raw_restaurant_name,
                                parsed.platform,
                                parsed.document_type,
                                parsed.document_number,
                                parsed.period_start,
                                parsed.period_end,
                                parsed.currency,
                                parsed.gross_sales_report,
                                parsed.gross_sales_net_of_vat,
                                parsed.vat_amount,
                                parsed.total_commissions_fees_gross,
                                parsed.total_ads_campaigns_gross,
                                parsed.total_merchant_discounts_gross,
                                parsed.total_deductions_refunds_gross,
                                parsed.total_other_adjustments_gross,
                                parsed.total_costs_and_losses_gross,
                                parsed.payout_amount_gross,
                                fname,
                                filepath,
                                file_sha256,
                                fingerprint,
                                parse_status,
                                warnings_text,
                                imported_at,
                            ),
                        )
                        report_id = int(cur.lastrowid)

                        for item in parsed.cost_items:
                            cur.execute(
                                """
                                INSERT INTO payout_cost_items (
                                    payout_report_id, brand_id, platform, period_start, period_end,
                                    cost_category, raw_label, amount_gross, amount_net, vat_amount, notes
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    report_id,
                                    brand_id,
                                    parsed.platform,
                                    parsed.period_start,
                                    parsed.period_end,
                                    item.cost_category,
                                    item.raw_label,
                                    item.amount_gross,
                                    item.amount_net,
                                    item.vat_amount,
                                    item.notes,
                                ),
                            )

                        conn.commit()
                        summary["imported"] += 1
                        summary["imported_files"].append(fname)
                        if file_warnings:
                            summary["warnings_count"] += 1
                            summary["warnings"].append({"file": fname, "reasons": file_warnings})
                    except Exception:
                        conn.rollback()
                        raise

            except Exception as exc:
                log_error(f"Payout import error for '{filepath}': {str(exc)}")
                summary["errors_count"] += 1
                summary["errors"].append({"file": fname, "path": filepath, "reason": str(exc)})

        return summary


def import_payouts_recursive(
    paths: Union[str, Path, Iterable[Union[str, Path]]],
    auto_create_brands: bool = True,
    create_backup: bool = False,
) -> dict:
    """Convenience wrapper for recursive directory / multi-file payout import."""
    if isinstance(paths, (str, Path)):
        path_list = [paths]
    else:
        path_list = list(paths)
    importer = PayoutImporter(auto_create_brands=auto_create_brands)
    return importer.import_files(path_list, create_backup=create_backup)


def merge_brands(source_brand_id: int, target_brand_id: int) -> int:
    if source_brand_id == target_brand_id:
        raise ValueError("Source and target brands must be different.")
    backup_db()
    with closing(get_connection()) as conn:
        cur = conn.cursor()
        try:
            target = cur.execute("SELECT id, location_id FROM brands WHERE id = ?", (target_brand_id,)).fetchone()
            if not target:
                raise ValueError("Target brand no longer exists.")

            target_loc_id = target['location_id']
            cur.execute("""
                UPDATE OR IGNORE orders SET brand_id = ?, location_id = ? WHERE brand_id = ?
            """, (target_brand_id, target_loc_id, source_brand_id))
            moved_orders = cur.rowcount

            rem_orders = cur.execute(
                "SELECT rowid, platform, order_id FROM orders WHERE brand_id = ?",
                (source_brand_id,),
            ).fetchall()
            for r_row in rem_orders:
                r_id = r_row[0]
                plat = r_row[1]
                orig_oid = str(r_row[2])
                new_oid = f"{orig_oid}__m{source_brand_id}_{r_id}"
                cur.execute(
                    "UPDATE orders SET brand_id = ?, location_id = ?, order_id = ? WHERE rowid = ?",
                    (target_brand_id, target_loc_id, new_oid, r_id),
                )
                moved_orders += cur.rowcount
                cur.execute(
                    "UPDATE OR IGNORE order_items SET brand_id = ?, location_id = ?, order_id = ? WHERE brand_id = ? AND platform = ? AND order_id = ?",
                    (target_brand_id, target_loc_id, new_oid, source_brand_id, plat, orig_oid),
                )
                cur.execute(
                    "DELETE FROM order_items WHERE brand_id = ? AND platform = ? AND order_id = ?",
                    (source_brand_id, plat, orig_oid),
                )

            has_items_tbl = cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_items'").fetchone()
            if has_items_tbl:
                cur.execute("""
                    UPDATE OR IGNORE order_items SET brand_id = ?, location_id = ? WHERE brand_id = ?
                """, (target_brand_id, target_loc_id, source_brand_id))
                rem_items = cur.execute(
                    "SELECT rowid, platform, order_id FROM order_items WHERE brand_id = ?",
                    (source_brand_id,),
                ).fetchall()
                for i_row in rem_items:
                    i_id = i_row[0]
                    orig_oid = str(i_row[2])
                    new_oid = f"{orig_oid}__m{source_brand_id}_{i_id}"
                    cur.execute(
                        "UPDATE order_items SET brand_id = ?, location_id = ?, order_id = ? WHERE rowid = ?",
                        (target_brand_id, target_loc_id, new_oid, i_id),
                    )

            cur.execute("""
                UPDATE platform_aliases SET brand_id = ?, location_id = ? WHERE brand_id = ?
            """, (target_brand_id, target_loc_id, source_brand_id))

            # Keep payout_reports, payout_cost_items, and brand_aliases consistent on brand merge
            cur.execute("UPDATE payout_reports SET brand_id = ? WHERE brand_id = ?", (target_brand_id, source_brand_id))
            cur.execute("UPDATE payout_cost_items SET brand_id = ? WHERE brand_id = ?", (target_brand_id, source_brand_id))
            cur.execute("UPDATE brand_aliases SET brand_id = ? WHERE brand_id = ?", (target_brand_id, source_brand_id))

            cur.execute("DELETE FROM operating_calendar WHERE brand_id = ?", (source_brand_id,))
            cur.execute("DELETE FROM brands WHERE id = ?", (source_brand_id,))
            conn.commit()
            return moved_orders
        except Exception as e:
            conn.rollback()
            log_error(f"Brand merge failed: {str(e)}")
            raise


def _dispatch_orders_file(file_path: Path) -> dict:
    """Imports sales/orders file via CSVImporter.auto_import_file."""
    fp = str(file_path)
    importer_cls = globals().get("CSVImporter")
    if importer_cls is None:
        raise RuntimeError("Missing CSVImporter class in importer.py.")

    try:
        inst = importer_cls()
    except TypeError:
        # When __init__ requires a database path or db_path
        import inspect
        params = [
            name for name, par in inspect.signature(importer_cls.__init__).parameters.items()
            if name != "self" and par.default is inspect.Parameter.empty
        ]
        kwargs = {}
        for name in params:
            if "db" in name.lower():
                for candidate in ("DB_PATH", "DATABASE_PATH", "db_path"):
                    if candidate in globals():
                        kwargs[name] = globals()[candidate]
                        break
        inst = importer_cls(**kwargs)

    if hasattr(inst, "auto_import_file") and callable(inst.auto_import_file):
        res = inst.auto_import_file(fp)
        return res if isinstance(res, dict) else {"imported": 1, "result": res}

    raise RuntimeError("CSVImporter does not implement auto_import_file.")



def import_day_folder_recursive(folder_path: str) -> dict:
    """
    Recursively scans a day folder:
    - routes PDF and payout/invoice CSV files to import_payouts_recursive,
    - routes remaining CSV/XLSX order files to the orders importer.
    """
    root = Path(folder_path)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Folder does not exist: {folder_path}")

    payout_files: list[str] = []
    order_files: list[str] = []
    other_files: list[str] = []

    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        name_low = p.name.lower()
        if ext == ".pdf":
            payout_files.append(str(p))
        elif ext == ".csv":
            try:
                df_probe = CSVImporter.sniff_csv(str(p))
                if is_payout_csv(df_probe):
                    payout_files.append(str(p))
                else:
                    order_files.append(str(p))
            except Exception:
                if ("order" not in name_low and "orders" not in name_low) and any(
                    k in name_low for k in ("payout", "invoice")
                ):
                    payout_files.append(str(p))
                else:
                    order_files.append(str(p))
        elif ext in {".xlsx", ".xls"}:
            order_files.append(str(p))
        else:
            other_files.append(str(p))

    payout_summary = {"imported": 0, "duplicates": 0, "warnings": [], "errors": []}
    if payout_files:
        # Import selected files or an entire folder if import_payouts_recursive accepts a path
        try:
            payout_summary = import_payouts_recursive(str(root))
        except Exception as exc:
            payout_summary["errors"].append(f"Payouts: {exc}")

    orders_imported = 0
    orders_duplicates = 0
    orders_errors: list[str] = []

    for ofp in order_files:
        try:
            res = _dispatch_orders_file(Path(ofp))
            if isinstance(res, dict):
                orders_imported += int(res.get("imported", res.get("inserted", 1)) or 0)
                orders_duplicates += int(res.get("duplicates", res.get("skipped", 0)) or 0)
                for err in res.get("errors", []) or []:
                    orders_errors.append(str(err))
            else:
                orders_imported += 1
        except Exception as exc:
            orders_errors.append(f"{Path(ofp).name}: {exc}")

    return {
        "folder": str(root),
        "scanned_pdf": len(payout_files),
        "scanned_orders_files": len(order_files),
        "ignored_other_files": len(other_files),
        "payouts": payout_summary,
        "orders": {
            "imported": orders_imported,
            "duplicates": orders_duplicates,
            "errors": orders_errors,
        },
    }
