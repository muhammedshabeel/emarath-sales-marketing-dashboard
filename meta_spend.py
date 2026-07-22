from __future__ import annotations

from datetime import date

import pandas as pd
import requests


GRAPH_API_VERSION = "v23.0"
GRAPH_API_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

META_AD_ACCOUNTS = {
    "Emarath Global - KSA": "1221121826639046",
    "Ahamed Sijil Cv": "579039913293843",
    "Bsparq": "435136069211937",
    "Emarath": "1300398841158614",
    "Emarath-Qatar": "812635408189651",
    "emirath": "701691174308997",
}


def _insights_url(account_id: str) -> str:
    return f"{GRAPH_API_URL}/act_{account_id}/insights"


def fetch_meta_spend(
    access_token: str,
    start_date: date,
    end_date: date,
    accounts: dict[str, str] | None = None,
    timeout: int = 60,
) -> tuple[pd.DataFrame, list[str]]:
    """Fetch calendar-day campaign spend from Meta for an inclusive date range.

    Returns one row per account/campaign/day plus non-fatal account errors. Meta
    account reporting timezone is authoritative for the returned daily dates.
    """
    if not access_token:
        return pd.DataFrame(), ["META_ACCESS_TOKEN is missing in Streamlit Secrets."]
    if start_date > end_date:
        raise ValueError("Meta spend start date must be on or before the end date.")

    rows: list[dict] = []
    errors: list[str] = []
    account_map = accounts or META_AD_ACCOUNTS
    session = requests.Session()
    for account_name, account_id in account_map.items():
        url = _insights_url(account_id)
        params = {
            "access_token": access_token,
            "level": "campaign",
            "fields": "account_id,account_name,campaign_id,campaign_name,date_start,date_stop,spend",
            "time_range": '{"since":"%s","until":"%s"}' % (
                start_date.isoformat(), end_date.isoformat()
            ),
            "time_increment": 1,
            "limit": 500,
        }
        try:
            while url:
                response = session.get(url, params=params, timeout=timeout)
                payload = response.json()
                if not response.ok or payload.get("error"):
                    error = payload.get("error", {})
                    message = error.get("message") or response.text[:240]
                    raise RuntimeError(message)
                for item in payload.get("data", []):
                    rows.append({
                        "date": item.get("date_start"),
                        "account": account_name,
                        "account_id": str(item.get("account_id") or account_id),
                        "campaign_id": str(item.get("campaign_id") or ""),
                        "campaign_name": item.get("campaign_name") or "Unmapped campaign",
                        "spend": item.get("spend", 0),
                    })
                url = payload.get("paging", {}).get("next")
                params = None  # paging.next already contains the cursor and query
        except Exception as exc:
            errors.append(f"{account_name}: {str(exc)[:240]}")

    columns = ["date", "account", "account_id", "campaign_id", "campaign_name", "spend"]
    data = pd.DataFrame(rows, columns=columns)
    if data.empty:
        return data, errors
    data["date"] = pd.to_datetime(data["date"], errors="coerce").dt.date
    data["spend"] = pd.to_numeric(data["spend"], errors="coerce").fillna(0.0)
    data = data.dropna(subset=["date"])
    return data.sort_values(["date", "account", "campaign_name"]).reset_index(drop=True), errors


def daily_spend_summary(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame(columns=["date", "spend"])
    return data.groupby("date", as_index=False)["spend"].sum().sort_values("date")


def monthly_spend_summary(data: pd.DataFrame) -> pd.DataFrame:
    if data.empty:
        return pd.DataFrame(columns=["month", "spend"])
    monthly = data.copy()
    monthly["month"] = pd.to_datetime(monthly["date"]).dt.to_period("M").astype(str)
    return monthly.groupby("month", as_index=False)["spend"].sum().sort_values("month")
