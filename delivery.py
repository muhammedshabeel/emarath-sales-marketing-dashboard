from __future__ import annotations

import pandas as pd

from data_io import phone_digits


PAID_STATUS = "SALE CLOSED"
UNPAID_STATUS = "DELIVERD/UNPAID"
CANCELLED_STATUS = "CANCELLD&RETURN"


def _clean_text(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.strip()


def _status_key(series: pd.Series) -> pd.Series:
    return (
        _clean_text(series)
        .str.upper()
        .str.replace(r"\s+", " ", regex=True)
        .str.replace("DELIVERED/UNPAID", UNPAID_STATUS, regex=False)
        .str.replace("CANCELLED&RETURN", CANCELLED_STATUS, regex=False)
    )


def _identifier(series: pd.Series) -> pd.Series:
    values = _clean_text(series)
    numeric = pd.to_numeric(values, errors="coerce")
    integer_like = numeric.notna() & numeric.mod(1).eq(0)
    values.loc[integer_like] = numeric.loc[integer_like].map(lambda value: f"{value:.0f}")
    return values.str.upper().str.replace(r"\s+", "", regex=True)


def normalize_delivery(source: pd.DataFrame, mapping: dict, source_tz: str, report_tz: str) -> pd.DataFrame:
    """Normalize the delivery CRM at one row per tracking ID.

    ``CS STATUS = Sale closed`` is deliberately authoritative for paid revenue.
    Reason, notes and the spreadsheet workflow flags do not override it.
    """
    if source is None or source.empty:
        return pd.DataFrame()

    required = ["tracking_id", "status"]
    missing = [role for role in required if not mapping.get(role) or mapping[role] not in source]
    if missing:
        raise ValueError("Delivery CRM requires mapped columns: " + ", ".join(missing))

    frame = pd.DataFrame(index=source.index)
    frame["tracking_id"] = _identifier(source[mapping["tracking_id"]])
    frame = frame[frame["tracking_id"].ne("")].copy()
    frame["delivery_status_raw"] = _clean_text(source.loc[frame.index, mapping["status"]])
    frame["delivery_status"] = _status_key(source.loc[frame.index, mapping["status"]])

    def text(role, default=""):
        column = mapping.get(role)
        if column and column in source:
            return _clean_text(source.loc[frame.index, column])
        return pd.Series(default, index=frame.index, dtype="string")

    frame["sales_agent"] = text("agent", "UNASSIGNED").replace("", "UNASSIGNED")
    frame["delivery_partner"] = text("delivery_partner", "UNASSIGNED").replace("", "UNASSIGNED")
    frame["country"] = text("country", "UNMAPPED").replace("", "UNMAPPED")
    frame["customer_name"] = text("customer_name")
    frame["payment_method"] = text("payment_method")
    frame["vendor"] = text("vendor", "UNMAPPED").replace("", "UNMAPPED")
    frame["product"] = text("product")

    phone_one = text("phone")
    phone_two = text("phone_secondary")
    frame["customer_phone"] = phone_digits(phone_one)
    fallback = frame["customer_phone"].eq("")
    frame.loc[fallback, "customer_phone"] = phone_digits(phone_two.loc[fallback])
    frame["phone_key"] = frame["customer_phone"].str[-8:]

    amount_column = mapping.get("amount")
    frame["order_value"] = (
        pd.to_numeric(source.loc[frame.index, amount_column], errors="coerce").fillna(0.0)
        if amount_column and amount_column in source
        else 0.0
    )

    for role, output in (("order_date", "order_date"), ("outcome_date", "outcome_date"), ("unpaid_date", "unpaid_date")):
        column = mapping.get(role)
        parsed = pd.to_datetime(source.loc[frame.index, column], errors="coerce", dayfirst=True, format="mixed") if column and column in source else pd.Series(pd.NaT, index=frame.index, dtype="datetime64[ns]")
        if getattr(parsed.dt, "tz", None) is None:
            parsed = parsed.dt.tz_localize(source_tz, ambiguous="NaT", nonexistent="shift_forward")
        frame[output] = parsed.dt.tz_convert(report_tz)

    frame["is_paid"] = frame["delivery_status"].eq(PAID_STATUS)
    frame["is_delivered_unpaid"] = frame["delivery_status"].eq(UNPAID_STATUS)
    frame["is_cancelled_returned"] = frame["delivery_status"].eq(CANCELLED_STATUS)
    frame["is_delivered"] = frame["is_paid"] | frame["is_delivered_unpaid"]
    frame["realized_revenue"] = frame["order_value"].where(frame["is_paid"], 0.0)
    frame["unpaid_value"] = frame["order_value"].where(frame["is_delivered_unpaid"], 0.0)

    duplicates = frame["tracking_id"].duplicated(keep=False)
    if duplicates.any():
        duplicate_ids = frame.loc[duplicates, "tracking_id"].nunique()
        raise ValueError(f"Delivery CRM contains {duplicate_ids:,} duplicate tracking IDs. Resolve them before reporting exact cash outcomes.")
    return frame.reset_index(drop=True)


def delivery_kpis(delivery: pd.DataFrame) -> dict:
    paid = int(delivery["is_paid"].sum())
    unpaid = int(delivery["is_delivered_unpaid"].sum())
    cancelled = int(delivery["is_cancelled_returned"].sum())
    resolved = paid + unpaid + cancelled
    delivered = paid + unpaid
    return {
        "orders": int(len(delivery)),
        "paid_orders": paid,
        "realized_revenue": float(delivery["realized_revenue"].sum()),
        "unpaid_orders": unpaid,
        "unpaid_value": float(delivery["unpaid_value"].sum()),
        "cancelled_returned": cancelled,
        "delivered_orders": delivered,
        "pending_orders": int(len(delivery) - resolved),
        "delivery_success_rate": delivered / resolved * 100 if resolved else 0.0,
        "collection_rate": paid / delivered * 100 if delivered else 0.0,
        "return_rate": cancelled / resolved * 100 if resolved else 0.0,
    }


def delivery_summary(delivery: pd.DataFrame, dimension: str) -> pd.DataFrame:
    if delivery.empty:
        return pd.DataFrame()
    summary = delivery.groupby(dimension, dropna=False).agg(
        orders=("tracking_id", "size"),
        paid_orders=("is_paid", "sum"),
        realized_revenue=("realized_revenue", "sum"),
        delivered_unpaid=("is_delivered_unpaid", "sum"),
        unpaid_value=("unpaid_value", "sum"),
        cancelled_returned=("is_cancelled_returned", "sum"),
    ).reset_index()
    resolved = summary["paid_orders"] + summary["delivered_unpaid"] + summary["cancelled_returned"]
    delivered = summary["paid_orders"] + summary["delivered_unpaid"]
    summary["delivery_success_rate"] = delivered.div(resolved.replace(0, pd.NA)).mul(100)
    summary["collection_rate"] = summary["paid_orders"].div(delivered.replace(0, pd.NA)).mul(100)
    return summary.sort_values(["realized_revenue", "paid_orders"], ascending=False)
