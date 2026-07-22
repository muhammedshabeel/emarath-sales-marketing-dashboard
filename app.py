from __future__ import annotations
import io
from datetime import date, time, timedelta
import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from urllib.parse import quote

from analytics import agent_directory_frame, attach_attribution, build_analysis, grouped, normalize_attribution, normalize_calls, normalize_google_crm_orders, normalize_leads, qa_report
from data_io import choose_best_sheet, detect_column, read_upload
from enrichment import classify_campaign, generate_fixed_zip_report
from historical import (
    HISTORICAL_SHEET_ID,
    VENDOR_PROFIT_PER_ORDER,
    classify_vendor,
    dimension_summary,
    monthly_summary,
    normalize_historical_rows,
    quality_summary,
    read_historical_workbook,
)
from meta_spend import fetch_meta_spend, monthly_spend_summary

st.set_page_config(page_title="Emarath Intelligence", page_icon="📊", layout="wide")

st.markdown("""
<style>
.stApp{background:linear-gradient(180deg,#f7f9fc 0,#fff 340px)}
.block-container{padding-top:1.25rem;max-width:1480px}h1,h2,h3{letter-spacing:-.035em;color:#132238}
[data-testid="stSidebar"]{background:#fff;border-right:1px solid #e6eaf0}
[data-testid="stMetric"]{background:#fff;border:1px solid #e6eaf0;padding:18px;border-radius:18px;box-shadow:0 7px 24px rgba(23,42,79,.055)}
[data-testid="stMetricLabel"]{color:#667085;font-weight:650}
[data-testid="stMetricValue"]{color:#132238;font-weight:750;font-size:clamp(1.55rem,2.15vw,2.65rem);white-space:nowrap;overflow:visible;text-overflow:clip;letter-spacing:-.04em}
[data-testid="stMetricValue"]>div{white-space:nowrap;overflow:visible;text-overflow:clip}
.hero{padding:26px 30px;border-radius:24px;background:linear-gradient(120deg,#102a43,#176b87);color:white;margin:.4rem 0 1.2rem;box-shadow:0 16px 40px rgba(16,42,67,.18)}
.hero h2{color:white;margin:0 0 7px}.hero p{margin:0;color:#d9edf4}
.section-label{font-size:.75rem;font-weight:800;letter-spacing:.12em;text-transform:uppercase;color:#d4a017;margin:1.4rem 0 .5rem}
.agent-card{background:#fff;border:1px solid #e6eaf0;border-radius:20px;padding:20px;box-shadow:0 8px 24px rgba(23,42,79,.05);min-height:164px}
.agent-name{font-size:1.1rem;font-weight:800;color:#132238}.agent-sub{color:#667085;font-size:.82rem;margin-bottom:14px}
.agent-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.agent-kpi{background:#f7f9fc;border-radius:12px;padding:10px}
.agent-kpi b{display:block;font-size:1.35rem;color:#132238}.agent-kpi span{font-size:.72rem;color:#667085}
.good{color:#16856b}.risk{color:#d04a42}
.stTabs [data-baseweb="tab-list"]{gap:8px;background:#eef2f6;padding:6px;border-radius:14px}.stTabs [data-baseweb="tab"]{border-radius:10px;padding:9px 17px}
</style>
""", unsafe_allow_html=True)

st.title("Sales & Marketing Intelligence")
st.caption("DoubleTick attribution × live Google CRM orders × 3CX call execution")

ANALYSIS_SCHEMA_VERSION = 26
if st.session_state.get("analysis_schema_version") != ANALYSIS_SCHEMA_VERSION:
    st.session_state.pop("analysis_results", None)
    st.session_state.pop("analysis_inputs", None)
    st.session_state["analysis_schema_version"] = ANALYSIS_SCHEMA_VERSION

HISTORICAL_SCHEMA_VERSION = 2
if st.session_state.get("historical_schema_version") != HISTORICAL_SCHEMA_VERSION:
    st.session_state.pop("historical_analysis", None)
    st.session_state["historical_schema_version"] = HISTORICAL_SCHEMA_VERSION

st_autorefresh(interval=300_000, limit=None, key="google_crm_order_refresh")


def secret(name, default=""):
    try: return str(st.secrets.get(name, default))
    except Exception: return default


@st.cache_data(ttl=21600, max_entries=3, show_spinner=False)
def cached_attribution_report(phone_tuple, api_key, meta_token, waba_tuple, start_date, end_date):
    """Cache the expensive DoubleTick → Meta report for identical inputs."""
    return generate_fixed_zip_report(
        phone_tuple, api_key, meta_token, list(waba_tuple), start_date, end_date,
        doubletick_workers=24, meta_workers=16,
    )


@st.cache_data(ttl=1800, max_entries=12, show_spinner=False)
def cached_meta_spend(access_token, start_date, end_date):
    return fetch_meta_spend(access_token, start_date, end_date)


SPEND_SHEET_ID = "1RSGCdB6UUFeFrX1mksMCBtElc9AijrKrlqR7tsP5fNg"
SPEND_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPEND_SHEET_ID}/edit"
SPEND_TABS = [
    "Campaign - Ahamed Sijil Cv", "Campaign - emirath", "Campaign - Bsparq",
    "Campaign - Emarath", "Campaign - Emarath-Qatar",
    "Campaign - Emarath Global - KSA",
]

ORDER_SHEET_ID = "1965jh8ovT2piechopznKTHkVRRhathkIxXeV0wczCfY"
ORDER_START_DATE = date(2026, 7, 4)
ORDER_END_DATE = date(2026, 7, 18)
SPEND_START_DATE = date(2026, 7, 4)
SPEND_END_DATE = date(2026, 7, 18)


def campaign_key(value):
    return pd.Series(value, dtype="string").fillna("").str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_google_campaign_spend(window_start_date, window_end_date):
    frames = []
    errors = []
    for tab in SPEND_TABS:
        csv_url = (
            f"https://docs.google.com/spreadsheets/d/{SPEND_SHEET_ID}/gviz/tq"
            f"?tqx=out:csv&sheet={quote(tab)}"
        )
        try:
            frame = pd.read_csv(csv_url)
            required = {"Window End Date (Dubai)", "Campaign Name", "Spend"}
            if not required.issubset(frame.columns):
                errors.append(f"{tab}: required spend columns missing")
                continue
            dates = pd.to_datetime(frame["Window End Date (Dubai)"], errors="coerce").dt.date
            in_window = dates.ge(window_start_date) & dates.le(window_end_date)
            frame = frame.loc[in_window, ["Campaign Name", "Campaign ID", "Spend"]].copy()
            frame["spend_date"] = dates.loc[in_window].values
            if frame.empty:
                continue
            frame["Spend"] = pd.to_numeric(frame["Spend"], errors="coerce").fillna(0.0)
            frame["Account"] = tab.removeprefix("Campaign - ")
            frames.append(frame)
        except Exception as exc:
            errors.append(f"{tab}: {str(exc)[:120]}")
    summary_url = (
        f"https://docs.google.com/spreadsheets/d/{SPEND_SHEET_ID}/gviz/tq"
        f"?tqx=out:csv&sheet={quote('Meta Report (5pm-5pm)')}"
    )
    authoritative_total = None
    daily_spend = pd.DataFrame(columns=["date", "spend"])
    try:
        summary = pd.read_csv(summary_url)
        summary_dates = pd.to_datetime(summary["Window End Date (Dubai)"], errors="coerce").dt.date
        summary_window = summary_dates.ge(window_start_date) & summary_dates.le(window_end_date)
        summary_values = pd.to_numeric(summary.loc[summary_window, "Spend"], errors="coerce").fillna(0)
        authoritative_total = float(summary_values.sum())
        daily_spend = pd.DataFrame({
            "date": summary_dates.loc[summary_window],
            "spend": summary_values,
        }).groupby("date", as_index=False)["spend"].sum()
    except Exception as exc:
        errors.append(f"Meta Report (5pm-5pm): {str(exc)[:120]}")
    if not frames:
        empty = pd.DataFrame(columns=["campaign_name_spend", "campaign_key", "spend", "spend_accounts"])
        empty_daily = pd.DataFrame(columns=["spend_date", "campaign_name_spend", "campaign_key", "spend", "spend_accounts"])
        return empty, errors, authoritative_total, daily_spend, empty_daily
    raw = pd.concat(frames, ignore_index=True)
    raw["campaign_key"] = campaign_key(raw["Campaign Name"])
    daily_campaign_spend = raw.rename(columns={
        "Campaign Name": "campaign_name_spend", "Account": "spend_accounts"
    })[["spend_date", "campaign_name_spend", "campaign_key", "Spend", "spend_accounts"]]
    daily_campaign_spend = daily_campaign_spend.rename(columns={"Spend": "spend"})
    grouped_spend = raw.groupby(["campaign_key", "Campaign Name"], as_index=False).agg(
        spend=("Spend", "sum"),
        spend_accounts=("Account", lambda values: ", ".join(sorted(set(values)))),
    )
    grouped_spend = grouped_spend.rename(columns={"Campaign Name": "campaign_name_spend"})
    # Account-summary rows are authoritative. Campaign rows can differ by one
    # fils because the displayed campaign spends are individually rounded.
    if authoritative_total is not None and not grouped_spend.empty:
        difference = round(authoritative_total - float(grouped_spend["spend"].sum()), 2)
        if abs(difference) <= 0.05 and difference:
            largest = grouped_spend["spend"].idxmax()
            grouped_spend.loc[largest, "spend"] += difference
    return grouped_spend, errors, authoritative_total, daily_spend, daily_campaign_spend


