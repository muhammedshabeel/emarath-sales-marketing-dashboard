from __future__ import annotations

import io
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import pandas as pd


HISTORICAL_SHEET_ID = "1txj_IlJ_t8SqEzh4kJQ9eGcTkSrLW5emZ0EE0HCPspE"

# Monthly lead workbooks are the authoritative source for lead volume.  The
# aligned historical workbook remains the source for orders, revenue and
# repeat-order analysis.  Do not deduplicate phone values: repeat enquiries
# are valid lead rows.
HISTORICAL_LEAD_SOURCES = {
    "2025-01": {
        "spreadsheet_id": "1LAJqVKrqbAsVKge-fopfhCBOWjxf8-Lt9CalRWfENdY",
        "sheet_name": "CRM",
        "phone_column": "PHONE",
    },
}


@dataclass(frozen=True)
class Layout:
    name: str
    date: int
    country: int | None
    agent: int
    customer_path: int
    customer_name: int
    phone: int
    secondary_phone: int | None
    product: int
    quantity: int
    value: int
    lead_source: int
    ad_source: int
    status: int
    order_id: int | None


# Check the newest, most explicit layout first. A small number of free-text
# fields happen to resemble dates; country + date must win over those values.
LAYOUTS = (
    Layout("current_country_first", 1, 0, 2, 3, 4, 5, 6, 11, 12, 22, 23, 15, 24, 29),
    Layout("legacy_date_first", 0, None, 1, 2, 3, 4, 5, 10, 11, 21, 22, 14, 23, None),
    Layout("legacy_country_first", 3, 1, 2, 15, 4, 5, 16, 8, 9, 12, 17, 18, 20, None),
)

COUNTRY_PREFIXES = {
    "971": "UAE",
    "966": "KSA",
    "974": "QATAR",
    "973": "BAHRAIN",
    "965": "KUWAIT",
    "968": "OMAN",
}

VENDOR_PROFIT_PER_ORDER = {
    "Scent Passion": 40.0,
    "Oud Al Salam": 50.0,
    "La Parfume (LPG)": 30.0,
    "RT Fragrance": 30.0,
    "Al Hajees": 40.0,
    "Athiyaf": 30.0,
}

VENDOR_PRODUCT_TERMS = (
    ("Oud Al Salam", ("AL HUDA", "ALHUDA", "PREMIUM EDITION", "LUMINUX")),
    ("La Parfume (LPG)", ("INTENSE", "INTENS ", "OUD LOVER", "FALCON")),
    ("RT Fragrance", ("SEVEN DAYS", "OLD MEMORIES", "OLD MEMMORIES", "MYSTERY", "PEACOCK")),
    ("Athiyaf", ("ARCHER", "HECTOR", "VOLGA", "ASEEL", "MIRAMAR", "SHADOW FLAME", "COLLECTION OF MOODS")),
)


def classify_vendor(product) -> str:
    normalized = re.sub(r"[^A-Z0-9]+", " ", str(product or "").upper()).strip()
    for vendor, terms in VENDOR_PRODUCT_TERMS:
        if any(re.sub(r"[^A-Z0-9]+", " ", term).strip() in normalized for term in terms):
            return vendor
    # Confirmed business rule: all remaining historical products belong to
    # Scent Passion, including Ambre/Oniro, Laroche, Doller, Ferragamo and Sufi.
    return "Scent Passion"


def spreadsheet_id(value: str) -> str:
    value = str(value or "").strip()
    match = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", value)
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{20,}", value):
        return value
    raise ValueError("Enter a valid Google Sheets URL or spreadsheet ID.")


