from __future__ import annotations
import io, re, zipfile
from pathlib import Path
import pandas as pd


SYNONYMS = {
    "phone": ["phone", "mobile", "customer number", "contact number", "whatsapp number", "lead number", "number", "to"],
    "agent": ["assigned agent", "agent name", "assigned to", "owner", "sales agent", "agent", "extension name", "assigned", "from"],
    "agent_number": ["assigned user number", "agent phone", "agent number", "extension", "extension number"],
    "datetime": ["last ctwa lead at", "call time", "created date", "assigned date", "assigned time", "created at", "lead date", "date time", "datetime", "date"],
    "ad_id": ["ad id", "ad_id", "source id", "source_id"],
    "campaign": ["campaign name", "meta campaign name", "meta_campaign_name", "campaign"],
    "attribution_status": ["meta lookup status", "meta_lookup_status", "dt status", "status"],
    "order_id": ["order id", "order number", "awb", "reference", "invoice"],
    "status": ["order status", "conversion status", "lead status", "status", "disposition"],
    "product": ["product name", "ordered product", "product", "item"],
    "amount": ["order value", "sale amount", "amount", "total", "price"],
    "call_status": ["call status", "result", "answered", "status", "disposition"],
    "duration": ["talking", "talk duration", "call duration", "duration", "talk time"],
    "direction": ["direction", "call direction"],
}


def norm_col(value):
    return re.sub(r"[^a-z0-9]+", " ", str(value).strip().lower()).strip()


def detect_column(columns, role):
    normalized = {c: norm_col(c) for c in columns}
    choices = SYNONYMS.get(role, [])
    for target in choices:
        exact = [c for c, n in normalized.items() if n == target]
        if exact:
            return exact[0]
    for target in choices:
        partial = [c for c, n in normalized.items() if target in n]
        if partial:
            return partial[0]
    return None


def _read_one(name, raw):
    suffix = Path(name).suffix.lower()
    if suffix == ".csv":
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                return {Path(name).stem: pd.read_csv(io.BytesIO(raw), encoding=encoding, sep=None, engine="python")}
            except (UnicodeDecodeError, pd.errors.ParserError):
                continue
        raise ValueError(f"Could not decode {name}")
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(io.BytesIO(raw), sheet_name=None)
    return {}


def read_upload(uploaded):
    if uploaded is None:
        return {}
    raw = uploaded.getvalue()
    if uploaded.name.lower().endswith(".zip"):
        frames = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            for member in zf.namelist():
                if member.endswith("/") or Path(member).suffix.lower() not in (".csv", ".xlsx", ".xls"):
                    continue
                for sheet, frame in _read_one(Path(member).name, zf.read(member)).items():
                    key = f"{Path(member).stem} — {sheet}"
                    frames[key] = frame
        if not frames:
            raise ValueError(f"{uploaded.name} contains no CSV/XLSX report")
        return frames
    return _read_one(uploaded.name, raw)


def choose_best_sheet(frames, role):
    if not frames:
        return "", pd.DataFrame()
    scores = {}
    wanted = ["phone", "datetime"]
    if role == "workpex": wanted += ["status", "order_id", "product"]
    if role == "3cx": wanted += ["call_status", "duration", "agent"]
    if role == "doubletick": wanted += ["agent", "ad_id", "campaign"]
    if role == "attribution": wanted += ["ad_id", "campaign"]
    for name, df in frames.items():
        scores[name] = sum(detect_column(df.columns, item) is not None for item in wanted) + min(len(df), 10_000) / 100_000
    selected = max(scores, key=scores.get)
    return selected, frames[selected]


def phone_digits(series):
    # CSV columns containing phone numbers and blanks are often inferred as
    # floats. Converting those values directly to strings adds `.0`, which
    # would incorrectly turn 918089262612 into 9180892626120. Normalize
    # integer-like numeric values first, including scientific notation.
    values = series.astype("string").fillna("").str.strip()
    numeric = pd.to_numeric(values, errors="coerce")
    integer_like = numeric.notna() & numeric.mod(1).eq(0)
    values.loc[integer_like] = numeric.loc[integer_like].map(lambda value: f"{value:.0f}")
    return values.str.replace(r"\D", "", regex=True).str.removeprefix("00")


def last8(series):
    return phone_digits(series).str[-8:]


def parse_duration_seconds(series):
    def one(value):
        if pd.isna(value): return 0.0
        if isinstance(value, (int, float)): return float(value)
        text = str(value).strip()
        if re.fullmatch(r"\d+(\.\d+)?", text): return float(text)
        parts = text.split(":")
        try:
            nums = [float(x) for x in parts]
            if len(nums) == 3: return nums[0] * 3600 + nums[1] * 60 + nums[2]
            if len(nums) == 2: return nums[0] * 60 + nums[1]
        except ValueError: pass
        return 0.0
    return series.map(one)
