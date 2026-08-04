from __future__ import annotations

import io
from datetime import date

import numpy as np
import pandas as pd
import streamlit as st

from historical_business import load_historical_dataset
from meta_spend import META_AD_ACCOUNTS, fetch_meta_spend

st.set_page_config(page_title="Business Command Center", page_icon="🏢", layout="wide")

st.markdown(
    """
    <style>
    .block-container{padding-top:1rem;max-width:1600px}
    [data-testid="stMetric"]{background:#fff;border:1px solid #e5e9ef;padding:15px;border-radius:16px}
    .hero{padding:24px;border-radius:20px;background:linear-gradient(120deg,#0f2740,#176b87);color:#fff;margin-bottom:16px}
    .hero h1,.hero h2{color:#fff;margin:0 0 8px}.hero p{margin:0;color:#d9edf4}
    .callout{border-left:5px solid #d7a928;padding:13px 15px;background:#fffaf0;border-radius:10px;margin:8px 0}
    </style>
    """,
    unsafe_allow_html=True,
)

VENDOR_MARGIN = {
    "Scent Passion": 0.40,
    "Oud Al Salam": 0.50,
    "La Parfume": 0.30,
    "Lpg": 0.30,
    "Rt Fragrance": 0.30,
    "Al Hajees": 0.40,
    "Athiyaf": 0.30,
    "Athyaf": 0.30,
}


def secret(name: str, default: str = "") -> str:
    try:
        return str(st.secrets.get(name, default))
    except Exception:
        return default


def configured_meta_accounts():
    raw = secret("META_AD_ACCOUNT_IDS")
    ids = [value.strip().removeprefix("act_") for value in raw.split(",") if value.strip()]
    if not ids:
        return META_AD_ACCOUNTS
    return {f"Meta account {i}": account_id for i, account_id in enumerate(ids, 1)}


@st.cache_data(ttl=21600, show_spinner=False)
def year_meta_spend(access_token: str, year: int, accounts_items):
    if not access_token:
        return pd.DataFrame(columns=["date", "campaign_name", "account", "spend"]), ["META_ACCESS_TOKEN missing"]
    end = date(year, 4, 30) if year == 2026 else date(year, 12, 31)
    return fetch_meta_spend(access_token, date(year, 1, 1), end, accounts=dict(accounts_items))


def pct(a, b):
    return a / b * 100 if b else 0.0


