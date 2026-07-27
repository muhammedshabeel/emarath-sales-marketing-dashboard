from __future__ import annotations

import pandas as pd
import numpy as np
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


def summary(data: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    result = data.groupby(keys, as_index=False).agg(
        records=("source_row", "size"),
        leads=("customer_path", lambda x: x.eq("LEAD").sum()),
        orders=("is_order", "sum"),
        order_value=("order_value", "sum"),
        closed=("is_closed", "sum"),
        closed_revenue=("final_revenue", lambda x: x[data.loc[x.index, "is_closed"]].sum()),
        cancelled=("is_cancelled", "sum"),
        pending=("is_pending", "sum"),
        unmatched=("crm_outcome", lambda x: x.eq("NOT FOUND IN CRM").sum()),
    )
    result["lead_to_order_pct"] = result.apply(lambda r: pct(r.orders, r.leads), axis=1)
    result["order_to_close_pct"] = result.apply(lambda r: pct(r.closed, r.orders), axis=1)
    result["cancel_pct"] = result.apply(lambda r: pct(r.cancelled, r.orders), axis=1)
    result["unmatched_pct"] = result.apply(lambda r: pct(r.unmatched, r.records), axis=1)
    result["revenue_leakage"] = result["order_value"] - result["closed_revenue"]
    result["avg_closed_value"] = result.apply(lambda r: safe_div(r.closed_revenue, r.closed), axis=1)
    return result


def zscore(series: pd.Series) -> pd.Series:
    values = pd.to_numeric(series, errors="coerce").fillna(0.0)
    std = values.std(ddof=0)
    if not std or np.isnan(std):
        return pd.Series(0.0, index=series.index)
    return (values - values.mean()) / std


st.markdown(
    '<div class="hero"><h1>Deep-Dive Analytics & Root-Cause Diagnostics</h1>'
    '<p>Filter from year to month to agent, product, vendor, country and customer path, then identify exact success and failure drivers.</p></div>',
    unsafe_allow_html=True,
)

with st.spinner("Opening cached historical dataset…"):
    data = load_historical_dataset()

if data.empty:
    st.error("Historical dataset is empty.")
    st.stop()

# Global synchronized filters
st.sidebar.header("Global Filters")
years = sorted(data["month"].str[:4].astype(int).unique())
selected_years = st.sidebar.multiselect("Year", years, default=years)
working = data[data["month"].str[:4].astype(int).isin(selected_years)].copy()

months = sorted(working["month"].dropna().unique())
selected_months = st.sidebar.multiselect("Month", months, default=months)
working = working[working["month"].isin(selected_months)]

for column, label in [
    ("agent", "Agent"),
    ("product", "Product"),
    ("vendor", "Vendor"),
    ("country", "Country"),
    ("customer_path", "Customer Path"),
]:
    options = sorted(working[column].dropna().astype(str).unique())
    chosen = st.sidebar.multiselect(label, options, default=[])
    if chosen:
        working = working[working[column].isin(chosen)]

if working.empty:
    st.warning("No records match the selected filters.")
    st.stop()

month_summary = summary(working, ["month"]).sort_values("month")

k = st.columns(8)
k[0].metric("Records", f"{len(working):,}")
k[1].metric("Meta leads", f"{int((working['customer_path'] == 'LEAD').sum()):,}")
k[2].metric("Orders", f"{int(working['is_order'].sum()):,}")
k[3].metric("Closed", f"{int(working['is_closed'].sum()):,}")
k[4].metric("Cancelled", f"{int(working['is_cancelled'].sum()):,}")
k[5].metric("Closed revenue", f"AED {working.loc[working['is_closed'], 'final_revenue'].sum():,.2f}")
k[6].metric("Leakage", f"AED {(working['order_value'].sum() - working.loc[working['is_closed'], 'final_revenue'].sum()):,.2f}")
k[7].metric("CRM unmatched", f"{int((working['crm_outcome'] == 'NOT FOUND IN CRM').sum()):,}")

# Month comparison and failure reasons
main_tabs = st.tabs([
    "Month Comparison",
    "Root Cause",
    "Anomalies",
    "Agent Drilldown",
    "Product Drilldown",
    "Vendor Drilldown",
    "Country Drilldown",
    "Customer Path",
    "CRM Outcomes",
    "Row-Level Data",
])

with main_tabs[0]:
    st.dataframe(month_summary, hide_index=True, use_container_width=True)
    st.line_chart(month_summary.set_index("month")[["lead_to_order_pct", "order_to_close_pct", "cancel_pct", "unmatched_pct"]])
    st.line_chart(month_summary.set_index("month")[["closed_revenue", "revenue_leakage"]])

    if len(month_summary) >= 2:
        compare_cols = st.columns(2)
        month_a = compare_cols[0].selectbox("Compare month A", month_summary["month"].tolist(), index=max(0, len(month_summary) - 2))
        month_b = compare_cols[1].selectbox("Compare month B", month_summary["month"].tolist(), index=len(month_summary) - 1)
        a = month_summary[month_summary["month"] == month_a].iloc[0]
        b = month_summary[month_summary["month"] == month_b].iloc[0]
        compare = pd.DataFrame({
            "Metric": ["Leads", "Orders", "Closed", "Closed Revenue", "Lead→Order %", "Order→Close %", "Cancel %", "Leakage"],
            month_a: [a.leads, a.orders, a.closed, a.closed_revenue, a.lead_to_order_pct, a.order_to_close_pct, a.cancel_pct, a.revenue_leakage],
            month_b: [b.leads, b.orders, b.closed, b.closed_revenue, b.lead_to_order_pct, b.order_to_close_pct, b.cancel_pct, b.revenue_leakage],
        })
        compare["Difference"] = compare[month_b] - compare[month_a]
        st.dataframe(compare, hide_index=True, use_container_width=True)

with main_tabs[1]:
    root = summary(working, ["month", "agent", "product", "vendor", "country"])
    root["failure_score"] = (
        root["cancel_pct"] * 0.35
        + root["unmatched_pct"] * 0.20
        + zscore(root["revenue_leakage"]).clip(lower=0) * 20
        + (100 - root["order_to_close_pct"]) * 0.25
    )
    st.markdown("#### Highest-impact failure combinations")
    st.dataframe(
        root.sort_values(["failure_score", "revenue_leakage"], ascending=False).head(250),
        hide_index=True,
        use_container_width=True,
    )

    reasons = working[working["is_cancelled"]].groupby(
        ["reason", "product", "country"], as_index=False
    ).agg(cases=("source_row", "size"), lost_value=("order_value", "sum"))
    st.markdown("#### Exact cancellation causes by product and country")
    st.dataframe(reasons.sort_values(["lost_value", "cases"], ascending=False).head(200), hide_index=True, use_container_width=True)

with main_tabs[2]:
    anomaly = month_summary.copy()
    for column in ["closed_revenue", "revenue_leakage", "cancel_pct", "order_to_close_pct", "unmatched_pct"]:
        anomaly[f"{column}_z"] = zscore(anomaly[column])
    anomaly["anomaly_score"] = (
        anomaly["closed_revenue_z"].abs()
        + anomaly["revenue_leakage_z"].abs()
        + anomaly["cancel_pct_z"].abs()
        + anomaly["order_to_close_pct_z"].abs()
        + anomaly["unmatched_pct_z"].abs()
    )
    st.dataframe(anomaly.sort_values("anomaly_score", ascending=False), hide_index=True, use_container_width=True)

    if not anomaly.empty:
        top = anomaly.sort_values("anomaly_score", ascending=False).iloc[0]
        findings = [
            f"Most unusual month: {top['month']}.",
            f"Closed revenue: AED {top['closed_revenue']:,.2f}.",
            f"Cancellation rate: {top['cancel_pct']:.1f}%.",
            f"Revenue leakage: AED {top['revenue_leakage']:,.2f}.",
            f"CRM unmatched rate: {top['unmatched_pct']:.1f}%.",
        ]
        for finding in findings:
            st.markdown(f'<div class="finding">{finding}</div>', unsafe_allow_html=True)

with main_tabs[3]:
    agent = summary(working, ["agent"])
    st.dataframe(agent.sort_values(["closed", "orders"], ascending=False), hide_index=True, use_container_width=True)

with main_tabs[4]:
    product = summary(working, ["product"])
    st.dataframe(product.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)

with main_tabs[5]:
    vendor = summary(working, ["vendor"])
    st.dataframe(vendor.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)

with main_tabs[6]:
    country = summary(working, ["country"])
    st.dataframe(country.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)

with main_tabs[7]:
    path = summary(working, ["customer_path"])
    st.dataframe(path.sort_values("records", ascending=False), hide_index=True, use_container_width=True)

with main_tabs[8]:
    outcomes = working.groupby("crm_outcome", as_index=False).agg(
        records=("source_row", "size"),
        order_value=("order_value", "sum"),
        final_revenue=("final_revenue", "sum"),
    )
    outcomes["share_pct"] = outcomes["records"] / len(working) * 100
    st.dataframe(outcomes.sort_values("records", ascending=False), hide_index=True, use_container_width=True)

with main_tabs[9]:
    visible_columns = [
        "month", "date", "agent", "customer_path", "phone", "phone_2", "product", "quantity",
        "order_value", "status_raw", "is_order", "country", "vendor", "crm_outcome",
        "crm_status_raw", "crm_value", "final_revenue", "reason", "tracking_number", "em_number",
    ]
    available = [column for column in visible_columns if column in working.columns]
    st.dataframe(working[available], hide_index=True, use_container_width=True)

st.markdown("### Automated improvement recommendations")

best_month = month_summary.loc[month_summary["closed_revenue"].idxmax()]
worst_cancel = month_summary.loc[month_summary["cancel_pct"].idxmax()]
worst_leak = month_summary.loc[month_summary["revenue_leakage"].idxmax()]
best_close = month_summary.loc[month_summary["order_to_close_pct"].idxmax()]

recommendations = [
    f"Replicate the agent, product, vendor and country mix from {best_month['month']}, the highest-revenue month.",
    f"Use {best_close['month']} as the operational close-rate benchmark and compare weaker months against it.",
    f"Prioritize a cancellation audit for {worst_cancel['month']}, where cancellation reached {worst_cancel['cancel_pct']:.1f}%.",
    f"Investigate every high-value order from {worst_leak['month']}, which produced AED {worst_leak['revenue_leakage']:,.2f} revenue leakage.",
    "Assign owners to the top cancellation reasons and review their case counts and lost value every month.",
    "Require a final CRM outcome for all agent-created orders before closing monthly reporting.",
    "Scale campaigns only when lead-to-order, order-to-close and final revenue improve together.",
]
for recommendation in recommendations:
    st.markdown(f'<div class="finding">{recommendation}</div>', unsafe_allow_html=True)
