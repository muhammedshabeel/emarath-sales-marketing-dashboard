from __future__ import annotations
import re
import pandas as pd
from config import AGENT_PHONE_NAME, ANSWERED_WORDS, WON_WORDS
from data_io import last8, parse_duration_seconds, phone_digits
from enrichment import classify_campaign


WORKPEX_AGENT_ALIASES = {
    "RESHMI EMARATH": "Reshmi Emarath",
    "ANSAR UAE": "Ansar Emarath",
    "ANSHAD UAE": "ANSHAD EMARATH",
    "NIHAD V P": "NIHAD",
    "JAHID N": "JAHID",
    "NEHA P": "NEHA P",
    "RAHIYAD K": "RAHIYAD",
    "SHIHAD K": "SHIHAD",
    "FATHIMA LIYA": "FATHIMA LIYA",
    "ADWAITHA T M": "ADWAITHA T M",
    "NAFIH P": "NAFIH",
    "RANJITH LAL": "RANJITH",
    "SHIBIL P": "SHIBIL",
    "SHAMNA NAJIYA": "SHAMNA NAJIYA",
    "HASNA H": "Hasna",
    "ADNAN S": "ADNAN",
    "MOINUDEEEN M": "Moinudeen",
}


def normalize_workpex_agent(value):
    if pd.isna(value) or not str(value).strip():
        return "Unassigned"
    # Workpex can export the same owner more than once in one cell, e.g.
    # "Ansar UAE, Ansar UAE, Ansar UAE". Collapse repeated labels without
    # changing the number of converted-lead rows.
    labels = [label.strip() for label in str(value).split(",") if label.strip()]
    distinct = list(dict.fromkeys(re.sub(r"\s+", " ", label).upper() for label in labels))
    if not distinct:
        return "Unassigned"
    if len(distinct) > 1:
        return " / ".join(WORKPEX_AGENT_ALIASES.get(label, label.title()) for label in distinct)
    return WORKPEX_AGENT_ALIASES.get(distinct[0], labels[0])


ORDER_COUNTRY_PREFIXES = {
    "UAE": "971",
    "UNITED ARAB EMIRATES": "971",
    "BAHRAIN": "973",
    "QATAR": "974",
    "KSA": "966",
    "SAUDI ARABIA": "966",
}


def _clean_text(value):
    return "" if pd.isna(value) else str(value).strip()


def _crm_phone_series(values, countries):
    digits = phone_digits(values).str.lstrip("0")
    prefixes = countries.str.upper().map(ORDER_COUNTRY_PREFIXES).fillna("")
    local_lengths = prefixes.map({"971": 9, "973": 8, "974": 8, "966": 9})
    needs_prefix = prefixes.ne("") & digits.str.len().eq(local_lengths) & ~digits.str.startswith(("971", "973", "974", "966"))
    return digits.mask(needs_prefix, prefixes + digits)