@st.cache_data(ttl=300, show_spinner=False)
def load_google_crm_orders():
    selected_columns = "A,B,C,D,E,G,H,K,M,O,U,V,X,Y"
    query = (
        f"select {selected_columns} "
        f"where C >= date '{ORDER_START_DATE.isoformat()}' "
        f"and C <= date '{ORDER_END_DATE.isoformat()}'"
    )
    csv_url = (
        f"https://docs.google.com/spreadsheets/d/{ORDER_SHEET_ID}/gviz/tq"
        f"?tqx=out:csv&sheet=CRM&tq={quote(query)}&headers=1"
    )
    frame = pd.read_csv(csv_url)
    required = {
        "COUNTRY", "AGENT", "DATE", "TRACKING NUM", "EM NUMBER",
        "NUMBER1", "NUMBER2", "PRODUCT 1", "PRODUCT 2", "VALUE",
        "VENDOR", "CS STATUS", "REASON", "Customer Path",
    }
    missing = required - set(frame.columns)
    if missing:
        raise ValueError("Google CRM columns missing: " + ", ".join(sorted(missing)))
    return frame


def campaign_performance(joined, spend_data):
    # Marketing performance is limited to leads attributed by the generated
    # ZIP/Meta report. Blank/unattributed DoubleTick rows are not marketing leads.
    attributed = joined[joined["campaign_name"].fillna("").astype(str).str.strip().ne("")].copy()
    performance = grouped(attributed, "campaign_name")
    performance["campaign_key"] = campaign_key(performance["campaign_name"])
    performance = performance.merge(spend_data, on="campaign_key", how="outer")
    performance["campaign_name"] = performance["campaign_name"].fillna(performance["campaign_name_spend"])
    for column in ["leads", "orders", "revenue", "called_leads", "answered_leads"]:
        if column not in performance:
            performance[column] = 0
        performance[column] = pd.to_numeric(performance[column], errors="coerce").fillna(0)
    performance["spend"] = pd.to_numeric(performance.get("spend", 0), errors="coerce").fillna(0.0)
    performance["cpl"] = performance["spend"].div(performance["leads"].replace(0, pd.NA))
    performance["conversion_rate"] = performance["orders"].div(performance["leads"].replace(0, pd.NA)).mul(100)
    performance["roas"] = performance["revenue"].div(performance["spend"].replace(0, pd.NA))
    performance["campaign_name"] = performance["campaign_name"].fillna("Unmatched spend campaign").astype(str)
    performance["conversion_rate"] = pd.to_numeric(performance["conversion_rate"], errors="coerce").fillna(0.0).astype(float)
    performance["orders"] = pd.to_numeric(performance["orders"], errors="coerce").fillna(0).astype(int)
    performance["spend"] = pd.to_numeric(performance["spend"], errors="coerce").fillna(0.0).astype(float)
    return performance.sort_values("spend", ascending=False)


def country_performance(joined, spend_data):
    """Return complete UAE/KSA/Qatar/Bahrain spend and CPL performance."""
    countries = pd.DataFrame({"country": ["UAE", "KSA", "QATAR", "BAHRAIN"]})
    lead_summary = grouped(joined, "country")
    if lead_summary.empty:
        lead_summary = pd.DataFrame(columns=["country", "leads", "orders", "revenue", "conversion_rate"])
    lead_summary["country"] = lead_summary["country"].astype("string").str.upper()

    spend_rows = spend_data.copy()
    spend_rows["country"] = spend_rows["campaign_name_spend"].map(
        lambda value: str(classify_campaign(value)[0]).upper()
    )
    country_spend = spend_rows.groupby("country", as_index=False)["spend"].sum()
    result = countries.merge(
        lead_summary[["country", "leads", "orders", "revenue", "conversion_rate"]],
        on="country", how="left",
    ).merge(country_spend, on="country", how="left")
    for column in ["leads", "orders", "revenue", "conversion_rate", "spend"]:
        result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0)
    result["leads"] = result["leads"].astype(int)
    result["orders"] = result["orders"].astype(int)
    result["cpl"] = result["spend"].div(result["leads"].replace(0, pd.NA))
    return result


def aggregate_campaign_spend(daily_campaign_spend, authoritative_total=None):
    if daily_campaign_spend.empty:
        return pd.DataFrame(columns=["campaign_name_spend", "campaign_key", "spend", "spend_accounts"])
    grouped_spend = daily_campaign_spend.groupby(
        ["campaign_key", "campaign_name_spend"], as_index=False
    ).agg(
        spend=("spend", "sum"),
        spend_accounts=("spend_accounts", lambda values: ", ".join(sorted(set(values)))),
    )
    if authoritative_total is not None and not grouped_spend.empty:
        difference = round(float(authoritative_total) - float(grouped_spend["spend"].sum()), 2)
        if abs(difference) <= 0.05 and difference:
            grouped_spend.loc[grouped_spend["spend"].idxmax(), "spend"] += difference
    return grouped_spend


def daily_dimension_performance(joined, daily_campaign_spend, dimension):
    attributed = joined[joined["campaign_name"].fillna("").astype(str).str.strip().ne("")].copy()
    attributed["date"] = pd.to_datetime(attributed["lead_time"], errors="coerce").dt.date
    attributed[dimension] = attributed[dimension].astype("string")
    daily_leads = attributed.groupby(["date", dimension], as_index=False).agg(
        leads=("lead_row", "size"), orders=("converted", "sum"), revenue=("order_value", "sum")
    )

    spend_rows = daily_campaign_spend.copy()
    classification_index = {"country": 0, "vendor": 2}[dimension]
    spend_rows[dimension] = spend_rows["campaign_name_spend"].map(
        lambda value: str(classify_campaign(value)[classification_index])
    )
    spend_rows["date"] = spend_rows["spend_date"]
    daily_dimension_spend = spend_rows.groupby(["date", dimension], as_index=False)["spend"].sum()
    result = daily_leads.merge(daily_dimension_spend, on=["date", dimension], how="outer")
    for column in ["spend", "leads", "orders", "revenue"]:
        result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0)
    result["leads"] = result["leads"].astype(int)
    result["orders"] = result["orders"].astype(int)
    result["cpl"] = result["spend"].div(result["leads"].replace(0, pd.NA))
    result["conversion_rate"] = result["orders"].div(result["leads"].replace(0, pd.NA)).mul(100)
    return result.sort_values(["date", dimension])


