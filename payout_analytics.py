from contextlib import closing
from typing import Dict, List, Optional
import pandas as pd
from database import get_connection
from payout_parser import normalize_brand_alias


COST_CATEGORY_LABELS: Dict[str, str] = {
    "commission_and_fees": "Commissions & Fees",
    "ads_and_campaigns": "Ads & Campaigns",
    "merchant_discounts": "Merchant Discounts",
    "deductions_and_refunds": "Deductions & Refunds",
    "other_adjustments": "Other Adjustments",
}


def _build_period_filter(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    brand_id: Optional[int] = None,
    platform: Optional[str] = None,
    alias: str = "r",
) -> tuple[str, list]:
    """
    Filters payout reports overlapping [date_from, date_to] (inclusive)
    where report.period_start <= date_to and report.period_end >= date_from.
    """
    clauses: List[str] = []
    params: list = []

    if date_from:
        clauses.append(f"{alias}.period_end >= ?")
        params.append(date_from)
    if date_to:
        clauses.append(f"{alias}.period_start <= ?")
        params.append(date_to)
    if brand_id is not None:
        clauses.append(f"{alias}.brand_id = ?")
        params.append(int(brand_id))
    if platform and platform.strip().lower() not in {"", "all", "all"}:
        clauses.append(f"LOWER({alias}.platform) = LOWER(?)")
        params.append(platform.strip())

    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where_sql, params


def _build_dedup_periods_cte(where_sql: str) -> str:
    """
    Aggregates payout_reports to unique (platform, brand_id, period_start, period_end) windows
    so that overlapping sales_report + invoice files do not double-count gross sales or costs.
    """
    return f"""
    WITH filtered_reports AS (
        SELECT
            r.*,
            COALESCE(b.name, r.raw_restaurant_name, 'Unassigned') AS resolved_brand_name
        FROM payout_reports r
        LEFT JOIN brands b ON b.id = r.brand_id
        {where_sql}
    ),
    period_agg AS (
        SELECT
            platform,
            brand_id,
            MAX(resolved_brand_name) AS brand_name,
            COALESCE(period_start, '') AS p_start,
            COALESCE(period_end, '') AS p_end,
            COUNT(*) AS docs_count,
            SUM(CASE WHEN brand_id IS NULL THEN 1 ELSE 0 END) AS unmapped_docs,
            SUM(CASE WHEN parse_status != 'ok' THEN 1 ELSE 0 END) AS warning_docs,
            SUM(CASE WHEN document_type != 'sales_report' THEN 1 ELSE 0 END) AS settlement_docs_count,
            MAX(COALESCE(gross_sales_report, 0.0)) AS gross_sales_period,
            SUM(CASE WHEN document_type != 'sales_report' THEN COALESCE(total_commissions_fees_gross, 0.0) ELSE 0.0 END) AS comm_settlement,
            SUM(COALESCE(total_commissions_fees_gross, 0.0)) AS comm_all,
            SUM(CASE WHEN document_type != 'sales_report' THEN COALESCE(total_ads_campaigns_gross, 0.0) ELSE 0.0 END) AS ads_settlement,
            SUM(COALESCE(total_ads_campaigns_gross, 0.0)) AS ads_all,
            SUM(CASE WHEN document_type != 'sales_report' THEN COALESCE(total_merchant_discounts_gross, 0.0) ELSE 0.0 END) AS disc_settlement,
            SUM(COALESCE(total_merchant_discounts_gross, 0.0)) AS disc_all,
            SUM(CASE WHEN document_type != 'sales_report' THEN COALESCE(total_deductions_refunds_gross, 0.0) ELSE 0.0 END) AS ref_settlement,
            SUM(COALESCE(total_deductions_refunds_gross, 0.0)) AS ref_all,
            SUM(CASE WHEN document_type != 'sales_report' THEN COALESCE(total_other_adjustments_gross, 0.0) ELSE 0.0 END) AS other_settlement,
            SUM(COALESCE(total_other_adjustments_gross, 0.0)) AS other_all,
            SUM(CASE WHEN document_type != 'sales_report' THEN COALESCE(total_costs_and_losses_gross, 0.0) ELSE 0.0 END) AS costs_settlement,
            SUM(COALESCE(total_costs_and_losses_gross, 0.0)) AS costs_all,
            SUM(COALESCE(payout_amount_gross, 0.0)) AS payout_period
        FROM filtered_reports
        GROUP BY platform, brand_id, CASE WHEN brand_id IS NOT NULL THEN '' ELSE COALESCE(NULLIF(TRIM(raw_restaurant_name), ''), CAST(id AS TEXT)) END, COALESCE(period_start, ''), COALESCE(period_end, '')
    ),
    period_clean AS (
        SELECT
            platform,
            brand_id,
            brand_name,
            p_start,
            p_end,
            docs_count,
            unmapped_docs,
            warning_docs,
            CASE
                WHEN p_start != '' AND p_end != '' AND julianday(p_end) >= julianday(p_start)
                     AND (? IS NOT NULL OR ? IS NOT NULL)
                THEN
                    CAST(
                        MAX(
                            0.0,
                            MIN(julianday(p_end), julianday(COALESCE(?, p_end)))
                            - MAX(julianday(p_start), julianday(COALESCE(?, p_start)))
                            + 1.0
                        ) / (julianday(p_end) - julianday(p_start) + 1.0)
                        AS REAL
                    )
                ELSE 1.0
            END AS overlap_ratio,
            gross_sales_period,
            CASE WHEN settlement_docs_count > 0 THEN comm_settlement ELSE comm_all END AS comm_period,
            CASE WHEN settlement_docs_count > 0 THEN ads_settlement ELSE ads_all END AS ads_period,
            CASE WHEN settlement_docs_count > 0 THEN disc_settlement ELSE disc_all END AS disc_period,
            CASE WHEN settlement_docs_count > 0 THEN ref_settlement ELSE ref_all END AS ref_period,
            CASE WHEN settlement_docs_count > 0 THEN other_settlement ELSE other_all END AS other_period,
            (
                COALESCE(CASE WHEN settlement_docs_count > 0 THEN comm_settlement ELSE comm_all END, 0.0)
                + COALESCE(CASE WHEN settlement_docs_count > 0 THEN ads_settlement ELSE ads_all END, 0.0)
                + COALESCE(CASE WHEN settlement_docs_count > 0 THEN disc_settlement ELSE disc_all END, 0.0)
                + COALESCE(CASE WHEN settlement_docs_count > 0 THEN ref_settlement ELSE ref_all END, 0.0)
                + COALESCE(CASE WHEN settlement_docs_count > 0 THEN other_settlement ELSE other_all END, 0.0)
            ) AS costs_period,
            payout_period
        FROM period_agg
    )
    """


