import hashlib
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple
import pandas as pd


VALID_COST_CATEGORIES = {
    "commission_and_fees",
    "ads_and_campaigns",
    "merchant_discounts",
    "deductions_and_refunds",
    "other_adjustments",
}


@dataclass
class ParsedCostItem:
    """
    Represents a single cost/deduction/discount line item (GROSS / incl. VAT).

    Sign convention for `amount_gross`:
    - Positive (`>= 0`): cost, fee, merchant discount, or deduction borne by the restaurant.
    - Negative (`< 0`): explicit credit/reimbursement in favor of the restaurant (noted in `notes`).
    """
    cost_category: str
    raw_label: str
    amount_gross: float
    amount_net: Optional[float] = None
    vat_amount: Optional[float] = None
    notes: Optional[str] = None


@dataclass
class ParsedPayoutReport:
    platform: str
    document_type: str
    raw_restaurant_name: str
    period_start: str
    period_end: str
    currency: str = "EUR"
    document_number: Optional[str] = None
    gross_sales_report: float = 0.0
    gross_sales_net_of_vat: Optional[float] = None
    vat_amount: Optional[float] = None
    total_commissions_fees_gross: float = 0.0
    total_ads_campaigns_gross: float = 0.0
    total_merchant_discounts_gross: float = 0.0
    total_deductions_refunds_gross: float = 0.0
    total_other_adjustments_gross: float = 0.0
    total_costs_and_losses_gross: float = 0.0
    payout_amount_gross: Optional[float] = None
    cost_items: List[ParsedCostItem] = field(default_factory=list)
    parse_status: str = "ok"
    parse_warnings: List[str] = field(default_factory=list)
    cash_received_info: Optional[float] = None

    def recompute_totals(self) -> None:
        comm = 0.0
        ads = 0.0
        disc = 0.0
        ded = 0.0
        other = 0.0
        for item in self.cost_items:
            val = float(item.amount_gross or 0.0)
            if item.cost_category == "commission_and_fees":
                comm += val
            elif item.cost_category == "ads_and_campaigns":
                ads += val
            elif item.cost_category == "merchant_discounts":
                disc += val
            elif item.cost_category == "deductions_and_refunds":
                ded += val
            else:
                other += val

        self.total_commissions_fees_gross = round(comm, 2)
        self.total_ads_campaigns_gross = round(ads, 2)
        self.total_merchant_discounts_gross = round(disc, 2)
        self.total_deductions_refunds_gross = round(ded, 2)
        self.total_other_adjustments_gross = round(other, 2)
        self.total_costs_and_losses_gross = round(comm + ads + disc + ded + other, 2)

    def build_fingerprint(self) -> str:
        norm_name = normalize_brand_alias(self.raw_restaurant_name or "unknown")
        doc_part = (self.document_number or "").strip().lower()
        sales_part = f"{round(float(self.gross_sales_report or 0.0), 2):.2f}"
        payout_part = (
            f"{round(float(self.payout_amount_gross), 2):.2f}"
            if self.payout_amount_gross is not None
            else "none"
        )
        raw = f"{self.platform.lower()}|{self.document_type}|{norm_name}|{self.period_start}|{self.period_end}|{doc_part}|{sales_part}|{payout_part}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_file_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def normalize_brand_alias(name: str) -> str:
    """Normalizes restaurant/brand names for resilient alias matching (handles accents like Poke -> poke and slash-l -> l)."""
    if not name:
        return ""
    s = str(name).replace(chr(322), "l").replace(chr(321), "l")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.lower().strip()
    s = re.sub(r"[’'`´]", "", s)
    cleaned = re.sub(r"[^a-z0-9]+", " ", s).strip()
    if cleaned:
        return re.sub(r"\s+", " ", cleaned)
    fallback = "".join(ch for ch in s if ch.isalnum() or ch.isspace()).strip()
    return re.sub(r"\s+", " ", fallback)


