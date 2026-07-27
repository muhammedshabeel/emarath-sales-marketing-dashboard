from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass

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
    "JAN": 1, "JANUARY": 1, "FEB": 2, "FEBRUARY": 2, "MARCH": 3,
    "APR": 4, "APRIL": 4, "MAY": 5, "JUNE": 6, "JULY": 7,
    "AUG": 8, "AUGUST": 8, "SEP": 9, "SEPTEMBER": 9,
    "OCT": 10, "OCTOBER": 10, "NOV": 11, "NOVEMBER": 11,
    "DEC": 12, "DECEMBER": 12,
}


def export_url(sheet_id: str) -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=xlsx"


def canon(value) -> str:
    return re.sub(r"[^A-Z0-9]+", "", str(value or "").upper())


def clean(series: pd.Series) -> pd.Series:
    return series.astype("string").fillna("").str.replace(r"\s+", " ", regex=True).str.strip()


def amount(series: pd.Series) -> pd.Series:
    return pd.to_numeric(
        clean(series).str.replace(",", "", regex=False).str.extract(r"(-?\d+(?:\.\d+)?)", expand=False),
        errors="coerce",
    ).fillna(0.0)


def phone(series: pd.Series) -> pd.Series:
    return clean(series).str.replace(r"\D", "", regex=True).str.removeprefix("00").str[-12:]


def find_column(frame: pd.DataFrame, names: tuple[str, ...]) -> str | None:
    lookup = {canon(column): column for column in frame.columns}
    for name in names:
        if canon(name) in lookup:
            return lookup[canon(name)]
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
    if "NEWENQUIRY" in value or "NEWENQUIRY" in value:
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
    try:
        return pd.read_excel(url, sheet_name=None, dtype=object)
    except Exception as exc:
        if "401" in str(exc) or "Unauthorized" in str(exc):
            raise RuntimeError(
                "Google Sheets returned 401 Unauthorized. Either set each source sheet to "
                "Anyone with the link - Viewer, or add a gcp_service_account section in "
                "Streamlit Secrets and share all source sheets with that service-account email."
            ) from exc
        raise


def _read_monthly_workbook(sheet_id: str, year: int, through_month: int = 12) -> dict[str, pd.DataFrame]:
    sheets = _read_google_workbook(sheet_id)
    selected = {}
    for name, frame in sheets.items():
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
    data["date"] = pd.to_datetime(col(frame, ("DATE",)), errors="coerce", dayfirst=True, format="mixed")
    data["agent"] = clean(col(frame, ("AGENT",))).str.upper().replace("", "UNASSIGNED")
    data["phone"] = phone(col(frame, ("NUMBER", "NUMBER1", "PHONE")))
    data["phone_2"] = phone(col(frame, ("NUMBER2", "PHONE 2")))
    data["em_number"] = clean(col(frame, ("EM NUMBER", "EMNUMBER"))).str.upper()
    data["tracking_number"] = clean(col(frame, ("TRACKING NUM", "TRACKING NUMBER"))).str.upper()
    data["product"] = clean(col(frame, ("PRODUCT 1", "PRODUCT")))
    data["crm_value"] = amount(col(frame, ("VALUE", "TOTAL")))
    data["customer_path_crm"] = clean(col(frame, ("CUSTOMER PATH", "CUSTOMERPATH"))).map(normalize_customer_path)
    data["crm_status_raw"] = clean(col(frame, ("CS STATUS", "STATUS")))
    data["crm_outcome"] = data["crm_status_raw"].map(normalize_crm_status)
    data["reason"] = clean(col(frame, ("REASON", "NOTES", "REMARKS")))
    data["source_row_crm"] = frame.index + 2
    return data[data["phone"].ne("") | data["phone_2"].ne("") | data["em_number"].ne("")].reset_index(drop=True)


@st.cache_data(ttl=21600, show_spinner=False)
def load_historical_business() -> tuple[pd.DataFrame, pd.DataFrame]:
    lead_parts = []
    crm_parts = []
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