def normalize_google_crm_orders(df, generated_phones, report_tz="Asia/Dubai"):
    """Normalize live CRM rows and attribute orders by verified phone match.

    Phone membership in the freshly generated DoubleTick report always wins.
    Customer Path is consulted only when neither CRM phone belongs to that
    generated list.
    """
    leads = phone_digits(pd.Series(list(generated_phones), dtype="object"))
    lead_set = set(leads[leads.ne("")])
    countries = df["COUNTRY"].map(_clean_text)
    phone1 = _crm_phone_series(df["NUMBER1"], countries)
    phone2 = _crm_phone_series(df["NUMBER2"], countries)
    matched_phone = phone1.where(phone1.isin(lead_set), phone2.where(phone2.isin(lead_set), ""))
    customer_path = df["Customer Path"].map(_clean_text)
    tracking = df["TRACKING NUM"].map(_clean_text).str.replace(r"\.0$", "", regex=True)
    em_number = df["EM NUMBER"].map(_clean_text)
    row_fallback = pd.Series([f"CRM-{position + 2}" for position in range(len(df))], index=df.index)
    order_id = tracking.where(tracking.ne(""), em_number.where(em_number.ne(""), row_fallback))
    product1 = df["PRODUCT 1"].map(_clean_text)
    product2 = df["PRODUCT 2"].map(_clean_text)
    products = product1.where(product2.eq(""), product1 + ", " + product2).str.strip(", ")
    out = pd.DataFrame({
        "phone_key": matched_phone.where(matched_phone.ne(""), phone1.where(phone1.ne(""), phone2)).str[-8:],
        "sale_time": df["DATE"],
        "sales_agent": df["AGENT"].map(normalize_workpex_agent),
        "order_id": order_id,
        "order_status": df["CS STATUS"].map(_clean_text),
        "order_product": products,
        "order_amount": pd.to_numeric(df["VALUE"], errors="coerce"),
        "is_order": True,
        "order_from_generated_lead": matched_phone.ne(""),
        "matched_lead_phone": matched_phone,
        "crm_phone_1": phone1,
        "crm_phone_2": phone2,
        "customer_path": customer_path,
        "order_source": customer_path.where(customer_path.ne(""), "Other / Unknown").mask(matched_phone.ne(""), "Generated DoubleTick lead"),
        "country": countries,
        "vendor": df["VENDOR"].map(_clean_text),
    })
    if out.empty:
        return pd.DataFrame(columns=[
            "phone_key", "sale_time", "sales_agent", "order_id", "order_status",
            "order_product", "order_amount", "is_order", "order_from_generated_lead",
            "matched_lead_phone", "crm_phone_1", "crm_phone_2", "customer_path",
            "order_source", "country", "vendor",
        ])
    out["sale_time"] = pd.to_datetime(out["sale_time"], errors="coerce", dayfirst=True, format="mixed")
    out["sale_time"] = out["sale_time"].dt.tz_localize(report_tz, ambiguous="NaT", nonexistent="shift_forward")
    out["order_amount"] = pd.to_numeric(out["order_amount"], errors="coerce").fillna(0.0)
    # Tracking number is authoritative; EM number/row fallback keeps countries
    # whose logistics tracking is still blank at one order per CRM row.
    return out.drop_duplicates("order_id", keep="last").reset_index(drop=True)


def parse_time(series, source_tz, report_tz):
    dt = pd.to_datetime(series, errors="coerce", dayfirst=True, format="mixed")
    if getattr(dt.dt, "tz", None) is None:
        dt = dt.dt.tz_localize(source_tz, ambiguous="NaT", nonexistent="shift_forward")
    return dt.dt.tz_convert(report_tz)


def normalize_leads(df, mapping, source_tz, report_tz):
    out = pd.DataFrame(index=df.index)
    lead_digits = phone_digits(df[mapping["phone"]])
    out["lead_phone"] = lead_digits
    out["phone_key"] = lead_digits.str[-8:]
    out["call_key"] = lead_digits.str[-9:]
    gcc_prefixes = ("971", "973", "974", "966", "996")
    out["lead_region"] = lead_digits.map(lambda value: "GCC" if str(value).startswith(gcc_prefixes) else "Other country")
    out["lead_time"] = parse_time(df[mapping["datetime"]], source_tz, report_tz) if mapping.get("datetime") else pd.NaT
    agent_digits = phone_digits(df[mapping["agent_number"]]) if mapping.get("agent_number") else pd.Series("", index=df.index, dtype="string")
    out["agent_number"] = agent_digits
    out["agent"] = agent_digits.map(AGENT_PHONE_NAME).fillna("UNMAPPED AGENT")
    out["ad_id"] = ""
    out["campaign_name"] = ""
    out["lead_row"] = range(1, len(out) + 1)
    return out


