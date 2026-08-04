from __future__ import annotations

import pandas as pd
import plotly.express as px
import streamlit as st

from historical_business import load_historical_business

st.set_page_config(page_title="Agent Deep Analysis", page_icon="👥", layout="wide")

st.markdown(
    """
    <style>
    .block-container{padding-top:1rem;max-width:1500px}
    [data-testid="stMetric"]{background:#fff;border:1px solid #e5eaf1;border-radius:16px;padding:15px}
    .hero{padding:22px 26px;border-radius:20px;background:linear-gradient(120deg,#102a43,#176b87);color:white;margin-bottom:16px}
    .hero h1{color:white;margin:0 0 8px}.hero p{margin:0;color:#d9edf4}
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<div class="hero"><h1>Agent Deep Analysis</h1><p>Agent ownership → order creation → Sales CRM closure, with auditable row-level drill-down.</p></div>', unsafe_allow_html=True)

period_values = (
    [f"2024-{month:02d}" for month in range(1, 13)]
    + [f"2025-{month:02d}" for month in range(1, 13)]
    + [f"2026-{month:02d}" for month in range(1, 5)]
)
labels = [pd.Period(value, freq="M").strftime("%B %Y") for value in period_values]
stored = str(st.session_state.get("agent_deep_period", "2025-12"))
index = period_values.index(stored) if stored in period_values else len(period_values) - 1
selected_label = st.selectbox("Business month", labels, index=index)
selected_period = period_values[labels.index(selected_label)]
st.session_state["agent_deep_period"] = selected_period

if st.button("Load agent analysis", type="primary", use_container_width=True):
    st.session_state["agent_deep_open"] = True

if not st.session_state.get("agent_deep_open", False):
    st.info("The multi-year dataset is opened only after you choose a month. This keeps initial navigation fast.")
    st.stop()

with st.spinner("Opening shared historical cache…"):
    dataset, _ = load_historical_business()

month = dataset.loc[dataset["month"].astype(str).eq(selected_period)].copy()
if month.empty:
    st.warning("No agent rows were found for this month.")
    st.stop()

month["agent"] = month["agent"].astype("string").fillna("UNASSIGNED").replace("", "UNASSIGNED")
month["order_value"] = pd.to_numeric(month["order_value"], errors="coerce").fillna(0.0)
month["final_revenue"] = pd.to_numeric(month["final_revenue"], errors="coerce").fillna(0.0)
month["closed_revenue_value"] = month["final_revenue"].where(month["is_closed"].fillna(False), 0.0)

summary = month.groupby("agent", as_index=False).agg(
    assigned_rows=("source_row", "size"),
    meta_leads=("customer_path", lambda values: values.eq("LEAD").sum()),
    orders_created=("is_order", "sum"),
    order_value=("order_value", "sum"),
    sales_closed=("is_closed", "sum"),
    closed_revenue=("closed_revenue_value", "sum"),
    cancelled=("is_cancelled", "sum"),
    pending=("is_pending", "sum"),
)
summary["lead_to_order_pct"] = summary["orders_created"].div(summary["meta_leads"].replace(0, pd.NA)).mul(100).fillna(0)
summary["order_to_close_pct"] = summary["sales_closed"].div(summary["orders_created"].replace(0, pd.NA)).mul(100).fillna(0)
summary["average_closed_value"] = summary["closed_revenue"].div(summary["sales_closed"].replace(0, pd.NA)).fillna(0)
summary = summary.sort_values(["sales_closed", "closed_revenue", "orders_created"], ascending=False).reset_index(drop=True)
summary["team_rank"] = summary.index + 1

agents = summary["agent"].tolist()
selected_agent = st.selectbox("Agent", agents, index=0)
agent_row = summary.loc[summary["agent"].eq(selected_agent)].iloc[0]
agent_rows = month.loc[month["agent"].eq(selected_agent)].copy()

st.caption(f'{selected_label} · rank {int(agent_row["team_rank"])} of {len(summary)} by final closed sales')
r1 = st.columns(5)
r1[0].metric("Assigned rows", f'{int(agent_row["assigned_rows"]):,}')
r1[1].metric("Meta leads", f'{int(agent_row["meta_leads"]):,}')
r1[2].metric("Orders created", f'{int(agent_row["orders_created"]):,}', f'{agent_row["lead_to_order_pct"]:.1f}% of leads')
r1[3].metric("Sales closed", f'{int(agent_row["sales_closed"]):,}', f'{agent_row["order_to_close_pct"]:.1f}% of orders')
r1[4].metric("Closed revenue", f'AED {agent_row["closed_revenue"]:,.2f}')

r2 = st.columns(5)
r2[0].metric("Initial order value", f'AED {agent_row["order_value"]:,.2f}')
r2[1].metric("Average closed value", f'AED {agent_row["average_closed_value"]:,.2f}')
r2[2].metric("Cancelled / returned", f'{int(agent_row["cancelled"]):,}')
r2[3].metric("Pending / in process", f'{int(agent_row["pending"]):,}')
r2[4].metric("CRM unmatched", f'{int(agent_rows["crm_outcome"].eq("NOT FOUND IN CRM").sum()):,}')

left, right = st.columns(2)
with left:
    comparison = summary.head(20).sort_values("sales_closed")
    st.plotly_chart(px.bar(comparison, x="sales_closed", y="agent", orientation="h", title="Final closed sales — team comparison", text="sales_closed"), use_container_width=True)
with right:
    stages = pd.DataFrame({"Stage": ["Meta leads", "Orders created", "Sales closed"], "Count": [int(agent_row["meta_leads"]), int(agent_row["orders_created"]), int(agent_row["sales_closed"])]})
    st.plotly_chart(px.funnel(stages, x="Count", y="Stage", title=f"{selected_agent} conversion funnel"), use_container_width=True)

tabs = st.tabs(["Country", "Products", "Customer paths", "Failure reasons", "Team table", "Audit rows"])
with tabs[0]:
    country = agent_rows.groupby("country", as_index=False).agg(rows=("source_row", "size"), orders=("is_order", "sum"), closed=("is_closed", "sum"), revenue=("closed_revenue_value", "sum"))
    st.dataframe(country.sort_values("closed", ascending=False), hide_index=True, use_container_width=True)
with tabs[1]:
    product = agent_rows.groupby("product", as_index=False).agg(rows=("source_row", "size"), orders=("is_order", "sum"), closed=("is_closed", "sum"), revenue=("closed_revenue_value", "sum"))
    st.dataframe(product.sort_values(["closed", "revenue"], ascending=False).head(100), hide_index=True, use_container_width=True)
with tabs[2]:
    paths = agent_rows.groupby("customer_path", as_index=False).agg(rows=("source_row", "size"), orders=("is_order", "sum"), closed=("is_closed", "sum"), revenue=("closed_revenue_value", "sum"))
    st.dataframe(paths.sort_values("rows", ascending=False), hide_index=True, use_container_width=True)
with tabs[3]:
    failed = agent_rows.loc[agent_rows["is_cancelled"].fillna(False)].groupby("reason", as_index=False).size().sort_values("size", ascending=False)
    st.dataframe(failed.head(100), hide_index=True, use_container_width=True)
with tabs[4]:
    st.dataframe(summary, hide_index=True, use_container_width=True)
with tabs[5]:
    st.caption("Repeated customers remain separate source rows; no phone-level deduplication is applied here.")
    limit = st.selectbox("Rows to display", [100, 250, 500, 1000], index=1)
    audit_columns = [column for column in ["date", "agent", "customer_path", "phone", "product", "country", "order_stage", "order_value", "crm_outcome", "final_revenue", "reason", "source_row", "source_row_crm"] if column in agent_rows.columns]
    st.dataframe(agent_rows[audit_columns].head(limit), hide_index=True, use_container_width=True)