def mapping_ui(df, source):
    roles = {
        "DoubleTick": ["phone", "agent_number", "datetime"],
        "Attribution": ["phone", "ad_id", "campaign", "status"],
        "Workpex": ["phone", "datetime", "agent", "order_id", "status", "product", "amount"],
        "3CX": ["phone", "datetime", "agent", "call_status", "duration", "direction"],
    }[source]
    exact_defaults = {
        "DoubleTick": {"phone": "Phone number", "agent_number": "Agent Phone Number", "datetime": "Last message received at"},
        "Attribution": {"phone": "phone", "ad_id": "ad_id", "campaign": "meta_campaign_name", "status": "meta_lookup_status"},
        "Workpex": {"phone": "Primary Phone", "datetime": "Created Date", "agent": "Assigned", "status": "Lead Status", "product": "Product", "amount": "Actual Amount"},
        "3CX": {"phone": "To", "datetime": "Call Time", "agent": "From", "call_status": "Status", "duration": "Talking", "direction": "Direction"},
    }
    required = {"phone"}
    options = ["— Not available —"] + list(df.columns)
    mapping = {}
    cols = st.columns(3)
    for i, role in enumerate(roles):
        preferred = exact_defaults.get(source, {}).get(role)
        if source == "DoubleTick" and role == "datetime":
            # Exports may label the same event differently. Prefer the user's
            # Last message received field, then DoubleTick's CTWA equivalent.
            candidates = ["Last message received at", "Last message received", "Last CTWA lead at"]
            preferred = next((column for column in candidates if column in df.columns), preferred)
        detected = preferred if preferred in df.columns else detect_column(df.columns, role)
        index = options.index(detected) if detected in options else 0
        label = role.replace("_", " ").title() + (" *" if role in required else "")
        selected = cols[i % 3].selectbox(label, options, index=index, key=f"map_{source}_{role}")
        mapping[role] = None if selected.startswith("—") else selected
    return mapping


def source_range(df, col):
    if not col or col not in df or df[col].isna().all(): return (None, None)
    return (df[col].min(), df[col].max())


def excel_bytes(tables):
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, df in tables.items():
            safe = name[:31]
            export = df.copy()
            for col in export.select_dtypes(include=["datetimetz"]).columns:
                export[col] = export[col].astype(str)
            export.to_excel(writer, sheet_name=safe, index=False)
            ws = writer.sheets[safe]; ws.freeze_panes = "A2"; ws.auto_filter.ref = ws.dimensions
    return buffer.getvalue()


@st.cache_data(ttl=300, show_spinner=False)
def load_historical_source(sheet_url):
    raw = read_historical_workbook(sheet_url)
    return raw, normalize_historical_rows(raw)