def get_payout_overview(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    brand_id: Optional[int] = None,
    platform: Optional[str] = None,
) -> dict:
    """Returns deduplicated gross-level financial summary across matching payout_reports."""
    where_sql, params = _build_period_filter(date_from, date_to, brand_id, platform, alias="r")
    cte_sql = _build_dedup_periods_cte(where_sql)

    with closing(get_connection()) as conn:
        row = conn.execute(
            f"""
            {cte_sql}
            SELECT
                COALESCE(SUM(docs_count), 0) AS reports_count,
                COALESCE(SUM(unmapped_docs), 0) AS unmapped_reports_count,
                COALESCE(SUM(warning_docs), 0) AS warning_reports_count,
                COALESCE(SUM(gross_sales_period * overlap_ratio), 0.0) AS gross_sales_total,
                COALESCE(SUM(comm_period * overlap_ratio), 0.0) AS commissions_fees_gross,
                COALESCE(SUM(ads_period * overlap_ratio), 0.0) AS ads_campaigns_gross,
                COALESCE(SUM(disc_period * overlap_ratio), 0.0) AS merchant_discounts_gross,
                COALESCE(SUM(ref_period * overlap_ratio), 0.0) AS deductions_refunds_gross,
                COALESCE(SUM(other_period * overlap_ratio), 0.0) AS other_adjustments_gross,
                COALESCE(SUM(costs_period * overlap_ratio), 0.0) AS total_costs_gross,
                COALESCE(SUM(payout_period * overlap_ratio), 0.0) AS payout_amount_gross
            FROM period_clean
            """,
            list(params) + [date_to, date_from, date_to, date_from],
        ).fetchone()

    if row is None:
        return {
            "reports_count": 0, "unmapped_reports_count": 0, "warning_reports_count": 0,
            "gross_sales_total": 0.0, "commissions_fees_gross": 0.0, "ads_campaigns_gross": 0.0,
            "merchant_discounts_gross": 0.0, "deductions_refunds_gross": 0.0,
            "other_adjustments_gross": 0.0, "total_costs_gross": 0.0,
            "payout_amount_gross": 0.0, "cost_share_pct": 0.0,
        }
    gross_sales = round(float(row["gross_sales_total"] or 0.0), 2)
    total_costs = round(float(row["total_costs_gross"] or 0.0), 2)
    payout_total = round(float(row["payout_amount_gross"] or 0.0), 2)
    cost_share_pct = round((total_costs / gross_sales) * 100.0, 2) if gross_sales > 0 else 0.0

    return {
        "reports_count": int(row["reports_count"] or 0),
        "unmapped_reports_count": int(row["unmapped_reports_count"] or 0),
        "warning_reports_count": int(row["warning_reports_count"] or 0),
        "gross_sales_total": gross_sales,
        "commissions_fees_gross": round(float(row["commissions_fees_gross"] or 0.0), 2),
        "ads_campaigns_gross": round(float(row["ads_campaigns_gross"] or 0.0), 2),
        "merchant_discounts_gross": round(float(row["merchant_discounts_gross"] or 0.0), 2),
        "deductions_refunds_gross": round(float(row["deductions_refunds_gross"] or 0.0), 2),
        "other_adjustments_gross": round(float(row["other_adjustments_gross"] or 0.0), 2),
        "total_costs_gross": total_costs,
        "payout_amount_gross": payout_total,
        "cost_share_pct": cost_share_pct,
    }


