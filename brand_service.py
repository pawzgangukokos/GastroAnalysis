import json
import re
from typing import Any, Dict, List, Optional

from database import get_connection


def _normalize_key(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", (name or "").strip().lower())
    return cleaned


def _ensure_tables(conn) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS brands (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS brand_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            brand_id INTEGER NOT NULL,
            alias TEXT NOT NULL,
            alias_norm TEXT NOT NULL UNIQUE,
            source TEXT DEFAULT 'manual',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_brand_aliases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            raw_name TEXT NOT NULL,
            name_norm TEXT NOT NULL UNIQUE,
            platform TEXT DEFAULT '',
            source_file TEXT DEFAULT '',
            occurrences INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            last_seen TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )


def get_auto_create_brands_setting() -> bool:
    conn = get_connection()
    try:
        _ensure_tables(conn)
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'auto_create_brands'"
        ).fetchone()
        if not row:
            return False
        return str(row[0]).strip().lower() in ("1", "true", "yes", "on")
    finally:
        conn.close()


def set_auto_create_brands_setting(enabled: bool) -> bool:
    conn = get_connection()
    try:
        _ensure_tables(conn)
        val = "1" if enabled else "0"
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES ('auto_create_brands', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (val,),
        )
        conn.commit()
        return bool(enabled)
    finally:
        conn.close()


def resolve_brand_name(raw_name: str, platform: str = "", source_file: str = "") -> Optional[str]:
    if not raw_name or not str(raw_name).strip():
        return None

    clean_name = str(raw_name).strip()
    norm = _normalize_key(clean_name)

    conn = get_connection()
    try:
        _ensure_tables(conn)

        alias_row = conn.execute(
            """
            SELECT b.name
            FROM brand_aliases ba
            JOIN brands b ON b.id = ba.brand_id
            WHERE ba.alias_norm = ?
            """,
            (norm,),
        ).fetchone()
        if alias_row:
            return alias_row[0]

        brand_row = conn.execute(
            "SELECT name FROM brands WHERE lower(trim(name)) = ?",
            (norm,),
        ).fetchone()
        if brand_row:
            return brand_row[0]

        if get_auto_create_brands_setting():
            conn.execute(
                "INSERT OR IGNORE INTO brands (name, active) VALUES (?, 1)",
                (clean_name,),
            )
            created = conn.execute(
                "SELECT name FROM brands WHERE lower(trim(name)) = ?",
                (norm,),
            ).fetchone()
            conn.commit()
            return created[0] if created else clean_name

        record_pending_brand_alias(
            raw_name=clean_name,
            platform=platform,
            source_file=source_file,
            _conn=conn,
        )
        conn.commit()
        return None
    finally:
        conn.close()


def record_pending_brand_alias(
    raw_name: str,
    platform: str = "",
    source_file: str = "",
    _conn=None,
) -> None:
    if not raw_name or not str(raw_name).strip():
        return
    clean_name = str(raw_name).strip()
    norm = _normalize_key(clean_name)

    owns_conn = _conn is None
    conn = _conn or get_connection()
    try:
        _ensure_tables(conn)
        conn.execute(
            """
            INSERT INTO pending_brand_aliases (raw_name, name_norm, platform, source_file, occurrences, status)
            VALUES (?, ?, ?, ?, 1, 'pending')
            ON CONFLICT(name_norm) DO UPDATE SET
                occurrences = pending_brand_aliases.occurrences + 1,
                platform = CASE WHEN excluded.platform != '' THEN excluded.platform ELSE pending_brand_aliases.platform END,
                source_file = CASE WHEN excluded.source_file != '' THEN excluded.source_file ELSE pending_brand_aliases.source_file END,
                status = CASE WHEN pending_brand_aliases.status = 'deferred' THEN 'deferred' ELSE 'pending' END,
                last_seen = CURRENT_TIMESTAMP
            """,
            (clean_name, norm, platform or "", source_file or ""),
        )
        if owns_conn:
            conn.commit()
    finally:
        if owns_conn:
            conn.close()


def assign_alias_to_brand(alias: str, brand_name: str) -> None:
    clean_alias = (alias or "").strip()
    clean_brand = (brand_name or "").strip()
    if not clean_alias or not clean_brand:
        return
    alias_norm = _normalize_key(clean_alias)

    conn = get_connection()
    try:
        _ensure_tables(conn)
        conn.execute(
            "INSERT OR IGNORE INTO brands (name, active) VALUES (?, 1)",
            (clean_brand,),
        )
        brand_row = conn.execute(
            "SELECT id FROM brands WHERE name = ?",
            (clean_brand,),
        ).fetchone()
        if not brand_row:
            return
        brand_id = brand_row[0]

        conn.execute(
            """
            INSERT INTO brand_aliases (brand_id, alias, alias_norm, source)
            VALUES (?, ?, ?, 'manual')
            ON CONFLICT(alias_norm) DO UPDATE SET
                brand_id = excluded.brand_id,
                alias = excluded.alias
            """,
            (brand_id, clean_alias, alias_norm),
        )
        conn.execute(
            "DELETE FROM pending_brand_aliases WHERE name_norm = ?",
            (alias_norm,),
        )
        conn.commit()
    finally:
        conn.close()