def render_historical_dashboard(raw, historical):
    # Upgrade DataFrames retained in an existing Streamlit browser session
    # when profit columns are introduced or mapping rules change.
    required_profit_columns = {"vendor", "profit_per_order", "estimated_profit"}
    if not required_profit_columns.issubset(historical.columns):
        historical = historical.copy()
        historical["vendor"] = historical["product"].map(classify_vendor)
        historical["profit_per_order"] = historical["vendor"].map(VENDOR_PROFIT_PER_ORDER).fillna(0.0)
        historical["estimated_profit"] = historical["profit_per_order"].where(historical["is_won"], 0.0)
    available_months = sorted(historical["month"].dropna().unique(), reverse=True)
    selected_month = st.sidebar.selectbox(
        "Business month", available_months,
        format_func=lambda value: pd.Period(value, freq="M").strftime("%B %Y"),
        key="historical_business_month",
    )
    period = pd.Period(selected_month, freq="M")
    start = period.start_time.date()
    end = period.end_time.date()
    filtered = historical[historical["month"].eq(selected_month)].copy()
    if filtered.empty:
        st.warning("No historical rows exist in the selected period.")
        st.stop()

    won = filtered[filtered["is_won"]]
    first_orders = won[won["order_type"].eq("First-time order")]
    repeat_orders = won[won["order_type"].eq("Repeat order")]
    conversion = len(won) / len(filtered) * 100
    revenue = float(won["order_value"].sum())
    average_order = revenue / len(won) if len(won) else 0
    meta_token = secret("META_ACCESS_TOKEN")
    with st.spinner("Fetching selected-period spend from Meta…"):
        meta_spend, meta_errors = cached_meta_spend(meta_token, start, end)
    total_meta_spend = float(meta_spend["spend"].sum()) if not meta_spend.empty else 0.0
    cpl = total_meta_spend / len(filtered) if len(filtered) else 0.0
    cost_per_win = total_meta_spend / len(won) if len(won) else 0.0
    roas = revenue / total_meta_spend if total_meta_spend else 0.0
    estimated_profit = float(won["estimated_profit"].sum())
    profit_margin_rate = estimated_profit / revenue * 100 if revenue else 0.0
    profit_after_meta = estimated_profit - total_meta_spend
    previous_period = period - 1
    previous = historical[historical["month"].eq(str(previous_period))]
    previous_won = previous[previous["is_won"]]
    previous_revenue = float(previous_won["order_value"].sum())
    previous_profit = float(previous_won["estimated_profit"].sum())

    def change(current, prior):
        if not prior:
            return None
        return f"{(current - prior) / prior * 100:+.1f}% vs {previous_period.strftime('%b')}"
    st.markdown(
        f'<div class="hero"><h2>Historical business command centre</h2>'
        f'<p>{period.strftime("%B %Y")} · {len(filtered):,} lead/order rows · '
        f'repeated customer orders preserved</p></div>',
        unsafe_allow_html=True,
    )
    tabs = st.tabs(["Executive", "Monthly trends", "Meta spend", "Profit analysis", "Agents", "Markets & products", "Data quality"])

    with tabs[0]:
        st.markdown('<div class="section-label">Business outcomes</div>', unsafe_allow_html=True)
        row1 = st.columns(4)
        row1[0].metric("Lead rows", f"{len(filtered):,}", change(len(filtered), len(previous)))
        row1[1].metric("Won orders", f"{len(won):,}", change(len(won), len(previous_won)))
        row1[2].metric("Won revenue", f"AED {revenue:,.2f}", change(revenue, previous_revenue))
        row1[3].metric("Average order value", f"AED {average_order:,.2f}")
        row2 = st.columns(4)
        row2[0].metric("First-time orders", f"{len(first_orders):,}")
        row2[1].metric("Repeat orders", f"{len(repeat_orders):,}", f"{len(repeat_orders) / len(won) * 100:.1f}% of wins" if len(won) else None)
        row2[2].metric("Repeat-order revenue", f"AED {repeat_orders['order_value'].sum():,.2f}")
        row2[3].metric("Active agents", f"{filtered.loc[filtered.agent.ne('UNASSIGNED'), 'agent'].nunique():,}")
        st.markdown('<div class="section-label">Estimated order profit</div>', unsafe_allow_html=True)
        profit_kpis = st.columns(4)
        profit_kpis[0].metric("Vendor-based profit", f"AED {estimated_profit:,.2f}", change(estimated_profit, previous_profit))
        profit_kpis[1].metric("Profit margin", f"{profit_margin_rate:.1f}%", "Profit ÷ won revenue")
        profit_kpis[2].metric("Profit after Meta spend", f"AED {profit_after_meta:,.2f}")
        profit_kpis[3].metric("Profit per won order", f"AED {estimated_profit / len(won):,.2f}" if len(won) else "N/A")
        st.markdown('<div class="section-label">Meta advertising</div>', unsafe_allow_html=True)
        spend_kpis = st.columns(4)
        spend_kpis[0].metric("Meta spend", f"AED {total_meta_spend:,.2f}")
        spend_kpis[1].metric("Cost per lead", f"AED {cpl:,.2f}" if total_meta_spend else "N/A")
        spend_kpis[2].metric("Cost per won order", f"AED {cost_per_win:,.2f}" if total_meta_spend else "N/A")
        spend_kpis[3].metric("Revenue / ad spend", f"{roas:.2f}× ROAS" if total_meta_spend else "N/A")
        if meta_errors:
            st.warning("Meta spend could not be read from every account: " + " | ".join(meta_errors))
        overview = monthly_summary(historical)
        left, right = st.columns([1.55, 1])
        with left:
            trend = overview.melt(
                id_vars="month", value_vars=["leads", "won_orders"],
                var_name="metric", value_name="count",
            )
            fig = px.line(trend, x="month", y="count", color="metric", markers=True,
                          title="Monthly lead and order movement",
                          color_discrete_map={"leads": "#176b87", "won_orders": "#d4a017"})
            fig.update_layout(height=390, xaxis_title="", yaxis_title="Rows", legend_title="")
            st.plotly_chart(fig, use_container_width=True)
        with right:
            mix = pd.DataFrame({
                "type": ["First-time orders", "Repeat orders"],
                "orders": [len(first_orders), len(repeat_orders)],
            })
            fig = px.pie(mix, names="type", values="orders", hole=.65, title="Order mix",
                         color="type", color_discrete_map={"First-time orders": "#16856b", "Repeat orders": "#d4a017"})
            fig.update_layout(height=390, legend_orientation="h")
            st.plotly_chart(fig, use_container_width=True)

    with tabs[1]:
        monthly = monthly_summary(filtered)
        monthly = monthly.merge(monthly_spend_summary(meta_spend), on="month", how="left")
        monthly["spend"] = monthly["spend"].fillna(0.0)
        monthly["cost_per_lead"] = monthly["spend"].div(monthly["leads"].replace(0, pd.NA))
        monthly["cost_per_won_order"] = monthly["spend"].div(monthly["won_orders"].replace(0, pd.NA))
        monthly["roas"] = monthly["revenue"].div(monthly["spend"].replace(0, pd.NA))
        monthly["profit_margin"] = monthly["estimated_profit"].div(monthly["revenue"].replace(0, pd.NA)).mul(100)
        monthly["profit_after_meta"] = monthly["estimated_profit"] - monthly["spend"]
        st.markdown("#### Monthly operating scorecard")
        st.dataframe(
            monthly, hide_index=True, use_container_width=True,
            column_config={
                "month": "Month", "leads": st.column_config.NumberColumn("Lead rows", format="%d"),
                "won_orders": st.column_config.NumberColumn("Won orders", format="%d"),
                "conversion_rate": st.column_config.ProgressColumn("Conversion", min_value=0, max_value=100, format="%.1f%%"),
                "revenue": st.column_config.NumberColumn("Won revenue (AED)", format="%.2f"),
                "first_time_orders": "First-time orders", "repeat_orders": "Repeat orders",
                "repeat_revenue": st.column_config.NumberColumn("Repeat revenue (AED)", format="%.2f"),
                "active_agents": "Active agents",
                "estimated_profit": st.column_config.NumberColumn("Vendor profit (AED)", format="%.2f"),
                "profit_margin": st.column_config.NumberColumn("Profit margin", format="%.1f%%"),
                "profit_after_meta": st.column_config.NumberColumn("Profit after Meta (AED)", format="%.2f"),
                "spend": st.column_config.NumberColumn("Meta spend (AED)", format="%.2f"),
                "cost_per_lead": st.column_config.NumberColumn("CPL (AED)", format="%.2f"),
                "cost_per_won_order": st.column_config.NumberColumn("Cost / won order", format="%.2f"),
                "roas": st.column_config.NumberColumn("ROAS", format="%.2fx"),
            },
        )
        revenue_chart = monthly.melt(id_vars="month", value_vars=["revenue", "repeat_revenue"],
                                     var_name="metric", value_name="amount")
        fig = px.bar(revenue_chart, x="month", y="amount", color="metric", barmode="group",
                     title="Monthly won revenue and repeat-order contribution",
                     color_discrete_map={"revenue": "#176b87", "repeat_revenue": "#d4a017"})
        fig.update_layout(xaxis_title="", yaxis_title="AED", legend_title="")
        st.plotly_chart(fig, use_container_width=True)

    with tabs[2]:
        st.markdown("#### Meta spend by account and campaign")
        st.caption("Spend is fetched directly from Meta Insights for the selected historical date range. Lead and order values remain sourced only from the historical workbook.")
        if meta_spend.empty:
            st.warning("No Meta spend was returned. Confirm META_ACCESS_TOKEN in Streamlit Secrets and that it can access all six ad accounts.")
        else:
            account_spend = meta_spend.groupby("account", as_index=False)["spend"].sum().sort_values("spend", ascending=False)
            campaign_spend = meta_spend.groupby(["account", "campaign_name"], as_index=False)["spend"].sum().sort_values("spend", ascending=False)
            fig = px.bar(account_spend, x="account", y="spend", text_auto=".2s", title="Spend by Meta ad account", color_discrete_sequence=["#176b87"])
            fig.update_layout(xaxis_title="", yaxis_title="AED")
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(campaign_spend, hide_index=True, use_container_width=True, column_config={"spend": st.column_config.NumberColumn("Spend (AED)", format="%.2f")})
            st.download_button("Download Meta spend detail (.csv)", meta_spend.to_csv(index=False).encode("utf-8"), file_name=f"meta_spend_{start}_{end}.csv", mime="text/csv")

    with tabs[3]:
        st.markdown("#### Vendor-based profit analysis")
        st.caption("Profit is calculated only for WON rows using the confirmed fixed AED profit per order. Valid repeat orders are included.")
        vendors = dimension_summary(filtered, "vendor")
        fig = px.bar(
            vendors.sort_values("estimated_profit"), x="estimated_profit", y="vendor",
            orientation="h", text="won_orders", color="conversion_rate",
            title="Estimated profit by vendor · labels show WON orders",
            color_continuous_scale=["#dceff3", "#16856b"],
        )
        fig.update_layout(xaxis_title="Estimated profit (AED)", yaxis_title="", height=420)
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(
            vendors, hide_index=True, use_container_width=True,
            column_config={
                "vendor": "Vendor", "leads": "Rows", "won_orders": "WON orders",
                "revenue": st.column_config.NumberColumn("WON revenue (AED)", format="%.2f"),
                "estimated_profit": st.column_config.NumberColumn("Estimated profit (AED)", format="%.2f"),
                "conversion_rate": st.column_config.ProgressColumn("Conversion", min_value=0, max_value=100, format="%.1f%%"),
            },
        )

    with tabs[4]:
        agents = dimension_summary(filtered, "agent")
        agents = agents[agents["agent"].ne("UNASSIGNED")]
        st.markdown("#### Agent performance")
        fig = px.scatter(agents.head(40), x="leads", y="won_orders", size="revenue",
                         color="conversion_rate", hover_name="agent", title="Agent volume, wins and conversion",
                         color_continuous_scale=["#dceff3", "#16856b"])
        fig.update_layout(height=480, xaxis_title="Lead rows", yaxis_title="Won orders")
        st.plotly_chart(fig, use_container_width=True)
        st.dataframe(
            agents, hide_index=True, use_container_width=True,
            column_config={
                "agent": "Agent", "leads": "Lead rows", "won_orders": "Won orders",
                "revenue": st.column_config.NumberColumn("Won revenue (AED)", format="%.2f"),
                "repeat_orders": "Repeat orders",
                "conversion_rate": st.column_config.ProgressColumn("Conversion", min_value=0, max_value=100, format="%.1f%%"),
            },
        )

    with tabs[5]:
        breakdown_tabs = st.tabs(["Country", "Product", "Customer path", "Ad source"])
        for tab, dimension, label in zip(
            breakdown_tabs,
            ["country", "product", "customer_path", "ad_source"],
            ["Country", "Product", "Customer path", "Ad source"],
        ):
            with tab:
                summary = dimension_summary(filtered, dimension)
                summary[dimension] = summary[dimension].replace("", "UNMAPPED")
                chart = summary.head(25).sort_values("won_orders")
                fig = px.bar(chart, x="won_orders", y=dimension, orientation="h", color="conversion_rate",
                             title=f"Won orders by {label.lower()}", text="won_orders",
                             color_continuous_scale=["#dceff3", "#16856b"])
                fig.update_layout(height=max(380, len(chart) * 25), xaxis_title="Won orders", yaxis_title="")
                st.plotly_chart(fig, use_container_width=True)
                st.dataframe(summary, hide_index=True, use_container_width=True)

    with tabs[6]:
        quality = quality_summary(raw, historical)
        st.markdown("#### Reconciliation and source integrity")
        st.dataframe(quality, hide_index=True, use_container_width=True)
        st.info("Repeated phone numbers are intentionally retained. They can represent re-enquiries or reorders and are never used as a row-level uniqueness rule.")
        layout_counts = historical.groupby("source_layout").size().rename("normalized_rows").reset_index()
        st.markdown("#### Detected source layouts")
        st.dataframe(layout_counts, hide_index=True, use_container_width=True)
        st.markdown("#### Rows requiring review")
        review = historical[
            historical["phone"].eq("") | historical["agent"].eq("UNASSIGNED") | historical["exact_duplicate"]
        ]
        st.dataframe(review, hide_index=True, use_container_width=True)

    export = excel_bytes({
        "Normalized_Rows": filtered,
        "Monthly_Summary": monthly_summary(filtered),
        "Meta_Spend": meta_spend,
        "Agent_Summary": dimension_summary(filtered, "agent"),
        "Country_Summary": dimension_summary(filtered, "country"),
        "Product_Summary": dimension_summary(filtered, "product"),
        "Vendor_Profit": dimension_summary(filtered, "vendor"),
        "Data_Quality": quality_summary(raw, historical),
    })
    st.download_button(
        "Download historical analysis (.xlsx)", export,
        file_name=f"historical_business_analysis_{start}_{end}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary", use_container_width=True,
    )