def get_payout_breakdown_by_brand_and_platform(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    brand_id: Optional[int] = None,
    platform: Optional[str] = None,
) -> pd.DataFrame:
    """Returns a DataFrame grouped by brand and platform with deduplicated gross sales, cost categories, and payout."""
    where_sql, params = _build_period_filter(date_from, date_to, brand_id, platform, alias="r")
    cte_sql = _build_dedup_periods_cte(where_sql)

    with closing(get_connection()) as conn:
        df = pd.read_sql_query(
            f"""
            {cte_sql}
            SELECT
                brand_id,
                brand_name,
                platform,
                SUM(docs_count) AS reports_count,
                ROUND(COALESCE(SUM(gross_sales_period * overlap_ratio), 0.0), 2) AS gross_sales_total,
                ROUND(COALESCE(SUM(comm_period * overlap_ratio), 0.0), 2) AS commissions_fees_gross,
                ROUND(COALESCE(SUM(ads_period * overlap_ratio), 0.0), 2) AS ads_campaigns_gross,
                ROUND(COALESCE(SUM(disc_period * overlap_ratio), 0.0), 2) AS merchant_discounts_gross,
                ROUND(COALESCE(SUM(ref_period * overlap_ratio), 0.0), 2) AS deductions_refunds_gross,
                ROUND(COALESCE(SUM(other_period * overlap_ratio), 0.0), 2) AS other_adjustments_gross,
                ROUND(COALESCE(SUM(costs_period * overlap_ratio), 0.0), 2) AS total_costs_gross,
                ROUND(COALESCE(SUM(payout_period * overlap_ratio), 0.0), 2) AS payout_amount_gross
            FROM period_clean
            GROUP BY brand_id, brand_name, platform
            ORDER BY gross_sales_total DESC, brand_name ASC, platform ASC
            """,
            conn,
            params=list(params) + [date_to, date_from, date_to, date_from],
        )

    if not df.empty:
        def _row_share(x):
            base_sales = float(x["gross_sales_total"] or 0.0)
            return round((float(x["total_costs_gross"] or 0.0) / base_sales) * 100.0, 2) if base_sales > 0 else 0.0

        df["cost_share_pct"] = df.apply(_row_share, axis=1)
    return df