def match_crm(leads: pd.DataFrame, crm: pd.DataFrame) -> pd.DataFrame:
    crm_long = pd.concat([
        crm.assign(match_phone=crm["phone"]),
        crm[crm["phone_2"].ne("")].assign(match_phone=crm.loc[crm["phone_2"].ne(""), "phone_2"]),
    ], ignore_index=True)
    priority = {"SALE CLOSED": 1, "CANCELLED / RETURN": 2, "PENDING / IN PROCESS": 3, "OTHER / UNMAPPED": 4}
    crm_long["priority"] = crm_long["crm_outcome"].map(priority).fillna(9)
    crm_one = crm_long.sort_values(["month", "match_phone", "priority", "source_row_crm"]).drop_duplicates(
        ["month", "match_phone"], keep="first"
    )
    result = leads.merge(
        crm_one[["month", "match_phone", "crm_outcome", "crm_status_raw", "crm_value", "reason", "source_row_crm"]],
        left_on=["month", "phone"], right_on=["month", "match_phone"], how="left",
    )
    missing = result["crm_outcome"].isna() & result["phone_2"].ne("")
    if missing.any():
        secondary = leads.loc[missing].merge(
            crm_one[["month", "match_phone", "crm_outcome", "crm_status_raw", "crm_value", "reason", "source_row_crm"]],
            left_on=["month", "phone_2"], right_on=["month", "match_phone"], how="left",
        )
        for column in ["crm_outcome", "crm_status_raw", "crm_value", "reason", "source_row_crm"]:
            result.loc[missing, column] = secondary[column].to_numpy()
    result["crm_outcome"] = result["crm_outcome"].fillna("NOT FOUND IN CRM")
    result["crm_value"] = pd.to_numeric(result["crm_value"], errors="coerce").fillna(0.0)
    return result


