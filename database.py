import sqlite3
import os
import shutil
import time
import logging
import traceback
from datetime import datetime
from pathlib import Path
from contextlib import closing

# Cross-Platform Storage Resilience with Environment Fallback
env_app_dir = os.environ.get("GASTRO_APP_DIR")
if env_app_dir:
    APP_DIR = Path(env_app_dir)
else:
    try:
        APP_DIR = Path.home() / "Documents" / "GastroAnalytics"
        APP_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Fallback if ~/Documents is locked by OS permissions or OneDrive/iCloud sync
        APP_DIR = Path.home() / ".gastroanalytics"

DB_PATH = APP_DIR / "gastro_data.db"
BACKUP_DIR = APP_DIR / "backups"
LOG_PATH = APP_DIR / "error_log.txt"

APP_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s"
)


def log_error(err_msg: str):
    """Logs errors to file for debugging without crashing the GUI."""
    logging.error(f"{err_msg}\n{traceback.format_exc()}")


def _check_sqlite_integrity(db_path: Path) -> bool:
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True, timeout=5.0)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return bool(row and str(row[0]).lower() == "ok")
    except Exception:
        return False
    finally:
        if conn is not None:
            conn.close()


def backup_db():
    """Creates a backup of the DB, keeping only the last 20 backups to save space."""
    db_file = Path(DB_PATH)
    if not db_file.exists():
        return

    try:
        now = datetime.now()
        stamp = now.strftime("%Y%m%d_%H%M%S_%f")
        backup_file = BACKUP_DIR / f"gastro_data_backup_{stamp}.db"
        idx = 1
        while backup_file.exists():
            backup_file = BACKUP_DIR / f"gastro_data_backup_{stamp}_{idx}.db"
            idx += 1

        shutil.copy2(db_file, backup_file)
        ts_ns = time.time_ns(); os.utime(backup_file, ns=(ts_ns, ts_ns))

        backups = sorted(
            BACKUP_DIR.glob("*.db"),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
            reverse=True,
        )
        for old_backup in backups[20:]:
            old_backup.unlink()
    except Exception as e:
        log_error("Failed to backup database.")
        raise RuntimeError(f"Backup failed: {str(e)}")


def restore_db(backup_filename: str):
    """Restores a specific backup file safely after integrity verification."""
    backup_file = BACKUP_DIR / backup_filename
    if not backup_file.exists():
        raise FileNotFoundError("Backup file not found.")

    if not _check_sqlite_integrity(backup_file):
        raise ValueError("Selected backup file failed SQLite integrity check.")

    target_db = Path(DB_PATH)
    pre_restore_copy = None
    try:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        if target_db.exists():
            pre_restore_copy = APP_DIR / f"pre_restore_{stamp}.db"
            shutil.copy2(target_db, pre_restore_copy)

        staged_db = APP_DIR / f".restore_stage_{stamp}.db"
        shutil.copy2(backup_file, staged_db)
        os.replace(staged_db, target_db)
        if pre_restore_copy is not None and pre_restore_copy.exists():
            pre_restore_copy.unlink()
    except Exception as e:
        if pre_restore_copy is not None and pre_restore_copy.exists():
            try:
                os.replace(pre_restore_copy, target_db)
            except Exception:
                pass
        log_error("Failed to restore database.")
        raise RuntimeError(f"Restore failed: {str(e)}")