analysis_mode = st.sidebar.radio(
    "Analysis mode", ["Current operations", "Historical business analysis"],
    help="Historical mode reads the January 2025 onward CRM workbook. Current operations uses DoubleTick and 3CX uploads.",
)

if analysis_mode == "Historical business analysis":
    st.sidebar.subheader("Historical source")
    historical_url = st.sidebar.text_input(
        "Google Sheet URL",
        value=f"https://docs.google.com/spreadsheets/d/{HISTORICAL_SHEET_ID}/edit",
    )
    uploaded_history = st.sidebar.file_uploader(
        "Or upload Excel", type=["xlsx", "xls"], key="historical_upload",
        help="The uploaded workbook takes precedence over the Google Sheet URL.",
    )
    if st.sidebar.button("Refresh historical data", use_container_width=True):
        load_historical_source.clear()
        st.session_state.pop("historical_analysis", None)
    load_requested = st.sidebar.button(
        "Load historical dashboard", type="primary", use_container_width=True
    )
    if load_requested:
        try:
            with st.spinner("Loading and normalizing the historical workbook…"):
                if uploaded_history is not None:
                    raw_history = read_historical_workbook(uploaded_history)
                    historical_data = normalize_historical_rows(raw_history)
                else:
                    raw_history, historical_data = load_historical_source(historical_url)
                st.session_state["historical_analysis"] = (raw_history, historical_data)
        except Exception as exc:
            st.error(f"Could not load the historical workbook: {exc}")
            st.stop()
    if "historical_analysis" not in st.session_state:
        st.info(
            "Historical data is loaded only when requested so the app starts quickly. "
            "Choose the Google Sheet or upload an Excel file, then click Load historical dashboard."
        )
        st.stop()
    raw_history, historical_data = st.session_state["historical_analysis"]
    render_historical_dashboard(raw_history, historical_data)
    st.stop()


with st.sidebar:
    st.header("Report controls")
    report_tz = st.selectbox("Report timezone", ["Asia/Dubai", "Asia/Kolkata"], format_func=lambda x: "GCC — Dubai (UTC+4)" if x == "Asia/Dubai" else "India — IST (UTC+5:30)")
    streak_gap = st.slider("Consecutive retry gap (minutes)", 1, 60, 15)
    st.caption("The attribution and 3CX reporting window is detected automatically from the uploaded DoubleTick Last message received column.")
    st.divider()
    st.subheader("Source timezones")
    tz_options = ["Asia/Dubai", "Asia/Kolkata"]
    dt_tz = st.selectbox("DoubleTick", tz_options, 0, help="Used for lead-time and speed-to-call only. It never removes rows from the DoubleTick upload.")
    cx_tz = st.selectbox("3CX", tz_options, 0)
    st.divider()
    st.caption("Google CRM orders refresh automatically every five minutes.")
    if st.button("Refresh orders now", use_container_width=True):
        load_google_crm_orders.clear()
        st.rerun()

st.subheader("1. Upload the two source reports")
c1, c2 = st.columns(2)
dt_file = c1.file_uploader("DoubleTick assignments", type=["csv", "xlsx", "xls", "zip"], help="Customer number + assigned agent number only")
cx_file = c2.file_uploader("3CX calls", type=["csv", "xlsx", "xls", "zip"])

if not all((dt_file, cx_file)):
    st.info("Upload DoubleTick and 3CX reports. Orders are read automatically from the live Google CRM sheet.")
    st.markdown("**Accepted:** CSV, Excel or ZIP containing CSV/Excel. The app will propose column mappings before processing.")
    st.stop()

try:
    dt_frames, cx_frames = read_upload(dt_file), read_upload(cx_file)
except Exception as exc:
    st.error(f"Could not read an upload: {exc}"); st.stop()

st.subheader("2. Confirm sheets and columns")
tabs = st.tabs(["DoubleTick assignment mapping", "3CX mapping"])
selected = {}
for tab, name, frames in zip(tabs, ["DoubleTick", "3CX"], [dt_frames, cx_frames]):
    with tab:
        best, _ = choose_best_sheet(frames, name.lower())
        sheet = st.selectbox("Report sheet/file", list(frames), index=list(frames).index(best), key=f"sheet_{name}")
        df = frames[sheet]
        st.caption(f"{len(df):,} rows × {len(df.columns)} columns")
        selected[name] = (df, mapping_ui(df, name))
        with st.expander("Preview source rows"):
            st.dataframe(df.head(10), use_container_width=True)

if any(not selected[name][1].get("phone") for name in selected):
    st.error("A phone column is required for every source."); st.stop()

dt_source, dt_mapping = selected["DoubleTick"]
selected_time_column = dt_mapping.get("datetime")


def parse_doubletick_timestamps(series):
    raw = series.astype("string").str.strip().replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    parsed = pd.to_datetime(raw, errors="coerce", dayfirst=True, format="mixed")
    numeric = pd.to_numeric(raw, errors="coerce")
    # DoubleTick exports may contain Unix seconds/ms/us or Excel serial dates.
    formats = [
        (numeric.between(1e17, 1e19), "ns", None),
        (numeric.between(1e14, 1e17), "us", None),
        (numeric.between(1e11, 1e14), "ms", None),
        (numeric.between(1e8, 1e11), "s", None),
        (numeric.between(20000, 80000), "D", "1899-12-30"),
    ]
    for mask, unit, origin in formats:
        fill_mask = parsed.isna() & mask.fillna(False)
        if fill_mask.any():
            values = pd.to_datetime(numeric[fill_mask], errors="coerce", unit=unit, origin=origin or "unix")
            parsed.loc[fill_mask] = values
    return parsed


candidate_columns = []
for column in [
    selected_time_column, "Last message received at", "Last message received",
    "Last CTWA lead at", "Last message sent at",
]:
    if column and column in dt_source.columns and column not in candidate_columns:
        candidate_columns.append(column)
best_count, message_time_column, message_times = 0, None, None
for column in candidate_columns:
    candidate_times = parse_doubletick_timestamps(dt_source[column])
    valid_count = int(candidate_times.notna().sum())
    if valid_count > best_count:
        best_count, message_time_column, message_times = valid_count, column, candidate_times
if not message_time_column:
    st.error("No valid DoubleTick last-activity timestamps were found. Select a populated Last message/Last CTWA column."); st.stop()
if selected_time_column and message_time_column != selected_time_column:
    st.warning(f"{selected_time_column} is empty or invalid. Reporting period detected from {message_time_column} ({best_count:,} valid timestamps).")
if getattr(message_times.dt, "tz", None) is None:
    message_times = message_times.dt.tz_localize(dt_tz, ambiguous="NaT", nonexistent="shift_forward")
else:
    message_times = message_times.dt.tz_convert(dt_tz)
report_start = message_times.min().tz_convert(report_tz)
report_end = message_times.max().tz_convert(report_tz)
start_date, end_date = report_start.date(), report_end.date()
call_start, call_end = report_start, report_end + pd.Timedelta(microseconds=1)
filter_calls = True
st.info(
    f"Detected from DoubleTick {message_time_column}: "
    f"{report_start.strftime('%d/%m/%Y %I:%M %p')} → {report_end.strftime('%d/%m/%Y %I:%M %p')} "
    f"({report_tz}). This detected range drives attribution and 3CX analysis."
)

