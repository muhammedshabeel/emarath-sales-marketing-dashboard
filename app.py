from __future__ import annotations
import io
from datetime import date, timedelta
import pandas as pd
import plotly.express as px
import streamlit as st

from analytics import agent_directory_frame, attach_attribution, build_analysis, grouped, normalize_attribution, normalize_calls, normalize_leads, normalize_sales, qa_report
from data_io import choose_best_sheet, detect_column, read_upload
from enrichment import enrich_doubletick, enrich_meta

st.set_page_config(page_title="Emarath Intelligence", page_icon="📊", layout="wide")

st.markdown("""
<style>
.block-container{padding-top:1.4rem;max-width:1500px}.metric-card{background:white;border:1px solid #e8e7df;border-radius:14px;padding:14px}
h1,h2,h3{letter-spacing:-.03em}.stMetric{background:#fff;border:1px solid #e9e7df;padding:14px;border-radius:12px}
[data-testid="stSidebar"]{border-right:1px solid #e4e1d7}
</style>
""", unsafe_allow_html=True)

st.title("Sales & Marketing Intelligence")
st.caption("DoubleTick attribution × Workpex conversion × 3CX call execution")


def secret(name, default=""):
    try: return str(st.secrets.get(name, default))
    except Exception: return default


def mapping_ui(df, source):
    roles = {
        "DoubleTick": ["phone", "agent_number"],
        "Attribution": ["phone", "ad_id", "campaign", "status"],
        "Workpex": ["phone", "datetime", "agent", "order_id", "status", "product", "amount"],
        "3CX": ["phone", "datetime", "agent", "call_status", "duration", "direction"],
    }[source]
    exact_defaults = {
        "DoubleTick": {"phone": "Phone number", "agent_number": "Agent Phone Number"},
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


with st.sidebar:
    st.header("Report controls")
    today = date.today()
    start_date = st.date_input("DoubleTick attribution start date", today - timedelta(days=1))
    end_date = st.date_input("DoubleTick attribution end date (inclusive)", today)
    report_tz = st.selectbox("Report timezone", ["Asia/Dubai", "Asia/Kolkata"], format_func=lambda x: "GCC — Dubai (UTC+4)" if x == "Asia/Dubai" else "India — IST (UTC+5:30)")
    streak_gap = st.slider("Consecutive retry gap (minutes)", 1, 60, 15)
    st.divider()
    st.subheader("Source timezones")
    tz_options = ["Asia/Dubai", "Asia/Kolkata"]
    dt_tz = st.selectbox("DoubleTick", tz_options, 0, help="Used for lead-time and speed-to-call only. It never removes rows from the DoubleTick upload.")
    wp_tz = st.selectbox("Workpex", tz_options, 0)
    cx_tz = st.selectbox("3CX", tz_options, 0)

if start_date > end_date:
    st.error("Start date must not be after end date."); st.stop()

st.info(
    f"Attribution API dates: {start_date.strftime('%d/%m/%Y')} → {end_date.strftime('%d/%m/%Y')} inclusive. "
    "The uploaded DoubleTick, Workpex and 3CX reports are treated as prefiltered, authoritative datasets. "
    "The dashboard analyzes every uploaded row without imposing a fixed reporting-time window."
)

st.subheader("1. Upload the three source reports")
c1, c2, c3 = st.columns(3)
dt_file = c1.file_uploader("DoubleTick assignments", type=["csv", "xlsx", "xls", "zip"], help="Customer number + assigned agent number only")
wp_file = c2.file_uploader("Workpex sales", type=["csv", "xlsx", "xls", "zip"])
cx_file = c3.file_uploader("3CX calls", type=["csv", "xlsx", "xls", "zip"])

if not all((dt_file, wp_file, cx_file)):
    st.info("Upload all three reports. The Ad/Meta attribution report will be generated automatically from the DoubleTick assignments.")
    st.markdown("**Accepted:** CSV, Excel or ZIP containing CSV/Excel. The app will propose column mappings before processing.")
    st.stop()

try:
    dt_frames, wp_frames, cx_frames = read_upload(dt_file), read_upload(wp_file), read_upload(cx_file)
except Exception as exc:
    st.error(f"Could not read an upload: {exc}"); st.stop()

st.subheader("2. Confirm sheets and columns")
tabs = st.tabs(["DoubleTick assignment mapping", "Workpex mapping", "3CX mapping"])
selected = {}
for tab, name, frames in zip(tabs, ["DoubleTick", "Workpex", "3CX"], [dt_frames, wp_frames, cx_frames]):
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

st.subheader("3. Integrated fixed ZIP attribution engine")
api_key = secret("DOUBLETICK_API_KEY")
meta_token = secret("META_ACCESS_TOKEN")
st.caption("Only customer numbers are sent from the DoubleTick assignment upload into the integrated doubletick_ad_id_meta_campaign_fixed engine. Campaign, country, product and vendor come from its generated report and the uploaded product/vendor reference.")
submitted = st.button("Build dashboard", type="primary", use_container_width=True)

if submitted:
    with st.spinner("Normalizing and matching reports…"):
        leads = normalize_leads(*selected["DoubleTick"], dt_tz, report_tz)
        sales = normalize_sales(*selected["Workpex"], wp_tz, report_tz)
        calls = normalize_calls(*selected["3CX"], cx_tz, report_tz)
        agent_crosswalk = agent_directory_frame()
        wabas = ["".join(filter(str.isdigit, x)) for x in secret("DOUBLETICK_WABA_NUMBERS", "971521367907").split(",") if x.strip()]
    if not api_key or not meta_token:
        st.error("DOUBLETICK_API_KEY and META_ACCESS_TOKEN are both required in Streamlit App settings → Secrets."); st.stop()
    bar = st.progress(0, text="Fetching DoubleTick chats…")
    dt_report = enrich_doubletick(leads.lead_phone.str.replace(r"\D", "", regex=True), api_key, wabas, start_date, end_date, progress=lambda x: bar.progress(x, text="Fetching DoubleTick chats…"))
    bar.progress(0, text="Resolving Ad IDs through Meta…")
    meta = enrich_meta(dt_report.ad_id, meta_token, progress=lambda x: bar.progress(x, text="Resolving Ad IDs through Meta…"))
    dt_report["ad_id_join"] = dt_report.ad_id.astype(str)
    dt_report = dt_report.merge(meta, on="ad_id_join", how="left").drop(columns="ad_id_join")
    meta_cols = ["meta_ad_name", "meta_adset_id", "meta_adset_name", "meta_campaign_id", "meta_campaign_name", "meta_lookup_status", "meta_error"]
    dt_report.loc[dt_report.ad_id.eq(""), meta_cols] = dt_report.loc[dt_report.ad_id.eq(""), meta_cols].fillna({"meta_lookup_status": "NOT_LOOKED_UP"})
    dt_report[meta_cols] = dt_report[meta_cols].fillna("")
    attribution = normalize_attribution(dt_report, {"phone": "phone", "ad_id": "ad_id", "campaign": "meta_campaign_name", "status": "meta_lookup_status"})
    leads = attach_attribution(leads, attribution)
    bar.empty()
    joined, orders, calls_in_window = build_analysis(leads, sales, calls, start_date, end_date, report_tz, streak_gap)
    ranges = {"DoubleTick attribution API dates": (start_date, end_date), "Workpex upload": source_range(sales, "sale_time"), "3CX upload": source_range(calls, "call_time")}
    qa = qa_report(leads, sales, calls, ranges)
    st.session_state["analysis_results"] = (joined, orders, calls_in_window, ranges, qa, agent_crosswalk, dt_report)
elif "analysis_results" in st.session_state:
    joined, orders, calls_in_window, ranges, qa, agent_crosswalk, dt_report = st.session_state["analysis_results"]
else:
    st.stop()

if joined.empty:
    st.error("The DoubleTick upload contains no usable lead rows."); st.stop()

if len({dt_tz, wp_tz, cx_tz}) > 1:
    st.warning("Source timezones differ. Times were converted to the selected report timezone; verify those source timezone selections.")

tabs = st.tabs(["Executive", "Marketing", "Sales", "3CX calls", "Agent performance", "Data quality"])

with tabs[0]:
    total_leads, total_orders = len(joined), int(joined.order_count.sum())
    metrics = st.columns(7)
    metrics[0].metric("Leads", f"{total_leads:,}")
    metrics[1].metric("Orders", f"{total_orders:,}")
    metrics[2].metric("Conversion", f"{total_orders / total_leads * 100:.1f}%")
    metrics[3].metric("Never called", f"{(~joined.called).sum():,}")
    metrics[4].metric("Answered leads", f"{joined.answered_any.sum():,}")
    metrics[5].metric("Unmapped campaigns", f"{joined.country.eq('Unmapped').sum():,}")
    metrics[6].metric("Missing in Workpex", f"{(~joined.workpex_found).sum():,}")
    daily = grouped(joined, "lead_date")
    fig = px.line(daily, x="lead_date", y=["leads", "orders", "called_leads"], markers=True, title="Daily funnel")
    st.plotly_chart(fig, use_container_width=True)
    failures = pd.DataFrame({
        "failure point": ["No call made", "Called but never answered", "Answered but no order", "Campaign not classified"],
        "leads": [(~joined.called).sum(), (joined.called & ~joined.answered_any).sum(), (joined.answered_any & ~joined.converted).sum(), joined.country.eq("Unmapped").sum()],
    }).sort_values("leads", ascending=False)
    st.dataframe(failures, hide_index=True, use_container_width=True)

with tabs[1]:
    missing_attr = joined[~joined.attribution_found].copy() if "attribution_found" in joined else joined.iloc[0:0]
    if len(missing_attr): st.error(f"{len(missing_attr):,} DoubleTick assignments are missing from the Ad/Meta attribution report.")
    view = st.radio("Break down by", ["country", "vendor", "product", "campaign_name"], horizontal=True)
    market = grouped(joined, view)
    st.dataframe(market, hide_index=True, use_container_width=True, column_config={"conversion_rate": st.column_config.NumberColumn("Conversion %", format="%.1f%%"), "call_coverage": st.column_config.NumberColumn("Call coverage %", format="%.1f%%")})
    fig = px.bar(market.head(20), x=view, y="leads", color="conversion_rate", text="orders", title=f"Leads and orders by {view.replace('_',' ')}")
    st.plotly_chart(fig, use_container_width=True)
    st.markdown("#### Attribution/classification exceptions")
    exceptions = joined[joined.country.eq("Unmapped") | joined.product.eq("Unmapped")][["lead_phone", "ad_id", "campaign_name", "attribution_status", "country", "product", "vendor"]].copy()
    if exceptions.empty:
        st.success("Every generated campaign was classified.")
    else:
        st.warning(f"{len(exceptions):,} leads could not be fully classified. The campaign name and attribution status below show the exact reason.")
        st.dataframe(exceptions, hide_index=True, use_container_width=True)

with tabs[2]:
    missing_wp = joined[~joined.workpex_found].copy()
    multiple_wp = joined[joined.workpex_match_count.gt(1)].copy()
    st.error(f"{len(missing_wp):,} DoubleTick leads are missing from Workpex in the selected reporting window.") if len(missing_wp) else st.success("Every DoubleTick lead appears in Workpex.")
    st.markdown("#### DoubleTick → Workpex reconciliation")
    reconciliation = joined.workpex_reconciliation.value_counts().rename_axis("result").reset_index(name="leads")
    st.dataframe(reconciliation, hide_index=True, use_container_width=True)
    st.markdown("#### Missing from Workpex")
    st.dataframe(missing_wp[["lead_phone", "lead_time", "agent", "agent_number", "campaign_name", "country", "product"]], hide_index=True, use_container_width=True)
    if len(multiple_wp):
        st.warning(f"{len(multiple_wp):,} DoubleTick leads matched multiple Workpex rows. Review before treating row counts as unique orders.")
    sales_view = grouped(joined, "order_products") if "order_products" in joined else pd.DataFrame()
    st.dataframe(sales_view, hide_index=True, use_container_width=True)
    st.markdown("#### Converted order detail")
    st.dataframe(orders.sort_values("sale_time", ascending=False), hide_index=True, use_container_width=True)

with tabs[3]:
    gcc_calls = calls_in_window[calls_in_window.call_region.eq("GCC")].copy()
    other_calls = calls_in_window[calls_in_window.call_region.eq("Other country")].copy()
    c = st.columns(6)
    c[0].metric("Total calls", f"{int(joined.call_count.sum()):,}")
    c[1].metric("Unanswered calls", f"{int(joined.unanswered_calls.sum()):,}")
    c[2].metric("Never-called leads", f"{(~joined.called).sum():,}")
    c[3].metric("Repeated unanswered", f"{int(joined.consecutive_unanswered_retries.sum()):,}")
    avg_speed = joined.speed_to_first_call_minutes.clip(lower=0).mean()
    c[4].metric("Avg speed to first call", f"{avg_speed:.0f} min" if pd.notna(avg_speed) else "N/A")
    c[5].metric("Other-country calls", f"{len(other_calls):,}")
    call_agent = gcc_calls.groupby("call_agent", dropna=False).agg(calls=("call_key", "size"), answered=("answered", "sum"), unique_leads=("call_key", "nunique"), talk_minutes=("duration_seconds", lambda x: x.sum()/60)).reset_index()
    call_agent["answer_rate"] = call_agent.answered.div(call_agent.calls).mul(100)
    st.dataframe(call_agent.sort_values("calls", ascending=False), hide_index=True, use_container_width=True)
    st.markdown("#### Leads requiring immediate follow-up")
    followup = joined[(~joined.called) | ((~joined.answered_any) & (~joined.converted))].sort_values(["called", "unanswered_calls"], ascending=[True, False])
    st.dataframe(followup[["lead_phone", "agent", "lead_time", "country", "product", "call_count", "unanswered_calls", "consecutive_unanswered_retries"]], hide_index=True, use_container_width=True)
    st.markdown("#### Other-country outbound numbers — excluded from lead call matching")
    if other_calls.empty:
        st.success("No other-country outbound calls were found in this reporting window.")
    else:
        other_summary = other_calls.groupby("call_number", dropna=False).agg(calls=("call_number", "size"), answered=("answered", "sum"), first_call=("call_time", "min"), last_call=("call_time", "max")).reset_index().sort_values("calls", ascending=False)
        st.dataframe(other_summary, hide_index=True, use_container_width=True)

with tabs[4]:
    agents = grouped(joined, "agent")
    st.dataframe(agents, hide_index=True, use_container_width=True, column_config={"conversion_rate": st.column_config.ProgressColumn("Conversion %", min_value=0, max_value=100, format="%.1f%%"), "call_coverage": st.column_config.ProgressColumn("Call coverage %", min_value=0, max_value=100, format="%.1f%%")})
    fig = px.scatter(agents, x="call_coverage", y="conversion_rate", size="leads", color="answer_rate", hover_name="agent", title="Agent execution: call coverage vs conversion")
    st.plotly_chart(fig, use_container_width=True)

with tabs[5]:
    st.markdown("#### Detected source ranges")
    range_table = pd.DataFrame([{"source": k, "first timestamp": v[0], "last timestamp": v[1]} for k, v in ranges.items()])
    st.dataframe(range_table, hide_index=True, use_container_width=True)
    st.dataframe(qa, hide_index=True, use_container_width=True)
    st.markdown("#### Authoritative DoubleTick agent directory")
    st.caption("Transcribed from the supplied DoubleTick member screenshots. Unknown agent numbers remain visibly unmapped.")
    st.dataframe(agent_crosswalk, hide_index=True, use_container_width=True)
    unmapped = joined[joined.country.eq("Unmapped") | joined.product.eq("Unmapped")][["campaign_name", "ad_id", "lead_phone"]].drop_duplicates()
    st.markdown("#### Unmapped campaign names")
    st.dataframe(unmapped, hide_index=True, use_container_width=True)

agent_report = grouped(joined, "agent")
marketing_report = grouped(joined, "campaign_name")
missing_workpex = joined[~joined.workpex_found].copy()
missing_attribution = joined[~joined.attribution_found].copy() if "attribution_found" in joined else joined.iloc[0:0]
download = excel_bytes({"Joined_Lead_Detail": joined, "Missing_Attribution": missing_attribution, "Missing_From_Workpex": missing_workpex, "Agent_Performance": agent_report, "Agent_Directory": agent_crosswalk, "Marketing": marketing_report, "Orders": orders, "Calls": calls_in_window, "QA": qa})
window_name = f"{start_date.strftime('%Y-%m-%d')}_{end_date.strftime('%Y-%m-%d')}"
st.download_button("Download complete analysis (.xlsx)", download, file_name=f"sales_marketing_analysis_{window_name}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary", use_container_width=True)

attribution_download = excel_bytes({"All_Chats": dt_report, "Ad_ID_Found": dt_report[dt_report.ad_id.ne("")], "Ad_ID_Missing": dt_report[dt_report.ad_id.eq("")], "Summary": dt_report.groupby("status").size().rename("count").reset_index()})
st.download_button("Download automated DoubleTick Ad/Meta report (.xlsx)", attribution_download, file_name=f"doubletick_ad_id_report_{window_name}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