def create_brand_and_assign_alias(brand_name: str, alias: Optional[str] = None) -> None:
    clean_brand = (brand_name or "").strip()
    if not clean_brand:
        return
    assign_alias_to_brand(alias or clean_brand, clean_brand)


def defer_unmapped_brand(raw_name: str) -> None:
    norm = _normalize_key(raw_name)
    if not norm:
        return
    conn = get_connection()
    try:
        _ensure_tables(conn)
        conn.execute(
            "UPDATE pending_brand_aliases SET status = 'deferred' WHERE name_norm = ?",
            (norm,),
        )
        conn.commit()
    finally:
        conn.close()


def delete_brand_alias(alias_id: int) -> None:
    conn = get_connection()
    try:
        _ensure_tables(conn)
        conn.execute("DELETE FROM brand_aliases WHERE id = ?", (int(alias_id),))
        conn.commit()
    finally:
        conn.close()


def reapply_all_brand_aliases() -> Dict[str, int]:
    updated_reports = 0
    updated_orders = 0
    conn = get_connection()
    try:
        _ensure_tables(conn)
        rows = conn.execute(
            """
            SELECT ba.alias_norm, b.name
            FROM brand_aliases ba
            JOIN brands b ON b.id = ba.brand_id
            """
        ).fetchall()

        alias_map = {r[0]: r[1] for r in rows}
        brand_rows = conn.execute("SELECT name FROM brands").fetchall()
        for (bname,) in brand_rows:
            alias_map.setdefault(_normalize_key(bname), bname)

        # Aktualizacja payout_reports jeśli kolumna brand istnieje
        cols_pr = [c[1] for c in conn.execute("PRAGMA table_info(payout_reports)").fetchall()]
        if "brand" in cols_pr:
            for norm_key, target_brand in alias_map.items():
                cur = conn.execute(
                    "UPDATE payout_reports SET brand = ? WHERE lower(trim(brand)) = ? AND coalesce(brand, '') != ?",
                    (target_brand, norm_key, target_brand),
                )
                updated_reports += cur.rowcount or 0

        # Aktualizacja orders jeśli kolumna brand istnieje
        cols_ord = [c[1] for c in conn.execute("PRAGMA table_info(orders)").fetchall()]
        if "brand" in cols_ord:
            for norm_key, target_brand in alias_map.items():
                cur = conn.execute(
                    "UPDATE orders SET brand = ? WHERE lower(trim(brand)) = ? AND coalesce(brand, '') != ?",
                    (target_brand, norm_key, target_brand),
                )
                updated_orders += cur.rowcount or 0

        conn.commit()
        return {
            "updated_reports": updated_reports,
            "updated_orders": updated_orders,
        }
    finally:
        conn.close()


def get_brand_management_snapshot() -> Dict[str, Any]:
    conn = get_connection()
    try:
        _ensure_tables(conn)
        brands: List[Dict[str, Any]] = [
            {"id": r[0], "name": r[1], "active": bool(r[2])}
            for r in conn.execute("SELECT id, name, active FROM brands ORDER BY name COLLATE NOCASE").fetchall()
        ]
        aliases: List[Dict[str, Any]] = [
            {
                "id": r[0],
                "brand_id": r[1],
                "brand_name": r[2],
                "alias": r[3],
                "source": r[4],
            }
            for r in conn.execute(
                """
                SELECT ba.id, ba.brand_id, b.name, ba.alias, ba.source
                FROM brand_aliases ba
                JOIN brands b ON b.id = ba.brand_id
                ORDER BY b.name COLLATE NOCASE, ba.alias COLLATE NOCASE
                """
            ).fetchall()
        ]
        pending: List[Dict[str, Any]] = [
            {
                "raw_name": r[0],
                "platform": r[1],
                "source_file": r[2],
                "occurrences": r[3],
                "status": r[4],
                "last_seen": r[5],
            }
            for r in conn.execute(
                """
                SELECT raw_name, platform, source_file, occurrences, status, last_seen
                FROM pending_brand_aliases
                WHERE status != 'deferred'
                ORDER BY occurrences DESC, raw_name COLLATE NOCASE
                """
            ).fetchall()
        ]
        return {
            "auto_create_brands": get_auto_create_brands_setting(),
            "brands": brands,
            "aliases": aliases,
            "pending": pending,
            "summary_json": json.dumps(
                {
                    "brands_count": len(brands),
                    "aliases_count": len(aliases),
                    "pending_count": len(pending),
                }
            ),
        }
    finally:
        conn.close()