st.subheader("3. Integrated fixed ZIP attribution engine")
api_key = secret("DOUBLETICK_API_KEY")
meta_token = secret("META_ACCESS_TOKEN")
st.caption("Every build replaces the integrated ZIP engine's phone list with the current DoubleTick all-customer Phone number column. Marketing is then calculated only from the newly generated DoubleTick Ad/Meta report and the product/vendor reference.")
submitted = st.button("Build dashboard", type="primary", use_container_width=True)

if submitted:
    with st.spinner("Normalizing and matching reports…"):
        leads = normalize_leads(*selected["DoubleTick"], dt_tz, report_tz)
        calls = normalize_calls(*selected["3CX"], cx_tz, report_tz)
        agent_crosswalk = agent_directory_frame()
        wabas = ["".join(filter(str.isdigit, x)) for x in secret("DOUBLETICK_WABA_NUMBERS", "971521367907").split(",") if x.strip()]
    if not api_key or not meta_token:
        st.error("DOUBLETICK_API_KEY and META_ACCESS_TOKEN are both required in Streamlit App settings → Secrets."); st.stop()
    phone_tuple = tuple(leads.lead_phone.fillna("").astype(str))
    bar = st.progress(10, text=f"Generating attribution for {len(set(phone_tuple)):,} unique phones — cached results are reused…")
    dt_report = cached_attribution_report(
        phone_tuple, api_key, meta_token, tuple(wabas), start_date, end_date,
    )
    bar.progress(100, text="DoubleTick and Meta attribution ready.")
    attribution = normalize_attribution(dt_report, {"phone": "phone", "ad_id": "ad_id", "campaign": "meta_campaign_name", "status": "meta_lookup_status", "classification": "classification_text"})
    leads = attach_attribution(leads, attribution)
    bar.empty()
    st.session_state["analysis_inputs"] = (
        leads, calls, call_start, call_end, report_tz, streak_gap,
        filter_calls, agent_crosswalk, dt_report, start_date, end_date,
    )

if "analysis_inputs" in st.session_state:
    leads, calls, call_start, call_end, report_tz, streak_gap, filter_calls, agent_crosswalk, dt_report, start_date, end_date = st.session_state["analysis_inputs"]
    try:
        crm_raw = load_google_crm_orders()
    except Exception as exc:
        st.error(f"Could not refresh Google CRM orders: {exc}")
        st.stop()
    sales = normalize_google_crm_orders(crm_raw, dt_report["phone"], report_tz)
    joined, orders, calls_in_window = build_analysis(leads, sales, calls, call_start, call_end, report_tz, streak_gap, filter_calls=filter_calls)
    ranges = {"DoubleTick attribution API dates": (start_date, end_date), "Google CRM orders": source_range(sales, "sale_time"), "3CX upload": source_range(calls, "call_time")}
    qa = qa_report(leads, sales, calls, ranges)
    spend_data, spend_errors, authoritative_spend, daily_spend, daily_campaign_spend = load_google_campaign_spend(
        SPEND_START_DATE, SPEND_END_DATE
    )
    st.session_state["analysis_results"] = (joined, orders, calls_in_window, ranges, qa, agent_crosswalk, dt_report, spend_data, spend_errors)
elif "analysis_results" in st.session_state:
    joined, orders, calls_in_window, ranges, qa, agent_crosswalk, dt_report, spend_data, spend_errors = st.session_state["analysis_results"]
else:
    st.stop()

if joined.empty:
    st.error("The DoubleTick upload contains no usable lead rows."); st.stop()

# The uploaded DoubleTick report is authoritative row-for-row. Do not remove
# duplicate phone numbers: each uploaded row represents one reported lead.
doubletick_report = joined.copy()

if len({dt_tz, cx_tz}) > 1:
    st.warning("Source timezones differ. Times were converted to the selected report timezone; verify those source timezone selections.")

st.markdown(f"""<div class="hero"><h2>Performance command centre</h2><p>{len(doubletick_report):,} DoubleTick report leads · {pd.Timestamp(call_start).strftime('%d %b, %I:%M %p')} to {pd.Timestamp(call_end).strftime('%d %b, %I:%M %p')} · Dubai time</p></div>""", unsafe_allow_html=True)
tabs = st.tabs(["Overview", "Marketing", "Sales", "3CX calls", "Agent scorecards", "Data quality"])

with tabs[0]:
    total_leads, total_orders = len(doubletick_report), len(orders)
    lead_orders = int(orders.order_from_generated_lead.sum())
    other_source_orders = total_orders - lead_orders
    gcc_leads = joined[joined.lead_region.eq("GCC")]
    other_leads = joined[joined.lead_region.eq("Other country")]
    st.markdown('<div class="section-label">Business outcomes</div>', unsafe_allow_html=True)
    metrics = st.columns(4)
    metrics[0].metric("Leads", f"{total_leads:,}")
    metrics[1].metric(
        "Total CRM orders",
        f"{total_orders:,}",
        f"{ORDER_START_DATE.strftime('%d %b')}–{ORDER_END_DATE.strftime('%d %b %Y')}",
    )
    metrics[2].metric("Orders from generated leads", f"{lead_orders:,}", f"{lead_orders / total_leads * 100:.1f}% of assigned leads")
    metrics[3].metric("Other-source orders", f"{other_source_orders:,}", f"{other_source_orders / total_orders * 100:.1f}% of orders" if total_orders else None)
    st.markdown('<div class="section-label">Call execution · GCC leads only</div>', unsafe_allow_html=True)
    call_metrics = st.columns(4)
    call_metrics[0].metric("GCC assigned", f"{len(gcc_leads):,}")
    call_metrics[1].metric("Called", f"{gcc_leads.called.sum():,}", f"{gcc_leads.called.mean()*100:.1f}% coverage")
    call_metrics[2].metric("Answered", f"{gcc_leads.answered_any.sum():,}", f"{gcc_leads.answered_any.mean()*100:.1f}% of GCC leads")
    call_metrics[3].metric("Never called", f"{(~gcc_leads.called).sum():,}", f"{(~gcc_leads.called).mean()*100:.1f}% requires action", delta_color="inverse")
    funnel = pd.DataFrame({"stage":["Assigned leads","GCC leads","Called GCC leads","Answered GCC leads","Converted orders"],"leads":[len(joined),len(gcc_leads),int(gcc_leads.called.sum()),int(gcc_leads.answered_any.sum()),lead_orders]})
    left, right = st.columns([1.45, 1])
    with left:
        fig = px.funnel(funnel, x="leads", y="stage", title="Lead-to-order journey", color="stage", color_discrete_sequence=["#176b87","#2389a8","#42a5b8","#7bc5ca","#d4a017"])
        fig.update_layout(showlegend=False, margin=dict(l=15,r=15,t=55,b=15), height=390)
        st.plotly_chart(fig, use_container_width=True)
    with right:
        disposition = pd.DataFrame({"status":["Answered","Called, no answer","Never called"],"leads":[int(gcc_leads.answered_any.sum()),int((gcc_leads.called & ~gcc_leads.answered_any).sum()),int((~gcc_leads.called).sum())]})
        fig = px.pie(disposition, names="status", values="leads", hole=.68, title="GCC call disposition", color="status", color_discrete_map={"Answered":"#16856b","Called, no answer":"#f0b44d","Never called":"#d04a42"})
        fig.update_layout(margin=dict(l=10,r=10,t=55,b=10),height=390,legend_orientation="h")
        st.plotly_chart(fig, use_container_width=True)
    failures = pd.DataFrame({
        "failure point": ["No call made", "Called but never answered", "Answered but no order", "Campaign not classified"],
        "leads": [(~gcc_leads.called).sum(), (gcc_leads.called & ~gcc_leads.answered_any).sum(), (gcc_leads.answered_any & ~gcc_leads.converted).sum(), joined.country.eq("Unmapped").sum()],
    }).sort_values("leads", ascending=False)
    failures.columns = ["Action needed", "Leads"]
    st.dataframe(failures, hide_index=True, use_container_width=True)