def render_historical_business(leads: pd.DataFrame, crm: pd.DataFrame, meta_spend: pd.DataFrame, selected_month: str) -> None:
    joined = match_crm(leads[leads["month"].eq(selected_month)].copy(), crm[crm["month"].eq(selected_month)].copy())
    if joined.empty:
        st.warning("No historical lead rows were found for this month.")
        return

    new_leads = joined[joined["customer_path"].eq("LEAD")]
    orders = joined[joined["is_order"]]
    closed = joined[joined["crm_outcome"].eq("SALE CLOSED")]
    cancelled = joined[joined["crm_outcome"].eq("CANCELLED / RETURN")]
    pending = joined[joined["crm_outcome"].eq("PENDING / IN PROCESS")]
    total_spend = float(meta_spend["spend"].sum()) if not meta_spend.empty else 0.0
    order_value = float(orders["order_value"].sum())
    closed_revenue = float(closed["crm_value"].where(closed["crm_value"].gt(0), closed["order_value"]).sum())
    cpl = total_spend / len(new_leads) if len(new_leads) else 0.0
    order_rate = len(orders) / len(new_leads) * 100 if len(new_leads) else 0.0
    close_rate = len(closed) / len(orders) * 100 if len(orders) else 0.0
    cancel_rate = len(cancelled) / len(orders) * 100 if len(orders) else 0.0
    roas = closed_revenue / total_spend if total_spend else 0.0

    st.markdown(f'<div class="hero"><h2>Historical business command centre</h2><p>{pd.Period(selected_month).strftime("%B %Y")} · Leads sheet → agent orders → Sales CRM final outcome → Meta spend</p></div>', unsafe_allow_html=True)
    tabs = st.tabs(["Executive overview", "Customer paths", "Sales CRM outcomes", "Agents", "Marketing", "Insights & data"])

    with tabs[0]:
        row1 = st.columns(5)
        row1[0].metric("New leads", f"{len(new_leads):,}")
        row1[1].metric("Meta spend", f"AED {total_spend:,.2f}")
        row1[2].metric("CPL", f"AED {cpl:,.2f}" if len(new_leads) else "N/A")
        row1[3].metric("Agent-created orders", f"{len(orders):,}", f"{order_rate:.1f}% of leads")
        row1[4].metric("Initial order value", f"AED {order_value:,.2f}")
        row2 = st.columns(5)
        row2[0].metric("Final sales closed", f"{len(closed):,}", f"{close_rate:.1f}% of orders")
        row2[1].metric("Final closed revenue", f"AED {closed_revenue:,.2f}")
        row2[2].metric("Cancelled / returned", f"{len(cancelled):,}", f"{cancel_rate:.1f}% of orders")
        row2[3].metric("Pending / in process", f"{len(pending):,}")
        row2[4].metric("Final Meta ROAS", f"{roas:.2f}×" if total_spend else "N/A")

        funnel = pd.DataFrame({
            "Stage": ["New leads", "Agent-created orders", "Final sales closed"],
            "Count": [len(new_leads), len(orders), len(closed)],
        })
        st.plotly_chart(px.funnel(funnel, x="Count", y="Stage", title="Business conversion funnel"), use_container_width=True)

    with tabs[1]:
        path = joined.groupby("customer_path", as_index=False).agg(
            records=("source_row", "size"), orders=("is_order", "sum"),
            initial_value=("order_value", "sum"),
            closed_sales=("crm_outcome", lambda values: values.eq("SALE CLOSED").sum()),
            cancelled=("crm_outcome", lambda values: values.eq("CANCELLED / RETURN").sum()),
        )
        path["order_conversion_pct"] = path["orders"].div(path["records"].replace(0, pd.NA)).mul(100)
        path["final_close_pct"] = path["closed_sales"].div(path["orders"].replace(0, pd.NA)).mul(100)
        st.dataframe(path.sort_values("records", ascending=False), hide_index=True, use_container_width=True)
        st.plotly_chart(px.bar(path, x="customer_path", y=["records", "orders", "closed_sales"], barmode="group", title="Customer-path performance"), use_container_width=True)
        st.caption("Missed Lead, Broadcast, Re-Order and Lead & Re-Order are highlighted separately. Only LEAD is used as the Meta CPL denominator.")

    with tabs[2]:
        outcomes = joined.groupby("crm_outcome", as_index=False).agg(
            orders=("source_row", "size"), amount=("crm_value", "sum")
        )
        outcomes["share_pct"] = outcomes["orders"].div(len(orders) if len(orders) else len(joined)).mul(100)
        st.dataframe(outcomes.sort_values("orders", ascending=False), hide_index=True, use_container_width=True)
        reasons = joined[joined["crm_outcome"].eq("CANCELLED / RETURN")].groupby("reason", as_index=False).size().sort_values("size", ascending=False)
        st.markdown("#### Cancellation and rejection reasons")
        st.dataframe(reasons, hide_index=True, use_container_width=True)

    with tabs[3]:
        agent = joined.groupby("agent", as_index=False).agg(
            handled=("source_row", "size"), new_leads=("customer_path", lambda values: values.eq("LEAD").sum()),
            orders=("is_order", "sum"), initial_value=("order_value", "sum"),
            closed_sales=("crm_outcome", lambda values: values.eq("SALE CLOSED").sum()),
            cancelled=("crm_outcome", lambda values: values.eq("CANCELLED / RETURN").sum()),
        )
        agent["lead_to_order_pct"] = agent["orders"].div(agent["new_leads"].replace(0, pd.NA)).mul(100)
        agent["order_to_close_pct"] = agent["closed_sales"].div(agent["orders"].replace(0, pd.NA)).mul(100)
        st.dataframe(agent.sort_values(["closed_sales", "orders"], ascending=False), hide_index=True, use_container_width=True)

    with tabs[4]:
        metrics = pd.DataFrame([
            {"Metric": "Meta spend", "Value": total_spend},
            {"Metric": "New leads", "Value": len(new_leads)},
            {"Metric": "CPL", "Value": cpl},
            {"Metric": "Agent-created orders", "Value": len(orders)},
            {"Metric": "Cost per agent order", "Value": total_spend / len(orders) if len(orders) else 0},
            {"Metric": "Final closed sales", "Value": len(closed)},
            {"Metric": "Cost per final sale", "Value": total_spend / len(closed) if len(closed) else 0},
            {"Metric": "Final closed revenue", "Value": closed_revenue},
            {"Metric": "Final ROAS", "Value": roas},
        ])
        st.dataframe(metrics, hide_index=True, use_container_width=True)
        if not meta_spend.empty:
            daily = meta_spend.groupby("date", as_index=False)["spend"].sum()
            st.plotly_chart(px.line(daily, x="date", y="spend", markers=True, title="Daily Meta spend"), use_container_width=True)

    with tabs[5]:
        leakage = order_value - closed_revenue
        unmatched = int(joined["crm_outcome"].eq("NOT FOUND IN CRM").sum())
        st.markdown("#### Automated business insights")
        st.write(f"- Revenue leakage from agent-created value to final closed revenue: **AED {leakage:,.2f}**.")
        st.write(f"- Final order-to-close conversion: **{close_rate:.1f}%**.")
        st.write(f"- Cancellation/return ratio after agent conversion: **{cancel_rate:.1f}%**.")
        st.write(f"- Records not matched to Sales CRM: **{unmatched:,}**.")
        st.markdown("#### Row-level reconciliation")
        st.dataframe(joined, hide_index=True, use_container_width=True)
