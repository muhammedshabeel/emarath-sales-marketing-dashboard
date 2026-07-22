from datetime import date

from meta_spend import fetch_meta_spend, monthly_spend_summary


class Response:
    ok = True

    def json(self):
        return {
            "data": [{
                "account_id": "1", "campaign_id": "10", "campaign_name": "Test",
                "date_start": "2026-06-01", "spend": "12.50",
            }]
        }


def test_meta_spend_preserves_account_campaign_day(monkeypatch):
    monkeypatch.setattr("requests.Session.get", lambda *args, **kwargs: Response())
    data, errors = fetch_meta_spend(
        "token", date(2026, 6, 1), date(2026, 6, 30), accounts={"Account": "1"}
    )
    assert not errors
    assert len(data) == 1
    assert data.iloc[0].spend == 12.50
    assert monthly_spend_summary(data).iloc[0].spend == 12.50


def test_missing_token_is_reported_without_request():
    data, errors = fetch_meta_spend("", date(2026, 6, 1), date(2026, 6, 30))
    assert data.empty
    assert errors == ["META_ACCESS_TOKEN is missing in Streamlit Secrets."]
