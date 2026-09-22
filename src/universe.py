"""
Real S&P Composite 1500 constituent list (large + mid + small cap),
scraped live from Wikipedia. Used as the scan universe for the factor
screeners (src/factor_screener.py) -- same real-ticker source pattern as
scratch/simulate_composite_1500.py.
"""
from io import StringIO

import pandas as pd
import requests

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
}

_WIKI_PAGES = [
    "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
]


def get_sp1500_tickers():
    """Returns a deduplicated list of real S&P 500/400/600 ticker symbols."""
    tickers = []
    for url in _WIKI_PAGES:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            df = pd.read_html(StringIO(resp.text))[0]
            tickers.extend(df["Symbol"].tolist())
        except Exception as e:
            print(f"Warning: could not fetch tickers from {url}: {e}")

    cleaned = []
    seen = set()
    for t in tickers:
        t_clean = str(t).strip().upper().replace(".", "-")
        if t_clean and t_clean not in seen:
            seen.add(t_clean)
            cleaned.append(t_clean)
    return cleaned


if __name__ == "__main__":
    tickers = get_sp1500_tickers()
    print(f"Loaded {len(tickers)} unique tickers.")
    print(tickers[:20])