def export_url(value: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{spreadsheet_id(value)}/export?format=xlsx"


def _date_values(series: pd.Series) -> pd.Series:
    text = series.astype("string").str.strip()
    looks_like_date = text.str.match(
        r"^(?:\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{4}[/-]\d{1,2}[/-]\d{1,2})(?:\s|$)",
        na=False,
    )
    return pd.to_datetime(text.where(looks_like_date), errors="coerce", dayfirst=True, format="mixed")


def _clean_text(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.replace(r"\s+", " ", regex=True).str.strip()


def _phone_digits(series: pd.Series) -> pd.Series:
    values = _clean_text(series)
    numeric = pd.to_numeric(values, errors="coerce")
    integer_like = numeric.notna() & numeric.mod(1).eq(0)
    values.loc[integer_like] = numeric.loc[integer_like].map(lambda value: f"{value:.0f}")
    return values.str.replace(r"\D", "", regex=True).str.removeprefix("00")


def _amount(series: pd.Series) -> pd.Series:
    text = _clean_text(series).str.replace(",", "", regex=False)
    number = text.str.extract(r"(-?\d+(?:\.\d+)?)", expand=False)
    return pd.to_numeric(number, errors="coerce").fillna(0.0)


def _canonical_country(country: pd.Series, phone: pd.Series) -> pd.Series:
    result = _clean_text(country).str.upper().replace(
        {"UNITED ARAB EMIRATES": "UAE", "SAUDI ARABIA": "KSA", "QATAR ": "QATAR"}
    )
    for prefix, label in COUNTRY_PREFIXES.items():
        result = result.mask(result.eq("") & phone.str.startswith(prefix), label)
    return result.mask(result.eq(""), "UNKNOWN")


def read_historical_workbook(source) -> pd.DataFrame:
    """Read a Google Sheet export, uploaded workbook, bytes, or local path."""
    if hasattr(source, "getvalue"):
        source = io.BytesIO(source.getvalue())
    elif isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(source)
    elif isinstance(source, str) and source.startswith(("http://", "https://")):
        source = export_url(source)
    return pd.read_excel(source, sheet_name="Sheet1", header=None, dtype=object)


def read_monthly_lead_count(month: str) -> int | None:
    """Return populated lead-phone rows from the authoritative monthly CRM.

    Repeated phone numbers are deliberately retained.  Only blank phone cells
    are excluded, matching the operational definition of an uploaded lead.
    """
    source = HISTORICAL_LEAD_SOURCES.get(str(month))
    if not source:
        return None
    url = export_url(source["spreadsheet_id"])
    frame = pd.read_excel(url, sheet_name=source["sheet_name"], dtype=object)
    normalized = {
        re.sub(r"[^A-Z0-9]+", "", str(column).upper()): column
        for column in frame.columns
    }
    wanted = re.sub(r"[^A-Z0-9]+", "", source["phone_column"].upper())
    phone_column = normalized.get(wanted)
    if phone_column is None:
        raise ValueError(
            f"{source['sheet_name']} does not contain the expected "
            f"{source['phone_column']} column."
        )
    return int(_clean_text(frame[phone_column]).ne("").sum())


def normalize_historical_rows(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalize mixed CRM exports without deduplicating customer phones.

    The row grain is one original spreadsheet row. Repeated phone numbers are
    expected re-enquiries/reorders and remain separate. Source row numbers make
    every dashboard value traceable to the workbook.
    """
    if raw.shape[1] < 25:
        raise ValueError("Historical workbook must contain at least 25 columns.")

    normalized_parts = []
    assigned = pd.Series(False, index=raw.index)
    for layout in LAYOUTS:
        dates = _date_values(raw.iloc[:, layout.date])
        mask = dates.notna() & ~assigned
        if not mask.any():
            continue
        rows = raw.loc[mask]
        out = pd.DataFrame(index=rows.index)
        out["source_row"] = rows.index + 1
        out["source_layout"] = layout.name
        out["lead_date"] = dates.loc[mask]
        out["country_raw"] = rows.iloc[:, layout.country] if layout.country is not None else ""
        out["agent"] = _clean_text(rows.iloc[:, layout.agent]).str.upper().replace("", "UNASSIGNED")
        out["customer_path"] = _clean_text(rows.iloc[:, layout.customer_path])
        out["customer_name"] = _clean_text(rows.iloc[:, layout.customer_name])
        out["phone_raw"] = _clean_text(rows.iloc[:, layout.phone])
        out["phone"] = _phone_digits(rows.iloc[:, layout.phone])
        if layout.secondary_phone is not None:
            out["secondary_phone"] = _phone_digits(rows.iloc[:, layout.secondary_phone])
        else:
            out["secondary_phone"] = ""
        out["product"] = _clean_text(rows.iloc[:, layout.product])
        out["quantity"] = pd.to_numeric(rows.iloc[:, layout.quantity], errors="coerce")
        out["order_value"] = _amount(rows.iloc[:, layout.value])
        out["lead_source"] = _clean_text(rows.iloc[:, layout.lead_source]).str.upper()
        out["ad_source"] = _clean_text(rows.iloc[:, layout.ad_source])
        out["status"] = _clean_text(rows.iloc[:, layout.status]).str.upper()
        if layout.order_id is not None:
            out["source_order_id"] = _clean_text(rows.iloc[:, layout.order_id]).str.replace(r"\.0$", "", regex=True)
        else:
            out["source_order_id"] = ""
        normalized_parts.append(out)
        assigned |= mask

    if not normalized_parts:
        raise ValueError("No valid historical dates were detected in the workbook.")

    data = pd.concat(normalized_parts).sort_index().reset_index(drop=True)
    data = data[data["lead_date"].dt.year.between(2025, 2100)].copy()
    data["country"] = _canonical_country(data["country_raw"], data["phone"])
    data["is_won"] = data["status"].eq("WON")
    data["vendor"] = data["product"].map(classify_vendor)
    data["profit_per_order"] = data["vendor"].map(VENDOR_PROFIT_PER_ORDER).fillna(0.0)
    data["estimated_profit"] = data["profit_per_order"].where(data["is_won"], 0.0)
    data["order_id"] = data["source_order_id"].where(
        data["source_order_id"].ne(""), data["source_row"].map(lambda row: f"SOURCE-ROW-{row}")
    )
    data["month"] = data["lead_date"].dt.to_period("M").astype(str)
    data["phone_key"] = data["phone"].where(data["phone"].ne(""), "ROW-" + data["source_row"].astype(str))
    data["exact_duplicate"] = data.duplicated(
        ["lead_date", "agent", "phone", "product", "quantity", "order_value", "customer_path", "status"],
        keep=False,
    )

    # Repeated phones remain separate rows. Classification only labels whether
    # a won row is the customer's first observed win or a later reorder.
    won_order = data.loc[data["is_won"]].sort_values(["lead_date", "source_row"]).copy()
    won_order["customer_order_number"] = won_order.groupby("phone_key").cumcount() + 1
    data["customer_order_number"] = pd.Series(pd.NA, index=data.index, dtype="Int64")
    data.loc[won_order.index, "customer_order_number"] = won_order["customer_order_number"].astype("Int64")
    data["order_type"] = "Not won"
    data.loc[data["is_won"] & data["customer_order_number"].eq(1), "order_type"] = "First-time order"
    data.loc[data["is_won"] & data["customer_order_number"].gt(1), "order_type"] = "Repeat order"
    return data.reset_index(drop=True)


def monthly_summary(data: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for month, group in data.groupby("month", sort=True):
        won = group[group["is_won"]]
        first = won[won["order_type"].eq("First-time order")]
        repeat = won[won["order_type"].eq("Repeat order")]
        rows.append({
            "month": month,
            "leads": len(group),
            "won_orders": len(won),
            "conversion_rate": len(won) / len(group) * 100 if len(group) else 0,
            "revenue": won["order_value"].sum(),
            "first_time_orders": len(first),
            "repeat_orders": len(repeat),
            "repeat_revenue": repeat["order_value"].sum(),
            "active_agents": group.loc[group["agent"].ne("UNASSIGNED"), "agent"].nunique(),
            "estimated_profit": won["estimated_profit"].sum(),
        })
    return pd.DataFrame(rows)


def dimension_summary(data: pd.DataFrame, dimension: str) -> pd.DataFrame:
    if dimension not in data.columns:
        raise KeyError(dimension)
    result = data.groupby(dimension, dropna=False).agg(
        leads=("source_row", "size"),
        won_orders=("is_won", "sum"),
        revenue=("order_value", lambda values: values[data.loc[values.index, "is_won"]].sum()),
        repeat_orders=("order_type", lambda values: values.eq("Repeat order").sum()),
        estimated_profit=("estimated_profit", "sum"),
    ).reset_index()
    result["conversion_rate"] = result["won_orders"].div(result["leads"]).mul(100)
    return result.sort_values(["won_orders", "leads"], ascending=False)


def quality_summary(raw: pd.DataFrame, data: pd.DataFrame) -> pd.DataFrame:
    dated_rows = sum(_date_values(raw.iloc[:, layout.date]).notna().sum() for layout in LAYOUTS)
    return pd.DataFrame([
        {"check": "Workbook rows", "value": len(raw), "severity": "Info"},
        {"check": "Normalized dated rows", "value": len(data), "severity": "OK"},
        {"check": "Rows without usable phone", "value": int(data["phone"].eq("").sum()), "severity": "Review"},
        {"check": "Rows without assigned agent", "value": int(data["agent"].eq("UNASSIGNED").sum()), "severity": "Review"},
        {"check": "Won rows without value", "value": int((data["is_won"] & data["order_value"].eq(0)).sum()), "severity": "Review"},
        {"check": "Repeated phone rows retained", "value": int(data["phone"].ne("").sum() - data.loc[data["phone"].ne(""), "phone"].nunique()), "severity": "Expected"},
        {"check": "Possible exact duplicate rows retained", "value": int(data["exact_duplicate"].sum()), "severity": "Review"},
        {"check": "Unclassified source rows", "value": int(len(raw) - len(data)), "severity": "Info"},
    ])