def normalize_attribution(df, mapping):
    out = pd.DataFrame(index=df.index)
    out["phone_key"] = last8(df[mapping["phone"]])
    out["attribution_phone"] = df[mapping["phone"]].fillna("").astype(str)
    out["ad_id_attr"] = df[mapping["ad_id"]].fillna("").astype(str).str.replace(r"\.0$", "", regex=True) if mapping.get("ad_id") else ""
    out["campaign_name_attr"] = df[mapping["campaign"]].fillna("").astype(str).str.strip() if mapping.get("campaign") else ""
    out["attribution_status"] = df[mapping["status"]].fillna("").astype(str).str.strip() if mapping.get("status") else ""
    out["classification_text_attr"] = df[mapping["classification"]].fillna("").astype(str).str.strip() if mapping.get("classification") else out["campaign_name_attr"]
    # One customer should have one attribution row. Prefer a row with campaign,
    # then Ad ID, while retaining duplicate counts for QA.
    out["attribution_match_count"] = out.groupby("phone_key").phone_key.transform("size")
    out["_score"] = out.campaign_name_attr.ne("").astype(int) * 2 + out.ad_id_attr.ne("").astype(int)
    return out.sort_values(["phone_key", "_score"], ascending=[True, False]).drop_duplicates("phone_key").drop(columns="_score")


def attach_attribution(leads, attribution):
    out = leads.merge(attribution, on="phone_key", how="left")
    out["ad_id"] = out.ad_id_attr.fillna("")
    out["campaign_name"] = out.campaign_name_attr.fillna("")
    out["classification_text"] = out.classification_text_attr.fillna(out["campaign_name"])
    out["attribution_found"] = out.attribution_phone.notna()
    out["attribution_match_count"] = out.attribution_match_count.fillna(0).astype(int)
    return out


def normalize_sales(df, mapping, source_tz, report_tz):
    out = pd.DataFrame(index=df.index)
    out["phone_key"] = last8(df[mapping["phone"]])
    out["sale_time"] = parse_time(df[mapping["datetime"]], source_tz, report_tz) if mapping.get("datetime") else pd.NaT
    out["sales_agent"] = df[mapping["agent"]].map(normalize_workpex_agent) if mapping.get("agent") else "Unassigned"
    out["order_id"] = df[mapping["order_id"]].fillna("").astype(str).str.strip() if mapping.get("order_id") else ""
    out["order_status"] = df[mapping["status"]].fillna("").astype(str).str.strip() if mapping.get("status") else ""
    out["order_product"] = df[mapping["product"]].fillna("").astype(str).str.strip() if mapping.get("product") else ""
    if mapping.get("amount"):
        amount = df[mapping["amount"]].fillna("").astype(str).str.replace(",", "", regex=False).str.replace(r"[^0-9.\-]", "", regex=True)
        out["order_amount"] = pd.to_numeric(amount, errors="coerce").fillna(0)
    else:
        out["order_amount"] = 0.0
    status = out.order_status.str.lower().str.strip()
    out["is_order"] = status.isin(WON_WORDS) | status.str.contains("won|convert|confirm|ready to dispatch|date shipment|success|sold", regex=True, na=False)
    if not mapping.get("status"): out["is_order"] = True
    return out


def normalize_calls(df, mapping, source_tz, report_tz):
    if mapping.get("direction"):
        direction = df[mapping["direction"]].fillna("").astype(str).str.strip().str.lower()
        df = df[direction.eq("outbound")].copy()
    out = pd.DataFrame(index=df.index)
    call_digits = phone_digits(df[mapping["phone"]])
    out["phone_key"] = call_digits.str[-8:]
    out["call_key"] = call_digits.str[-9:]
    out["call_number"] = call_digits
    # Do not classify calls by their displayed prefix. 3CX may dial the same
    # GCC lead as 050..., 00971..., or +971.... Matching is by last 9 digits.
    out["call_time"] = parse_time(df[mapping["datetime"]], source_tz, report_tz) if mapping.get("datetime") else pd.NaT
    out["call_agent"] = df[mapping["agent"]].fillna("").astype(str).str.strip() if mapping.get("agent") else ""
    out["call_status"] = df[mapping["call_status"]].fillna("").astype(str).str.strip() if mapping.get("call_status") else ""
    out["duration_seconds"] = parse_duration_seconds(df[mapping["duration"]]) if mapping.get("duration") else 0.0
    status = out.call_status.str.lower().str.strip()
    explicitly_unanswered = status.str.contains("unanswered|not answered|no answer|missed|failed|busy|cancel", regex=True, na=False)
    positively_answered = status.isin(ANSWERED_WORDS) | status.str.contains(r"\b(?:answered|connected|completed|talked|success)\b", regex=True, na=False)
    out["answered"] = (~explicitly_unanswered) & (positively_answered | out.duration_seconds.gt(0))
    return out


