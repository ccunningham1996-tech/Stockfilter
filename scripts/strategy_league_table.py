"""
League table of ~200 published stock-selection strategies, compared against
the US stock market across several time periods.

Data:
  - Strategy returns: Open Source Asset Pricing (Chen & Zimmermann), the
    original-paper long-short portfolio for each published signal
    (value, momentum, quality, accruals, ...), monthly, in percent.
  - Market: Kenneth French's Mkt-RF (US market return minus T-bills), monthly.

Why Mkt-RF and not raw SPY: a long-short portfolio is self-financing (the
short side pays for the long side), so its return is already an "excess"
return. Mkt-RF is the like-for-like market comparison.

Outputs (in --out-dir):
  league_table_by_period.csv  one row per signal x period
  league_table_summary.csv    one row per signal, ranked by robustness

Requires (install openassetpricing WITHOUT its deps -- its `wrds` dependency
pins an old pandas that won't build on Python 3.14 and would downgrade the
pandas the live strategies use):
  pip install polars tabulate
  pip install --no-deps openassetpricing
Usage:
  python -m scripts.strategy_league_table
  python -m scripts.strategy_league_table --periods 1990-1999,2000-2009,2010-2019,2020-2099
"""
import argparse
import io
import os
import zipfile

import numpy as np
import pandas as pd
import requests

FRENCH_FACTORS_URL = (
    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
    "F-F_Research_Data_Factors_CSV.zip"
)
DEFAULT_PERIODS = "1990-1999,2000-2009,2010-2019,2020-2099"
MIN_MONTHS = 24


def parse_periods(spec):
    periods = []
    for chunk in spec.split(","):
        start, end = (int(x) for x in chunk.strip().split("-"))
        label = f"{start}-{end}" if end < 2099 else f"{start}-now"
        periods.append((label, start, end))
    return periods


def parse_french_monthly(csv_text):
    """Parses the monthly section of the French factors CSV into a DataFrame
    indexed by month-end date, with Mkt-RF and RF in percent."""
    rows, header, in_monthly = [], None, False
    for line in csv_text.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if not in_monthly:
            if len(parts) > 1 and parts[1] == "Mkt-RF":
                header = parts[1:]
                in_monthly = True
            continue
        if not parts[0].isdigit() or len(parts[0]) != 6:
            break  # annual section / copyright footer starts here
        rows.append([parts[0]] + [float(x) for x in parts[1:]])
    df = pd.DataFrame(rows, columns=["yyyymm"] + header)
    df["date"] = pd.to_datetime(df["yyyymm"], format="%Y%m") + pd.offsets.MonthEnd(0)
    return df.set_index("date")[["Mkt-RF", "RF"]]


def fetch_french_market():
    resp = requests.get(FRENCH_FACTORS_URL, timeout=60)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        text = zf.read(zf.namelist()[0]).decode("latin-1")
    return parse_french_monthly(text)


def _import_openap():
    # openassetpricing imports `wrds` at load time, but only uses it for CRSP
    # downloads we never call. wrds pins an old pandas with no Python 3.14
    # wheel (it tries to compile from source and would downgrade pandas), so
    # install openassetpricing with --no-deps and stub wrds if it's absent.
    try:
        import wrds  # noqa: F401
    except ImportError:
        import sys
        import types
        sys.modules["wrds"] = types.ModuleType("wrds")
    from openassetpricing import OpenAP
    return OpenAP


def fetch_strategy_returns(release=None):
    OpenAP = _import_openap()
    openap = OpenAP(release)
    ports = openap.dl_port("op", "pandas")
    doc = openap.dl_signal_doc("pandas")

    ls = ports[ports["port"] == "LS"].copy()
    ls["date"] = pd.to_datetime(ls["date"]) + pd.offsets.MonthEnd(0)
    wide = ls.pivot_table(index="date", columns="signalname", values="ret")
    return wide, doc


def period_metrics(strat, mkt):
    """strat, mkt: aligned monthly percent-return Series (NaNs dropped jointly)."""
    df = pd.concat([strat, mkt], axis=1, keys=["s", "m"]).dropna()
    n = len(df)
    if n < MIN_MONTHS:
        return None
    s, m = df["s"] / 100, df["m"] / 100

    ann_ret = s.mean() * 12
    ann_vol = s.std(ddof=1) * np.sqrt(12)
    sharpe = ann_ret / ann_vol if ann_vol > 0 else np.nan
    mkt_ann_ret = m.mean() * 12
    mkt_sharpe = mkt_ann_ret / (m.std(ddof=1) * np.sqrt(12))

    wealth = (1 + s).cumprod()
    max_dd = (wealth / wealth.cummax() - 1).min()

    beta = np.cov(s, m, ddof=1)[0, 1] / m.var(ddof=1)
    resid = s - beta * m
    alpha_ann = resid.mean() * 12
    alpha_t = resid.mean() / (resid.std(ddof=1) / np.sqrt(n))

    return {
        "months": n,
        "ann_return": ann_ret,
        "ann_vol": ann_vol,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "market_ann_return": mkt_ann_ret,
        "market_sharpe": mkt_sharpe,
        "sharpe_minus_market": sharpe - mkt_sharpe,
        "beta": beta,
        "capm_alpha_ann": alpha_ann,
        "capm_alpha_t": alpha_t,
    }


