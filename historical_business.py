from __future__ import annotations

import io
import json
import re
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st
from google.auth.transport.requests import AuthorizedSession
from google.oauth2 import service_account


HISTORICAL_LEADS = {
    2024: "1gbt6difdXdvUnAX4UgxpIEZkt1sH8OXB8gOe-u_7k_w",
    2025: "1kAf4fFOPd1QT-9WKKgYuui0diVquWVlmTsieQFJPU7M",
    2026: "10Ctbh-Rsa5xc73sYCesi9lo605FwVyQpdiJYS2k_cX0",
}

HISTORICAL_CRM = {
    2024: "1MxtnzMIf21SBBnXvv7mCvpRuAqL_8EQhhBCRHcyxeuQ",
    2025: "1vUlDIHOz1YBjZRCt3uKKVA-p9VqU4-2vul6bYzIBXxY",
}

MONTH_NAMES = {
    "JAN": 1,
    "JANUARY": 1,
    "FEB": 2,
    "FEBRUARY": 2,
    "MAR": 3,
    "MARCH": 3,
    "APR": 4,
    "APRIL": 4,
    "MAY": 5,
    "JUN": 6,
    "JUNE": 6,
    "JUL": 7,
    "JULY": 7,
    "AUG": 8,
    "AUGUST": 8,
    "SEP": 9,
    "SEPTEMBER": 9,
    "OCT": 10,
    "OCTOBER": 10,
    "NOV": 11,
    "NOVEMBER": 11,
    "DEC": 12,
    "DECEMBER": 12,
}

DATA_DIR = Path(__file__).resolve().parent / "data"
JOINED_SNAPSHOT = DATA_DIR / "historical_joined.pkl.gz"


def export_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"