with tabs[1]:
    selected_period = st.date_input(
        "Marketing date range",
        value=(SPEND_START_DATE, SPEND_END_DATE),
        min_value=SPEND_START_DATE,
        max_value=SPEND_END_DATE,
        format="DD/MM/YYYY",
    )
    if isinstance(selected_period, (tuple, list)) and len(selected_period) == 2:
        marketing_start, marketing_end = selected_period
    else:
        marketing_start = marketing_end = selected_period
    if marketing_start > marketing_end:
        st.error("The marketing start date must be on or before the end date.")
        st.stop()

    lead_dates = pd.to_datetime(doubletick_report["lead_time"], errors="coerce").dt.date
    date_filtered_joined = doubletick_report[
        lead_dates.ge(marketing_start) & lead_dates.le(marketing_end)
    ].copy()
    campaign_spend_view = daily_campaign_spend[
        daily_campaign_spend["spend_date"].ge(marketing_start)
        & daily_campaign_spend["spend_date"].le(marketing_end)
    ].copy()
    daily_spend_view = daily_spend[
        daily_spend["date"].ge(marketing_start) & daily_spend["date"].le(marketing_end)
    ].copy()
    selected_authoritative_spend = float(daily_spend_view["spend"].sum())
    spend_data_view = aggregate_campaign_spend(campaign_spend_view, selected_authoritative_spend)

    missing_attr = doubletick_report[~doubletick_report.attribution_found].copy() if "attribution_found" in doubletick_report else doubletick_report.iloc[0:0]
    if len(missing_attr):
        st.error(f"{len(missing_attr):,} DoubleTick assignments are missing from the Ad/Meta attribution report.")
    if spend_errors:
        st.warning("Some Google Sheet campaign tabs could not be read: " + " | ".join(spend_errors))
    campaign_market = campaign_performance(doubletick_report, spend_data_view)
    total_spend = selected_authoritative_spend
    # The uploaded/generated DoubleTick report is authoritative for lead count.
    # Never remove its rows with the marketing spend date selector.
    marketing_joined = doubletick_report
    total_leads = int(marketing_joined.shape[0])
    matched_orders = orders[orders.order_from_generated_lead].copy()
    converted_leads = int(len(matched_orders))
    total_revenue = float(pd.to_numeric(matched_orders["order_amount"], errors="coerce").fillna(0).sum())
    primary_kpis = st.columns(3)
    primary_kpis[0].metric("Meta spend", f"AED {total_spend:,.2f}")
    primary_kpis[1].metric("DoubleTick leads", f"{total_leads:,}")
    primary_kpis[2].metric("Cost per lead", f"AED {total_spend / total_leads:,.2f}" if total_leads else "N/A")
    outcome_kpis = st.columns(2)
    outcome_kpis[0].metric("Converted orders", f"{converted_leads:,}", f"{converted_leads / total_leads * 100:.1f}% conversion" if total_leads else None)
    outcome_kpis[1].metric("Lead-order revenue / ROAS", f"AED {total_revenue:,.2f}", f"{total_revenue / total_spend:.2f}× ROAS" if total_spend else "ROAS unavailable")
    st.caption(
        f"Spend: Meta Report Google Sheet, {marketing_start.strftime('%d %b')}–"
        f"{marketing_end.strftime('%d %b %Y')} inclusive · Leads: complete generated DoubleTick report "
        "(not reduced by the spend-date filter) · "
        "Orders and revenue: live Google CRM phone matches."
    )
    st.markdown("#### Country-wise marketing performance")
    country_market = country_performance(doubletick_report, spend_data_view)
    country_cards = st.columns(4)
    for position, row in enumerate(country_market.itertuples(index=False)):
        with country_cards[position]:
            st.metric(
                row.country,
                f"AED {row.cpl:,.2f} CPL" if pd.notna(row.cpl) else "CPL unavailable",
                f"AED {row.spend:,.2f} spend · {row.leads:,} leads",
                delta_color="off",
            )
    st.dataframe(
        country_market,
        hide_index=True,
        use_container_width=True,
        column_config={
            "country": "Country",
            "spend": st.column_config.NumberColumn("Spend (AED)", format="%.2f"),
            "leads": st.column_config.NumberColumn("Attributed leads", format="%d"),
            "cpl": st.column_config.NumberColumn("CPL (AED)", format="%.2f"),
            "orders": st.column_config.NumberColumn("Converted orders", format="%d"),
            "revenue": st.column_config.NumberColumn("Revenue (AED)", format="%.2f"),
            "conversion_rate": st.column_config.ProgressColumn("Conversion %", min_value=0, max_value=100, format="%.1f%%"),
        },
    )

with tabs[2]:
    missing_wp = joined[~joined.workpex_found].copy()
    multiple_wp = joined[joined.workpex_match_count.gt(1)].copy()
    st.markdown("#### Live Google CRM order attribution")
    order_metrics = st.columns(3)
    order_metrics[0].metric("Total orders", f"{len(orders):,}")
    order_metrics[1].metric("Orders from generated leads", f"{int(orders.order_from_generated_lead.sum()):,}")
    order_metrics[2].metric("Other-source orders", f"{int((~orders.order_from_generated_lead).sum()):,}")
    source_breakdown = orders.groupby("order_source", dropna=False).size().rename("orders").reset_index().sort_values("orders", ascending=False)
    st.dataframe(source_breakdown, hide_index=True, use_container_width=True)
    st.caption("Customer Path is used only when NUMBER1 and NUMBER2 do not match the current generated DoubleTick phone list.")
    st.markdown("#### DoubleTick lead → Google CRM reconciliation")
    reconciliation = joined.workpex_reconciliation.value_counts().rename_axis("result").reset_index(name="leads")
    st.dataframe(reconciliation, hide_index=True, use_container_width=True)
    st.markdown("#### Leads without a matching CRM order")
    st.dataframe(missing_wp[["lead_phone", "lead_time", "agent", "agent_number", "campaign_name", "country", "product"]], hide_index=True, use_container_width=True)
    if len(multiple_wp):
        st.info(f"{len(multiple_wp):,} DoubleTick leads generated more than one unique Google CRM order.")
    sales_view = grouped(joined, "order_products") if "order_products" in joined else pd.DataFrame()
    st.dataframe(sales_view, hide_index=True, use_container_width=True)
    st.markdown("#### Converted order detail")
    st.dataframe(orders.sort_values("sale_time", ascending=False), hide_index=True, use_container_width=True)

with tabs[3]:
    gcc_joined = joined[joined.lead_region.eq("GCC")]
    gcc_keys = set(joined.loc[joined.lead_region.eq("GCC"), "call_key"])
    gcc_calls = calls_in_window[calls_in_window.call_key.isin(gcc_keys)].copy()
    unmatched_calls = calls_in_window[~calls_in_window.call_key.isin(gcc_keys)].copy()
    c = st.columns(6)
    c[0].metric("Total calls", f"{int(joined.call_count.sum()):,}")
    c[1].metric("Unanswered calls", f"{int(joined.unanswered_calls.sum()):,}")
    c[2].metric("Never-called GCC leads", f"{(~gcc_joined.called).sum():,}")
    c[3].metric("Repeated unanswered", f"{int(joined.consecutive_unanswered_retries.sum()):,}")
    avg_speed = joined.speed_to_first_call_minutes.clip(lower=0).mean()
    c[4].metric("Avg speed to first call", f"{avg_speed:.0f} min" if pd.notna(avg_speed) else "N/A")
    c[5].metric("Unmatched outbound calls", f"{len(unmatched_calls):,}")
    call_agent = gcc_calls.groupby("call_agent", dropna=False).agg(calls=("call_key", "size"), answered=("answered", "sum"), unique_leads=("call_key", "nunique"), talk_minutes=("duration_seconds", lambda x: x.sum()/60)).reset_index()
    call_agent["answer_rate"] = call_agent.answered.div(call_agent.calls).mul(100)
    st.dataframe(call_agent.sort_values("calls", ascending=False), hide_index=True, use_container_width=True)
    st.markdown("#### Leads requiring immediate follow-up")
    followup = joined[(~joined.called) | ((~joined.answered_any) & (~joined.converted))].sort_values(["called", "unanswered_calls"], ascending=[True, False])
    st.dataframe(followup[["lead_phone", "agent", "lead_time", "country", "product", "call_count", "unanswered_calls", "consecutive_unanswered_retries"]], hide_index=True, use_container_width=True)
    st.markdown("#### Other-country DoubleTick leads — excluded from GCC call KPI")
    other_lead_detail = joined[joined.lead_region.eq("Other country")][["lead_phone", "agent", "workpex_found", "converted"]]
    if other_lead_detail.empty:
        st.success("No other-country DoubleTick leads were found.")
    else:
        st.dataframe(other_lead_detail, hide_index=True, use_container_width=True)

