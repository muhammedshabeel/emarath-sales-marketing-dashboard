from __future__ import annotations

import pandas as pd
import streamlit as st

from historical_business import load_historical_business, render_historical_business
from meta_spend import META_AD_ACCOUNTS, fetch_meta_spend

st.set_page_config(page_title="Historical Business Intelligence", page_icon="📈", layout="wide")

st.markdown(
    """
    <style>
    .block-container{padding-top:1.1rem;max-width:1500px}
    [data-testid="stMetric"]{background:white;border:1px solid #e6eaf0;padding:16px;border-radius:16px}
    .period-card{padding:20px 24px;border-radius:20px;background:linear-gradient(120deg,#102a43,#176b87);color:white;margin-bottom:16px}
    .period-card h2{color:white;margin:0 0 6px}.period-card p{margin:0;color:#d9edf4}
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("Historical Business Intelligence")
st.caption("Monthly leads sheet → agent-created orders → Sales CRM final outcome → live Meta spend")


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
    accounts = dict(accounts_items)
    return fetch_meta_spend(access_token, start_date, end_date, accounts=accounts)


if "historical_year" not in st.session_state:
    st.session_state["historical_year"] = 2025
if "historical_month" not in st.session_state:
    st.session_state["historical_month"] = 12

st.markdown("### Select reporting period")
year_cols = st.columns(3)
for index, year_value in enumerate([2024, 2025, 2026]):
    if year_cols[index].button(
        str(year_value),
        type="primary" if st.session_state["historical_year"] == year_value else "secondary",
        use_container_width=True,
        key=f"history_year_{year_value}",
    ):
        st.session_state["historical_year"] = year_value
        if year_value == 2026 and st.session_state["historical_month"] > 4:
            st.session_state["historical_month"] = 4
        st.rerun()

selected_year = int(st.session_state["historical_year"])
max_month = 4 if selected_year == 2026 else 12
month_names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][:max_month]
selected_month = min(int(st.session_state["historical_month"]), max_month)
st.session_state["historical_month"] = selected_month

for start_index in range(0, len(month_names), 6):
    row_months = month_names[start_index:start_index + 6]
    cols = st.columns(len(row_months))
    for offset, month_name in enumerate(row_months):
        month_number = start_index + offset + 1
        if cols[offset].button(
            month_name,
            type="primary" if selected_month == month_number else "secondary",
            use_container_width=True,
            key=f"history_month_{selected_year}_{month_number}",
        ):
            st.session_state["historical_month"] = month_number
            st.rerun()

selected_month = int(st.session_state["historical_month"])
selected_period = f"{selected_year}-{selected_month:02d}"
period = pd.Period(selected_period, freq="M")

st.markdown(
    f'<div class="period-card"><h2>{period.strftime("%B %Y")}</h2>'
    f'<p>2024 and 2025 include all months. 2026 currently includes January–April.</p></div>',
    unsafe_allow_html=True,
)

control_col, status_col = st.columns([1, 3])
refresh = control_col.button("Refresh source data", use_container_width=True)
status_col.info("Lead count for Meta CPL uses only Customer Path = LEAD. Missed Lead, Broadcast and Re-Order remain separate business metrics.")

if refresh:
    load_historical_business.clear()
    cached_meta_spend.clear()
    st.session_state.pop("historical_business_data", None)

if "historical_business_data" not in st.session_state:
    try:
        with st.spinner("Loading the 2024–2026 monthly leads and Sales CRM sheets…"):
            st.session_state["historical_business_data"] = load_historical_business()
    except Exception as exc:
        st.error(f"Could not load historical business sources: {exc}")
        st.stop()

historical_leads, historical_crm = st.session_state["historical_business_data"]
available_periods = set(historical_leads["month"].dropna().astype(str))
if selected_period not in available_periods:
    st.warning(f"No normalized lead rows were found for {period.strftime('%B %Y')}.")
    st.stop()

meta_token = secret("META_ACCESS_TOKEN")
try:
    with st.spinner("Fetching Meta spend for the selected month…"):
        meta_spend, meta_errors = cached_meta_spend(
            meta_token,
            period.start_time.date(),
            period.end_time.date(),
            tuple(configured_meta_accounts().items()),
        )
except Exception as exc:
    meta_spend = pd.DataFrame(columns=["date", "campaign_name", "account", "spend"])
    meta_errors = [str(exc)]

render_historical_business(
    historical_leads,
    historical_crm,
    meta_spend,
    selected_period,
)

if meta_errors:
    st.warning("Meta spend could not be read from every account: " + " | ".join(meta_errors))