def canon(value) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def clean(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.replace(r"\s+", " ", regex=True).str.strip()


def amount(series: pd.Series) -> pd.Series:
    extracted = clean(series).str.replace(",", "", regex=False).str.extract(r"(-?\d+(?:\.\d+)?)", expand=False)
    return pd.to_numeric(extracted, errors="coerce").fillna(0.0)


def phone(series: pd.Series) -> pd.Series:
    return clean(series).str.replace(r"\D", "", regex=True).str.removeprefix("00").str[-12:]


def find_column(frame: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    lookup = {canon(column): column for column in frame.columns}
    for name in names:
        key = canon(name)
        if key in lookup:
            return lookup[key]
    for key, column in lookup.items():
        if any(canon(name) in key or key in canon(name) for name in names):
            return column
    return None


def col(frame: pd.DataFrame, names: tuple[str, ...], default="") -> pd.Series:
    found = find_column(frame, names)
    if found is None:
        return pd.Series(default, index=frame.index, dtype="object")
    return frame[found]


def month_number(tab_name: str) -> int | None:
    normalized = canon(tab_name).replace("CALLING", "")
    for token, number in MONTH_NAMES.items():
        if normalized.startswith(token):
            return number
    return None


def normalize_customer_path(value) -> str:
    value = canon(value)
    if not value:
        return "LEAD"
    if "MISSED" in value and "LEAD" in value:
        return "MISSED LEAD"
    if "BROADCAST" in value:
        return "BROADCAST"
    if "REORDER" in value and "LEAD" in value:
        return "LEAD & RE-ORDER"
    if "REORDER" in value:
        return "RE-ORDER"
    if "NEWENQUIRY" in value or "NEWINQUIRY" in value:
        return "NEW ENQUIRY"
    if value in {"LEAD", "SALESCLOSED", "SALECLOSED"}:
        return "LEAD"
    return str(value).replace("_", " ")


def normalize_order_status(value) -> str:
    value = canon(value)
    if value in {"WON", "ORDERCONFIRMED", "SALECLOSED", "SALESCLOSED", "CONFIRMED"}:
        return "ORDER CREATED"
    return "NOT CONVERTED"


def normalize_crm_status(value) -> str:
    value = canon(value)
    if any(token in value for token in ("SALECLOSED", "SALESCLOSED", "DELIVERED", "DEILVERD")):
        return "SALE CLOSED"
    if any(token in value for token in ("CANCEL", "RETURN", "PICKUPCANCEL")):
        return "CANCELLED / RETURN"
    if any(token in value for token in ("DISPATCH", "RESCHEDULE", "UNPAID", "RTOASSIGNED", "PENDING")):
        return "PENDING / IN PROCESS"
    return "OTHER / UNMAPPED"


def _google_service_account_info() -> dict | None:
    try:
        section = st.secrets.get("gcp_service_account")
        if section:
            return dict(section)
    except Exception:
        pass
    try:
        raw = st.secrets.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if raw:
            return json.loads(str(raw))
    except Exception:
        pass
    return None


def _read_google_workbook(sheet_id: str) -> dict[str, pd.DataFrame]:
    url = export_url(sheet_id)
    info = _google_service_account_info()
    if info:
        credentials = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        response = AuthorizedSession(credentials).get(url, timeout=180)
        response.raise_for_status()
        return pd.read_excel(io.BytesIO(response.content), sheet_name=None, dtype=object)
    return pd.read_excel(url, sheet_name=None, dtype=object)


def _read_monthly_workbook(sheet_id: str, year: int, through_month: int = 12) -> dict[str, pd.DataFrame]:
    selected: dict[str, pd.DataFrame] = {}
    for name, frame in _read_google_workbook(sheet_id).items():
        month = month_number(name)
        if month is None or month > through_month:
            continue
        selected[f"{year}-{month:02d}"] = frame
    return selected


def normalize_lead_sheet(frame: pd.DataFrame, month: str) -> pd.DataFrame:
    data = pd.DataFrame(index=frame.index)
    data["month"] = month
    data["date"] = pd.to_datetime(col(frame, ("DATE",)), errors="coerce", dayfirst=True, format="mixed")
    data["agent"] = clean(col(frame, ("AGENT", "STAFF"))).str.upper().replace("", "UNASSIGNED")
    data["customer_path"] = clean(col(frame, ("CUSTOMER PATH", "CUSTOMERPATH"))).map(normalize_customer_path)
    data["phone"] = phone(col(frame, ("PHONE", "PHONE NO 1", "NUMBER", "NUMBER1")))
    data["phone_2"] = phone(col(frame, ("PHONE 2", "PHONE NO 2", "NUMBER2")))
    data["product"] = clean(col(frame, ("PRODUCT 1", "PRODUCT", "PRODUCT1")))
    data["quantity"] = pd.to_numeric(col(frame, ("QTY", "QTY 1", "QUANTITY")), errors="coerce").fillna(0)
    data["order_value"] = amount(col(frame, ("VALUE", "TOTAL")))
    data["status_raw"] = clean(col(frame, ("STATUS",)))
    data["order_stage"] = data["status_raw"].map(normalize_order_status)
    data["is_order"] = data["order_stage"].eq("ORDER CREATED")
    data["country"] = clean(col(frame, ("COUNTRY", "STATE"))).str.upper().replace("", "UNKNOWN")
    data["vendor"] = clean(col(frame, ("VENDORE", "VENDOR"))).str.title().replace("", "UNMAPPED")
    data["source_row"] = frame.index + 2
    return data[data["phone"].ne("") | data["phone_2"].ne("")].reset_index(drop=True)


def normalize_crm_sheet(frame: pd.DataFrame, month: str) -> pd.DataFrame:
    data = pd.DataFrame(index=frame.index)
    data["month"] = month
    data["date_crm"] = pd.to_datetime(col(frame, ("DATE",)), errors="coerce", dayfirst=True, format="mixed")
    data["agent_crm"] = clean(col(frame, ("AGENT",))).str.upper().replace("", "UNASSIGNED")
    data["phone"] = phone(col(frame, ("NUMBER", "NUMBER1", "PHONE")))
    data["phone_2"] = phone(col(frame, ("NUMBER2", "PHONE 2")))
    data["em_number"] = clean(col(frame, ("EM NUMBER", "EMNUMBER"))).str.upper()
    data["tracking_number"] = clean(col(frame, ("TRACKING NUM", "TRACKING NUMBER"))).str.upper()
    data["product_crm"] = clean(col(frame, ("PRODUCT 1", "PRODUCT")))
    data["crm_value"] = amount(col(frame, ("VALUE", "TOTAL")))
    data["customer_path_crm"] = clean(col(frame, ("CUSTOMER PATH", "CUSTOMERPATH"))).map(normalize_customer_path)
    data["crm_status_raw"] = clean(col(frame, ("CS STATUS", "STATUS")))
    data["crm_outcome"] = data["crm_status_raw"].map(normalize_crm_status)
    data["reason"] = clean(col(frame, ("REASON", "NOTES", "REMARKS")))
    data["source_row_crm"] = frame.index + 2
    keep = data["phone"].ne("") | data["phone_2"].ne("") | data["em_number"].ne("")
    return data[keep].reset_index(drop=True)


def _build_normalized_sources() -> tuple[pd.DataFrame, pd.DataFrame]:
    lead_parts: list[pd.DataFrame] = []
    crm_parts: list[pd.DataFrame] = []

    for year, sheet_id in HISTORICAL_LEADS.items():
        through = 4 if year == 2026 else 12
        for month, frame in _read_monthly_workbook(sheet_id, year, through).items():
            lead_parts.append(normalize_lead_sheet(frame, month))

    for year, sheet_id in HISTORICAL_CRM.items():
        for month, frame in _read_monthly_workbook(sheet_id, year).items():
            crm_parts.append(normalize_crm_sheet(frame, month))

    leads = pd.concat(lead_parts, ignore_index=True) if lead_parts else pd.DataFrame()
    crm = pd.concat(crm_parts, ignore_index=True) if crm_parts else pd.DataFrame()
    return leads, crm


def preprocess_historical_dataset(leads: pd.DataFrame, crm: pd.DataFrame) -> pd.DataFrame:
    if leads.empty:
        return leads.copy()

    crm_primary = crm.copy()
    crm_primary["match_phone"] = crm_primary["phone"]
    crm_secondary = crm[crm["phone_2"].ne("")].copy()
    crm_secondary["match_phone"] = crm_secondary["phone_2"]
    crm_long = pd.concat([crm_primary, crm_secondary], ignore_index=True)

    priority = {
        "SALE CLOSED": 1,
        "CANCELLED / RETURN": 2,
        "PENDING / IN PROCESS": 3,
        "OTHER / UNMAPPED": 4,
    }
    crm_long["priority"] = crm_long["crm_outcome"].map(priority).fillna(9)
    crm_one = crm_long.sort_values(
        ["month", "match_phone", "priority", "source_row_crm"],
        kind="stable",
    ).drop_duplicates(["month", "match_phone"], keep="first")

    crm_columns = [
        "month",
        "match_phone",
        "crm_outcome",
        "crm_status_raw",
        "crm_value",
        "reason",
        "source_row_crm",
        "tracking_number",
        "em_number",
    ]

    joined = leads.reset_index(drop=True).merge(
        crm_one[crm_columns],
        left_on=["month", "phone"],
        right_on=["month", "match_phone"],
        how="left",
        sort=False,
    )

    missing_mask = joined["crm_outcome"].isna() & joined["phone_2"].ne("")
    if missing_mask.any():
        secondary_keys = joined.loc[missing_mask, ["month", "phone_2"]].copy()
        secondary_keys["_row_id"] = secondary_keys.index
        secondary_matches = secondary_keys.merge(
            crm_one[crm_columns],
            left_on=["month", "phone_2"],
            right_on=["month", "match_phone"],
            how="left",
            sort=False,
        ).set_index("_row_id")
        for column in [
            "crm_outcome",
            "crm_status_raw",
            "crm_value",
            "reason",
            "source_row_crm",
            "tracking_number",
            "em_number",
        ]:
            joined.loc[secondary_matches.index, column] = secondary_matches[column]

    joined["crm_outcome"] = joined["crm_outcome"].fillna("NOT FOUND IN CRM")
    joined["crm_status_raw"] = joined["crm_status_raw"].fillna("")
    joined["reason"] = joined["reason"].fillna("")
    joined["crm_value"] = pd.to_numeric(joined["crm_value"], errors="coerce").fillna(0.0)
    joined["final_revenue"] = joined["crm_value"].where(joined["crm_value"].gt(0), joined["order_value"])
    joined["is_closed"] = joined["crm_outcome"].eq("SALE CLOSED")
    joined["is_cancelled"] = joined["crm_outcome"].eq("CANCELLED / RETURN")
    joined["is_pending"] = joined["crm_outcome"].eq("PENDING / IN PROCESS")
    return joined


@st.cache_resource(show_spinner=False)
def load_historical_dataset() -> pd.DataFrame:
    if JOINED_SNAPSHOT.exists():
        return pd.read_pickle(JOINED_SNAPSHOT, compression="gzip")
    leads, crm = _build_normalized_sources()
    return preprocess_historical_dataset(leads, crm)


@st.cache_resource(show_spinner=False)
def load_historical_business() -> tuple[pd.DataFrame, pd.DataFrame]:
    dataset = load_historical_dataset()
    return dataset, pd.DataFrame()


def render_historical_business(
    leads: pd.DataFrame,
    crm: pd.DataFrame,
    meta_spend: pd.DataFrame,
    selected_month: str,
    selected_view: str = "Executive overview",
) -> None:
    """Render only the requested section.

    Streamlit tabs execute every tab body on every rerun. Historical data is large,
    so a single-view renderer keeps navigation responsive and avoids constructing
    hidden charts and full row-level tables.
    """
    if "crm_outcome" in leads.columns:
        joined = leads.loc[leads["month"].eq(selected_month)].copy()
    else:
        month_leads = leads.loc[leads["month"].eq(selected_month)].copy()
        month_crm = crm.loc[crm["month"].eq(selected_month)].copy()
        joined = preprocess_historical_dataset(month_leads, month_crm)

    if joined.empty:
        st.warning("No historical lead rows were found for this month.")
        return

    new_leads = joined.loc[joined["customer_path"].eq("LEAD")]
    orders = joined.loc[joined["is_order"]]
    closed = joined.loc[joined["is_closed"]]
    cancelled = joined.loc[joined["is_cancelled"]]
    pending = joined.loc[joined["is_pending"]]

    total_spend = (
        float(pd.to_numeric(meta_spend["spend"], errors="coerce").fillna(0).sum())
        if not meta_spend.empty and "spend" in meta_spend.columns
        else 0.0
    )
    order_value = float(pd.to_numeric(orders["order_value"], errors="coerce").fillna(0).sum())
    closed_revenue = float(pd.to_numeric(closed["final_revenue"], errors="coerce").fillna(0).sum())
    cpl = total_spend / len(new_leads) if len(new_leads) and total_spend else 0.0
    order_rate = len(orders) / len(new_leads) * 100 if len(new_leads) else 0.0
    close_rate = len(closed) / len(orders) * 100 if len(orders) else 0.0
    cancel_rate = len(cancelled) / len(orders) * 100 if len(orders) else 0.0
    roas = closed_revenue / total_spend if total_spend else 0.0

    st.markdown(
        f'<div class="hero"><h2>Historical business command centre</h2><p>'
        f'{pd.Period(selected_month).strftime("%B %Y")} · Preprocessed leads + Sales CRM outcomes'
        f'{" + Meta spend" if total_spend else ""}</p></div>',
        unsafe_allow_html=True,
    )

    if selected_view == "Executive overview":
        row1 = st.columns(5)
        row1[0].metric("New leads", f"{len(new_leads):,}")
        row1[1].metric("Agent-created orders", f"{len(orders):,}", f"{order_rate:.1f}% of leads")
        row1[2].metric("Initial order value", f"AED {order_value:,.2f}")
        row1[3].metric("Final sales closed", f"{len(closed):,}", f"{close_rate:.1f}% of orders")
        row1[4].metric("Final closed revenue", f"AED {closed_revenue:,.2f}")

        row2 = st.columns(5)
        row2[0].metric("Cancelled / returned", f"{len(cancelled):,}", f"{cancel_rate:.1f}% of orders")
        row2[1].metric("Pending / in process", f"{len(pending):,}")
        row2[2].metric("Meta spend", f"AED {total_spend:,.2f}" if total_spend else "Not loaded")
        row2[3].metric("CPL", f"AED {cpl:,.2f}" if cpl else "Not loaded")
        row2[4].metric("Final Meta ROAS", f"{roas:.2f}×" if roas else "Not loaded")

        funnel = pd.DataFrame({
            "Stage": ["New leads", "Agent-created orders", "Final sales closed"],
            "Count": [len(new_leads), len(orders), len(closed)],
        })
        st.plotly_chart(
            px.funnel(funnel, x="Count", y="Stage", title="Business conversion funnel"),
            use_container_width=True,
        )
        return

    if selected_view == "Customer paths":
        path = joined.groupby("customer_path", as_index=False).agg(
            records=("source_row", "size"),
            orders=("is_order", "sum"),
            initial_value=("order_value", "sum"),
            closed_sales=("is_closed", "sum"),
            cancelled=("is_cancelled", "sum"),
        )
        path["order_conversion_pct"] = path["orders"].div(path["records"].replace(0, pd.NA)).mul(100)
        path["final_close_pct"] = path["closed_sales"].div(path["orders"].replace(0, pd.NA)).mul(100)
        st.dataframe(path.sort_values("records", ascending=False), hide_index=True, use_container_width=True)
        st.plotly_chart(
            px.bar(
                path,
                x="customer_path",
                y=["records", "orders", "closed_sales"],
                barmode="group",
                title="Customer-path performance",
            ),
            use_container_width=True,
        )
        st.caption("Only LEAD is used as the Meta CPL denominator. Other customer paths remain separate.")
        return

    if selected_view == "Sales CRM outcomes":
        outcomes = joined.groupby("crm_outcome", as_index=False).agg(
            records=("source_row", "size"),
            amount=("final_revenue", "sum"),
        )
        outcomes["share_pct"] = outcomes["records"].div(len(joined)).mul(100)
        st.dataframe(outcomes.sort_values("records", ascending=False), hide_index=True, use_container_width=True)
        reasons = cancelled.groupby("reason", as_index=False).size().sort_values("size", ascending=False)
        st.markdown("#### Cancellation and rejection reasons")
        st.dataframe(reasons.head(100), hide_index=True, use_container_width=True)
        return

    if selected_view == "Agents":
        agent = joined.groupby("agent", as_index=False).agg(
            handled=("source_row", "size"),
            new_leads=("customer_path", lambda values: values.eq("LEAD").sum()),
            orders=("is_order", "sum"),
            initial_value=("order_value", "sum"),
            closed_sales=("is_closed", "sum"),
            closed_revenue=("final_revenue", lambda values: values[joined.loc[values.index, "is_closed"]].sum()),
            cancelled=("is_cancelled", "sum"),
        )
        agent["lead_to_order_pct"] = agent["orders"].div(agent["new_leads"].replace(0, pd.NA)).mul(100)
        agent["order_to_close_pct"] = agent["closed_sales"].div(agent["orders"].replace(0, pd.NA)).mul(100)
        st.dataframe(
            agent.sort_values(["closed_sales", "orders"], ascending=False),
            hide_index=True,
            use_container_width=True,
        )
        return

    if selected_view == "Marketing":
        if not total_spend:
            st.info("Click “Load live Meta spend” above to add spend, CPL and ROAS.")
        metrics = pd.DataFrame([
            {"Metric": "Meta spend", "Value": total_spend},
            {"Metric": "New leads", "Value": len(new_leads)},
            {"Metric": "CPL", "Value": cpl},
            {"Metric": "Agent-created orders", "Value": len(orders)},
            {"Metric": "Final closed sales", "Value": len(closed)},
            {"Metric": "Final closed revenue", "Value": closed_revenue},
            {"Metric": "Final ROAS", "Value": roas},
        ])
        st.dataframe(metrics, hide_index=True, use_container_width=True)
        if not meta_spend.empty and {"date", "spend"}.issubset(meta_spend.columns):
            daily = meta_spend.groupby("date", as_index=False)["spend"].sum()
            st.plotly_chart(
                px.line(daily, x="date", y="spend", markers=True, title="Daily Meta spend"),
                use_container_width=True,
            )
        return

    leakage = order_value - closed_revenue
    unmatched = int(joined["crm_outcome"].eq("NOT FOUND IN CRM").sum())
    st.markdown("#### Automated business insights")
    st.write(f"- Revenue leakage from agent-created value to final closed revenue: **AED {leakage:,.2f}**.")
    st.write(f"- Final order-to-close conversion: **{close_rate:.1f}%**.")
    st.write(f"- Cancellation/return ratio after agent conversion: **{cancel_rate:.1f}%**.")
    st.write(f"- Records not matched to Sales CRM: **{unmatched:,}**.")
    st.markdown("#### Row-level reconciliation")
    show_rows = st.checkbox("Load detailed reconciliation rows", value=False)
    if show_rows:
        row_limit = st.selectbox("Rows to display", [250, 500, 1000, 2500], index=0)
        st.dataframe(joined.head(row_limit), hide_index=True, use_container_width=True)
        if len(joined) > row_limit:
            st.caption(f"Showing {row_limit:,} of {len(joined):,} rows.")
