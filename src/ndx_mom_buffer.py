"""
NDX Momentum Buffered -- pure strategy logic (no network, no broker).

Rules (implemented as specified, not tuned):
  - Universe: current Nasdaq-100 (GOOGL only, GOOG dropped upstream).
  - Signal date t0: the last trading day of the month just ended.
  - Momentum = close at the previous month-end / close 12 month-ends ago - 1
    (12-1 momentum: month-ends t0-1 and t0-12, skipping the latest month).
  - Trend OK = close at t0 > 200-day simple moving average ending at t0.
  - Eligible = at least 13 month-ends and at least 200 trading days of history.
  - Rank eligible names by momentum, highest first.
  - KEEP a current holding if rank <= 20 AND Trend OK.
  - FILL remaining slots with the highest-ranked Trend OK names not currently
    held, until 10 holdings; unfilled slots stay in cash.
  - Every target name is sized to 10% of equity; resize trades under $200 are
    skipped.
"""
import math

import pandas as pd

TARGET_HOLDINGS = 10
TARGET_WEIGHT = 0.10
KEEP_RANK_LIMIT = 20
SMA_DAYS = 200
MIN_MONTH_ENDS = 13
MIN_TRADE_DOLLARS = 200.0


def month_end_trading_days(trading_days):
    """Last trading day of each calendar month in `trading_days`. The caller
    must pass a calendar that extends past the latest month it cares about,
    otherwise the final (incomplete) month's "month-end" is wrong."""
    by_month = {}
    for d in sorted(set(trading_days)):
        by_month[(d.year, d.month)] = d
    return sorted(by_month.values())


def signal_date_for(trading_days, today):
    """The most recent month-end trading day strictly before `today`."""
    candidates = [d for d in month_end_trading_days(trading_days) if d < today]
    if not candidates:
        raise ValueError("calendar has no completed month-end before today")
    return candidates[-1]


def is_first_trading_day_of_month(trading_days, today):
    same_month = [d for d in trading_days if (d.year, d.month) == (today.year, today.month)]
    return bool(same_month) and today == min(same_month)


def compute_signals(closes, month_ends, signal_date):
    """
    closes: DataFrame indexed by trading date (Timestamp), one column per
            ticker, adjusted closes, NaN where the ticker has no bar.
    month_ends: month-end trading days (dates); only those <= signal_date used.
    Returns one row per ticker with momentum, trend, eligibility and rank.
    """
    t0 = pd.Timestamp(signal_date)
    mes = [pd.Timestamp(d) for d in month_ends if pd.Timestamp(d) <= t0]
    if not mes or mes[-1] != t0:
        raise ValueError("signal_date must itself be a month-end trading day")
    closes = closes.loc[:t0]

    rows = []
    for ticker in closes.columns:
        s = closes[ticker].dropna()
        row = {"symbol": ticker, "n_days": len(s), "n_month_ends": 0, "close": None,
               "sma200": None, "trend_ok": False, "momentum": None,
               "eligible": False, "ineligible_reason": None}
        if s.empty:
            row["ineligible_reason"] = "no price data"
            rows.append(row)
            continue

        first = s.index[0]
        row["n_month_ends"] = sum(1 for me in mes if me >= first)
        row["close"] = float(s.iloc[-1])
        if len(s) >= SMA_DAYS:
            row["sma200"] = float(s.iloc[-SMA_DAYS:].mean())
            row["trend_ok"] = row["close"] > row["sma200"]

        reasons = []
        if row["n_month_ends"] < MIN_MONTH_ENDS:
            reasons.append(f"only {row['n_month_ends']} month-ends of history (<{MIN_MONTH_ENDS})")
        if len(s) < SMA_DAYS:
            reasons.append(f"only {len(s)} days of history (<{SMA_DAYS})")
        if reasons:
            row["ineligible_reason"] = "; ".join(reasons)
        else:
            prev_me_close = s.asof(mes[-2])
            base_close = s.asof(mes[-MIN_MONTH_ENDS])
            row["momentum"] = float(prev_me_close / base_close - 1)
            row["eligible"] = True
        rows.append(row)

    df = pd.DataFrame(rows)
    ranked = (df[df["eligible"]]
              .sort_values(["momentum", "symbol"], ascending=[False, True]))
    df["rank"] = pd.NA
    df.loc[ranked.index, "rank"] = range(1, len(ranked) + 1)
    df = pd.concat([df.loc[ranked.index], df[~df["eligible"]].sort_values("symbol")])
    return df.reset_index(drop=True)


def _signal_note(row):
    trend = ("Trend OK" if row["trend_ok"]
             else f"trend fail (close {row['close']:.2f} <= SMA200 {row['sma200']:.2f})"
             if row["sma200"] is not None else "trend unknown")
    return f"rank {int(row['rank'])}, momentum {row['momentum']:+.1%}, {trend}"


