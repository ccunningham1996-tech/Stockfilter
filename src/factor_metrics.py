"""
Metric definitions for the factor screeners (src/factor_screener.py).

Each entry lists candidate Finnhub `metric=all` field names to try (in
priority order -- Finnhub has some naming drift between TTM/Annual/Quarterly
variants of the same ratio), a display name, and whether a LOWER raw value
is the "better" (cheaper / less levered) direction for that metric.

These field names reflect Finnhub's documented free-tier basic-financials
fields but have NOT been verified against a live API response from this
environment (no network access here). Before running a full universe scan,
run `python scripts/inspect_finnhub_metrics.py <ticker>` to confirm which
of these candidate keys Finnhub is actually returning, and adjust the
field_candidates lists below if any are missing/renamed.

These are simplified proxies for what MSCI's Enhanced Value / Sector
Neutral Quality indices use -- built from whatever ratios are reliably
available on Finnhub's free tier, not a byte-for-byte methodology replica.
"""

VALUE_METRICS = [
    {
        "name": "price_to_book",
        "field_candidates": ["pbAnnual", "pbQuarterly"],
        "lower_is_better": True,
    },
    {
        "name": "price_to_earnings",
        "field_candidates": ["peExclExtraTTM", "peTTM", "peNormalizedAnnual"],
        "lower_is_better": True,
    },
    {
        "name": "price_to_free_cash_flow",
        "field_candidates": ["pfcfShareTTM", "pfcfShareAnnual"],
        "lower_is_better": True,
    },
]

QUALITY_METRICS = [
    {
        "name": "return_on_equity",
        "field_candidates": ["roeTTM", "roeRfy"],
        "lower_is_better": False,
    },
    {
        "name": "debt_to_equity",
        "field_candidates": ["totalDebt/totalEquityQuarterly", "totalDebt/totalEquityAnnual"],
        "lower_is_better": True,
    },
    {
        "name": "net_profit_margin",
        "field_candidates": ["netProfitMarginTTM", "netProfitMarginAnnual"],
        "lower_is_better": False,
    },
]
