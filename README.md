# ⚡ Top Analyst Momentum Screener

A quantitative stock screening system that identifies high-conviction analyst upgrade signals confirmed by volume spikes and analyst track records. The system runs on structured API responses using Alpaca and Finnhub (free tier), with an SQLite backend and an interactive Streamlit dashboard.

---

## 🛠️ Setup Instructions

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
2. **Configure Credentials**:
   Copy `.env.example` to `.env` and fill in your credentials:
   ```ini
   FINNHUB_API_KEY=your_key
   ALPACA_API_KEY=your_key
   ALPACA_SECRET_KEY=your_secret
   ```
3. **Seed Win-Rate History**:
   Run the historical data seeder to download and evaluate past analyst ratings from the last 12 months:
   ```bash
   python scripts/seed_history.py
   ```
4. **Launch Dashboard**:
   Start the Streamlit web server:
   ```bash
   streamlit run app.py
   ```

---

## ⚡ Streamlit Operational Buttons

*   **Run Scraper Now**: Triggers `scraper.run_scraper()`. Queries the Finnhub API for today's upgrade announcements and fetches their current quotes via Alpaca's IEX feed, logging them into `daily_signals` as `pending`.
*   **Run Filter Engine**: Triggers `filter.run_filter()`. Processes yesterday's `pending` signals, evaluating them against the 60% win-rate and 2.5x volume-spike filters, and logs passing trades.
*   **Evaluate Outcomes**: Triggers `outcome_tracker.evaluate_pending_ratings()`. Scans the historical database for ratings that have reached their 252-day evaluation window and scores them relative to SPY.

---

## 🎯 Win-Rate Definition

An analyst's rating is defined as **Won** if, after **252 trading days** (approximately 1 calendar year) from the signal date:
1. The stock's return is positive (`stock_return_pct > 0`).
2. The stock's return outperformed the SPY index return over that same window (`stock_return_pct > spy_return_pct`).

Otherwise, the rating is marked as **Lost**. If ticker data is missing or delisted at the evaluation date, the status is set to **Withdrawn** and excluded from all denominators.

---

## ⚠️ Important System Notes

### Win-Rate Filter Trial Period
The win-rate filter requires a minimum sample size of **$n \ge 10$ closed ratings** for an analyst/firm in order to score them. During the initial **~3 months of operation**, most analysts will have insufficient data. 
*   **Bypassing Rule**: When $n < 10$, the win-rate check is automatically bypassed with a log warning, and the signal proceeds to the volume filter. Signals are only rejected if they fail the volume check.

### Alpaca IEX Volume Feed
Volume figures are calculated using **Alpaca's IEX feed** (which represents approximately **2.5% of total US market volume**). 
*   **Recalibration Warning**: Because the IEX volume is a subset, the default 2.5x volume spike threshold should be monitored and recalibrated after **3–4 weeks** of forward data collection.

---

## 📊 Value & Quality Factor Screeners

Two additional strategies, mechanically different from the event-driven momentum screener above: they score the entire real S&P Composite 1500 (large/mid/small cap, scraped live from Wikipedia — `src/universe.py`) on fundamental ratios pulled from Finnhub, and rebalance a basket of the top-ranked names, similar to how factor ETFs like VLUE/QUAL are built.

Each strategy is designed to run against **its own Alpaca paper account** and **its own SQLite DB**, so the two forward tests stay fully isolated from each other and from the momentum screener's account/DB.

### Setup

1. Create two additional Alpaca **paper** accounts (one for value, one for quality).
2. Copy `.env.value.example` → `.env.value` and `.env.quality.example` → `.env.quality`, filling in each account's own key pair. Both also need `FINNHUB_API_KEY` and each sets its own `SCREENER_DB_PATH` (`data/value.db` / `data/quality.db`) so they never share signal/holdings history with each other or with `data/screener.db`.
3. **Verify Finnhub field names before a full run** — the exact field names in `src/factor_metrics.py` are documented Finnhub fields but haven't been checked against a live response. Run:
   ```bash
   python scripts/inspect_finnhub_metrics.py AAPL
   ```
   and cross-check the printed keys against `field_candidates` in `src/factor_metrics.py` before trusting the composite scores.
4. Run each screener (a full S&P 1500 scan takes roughly 30–60 minutes on Finnhub's free-tier rate limit; use `--universe-limit 50` for a quick smoke test first):
   ```bash
   python scripts/run_value_screen.py --universe-limit 50 --force
   python scripts/run_quality_screen.py --universe-limit 50 --force
   ```

### How stocks are picked

*   **Value** (`VALUE_METRICS`): Price/Book, Price/Earnings, Price/Free-Cash-Flow — all cheap-is-better.
*   **Quality** (`QUALITY_METRICS`): Return on Equity, Debt/Equity, Net Profit Margin — profitable-and-unlevered-is-better.

Each metric is **z-scored within its GICS sector** (not against the whole market), so the screen finds the relatively best-run company in every sector rather than just piling into whichever sector structurally scores highest. The sector z-scores are averaged into one composite score per ticker, and the top `--top-n` (default 40) tickers become the target basket.

### Rebalancing

Both scripts are safe to invoke daily (e.g. from `scheduler.py` or cron) — they check `factor_scores` for the last rebalance date and no-op unless at least 80 days have passed (`--force` bypasses this). On a real rebalance, they diff the new target basket against currently open `factor_holdings`: positions that fell out of the basket are sold first to free up cash, then the resulting cash is split equally across newly-added names.
