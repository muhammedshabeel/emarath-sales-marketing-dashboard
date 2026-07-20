from __future__ import annotations
import json, re, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import pandas as pd
import requests
from config import COUNTRY_ALIASES, PRODUCT_ALIASES, PRODUCT_VENDOR

DT_URL = "https://public.doubletick.io/chat-messages"
META_URL = "https://graph.facebook.com"
LOCAL = threading.local()


def _catalog():
    path = Path(__file__).with_name("product_vendor_reference.csv")
    if not path.exists(): return []
    frame = pd.read_csv(path).fillna("")
    return [(str(row.product_name).strip(), str(row.vendor_name).strip()) for row in frame.itertuples()]


PRODUCT_CATALOG = _catalog()


def _session():
    """One pooled HTTP session per worker thread."""
    if not hasattr(LOCAL, "session"):
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        LOCAL.session = session
    return LOCAL.session


def _flatten(value, path=""):
    out = []
    if isinstance(value, dict):
        for key, child in value.items(): out += _flatten(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for i, child in enumerate(value): out += _flatten(child, f"{path}[{i}]")
    elif value is not None: out.append((path, str(value)))
    return out


def _pick(data, names):
    wanted = {x.lower() for x in names}
    for path, value in _flatten(data):
        parts = [x for x in re.split(r"[.\[\]]", path.lower()) if x]
        if parts and parts[-1] in wanted and value.strip(): return value.strip()
    return ""


def _messages(data):
    if isinstance(data, list): return data
    if isinstance(data, dict):
        for key in ("messages", "data", "results", "items"):
            value = data.get(key)
            if isinstance(value, list): return value
            if isinstance(value, dict):
                for inner in ("messages", "data", "results", "items"):
                    if isinstance(value.get(inner), list): return value[inner]
    return []


def _is_ad(message):
    explicit = _pick(message, ["isFromAd", "fromAd", "isAd"]).lower()
    raw = json.dumps(message, ensure_ascii=False).lower()
    return explicit in ("true", "1", "yes") or any(x in raw for x in ("source_id", "sourceid", "ad_id", "adid", "ctwa_clid", '"referral"', "source_url", "thumbnail_url"))


def _incoming(message):
    return _pick(message, ["messageOriginType", "originType", "direction", "senderType"]).lower() in ("customer", "incoming", "inbound", "user")


def _ad_message(messages):
    ads = [message for message in messages if isinstance(message, dict) and _is_ad(message)]
    customer_ads = [message for message in ads if _incoming(message)]
    candidates = customer_ads or ads
    def ts(message):
        try: return float(_pick(message, ["messageTime", "timestamp", "createdAt", "sentAt"]) or "inf")
        except ValueError: return float("inf")
    return min(candidates, key=ts) if candidates else None


def enrich_doubletick(phones, api_key, wabas, start_date, end_date, workers=8, progress=None):
    headers = {"Authorization": api_key, "Accept": "application/json"}
    end_exclusive = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).strftime("%d-%m-%Y")
    start = pd.Timestamp(start_date).strftime("%d-%m-%Y")
    def one(phone):
        errors = []
        for waba in wabas:
            for phone_format in (phone, "+" + phone):
                for attempt in range(4):
                    try:
                        r = _session().get(DT_URL, headers=headers, params={"wabaNumber": waba, "customerNumber": phone_format, "startDate": start, "endDate": end_exclusive}, timeout=60)
                        if r.status_code in (429, 500, 502, 503, 504):
                            time.sleep(min(2 ** (attempt + 1), 20)); continue
                        r.raise_for_status()
                        msgs = _messages(r.json() if r.text.strip() else {})
                        if not msgs: break
                        ad = _ad_message(msgs)
                        if not ad:
                            return {"phone": phone, "waba_number": waba, "phone_format_used": phone_format, "messages_found": len(msgs), "ad_id": "", "campaign_id": "", "adset_id": "", "headline": "", "source_url": "", "ctwa_clid": "", "status": "CHAT_FOUND_NO_AD_ID", "raw_ad_json": "", "error": ""}
                        ad_id = _pick(ad, ["source_id", "sourceId", "ad_id", "adId"])
                        return {"phone": phone, "waba_number": waba, "phone_format_used": phone_format, "messages_found": len(msgs), "ad_id": ad_id, "campaign_id": _pick(ad, ["campaign_id", "campaignId"]), "adset_id": _pick(ad, ["adset_id", "adSetId", "adsetId"]), "headline": _pick(ad, ["headline", "title", "adHeadline"]), "source_url": _pick(ad, ["source_url", "sourceUrl"]), "ctwa_clid": _pick(ad, ["ctwa_clid", "ctwaClid"]), "status": "AD_ID_FOUND" if ad_id else "AD_MESSAGE_FOUND_ID_MISSING", "raw_ad_json": json.dumps(ad, ensure_ascii=False, separators=(",", ":")), "error": ""}
                    except Exception as exc:
                        errors.append(str(exc)[:180])
                        if attempt < 3: time.sleep(min(2 ** (attempt + 1), 20))
        return {"phone": phone, "waba_number": "", "phone_format_used": "", "messages_found": 0, "ad_id": "", "campaign_id": "", "adset_id": "", "headline": "", "source_url": "", "ctwa_clid": "", "status": "API_ERROR" if errors else "NO_CHAT_FOUND", "raw_ad_json": "", "error": " | ".join(errors[:2])}
    unique = list(dict.fromkeys(str(x) for x in phones if len(str(x)) >= 8))
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, p): p for p in unique}
        for i, future in enumerate(as_completed(futures), 1):
            rows.append(future.result())
            if progress: progress(i / max(len(unique), 1))
    return pd.DataFrame(rows)


