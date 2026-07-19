# Emarath Sales & Marketing Intelligence Dashboard

A Streamlit dashboard that joins DoubleTick lead assignments, Workpex sales and
outbound 3CX calls. It automatically generates the DoubleTick/Meta attribution
report during processing.

## What it reports

- Leads by country, product, vendor, campaign, date and assigned agent
- Orders, conversion rate and product/order mix
- DoubleTick leads found, missing or multiply matched in Workpex
- Called vs never called leads, answered and unanswered calls
- Unanswered call counts and repeated consecutive unanswered-call streaks
- Agent funnel from assignment to call to order
- Time-window, timezone, duplicate-key, unmatched-row and attribution warnings
- Downloadable joined detail, agent report, marketing report and QA exceptions

## Deploy on GitHub + Streamlit Community Cloud

1. Create a **private GitHub repository**. Do not commit API tokens.
2. Upload every file in this folder, preserving `.streamlit/config.toml`.
3. In Streamlit Community Cloud, create an app from `app.py`.
4. Open **App settings → Secrets** and paste the contents of
   `.streamlit/secrets.toml.example`, replacing the placeholder values.
5. Deploy. Upload DoubleTick assignments, Workpex and 3CX exports. The app runs
   DoubleTick/Meta enrichment and creates the attribution output automatically.

Run locally:

```bash
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Required data

The app detects common column names and shows every detected mapping before it
runs. At minimum:

- DoubleTick assignments: customer phone and assigned agent phone only.
- Workpex: customer phone. Order status, agent, product and order ID improve the
  sales analysis.
- 3CX: external/customer number. Call time, agent/extension, status and duration
  improve call analysis.

Defaults are tuned for the supplied exports: DoubleTick uses `Phone number` and
`Agent Phone Number`; Workpex uses `Primary Phone`,
`Created Date`, `Assigned`, `Lead Status`, `Product`, `Actual Amount`; 3CX uses
outbound rows only with `To`, `From`, `Call Time`, `Status`, and `Talking`.

The DoubleTick assignment upload is the authoritative lead population. Every
row is kept, and only its customer number and assigned agent number are used.
Agent names come from the supplied member screenshots. Marketing attribution
comes exclusively from the automatically generated Ad/Meta report. The selected
reporting dates drive the DoubleTick API window and filter Workpex and outbound
3CX.

The report timezone selector does not magically convert naive timestamps. The
source timezone selected for each upload is treated as fact, converted to the
chosen report timezone, and displayed in the QA panel.

## Campaign parsing

For `EG-BAHRAIN-OUDLOVERS - 13/JULY/2026 | Campaign`, the parser returns
`Bahrain` and `Oud Lovers`. Default product-to-vendor rules live in
`config.py`; edit them for new products/vendors. Unknown values remain
`Unmapped` so mistakes are visible.

## Matching rules

- Phone values are normalized to digits.
- Cross-system joins use the exact last 8 digits, as requested.
- A duplicated last-8 key in any source is flagged. Workpex orders are
  de-duplicated by order ID when available.
- Calls and orders outside the selected window are excluded.
- Consecutive unanswered calls are same agent + same lead + unanswered rows
  adjacent in chronological call order, within the configurable minute gap.

## Security

Use a long-lived Meta system-user token with `ads_read` and access only to the
required ad accounts. Put keys in Streamlit Secrets. Never upload or commit a
real `secrets.toml`.
