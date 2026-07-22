import pandas as pd

from historical import classify_vendor, normalize_historical_rows


def _raw(rows=3):
    return pd.DataFrame([[None] * 31 for _ in range(rows)], dtype=object)


def test_mixed_layouts_and_repeat_phone_rows_are_preserved():
    raw = _raw()
    # Legacy country-first row.
    raw.iloc[0, [1, 2, 3, 5, 8, 12, 15, 20]] = [
        "UAE", "ANSAR", "02/01/2025", "+971 50 000 0001", "CLIVE", 130,
        "LEAD", "Won",
    ]
    # Legacy date-first row for the same customer: a legitimate reorder.
    raw.iloc[1, [0, 1, 2, 4, 10, 21, 23]] = [
        "03/02/2025", "ANSAR", "RE-ORDER", "971500000001", "CLIVE", 130,
        "Won",
    ]
    # Current country-first row.
    raw.iloc[2, [0, 1, 2, 3, 5, 11, 22, 24, 29]] = [
        "KSA", "04/03/2025", "RESHMI", "LEAD", "966500000001", "HECTOR",
        160, "Follow-Up", "TRACK-1",
    ]

    data = normalize_historical_rows(raw)

    assert len(data) == 3
    assert data["is_won"].sum() == 2
    assert list(data.loc[data["is_won"], "order_type"]) == ["First-time order", "Repeat order"]


def test_confirmed_vendor_profit_rules_and_fallback():
    assert classify_vendor("Oud Lovers-LPG") == "La Parfume (LPG)"
    assert classify_vendor("Mystery Combo") == "RT Fragrance"
    assert classify_vendor("Peacock Collection") == "RT Fragrance"
    assert classify_vendor("Volga Combo") == "Athiyaf"
    assert classify_vendor("Premium Edition") == "Oud Al Salam"
    assert classify_vendor("Ambre + Oniro") == "Scent Passion"
    assert data.loc[1, "order_id"] == "SOURCE-ROW-2"
    assert data.loc[2, "order_id"] == "TRACK-1"


def test_rows_without_phone_are_retained_with_row_identity():
    raw = _raw(1)
    raw.iloc[0, [0, 1, 2, 3, 24]] = ["UAE", "04/03/2025", "ANSAR", "LEAD", "Won"]

    data = normalize_historical_rows(raw)

    assert len(data) == 1
    assert data.loc[0, "phone"] == ""
    assert data.loc[0, "phone_key"] == "ROW-1"