def agent_directory_frame():
    return pd.DataFrame([{"agent_number": number, "agent": name} for number, name in AGENT_PHONE_NAME.items()]).sort_values("agent")


def _window(df, col, start, end, report_tz):
    if col not in df or df[col].isna().all(): return df.copy()
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end)
    if lo.tzinfo is None: lo = lo.tz_localize(report_tz)
    else: lo = lo.tz_convert(report_tz)
    if hi.tzinfo is None: hi = hi.tz_localize(report_tz)
    else: hi = hi.tz_convert(report_tz)
    return df[df[col].ge(lo) & df[col].lt(hi)].copy()


def build_analysis(leads, sales, calls, start, end, report_tz, streak_gap_minutes=15, filter_calls=False):
    # The DoubleTick export is authoritative: every uploaded row is an assigned
    # lead for the chosen reporting period. Never discard it using historical
    # customer timestamps such as First message received at.
    leads = leads.copy()
    # The uploaded Workpex conversion report is authoritative for orders.
    # It is not silently trimmed again; users can upload the matching reporting
    # period and retain legitimate repeat orders as separate rows.
    sales = sales.copy()
    # The window is user-selectable. When disabled, trust the full uploaded
    # 3CX export. Never discard local-format destinations before last-9 matching.
    calls = _window(calls, "call_time", start, end, report_tz) if filter_calls else calls.copy()
    sales_presence = sales.groupby("phone_key", dropna=False).size().rename("workpex_match_count").reset_index()
    orders = sales[sales.is_order].copy()
    if orders.order_id.ne("").any():
        orders = orders.sort_values("sale_time").drop_duplicates("order_id", keep="last")
    order_agg = orders.groupby("phone_key", dropna=False).agg(order_count=("is_order", "size"), order_value=("order_amount", "sum"), order_products=("order_product", lambda x: ", ".join(sorted(set(v for v in x if v))))).reset_index()
    call_agg = calls.groupby("call_key", dropna=False).agg(call_count=("call_key", "size"), answered_calls=("answered", "sum"), talk_seconds=("duration_seconds", "sum"), first_call_time=("call_time", "min"), last_call_time=("call_time", "max")).reset_index()
    call_agg["unanswered_calls"] = call_agg.call_count - call_agg.answered_calls
    if not calls.empty:
        seq = calls.sort_values(["call_agent", "call_time"]).copy()
        prev_same = seq.call_key.eq(seq.call_key.shift()) & seq.call_agent.eq(seq.call_agent.shift())
        gap = seq.call_time.sub(seq.call_time.shift()).dt.total_seconds().div(60)
        seq["consecutive_unanswered_retry"] = (~seq.answered) & (~seq.answered.shift(fill_value=True)) & prev_same & gap.le(streak_gap_minutes)
        retry = seq.groupby("call_key").consecutive_unanswered_retry.sum().rename("consecutive_unanswered_retries").reset_index()
        call_agg = call_agg.merge(retry, on="call_key", how="left")
    joined = leads.merge(sales_presence, on="phone_key", how="left").merge(order_agg, on="phone_key", how="left").merge(call_agg, on="call_key", how="left")
    for col in ("order_count", "order_value", "call_count", "answered_calls", "unanswered_calls", "talk_seconds", "consecutive_unanswered_retries"):
        if col not in joined: joined[col] = 0
        joined[col] = joined[col].fillna(0)
    joined["converted"] = joined.order_count.gt(0)
    joined["workpex_match_count"] = joined.workpex_match_count.fillna(0).astype(int)
    joined["workpex_found"] = joined.workpex_match_count.gt(0)
    joined["workpex_reconciliation"] = "FOUND IN WORKPEX"
    joined.loc[~joined.workpex_found, "workpex_reconciliation"] = "NO MATCHING WORKPEX CONVERSION"
    joined.loc[joined.workpex_match_count.gt(1), "workpex_reconciliation"] = "MULTIPLE WORKPEX ORDERS"
    joined["called"] = joined.lead_region.eq("GCC") & joined.call_count.gt(0)
    joined["answered_any"] = joined.lead_region.eq("GCC") & joined.answered_calls.gt(0)
    if joined.lead_time.notna().any():
        joined["speed_to_first_call_minutes"] = (joined.first_call_time - joined.lead_time).dt.total_seconds().div(60)
    else:
        joined["speed_to_first_call_minutes"] = float("nan")
    classification_source = joined["classification_text"] if "classification_text" in joined else joined.campaign_name
    classifications = classification_source.map(classify_campaign).apply(pd.Series)
    classifications.columns = ["country", "product", "vendor"]
    joined[["country", "product", "vendor"]] = classifications
    joined["lead_date"] = joined.lead_time.dt.date
    if joined["lead_date"].isna().all():
        joined["lead_date"] = pd.Timestamp(start).date()
    return joined, orders, calls


