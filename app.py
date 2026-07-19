from __future__ import annotations
import io
from datetime import date, time, timedelta
import pandas as pd
import plotly.express as px
import streamlit as st

from analytics import agent_directory_frame, attach_attribution, build_analysis, grouped, normalize_attribution, normalize_calls, normalize_leads, normalize_sales, qa_report
from data_io import choose_best_sheet, detect_column, read_upload
from enrichment import generate_fixed_zip_report

st.set_page_config(page_title="Emarath Intelligence", page_icon="📊", layout="wide")

st.markdown("""
<style>
.stApp{background:linear-gradient(180deg,#f7f9fc 0,#fff 340px)}
.block-container{padding-top:1.25rem;max-width:1480px}h1,h2,h3{letter-spacing:-.035em;color:#132238}
[data-testid="stSidebar"]{background:#fff;border-right:1px solid #e6eaf0}
[data-testid="stMetric"]{background:#fff;border:1px solid #e6eaf0;padding:18px;border-radius:18px;box-shadow:0 7px 24px rgba(23,42,79,.055)}
[data-testid="stMetricLabel"]{color:#667085;font-weight:650}[data-testid="stMetricValue"]{color:#132238;font-weight:750}
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
st.caption("DoubleTick attribution × Workpex conversion × 3CX call execution")

ANALYSIS_SCHEMA_VERSION = 6
if st.session_state.get("analysis_schema_version") != ANALYSIS_SCHEMA_VERSION:
    st.session_state.pop("analysis_results", None)
    st.session_state["analysis_schema_version"] = ANALYSIS_SCHEMA_VERSION


def secret(name, default=""):
    try: return str(st.secrets.get(name, default))
    except Exception: return default


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


with st.sidebar:
    st.header("Report controls")
    report_tz = st.selectbox("Report timezone", ["Asia/Dubai", "Asia/Kolkata"], format_func=lambda x: "GCC — Dubai (UTC+4)" if x == "Asia/Dubai" else "India — IST (UTC+5:30)")
    streak_gap = st.slider("Consecutive retry gap (minutes)", 1, 60, 15)
    st.caption("The attribution and 3CX reporting window is detected automatically from the uploaded DoubleTick Last message received column.")
    st.divider()
    st.subheader("Source timezones")
    tz_options = ["Asia/Dubai", "Asia/Kolkata"]
    dt_tz = st.selectbox("DoubleTick", tz_options, 0, help="Used for lead-time and speed-to-call only. It never removes rows from the DoubleTick upload.")
    wp_tz = st.selectbox("Workpex", tz_options, 0)
    cx_tz = st.selectbox("3CX", tz_options, 0)

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

dt_source, dt_mapping = selected["DoubleTick"]
message_time_column = dt_mapping.get("datetime")
if not message_time_column:
    st.error("Select the DoubleTick Last message received column to detect the reporting period."); st.stop()
message_times = pd.to_datetime(dt_source[message_time_column], errors="coerce", dayfirst=True, format="mixed")
if message_times.notna().sum() == 0:
    st.error(f"No valid timestamps were found in DoubleTick column: {message_time_column}."); st.stop()
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
        sales = normalize_sales(*selected["Workpex"], wp_tz, report_tz)
        calls = normalize_calls(*selected["3CX"], cx_tz, report_tz)
        agent_crosswalk = agent_directory_frame()
        wabas = ["".join(filter(str.isdigit, x)) for x in secret("DOUBLETICK_WABA_NUMBERS", "971521367907").split(",") if x.strip()]
    if not api_key or not meta_token:
        st.error("DOUBLETICK_API_KEY and META_ACCESS_TOKEN are both required in Streamlit App settings → Secrets."); st.stop()
    bar = st.progress(0, text="Fetching DoubleTick chats…")
    dt_report = generate_fixed_zip_report(
        leads.lead_phone, api_key, meta_token, wabas, start_date, end_date,
        doubletick_progress=lambda x: bar.progress(x, text="Generating report from the replaced DoubleTick phone list…"),
        meta_progress=lambda x: bar.progress(x, text="Resolving generated Ad IDs through Meta…"),
    )
    attribution = normalize_attribution(dt_report, {"phone": "phone", "ad_id": "ad_id", "campaign": "meta_campaign_name", "status": "meta_lookup_status", "classification": "classification_text"})
    leads = attach_attribution(leads, attribution)
    bar.empty()
    joined, orders, calls_in_window = build_analysis(leads, sales, calls, call_start, call_end, report_tz, streak_gap, filter_calls=filter_calls)
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

st.markdown(f"""<div class="hero"><h2>Performance command centre</h2><p>{len(joined):,} assigned leads · {pd.Timestamp(call_start).strftime('%d %b, %I:%M %p')} to {pd.Timestamp(call_end).strftime('%d %b, %I:%M %p')} · Dubai time</p></div>""", unsafe_allow_html=True)
tabs = st.tabs(["Overview", "Marketing", "Sales", "3CX calls", "Agent scorecards", "Data quality"])

with tabs[0]:
    total_leads, total_orders = len(joined), int(joined.order_count.sum())
    gcc_leads = joined[joined.lead_region.eq("GCC")]
    other_leads = joined[joined.lead_region.eq("Other country")]
    st.markdown('<div class="section-label">Business outcomes</div>', unsafe_allow_html=True)
    metrics = st.columns(4)
    metrics[0].metric("Leads", f"{total_leads:,}")
    metrics[1].metric("Orders", f"{total_orders:,}")
    metrics[2].metric("Conversion", f"{total_orders / total_leads * 100:.1f}%")
    metrics[3].metric("Workpex matched", f"{joined.workpex_found.sum():,}", f"{joined.workpex_found.mean()*100:.1f}% coverage")
    st.markdown('<div class="section-label">Call execution · GCC leads only</div>', unsafe_allow_html=True)
    call_metrics = st.columns(4)
    call_metrics[0].metric("GCC assigned", f"{len(gcc_leads):,}")
    call_metrics[1].metric("Called", f"{gcc_leads.called.sum():,}", f"{gcc_leads.called.mean()*100:.1f}% coverage")
    call_metrics[2].metric("Answered", f"{gcc_leads.answered_any.sum():,}", f"{gcc_leads.answered_any.mean()*100:.1f}% of GCC leads")
    call_metrics[3].metric("Never called", f"{(~gcc_leads.called).sum():,}", f"{(~gcc_leads.called).mean()*100:.1f}% requires action", delta_color="inverse")
    funnel = pd.DataFrame({"stage":["Assigned leads","GCC leads","Called GCC leads","Answered GCC leads","Converted leads"],"leads":[len(joined),len(gcc_leads),int(gcc_leads.called.sum()),int(gcc_leads.answered_any.sum()),int(joined.converted.sum())]})
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
    missing_attr = joined[~joined.attribution_found].copy() if "attribution_found" in joined else joined.iloc[0:0]
    if len(missing_attr): st.error(f"{len(missing_attr):,} DoubleTick assignments are missing from the Ad/Meta attribution report.")
    view = st.radio("Break down by", ["country", "vendor", "product", "campaign_name"], horizontal=True)
    market = grouped(joined, view)
    st.dataframe(market, hide_index=True, use_container_width=True, column_config={"conversion_rate": st.column_config.NumberColumn("Conversion %", format="%.1f%%"), "call_coverage": st.column_config.NumberColumn("Call coverage %", format="%.1f%%")})
    fig = px.bar(market.head(20), x=view, y="leads", color="conversion_rate", text="orders", title=f"Leads and orders by {view.replace('_',' ')}")
    st.plotly_chart(fig, use_container_width=True)
    st.markdown("#### Attribution/classification exceptions")
    country_values = joined["country"] if "country" in joined else pd.Series("Unmapped", index=joined.index)
    product_values = joined["product"] if "product" in joined else pd.Series("Unmapped", index=joined.index)
    exception_mask = country_values.eq("Unmapped") | product_values.eq("Unmapped")
    exception_columns = [column for column in ["lead_phone", "ad_id", "campaign_name", "attribution_status", "country", "product", "vendor"] if column in joined.columns]
    exceptions = joined.loc[exception_mask, exception_columns].copy()
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
    agents = grouped(joined, "agent")
    agents = agents.sort_values(["orders","conversion_rate"], ascending=False)
    agent_cards = []
    for agent_name, agent_rows in joined.groupby("agent", dropna=False):
        agent_gcc = agent_rows[agent_rows.lead_region.eq("GCC")]
        assigned = len(agent_rows)
        converted = int(agent_rows.converted.sum())
        agent_cards.append({"agent": agent_name, "assigned": assigned, "converted": converted,
                            "conversion": converted / assigned * 100 if assigned else 0,
                            "answered": int(agent_gcc.answered_any.sum()),
                            "not_dialed": int((~agent_gcc.called).sum()),
                            "coverage": agent_gcc.called.mean() * 100 if len(agent_gcc) else 0})
    agent_cards = pd.DataFrame(agent_cards).sort_values(["converted", "conversion"], ascending=False)
    st.markdown('<div class="section-label">Team leaderboard</div>', unsafe_allow_html=True)
    leaderboard = px.bar(agent_cards, x="agent", y="converted", color="conversion", text="converted", title="Converted leads by assigned agent", color_continuous_scale=["#dceff3","#16856b"])
    leaderboard.update_layout(xaxis_title="",yaxis_title="Converted leads",height=390,margin=dict(l=15,r=15,t=55,b=15))
    st.plotly_chart(leaderboard, use_container_width=True)
    st.markdown('<div class="section-label">Individual agent cards</div>', unsafe_allow_html=True)
    card_columns = st.columns(3)
    for position, row in enumerate(agent_cards.itertuples(index=False)):
        agent_rows = joined[joined.agent.eq(row.agent)]
        with card_columns[position % 3]:
            st.markdown(f'''<div class="agent-card"><div class="agent-name">👤 {row.agent}</div><div class="agent-sub">Assigned lead owner</div><div class="agent-grid"><div class="agent-kpi"><b>{row.assigned:,}</b><span>Assigned leads</span></div><div class="agent-kpi"><b class="good">{row.converted:,}</b><span>Converted · {row.conversion:.1f}%</span></div><div class="agent-kpi"><b>{row.answered:,}</b><span>Answered GCC</span></div><div class="agent-kpi"><b class="risk">{row.not_dialed:,}</b><span>Not dialed GCC</span></div></div></div>''', unsafe_allow_html=True)
            st.progress(min(max(row.coverage / 100, 0), 1), text=f"Call coverage {row.coverage:.1f}%")
            with st.expander("View assigned lead details"):
                detail_cols = [column for column in ["lead_phone","country","product","called","answered_any","converted","order_count"] if column in agent_rows]
                st.dataframe(agent_rows[detail_cols], hide_index=True, use_container_width=True)
    st.markdown('<div class="section-label">Full team comparison</div>', unsafe_allow_html=True)
    st.dataframe(agents, hide_index=True, use_container_width=True, column_config={"conversion_rate": st.column_config.ProgressColumn("Conversion %", min_value=0, max_value=100, format="%.1f%%"), "call_coverage": st.column_config.ProgressColumn("Call coverage %", min_value=0, max_value=100, format="%.1f%%")})

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
marketing_report = grouped(joined, "campaign_name")
missing_workpex = joined[~joined.workpex_found].copy()
missing_attribution = joined[~joined.attribution_found].copy() if "attribution_found" in joined else joined.iloc[0:0]
download = excel_bytes({"Joined_Lead_Detail": joined, "Missing_Attribution": missing_attribution, "Missing_From_Workpex": missing_workpex, "Agent_Performance": agent_report, "Agent_Directory": agent_crosswalk, "Marketing": marketing_report, "Orders": orders, "Calls": calls_in_window, "QA": qa})
window_name = f"{start_date.strftime('%Y-%m-%d')}_{end_date.strftime('%Y-%m-%d')}"
st.download_button("Download complete analysis (.xlsx)", download, file_name=f"sales_marketing_analysis_{window_name}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary", use_container_width=True)

attribution_download = excel_bytes({"All_Chats": dt_report, "Ad_ID_Found": dt_report[dt_report.ad_id.ne("")], "Ad_ID_Missing": dt_report[dt_report.ad_id.eq("")], "Summary": dt_report.groupby("status").size().rename("count").reset_index()})
st.download_button("Download automated DoubleTick Ad/Meta report (.xlsx)", attribution_download, file_name=f"doubletick_ad_id_report_{window_name}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