def build_portfolio(signals, current_symbols):
    """
    Applies the buffer rule. current_symbols: symbols held in the account now.
    Returns (decisions, target_symbols); decisions has one entry per symbol
    that is held or bought, with action keep/sell/buy and a reason.
    """
    by_symbol = signals.set_index("symbol")
    decisions, keep = [], []

    for sym in sorted(current_symbols):
        if sym not in by_symbol.index:
            decisions.append({"symbol": sym, "action": "sell", "rank": None,
                              "reason": "no longer in the Nasdaq-100 universe"})
            continue
        row = by_symbol.loc[sym]
        if not row["eligible"]:
            decisions.append({"symbol": sym, "action": "sell", "rank": None,
                              "reason": f"ineligible: {row['ineligible_reason']}"})
            continue
        rank = int(row["rank"])
        failures = []
        if rank > KEEP_RANK_LIMIT:
            failures.append(f"rank {rank} > {KEEP_RANK_LIMIT}")
        if not row["trend_ok"]:
            failures.append(f"trend fail (close {row['close']:.2f} <= SMA200 {row['sma200']:.2f})")
        if failures:
            decisions.append({"symbol": sym, "action": "sell", "rank": rank,
                              "reason": "; ".join(failures)})
        else:
            keep.append((rank, sym))

    keep.sort()
    for rank, sym in keep[TARGET_HOLDINGS:]:
        decisions.append({"symbol": sym, "action": "sell", "rank": rank,
                          "reason": f"over the {TARGET_HOLDINGS}-holding cap (rank {rank})"})
    kept = [sym for _, sym in keep[:TARGET_HOLDINGS]]
    for sym in kept:
        decisions.append({"symbol": sym, "action": "keep", "rank": int(by_symbol.loc[sym, "rank"]),
                          "reason": f"still top {KEEP_RANK_LIMIT} and Trend OK: {_signal_note(by_symbol.loc[sym])}"})

    targets = list(kept)
    held = set(current_symbols)
    for _, row in signals[signals["eligible"]].iterrows():
        if len(targets) >= TARGET_HOLDINGS:
            break
        if row["trend_ok"] and row["symbol"] not in held:
            targets.append(row["symbol"])
            decisions.append({"symbol": row["symbol"], "action": "buy", "rank": int(row["rank"]),
                              "reason": f"fills an open slot: {_signal_note(row)}"})

    order = {"keep": 0, "buy": 1, "sell": 2}
    decisions.sort(key=lambda d: (order[d["action"]], d["rank"] if d["rank"] is not None else 10**6, d["symbol"]))
    return decisions, targets


def plan_orders(decisions, targets, positions, equity, min_trade=MIN_TRADE_DOLLARS):
    """
    positions: {symbol: {"qty": float, "market_value": float}} (live account).
    Returns (sells, buys, skipped). Full exits sell the whole position by
    quantity so no residual fraction is left behind; resizes and new names
    use notional dollar amounts. Resize trades under `min_trade` are skipped.
    """
    target_value = round(TARGET_WEIGHT * equity, 2)
    reasons = {d["symbol"]: d["reason"] for d in decisions}
    sells, buys, skipped = [], [], []

    for d in decisions:
        if d["action"] == "sell" and d["symbol"] in positions:
            pos = positions[d["symbol"]]
            sells.append({"symbol": d["symbol"], "side": "sell", "kind": "exit",
                          "qty": pos["qty"], "notional": round(pos["market_value"], 2),
                          "reason": d["reason"]})

    for sym in targets:
        current = positions.get(sym, {}).get("market_value", 0.0)
        delta = round(target_value - current, 2)
        base = {"symbol": sym, "reason": reasons.get(sym, "")}
        if sym not in positions:
            buys.append({**base, "side": "buy", "kind": "new", "notional": target_value, "qty": None})
        elif delta <= -min_trade:
            sells.append({**base, "side": "sell", "kind": "trim", "notional": -delta, "qty": None})
        elif delta >= min_trade:
            buys.append({**base, "side": "buy", "kind": "topup", "notional": delta, "qty": None})
        else:
            skipped.append({**base, "kind": "hold", "delta": delta,
                            "note": f"within ${min_trade:.0f} of target, no trade"})
    return sells, buys, skipped


def scale_buys_to_cash(buys, cash):
    """Never buy on margin: if planned buys exceed cash, scale them down
    proportionally. Returns (scaled_buys, scale_factor)."""
    total = sum(b["notional"] for b in buys)
    if total <= 0 or total <= cash:
        return buys, 1.0
    factor = max(cash, 0.0) / total
    scaled = []
    for b in buys:
        notional = math.floor(b["notional"] * factor * 100) / 100
        if notional >= 1.0:
            scaled.append({**b, "notional": notional})
    return scaled, factor


def turnover(sells, buys, equity):
    gross = sum(o["notional"] for o in sells) + sum(o["notional"] for o in buys)
    return {"gross_traded": round(gross, 2),
            "one_sided": round(gross / (2 * equity), 4) if equity else None}


def sector_counts(symbols, sector_map):
    counts = {}
    for sym in symbols:
        sector = sector_map.get(sym) or "Unknown"
        counts[sector] = counts.get(sector, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))