def parse_money(raw_value) -> Optional[float]:
    """
    Parses monetary strings across European/US formats:
    - spaces / non-breaking spaces / apostrophes as thousand separators ("1 463,86", "2,204.29", "1'234.56")
    - comma or dot decimal separators (including leading decimal like ".99" or "£.99")
    - leading/trailing '+' or '-' signs, unicode minus '−' (rejects malformed stacked signs like "--5", "+-5")
    - accounting parentheses "(123.45)" -> -123.45
    - currency symbols/codes (€, EUR, PLN, zl, $, USD, GBP, £)
    """
    import math

    if raw_value is None or isinstance(raw_value, bool):
        return None
    if isinstance(raw_value, (int, float)):
        if isinstance(raw_value, float) and (math.isnan(raw_value) or not math.isfinite(raw_value)):
            return None
        val = float(raw_value)
        return val if math.isfinite(val) else None

    s = str(raw_value).strip()
    if not s or s.lower() in {"nan", "none", "null", "-", "—", "n/a"}:
        return None

    # Reject hex literals or stacked signs before stripping
    if re.search(r"\b0x[0-9a-fA-F]+\b", s, flags=re.IGNORECASE):
        return None

    s = s.replace("\u00a0", " ").replace("\u202f", " ").replace("−", "-").replace("–", "-")

    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()

    # Strip currency tokens
    s = re.sub(r"(?:EUR|PLN|USD|GBP|CHF|CZK|zl|€|\$|£)", "", s, flags=re.IGNORECASE).strip()

    if re.search(r"[+-]{2,}", s):
        return None
    if s.startswith("-") and s.endswith("-"):
        return None

    if s.startswith("-"):
        negative = True
        s = s[1:].strip()
    elif s.startswith("+"):
        s = s[1:].strip()

    if s.endswith("-"):
        if negative:
            return None
        negative = True
        s = s[:-1].strip()
    elif s.endswith("+"):
        s = s[:-1].strip()

    if not s or re.search(r"[+-]", s):
        return None

    # Allow apostrophe or space as thousand separator inside numeric portion
    s_clean = s.replace("'", "")
    m = re.search(r"(?:[0-9][0-9\s.,]*[0-9]|\.[0-9]+|[0-9]+\.?|[0-9])", s_clean)
    if not m:
        return None
    num = m.group(0).replace(" ", "")

    has_comma = "," in num
    has_dot = "." in num

    if has_comma and has_dot:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif has_comma:
        parts = num.split(",")
        if len(parts) == 2 and len(parts[1]) in (1, 2, 3, 4):
            if len(parts[1]) == 3 and parts[0].isdigit() and int(parts[0]) > 0:
                num = parts[0] + parts[1]
            else:
                num = parts[0] + "." + parts[1]
        else:
            num = num.replace(",", "")
    elif has_dot:
        parts = num.split(".")
        if len(parts) > 2:
            num = "".join(parts)

    try:
        val = float(num)
        if not math.isfinite(val):
            return None
        return -val if negative and val != 0.0 else val
    except ValueError:
        return None

