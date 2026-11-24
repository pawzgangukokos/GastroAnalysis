"""
Brand / alias management service.
 
IMPORTANT: this module does NOT define its own `brands`, `brand_aliases` or
`platform_aliases` tables. Those are owned by database.py and already used
by main.py, importer.py and payout_analytics.py with this shape:
 
    brands(id, location_id, name, status)
    brand_aliases(id, brand_id, platform, alias_name, normalized_alias)
    platform_aliases(id, csv_venue_name UNIQUE, platform, location_id, brand_id)
 
This module only owns two of its own bookkeeping tables:
    pending_brand_aliases  -- tracks unmapped names seen during import so the
                              "Unmapped" screen can hide ones the user deferred
    app_settings            -- generic key/value settings (auto_create_brands)
"""
 
import json
from typing import Any, Dict, List, Optional
 
from database import get_connection
from payout_parser import normalize_brand_alias
 
 
# Table names deliberately unique to this module/version. Earlier app builds
# shipped a `pending_brand_aliases` / `app_settings` table under their own,
# incompatible shapes; reusing those names and relying on
# "CREATE TABLE IF NOT EXISTS" would silently keep whatever old columns
# already exist on a user's real database. New names sidestep that entirely.
_PENDING_TABLE = "brand_unmapped_queue"
_SETTINGS_TABLE = "brand_service_settings"
 
_PENDING_CREATE_SQL = f"""
    CREATE TABLE IF NOT EXISTS {_PENDING_TABLE} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_name TEXT NOT NULL,
        name_norm TEXT NOT NULL,
        platform TEXT NOT NULL DEFAULT '',
        source_type TEXT DEFAULT 'import',
        occurrences INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'pending',
        last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(name_norm, platform)
    )
"""
 
_SETTINGS_CREATE_SQL = f"""
    CREATE TABLE IF NOT EXISTS {_SETTINGS_TABLE} (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
"""
 
 
def _ensure_fresh_table(conn_or_cur, table_name: str, required_columns: set, create_sql: str) -> None:
    """Creates table_name if missing. If a table with that name already
    exists but is missing a column this module needs (leftover from a
    different/older version of this file), it is dropped and recreated.
    Safe here because these two tables only ever hold disposable bookkeeping
    (an "unmapped names" queue and one checkbox setting) -- never orders,
    brands or payout data."""
    row = conn_or_cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
    ).fetchone()
    if row:
        existing_cols = {r[1] for r in conn_or_cur.execute(f"PRAGMA table_info({table_name})").fetchall()}
        if not required_columns.issubset(existing_cols):
            conn_or_cur.execute(f"DROP TABLE {table_name}")
    conn_or_cur.execute(create_sql)
 
 
def _ensure_tables(conn) -> None:
    """Creates only the tables this module owns. Never touches brands/
    brand_aliases/platform_aliases -- those already exist via database.py."""
    _ensure_fresh_table(conn, _PENDING_TABLE, {"name_norm", "platform", "status"}, _PENDING_CREATE_SQL)
    _ensure_fresh_table(conn, _SETTINGS_TABLE, {"key", "value"}, _SETTINGS_CREATE_SQL)
 
 
# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
 
def get_auto_create_brands_setting() -> bool:
    conn = get_connection()
    try:
        _ensure_tables(conn)
        row = conn.execute(
            f"SELECT value FROM {_SETTINGS_TABLE} WHERE key = 'auto_create_brands'"
        ).fetchone()
        if row is None:
            return True  # matches the UI checkbox's default (checked)
        return str(row[0]).strip().lower() in ("1", "true", "yes", "on")
    finally:
        conn.close()
 
 
