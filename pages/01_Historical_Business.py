from __future__ import annotations

import pandas as pd
import streamlit as st

from historical_business import (
    load_historical_business,
    load_historical_dataset,
    render_historical_business,
)
from meta_spend import META_AD_ACCOUNTS, fetch_meta_spend

st.set_page_config(page_title="Historical Business Intelligence", page_icon="📈", layout="wide")

st.markdown(
    """
    <style>
    .block-container{padding-top:1.1rem;max-width:1500px}
    [data-testid="stMetric"]{background:white;border:1px solid #e6eaf0;padding:16px;border-radius:16px}
    .period-card,.hero{padding:20px 24px;border-radius:20px;background:linear-gradient(120deg,#102a43,#176b87);color:white;margin-bottom:16px}
    .period-card h2,.hero h2{color:white;margin:0 0 6px}.period-card p,.hero p{margin:0;color:#d9edf4}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Historical Business Intelligence")
st.caption("Monthly leads sheet → agent-created orders → Sales CRM final outcome → optional live Meta spend")


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


@st.cache_data(ttl=1800, show_spinner=False)
def cached_meta_spend(access_token: str, start_date, end_date, accounts_items):
    return fetch_meta_spend(
        access_token,
        start_date,
        end_date,
        accounts=dict(accounts_items),
    )


refresh = st.button("Refresh historical source", use_container_width=False)
if refresh:
    load_historical_business.clear()
    load_historical_dataset.clear()
    cached_meta_spend.clear()
    for key in list(st.session_state):
        if key.startswith("historical_meta_"):
            st.session_state.pop(key, None)

try:
    # cache_resource owns the one shared in-memory DataFrame. Do not duplicate this
    # large dataset in every browser session.
    with st.spinner("Opening cached historical dataset…"):
        historical_leads, historical_crm = load_historical_business()
except Exception as exc:
    st.error(f"Could not load the cached historical dataset: {exc}")
    st.stop()

if historical_leads.empty or "month" not in historical_leads.columns:
    st.error("Historical snapshot is empty or does not contain a month column.")
    st.stop()

period_values = sorted(
    {
        str(value)
        for value in historical_leads["month"].dropna().astype(str)
        if len(str(value)) == 7 and str(value)[4] == "-"
    }
)
if not period_values:
    st.error("No valid historical reporting months were found.")
    st.stop()

years = sorted({int(value[:4]) for value in period_values})
default_year = 2025 if 2025 in years else years[-1]
year_index = years.index(int(st.session_state.get("historical_year", default_year))) if int(st.session_state.get("historical_year", default_year)) in years else years.index(default_year)
selected_year = st.selectbox("Reporting year", years, index=year_index)
st.session_state["historical_year"] = selected_year

year_periods = [value for value in period_values if value.startswith(f"{selected_year}-")]
month_labels = [pd.Period(value, freq="M").strftime("%B") for value in year_periods]
stored_period = str(st.session_state.get("historical_period", year_periods[-1]))
period_index = year_periods.index(stored_period) if stored_period in year_periods else len(year_periods) - 1
selected_label = st.selectbox("Reporting month", month_labels, index=period_index)
selected_period = year_periods[month_labels.index(selected_label)]
st.session_state["historical_period"] = selected_period
period = pd.Period(selected_period, freq="M")

st.markdown(
    f'<div class="period-card"><h2>{period.strftime("%B %Y")}</h2>'
    f'<p>Select one analysis view. Only that section is calculated.</p></div>',
    unsafe_allow_html=True,
)

views = [
    "Executive overview",
    "Customer paths",
    "Sales CRM outcomes",
    "Agents",
    "Marketing",
    "Insights & data",
]
selected_view = st.selectbox("Analysis view", views, index=0)

meta_spend = pd.DataFrame(columns=["date", "campaign_name", "account", "spend"])
meta_errors: list[str] = []
meta_key = f"historical_meta_{selected_period}"

if selected_view in {"Executive overview", "Marketing"}:
    load_col, status_col = st.columns([1, 3])
    load_meta = load_col.button(
        "Load live Meta spend",
        use_container_width=True,
        help="Historical sales and lead metrics render without waiting for Meta.",
    )
    if load_meta:
        token = secret("META_ACCESS_TOKEN")
        if not token:
            status_col.warning("META_ACCESS_TOKEN is not configured in Streamlit secrets.")
        else:
            try:
                with st.spinner("Fetching Meta spend for this month…"):
                    result, errors = cached_meta_spend(
                        token,
                        period.start_time.date(),
                        period.end_time.date(),
                        tuple(configured_meta_accounts().items()),
                    )
                st.session_state[meta_key] = (result, errors)
            except Exception as exc:
                st.session_state[meta_key] = (meta_spend, [str(exc)])
    if meta_key in st.session_state:
        meta_spend, meta_errors = st.session_state[meta_key]
        status_col.success("Meta spend loaded from cache.")
    else:
        status_col.info("Historical business data is ready. Meta spend is optional and loads separately.")

render_historical_business(
    historical_leads,
    historical_crm,
    meta_spend,
    selected_period,
    selected_view=selected_view,
)

if meta_errors:
    st.warning("Meta spend could not be read from every account: " + " | ".join(meta_errors))