def get_connection() -> sqlite3.Connection:
    """Returns a connected SQLite instance with strict timeouts and foreign keys enforced."""
    conn = sqlite3.connect(DB_PATH, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = 1")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db():
    """Initializes the database schema and seeds default settings if empty."""
    with closing(get_connection()) as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS brands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    location_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT DEFAULT 'Active',
                    FOREIGN KEY(location_id) REFERENCES locations(id) ON DELETE CASCADE,
                    UNIQUE(location_id, name)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS platform_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    csv_venue_name TEXT NOT NULL UNIQUE,
                    platform TEXT NOT NULL,
                    location_id INTEGER NOT NULL,
                    brand_id INTEGER NOT NULL,
                    FOREIGN KEY(location_id) REFERENCES locations(id) ON DELETE CASCADE,
                    FOREIGN KEY(brand_id) REFERENCES brands(id) ON DELETE CASCADE
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    location_id INTEGER NOT NULL,
                    brand_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    order_datetime TEXT NOT NULL,
                    status TEXT NOT NULL,
                    items_count INTEGER DEFAULT 0,
                    total_sales REAL DEFAULT 0.0,
                    rating REAL NULL,
                    prep_minutes REAL NULL,
                    source_venue_name TEXT NULL,
                    FOREIGN KEY(location_id) REFERENCES locations(id) ON DELETE CASCADE,
                    FOREIGN KEY(brand_id) REFERENCES brands(id) ON DELETE CASCADE,
                    UNIQUE(location_id, brand_id, platform, order_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    location_id INTEGER NOT NULL,
                    brand_id INTEGER NOT NULL,
                    platform TEXT NOT NULL,
                    order_id TEXT NOT NULL,
                    order_datetime TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    quantity INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(location_id, brand_id, platform, order_id, item_name)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS operating_calendar (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    location_id INTEGER NOT NULL,
                    brand_id INTEGER NOT NULL,
                    date TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'Open',
                    open_time TEXT DEFAULT '10:00',
                    close_time TEXT DEFAULT '22:00',
                    notes TEXT DEFAULT '',
                    FOREIGN KEY(location_id) REFERENCES locations(id) ON DELETE CASCADE,
                    FOREIGN KEY(brand_id) REFERENCES brands(id) ON DELETE CASCADE,
                    UNIQUE(location_id, brand_id, date)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS staffing_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    min_load REAL NOT NULL,
                    max_load REAL NOT NULL,
                    metric TEXT NOT NULL,
                    suggested_cooks INTEGER NOT NULL,
                    label TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS meal_shifts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    start_time TEXT NOT NULL,
                    end_time TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS exclusion_keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    keyword TEXT NOT NULL UNIQUE
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS csv_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_name TEXT NOT NULL UNIQUE,
                    mapping_json TEXT NOT NULL
                )
            """)

            # =================================================================
            # STAGE 1: PAYOUTS, COSTS & DEDUCTIONS SCHEMA (GROSS / INCL. VAT)
            # =================================================================
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS brand_aliases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_id INTEGER NOT NULL,
                    platform TEXT NULL,
                    alias_name TEXT NOT NULL,
                    normalized_alias TEXT NOT NULL UNIQUE,
                    FOREIGN KEY(brand_id) REFERENCES brands(id) ON DELETE CASCADE
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS payout_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    brand_id INTEGER NULL,
                    raw_restaurant_name TEXT NULL,
                    platform TEXT NOT NULL,
                    document_type TEXT NULL,
                    document_number TEXT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    currency TEXT DEFAULT 'EUR',
                    gross_sales_report REAL DEFAULT 0.0,
                    gross_sales_net_of_vat REAL NULL,
                    vat_amount REAL NULL,
                    total_commissions_fees_gross REAL DEFAULT 0.0,
                    total_ads_campaigns_gross REAL DEFAULT 0.0,
                    total_merchant_discounts_gross REAL DEFAULT 0.0,
                    total_deductions_refunds_gross REAL DEFAULT 0.0,
                    total_other_adjustments_gross REAL DEFAULT 0.0,
                    total_costs_and_losses_gross REAL DEFAULT 0.0,
                    payout_amount_gross REAL NULL,
                    source_file_name TEXT NOT NULL,
                    source_file_path TEXT NULL,
                    file_sha256 TEXT NOT NULL UNIQUE,
                    report_fingerprint TEXT NULL,
                    parse_status TEXT DEFAULT 'ok',
                    parse_warnings TEXT NULL,
                    imported_at TEXT NOT NULL,
                    FOREIGN KEY(brand_id) REFERENCES brands(id) ON DELETE SET NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS payout_cost_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    payout_report_id INTEGER NOT NULL,
                    brand_id INTEGER NULL,
                    platform TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL,
                    cost_category TEXT NOT NULL,
                    raw_label TEXT NOT NULL,
                    amount_gross REAL NOT NULL,
                    amount_net REAL NULL,
                    vat_amount REAL NULL,
                    notes TEXT NULL,
                    FOREIGN KEY(payout_report_id) REFERENCES payout_reports(id) ON DELETE CASCADE,
                    FOREIGN KEY(brand_id) REFERENCES brands(id) ON DELETE SET NULL
                )
            """)

            # Indexes for payout tables & brand aliases
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_brand_aliases_brand_id ON brand_aliases(brand_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_brand_aliases_norm ON brand_aliases(normalized_alias)")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_reports_brand_id ON payout_reports(brand_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_reports_platform ON payout_reports(platform)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_reports_period_start ON payout_reports(period_start)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_reports_period_end ON payout_reports(period_end)")
            cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_payout_reports_sha256 ON payout_reports(file_sha256)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_reports_fingerprint ON payout_reports(report_fingerprint)")

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_cost_items_report_id ON payout_cost_items(payout_report_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_cost_items_brand_id ON payout_cost_items(brand_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_cost_items_platform ON payout_cost_items(platform)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_cost_items_period_start ON payout_cost_items(period_start)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_cost_items_period_end ON payout_cost_items(period_end)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_payout_cost_items_category ON payout_cost_items(cost_category)")

            conn.commit()

            # --- MIGRATION BLOCK: Safely add missing columns to existing databases ---
            existing_order_cols = [r['name'] for r in cursor.execute("PRAGMA table_info(orders)").fetchall()]
            if 'prep_minutes' not in existing_order_cols:
                cursor.execute("ALTER TABLE orders ADD COLUMN prep_minutes REAL NULL")
            if 'source_venue_name' not in existing_order_cols:
                cursor.execute("ALTER TABLE orders ADD COLUMN source_venue_name TEXT NULL")
            conn.commit()

            # --- MIGRATION BLOCK: Close legacy 0.1 staffing rule gaps ---
            cursor.execute("UPDATE staffing_rules SET min_load = 5.0 WHERE metric = 'Orders/hr' AND min_load = 5.1")
            cursor.execute("UPDATE staffing_rules SET min_load = 15.0 WHERE metric = 'Orders/hr' AND min_load = 15.1")
            cursor.execute("UPDATE staffing_rules SET min_load = 25.0 WHERE metric = 'Orders/hr' AND min_load = 25.1")
            cursor.execute("UPDATE staffing_rules SET max_load = 9999.0 WHERE metric = 'Orders/hr' AND max_load = 999.0")

            cursor.execute("UPDATE staffing_rules SET min_load = 15.0 WHERE metric = 'Items/hr' AND min_load = 15.1")
            cursor.execute("UPDATE staffing_rules SET min_load = 45.0 WHERE metric = 'Items/hr' AND min_load = 45.1")
            cursor.execute("UPDATE staffing_rules SET min_load = 80.0 WHERE metric = 'Items/hr' AND min_load = 80.1")
            cursor.execute("UPDATE staffing_rules SET max_load = 9999.0 WHERE metric = 'Items/hr' AND max_load = 999.0")
            conn.commit()
            # -------------------------------------------------------------------------

            _seed_defaults(cursor)
            conn.commit()
        except Exception as e:
            conn.rollback()
            log_error("Database initialization failed.")
            raise e


def _seed_defaults(cursor: sqlite3.Cursor):
    """Injects default business rules for new DBs and patches existing missing rules."""
    cursor.execute("SELECT COUNT(*) FROM meal_shifts")
    if cursor.fetchone()[0] == 0:
        shifts = [
            ('Pre-Lunch', '08:00', '11:29'),
            ('Lunch Rush', '11:30', '14:30'),
            ('Afternoon', '14:31', '18:29'),
            ('Dinner Rush', '18:30', '21:30'),
            ('Late Night', '21:31', '23:59')
        ]
        cursor.executemany("INSERT INTO meal_shifts (name, start_time, end_time) VALUES (?, ?, ?)", shifts)

    cursor.execute("SELECT COUNT(*) FROM exclusion_keywords")
    if cursor.fetchone()[0] == 0:
        exclusions = [
            ('coca cola',), ('sprite',), ('fanta',), ('water',), ('pepsi',),
            ('330ml',), ('500ml',), ('ketchup',), ('mayo',), ('cutlery',)
        ]
        cursor.executemany("INSERT INTO exclusion_keywords (keyword) VALUES (?)", exclusions)

    # Orders/hr continuous intervals
    cursor.execute("SELECT COUNT(*) FROM staffing_rules WHERE metric = 'Orders/hr'")
    if cursor.fetchone()[0] == 0:
        hr_rules = [
            (0.0, 5.0, 'Orders/hr', 1, 'Quiet'),
            (5.0, 15.0, 'Orders/hr', 2, 'Steady'),
            (15.0, 25.0, 'Orders/hr', 3, 'Busy'),
            (25.0, 9999.0, 'Orders/hr', 4, 'Peak Rush')
        ]
        cursor.executemany(
            "INSERT INTO staffing_rules (min_load, max_load, metric, suggested_cooks, label) VALUES (?, ?, ?, ?, ?)",
            hr_rules
        )

    # Items/hr continuous intervals
    cursor.execute("SELECT COUNT(*) FROM staffing_rules WHERE metric = 'Items/hr'")
    if cursor.fetchone()[0] == 0:
        item_rules = [
            (0.0, 15.0, 'Items/hr', 1, 'Quiet'),
            (15.0, 45.0, 'Items/hr', 2, 'Steady'),
            (45.0, 80.0, 'Items/hr', 3, 'Busy'),
            (80.0, 9999.0, 'Items/hr', 4, 'Peak Rush')
        ]
        cursor.executemany(
            "INSERT INTO staffing_rules (min_load, max_load, metric, suggested_cooks, label) VALUES (?, ?, ?, ?, ?)",
            item_rules
        )


# Initialize on import
init_db()