def set_auto_create_brands_setting(enabled: bool) -> bool:
    conn = get_connection()
    try:
        _ensure_tables(conn)
        val = "1" if enabled else "0"
        conn.execute(
            f"""
            INSERT INTO {_SETTINGS_TABLE} (key, value) VALUES ('auto_create_brands', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (val,),
        )
        conn.commit()
        return bool(enabled)
    finally:
        conn.close()
 
 
# ---------------------------------------------------------------------------
# Pending / unmapped bookkeeping
# ---------------------------------------------------------------------------
 
def record_pending_brand_alias(
    cur,
    raw_name: str,
    platform: str = "",
    source_type: str = "import",
    status: str = "pending",
) -> None:
    """Upserts a bookkeeping row for a restaurant name importer.py could not
    resolve to a brand. Takes an EXISTING cursor so it participates in the
    caller's transaction -- it never commits or closes the connection."""
    clean_name = (raw_name or "").strip()
    if not clean_name:
        return
    norm = normalize_brand_alias(clean_name)
    if not norm:
        return
    platform = platform or ""
 
    _ensure_fresh_table(cur, _PENDING_TABLE, {"name_norm", "platform", "status"}, _PENDING_CREATE_SQL)
    cur.execute(
        f"""
        INSERT INTO {_PENDING_TABLE} (raw_name, name_norm, platform, source_type, occurrences, status)
        VALUES (?, ?, ?, ?, 1, ?)
        ON CONFLICT(name_norm, platform) DO UPDATE SET
            raw_name = excluded.raw_name,
            occurrences = {_PENDING_TABLE}.occurrences + 1,
            source_type = excluded.source_type,
            status = CASE WHEN {_PENDING_TABLE}.status = 'deferred' THEN 'deferred' ELSE excluded.status END,
            last_seen = CURRENT_TIMESTAMP
        """,
        (clean_name, norm, platform, source_type, status),
    )
 
 
def defer_unmapped_brand(raw_name: str, platform: str = "") -> None:
    """Marks a raw name as 'deal with later' so it stops showing in the
    unmapped list, without creating a brand or alias for it."""
    clean_name = (raw_name or "").strip()
    norm = normalize_brand_alias(clean_name)
    if not norm:
        return
    platform = platform or ""
 
    conn = get_connection()
    try:
        _ensure_tables(conn)
        result = conn.execute(
            f"""
            UPDATE {_PENDING_TABLE}
            SET status = 'deferred', last_seen = CURRENT_TIMESTAMP
            WHERE name_norm = ? AND platform = ?
            """,
            (norm, platform),
        )
        if result.rowcount == 0:
            conn.execute(
                f"""
                INSERT INTO {_PENDING_TABLE} (raw_name, name_norm, platform, source_type, occurrences, status)
                VALUES (?, ?, ?, 'manual', 1, 'deferred')
                ON CONFLICT(name_norm, platform) DO UPDATE SET
                    status = 'deferred', last_seen = CURRENT_TIMESTAMP
                """,
                (clean_name, norm, platform),
            )
        conn.commit()
    finally:
        conn.close()
 
 
# ---------------------------------------------------------------------------
# Alias assignment
# ---------------------------------------------------------------------------
 
def assign_alias_to_brand(
    alias: str,
    brand_id: int,
    platform: str = "",
    reapply: bool = False,
) -> Dict[str, Any]:
    """Maps a raw restaurant name to an existing brand (brand_aliases +
    platform_aliases), clears it from the pending list, and optionally
    reapplies it to already-imported unmapped orders/reports."""
    clean_alias = (alias or "").strip()
    if not clean_alias:
        raise ValueError("Alias name cannot be empty.")
    norm = normalize_brand_alias(clean_alias)
    if not norm:
        raise ValueError("Alias name cannot be empty after normalization.")
    platform = platform or ""
 
    conn = get_connection()
    try:
        _ensure_tables(conn)
        brand_row = conn.execute(
            "SELECT id, name, location_id FROM brands WHERE id = ?", (int(brand_id),)
        ).fetchone()
        if not brand_row:
            raise ValueError(f"Brand with id={brand_id} does not exist.")
 
        conn.execute(
            """
            INSERT INTO brand_aliases (brand_id, platform, alias_name, normalized_alias)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(normalized_alias) DO UPDATE SET
                brand_id = excluded.brand_id,
                platform = excluded.platform,
                alias_name = excluded.alias_name
            """,
            (int(brand_id), platform, clean_alias, norm),
        )
        conn.execute(
            """
            INSERT INTO platform_aliases (csv_venue_name, platform, location_id, brand_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(csv_venue_name) DO UPDATE SET
                platform = excluded.platform,
                location_id = excluded.location_id,
                brand_id = excluded.brand_id
            """,
            (clean_alias, platform, brand_row["location_id"], int(brand_id)),
        )
        conn.execute(f"DELETE FROM {_PENDING_TABLE} WHERE name_norm = ?", (norm,))
        conn.commit()
        brand_name = brand_row["name"]
    finally:
        conn.close()
 
    if reapply:
        reapply_all_brand_aliases()
 
    return {"brand_id": int(brand_id), "brand_name": brand_name}
 
 
def create_brand_and_assign_alias(
    brand_name: str,
    raw_name: Optional[str] = None,
    platform: str = "",
) -> Dict[str, Any]:
    """Creates a new brand (under the first/default location, same convention
    as importer.py's auto-create path) and maps raw_name to it."""
    clean_brand = (brand_name or "").strip()
    if not clean_brand:
        raise ValueError("Brand name cannot be empty.")
    alias = (raw_name or clean_brand).strip()
 
    conn = get_connection()
    try:
        loc_row = conn.execute("SELECT id FROM locations ORDER BY id LIMIT 1").fetchone()
        if loc_row:
            loc_id = int(loc_row["id"])
        else:
            cur = conn.execute("INSERT INTO locations (name) VALUES (?)", ("Main Kitchen",))
            loc_id = int(cur.lastrowid)
 
        conn.execute(
            "INSERT INTO brands (location_id, name, status) VALUES (?, ?, 'Active')",
            (loc_id, clean_brand),
        )
        brand_row = conn.execute(
            "SELECT id FROM brands WHERE location_id = ? AND name = ?",
            (loc_id, clean_brand),
        ).fetchone()
        brand_id = int(brand_row["id"])
        conn.commit()
    finally:
        conn.close()
 
    return assign_alias_to_brand(alias, brand_id, platform=platform, reapply=True)
 
 
def delete_brand_alias(alias_id: int, unmap_matching_records: bool = False) -> None:
    """Removes an alias mapping. unmap_matching_records only stops that exact
    raw name from auto-matching on FUTURE imports (via platform_aliases); it
    deliberately does not touch orders/reports already linked to a brand."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT alias_name FROM brand_aliases WHERE id = ?", (int(alias_id),)
        ).fetchone()
        conn.execute("DELETE FROM brand_aliases WHERE id = ?", (int(alias_id),))
        if unmap_matching_records and row:
            conn.execute(
                "DELETE FROM platform_aliases WHERE csv_venue_name = ?",
                (row["alias_name"],),
            )
        conn.commit()
    finally:
        conn.close()
 
 
def reapply_all_brand_aliases() -> Dict[str, int]:
    """Scans currently-unmapped orders and payout_reports and tries to attach
    them to a brand using brand_aliases / brand names, now that new aliases
    may have been registered."""
    conn = get_connection()
    try:
        _ensure_tables(conn)
 
        alias_rows = conn.execute(
            "SELECT normalized_alias, platform, brand_id FROM brand_aliases"
        ).fetchall()
        alias_map: Dict[tuple, int] = {}
        alias_map_any_platform: Dict[str, int] = {}
        for r in alias_rows:
            key = (r["normalized_alias"], r["platform"] or "")
            alias_map.setdefault(key, r["brand_id"])
            alias_map_any_platform.setdefault(r["normalized_alias"], r["brand_id"])
 
        for r in conn.execute("SELECT id, name FROM brands").fetchall():
            alias_map_any_platform.setdefault(normalize_brand_alias(r["name"]), r["id"])
 
        def _lookup(norm: str, platform: str) -> Optional[int]:
            if not norm:
                return None
            hit = alias_map.get((norm, platform or ""))
            if hit is not None:
                return hit
            return alias_map_any_platform.get(norm)
 
        updated_orders = 0
        order_rows = conn.execute(
            """
            SELECT id, source_venue_name, platform
            FROM orders
            WHERE brand_id IS NULL AND source_venue_name IS NOT NULL AND TRIM(source_venue_name) != ''
            """
        ).fetchall()
        for row in order_rows:
            brand_id = _lookup(normalize_brand_alias(row["source_venue_name"]), row["platform"])
            if brand_id is None:
                continue
            brand = conn.execute(
                "SELECT location_id FROM brands WHERE id = ?", (brand_id,)
            ).fetchone()
            if not brand:
                continue
            conn.execute(
                "UPDATE orders SET brand_id = ?, location_id = ? WHERE id = ?",
                (brand_id, brand["location_id"], row["id"]),
            )
            updated_orders += 1
 
        updated_reports = 0
        updated_cost_items = 0
        report_rows = conn.execute(
            """
            SELECT id, raw_restaurant_name, platform
            FROM payout_reports
            WHERE brand_id IS NULL AND raw_restaurant_name IS NOT NULL AND TRIM(raw_restaurant_name) != ''
            """
        ).fetchall()
        for row in report_rows:
            brand_id = _lookup(normalize_brand_alias(row["raw_restaurant_name"]), row["platform"])
            if brand_id is None:
                continue
            conn.execute("UPDATE payout_reports SET brand_id = ? WHERE id = ?", (brand_id, row["id"]))
            updated_reports += 1
            cur = conn.execute(
                "UPDATE payout_cost_items SET brand_id = ? WHERE payout_report_id = ? AND brand_id IS NULL",
                (brand_id, row["id"]),
            )
            updated_cost_items += cur.rowcount or 0
 
        conn.commit()
        return {
            "updated_orders": updated_orders,
            "updated_reports": updated_reports,
            "updated_cost_items": updated_cost_items,
        }
    finally:
        conn.close()
 
 
# ---------------------------------------------------------------------------
# Snapshot for the Settings / Brand Management screen
# ---------------------------------------------------------------------------
 
def get_brand_management_snapshot() -> Dict[str, Any]:
    conn = get_connection()
    try:
        _ensure_tables(conn)
 
        brands: List[Dict[str, Any]] = [
            {
                "id": r["id"],
                "name": r["name"],
                "status": r["status"],
                "location_name": r["location_name"],
            }
            for r in conn.execute(
                """
                SELECT b.id, b.name, b.status, l.name as location_name
                FROM brands b
                JOIN locations l ON l.id = b.location_id
                ORDER BY l.name COLLATE NOCASE, b.name COLLATE NOCASE
                """
            ).fetchall()
        ]
 
        aliases: List[Dict[str, Any]] = [
            {
                "id": r["id"],
                "alias_name": r["alias_name"],
                "platform": r["platform"] or "",
                "brand_id": r["brand_id"],
                "brand_name": r["brand_name"],
            }
            for r in conn.execute(
                """
                SELECT ba.id, ba.alias_name, ba.platform, ba.brand_id, b.name as brand_name
                FROM brand_aliases ba
                JOIN brands b ON b.id = ba.brand_id
                ORDER BY b.name COLLATE NOCASE, ba.alias_name COLLATE NOCASE
                """
            ).fetchall()
        ]
 
        orders_unmapped = conn.execute(
            """
            SELECT source_venue_name AS raw_name, COALESCE(platform, '') AS platform,
                   COUNT(*) AS cnt, SUM(COALESCE(total_sales, 0.0)) AS total,
                   MIN(date(order_datetime)) AS dmin, MAX(date(order_datetime)) AS dmax
            FROM orders
            WHERE brand_id IS NULL AND source_venue_name IS NOT NULL AND TRIM(source_venue_name) != ''
            GROUP BY source_venue_name, platform
            """
        ).fetchall()
 
        reports_unmapped = conn.execute(
            """
            SELECT raw_restaurant_name AS raw_name, COALESCE(platform, '') AS platform,
                   COUNT(*) AS cnt, SUM(COALESCE(gross_sales_report, 0.0)) AS total,
                   MIN(period_start) AS dmin, MAX(period_end) AS dmax
            FROM payout_reports
            WHERE brand_id IS NULL AND raw_restaurant_name IS NOT NULL AND TRIM(raw_restaurant_name) != ''
            GROUP BY raw_restaurant_name, platform
            """
        ).fetchall()
 
        status_map = {
            (r["name_norm"], r["platform"] or ""): r["status"]
            for r in conn.execute(f"SELECT name_norm, platform, status FROM {_PENDING_TABLE}").fetchall()
        }
 
        merged: Dict[tuple, Dict[str, Any]] = {}
 
        def _merge(raw_name, platform, kind, cnt, total, dmin, dmax):
            norm = normalize_brand_alias(raw_name)
            key = (norm, platform or "")
            entry = merged.setdefault(
                key,
                {
                    "raw_name": raw_name,
                    "platform": platform or "",
                    "orders_count": 0,
                    "orders_sales": 0.0,
                    "reports_count": 0,
                    "reports_gross_sales": 0.0,
                    "date_min": None,
                    "date_max": None,
                },
            )
            if kind == "orders":
                entry["orders_count"] += cnt
                entry["orders_sales"] += float(total or 0.0)
            else:
                entry["reports_count"] += cnt
                entry["reports_gross_sales"] += float(total or 0.0)
            if dmin and (entry["date_min"] is None or dmin < entry["date_min"]):
                entry["date_min"] = dmin
            if dmax and (entry["date_max"] is None or dmax > entry["date_max"]):
                entry["date_max"] = dmax
 
        for r in orders_unmapped:
            _merge(r["raw_name"], r["platform"], "orders", r["cnt"], r["total"], r["dmin"], r["dmax"])
        for r in reports_unmapped:
            _merge(r["raw_name"], r["platform"], "reports", r["cnt"], r["total"], r["dmin"], r["dmax"])
 
        unmapped_candidates: List[Dict[str, Any]] = []
        for key, entry in merged.items():
            status = status_map.get(key, "pending")
            if status == "deferred":
                continue
            entry["status"] = status
            unmapped_candidates.append(entry)
        unmapped_candidates.sort(
            key=lambda e: (e["orders_count"] + e["reports_count"]), reverse=True
        )
 
        return {
            "auto_create_unknown_brands": get_auto_create_brands_setting(),
            "brands": brands,
            "aliases": aliases,
            "unmapped_candidates": unmapped_candidates,
            "summary_json": json.dumps(
                {
                    "brands_count": len(brands),
                    "aliases_count": len(aliases),
                    "unmapped_count": len(unmapped_candidates),
                }
            ),
        }
    finally:
        conn.close()


def set_brand_status(brand_id: int, status: str) -> str:
    """Sets a restaurant/brand status to 'Active' or 'Inactive'."""
    normalized_status = "Active" if str(status).strip().lower() == "active" else "Inactive"
    conn = get_connection()
    try:
        row = conn.execute("SELECT id FROM brands WHERE id = ?", (int(brand_id),)).fetchone()
        if not row:
            raise ValueError(f"Brand with id={brand_id} does not exist.")
        conn.execute(
            "UPDATE brands SET status = ? WHERE id = ?",
            (normalized_status, int(brand_id)),
        )
        conn.commit()
        return normalized_status
    finally:
        conn.close()


def assign_brand_to_location(brand_id: int, location_id: int, reassign_records: bool = True) -> None:
    """Moves/links an existing restaurant/brand to a target location and optionally updates related records."""
    conn = get_connection()
    try:
        brand = conn.execute("SELECT id FROM brands WHERE id = ?", (int(brand_id),)).fetchone()
        if not brand:
            raise ValueError(f"Brand with id={brand_id} does not exist.")
        loc = conn.execute("SELECT id FROM locations WHERE id = ?", (int(location_id),)).fetchone()
        if not loc:
            raise ValueError(f"Location with id={location_id} does not exist.")

        target_loc_id = int(location_id)
        target_brand_id = int(brand_id)

        conn.execute(
            "UPDATE brands SET location_id = ? WHERE id = ?",
            (target_loc_id, target_brand_id),
        )
        conn.execute(
            "UPDATE platform_aliases SET location_id = ? WHERE brand_id = ?",
            (target_loc_id, target_brand_id),
        )
        if reassign_records:
            conn.execute(
                "UPDATE orders SET location_id = ? WHERE brand_id = ?",
                (target_loc_id, target_brand_id),
            )
            conn.execute(
                "UPDATE order_items SET location_id = ? WHERE brand_id = ?",
                (target_loc_id, target_brand_id),
            )
            conn.execute(
                "UPDATE OR IGNORE operating_calendar SET location_id = ? WHERE brand_id = ?",
                (target_loc_id, target_brand_id),
            )
            conn.execute(
                "DELETE FROM operating_calendar WHERE brand_id = ? AND location_id != ?",
                (target_brand_id, target_loc_id),
            )
        conn.commit()
    finally:
        conn.close()


def get_brand_settings(brand_id: int) -> Dict[str, Any]:
    """Returns settings and alias summary for a single restaurant/brand."""
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT b.id, b.name, b.status, b.location_id, l.name AS location_name,
                   (SELECT COUNT(*) FROM brand_aliases ba WHERE ba.brand_id = b.id) AS aliases_count,
                   (SELECT COUNT(*) FROM orders o WHERE o.brand_id = b.id) AS orders_count
            FROM brands b
            JOIN locations l ON l.id = b.location_id
            WHERE b.id = ?
            """,
            (int(brand_id),),
        ).fetchone()
        if not row:
            raise ValueError(f"Brand with id={brand_id} does not exist.")
        return {
            "id": int(row["id"]),
            "name": row["name"],
            "status": row["status"] or "Active",
            "location_id": int(row["location_id"]),
            "location_name": row["location_name"],
            "aliases_count": int(row["aliases_count"] or 0),
            "orders_count": int(row["orders_count"] or 0),
        }
    finally:
        conn.close()


