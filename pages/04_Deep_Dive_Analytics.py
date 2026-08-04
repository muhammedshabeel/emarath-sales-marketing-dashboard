from __future__ import annotations

import numpy as np
import pandas as pd
import streamlit as st

from historical_business import load_historical_dataset

st.set_page_config(page_title="Deep Dive Analytics", page_icon="🔎", layout="wide")

st.markdown(
    """
    <style>
    .block-container{padding-top:1rem;max-width:1600px}
    [data-testid="stMetric"]{background:#fff;border:1px solid #e5e9ef;padding:15px;border-radius:16px}
    .hero{padding:22px;border-radius:20px;background:linear-gradient(120deg,#102a43,#176b87);color:white;margin-bottom:16px}
    .hero h1{color:white;margin:0 0 8px}.hero p{margin:0;color:#d9edf4}
    .finding{border-left:5px solid #d7a928;padding:12px 15px;background:#fffaf0;border-radius:10px;margin:8px 0}
    </style>
    """,
    unsafe_allow_html=True,
)


def pct(a, b):
    return a / b * 100 if b else 0.0


def safe_div(a, b):
    return a / b if b else 0.0


def summary(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    grouped = frame.groupby(keys, as_index=False, observed=True).agg(
        records=("source_row", "size"),
        leads=("customer_path", lambda x: x.eq("LEAD").sum()),
        orders=("is_order", "sum"),
        order_value=("order_value", "sum"),
        closed=("is_closed", "sum"),
        cancelled=("is_cancelled", "sum"),
        pending=("is_pending", "sum"),
        unmatched=("crm_outcome", lambda x: x.eq("NOT FOUND IN CRM").sum()),
    )
    closed_revenue = (
        frame.loc[frame["is_closed"]]
        .groupby(keys, observed=True)["final_revenue"]
        .sum()
        .rename("closed_revenue")
        .reset_index()
    )
    grouped = grouped.merge(closed_revenue, on=keys, how="left")
    grouped["closed_revenue"] = grouped["closed_revenue"].fillna(0.0)
    grouped["lead_to_order_pct"] = np.where(grouped["leads"] > 0, grouped["orders"] / grouped["leads"] * 100, 0)
    grouped["order_to_close_pct"] = np.where(grouped["orders"] > 0, grouped["closed"] / grouped["orders"] * 100, 0)
    grouped["cancel_pct"] = np.where(grouped["orders"] > 0, grouped["cancelled"] / grouped["orders"] * 100, 0)
    grouped["unmatched_pct"] = np.where(grouped["records"] > 0, grouped["unmatched"] / grouped["records"] * 100, 0)
    grouped["revenue_leakage"] = grouped["order_value"] - grouped["closed_revenue"]
    grouped["avg_closed_value"] = np.where(grouped["closed"] > 0, grouped["closed_revenue"] / grouped["closed"], 0)
    return grouped


def zscore(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").fillna(0.0)
    std = values.std(ddof=0)
    if not std or np.isnan(std):
        return pd.Series(0.0, index=series.index)
    return (values - values.mean()) / std


st.markdown(
    '<div class="hero"><h1>Deep-Dive Analytics & Root-Cause Diagnostics</h1>'
    '<p>Apply filters once, then open only the analysis you need. Heavy sections are calculated on demand.</p></div>',
    unsafe_allow_html=True,
)

with st.spinner("Opening cached historical dataset…"):
    data = load_historical_dataset()

if data.empty:
    st.error("Historical dataset is empty.")
    st.stop()

data = data.copy(deep=False)
year_values = pd.to_numeric(data["month"].astype(str).str[:4], errors="coerce")
all_years = sorted(year_values.dropna().astype(int).unique().tolist())
all_months = sorted(data["month"].dropna().astype(str).unique().tolist())

if "deep_filters" not in st.session_state:
    st.session_state.deep_filters = {
        "years": all_years,
        "months": all_months,
        "agent": [],
        "product": [],
        "vendor": [],
        "country": [],
        "customer_path": [],
    }

with st.sidebar.form("deep_dive_filters"):
    st.header("Global Filters")
    chosen_years = st.multiselect("Year", all_years, default=st.session_state.deep_filters["years"])
    eligible_months = [m for m in all_months if int(str(m)[:4]) in chosen_years] if chosen_years else all_months
    previous_months = [m for m in st.session_state.deep_filters["months"] if m in eligible_months]
    chosen_months = st.multiselect("Month", eligible_months, default=previous_months or eligible_months)
    selections = {}
    for column, label in [
        ("agent", "Agent"), ("product", "Product"), ("vendor", "Vendor"),
        ("country", "Country"), ("customer_path", "Customer Path"),
    ]:
        options = sorted(data[column].dropna().astype(str).unique().tolist())
        defaults = [x for x in st.session_state.deep_filters[column] if x in options]
        selections[column] = st.multiselect(label, options, default=defaults)
    applied = st.form_submit_button("Apply filters", use_container_width=True)

if applied:
    st.session_state.deep_filters = {
        "years": chosen_years,
        "months": chosen_months,
        **selections,
    }

filters = st.session_state.deep_filters
mask = pd.Series(True, index=data.index)
if filters["years"]:
    mask &= year_values.isin(filters["years"])
if filters["months"]:
    mask &= data["month"].astype(str).isin(filters["months"])
for column in ["agent", "product", "vendor", "country", "customer_path"]:
    if filters[column]:
        mask &= data[column].astype(str).isin(filters[column])
working = data.loc[mask]

if working.empty:
    st.warning("No records match the applied filters.")
    st.stop()

closed_revenue_total = working.loc[working["is_closed"], "final_revenue"].sum()
k = st.columns(8)
k[0].metric("Records", f"{len(working):,}")
k[1].metric("Meta leads", f"{int((working['customer_path'] == 'LEAD').sum()):,}")
k[2].metric("Orders", f"{int(working['is_order'].sum()):,}")
k[3].metric("Closed", f"{int(working['is_closed'].sum()):,}")
k[4].metric("Cancelled", f"{int(working['is_cancelled'].sum()):,}")
k[5].metric("Closed revenue", f"AED {closed_revenue_total:,.2f}")
k[6].metric("Leakage", f"AED {(working['order_value'].sum() - closed_revenue_total):,.2f}")
k[7].metric("CRM unmatched", f"{int((working['crm_outcome'] == 'NOT FOUND IN CRM').sum()):,}")

view = st.selectbox(
    "Analysis view",
    [
        "Month Comparison", "Root Cause", "Anomalies", "Agent Drilldown",
        "Product Drilldown", "Vendor Drilldown", "Country Drilldown",
        "Customer Path", "CRM Outcomes", "Row-Level Data",
    ],
)

if view in {"Month Comparison", "Anomalies"}:
    month_summary = summary(working, ["month"]).sort_values("month")

if view == "Month Comparison":
    st.dataframe(month_summary, hide_index=True, use_container_width=True)
    st.line_chart(month_summary.set_index("month")[["lead_to_order_pct", "order_to_close_pct", "cancel_pct", "unmatched_pct"]])
    st.line_chart(month_summary.set_index("month")[["closed_revenue", "revenue_leakage"]])
elif view == "Root Cause":
    root = summary(working, ["month", "agent", "product", "vendor", "country"])
    root["failure_score"] = (
        root["cancel_pct"] * 0.35 + root["unmatched_pct"] * 0.20
        + zscore(root["revenue_leakage"]).clip(lower=0) * 20
        + (100 - root["order_to_close_pct"]) * 0.25
    )
    st.markdown("#### Highest-impact failure combinations")
    st.dataframe(root.nlargest(250, "failure_score"), hide_index=True, use_container_width=True)
    reasons = working.loc[working["is_cancelled"]].groupby(
        ["reason", "product", "country"], as_index=False, observed=True
    ).agg(cases=("source_row", "size"), lost_value=("order_value", "sum"))
    st.markdown("#### Exact cancellation causes")
    st.dataframe(reasons.nlargest(200, "lost_value"), hide_index=True, use_container_width=True)
elif view == "Anomalies":
    anomaly = month_summary.copy()
    for column in ["closed_revenue", "revenue_leakage", "cancel_pct", "order_to_close_pct", "unmatched_pct"]:
        anomaly[f"{column}_z"] = zscore(anomaly[column])
    anomaly["anomaly_score"] = sum(anomaly[f"{c}_z"].abs() for c in ["closed_revenue", "revenue_leakage", "cancel_pct", "order_to_close_pct", "unmatched_pct"])
    st.dataframe(anomaly.sort_values("anomaly_score", ascending=False), hide_index=True, use_container_width=True)
elif view.endswith("Drilldown"):
    dimension = view.split()[0].lower()
    result = summary(working, [dimension])
    st.dataframe(result.sort_values(["closed_revenue", "closed"], ascending=False), hide_index=True, use_container_width=True)
elif view == "Customer Path":
    st.dataframe(summary(working, ["customer_path"]).sort_values("records", ascending=False), hide_index=True, use_container_width=True)
elif view == "CRM Outcomes":
    outcomes = working.groupby("crm_outcome", as_index=False, observed=True).agg(
        records=("source_row", "size"), order_value=("order_value", "sum"), final_revenue=("final_revenue", "sum")
    )
    outcomes["share_pct"] = outcomes["records"] / len(working) * 100
    st.dataframe(outcomes.sort_values("records", ascending=False), hide_index=True, use_container_width=True)
else:
    visible_columns = [
        "month", "date", "agent", "customer_path", "phone", "phone_2", "product",
        "quantity", "order_value", "status_raw", "is_order", "country", "vendor",
        "crm_outcome", "crm_status_raw", "crm_value", "final_revenue", "reason",
        "tracking_number", "em_number",
    ]
    available = [column for column in visible_columns if column in working.columns]
    limit = st.number_input("Rows to display", min_value=100, max_value=5000, value=500, step=100)
    st.caption(f"Showing {min(int(limit), len(working)):,} of {len(working):,} filtered rows.")
    st.dataframe(working[available].head(int(limit)), hide_index=True, use_container_width=True)