def monthly_summary(data: pd.DataFrame) -> pd.DataFrame:
    out = data.groupby("month", as_index=False).agg(
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
    out["lead_to_order_pct"] = out.apply(lambda r: pct(r.orders, r.leads), axis=1)
    out["order_to_close_pct"] = out.apply(lambda r: pct(r.closed, r.orders), axis=1)
    out["cancel_pct"] = out.apply(lambda r: pct(r.cancelled, r.orders), axis=1)
    out["revenue_leakage"] = out["order_value"] - out["closed_revenue"]
    out["year"] = out["month"].str[:4].astype(int)
    out["month_num"] = out["month"].str[-2:].astype(int)
    out["quarter"] = "Q" + (((out["month_num"] - 1) // 3) + 1).astype(str)
    return out.sort_values("month")


def attach_spend(summary: pd.DataFrame, spend: pd.DataFrame) -> pd.DataFrame:
    result = summary.copy()
    result["meta_spend"] = 0.0
    if not spend.empty and {"date", "spend"}.issubset(spend.columns):
        temp = spend.copy()
        temp["date"] = pd.to_datetime(temp["date"], errors="coerce")
        temp["month"] = temp["date"].dt.to_period("M").astype(str)
        spend_m = temp.groupby("month", as_index=False)["spend"].sum().rename(columns={"spend": "meta_spend"})
        result = result.drop(columns=["meta_spend"]).merge(spend_m, on="month", how="left")
        result["meta_spend"] = result["meta_spend"].fillna(0.0)
    result["cpl"] = result.apply(lambda r: r.meta_spend / r.leads if r.leads else 0.0, axis=1)
    result["roas"] = result.apply(lambda r: r.closed_revenue / r.meta_spend if r.meta_spend else 0.0, axis=1)
    return result


def dim_summary(data: pd.DataFrame, column: str) -> pd.DataFrame:
    out = data.groupby(column, as_index=False).agg(
        records=("source_row", "size"),
        leads=("customer_path", lambda x: x.eq("LEAD").sum()),
        orders=("is_order", "sum"),
        order_value=("order_value", "sum"),
        closed=("is_closed", "sum"),
        closed_revenue=("final_revenue", lambda x: x[data.loc[x.index, "is_closed"]].sum()),
        cancelled=("is_cancelled", "sum"),
    )
    out["lead_to_order_pct"] = out.apply(lambda r: pct(r.orders, r.leads), axis=1)
    out["order_to_close_pct"] = out.apply(lambda r: pct(r.closed, r.orders), axis=1)
    out["cancel_pct"] = out.apply(lambda r: pct(r.cancelled, r.orders), axis=1)
    out["revenue_leakage"] = out["order_value"] - out["closed_revenue"]
    return out


def forecast(values: pd.Series, periods: int = 3) -> pd.DataFrame:
    series = pd.to_numeric(values, errors="coerce").fillna(0.0).to_numpy(dtype=float)
    x = np.arange(len(series), dtype=float)
    if len(series) < 2:
        future = np.repeat(series[-1] if len(series) else 0.0, periods)
    else:
        slope, intercept = np.polyfit(x, series, 1)
        future = np.maximum(0, intercept + slope * np.arange(len(series), len(series) + periods))
    return pd.DataFrame({"period": [f"Next {i}" for i in range(1, periods + 1)], "forecast": future})


def excel_export(tables: dict[str, pd.DataFrame]) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in tables.items():
            frame.to_excel(writer, sheet_name=name[:31], index=False)
    return buffer.getvalue()


st.markdown(
    '<div class="hero"><h1>Emarath Business Intelligence Command Center</h1>'
    '<p>Executive, marketing, sales, operations, finance, forecasting and next-year planning from the preprocessed historical dataset.</p></div>',
    unsafe_allow_html=True,
)

if not st.session_state.get("command_dataset_open", False):
    st.info("The command center uses the large multi-year dataset and is kept unloaded to protect the app.")
    if st.button("Open business command center", type="primary", use_container_width=True):
        st.session_state["command_dataset_open"] = True
        st.rerun()
    st.stop()

try:
    with st.spinner("Opening cached historical intelligence dataset…"):
        data = load_historical_dataset()
except Exception as exc:
    st.session_state["command_dataset_open"] = False
    st.error(f"Historical data could not be opened safely: {exc}")
    st.info("Return to the main app for current operations, or retry after the app recovers.")
    st.stop()

if data.empty:
    st.error("Historical dataset is empty.")
    st.stop()

available_years = sorted(data["month"].str[:4].astype(int).unique())
selected_years = st.multiselect("Analysis years", available_years, default=list(available_years))
if not selected_years:
    st.stop()

filtered = data[data["month"].str[:4].astype(int).isin(selected_years)].copy()
summary = monthly_summary(filtered)

spend_parts, spend_errors = [], []
for year in selected_years:
    try:
        frame, errors = year_meta_spend(secret("META_ACCESS_TOKEN"), int(year), tuple(configured_meta_accounts().items()))
        if not frame.empty:
            spend_parts.append(frame)
        spend_errors.extend(errors)
    except Exception as exc:
        spend_errors.append(f"{year}: {exc}")
spend = pd.concat(spend_parts, ignore_index=True) if spend_parts else pd.DataFrame()
summary = attach_spend(summary, spend)

agent = dim_summary(filtered, "agent")
product = dim_summary(filtered, "product")
vendor = dim_summary(filtered, "vendor")
country = dim_summary(filtered, "country")
path = dim_summary(filtered, "customer_path")

vendor["margin_rate"] = vendor["vendor"].map(VENDOR_MARGIN).fillna(0.30)
vendor["estimated_gross_profit"] = vendor["closed_revenue"] * vendor["margin_rate"]

filtered["customer_key"] = filtered["phone"].where(filtered["phone"].ne(""), filtered["phone_2"])
customer_orders = filtered[filtered["is_order"]].groupby("customer_key", as_index=False).agg(
    orders=("source_row", "size"), revenue=("final_revenue", "sum"), first_month=("month", "min"), last_month=("month", "max")
)
customer_orders["customer_type"] = np.where(customer_orders["orders"] > 1, "Repeat Customer", "One-Time Customer")
cohort = customer_orders.groupby("first_month", as_index=False).agg(customers=("customer_key", "nunique"), revenue=("revenue", "sum"), repeat_customers=("customer_type", lambda x: x.eq("Repeat Customer").sum()))
cohort["repeat_rate_pct"] = cohort.apply(lambda r: pct(r.repeat_customers, r.customers), axis=1)

closed_revenue = summary["closed_revenue"].sum()
meta_spend_total = summary["meta_spend"].sum()
estimated_profit = vendor["estimated_gross_profit"].sum() - meta_spend_total

kpis = st.columns(7)
kpis[0].metric("Meta leads", f"{int(summary['leads'].sum()):,}")
kpis[1].metric("Orders", f"{int(summary['orders'].sum()):,}")
kpis[2].metric("Closed sales", f"{int(summary['closed'].sum()):,}")
kpis[3].metric("Closed revenue", f"AED {closed_revenue:,.2f}")
kpis[4].metric("Meta spend", f"AED {meta_spend_total:,.2f}")
kpis[5].metric("ROAS", f"{closed_revenue / meta_spend_total:.2f}×" if meta_spend_total else "N/A")
kpis[6].metric("Estimated profit", f"AED {estimated_profit:,.2f}")

best_month = summary.loc[summary["closed_revenue"].idxmax()]
worst_month = summary.loc[summary["revenue_leakage"].idxmax()]
best_conversion = summary.loc[summary["order_to_close_pct"].idxmax()]

for text in [
    f"Best month by closed revenue: {best_month['month']} — AED {best_month['closed_revenue']:,.2f}.",
    f"Best operational month: {best_conversion['month']} — {best_conversion['order_to_close_pct']:.1f}% order-to-close conversion.",
    f"Highest leakage month: {worst_month['month']} — AED {worst_month['revenue_leakage']:,.2f} gap between created and closed revenue.",
]:
    st.markdown(f'<div class="callout">{text}</div>', unsafe_allow_html=True)

controls = st.columns([1, 1, 2])
export = excel_export({
    "Monthly": summary,
    "Agents": agent,
    "Products": product,
    "Vendors": vendor,
    "Countries": country,
    "Customer Paths": path,
    "Cohorts": cohort,
    "Customers": customer_orders,
    "Reconciliation": filtered,
})
controls[0].download_button("Download complete Excel", export, "emarath_business_intelligence.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
controls[1].metric("Dataset rows", f"{len(filtered):,}")
controls[2].info("All pages use the cached preprocessed dataset. Year and month navigation does not reload Google Sheets.")

main_tabs = st.tabs([
    "CEO",
    "Marketing Director",
    "Sales Manager",
    "Operations",
    "Finance",
    "Agents",
    "Products & Vendors",
    "Markets",
    "Customers & Cohorts",
    "Forecasting",
    "Budget Planner",
    "Targets",
    "Failure Diagnostics",
    "Next-Year Plan",
])

with main_tabs[0]:
    st.line_chart(summary.set_index("month")[["closed_revenue", "order_value", "revenue_leakage"]])
    st.dataframe(summary.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)

with main_tabs[1]:
    st.dataframe(summary[["month", "meta_spend", "leads", "cpl", "closed_revenue", "roas"]], hide_index=True, use_container_width=True)
    st.line_chart(summary.set_index("month")[["meta_spend", "closed_revenue"]])
    st.line_chart(summary.set_index("month")[["cpl", "roas"]])
    if spend_errors:
        st.warning("Meta spend gaps: " + " | ".join(spend_errors))

with main_tabs[2]:
    st.dataframe(summary[["month", "orders", "closed", "order_to_close_pct", "cancelled", "cancel_pct", "closed_revenue"]], hide_index=True, use_container_width=True)
    st.bar_chart(summary.set_index("month")[["closed", "cancelled"]])

with main_tabs[3]:
    ops = summary[["month", "orders", "closed", "cancelled", "pending", "unmatched", "order_to_close_pct", "cancel_pct"]]
    st.dataframe(ops, hide_index=True, use_container_width=True)
    st.line_chart(ops.set_index("month")[["order_to_close_pct", "cancel_pct"]])

with main_tabs[4]:
    finance = summary[["month", "order_value", "closed_revenue", "revenue_leakage", "meta_spend", "roas"]]
    st.dataframe(finance, hide_index=True, use_container_width=True)
    st.dataframe(vendor[["vendor", "closed_revenue", "margin_rate", "estimated_gross_profit", "revenue_leakage"]].sort_values("estimated_gross_profit", ascending=False), hide_index=True, use_container_width=True)

with main_tabs[5]:
    st.dataframe(agent.sort_values(["closed", "orders"], ascending=False), hide_index=True, use_container_width=True)

with main_tabs[6]:
    p1, p2 = st.columns(2)
    p1.markdown("#### Product performance")
    p1.dataframe(product.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)
    p2.markdown("#### Vendor performance")
    p2.dataframe(vendor.sort_values("estimated_gross_profit", ascending=False), hide_index=True, use_container_width=True)

with main_tabs[7]:
    st.dataframe(country.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)
    st.dataframe(path.sort_values("records", ascending=False), hide_index=True, use_container_width=True)

with main_tabs[8]:
    repeat = customer_orders.groupby("customer_type", as_index=False).agg(customers=("customer_key", "nunique"), orders=("orders", "sum"), revenue=("revenue", "sum"))
    st.dataframe(repeat, hide_index=True, use_container_width=True)
    st.dataframe(cohort.sort_values("first_month"), hide_index=True, use_container_width=True)

with main_tabs[9]:
    revenue_fc = forecast(summary["closed_revenue"], 3)
    orders_fc = forecast(summary["orders"], 3)
    leads_fc = forecast(summary["leads"], 3)
    forecast_table = revenue_fc.rename(columns={"forecast": "revenue_forecast"})
    forecast_table["orders_forecast"] = orders_fc["forecast"]
    forecast_table["leads_forecast"] = leads_fc["forecast"]
    st.caption("Forecast is a simple historical trend projection and should be used as a planning estimate, not a guarantee.")
    st.dataframe(forecast_table, hide_index=True, use_container_width=True)

with main_tabs[10]:
    monthly_budget = st.number_input("Planned monthly Meta budget (AED)", min_value=0.0, value=float(meta_spend_total / max(len(summary), 1)), step=500.0)
    target_cpl = st.number_input("Target CPL (AED)", min_value=0.01, value=float(summary[summary['cpl'].gt(0)]['cpl'].median() if summary['cpl'].gt(0).any() else 2.0), step=0.10)
    target_close = st.slider("Expected order-to-close rate (%)", 1.0, 100.0, float(summary['order_to_close_pct'].median()))
    avg_order = closed_revenue / summary["closed"].sum() if summary["closed"].sum() else 0.0
    expected_leads = monthly_budget / target_cpl if target_cpl else 0
    lead_to_order = summary["lead_to_order_pct"].median() / 100
    expected_orders = expected_leads * lead_to_order
    expected_closed = expected_orders * target_close / 100
    expected_revenue = expected_closed * avg_order
    b = st.columns(4)
    b[0].metric("Expected leads", f"{expected_leads:,.0f}")
    b[1].metric("Expected orders", f"{expected_orders:,.0f}")
    b[2].metric("Expected closed sales", f"{expected_closed:,.0f}")
    b[3].metric("Expected revenue", f"AED {expected_revenue:,.2f}")

with main_tabs[11]:
    annual_target = st.number_input("Annual closed revenue target (AED)", min_value=0.0, value=float(closed_revenue * 1.20), step=10000.0)
    achieved = closed_revenue
    gap = max(annual_target - achieved, 0)
    months_in_scope = max(len(summary), 1)
    next_month_required = gap / 12
    t = st.columns(4)
    t[0].metric("Target", f"AED {annual_target:,.2f}")
    t[1].metric("Historical achieved", f"AED {achieved:,.2f}")
    t[2].metric("Gap", f"AED {gap:,.2f}")
    t[3].metric("Required monthly run-rate", f"AED {next_month_required:,.2f}")
    st.progress(min(achieved / annual_target, 1.0) if annual_target else 0.0)

with main_tabs[12]:
    failures = summary.sort_values(["cancel_pct", "revenue_leakage"], ascending=False)
    st.dataframe(failures[["month", "cancel_pct", "revenue_leakage", "unmatched", "order_to_close_pct", "cpl", "roas"]], hide_index=True, use_container_width=True)
    reasons = filtered[filtered["is_cancelled"]].groupby("reason", as_index=False).agg(cases=("source_row", "size"), lost_value=("order_value", "sum")).sort_values("lost_value", ascending=False)
    st.markdown("#### Exact cancellation and return causes")
    st.dataframe(reasons, hide_index=True, use_container_width=True)
    st.markdown("#### Root-cause drill-down")
    drill = filtered.groupby(["month", "agent", "product", "vendor", "country"], as_index=False).agg(
        orders=("is_order", "sum"), closed=("is_closed", "sum"), cancelled=("is_cancelled", "sum"), order_value=("order_value", "sum"), closed_revenue=("final_revenue", lambda x: x[filtered.loc[x.index, "is_closed"]].sum())
    )
    drill["cancel_pct"] = drill.apply(lambda r: pct(r.cancelled, r.orders), axis=1)
    drill["leakage"] = drill["order_value"] - drill["closed_revenue"]
    st.dataframe(drill.sort_values(["leakage", "cancel_pct"], ascending=False).head(200), hide_index=True, use_container_width=True)

with main_tabs[13]:
    st.markdown("### Exact priorities for the next year")
    priorities = [
        f"Replicate the operating mix of {best_month['month']}, the highest-revenue month.",
        f"Use {best_conversion['month']} as the operational conversion benchmark.",
        f"Run a corrective review for {worst_month['month']}, the highest-leakage month.",
        "Scale only products, countries and vendors that combine strong final ROAS, high close rate and low cancellation.",
        "Set agent targets from top-quartile lead-to-order and order-to-close performance, not simple order counts.",
        "Track every agent-created order until a final CRM outcome is recorded to reduce unmatched revenue leakage.",
        "Separate Meta Leads from Broadcast, Re-Order, Missed Lead and New Enquiry in every acquisition KPI.",
        "Review the top cancellation reasons monthly and assign a responsible owner for each corrective action.",
        "Use repeat-customer cohorts to design reorder campaigns and improve lifetime value.",
        "Approve marketing budget increases only when CPL, final close rate and ROAS all meet target together.",
    ]
    for item in priorities:
        st.markdown(f'<div class="callout">{item}</div>', unsafe_allow_html=True)