def update_brand_settings(brand_id: int, name: str, location_id: int, status: str) -> Dict[str, Any]:
    """Updates name, location link, and Active/Inactive status for a single restaurant/brand."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Brand name cannot be empty.")
    normalized_status = "Active" if str(status).strip().lower() == "active" else "Inactive"

    conn = get_connection()
    try:
        brand = conn.execute("SELECT id FROM brands WHERE id = ?", (int(brand_id),)).fetchone()
        if not brand:
            raise ValueError(f"Brand with id={brand_id} does not exist.")
        loc = conn.execute("SELECT id FROM locations WHERE id = ?", (int(location_id),)).fetchone()
        if not loc:
            raise ValueError(f"Location with id={location_id} does not exist.")

        conn.execute(
            """
            UPDATE brands
            SET name = ?, location_id = ?, status = ?
            WHERE id = ?
            """,
            (clean_name, int(location_id), normalized_status, int(brand_id)),
        )
        conn.execute(
            "UPDATE platform_aliases SET location_id = ? WHERE brand_id = ?",
            (int(location_id), int(brand_id)),
        )
        conn.execute(
            "UPDATE orders SET location_id = ? WHERE brand_id = ?",
            (int(location_id), int(brand_id)),
        )
        conn.execute(
            "UPDATE order_items SET location_id = ? WHERE brand_id = ?",
            (int(location_id), int(brand_id)),
        )
        conn.commit()
    finally:
        conn.close()

    return get_brand_settings(int(brand_id))


def merge_restaurants_into_target(source_brand_id: int, target_brand_id: int) -> dict:
    """Merges source_brand_id into target_brand_id, moves orders/items/reports/costs,
    and saves source brand name & aliases as aliases of target_brand_id for future imports."""
    from importer import merge_brands

    src_id = int(source_brand_id)
    tgt_id = int(target_brand_id)
    if src_id == tgt_id:
        raise ValueError("Source and target restaurants must be different.")

    conn = get_connection()
    try:
        src_row = conn.execute("SELECT id, name, location_id FROM brands WHERE id = ?", (src_id,)).fetchone()
        tgt_row = conn.execute("SELECT id, name, location_id FROM brands WHERE id = ?", (tgt_id,)).fetchone()
        if not src_row or not tgt_row:
            raise ValueError("Source or target restaurant does not exist.")
        src_name = str(src_row["name"])
        tgt_name = str(tgt_row["name"])
        tgt_loc_id = int(tgt_row["location_id"])

        old_aliases = conn.execute(
            "SELECT alias_name, platform FROM brand_aliases WHERE brand_id = ?", (src_id,)
        ).fetchall()
        old_plat_aliases = conn.execute(
            "SELECT csv_venue_name, platform FROM platform_aliases WHERE brand_id = ?", (src_id,)
        ).fetchall()
        raw_report_names = conn.execute(
            "SELECT DISTINCT raw_restaurant_name, platform FROM payout_reports WHERE brand_id = ? AND raw_restaurant_name IS NOT NULL",
            (src_id,),
        ).fetchall()
        raw_order_names = conn.execute(
            "SELECT DISTINCT source_venue_name, platform FROM orders WHERE brand_id = ? AND source_venue_name IS NOT NULL",
            (src_id,),
        ).fetchall()
    finally:
        conn.close()

    moved_orders = merge_brands(src_id, tgt_id)

    conn = get_connection()
    try:
        candidates = [(src_name, "")]
        for r in old_aliases:
            candidates.append((r["alias_name"], r["platform"] or ""))
        for r in old_plat_aliases:
            candidates.append((r["csv_venue_name"], r["platform"] or ""))
        for r in raw_report_names:
            candidates.append((r["raw_restaurant_name"], r["platform"] or ""))
        for r in raw_order_names:
            candidates.append((r["source_venue_name"], r["platform"] or ""))

        saved_aliases = 0
        for raw_n, plat in candidates:
            clean_n = (raw_n or "").strip()
            norm = normalize_brand_alias(clean_n)
            if not clean_n or not norm:
                continue
            conn.execute(
                """
                INSERT INTO brand_aliases (brand_id, platform, alias_name, normalized_alias)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(normalized_alias) DO UPDATE SET
                    brand_id = excluded.brand_id,
                    platform = COALESCE(NULLIF(excluded.platform, ''), brand_aliases.platform),
                    alias_name = excluded.alias_name
                """,
                (tgt_id, plat or "", clean_n, norm),
            )
            conn.execute(
                """
                INSERT INTO platform_aliases (csv_venue_name, platform, location_id, brand_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(csv_venue_name) DO UPDATE SET
                    platform = COALESCE(NULLIF(excluded.platform, ''), platform_aliases.platform),
                    location_id = excluded.location_id,
                    brand_id = excluded.brand_id
                """,
                (clean_n, plat or "", tgt_loc_id, tgt_id),
            )
            conn.execute(f"DELETE FROM {_PENDING_TABLE} WHERE name_norm = ?", (norm,))
            saved_aliases += 1

        conn.commit()
    finally:
        conn.close()

    return {
        "source_name": src_name,
        "target_name": tgt_name,
        "moved_orders": int(moved_orders or 0),
        "saved_aliases": saved_aliases,
    }
