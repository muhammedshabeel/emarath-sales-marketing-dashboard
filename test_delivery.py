import pandas as pd
import pytest

from delivery import delivery_kpis, normalize_delivery


MAPPING = {
    "tracking_id": "TRACKING ID", "status": "CS STATUS", "amount": "VALUE",
    "phone": "NUMBER1", "agent": "AGENT", "delivery_partner": "DELIVERY AGENTS",
    "order_date": "DATE",
}


def test_status_is_authoritative_and_reorders_are_preserved():
    source = pd.DataFrame({
        "TRACKING ID": ["A1", "A2", "A3", "A4"],
        "CS STATUS": ["Sale closed", "deliverd/unpaid", "Cancelld&Return", "Dispatched"],
        "VALUE": [100, 80, 60, 50],
        "NUMBER1": [971501234567] * 4,
        "AGENT": ["Agent A"] * 4,
        "DELIVERY AGENTS": ["Partner X"] * 4,
        "DATE": ["01/08/2026"] * 4,
        "REASON": ["NO RESPONSE", "", "", ""],
    })
    result = normalize_delivery(source, MAPPING, "Asia/Dubai", "Asia/Dubai")
    assert len(result) == 4
    assert result.loc[result.tracking_id.eq("A1"), "is_paid"].item()
    assert delivery_kpis(result) == {
        "orders": 4, "paid_orders": 1, "realized_revenue": 100.0,
        "unpaid_orders": 1, "unpaid_value": 80.0, "cancelled_returned": 1,
        "delivered_orders": 2, "pending_orders": 1,
        "delivery_success_rate": pytest.approx(66.6666667),
        "collection_rate": 50.0, "return_rate": pytest.approx(33.3333333),
    }


def test_duplicate_tracking_id_is_rejected():
    source = pd.DataFrame({
        "TRACKING ID": ["A1", "A1"], "CS STATUS": ["Sale closed", "Sale closed"],
        "VALUE": [100, 100], "NUMBER1": [971501234567, 971501234567],
        "AGENT": ["A", "A"], "DELIVERY AGENTS": ["P", "P"],
        "DATE": ["01/08/2026", "01/08/2026"],
    })
    with pytest.raises(ValueError, match="duplicate tracking IDs"):
        normalize_delivery(source, MAPPING, "Asia/Dubai", "Asia/Dubai")