def build_by_period(strat_wide, market, periods):
    mkt = market["Mkt-RF"]
    records = []
    for signal in strat_wide.columns:
        for label, start, end in periods:
            mask = (strat_wide.index.year >= start) & (strat_wide.index.year <= end)
            metrics = period_metrics(strat_wide.loc[mask, signal], mkt)
            if metrics:
                records.append({"signal": signal, "period": label, **metrics})
    return pd.DataFrame(records)


def publication_split(strat_wide, doc):
    """Mean annualized return before vs after each signal's publication year,
    when the signal doc provides one. Shows how much of the edge survived."""
    if doc is None or "Acronym" not in doc.columns or "Year" not in doc.columns:
        return pd.DataFrame()
    years = pd.to_numeric(doc.set_index("Acronym")["Year"], errors="coerce").dropna()
    rows = []
    for signal in strat_wide.columns:
        if signal not in years.index:
            continue
        pub = int(years[signal])
        s = strat_wide[signal].dropna() / 100
        pre, post = s[s.index.year <= pub], s[s.index.year > pub]
        if len(pre) >= MIN_MONTHS and len(post) >= MIN_MONTHS:
            rows.append({
                "signal": signal,
                "pub_year": pub,
                "pre_pub_ann_return": pre.mean() * 12,
                "post_pub_ann_return": post.mean() * 12,
            })
    return pd.DataFrame(rows)


def build_summary(by_period, n_periods, pub=None):
    g = by_period.groupby("signal")
    summary = pd.DataFrame({
        "periods_covered": g["period"].nunique(),
        "periods_beating_market_sharpe": g["sharpe_minus_market"].apply(lambda x: int((x > 0).sum())),
        "periods_positive_alpha": g["capm_alpha_ann"].apply(lambda x: int((x > 0).sum())),
        "worst_period_sharpe": g["sharpe"].min(),
        "avg_sharpe": g["sharpe"].mean(),
        "avg_capm_alpha_ann": g["capm_alpha_ann"].mean(),
        "worst_max_drawdown": g["max_drawdown"].min(),
    })
    # Only rank signals with data in every requested period, so a strategy
    # can't look robust just because it has no data for its bad decade.
    summary["all_periods_covered"] = summary["periods_covered"] == n_periods
    if pub is not None and not pub.empty:
        summary = summary.join(pub.set_index("signal"))
    summary = summary.sort_values(
        ["all_periods_covered", "worst_period_sharpe"], ascending=[False, False]
    )
    return summary


def main():
    parser = argparse.ArgumentParser(description="Published-strategy league table vs the US market")
    parser.add_argument("--periods", default=DEFAULT_PERIODS, help=f"comma-separated START-END years (default {DEFAULT_PERIODS})")
    parser.add_argument("--release", default=None, help="Open Source Asset Pricing release, e.g. 202510 (default: latest)")
    parser.add_argument("--out-dir", default="scratch", help="where to write the CSVs")
    parser.add_argument("--top", type=int, default=25, help="rows to print")
    args = parser.parse_args()

    periods = parse_periods(args.periods)
    print("Downloading market returns (Kenneth French)...")
    market = fetch_french_market()
    print("Downloading strategy returns (Open Source Asset Pricing, ~few minutes)...")
    strat_wide, doc = fetch_strategy_returns(args.release)
    print(f"Loaded {strat_wide.shape[1]} strategies, {strat_wide.index.min():%Y-%m} to {strat_wide.index.max():%Y-%m}")

    by_period = build_by_period(strat_wide, market, periods)
    pub = publication_split(strat_wide, doc)
    summary = build_summary(by_period, len(periods), pub)

    os.makedirs(args.out_dir, exist_ok=True)
    by_period_path = os.path.join(args.out_dir, "league_table_by_period.csv")
    summary_path = os.path.join(args.out_dir, "league_table_summary.csv")
    by_period.to_csv(by_period_path, index=False)
    summary.to_csv(summary_path)

    print("\nMarket (Mkt-RF) Sharpe by period:")
    mkt_by_period = by_period.groupby("period")["market_sharpe"].first()
    for label, _, _ in periods:
        if label in mkt_by_period:
            print(f"  {label:<12} {mkt_by_period[label]:.2f}")

    pd.options.display.float_format = "{:.2f}".format
    cols = ["periods_beating_market_sharpe", "periods_positive_alpha", "worst_period_sharpe",
            "avg_sharpe", "avg_capm_alpha_ann", "worst_max_drawdown"]
    if "post_pub_ann_return" in summary.columns:
        cols += ["pre_pub_ann_return", "post_pub_ann_return"]
    ranked = summary[summary["all_periods_covered"]]
    print(f"\nTop {args.top} most robust strategies (ranked by their WORST period's Sharpe, "
          f"{len(ranked)} strategies with data in all {len(periods)} periods):")
    print(ranked[cols].head(args.top).to_string())

    if not pub.empty:
        decay = 1 - pub["post_pub_ann_return"].mean() / pub["pre_pub_ann_return"].mean()
        print(f"\nAverage return decay after publication across {len(pub)} strategies: {decay:.0%}")

    print(f"\nFull results: {by_period_path}, {summary_path}")


if __name__ == "__main__":
    main()
