"""
Current Nasdaq-100 constituents (with sector) for NDX Momentum Buffered.

Fetched from Wikipedia's Nasdaq-100 components table at every rebalance and
cached to JSON with a date stamp. If the fetch or parse fails, the last cached
list is used and a warning is logged. GOOG is dropped so Alphabet is only
held once (as GOOGL).
"""
import json
import logging
import os
from datetime import datetime, timezone
from io import StringIO

import pandas as pd
import requests

WIKI_URL = "https://en.wikipedia.org/wiki/Nasdaq-100"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}
DROPPED_SYMBOLS = {"GOOG"}
MIN_PLAUSIBLE, MAX_PLAUSIBLE = 90, 110

log = logging.getLogger(__name__)


def parse_components(tables):
    """Picks the constituents table out of pd.read_html output and returns
    (members, sector_source) where members is a list of dicts with symbol,
    company and sector. Raises ValueError if no plausible table is found."""
    for t in tables:
        cols = {str(c).strip(): c for c in t.columns}
        sym_col = next((cols[c] for c in ("Ticker", "Symbol") if c in cols), None)
        if sym_col is None or not (MIN_PLAUSIBLE <= len(t) <= MAX_PLAUSIBLE + 5):
            continue
        sector_col = next((cols[c] for c in cols if "GICS Sector" in c), None)
        sector_source = "GICS Sector"
        if sector_col is None:
            sector_col = next((cols[c] for c in cols if "Sector" in c or "Industry" in c), None)
            sector_source = str(sector_col) if sector_col is not None else "none"
        company_col = next((cols[c] for c in ("Company", "Security", "Name") if c in cols), None)

        members = []
        for _, r in t.iterrows():
            sym = str(r[sym_col]).strip().upper()
            if not sym or sym == "NAN" or sym in DROPPED_SYMBOLS:
                continue
            members.append({
                "symbol": sym,
                "company": str(r[company_col]).strip() if company_col is not None else "",
                "sector": str(r[sector_col]).strip() if sector_col is not None else "Unknown",
            })
        if MIN_PLAUSIBLE - len(DROPPED_SYMBOLS) <= len(members) <= MAX_PLAUSIBLE:
            return members, sector_source
    raise ValueError("no plausible Nasdaq-100 components table found")


def _fetch_live():
    resp = requests.get(WIKI_URL, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    return parse_components(pd.read_html(StringIO(resp.text)))


def load_universe(cache_path, fetch=_fetch_live):
    """Returns a dict: members, sector_source, source ('live'|'cache'),
    as_of (ISO timestamp of the list), warning (str|None)."""
    try:
        members, sector_source = fetch()
        payload = {"as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "source_url": WIKI_URL, "sector_source": sector_source,
                   "members": members}
        os.makedirs(os.path.dirname(cache_path) or ".", exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, cache_path)
        return {**payload, "source": "live", "warning": None}
    except Exception as e:
        if not os.path.exists(cache_path):
            raise RuntimeError(f"Nasdaq-100 fetch failed ({e}) and no cached list exists at {cache_path}") from e
        with open(cache_path) as f:
            payload = json.load(f)
        warning = (f"Nasdaq-100 fetch failed ({e}); using cached list from {payload['as_of']} "
                   f"({len(payload['members'])} names)")
        log.warning(warning)
        return {**payload, "source": "cache", "warning": warning}
