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
