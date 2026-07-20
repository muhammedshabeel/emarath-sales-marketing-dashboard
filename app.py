from __future__ import annotations
import io
from datetime import date, time, timedelta
import pandas as pd
import plotly.express as px
import streamlit as st
from urllib.parse import quote

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

ANALYSIS_SCHEMA_VERSION = 12
if st.session_state.get("analysis_schema_version") != ANALYSIS_SCHEMA_VERSION:
    st.session_state.pop("analysis_results", None)
    st.session_state["analysis_schema_version"] = ANALYSIS_SCHEMA_VERSION


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


SPEND_SHEET_ID = "1RSGCdB6UUFeFrX1mksMCBtElc9AijrKrlqR7tsP5fNg"
SPEND_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPEND_SHEET_ID}/edit"
SPEND_TABS = [
    "Campaign - Ahamed Sijil Cv", "Campaign - emirath", "Campaign - Bsparq",
    "Campaign - Emarath", "Campaign - Emarath-Qatar",
    "Campaign - Emarath Global - KSA",
]


def campaign_key(value):
    return pd.Series(value, dtype="string").fillna("").str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)


@st.cache_data(ttl=300, show_spinner=False)
def load_google_campaign_spend(window_end_date):
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
            frame = frame.loc[dates.eq(window_end_date), ["Campaign Name", "Campaign ID", "Spend"]].copy()
            if frame.empty:
                continue
            frame["Spend"] = pd.to_numeric(frame["Spend"], errors="coerce").fillna(0.0)
            frame["Account"] = tab.removeprefix("Campaign - ")
            frames.append(frame)
        except Exception as exc:
            errors.append(f"{tab}: {str(exc)[:120]}")
    if not frames:
        return pd.DataFrame(columns=["campaign_name_spend", "campaign_key", "spend", "spend_accounts"]), errors
    raw = pd.concat(frames, ignore_index=True)
    raw["campaign_key"] = campaign_key(raw["Campaign Name"])
    grouped_spend = raw.groupby(["campaign_key", "Campaign Name"], as_index=False).agg(
        spend=("Spend", "sum"),
        spend_accounts=("Account", lambda values: ", ".join(sorted(set(values)))),
    )
    grouped_spend = grouped_spend.rename(columns={"Campaign Name": "campaign_name_spend"})
    return grouped_spend, errors


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
    performance["cost_per_order"] = performance["spend"].div(performance["orders"].replace(0, pd.NA))
    performance["roas"] = performance["revenue"].div(performance["spend"].replace(0, pd.NA))
    performance["campaign_name"] = performance["campaign_name"].fillna("Unmatched spend campaign").astype(str)
    performance["conversion_rate"] = pd.to_numeric(performance["conversion_rate"], errors="coerce").fillna(0.0).astype(float)
    performance["orders"] = pd.to_numeric(performance["orders"], errors="coerce").fillna(0).astype(int)
    performance["spend"] = pd.to_numeric(performance["spend"], errors="coerce").fillna(0.0).astype(float)
    return performance.sort_values("spend", ascending=False)


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
        sales = normalize_sales(*selected["Workpex"], wp_tz, report_tz)
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
    joined, orders, calls_in_window = build_analysis(leads, sales, calls, call_start, call_end, report_tz, streak_gap, filter_calls=filter_calls)
    ranges = {"DoubleTick attribution API dates": (start_date, end_date), "Workpex upload": source_range(sales, "sale_time"), "3CX upload": source_range(calls, "call_time")}
    qa = qa_report(leads, sales, calls, ranges)
    spend_data, spend_errors = load_google_campaign_spend(end_date)
    st.session_state["analysis_results"] = (joined, orders, calls_in_window, ranges, qa, agent_crosswalk, dt_report, spend_data, spend_errors)