def get_cost_items_breakdown(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    brand_id: Optional[int] = None,
    platform: Optional[str] = None,
) -> pd.DataFrame:
    """Returns itemized cost aggregation grouped by category and raw label (gross)."""
    where_sql, params = _build_period_filter(date_from, date_to, brand_id, platform, alias="i")
    dedup_clause = """(
        COALESCE(r.document_type, '') != 'sales_report'
        OR NOT EXISTS (
            SELECT 1 FROM payout_reports r2
            WHERE COALESCE(r2.platform, '') = COALESCE(i.platform, '')
              AND COALESCE(r2.brand_id, -1) = COALESCE(i.brand_id, -1)
              AND COALESCE(r2.period_start, '') = COALESCE(i.period_start, '')
              AND COALESCE(r2.period_end, '') = COALESCE(i.period_end, '')
              AND COALESCE(r2.document_type, '') != 'sales_report'
        )
    )"""
    full_where = f"{where_sql} AND {dedup_clause}" if where_sql else f" WHERE {dedup_clause}"

    has_bounds = bool(date_from or date_to)
    overlap_expr = (
        """
        CASE
            WHEN i.period_start IS NOT NULL AND i.period_end IS NOT NULL
                 AND i.period_start != '' AND i.period_end != ''
                 AND julianday(i.period_end) >= julianday(i.period_start)
            THEN CAST(
                MAX(
                    0.0,
                    MIN(julianday(i.period_end), julianday(COALESCE(?, i.period_end)))
                    - MAX(julianday(i.period_start), julianday(COALESCE(?, i.period_start)))
                    + 1.0
                ) / (julianday(i.period_end) - julianday(i.period_start) + 1.0)
                AS REAL
            )
            ELSE 1.0
        END
        """
        if has_bounds
        else "1.0"
    )
    query_params = ([date_to, date_from] * 3 if has_bounds else []) + list(params)

    with closing(get_connection()) as conn:
        df = pd.read_sql_query(
            f"""
            SELECT
                i.cost_category,
                i.raw_label,
                i.platform,
                COUNT(*) AS occurrences,
                ROUND(COALESCE(SUM(i.amount_gross * ({overlap_expr})), 0.0), 2) AS amount_gross_total,
                ROUND(COALESCE(SUM(COALESCE(i.amount_net, 0.0) * ({overlap_expr})), 0.0), 2) AS amount_net_total,
                ROUND(COALESCE(SUM(COALESCE(i.vat_amount, 0.0) * ({overlap_expr})), 0.0), 2) AS vat_amount_total
            FROM payout_cost_items i
            LEFT JOIN payout_reports r ON r.id = i.payout_report_id
            {full_where}
            GROUP BY i.cost_category, i.raw_label, i.platform
            ORDER BY amount_gross_total DESC, i.cost_category ASC, i.raw_label ASC
            """,
            conn,
            params=query_params,
        )

    if not df.empty:
        df["category_label"] = df["cost_category"].map(
            lambda c: COST_CATEGORY_LABELS.get(c, c)
        )
    return df
def add_brand_alias(brand_id: int, alias_name: str, platform: Optional[str] = None) -> int:
    """Registers a normalized alias for an existing brand."""
    norm = normalize_brand_alias(alias_name)
    if not norm:
        raise ValueError("Alias name cannot be empty after normalization.")

    with closing(get_connection()) as conn:
        cur = conn.cursor()
        brand = cur.execute("SELECT id FROM brands WHERE id = ?", (int(brand_id),)).fetchone()
        if not brand:
            raise ValueError(f"Brand with id={brand_id} does not exist.")

        cur.execute(
            """
            INSERT INTO brand_aliases (brand_id, platform, alias_name, normalized_alias)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(normalized_alias) DO UPDATE SET
                brand_id = excluded.brand_id,
                platform = COALESCE(excluded.platform, brand_aliases.platform),
                alias_name = excluded.alias_name
            """,
            (int(brand_id), platform, alias_name.strip(), norm),
        )
        row = cur.execute(
            "SELECT id FROM brand_aliases WHERE normalized_alias = ?",
            (norm,),
        ).fetchone()
        conn.commit()
        return int(row["id"])