def enrich_meta(ad_ids, token, workers=8, progress=None):
    def get(object_id, fields):
        r = _session().get(f"{META_URL}/{object_id}", params={"fields": fields, "access_token": token}, timeout=45)
        data = r.json() if r.text.strip() else {}
        if not r.ok: raise RuntimeError(data.get("error", {}).get("message", r.text[:300]))
        return data
    def one(ad_id):
        try:
            ad = get(ad_id, "id,name,account_id,adset_id,campaign_id")
            campaign = get(ad.get("campaign_id"), "id,name") if ad.get("campaign_id") else {}
            adset = get(ad.get("adset_id"), "id,name") if ad.get("adset_id") else {}
            status = "MATCHED_FROM_META" if campaign.get("name") else "META_IDS_FOUND_NAMES_MISSING"
            return {"ad_id_join": ad_id, "meta_ad_name": ad.get("name", ""), "meta_adset_id": str(ad.get("adset_id", "")), "meta_adset_name": adset.get("name", ""), "meta_campaign_id": str(ad.get("campaign_id", "")), "meta_campaign_name": campaign.get("name", ""), "meta_lookup_status": status, "meta_error": ""}
        except Exception as exc:
            return {"ad_id_join": ad_id, "meta_ad_name": "", "meta_adset_id": "", "meta_adset_name": "", "meta_campaign_id": "", "meta_campaign_name": "", "meta_lookup_status": "META_API_ERROR", "meta_error": str(exc)}
    unique = list(dict.fromkeys(str(x).strip() for x in ad_ids if str(x).strip() and str(x).lower() != "nan"))
    rows = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, x): x for x in unique}
        for i, future in enumerate(as_completed(futures), 1):
            rows.append(future.result())
            if progress: progress(i / max(len(unique), 1))
    columns = ["ad_id_join", "meta_ad_name", "meta_adset_id", "meta_adset_name", "meta_campaign_id", "meta_campaign_name", "meta_lookup_status", "meta_error"]
    return pd.DataFrame(rows, columns=columns)


def generate_fixed_zip_report(phones, api_key, meta_token, wabas, start_date, end_date,
                              doubletick_progress=None, meta_progress=None,
                              doubletick_workers=24, meta_workers=16):
    """Run the integrated ZIP workflow with a freshly replaced phone list.

    ``phones`` is rebuilt exclusively from the current DoubleTick all-customer
    upload on every click; no bundled or previous phone_numbers.txt is reused.
    The returned frame is the generated doubletick_ad_id_report used by the
    marketing dashboard and its downloadable workbook.
    """
    phone_list = list(dict.fromkeys(
        re.sub(r"\D", "", str(value)) for value in phones
        if len(re.sub(r"\D", "", str(value))) >= 8
    ))
    report = enrich_doubletick(
        phone_list, api_key, wabas, start_date, end_date,
        workers=doubletick_workers, progress=doubletick_progress,
    )
    meta = enrich_meta(report.ad_id, meta_token, workers=meta_workers, progress=meta_progress)
    report["ad_id_join"] = report.ad_id.astype(str)
    report = report.merge(meta, on="ad_id_join", how="left").drop(columns="ad_id_join")
    meta_cols = ["meta_ad_name", "meta_adset_id", "meta_adset_name", "meta_campaign_id", "meta_campaign_name", "meta_lookup_status", "meta_error"]
    for column in meta_cols:
        if column not in report: report[column] = ""
    report.loc[report.ad_id.eq(""), "meta_lookup_status"] = "NOT_LOOKED_UP"
    report[meta_cols] = report[meta_cols].fillna("")
    evidence_columns = [column for column in ("meta_campaign_name", "meta_adset_name", "meta_ad_name", "headline", "raw_ad_json") if column in report]
    report["classification_text"] = report[evidence_columns].fillna("").astype(str).agg(" | ".join, axis=1)
    return report


def classify_campaign(name):
    text = re.sub(r"[_|\-]+", " ", str(name or "")).upper()
    text = re.sub(r"\s+", " ", text)
    compact = re.sub(r"[^A-Z0-9]", "", text)
    country = "Unmapped"
    for canonical, aliases in COUNTRY_ALIASES.items():
        if any(re.search(rf"(?<![A-Z]){re.escape(a)}(?![A-Z])", text) for a in aliases):
            country = canonical; break
    product = "Unmapped"
    vendor = "Unmapped"
    # The uploaded product/vendor reference is authoritative. Prefer the
    # longest matching product so specific collection names beat short aliases.
    matches = []
    for catalog_product, catalog_vendor in PRODUCT_CATALOG:
        product_key = re.sub(r"[^A-Z0-9]", "", catalog_product.upper())
        if product_key and product_key in compact:
            matches.append((len(product_key), catalog_product.title(), catalog_vendor.title()))
    if matches:
        _, product, vendor = max(matches)
    for canonical, aliases in PRODUCT_ALIASES.items():
        if product != "Unmapped": break
        if any(re.sub(r"[^A-Z0-9]", "", a.upper()) in compact for a in aliases):
            product = canonical; vendor = PRODUCT_VENDOR.get(product, "Unmapped"); break
    # Country is inferred from the generated report's product evidence only
    # when the campaign itself does not explicitly name a country.
    if country == "Unmapped":
        product_country = {
            "AL HUDA": "UAE", "PREMIUM EDITION": "UAE", "DOE": "UAE",
            "LUMINUX": "UAE", "ARCHER": "KSA", "COLLECTION OF MOODS": "KSA",
            "OUD LOVERS": "Bahrain", "ABSOLUTE MOUNTAIN": "KSA",
            "HECTOR": "KSA", "VOLGA": "KSA",
        }
        country = product_country.get(product.upper(), country)
    return country, product, vendor