elif "analysis_results" in st.session_state:
    joined, orders, calls_in_window, ranges, qa, agent_crosswalk, dt_report, spend_data, spend_errors = st.session_state["analysis_results"]
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
    if len(missing_attr):
        st.error(f"{len(missing_attr):,} DoubleTick assignments are missing from the Ad/Meta attribution report.")
    if spend_errors:
        st.warning("Some Google Sheet campaign tabs could not be read: " + " | ".join(spend_errors))
    campaign_market = campaign_performance(joined, spend_data)
    total_spend = float(campaign_market.spend.sum())
    marketing_joined = joined[joined["campaign_name"].fillna("").astype(str).str.strip().ne("")]
    total_leads = int(marketing_joined.shape[0])
    converted_leads = int(marketing_joined.converted.sum())
    total_revenue = float(marketing_joined.order_value.sum())
    k = st.columns(6)
    k[0].metric("Meta spend", f"AED {total_spend:,.2f}")
    k[1].metric("Attributed leads", f"{total_leads:,}")
    k[2].metric("Cost per lead", f"AED {total_spend / total_leads:,.2f}" if total_leads else "N/A")
    k[3].metric("Converted leads", f"{converted_leads:,}", f"{converted_leads / total_leads * 100:.1f}% conversion" if total_leads else None)
    k[4].metric("Cost per order", f"AED {total_spend / converted_leads:,.2f}" if converted_leads else "N/A")
    k[5].metric("Workpex revenue / ROAS", f"AED {total_revenue:,.2f}", f"{total_revenue / total_spend:.2f}× ROAS" if total_spend else "ROAS unavailable")
    st.caption(f"Spend only: Google Sheet · Window ending {end_date.strftime('%d %b %Y')}. Leads: generated DoubleTick report. Orders and revenue: Workpex.")
    st.markdown("#### Exact campaign performance")
    campaign_columns = ["campaign_name", "spend_accounts", "spend", "leads", "orders", "revenue", "cpl", "conversion_rate", "cost_per_order", "roas"]
    st.dataframe(campaign_market[campaign_columns], hide_index=True, use_container_width=True, column_config={
        "spend": st.column_config.NumberColumn("Spend (AED)", format="%.2f"),
        "revenue": st.column_config.NumberColumn("Revenue (AED)", format="%.2f"),
        "cpl": st.column_config.NumberColumn("CPL (AED)", format="%.2f"),
        "conversion_rate": st.column_config.ProgressColumn("Conversion %", min_value=0, max_value=100, format="%.1f%%"),
        "cost_per_order": st.column_config.NumberColumn("Cost/order (AED)", format="%.2f"),
        "roas": st.column_config.NumberColumn("ROAS", format="%.2fx"),
    })
    chart_data = campaign_market[campaign_market.spend.gt(0)].head(20).copy()
    chart_data["campaign_name"] = chart_data["campaign_name"].fillna("Unmatched spend campaign").astype(str)
    chart_data["spend"] = pd.to_numeric(chart_data["spend"], errors="coerce").fillna(0.0).astype(float)
    chart_data["conversion_rate"] = pd.to_numeric(chart_data["conversion_rate"], errors="coerce").fillna(0.0).astype(float)
    chart_data["orders"] = pd.to_numeric(chart_data["orders"], errors="coerce").fillna(0).astype(int)
    fig = px.bar(chart_data, x="campaign_name", y="spend", color="conversion_rate", text="orders", title="Campaign spend and converted orders", color_continuous_scale=["#dceff3","#16856b"])
    fig.update_layout(xaxis_title="", yaxis_title="Spend (AED)", height=430)
    st.plotly_chart(fig, use_container_width=True)
    view = st.radio("Operational breakdown", ["country", "vendor", "product"], horizontal=True)
    market = grouped(joined, view)
    campaign_dimensions = joined[["campaign_name", view]].drop_duplicates()
    campaign_dimensions["campaign_key"] = campaign_key(campaign_dimensions["campaign_name"])
    dimension_spend = spend_data.merge(campaign_dimensions[["campaign_key", view]], on="campaign_key", how="left").groupby(view, dropna=False).spend.sum().reset_index()
    market = market.merge(dimension_spend, on=view, how="left")
    market["spend"] = market["spend"].fillna(0.0)
    market["cpl"] = market.spend.div(market.leads.replace(0, pd.NA))
    market["cost_per_order"] = market.spend.div(market.orders.replace(0, pd.NA))
    st.dataframe(market, hide_index=True, use_container_width=True, column_config={
        "spend": st.column_config.NumberColumn("Spend (AED)", format="%.2f"),
        "cpl": st.column_config.NumberColumn("CPL (AED)", format="%.2f"),
        "cost_per_order": st.column_config.NumberColumn("Cost/order (AED)", format="%.2f"),
        "conversion_rate": st.column_config.NumberColumn("Conversion %", format="%.1f%%"),
    })
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
    if len(missing_wp):
        st.error(f"{len(missing_wp):,} DoubleTick leads are missing from Workpex in the detected reporting period.")
    else:
        st.success("Every DoubleTick lead appears in Workpex.")
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
marketing_report = campaign_performance(joined, spend_data)
missing_workpex = joined[~joined.workpex_found].copy()
missing_attribution = joined[~joined.attribution_found].copy() if "attribution_found" in joined else joined.iloc[0:0]
download = excel_bytes({"Joined_Lead_Detail": joined, "Missing_Attribution": missing_attribution, "Missing_From_Workpex": missing_workpex, "Agent_Performance": agent_report, "Agent_Directory": agent_crosswalk, "Marketing_Performance": marketing_report, "Google_Sheet_Spend": spend_data, "Orders": orders, "Calls": calls_in_window, "QA": qa})
window_name = f"{start_date.strftime('%Y-%m-%d')}_{end_date.strftime('%Y-%m-%d')}"
st.download_button("Download complete analysis (.xlsx)", download, file_name=f"sales_marketing_analysis_{window_name}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary", use_container_width=True)

attribution_download = excel_bytes({"All_Chats": dt_report, "Ad_ID_Found": dt_report[dt_report.ad_id.ne("")], "Ad_ID_Missing": dt_report[dt_report.ad_id.eq("")], "Summary": dt_report.groupby("status").size().rename("count").reset_index()})
st.download_button("Download automated DoubleTick Ad/Meta report (.xlsx)", attribution_download, file_name=f"doubletick_ad_id_report_{window_name}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", use_container_width=True)