def parse_single_date(raw_date: str) -> Optional[str]:
    """Parses a single date string into ISO YYYY-MM-DD."""
    if not raw_date:
        return None
    s = str(raw_date).strip()
    formats = [
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%d-%m-%Y",
        "%Y.%m.%d",
        "%m/%d/%Y",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalize_date_range(raw_range: str) -> Tuple[str, str]:
    """
    Extracts and validates (period_start, period_end) in ISO YYYY-MM-DD format.
    Ensures period_start <= period_end.
    """
    if not raw_range:
        raise ValueError("Empty date range string.")

    date_tokens = re.findall(r"\b(?:\d{4}[-/.]\d{2}[-/.]\d{2}|\d{2}[-/.]\d{2}[-/.]\d{4})\b", str(raw_range))
    parsed_dates = [d for tok in date_tokens if (d := parse_single_date(tok)) is not None]

    if len(parsed_dates) >= 2:
        start_d, end_d = parsed_dates[0], parsed_dates[1]
    elif len(parsed_dates) == 1:
        start_d = end_d = parsed_dates[0]
    else:
        raise ValueError(f"Could not extract valid ISO dates from '{raw_range}'.")

    if start_d > end_d:
        raise ValueError(f"Invalid period: period_start ({start_d}) > period_end ({end_d}).")

    return start_d, end_d


def extract_pdf_text(filepath: str) -> str:
    """Extracts text from all pages of a PDF using pypdf, pdfplumber, or PyMuPDF."""
    text_pages: List[str] = []

    try:
        import pypdf  # type: ignore
        reader = pypdf.PdfReader(filepath)
        for page in reader.pages:
            txt = page.extract_text() or ""
            if txt.strip():
                text_pages.append(txt)
        if text_pages:
            return "\n".join(text_pages)
    except Exception:
        pass

    try:
        import pdfplumber  # type: ignore
        with pdfplumber.open(filepath) as pdf:
            for page in pdf.pages:
                txt = page.extract_text() or ""
                if txt.strip():
                    text_pages.append(txt)
        if text_pages:
            return "\n".join(text_pages)
    except Exception:
        pass

    try:
        import fitz  # PyMuPDF
        doc = fitz.open(filepath)
        for page in doc:
            txt = page.get_text("text") or ""
            if txt.strip():
                text_pages.append(txt)
        if text_pages:
            return "\n".join(text_pages)
    except Exception:
        pass

    raise ValueError(f"Unable to extract text from PDF '{os.path.basename(filepath)}'.")


def _extract_restaurant_from_filename(filename: str) -> Optional[str]:
    stem = os.path.splitext(os.path.basename(filename))[0]
    m_bolt = re.search(
        r"\d{4}-\d{2}-\d{2}\s*--\s*\d{4}-\d{2}-\d{2}\s+(.+?)\s+Weekly\s+Report",
        stem,
        flags=re.IGNORECASE,
    )
    if m_bolt:
        return m_bolt.group(1).replace("_s ", "'s ").strip()

    m_wolt = re.search(
        r"^\d+_(.+?)_(?:WOLT_TO_MERCHANT_INVOICE|PAYOUT_REPORT|SALES_REPORT)",
        stem,
        flags=re.IGNORECASE,
    )
    if m_wolt:
        return m_wolt.group(1).replace("_s ", "'s ").strip()

    return None


def _extract_period_from_filename(filename: str) -> Optional[Tuple[str, str]]:
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = re.search(r"(\d{4}-\d{2}-\d{2})\s*--\s*(\d{4}-\d{2}-\d{2})", stem)
    if m:
        return normalize_date_range(f"{m.group(1)} - {m.group(2)}")
    return None


def parse_bolt_food_pdf_text(text: str, filename: str = "") -> ParsedPayoutReport:
    """
    Parses Bolt Food Weekly Report PDFs (gross / incl. VAT amounts).

    Rules enforced:
    - Extracts Recipient, Period of report, Total sales, and WEEKLY TOTALS items.
    - Strictly ignores informational breakdown lines starting with 'Including...'
      to prevent double counting.
    - 'Cash received total' is NOT treated as a restaurant cost; stored in notes/cash_received_info.
    """
    warnings: List[str] = []

    # 1. Recipient
    restaurant_name = ""
    m_rec = re.search(r"Recipient\s*\n+\s*([^\n]+)", text, flags=re.IGNORECASE)
    if m_rec:
        restaurant_name = m_rec.group(1).strip()
    if not restaurant_name:
        restaurant_name = _extract_restaurant_from_filename(filename) or "Unknown Bolt Restaurant"
        warnings.append("Recipient not explicitly matched in PDF body; fell back to filename.")

    # 2. Period of report
    m_period = re.search(r"Period\s+of\s+report\s*:?\s*([^\n]+)", text, flags=re.IGNORECASE)
    if m_period:
        period_start, period_end = normalize_date_range(m_period.group(1))
    else:
        fn_period = _extract_period_from_filename(filename)
        if fn_period:
            period_start, period_end = fn_period
            warnings.append("Period of report extracted from filename.")
        else:
            raise ValueError("Missing 'Period of report' in Bolt Food PDF.")

    # 3. Currency
    currency = "EUR"
    if re.search(r"\bPLN\b|zl", text, flags=re.IGNORECASE):
        currency = "PLN"

    # 4. WEEKLY TOTALS section
    m_wt = re.search(r"WEEKLY\s+TOTALS\b(.*)", text, flags=re.IGNORECASE | re.DOTALL)
    if not m_wt:
        raise ValueError("Missing 'WEEKLY TOTALS' section in Bolt Food PDF.")
    wt_block = m_wt.group(1)

    raw_lines = [ln.strip() for ln in wt_block.splitlines() if ln.strip()]
    entries: List[Tuple[str, float]] = []
    pending_label_parts: List[str] = []

    header_noise = {"total", "(eur)", "(pln)", "total (eur)", "total (pln)"}

    for ln in raw_lines:
        ln_clean = ln.lstrip("|").strip()
        if not ln_clean or ln_clean.lower() in header_noise:
            continue

        m_inline = re.match(r"^(.+?)\s*(?:\|\s*)?([+-]?\d[\d\s.,]*)$", ln_clean)
        if m_inline and re.search(r"[A-Za-z]", m_inline.group(1)):
            lbl = m_inline.group(1).strip(" |")
            val = parse_money(m_inline.group(2))
            if val is not None:
                if pending_label_parts:
                    lbl = " ".join(pending_label_parts + [lbl]).strip()
                    pending_label_parts.clear()
                entries.append((lbl, val))
                continue

        if re.match(r"^[+-]?\d[\d\s.,]*$", ln_clean):
            val = parse_money(ln_clean)
            if val is not None and pending_label_parts:
                lbl = " ".join(pending_label_parts).strip()
                pending_label_parts.clear()
                entries.append((lbl, val))
            continue

        pending_label_parts.append(ln_clean)

    gross_sales = 0.0
    payout_amount: Optional[float] = None
    cash_received: Optional[float] = None
    cost_items: List[ParsedCostItem] = []

    for label, val in entries:
        low = label.lower().strip()

        # STRICT ANTI-DOUBLE-COUNTING RULE: Skip informational sub-rows ("Including...")
        if low.startswith("including "):
            continue
        if re.fullmatch(r"reference\s+year(?:\s+\d{4})?", low):
            continue

        if low == "total sales":
            gross_sales = round(val, 2)
            continue

        if low.startswith("cash received total"):
            cash_received = round(abs(val), 2)
            continue

        if low.startswith("weekly earnings") or low.startswith("payout"):
            payout_amount = round(val, 2)
            continue

        if val < 0:
            cost_amount = round(abs(val), 2)
            note = None
        elif val > 0:
            cost_amount = round(-val, 2)
            note = "Credit/reimbursement in favor of restaurant"
        else:
            cost_amount = 0.0
            note = None

        if any(k in low for k in ("campaign fee", "ad campaign", "ads")):
            cat = "ads_and_campaigns"
        elif any(k in low for k in ("refund", "deduction", "compensation")):
            cat = "deductions_and_refunds"
        elif any(k in low for k in ("price adjustment",)):
            cat = "other_adjustments"
        elif any(k in low for k in ("agency fee", "marketplace fee", "service fee", "software", "sim card", "commission", "platform fee")):
            cat = "commission_and_fees"
        else:
            cat = "other_adjustments"
            warnings.append(f"Unclassified Bolt Food line item mapped to 'other_adjustments': {label}")

        cost_items.append(
            ParsedCostItem(
                cost_category=cat,
                raw_label=label,
                amount_gross=cost_amount,
                notes=note,
            )
        )

    if cash_received is not None and cash_received > 0:
        warnings.append(f"Cash received total (informational, excluded from costs): {cash_received:.2f} {currency}")

    report = ParsedPayoutReport(
        platform="Bolt Food",
        document_type="weekly_report",
        raw_restaurant_name=restaurant_name,
        period_start=period_start,
        period_end=period_end,
        currency=currency,
        gross_sales_report=gross_sales,
        payout_amount_gross=payout_amount,
        cost_items=_dedup_doc_cost_items(cost_items),
        parse_status="partial" if warnings else "ok",
        parse_warnings=warnings,
        cash_received_info=cash_received,
    )
    report.recompute_totals()
    return report


def _extract_wolt_restaurant_and_period(text: str, filename: str) -> Tuple[str, str, str, Optional[str]]:
    """Extracts venue/restaurant name, period_start, period_end, and invoice number from Wolt PDFs."""
    doc_num = None
    m_inv = re.search(r"Invoice\s+number\s*\|?\s*([A-Z0-9/ -]+)", text, flags=re.IGNORECASE)
    if m_inv:
        candidate = m_inv.group(1).strip().splitlines()[0].strip()
        if candidate and candidate.lower() != "vat%":
            doc_num = candidate

    m_tf = re.search(
        r"Time\s+frame\s*\|?\s*(\d{2}\.\d{2}\.\d{4}\s*[-–]?\s*\d{2}\.\d{2}\.\d{4})",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if m_tf:
        period_start, period_end = normalize_date_range(m_tf.group(1))
    else:
        raise ValueError("Missing 'Time frame' in Wolt PDF.")

    restaurant_name = ""
    m_venue = re.search(r"\bVenue\s+Address\s*\n+\s*([^\n]+)", text, flags=re.IGNORECASE)
    if not m_venue:
        m_venue = re.search(r"\bVenue\s*\n+\s*([^\n]+)", text, flags=re.IGNORECASE)
    if m_venue:
        cand = m_venue.group(1).strip()
        cand = re.split(r"\s+(?:Judge|Triq|Level|The Plaza|Bisazza)\b", cand, maxsplit=1)[0].strip()
        restaurant_name = cand

    if not restaurant_name:
        m_biz = re.search(r"Business\s+ID:\s*[A-Z0-9 ]+\n+([^\n]+)", text, flags=re.IGNORECASE)
        if m_biz:
            cand = m_biz.group(1).strip()
            if cand.lower() not in {"wolt", "all prices are in eur"}:
                restaurant_name = cand

    if not restaurant_name:
        restaurant_name = _extract_restaurant_from_filename(filename) or "Unknown Wolt Restaurant"

    return restaurant_name, period_start, period_end, doc_num



def _dedup_doc_cost_items(items: list) -> list:
    """Removes repeated identical cost rows extracted multiple times from the same document/pages."""
    seen = set()
    unique = []
    for it in items:
        cat = getattr(it, "cost_category", None) if not isinstance(it, dict) else it.get("cost_category")
        lbl = (getattr(it, "raw_label", None) if not isinstance(it, dict) else it.get("raw_label") or "").strip().lower()
        ag = round(float((getattr(it, "amount_gross", None) if not isinstance(it, dict) else it.get("amount_gross")) or 0.0), 2)
        an_val = getattr(it, "amount_net", None) if not isinstance(it, dict) else it.get("amount_net")
        an = round(float(an_val), 2) if an_val is not None else None
        key = (cat, lbl, ag, an)
        if key in seen:
            continue
        seen.add(key)
        unique.append(it)
    return unique

def _parse_wolt_sales_row_nums(line_str: str) -> list[float]:
    if "|" in line_str:
        parts = [pt.strip() for pt in line_str.split("|")]
        tail = " ".join(parts[1:])
    else:
        m_lead = re.match(r"^(?:Total,?\s*)?[A-Za-z0-9 &/(),+\-éë]+?(?=\s+-?\d|\s+\d)", line_str.strip())
        tail = line_str.strip()[m_lead.end():] if m_lead else line_str.strip()
    no_pct = re.sub(r"-?\d+(?:[.,]\d+)?\s*%", " ", tail)
    out: list[float] = []
    for tok in re.findall(r"\(?-?\d+(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d{3})*(?:,\d+)?|-?\d+(?:[.,]\d+)?\)?", no_pct):
        v = parse_money(tok)
        if v is not None and abs(v) <= 1_000_000.0:
            out.append(v)
    return out


def parse_wolt_pdf_text(text: str, filename: str = "") -> ParsedPayoutReport:
    """
    Parses Wolt PDF documents (gross / VAT-inclusive amounts as primary):
    - Payout report ('payout_statement'): gross sales, merchant discounts, deductions, payout
    - Wolt-to-Merchant Invoice ('invoice'): commissions, platform fees, ad campaigns
    - Sales report ('sales_report') is skipped.
    """
    restaurant_name, period_start, period_end, doc_num = _extract_wolt_restaurant_and_period(text, filename)
    low_text = text.lower()
    fn_low = (filename or "").lower()

    currency = "EUR"
    m_curr = re.search(r"All\s+prices\s+are\s+in\s+([A-Z]{3})", text, flags=re.IGNORECASE)
    if m_curr:
        currency = m_curr.group(1).upper()

    gross_sales = 0.0
    net_sales: Optional[float] = None
    vat_sales: Optional[float] = None
    payout_amount: Optional[float] = None
    cost_items: List[ParsedCostItem] = []
    warnings: List[str] = []

    is_sales_report = (
        "sales_report" in fn_low
        or "sales report" in low_text
        or "total goods daily" in low_text
    )
    if is_sales_report and "payout report" not in low_text and "wolt commission" not in low_text:
        warnings.append(
            f"Wolt sales report ('{filename}') skipped for cost/payout calculation (processed via order/sales reports)."
        )
        return ParsedPayoutReport(
            platform="Wolt",
            document_type="sales_report",
            raw_restaurant_name=restaurant_name,
            period_start=period_start,
            period_end=period_end,
            currency=currency,
            document_number=doc_num,
            gross_sales_report=0.0,
            payout_amount_gross=0.0,
            cost_items=[],
            parse_status="partial",
            parse_warnings=warnings,
        )

    is_invoice = (
        "merchant_invoice" in fn_low
        or "to_merchant" in fn_low
        or "wolt commission" in low_text
        or "invoice amount" in low_text
    )
    is_payout = (
        "payout report" in low_text
        or "payout amount" in low_text
        or "payout" in fn_low
    )

    if not is_invoice and not is_payout:
        raise ValueError(
            f"Skipping Wolt document ('{filename}'): expected payout report or merchant invoice."
        )

    doc_type = "invoice" if is_invoice else "payout_statement"

    def _parse_row_values(line_str: str) -> tuple[str, list[float]]:
        if "|" in line_str:
            lbl_raw, tail_raw = line_str.split("|", 1)
        else:
            m_head = re.match(r"^([A-Za-z]+(?:[^0-9\n|-]*[A-Za-z%]))(.*)$", line_str)
            if m_head:
                lbl_raw, tail_raw = m_head.group(1), m_head.group(2)
            else:
                lbl_raw, tail_raw = line_str, ""

        # Strip percentages (e.g. 20%, 18.00%) so parse_money extracts only amounts/numbers
        no_pct = re.sub(r"-?\d+(?:[.,]\d+)?\s*%", " ", tail_raw)
        vals = [
            v for m_v in re.finditer(r"-?\d[\d.,]*", no_pct)
            if (v := parse_money(m_v.group(0))) is not None
        ]
        lbl_clean = re.sub(r"\s+", " ", lbl_raw).strip()
        return lbl_clean, vals

    if is_invoice:
        doc_type = "invoice"
        for line in text.splitlines():
            s_line = line.strip()
            low_l = s_line.lower()
            if re.match(r"^total\s+goods\s+sold\b", low_l) and "wolt delivery" not in low_l and "daily" not in low_l:
                parts = [pt.strip() for pt in s_line.split("|")]
                nums = [
                    n for pt in parts[1:]
                    if "%" not in pt and (n := parse_money(pt)) is not None
                ]
                if nums:
                    gross_sales = round(abs(nums[-1]), 2)
                    if len(nums) >= 2:
                        net_sales = round(abs(nums[0]), 2)
                        if vat_sales is None:
                            vat_sales = round(gross_sales - net_sales, 2)
                    break
        if doc_num is None:
            m_inv_no = re.search(
                r"Invoice\s+number\s*\|?\s*([A-Z0-9/\- ]+?)(?:\n|\r|$)",
                text,
                flags=re.IGNORECASE,
            )
            if m_inv_no:
                doc_num = re.sub(r"\s+", " ", m_inv_no.group(1)).strip()

        for raw_line in text.splitlines():
            s_ln = raw_line.strip()
            if not s_ln:
                continue
            s_low = s_ln.lower()
            if s_low.startswith("total"):
                continue

            if s_low.startswith("commission,") or s_low.startswith("platform fee"):
                lbl, nums = _parse_row_values(s_ln)
                if nums:
                    gross_v = round(abs(nums[-1]), 2)
                    net_v = round(abs(nums[1]), 2) if len(nums) >= 3 else None
                    vat_v = round(abs(nums[-2]), 2) if len(nums) >= 4 else None
                    if gross_v > 0:
                        cost_items.append(
                            ParsedCostItem(
                                cost_category="commission_and_fees",
                                raw_label=lbl,
                                amount_gross=gross_v,
                                amount_net=net_v,
                                vat_amount=vat_v,
                            )
                        )

            elif s_low.startswith("ad campaign"):
                ad_nums = []
                raw_txt = line if "\n" in line else text
                s_lines = raw_txt.splitlines()
                start_pos = next((i for i, l in enumerate(s_lines) if "ad campaign" in l.lower()), -1)
                if start_pos != -1:
                    for look_ahead in range(start_pos, min(len(s_lines), start_pos + 10)):
                        cand = s_lines[look_ahead].strip()
                        cand_low = cand.lower()
                        if look_ahead > start_pos and any(
                            cand_low.startswith(k) for k in ("vat %", "invoice amount", "bill to", "wolt commission", "finance and transaction")
                        ):
                            break
                        nums_found = [
                            float(m.replace(",", "").replace(" ", ""))
                            for m in re.findall(r"\d[\d,\s]*\.\d{2}", cand)
                        ]
                        if len(nums_found) >= 2 and (cand_low.startswith("total") or re.match(r"^\d+\s+\d", cand)):
                            ad_nums = nums_found
                            if cand_low.startswith("total"):
                                break
                if ad_nums:
                    gross_v = round(abs(ad_nums[-1]), 2)
                    net_v = round(abs(ad_nums[0]), 2) if len(ad_nums) >= 2 else None
                    vat_v = round(abs(ad_nums[-2]), 2) if len(ad_nums) >= 3 else None
                    if gross_v > 0:
                        cost_items.append(
                            ParsedCostItem(
                                cost_category="ads_and_campaigns",
                                raw_label="Ad campaign",
                                amount_gross=gross_v,
                                amount_net=net_v,
                                vat_amount=vat_v,
                            )
                        )
    else:
        fallback_gross = None
        fallback_net = None
        for line in text.splitlines():
            s_line = line.strip()
            low_l = s_line.lower()
            if not re.search(r"(?:total\s+goods\s+sold|gross\s+sales|total\s+sales|sales\s+total)", low_l):
                continue
            nums = _parse_wolt_sales_row_nums(s_line)
            if not nums:
                continue
            g_cand = round(abs(nums[-1]), 2)
            n_cand = round(abs(nums[0]), 2) if len(nums) >= 2 else None
            if re.match(r"^total\s+goods\s+sold\b", low_l) and "wolt delivery" not in low_l and "daily" not in low_l:
                gross_sales = g_cand
                if n_cand is not None:
                    net_sales = n_cand
                    if vat_sales is None:
                        vat_sales = round(max(0.0, gross_sales - net_sales), 2)
                break
            if fallback_gross is None and "wolt delivery" not in low_l and "daily" not in low_l:
                fallback_gross = g_cand
                fallback_net = n_cand
        if gross_sales == 0.0 and fallback_gross is not None:
            gross_sales = fallback_gross
            if net_sales is None and fallback_net is not None:
                net_sales = fallback_net
            if net_sales is not None and vat_sales is None:
                vat_sales = round(max(0.0, gross_sales - net_sales), 2)

        for m in re.finditer(
            r"(Total,?\s*(?:goods|items)\s+discounted\s+by\s+merchant)\s*\|?\s*(-?[0-9.,]+)\s*\|?\s*[0-9.,]+%\s*\|?\s*(-?[0-9.,]+)\s*\|?\s*(-?[0-9.,]+)",
            text,
            flags=re.IGNORECASE,
        ):
            label = re.sub(r"\s+", " ", m.group(1)).strip()
            d_net = parse_money(m.group(2))
            d_vat = parse_money(m.group(3))
            d_gross = parse_money(m.group(4))
            if d_gross is not None:
                cost_items.append(
                    ParsedCostItem(
                        cost_category="merchant_discounts",
                        raw_label=label,
                        amount_gross=round(abs(d_gross), 2),
                        amount_net=round(abs(d_net), 2) if d_net is not None else None,
                        vat_amount=round(abs(d_vat), 2) if d_vat is not None else None,
                    )
                )

        for m in re.finditer(
            r"\b(Deductions)\s*\|?\s*(-?[0-9.,]+)\s*\|?\s*(-?[0-9.,]+)\s*\|?\s*(-?[0-9.,]+)",
            text,
            flags=re.IGNORECASE,
        ):
            label = m.group(1).strip()
            ded_net = parse_money(m.group(2))
            ded_vat = parse_money(m.group(3))
            ded_gross = parse_money(m.group(4))
            if ded_gross is not None:
                cost_items.append(
                    ParsedCostItem(
                        cost_category="deductions_and_refunds",
                        raw_label=label,
                        amount_gross=round(abs(ded_gross), 2),
                        amount_net=round(abs(ded_net), 2) if ded_net is not None else None,
                        vat_amount=round(abs(ded_vat), 2) if ded_vat is not None else None,
                    )
                )

        m_inv_link = re.search(
            r"Invoice\s+from\s+Wolt\s+to\s+merchant\s+total\s*\|?\s*(-?[0-9.,]+)",
            text,
            flags=re.IGNORECASE,
        )
        if m_inv_link:
            inv_link_val = parse_money(m_inv_link.group(1))
            if inv_link_val is not None:
                warnings.append(
                    f"Payout statement references Wolt invoice total: {abs(inv_link_val):.2f} {currency}."
                )

        m_payout = re.search(r"Payout\s+amount\s*\|?\s*(-?[0-9.,]+)", text, flags=re.IGNORECASE)
        if m_payout:
            p_val = parse_money(m_payout.group(1))
            if p_val is not None:
                payout_amount = round(p_val, 2)

    if gross_sales == 0.0:
        for _ln in text.splitlines():
            _s = _ln.strip()
            _low = _s.lower()
            if _low.startswith("total goods sold") and "wolt delivery" not in _low and "daily" not in _low:
                _nums = _parse_wolt_sales_row_nums(_s)
                if _nums:
                    gross_sales = round(abs(_nums[-1]), 2)
                    if len(_nums) >= 2:
                        net_sales = round(abs(_nums[0]), 2)
                        vat_sales = round(max(0.0, gross_sales - net_sales), 2)
                    break

    report = ParsedPayoutReport(
        platform="Wolt",
        document_type=doc_type,
        raw_restaurant_name=restaurant_name,
        period_start=period_start,
        period_end=period_end,
        currency=currency,
        document_number=doc_num,
        gross_sales_report=gross_sales,
        gross_sales_net_of_vat=net_sales,
        vat_amount=vat_sales,
        payout_amount_gross=payout_amount,
        cost_items=_dedup_doc_cost_items(cost_items),
        parse_status="partial" if warnings else "ok",
        parse_warnings=warnings,
    )
    report.recompute_totals()
    return report


def parse_pdf_payout_report(filepath: str) -> ParsedPayoutReport:
    """Detects platform (Bolt Food vs Wolt) from PDF text/filename and parses into ParsedPayoutReport."""
    text = extract_pdf_text(filepath)
    low = text.lower()
    fn_low = os.path.basename(filepath).lower()

    if "bolt food" in low or "bolt operations" in low or "weekly aggregate agency fees" in low or "bolt" in fn_low:
        return parse_bolt_food_pdf_text(text, filename=filepath)
    if "wolt" in low or "wolt" in fn_low:
        return parse_wolt_pdf_text(text, filename=filepath)

    raise ValueError(f"Unrecognized PDF report platform in '{os.path.basename(filepath)}'.")


def is_payout_csv(df: pd.DataFrame) -> bool:
    """Distinguishes payout/settlement CSV files from regular order-level CSV files."""
    cols_lower = {str(c).strip().lower() for c in df.columns}
    if {"order placed", "review score"}.issubset(cols_lower):
        return False
    if {"provider name", "order reference id"}.issubset(cols_lower):
        return False

    payout_markers = {
        "payout_amount",
        "payout amount",
        "weekly earnings",
        "commission_gross",
        "commission gross",
        "agency fee",
        "platform fee",
        "gross_sales_report",
        "total goods sold",
        "cost_category",
    }
    return bool(cols_lower & payout_markers)


def parse_csv_payout_report(df: pd.DataFrame, filepath: str) -> ParsedPayoutReport:
    """Parses a payout/cost summary CSV into ParsedPayoutReport (gross amounts)."""
    lower_map = {str(c).strip().lower(): c for c in df.columns}

    def get_first_val(candidates: List[str]) -> Optional[str]:
        for c in candidates:
            if c in lower_map:
                series = df[lower_map[c]].dropna()
                if not series.empty:
                    val = str(series.iloc[0]).strip()
                    if val and val.lower() != "nan":
                        return val
        return None

    platform_raw = get_first_val(["platform", "provider"]) or ""
    if "bolt" in platform_raw.lower() or "bolt" in os.path.basename(filepath).lower():
        platform = "Bolt Food"
    elif "wolt" in platform_raw.lower() or "wolt" in os.path.basename(filepath).lower():
        platform = "Wolt"
    else:
        platform = platform_raw or "Unknown Platform"

    restaurant_name = (
        get_first_val(["restaurant", "restaurant_name", "venue", "recipient", "brand", "merchant"])
        or os.path.splitext(os.path.basename(filepath))[0]
    )

    p_start_raw = get_first_val(["period_start", "start_date", "from_date", "date_from"])
    p_end_raw = get_first_val(["period_end", "end_date", "to_date", "date_to"])
    if p_start_raw and p_end_raw:
        period_start, period_end = normalize_date_range(f"{p_start_raw} - {p_end_raw}")
    elif p_start_raw:
        period_start, period_end = normalize_date_range(p_start_raw)
    else:
        raise ValueError("CSV payout file is missing period_start / period_end columns.")

    currency = get_first_val(["currency"]) or "EUR"
    doc_num = get_first_val(["document_number", "invoice_number", "payout_id"])

    gross_sales = parse_money(get_first_val(["gross_sales_report", "gross_sales", "total_sales", "total goods sold"])) or 0.0
    net_sales = parse_money(get_first_val(["gross_sales_net_of_vat", "net_sales"]))
    vat_sales = parse_money(get_first_val(["vat_amount", "vat"]))
    payout_amt = parse_money(get_first_val(["payout_amount_gross", "payout_amount", "payout", "weekly earnings"]))

    cost_items: List[ParsedCostItem] = []

    if "cost_category" in lower_map and ("amount_gross" in lower_map or "amount" in lower_map):
        amt_col = lower_map.get("amount_gross") or lower_map.get("amount")
        net_col = lower_map.get("amount_net")
        vat_col = lower_map.get("vat_amount")
        lbl_col = lower_map.get("raw_label") or lower_map.get("label") or lower_map.get("description")
        cat_col = lower_map["cost_category"]
        note_col = lower_map.get("notes")

        for _, row in df.iterrows():
            cat_raw = str(row.get(cat_col, "")).strip()
            if not cat_raw or cat_raw.lower() == "nan":
                continue
            cat = cat_raw if cat_raw in VALID_COST_CATEGORIES else "other_adjustments"
            amt_g = parse_money(row.get(amt_col))
            if amt_g is None:
                continue
            lbl = str(row.get(lbl_col, cat)).strip() if lbl_col else cat
            amt_n = parse_money(row.get(net_col)) if net_col else None
            amt_v = parse_money(row.get(vat_col)) if vat_col else None
            note = str(row.get(note_col, "")).strip() if note_col and pd.notna(row.get(note_col)) else None
            cost_items.append(
                ParsedCostItem(
                    cost_category=cat,
                    raw_label=lbl,
                    amount_gross=round(amt_g, 2),
                    amount_net=round(amt_n, 2) if amt_n is not None else None,
                    vat_amount=round(amt_v, 2) if amt_v is not None else None,
                    notes=note,
                )
            )
    else:
        col_cat_map = [
            (["commission_gross", "commission", "agency_fee", "total_commissions_fees_gross"], "commission_and_fees", "Commission & Fees (CSV)"),
            (["platform_fee_gross", "platform_fee"], "commission_and_fees", "Platform Fee (CSV)"),
            (["ads_gross", "ad_campaign", "campaign_fee", "total_ads_campaigns_gross"], "ads_and_campaigns", "Ads & Campaigns (CSV)"),
            (["merchant_discounts_gross", "merchant_discounts", "total_merchant_discounts_gross"], "merchant_discounts", "Merchant Discounts (CSV)"),
            (["deductions_gross", "deductions", "refunds", "total_deductions_refunds_gross"], "deductions_and_refunds", "Deductions & Refunds (CSV)"),
            (["other_adjustments_gross", "adjustments", "total_other_adjustments_gross"], "other_adjustments", "Other Adjustments (CSV)"),
        ]
        for cols, cat, default_lbl in col_cat_map:
            val_str = get_first_val(cols)
            val = parse_money(val_str)
            if val is not None and val != 0.0:
                cost_items.append(
                    ParsedCostItem(
                        cost_category=cat,
                        raw_label=default_lbl,
                        amount_gross=round(abs(val), 2),
                    )
                )

    report = ParsedPayoutReport(
        platform=platform,
        document_type="csv_payout",
        raw_restaurant_name=restaurant_name,
        period_start=period_start,
        period_end=period_end,
        currency=currency,
        document_number=doc_num,
        gross_sales_report=round(gross_sales, 2),
        gross_sales_net_of_vat=round(net_sales, 2) if net_sales is not None else None,
        vat_amount=round(vat_sales, 2) if vat_sales is not None else None,
        payout_amount_gross=round(payout_amt, 2) if payout_amt is not None else None,
        cost_items=_dedup_doc_cost_items(cost_items),
        parse_status="ok",
    )
    report.recompute_totals()
    return report