with tabs[4]:
    total_by_agent = orders.groupby("sales_agent", dropna=False).size().rename("total_orders").astype(int)
    lead_by_agent = orders[orders.order_from_generated_lead].groupby("sales_agent", dropna=False).size().rename("lead_orders").astype(int)
    other_by_agent = orders[~orders.order_from_generated_lead].groupby("sales_agent", dropna=False).size().rename("other_orders").astype(int)
    agent_names = sorted(set(joined.agent.dropna().astype(str)) | set(total_by_agent.index.astype(str)))
    agent_cards = []
    for agent_name in agent_names:
        agent_rows = joined[joined.agent.eq(agent_name)]
        agent_gcc = agent_rows[agent_rows.lead_region.eq("GCC")]
        assigned = len(agent_rows)
        total_agent_orders = int(total_by_agent.get(agent_name, 0))
        lead_agent_orders = int(lead_by_agent.get(agent_name, 0))
        other_agent_orders = int(other_by_agent.get(agent_name, 0))
        agent_cards.append({"agent": agent_name, "assigned": assigned, "total_orders": total_agent_orders,
                            "lead_orders": lead_agent_orders, "other_orders": other_agent_orders,
                            "conversion": lead_agent_orders / assigned * 100 if assigned else 0.0,
                            "answered": int(agent_gcc.answered_any.sum()),
                            "not_dialed": int((~agent_gcc.called).sum()),
                            "coverage": agent_gcc.called.mean() * 100 if len(agent_gcc) else 0})
    agent_cards = pd.DataFrame(agent_cards).sort_values(["lead_orders", "total_orders"], ascending=False)
    st.markdown('<div class="section-label">Team leaderboard</div>', unsafe_allow_html=True)
    leaderboard_data = agent_cards.melt(id_vars="agent", value_vars=["lead_orders", "other_orders"], var_name="source", value_name="orders")
    leaderboard_data["source"] = leaderboard_data["source"].map({"lead_orders": "Generated leads", "other_orders": "Other sources"})
    leaderboard = px.bar(leaderboard_data, x="agent", y="orders", color="source", text="orders", title="Google CRM orders by agent and verified source", color_discrete_map={"Generated leads":"#16856b","Other sources":"#d4a017"})
    leaderboard.update_layout(xaxis_title="",yaxis_title="Orders",height=420,margin=dict(l=15,r=15,t=55,b=15),barmode="stack")
    st.plotly_chart(leaderboard, use_container_width=True)
    st.markdown('<div class="section-label">Individual agent cards</div>', unsafe_allow_html=True)
    card_columns = st.columns(3)
    for position, row in enumerate(agent_cards.itertuples(index=False)):
        agent_rows = joined[joined.agent.eq(row.agent)]
        with card_columns[position % 3]:
            st.markdown(f'''<div class="agent-card"><div class="agent-name">👤 {row.agent}</div><div class="agent-sub">Live Google CRM + current generated lead report</div><div class="agent-grid"><div class="agent-kpi"><b>{row.assigned:,}</b><span>DoubleTick assigned</span></div><div class="agent-kpi"><b>{row.total_orders:,}</b><span>Total orders</span></div><div class="agent-kpi"><b class="good">{row.lead_orders:,}</b><span>Orders from leads</span></div><div class="agent-kpi"><b>{row.other_orders:,}</b><span>Other-source orders</span></div><div class="agent-kpi"><b>{row.answered:,}</b><span>Answered GCC</span></div><div class="agent-kpi"><b class="risk">{row.not_dialed:,}</b><span>Not dialed GCC</span></div></div></div>''', unsafe_allow_html=True)
            st.progress(min(max(row.coverage / 100, 0), 1), text=f"Call coverage {row.coverage:.1f}%")
            with st.expander("View assigned lead details"):
                detail_cols = [column for column in ["lead_phone","country","product","called","answered_any","converted","order_count"] if column in agent_rows]
                st.dataframe(agent_rows[detail_cols], hide_index=True, use_container_width=True)
            with st.expander("View Google CRM orders"):
                converted_rows = orders[orders.sales_agent.eq(row.agent)]
                converted_cols = [column for column in ["order_id", "sale_time", "sales_agent", "order_source", "order_from_generated_lead", "matched_lead_phone", "customer_path", "order_status", "order_product", "order_amount"] if column in converted_rows]
                st.dataframe(converted_rows[converted_cols], hide_index=True, use_container_width=True)
    st.markdown('<div class="section-label">Full team comparison</div>', unsafe_allow_html=True)
    st.dataframe(agent_cards, hide_index=True, use_container_width=True, column_config={"conversion": st.column_config.ProgressColumn("Lead conversion %", min_value=0, max_value=100, format="%.1f%%"), "coverage": st.column_config.ProgressColumn("Call coverage %", min_value=0, max_value=100, format="%.1f%%")})

with tabs[5]:
    st.markdown("#### Detected source ranges")
    range_table = pd.DataFrame([{"source": k, "first timestamp": v[0], "last timestamp": v[1]} for k, v in ranges.items()])
    st.dataframe(range_table, hide_index=True, use_container_width=True)
    st.dataframe(qa, hide_index=True, use_container_width=True)
    st.markdown("#### Authoritative DoubleTick agent directory")
    st.caption("Transcribed from the supplied DoubleTick member screenshots. Unknown agent numbers remain visibly unmapped.")
    st.dataframe(agent_crosswalk, hide_index=True, use_container_width=True)
    country_values = joined["country"] if "country" in joined else pd.Series("Unmapped", index=joined.index)
    product_values = joined["product"] if "product" in joined else pd.Series("Unmapped", index=joined.index)
    unmapped_columns = [column for column in ["campaign_name", "ad_id", "lead_phone", "attribution_status"] if column in joined.columns]
    unmapped = joined.loc[country_values.eq("Unmapped") | product_values.eq("Unmapped"), unmapped_columns].drop_duplicates()
    st.markdown("#### Unmapped campaign names")
    st.dataframe(unmapped, hide_index=True, use_container_width=True)

agent_report = grouped(joined, "agent")
marketing_report = campaign_performance(joined, spend_data)
missing_workpex = joined[~joined.workpex_found].copy()
missing_attribution = joined[~joined.attribution_found].copy() if "attribution_found" in joined else joined.iloc[0:0]
download = excel_bytes({"Joined_Lead_Detail": joined, "Missing_Attribution": missing_attribution, "Leads_Without_CRM_Order": missing_workpex, "Agent_Performance": agent_report, "Agent_Directory": agent_crosswalk, "Marketing_Performance": marketing_report, "Google_Sheet_Spend": spend_data, "Google_CRM_Orders": orders, "Calls": calls_in_window, "QA": qa})
window_name = f"{start_date.strftime('%Y-%m-%d')}_{end_date.strftime('%Y-%m-%d')}"
st.download_button("Download complete analysis (.xlsx)", download, file_name=f"sales_marketing_analysis_{window_name}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary", use_container_width=True)

attribution_download = excel_bytes({"All_Chats": dt_report, "Ad_ID_Found": dt_report[dt_report.ad_id.ne("")], "Ad_ID_Missing": dt_report[dt_report.ad_id.eq("")], "Summary": dt_report.groupby("status").size().rename("count").reset_index()})
st.download_button("Download automated DoubleTick Ad/Meta report (.xlsx)", attribution_download, file_name=f"doubletick_ad_id_report_{window_name}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
