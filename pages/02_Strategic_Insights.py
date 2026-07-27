from __future__ import annotations

import io
from datetime import date

import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from matplotlib.backends.backend_pdf import PdfPages

from historical_business import load_historical_dataset
from meta_spend import META_AD_ACCOUNTS, fetch_meta_spend

st.set_page_config(page_title="Strategic Insights", page_icon="🧭", layout="wide")

st.markdown(
    """
    <style>
    .block-container{padding-top:1rem;max-width:1550px}
    [data-testid="stMetric"]{background:white;border:1px solid #e5e9ef;padding:15px;border-radius:16px}
    .strategy-hero{padding:24px;border-radius:20px;background:linear-gradient(120deg,#102a43,#176b87);color:white;margin-bottom:16px}
    .strategy-hero h1,.strategy-hero h2{color:white;margin:0 0 8px}.strategy-hero p{margin:0;color:#d9edf4}
    .insight-box{border-left:5px solid #d7a928;padding:14px 16px;background:#fffaf0;border-radius:10px;margin:8px 0}
    </style>
    """,
    unsafe_allow_html=True,
)


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
    return {f"Meta account {position}": account_id for position, account_id in enumerate(ids, 1)}


@st.cache_data(ttl=21600, show_spinner=False)
def cached_year_meta_spend(access_token: str, year: int, accounts_items):
    if not access_token:
        return pd.DataFrame(columns=["date", "campaign_name", "account", "spend"]), ["META_ACCESS_TOKEN is not configured"]
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    if year == 2026:
        end = date(2026, 4, 30)
    return fetch_meta_spend(access_token, start, end, accounts=dict(accounts_items))


def ratio(numerator, denominator):
    return numerator / denominator * 100 if denominator else 0.0