def assign_report_brand(report_id: int, brand_id: int, register_alias: bool = True) -> None:
    """Assigns a payout report (and its cost items) to a target brand, optionally saving its alias."""
    with closing(get_connection()) as conn:
        cur = conn.cursor()
        rep = cur.execute(
            "SELECT id, raw_restaurant_name, platform FROM payout_reports WHERE id = ?",
            (int(report_id),),
        ).fetchone()
        if not rep:
            raise ValueError(f"Payout report id={report_id} not found.")

        brand = cur.execute("SELECT id FROM brands WHERE id = ?", (int(brand_id),)).fetchone()
        if not brand:
            raise ValueError(f"Target brand id={brand_id} not found.")

        cur.execute(
            "UPDATE payout_reports SET brand_id = ? WHERE id = ?",
            (int(brand_id), int(report_id)),
        )
        cur.execute(
            "UPDATE payout_cost_items SET brand_id = ? WHERE payout_report_id = ?",
            (int(brand_id), int(report_id)),
        )

        if register_alias and rep["raw_restaurant_name"]:
            norm = normalize_brand_alias(rep["raw_restaurant_name"])
            if norm:
                cur.execute(
                    """
                    INSERT INTO brand_aliases (brand_id, platform, alias_name, normalized_alias)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(normalized_alias) DO UPDATE SET
                        brand_id = excluded.brand_id,
                        platform = COALESCE(excluded.platform, brand_aliases.platform)
                    """,
                    (int(brand_id), rep["platform"], rep["raw_restaurant_name"], norm),
                )

        conn.commit()




def _orders_sales_for_scope(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    brand_id: Optional[int] = None,
    platform: Optional[str] = None,
) -> float:
    clauses = [
        "LOWER(TRIM(COALESCE(o.status, ''))) NOT IN ('rejected', 'cancelled', 'canceled', 'refunded', 'void', 'odrzucone', 'odrzucony', 'anulowane', 'anulowany', 'zwrocone')"
    ]
    params: list = []
    if date_from:
        clauses.append("date(o.order_datetime) >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("date(o.order_datetime) <= ?")
        params.append(date_to)
    if brand_id is not None:
        clauses.append("o.brand_id = ?")
        params.append(int(brand_id))
    if platform and platform.strip().lower() not in {"", "all"}:
        clauses.append("LOWER(o.platform) = LOWER(?)")
        params.append(platform.strip())

    where_sql = " WHERE " + " AND ".join(clauses)
    with closing(get_connection()) as conn:
        row = conn.execute(
            f"SELECT COALESCE(SUM(o.total_sales), 0.0) AS s FROM orders o {where_sql}",
            params,
        ).fetchone()
    return round(float(row["s"] or 0.0), 2) if row else 0.0


def get_loss_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    brand_id: Optional[int] = None,
    platform: Optional[str] = None,
) -> dict:
    ov = get_payout_overview(
        date_from=date_from,
        date_to=date_to,
        brand_id=brand_id,
        platform=platform,
    )
    gross_sales = float(ov["gross_sales_total"] or 0.0)
    ads = float(ov["ads_campaigns_gross"] or 0.0)
    service_and_comm = float(ov["commissions_fees_gross"] or 0.0)
    other_losses = round(
        float(ov["merchant_discounts_gross"] or 0.0)
        + float(ov["deductions_refunds_gross"] or 0.0)
        + float(ov["other_adjustments_gross"] or 0.0),
        2,
    )
    total_losses = round(service_and_comm + ads + other_losses, 2)

    def pct(val: float) -> float:
        return round((val / gross_sales) * 100.0, 2) if gross_sales > 0 else 0.0

    return {
        "reports_count": ov["reports_count"],
        "gross_sales_total": round(gross_sales, 2),
        "ads_loss_gross": round(ads, 2),
        "ads_loss_pct": pct(ads),
        "service_fees_loss_gross": round(service_and_comm, 2),
        "service_fees_loss_pct": pct(service_and_comm),
        "other_losses_gross": other_losses,
        "other_losses_pct": pct(other_losses),
        "total_losses_gross": total_losses,
        "total_loss_pct": pct(total_losses),
        "payout_amount_gross": round(float(ov["payout_amount_gross"] or 0.0), 2),
    }