def grouped(joined, field):
    if joined.empty: return pd.DataFrame()
    out = joined.groupby(field, dropna=False).agg(leads=("lead_row", "size"), missing_from_workpex=("workpex_found", lambda x: (~x).sum()), called_leads=("called", "sum"), answered_leads=("answered_any", "sum"), orders=("order_count", "sum"), revenue=("order_value", "sum"), total_calls=("call_count", "sum"), unanswered_calls=("unanswered_calls", "sum"), repeat_unanswered=("consecutive_unanswered_retries", "sum"), avg_first_call_min=("speed_to_first_call_minutes", "mean")).reset_index()
    out["conversion_rate"] = out.orders.div(out.leads).mul(100)
    out["call_coverage"] = out.called_leads.div(out.leads).mul(100)
    out["answer_rate"] = out.answered_leads.div(out.called_leads.replace(0, pd.NA)).mul(100)
    for col in ("conversion_rate", "call_coverage", "answer_rate"):
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0).astype(float)
    return out.sort_values("leads", ascending=False)


def qa_report(leads, sales, calls, source_ranges):
    rows = []
    for source, df, key in (("DoubleTick", leads, "phone_key"), ("Workpex", sales, "phone_key"), ("3CX", calls, "call_key")):
        invalid = int(df[key].str.len().lt(8).sum()) if key in df else len(df)
        collisions = int(df[df[key].ne("")].groupby(key).size().gt(1).sum()) if key in df else 0
        rows.append({"check": f"{source}: invalid phone keys", "value": invalid, "severity": "High" if invalid else "OK"})
        rows.append({"check": f"{source}: repeated last-8 keys", "value": collisions, "severity": "Review" if collisions else "OK"})
    rows.append({"check": "Source handling", "value": "DoubleTick upload + Workpex upload + 3CX upload + Meta API", "severity": "OK"})
    lead_keys = set(leads.phone_key); sales_keys = set(sales.phone_key)
    gcc_leads = leads[leads.lead_region.eq("GCC")] if "lead_region" in leads else leads
    lead_call_keys = set(gcc_leads.call_key)
    call_keys = set(calls.call_key)
    rows.append({"check": "DoubleTick leads without a Workpex conversion", "value": len(lead_keys - sales_keys), "severity": "Review" if lead_keys - sales_keys else "OK"})
    rows.append({"check": "DoubleTick leads absent from outbound GCC 3CX (last 9 digits)", "value": len(lead_call_keys - call_keys), "severity": "Review" if lead_call_keys - call_keys else "OK"})
    rows.append({"check": "Workpex orders from other sources", "value": int((~sales.order_from_generated_lead).sum()) if "order_from_generated_lead" in sales else len(sales_keys - lead_keys), "severity": "Review"})
    rows.append({"check": "GCC 3CX numbers unmatched to a lead", "value": len(call_keys - lead_call_keys), "severity": "Review"})
    rows.append({"check": "Other-country DoubleTick leads shown separately", "value": int(leads.lead_region.eq("Other country").sum()) if "lead_region" in leads else 0, "severity": "Review"})
    return pd.DataFrame(rows)