def monthly_summary(data: pd.DataFrame) -> pd.DataFrame:
    summary = data.groupby("month", as_index=False).agg(
        records=("source_row", "size"),
        meta_leads=("customer_path", lambda values: values.eq("LEAD").sum()),
        orders=("is_order", "sum"),
        order_value=("order_value", "sum"),
        closed_sales=("is_closed", "sum"),
        closed_revenue=("final_revenue", lambda values: values[data.loc[values.index, "is_closed"]].sum()),
        cancelled=("is_cancelled", "sum"),
        pending=("is_pending", "sum"),
        unmatched=("crm_outcome", lambda values: values.eq("NOT FOUND IN CRM").sum()),
    )
    summary["lead_to_order_pct"] = summary.apply(lambda row: ratio(row["orders"], row["meta_leads"]), axis=1)
    summary["order_to_close_pct"] = summary.apply(lambda row: ratio(row["closed_sales"], row["orders"]), axis=1)
    summary["cancel_pct"] = summary.apply(lambda row: ratio(row["cancelled"], row["orders"]), axis=1)
    summary["revenue_leakage"] = summary["order_value"] - summary["closed_revenue"]
    summary["year"] = summary["month"].str[:4].astype(int)
    summary["month_num"] = summary["month"].str[-2:].astype(int)
    summary["quarter"] = "Q" + (((summary["month_num"] - 1) // 3) + 1).astype(str)
    return summary.sort_values("month")


def attach_meta_spend(summary: pd.DataFrame, meta_spend: pd.DataFrame) -> pd.DataFrame:
    output = summary.copy()
    output["meta_spend"] = 0.0
    if not meta_spend.empty and {"date", "spend"}.issubset(meta_spend.columns):
        spend = meta_spend.copy()
        spend["date"] = pd.to_datetime(spend["date"], errors="coerce")
        spend["month"] = spend["date"].dt.to_period("M").astype(str)
        spend_month = spend.groupby("month", as_index=False)["spend"].sum().rename(columns={"spend": "meta_spend"})
        output = output.drop(columns=["meta_spend"]).merge(spend_month, on="month", how="left")
        output["meta_spend"] = output["meta_spend"].fillna(0.0)
    output["cpl"] = output.apply(lambda row: row["meta_spend"] / row["meta_leads"] if row["meta_leads"] else 0.0, axis=1)
    output["roas"] = output.apply(lambda row: row["closed_revenue"] / row["meta_spend"] if row["meta_spend"] else 0.0, axis=1)
    return output


def build_excel(data: pd.DataFrame, month: pd.DataFrame, agent: pd.DataFrame, product: pd.DataFrame, vendor: pd.DataFrame, country: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        month.to_excel(writer, sheet_name="Monthly Summary", index=False)
        agent.to_excel(writer, sheet_name="Agent Performance", index=False)
        product.to_excel(writer, sheet_name="Product Performance", index=False)
        vendor.to_excel(writer, sheet_name="Vendor Performance", index=False)
        country.to_excel(writer, sheet_name="Country Performance", index=False)
        data.to_excel(writer, sheet_name="Reconciliation Data", index=False)
    return buffer.getvalue()


def build_pdf(month: pd.DataFrame, title: str, insight_lines: list[str]) -> bytes:
    buffer = io.BytesIO()
    with PdfPages(buffer) as pdf:
        fig = plt.figure(figsize=(11.69, 8.27))
        fig.text(0.06, 0.94, title, fontsize=20, weight="bold")
        fig.text(0.06, 0.89, "Strategic historical performance report", fontsize=11)
        y = 0.82
        for line in insight_lines[:10]:
            fig.text(0.07, y, f"• {line}", fontsize=10, wrap=True)
            y -= 0.055
        plt.axis("off")
        pdf.savefig(fig, bbox_inches="tight")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        ax.plot(month["month"], month["closed_revenue"], marker="o", label="Closed revenue")
        ax.plot(month["month"], month["order_value"], marker="o", label="Initial order value")
        ax.set_title("Monthly Revenue Performance")
        ax.tick_params(axis="x", rotation=60)
        ax.legend()
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(11.69, 8.27))
        ax.plot(month["month"], month["lead_to_order_pct"], marker="o", label="Lead to order %")
        ax.plot(month["month"], month["order_to_close_pct"], marker="o", label="Order to close %")
        ax.plot(month["month"], month["cancel_pct"], marker="o", label="Cancellation %")
        ax.set_title("Monthly Conversion and Failure Indicators")
        ax.tick_params(axis="x", rotation=60)
        ax.legend()
        fig.tight_layout()
        pdf.savefig(fig)
        plt.close(fig)
    return buffer.getvalue()


st.markdown(
    '<div class="strategy-hero"><h1>Strategic Insights & Next-Year Improvement</h1>'
    '<p>Complete historical performance, exact success drivers, failure causes, opportunity areas and next-year actions.</p></div>',
    unsafe_allow_html=True,
)

try:
    with st.spinner("Opening the preprocessed historical dataset…"):
        dataset = load_historical_dataset()
except Exception as exc:
    st.error(f"Could not open historical dataset: {exc}")
    st.stop()

if dataset.empty:
    st.warning("Historical dataset is empty.")
    st.stop()

available_years = sorted(dataset["month"].str[:4].astype(int).unique().tolist())
selected_years = st.multiselect("Years included in analysis", available_years, default=available_years)
if not selected_years:
    st.warning("Select at least one year.")
    st.stop()

filtered = dataset[dataset["month"].str[:4].astype(int).isin(selected_years)].copy()
summary = monthly_summary(filtered)

meta_frames = []
meta_errors = []
for year in selected_years:
    try:
        frame, errors = cached_year_meta_spend(secret("META_ACCESS_TOKEN"), year, tuple(configured_meta_accounts().items()))
        if not frame.empty:
            meta_frames.append(frame)
        meta_errors.extend(errors)
    except Exception as exc:
        meta_errors.append(f"{year}: {exc}")
meta_spend = pd.concat(meta_frames, ignore_index=True) if meta_frames else pd.DataFrame()
summary = attach_meta_spend(summary, meta_spend)

agent = filtered.groupby("agent", as_index=False).agg(
    handled=("source_row", "size"),
    leads=("customer_path", lambda values: values.eq("LEAD").sum()),
    orders=("is_order", "sum"),
    order_value=("order_value", "sum"),
    closed_sales=("is_closed", "sum"),
    closed_revenue=("final_revenue", lambda values: values[filtered.loc[values.index, "is_closed"]].sum()),
    cancelled=("is_cancelled", "sum"),
)
agent["lead_to_order_pct"] = agent.apply(lambda row: ratio(row["orders"], row["leads"]), axis=1)
agent["order_to_close_pct"] = agent.apply(lambda row: ratio(row["closed_sales"], row["orders"]), axis=1)
agent["cancel_pct"] = agent.apply(lambda row: ratio(row["cancelled"], row["orders"]), axis=1)


def dimension_summary(column: str) -> pd.DataFrame:
    result = filtered.groupby(column, as_index=False).agg(
        records=("source_row", "size"),
        leads=("customer_path", lambda values: values.eq("LEAD").sum()),
        orders=("is_order", "sum"),
        order_value=("order_value", "sum"),
        closed_sales=("is_closed", "sum"),
        closed_revenue=("final_revenue", lambda values: values[filtered.loc[values.index, "is_closed"]].sum()),
        cancelled=("is_cancelled", "sum"),
    )
    result["lead_to_order_pct"] = result.apply(lambda row: ratio(row["orders"], row["leads"]), axis=1)
    result["order_to_close_pct"] = result.apply(lambda row: ratio(row["closed_sales"], row["orders"]), axis=1)
    result["cancel_pct"] = result.apply(lambda row: ratio(row["cancelled"], row["orders"]), axis=1)
    result["revenue_leakage"] = result["order_value"] - result["closed_revenue"]
    return result


product = dimension_summary("product")
vendor = dimension_summary("vendor")
country = dimension_summary("country")
customer_path = dimension_summary("customer_path")
crm_outcome = filtered.groupby("crm_outcome", as_index=False).agg(records=("source_row", "size"), amount=("final_revenue", "sum"))

best_revenue = summary.loc[summary["closed_revenue"].idxmax()]
best_close = summary.loc[summary["order_to_close_pct"].idxmax()]
worst_cancel = summary.loc[summary["cancel_pct"].idxmax()]
worst_leakage = summary.loc[summary["revenue_leakage"].idxmax()]
lowest_cpl = summary[summary["meta_spend"].gt(0)].sort_values("cpl").head(1)
best_roas = summary[summary["meta_spend"].gt(0)].sort_values("roas", ascending=False).head(1)

insight_lines = [
    f"Best revenue month: {best_revenue['month']} with AED {best_revenue['closed_revenue']:,.2f} closed revenue.",
    f"Best order-to-close month: {best_close['month']} at {best_close['order_to_close_pct']:.1f}%.",
    f"Highest cancellation pressure: {worst_cancel['month']} at {worst_cancel['cancel_pct']:.1f}% of agent-created orders.",
    f"Largest revenue leakage: {worst_leakage['month']} at AED {worst_leakage['revenue_leakage']:,.2f}.",
]
if not lowest_cpl.empty:
    row = lowest_cpl.iloc[0]
    insight_lines.append(f"Lowest CPL month: {row['month']} at AED {row['cpl']:.2f}.")
if not best_roas.empty:
    row = best_roas.iloc[0]
    insight_lines.append(f"Highest Meta ROAS month: {row['month']} at {row['roas']:.2f}×.")

kpis = st.columns(6)
kpis[0].metric("Historical leads", f"{int(summary['meta_leads'].sum()):,}")
kpis[1].metric("Agent orders", f"{int(summary['orders'].sum()):,}")
kpis[2].metric("Sales closed", f"{int(summary['closed_sales'].sum()):,}")
kpis[3].metric("Closed revenue", f"AED {summary['closed_revenue'].sum():,.2f}")
kpis[4].metric("Revenue leakage", f"AED {summary['revenue_leakage'].sum():,.2f}")
kpis[5].metric("Meta ROAS", f"{summary['closed_revenue'].sum() / summary['meta_spend'].sum():.2f}×" if summary['meta_spend'].sum() else "N/A")

for line in insight_lines:
    st.markdown(f'<div class="insight-box">{line}</div>', unsafe_allow_html=True)

export_col1, export_col2, export_col3 = st.columns([1, 1, 2])
excel_bytes = build_excel(filtered, summary, agent, product, vendor, country)
export_col1.download_button("Download Excel report", excel_bytes, "historical_business_intelligence.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
pdf_bytes = build_pdf(summary, "Emarath Historical Business Intelligence", insight_lines)
export_col2.download_button("Download PDF report", pdf_bytes, "historical_business_intelligence.pdf", "application/pdf", use_container_width=True)
export_col3.info("Month, quarter and year changes use the cached preprocessed dataset. Google Sheets are not parsed during navigation.")

tabs = st.tabs([
    "Executive Dashboard",
    "Monthly / Quarterly / Yearly",
    "Marketing & ROAS",
    "Sales & Leakage",
    "Agents",
    "Products",
    "Vendors",
    "Countries",
    "Customer Paths",
    "CRM Outcomes",
    "Next-Year Action Plan",
])

with tabs[0]:
    st.line_chart(summary.set_index("month")[["closed_revenue", "order_value"]])
    st.dataframe(summary.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)

with tabs[1]:
    view = st.radio("Comparison level", ["Monthly", "Quarterly", "Yearly"], horizontal=True)
    if view == "Monthly":
        compare = summary.copy()
        key = "month"
    elif view == "Quarterly":
        compare = summary.groupby(["year", "quarter"], as_index=False).agg(
            meta_leads=("meta_leads", "sum"), orders=("orders", "sum"), closed_sales=("closed_sales", "sum"),
            order_value=("order_value", "sum"), closed_revenue=("closed_revenue", "sum"), cancelled=("cancelled", "sum"),
            meta_spend=("meta_spend", "sum"), revenue_leakage=("revenue_leakage", "sum"),
        )
        compare["period"] = compare["year"].astype(str) + " " + compare["quarter"]
        key = "period"
    else:
        compare = summary.groupby("year", as_index=False).agg(
            meta_leads=("meta_leads", "sum"), orders=("orders", "sum"), closed_sales=("closed_sales", "sum"),
            order_value=("order_value", "sum"), closed_revenue=("closed_revenue", "sum"), cancelled=("cancelled", "sum"),
            meta_spend=("meta_spend", "sum"), revenue_leakage=("revenue_leakage", "sum"),
        )
        key = "year"
    compare["lead_to_order_pct"] = compare.apply(lambda row: ratio(row["orders"], row["meta_leads"]), axis=1)
    compare["order_to_close_pct"] = compare.apply(lambda row: ratio(row["closed_sales"], row["orders"]), axis=1)
    compare["cancel_pct"] = compare.apply(lambda row: ratio(row["cancelled"], row["orders"]), axis=1)
    compare["roas"] = compare.apply(lambda row: row["closed_revenue"] / row["meta_spend"] if row["meta_spend"] else 0, axis=1)
    st.dataframe(compare, hide_index=True, use_container_width=True)
    st.line_chart(compare.set_index(key)[["lead_to_order_pct", "order_to_close_pct", "cancel_pct"]])

with tabs[2]:
    st.dataframe(summary[["month", "meta_spend", "meta_leads", "cpl", "closed_revenue", "roas"]], hide_index=True, use_container_width=True)
    st.line_chart(summary.set_index("month")[["meta_spend", "closed_revenue"]])
    st.line_chart(summary.set_index("month")[["cpl", "roas"]])
    if meta_errors:
        st.warning("Some Meta accounts or periods could not be fetched: " + " | ".join(meta_errors))

with tabs[3]:
    st.dataframe(summary[["month", "orders", "order_value", "closed_sales", "closed_revenue", "cancelled", "revenue_leakage"]], hide_index=True, use_container_width=True)
    st.bar_chart(summary.set_index("month")[["closed_revenue", "revenue_leakage"]])

with tabs[4]:
    st.dataframe(agent.sort_values(["closed_sales", "orders"], ascending=False), hide_index=True, use_container_width=True)

with tabs[5]:
    st.dataframe(product.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)

with tabs[6]:
    st.dataframe(vendor.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)

with tabs[7]:
    st.dataframe(country.sort_values("closed_revenue", ascending=False), hide_index=True, use_container_width=True)

with tabs[8]:
    st.dataframe(customer_path.sort_values("records", ascending=False), hide_index=True, use_container_width=True)

with tabs[9]:
    st.dataframe(crm_outcome.sort_values("records", ascending=False), hide_index=True, use_container_width=True)
    cancellation_reasons = filtered[filtered["is_cancelled"]].groupby("reason", as_index=False).size().sort_values("size", ascending=False)
    st.markdown("#### Exact cancellation / return reasons")
    st.dataframe(cancellation_reasons, hide_index=True, use_container_width=True)

with tabs[10]:
    st.markdown("### Data-led priorities for the next year")
    st.write("1. Replicate the campaign, product, agent and market mix from the highest-revenue and highest-ROAS months.")
    st.write("2. Audit the highest-cancellation months by product, agent, country and reason before increasing ad spend.")
    st.write("3. Set monthly agent benchmarks from the top quartile for lead-to-order and order-to-close conversion.")
    st.write("4. Prioritize products and vendors with high closed revenue, strong conversion and low cancellation rates.")
    st.write("5. Reduce revenue leakage by tracking every agent-created order until a final CRM outcome is recorded.")
    st.write("6. Separate Meta Lead performance from Broadcast, Re-Order, Missed Lead and New Enquiry paths in all targets.")
    st.write("7. Use the lowest-CPL months as acquisition benchmarks, but scale only when final ROAS and CRM close rate are also strong.")

    st.markdown("### Exact diagnostic months")
    diagnostics = summary[[
        "month", "meta_leads", "orders", "lead_to_order_pct", "closed_sales", "order_to_close_pct",
        "cancelled", "cancel_pct", "closed_revenue", "revenue_leakage", "meta_spend", "cpl", "roas",
    ]].sort_values("month")
    st.dataframe(diagnostics, hide_index=True, use_container_width=True)
