"""
myNSE200 / backtest.py
------------------------
Replays the Apex200 technical entry/exit rules against your already-
downloaded historical price data, to give REAL win rate / CAGR / holding
period numbers instead of guesses.

HONEST LIMITATION — read this before trusting the output:
Your fundamentals table (promoter holding, FII%, pledge) only stores the
CURRENT snapshot, not history. Applying today's fundamentals to filter
trades from years ago would be look-ahead bias (cheating) — the backtest
would look better than the strategy could have actually performed, because
it would "know" today's clean balance sheet applied to a 2016 trade.

So this backtest tests ONLY the price/technical side of Apex200:
  - score from momentum + trend + quality (growth is neutral — no history)
  - SMA20/50 trigger + volume surge trigger
  - ATR/swing-low stop-loss, 3:1 target, 20-day max holding
It does NOT apply the ★ Recommended fundamental filter. Real forward
results will likely differ from this backtest, in either direction —
past performance never guarantees future performance, tested or not.

Usage:
    python3 backtest.py
    python3 backtest.py --start 2018-01-01 --capital 500000
"""

import argparse
import logging
from datetime import datetime

import numpy as np
import pandas as pd

import config
import db

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backtest")


def load_market_choppiness(period=20):
    """Kaufman's Efficiency Ratio on the Nifty 50 index — measures whether
    the market is actually TRENDING or just whipsawing sideways, which the
    bull/bear regime filter can't tell apart (a choppy market can still be
    technically "above its 200-day average" while whipsawing hard day to
    day). ER close to 1 = strong clean trend; ER close to 0 = lots of
    volatility with no real net direction — the exact condition that
    breakout/momentum entries get faked out by repeatedly.

    ER(n) = |net move over n days| / (sum of each day's absolute move)

    Returns a per-date Series of the efficiency ratio, or None if no index
    data is available."""
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT date, close FROM candle WHERE symbol = 'NIFTY50' ORDER BY date", conn
    )
    conn.close()
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    net_move = (df["close"] - df["close"].shift(period)).abs()
    daily_moves = df["close"].diff().abs()
    path_length = daily_moves.rolling(period).sum()
    df["efficiency_ratio"] = net_move / path_length.replace(0, np.nan)
    return df.set_index("date")["efficiency_ratio"]


def load_sector_map():
    """Loads symbol -> sector mapping from data/sector_map.csv, for the
    sector concentration cap. This is a MANUALLY COMPILED, best-effort
    mapping (not fetched from an official source) — spot-check it against
    a real source if precision matters to you; sector classifications can
    also shift over time (e.g. a conglomerate diversifying). Symbols not
    found in the map are simply not sector-capped (treated as unknown)."""
    if not (config.DATA_DIR / "sector_map.csv").exists():
        return None
    df = pd.read_csv(config.DATA_DIR / "sector_map.csv")
    return dict(zip(df["symbol"], df["sector"]))


def load_market_regime(sma_period=200):
    """Loads the Nifty 50 INDEX (not a tradeable stock — fetched separately
    via data_fetch.py --fetch-index) and returns a per-date boolean Series:
    True = index close is above its own N-day SMA (bull regime), False =
    below (bear/weak regime). Used to test whether only trading when the
    broader market itself is healthy improves results — the strategy has
    never had this context before; it's evaluated each stock in isolation.

    Returns None if no index data has been fetched yet."""
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT date, close FROM candle WHERE symbol = 'NIFTY50' ORDER BY date", conn
    )
    conn.close()
    if df.empty:
        return None
    df["date"] = pd.to_datetime(df["date"])
    df["sma"] = df["close"].rolling(sma_period).mean()
    df["bull"] = df["close"] > df["sma"]
    return df.set_index("date")["bull"]


def load_all_candles(restrict_to_current_universe=True):
    """Loads candle data. By default, restricts to symbols currently in your
    universe file (data/nifty200.csv) — this matters because the database
    can accumulate leftover symbols from a PREVIOUS universe (e.g. if you
    tested Nifty 500 at some point, then switched back to Nifty 200), and
    without this filter, backtests would silently be testing against a
    larger, contaminated symbol set than you intended. Pass False only if
    you deliberately want to test against everything ever downloaded."""
    conn = db.get_conn()
    df = pd.read_sql_query("SELECT symbol, date, open, high, low, close, volume FROM candle", conn)
    conn.close()
    df["date"] = pd.to_datetime(df["date"])

    if restrict_to_current_universe:
        try:
            import data_fetch
            current_universe = set(data_fetch.load_universe_snapshot())
            before = df["symbol"].nunique()
            df = df[df["symbol"].isin(current_universe)]
            after = df["symbol"].nunique()
            if before != after:
                print(f"  (Filtered to current universe: {after} of {before} symbols in the "
                      f"database are in data/nifty200.csv — the rest are leftover from a "
                      f"previous universe setting and were excluded.)")
        except Exception as e:
            print(f"  Warning: could not filter to current universe ({e}) — using all symbols in DB.")

    return df


def clean_extreme_moves(df, threshold_pct=45, revert_check_days=3, revert_tolerance_pct=25):
    """Neutralizes implausible single-day price jumps before they can corrupt
    indicators or the equity curve. Found necessary after a -98% 3-week
    "drawdown" in one backtest fold turned out to trace back to several
    unrelated large-caps (NESTLEIND, ABBOTINDIA, WHIRLPOOL) all showing wild
    price anomalies on the exact same date — a data provider glitch, not a
    real corporate action or market crash. auto_adjust=True in data_fetch.py
    fixes genuine, correctly-recorded splits; it can't fix a wrong data
    point or a bad adjustment factor. This is a defensive backstop.

    Two different anomaly shapes need two different fixes:
      - ONE-DAY BAD TICK (price jumps, then comes back close to its prior
        level within a few days): flatten just that one day.
      - PERMANENT LEVEL SHIFT (price jumps and STAYS at the new level —
        e.g. an unadjusted split, or a bad adjustment factor): rescale
        every subsequent price for that symbol by the inverse ratio, to
        restore a continuous series. Flattening only the jump day would
        just delay the same discontinuity by one day, not fix it."""
    out = []
    total_flattened = 0
    total_rescaled = 0
    for symbol, g in df.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        close = g["close"]
        pct_change = close.pct_change() * 100
        suspect_idxs = pct_change[pct_change.abs() >= threshold_pct].index.tolist()

        for idx in suspect_idxs:
            if idx >= len(close) or idx < 1:
                continue
            pre_jump_level = close.iloc[idx - 1]
            post_jump_level = close.iloc[idx]
            check_end = min(idx + revert_check_days, len(close) - 1)
            later_level = close.iloc[check_end]
            reverted = abs(later_level / pre_jump_level - 1) * 100 <= revert_tolerance_pct

            if reverted:
                # One-day bad tick: flatten just this day
                for col in ["open", "high", "low", "close"]:
                    g.loc[idx, col] = g[col].iloc[idx - 1]
                total_flattened += 1
            else:
                # Permanent level shift: rescale everything from here onward
                ratio = pre_jump_level / post_jump_level
                for col in ["open", "high", "low", "close"]:
                    g.loc[idx:, col] = g.loc[idx:, col] * ratio
                total_rescaled += 1
                # Recompute close/pct_change for any further checks on this symbol
                close = g["close"]

        out.append(g)

    result = pd.concat(out, ignore_index=True) if out else df
    if total_flattened or total_rescaled:
        print(f"  Data cleaning: {total_flattened} one-day bad tick(s) flattened, "
              f"{total_rescaled} permanent level shift(s) rescaled for continuity "
              f"(likely unadjusted splits or bad adjustment factors). "
              f"See find_price_anomalies.py to inspect the originals.")
    return result


def compute_indicators(df):
    """Per-symbol rolling indicators, computed for EVERY historical day
    (not just the latest, like scoring.py does for live signals)."""
    df = clean_extreme_moves(df)
    out = []
    for symbol, g in df.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        close, high, low, vol = g["close"], g["high"], g["low"], g["volume"]

        g["sma20"] = close.rolling(20).mean()
        g["sma50"] = close.rolling(50).mean()
        g["sma200"] = close.rolling(200).mean()
        g["ret_1m"] = close.pct_change(21) * 100
        g["ret_3m"] = close.pct_change(63) * 100
        g["ret_6m"] = close.pct_change(126) * 100

        daily_ret = close.pct_change()
        g["ann_vol"] = daily_ret.rolling(60).std() * np.sqrt(252) * 100
        g["ann_ret"] = daily_ret.rolling(60).mean() * 252 * 100
        g["sharpe_like"] = g["ann_ret"] / g["ann_vol"].replace(0, np.nan)

        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        g["atr14"] = tr.rolling(config.ATR_PERIOD).mean()

        g["vol_avg20"] = vol.rolling(20).mean()
        g["vol_surge_ratio"] = vol / g["vol_avg20"].replace(0, np.nan)
        g["swing_low10"] = low.rolling(config.SWING_LOW_LOOKBACK_DAYS).min()

        g["above_sma20"] = close > g["sma20"]
        g["above_sma50"] = close > g["sma50"]

        out.append(g)
    return pd.concat(out, ignore_index=True)


def _zscore(s):
    s = pd.Series(s, dtype="float64")
    std = s.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series([0.0] * len(s), index=s.index)
    return ((s - s.mean()) / std).fillna(0.0)


def generate_daily_signals(ind_df, start_date=None, end_date=None, fundamentals_filter=None,
                            min_score_percentile=None, min_volume_surge=None, regime_filter=None,
                            choppiness_filter=None, min_efficiency_ratio=0.3):
    """For each date, cross-sectionally score the universe and flag which
    symbols trigger a BUY signal that day (signal generated on that day's
    close, entry would be the NEXT trading day's open — same no-look-ahead
    rule as the live strategy).

    fundamentals_filter: optional set of symbols currently passing the
    ★ Recommended fundamental filter (promoter/FII+DII/pledge). If given,
    only these symbols can ever qualify for a signal. IMPORTANT CAVEAT:
    this applies TODAY's fundamentals to every historical date, which is
    look-ahead bias — a stock disqualified today (say, high pledge in 2026)
    would be excluded from a 2015 trade too, even though its 2015
    fundamentals may have been fine, or vice versa. There is no historical
    fundamentals data available to do this correctly. Treat any result
    using this filter as an optimistic approximation, not a clean backtest.

    min_score_percentile / min_volume_surge: override config values, for
    testing whether the entry trigger's selectivity affects results — this
    was never swept alongside stop/target, unlike everything else tuned in
    this project.
    """
    score_pct = min_score_percentile if min_score_percentile is not None else config.MIN_SCORE_PERCENTILE
    vol_surge = min_volume_surge if min_volume_surge is not None else config.MIN_VOLUME_SURGE_RATIO

    ind_df = ind_df.dropna(subset=["sma50", "atr14", "vol_avg20"])
    if start_date:
        ind_df = ind_df[ind_df["date"] >= pd.Timestamp(start_date)]
    if end_date:
        ind_df = ind_df[ind_df["date"] <= pd.Timestamp(end_date)]
    if fundamentals_filter is not None:
        ind_df = ind_df[ind_df["symbol"].isin(fundamentals_filter)]

    signals = []
    min_required = max(5, int(0.1 * ind_df["symbol"].nunique())) if len(ind_df) else 20
    regime_skipped_days = 0
    choppy_skipped_days = 0
    for date, day_df in ind_df.groupby("date"):
        if len(day_df) < min_required:
            continue  # not enough of the (possibly fundamentals-filtered) universe has data yet

        if regime_filter is not None:
            is_bull = regime_filter.get(date)
            if is_bull is not None and not is_bull:
                regime_skipped_days += 1
                continue  # market itself is in a weak/bear regime — no new entries today

        if choppiness_filter is not None:
            er = choppiness_filter.get(date)
            if er is not None and not pd.isna(er) and er < min_efficiency_ratio:
                choppy_skipped_days += 1
                continue  # market is whipsawing with no real trend — sit out

        mom_z = (_zscore(day_df["ret_1m"]) + _zscore(day_df["ret_3m"]) + _zscore(day_df["ret_6m"])) / 3.0
        trend_raw = day_df["above_sma50"].astype(int) + (day_df["close"] > day_df["sma200"]).fillna(False).astype(int)
        trend_z = _zscore(trend_raw)
        qual_z = _zscore(day_df["sharpe_like"])
        # growth_z intentionally 0 (neutral) — no historical fundamentals available

        overall = (
            100
            + config.SCORE_WEIGHTS["momentum"] * mom_z * 10
            + config.SCORE_WEIGHTS["trend"] * trend_z * 10
            + config.SCORE_WEIGHTS["quality"] * qual_z * 10
        )
        cutoff = overall.quantile(score_pct / 100.0)

        trigger = (
            day_df["above_sma20"].fillna(False)
            & day_df["above_sma50"].fillna(False)
            & (day_df["vol_surge_ratio"] >= vol_surge)
        )
        qualifies = (overall >= cutoff) & trigger

        day_signals = day_df[qualifies].copy()
        day_signals["overall_score"] = overall[qualifies]
        signals.append(day_signals)

    if not signals:
        return pd.DataFrame()
    if regime_filter is not None and regime_skipped_days:
        print(f"  Regime filter: skipped new entries on {regime_skipped_days} day(s) where the "
              f"Nifty 50 itself was below its own trend — no trades taken during weak markets.")
    if choppiness_filter is not None and choppy_skipped_days:
        print(f"  Choppiness filter: skipped new entries on {choppy_skipped_days} day(s) where "
              f"the Nifty was whipsawing with no clear trend (efficiency ratio < {min_efficiency_ratio}).")
    return pd.concat(signals, ignore_index=True)


def simulate_trades(ind_df, signals_df, capital, max_positions, use_trailing_stop=False,
                     atr_multiplier=None, reward_risk_ratio=None, risk_pct=None, compound_sizing=False,
                     max_per_order=None, max_per_order_pct=None, max_holding_days=None,
                     slippage_pct=0.0, sector_map=None, max_per_sector_pct=None):
    """Walk forward day by day, opening/closing positions per Apex200's
    stop/target/max-holding rules, respecting position count and capital.

    slippage_pct: every backtest before this defaulted to 0 — perfect fills
    at the exact stop/target price. Real fills are worse: buying pushes the
    price up slightly, selling into a stop pushes it down slightly. This
    worsens every entry (pay slippage_pct% more) and every exit (receive
    slippage_pct% less), a simple but honest approximation of execution
    cost. 0.1-0.3% is a reasonable starting range to test.

    sector_map / max_per_sector_pct: if given, no single sector's OPEN
    positions can exceed max_per_sector_pct of current equity — prevents
    silent concentration (e.g. 8 of your 20 positions all being banks).

    use_trailing_stop: if True, once a position has moved
    TRAIL_STOP_AFTER_R_MULTIPLE x its original risk in your favor, the
    stop is moved up to breakeven (entry price) for the rest of the trade
    — matches config.TRAIL_STOP_AFTER_R_MULTIPLE's original intent.

    atr_multiplier / reward_risk_ratio / risk_pct: override config values,
    for quick A/B testing without editing config.py each time.

    compound_sizing: if True, each trade risks risk_pct of your CURRENT
    equity (cash + open positions' value) at the time of entry, instead of
    your original starting capital. This matches how position sizing
    actually behaves in real trading as an account grows or shrinks.
    It amplifies whatever the underlying trade sequence already does —
    if winners tend to cluster early, compounding helps; if losses cluster
    early, compounding hurts more than fixed sizing would. It is NOT
    guaranteed to raise CAGR — test it and look at the actual number,
    don't assume the direction. Off by default so existing results stay
    comparable to what you've already seen.

    max_per_order / max_per_order_pct: max_per_order is a flat rupee cap per
    position. max_per_order_pct
    instead caps each position at that % of CURRENT equity (e.g. 20 = no
    single position over 20% of your account) — the safer way to let
    compounding work without letting one trade dominate the portfolio.
    If both are given, whichever is smaller for a given trade wins.
    """
    atr_mult = atr_multiplier if atr_multiplier is not None else config.ATR_STOP_MULTIPLIER
    rrr = reward_risk_ratio if reward_risk_ratio is not None else config.REWARD_RISK_RATIO
    risk_percent = risk_pct if risk_pct is not None else config.RISK_PER_TRADE_PCT
    hold_limit = max_holding_days if max_holding_days is not None else config.MAX_HOLDING_DAYS
    # Order cap: either a flat rupee amount (max_per_order) or a percentage of
    # CURRENT equity (max_per_order_pct) — the percentage version grows with
    # your account instead of staying frozen at a fixed rupee number, which
    # keeps diversification intact even as compounding grows position sizes.
    # Defaults to config.MAX_PER_ORDER_PCT_OF_EQUITY if neither is given.
    flat_cap = max_per_order
    default_pct = max_per_order_pct if max_per_order_pct is not None else (
        config.MAX_PER_ORDER_PCT_OF_EQUITY if max_per_order is None else None
    )

    if signals_df.empty:
        return pd.DataFrame(), pd.DataFrame({"date": sorted(ind_df["date"].unique()), "equity": capital})

    ind_by_symbol = {s: g.sort_values("date").reset_index(drop=True) for s, g in ind_df.groupby("symbol")}
    signals_by_date = {d: g for d, g in signals_df.groupby("date")}

    all_dates = sorted(ind_df["date"].unique())
    open_positions = {}   # symbol -> position dict
    trades = []
    cash = capital
    equity_curve = []

    # Forward-filled price lookup: if a symbol has no data row for a given
    # day (trading halt, exchange data gap — real, especially in older
    # years), fall back to its LAST KNOWN price instead of silently valuing
    # the position at ₹0. Found necessary after a fold's mark-to-market
    # drawdown stayed stuck at the exact same 3-week window through three
    # different data-jump fixes — the real cause was missing rows, not bad
    # values, which price-jump detection could never catch.
    close_lookup = {}
    for s, g in ind_by_symbol.items():
        series = g.set_index("date")["close"].reindex(all_dates).ffill()
        close_lookup[s] = series

    def current_equity(today):
        if not open_positions:
            return cash
        open_value = 0.0
        for s, p in open_positions.items():
            price = close_lookup[s].get(today)
            if price is not None and not pd.isna(price):
                open_value += price * p["quantity"]
            else:
                # No price ever seen yet for this symbol as of today — fall
                # back to entry price rather than zero (should be rare).
                open_value += p["entry_price"] * p["quantity"]
        return cash + open_value

    for i, today in enumerate(all_dates):
        # 1. Check exits for open positions
        for symbol in list(open_positions.keys()):
            pos = open_positions[symbol]
            sdf = ind_by_symbol[symbol]
            row = sdf[sdf["date"] == today]
            if row.empty:
                continue
            row = row.iloc[0]

            # Trailing stop: once price has moved TRAIL_STOP_AFTER_R_MULTIPLE x
            # the original risk in our favor, lock the stop at breakeven.
            if use_trailing_stop and not pos["trailing_active"]:
                trigger_price = pos["entry_price"] + config.TRAIL_STOP_AFTER_R_MULTIPLE * pos["original_risk"]
                if row["high"] >= trigger_price:
                    pos["stop_loss"] = max(pos["stop_loss"], pos["entry_price"])  # move to breakeven
                    pos["trailing_active"] = True

            exit_price = None
            exit_reason = None

            if row["low"] <= pos["stop_loss"]:
                exit_price = pos["stop_loss"] * (1 - slippage_pct / 100)  # sell for slightly less
                exit_reason = "trailing_stop" if pos["trailing_active"] else "stop_loss"
            elif row["high"] >= pos["target_price"]:
                exit_price = pos["target_price"] * (1 - slippage_pct / 100)  # sell for slightly less
                exit_reason = "target"
            elif pos["days_held"] >= hold_limit:
                exit_price = row["close"] * (1 - slippage_pct / 100)
                exit_reason = "max_holding"

            if exit_price is not None:
                pnl = (exit_price - pos["entry_price"]) * pos["quantity"]
                cash += pos["entry_price"] * pos["quantity"] + pnl
                trades.append({
                    "symbol": symbol, "entry_date": pos["entry_date"], "exit_date": today,
                    "entry_price": pos["entry_price"], "exit_price": exit_price,
                    "quantity": pos["quantity"], "pnl": pnl,
                    "return_pct": (exit_price / pos["entry_price"] - 1) * 100,
                    "holding_days": pos["days_held"], "exit_reason": exit_reason,
                })
                del open_positions[symbol]
            else:
                pos["days_held"] += 1

        # 2. Open new positions from yesterday's signals (entry = today's open, no look-ahead)
        if i > 0:
            prev_date = all_dates[i - 1]
            todays_candidates = signals_by_date.get(prev_date, pd.DataFrame())
            sizing_base = current_equity(today) if compound_sizing else capital
            for _, sig in todays_candidates.iterrows():
                symbol = sig["symbol"]
                if symbol in open_positions or len(open_positions) >= max_positions:
                    continue
                sdf = ind_by_symbol[symbol]
                row = sdf[sdf["date"] == today]
                if row.empty:
                    continue
                row = row.iloc[0]
                entry = row["open"] * (1 + slippage_pct / 100)  # buy for slightly more
                atr_stop = entry - atr_mult * sig["atr14"]
                stop = max(atr_stop, sig["swing_low10"]) if config.USE_SWING_LOW_STOP_IF_TIGHTER else atr_stop
                if pd.isna(stop) or stop >= entry:
                    continue
                risk_per_share = entry - stop
                target = entry + rrr * risk_per_share

                risk_capital = sizing_base * (risk_percent / 100.0)
                pct_cap = sizing_base * (default_pct / 100.0) if default_pct is not None else None
                caps = [c for c in [flat_cap, pct_cap] if c is not None]
                per_order_cap = min(caps) if caps else float("inf")
                qty = int(min(risk_capital // risk_per_share, per_order_cap // entry))
                cost = qty * entry
                if qty <= 0 or cost > cash:
                    continue

                # Sector concentration cap: would this push the sector's
                # total open exposure over the limit? Unknown-sector
                # symbols are never blocked (no data to check against).
                if sector_map is not None and max_per_sector_pct is not None:
                    sym_sector = sector_map.get(symbol)
                    if sym_sector is not None:
                        sector_value = sum(
                            p["entry_price"] * p["quantity"]
                            for s, p in open_positions.items()
                            if sector_map.get(s) == sym_sector
                        )
                        sector_cap_value = sizing_base * (max_per_sector_pct / 100.0)
                        if sector_value + cost > sector_cap_value:
                            continue

                cash -= cost
                open_positions[symbol] = {
                    "entry_date": today, "entry_price": entry, "stop_loss": stop,
                    "target_price": target, "quantity": qty, "days_held": 0,
                    "original_risk": risk_per_share, "trailing_active": False,
                }

        # 3. Mark-to-market equity for the day
        equity_curve.append({"date": today, "equity": current_equity(today)})

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_curve)
    return trades_df, equity_df


def load_recommended_symbols():
    """Symbols currently passing the ★ Recommended fundamental filter, using
    TODAY's fundamentals snapshot (see generate_daily_signals docstring for
    why this is an approximation, not a point-in-time-correct backtest)."""
    conn = db.get_conn()
    fundamentals = pd.read_sql_query("SELECT * FROM fundamentals", conn)
    conn.close()

    def is_recommended(row):
        promoter = row.get("promoter_holding")
        fii = row.get("fii_holding")
        dii = row.get("dii_holding")
        pledge_known_raw = row.get("pledge_known", 0)
        pledged = row.get("pledged_pct")
        if promoter is None or pd.isna(promoter) or promoter >= config.MAX_PROMOTER_HOLDING:
            return False
        fii_val = 0 if (fii is None or pd.isna(fii)) else fii
        dii_val = 0 if (dii is None or pd.isna(dii)) else dii
        if (fii_val + dii_val) < config.MIN_FII_PLUS_DII:
            return False
        pledge_known = bool(pledge_known_raw) if not (pledge_known_raw is None or pd.isna(pledge_known_raw)) else False
        if not config.ALLOW_UNKNOWN_PLEDGE and not pledge_known:
            return False
        if pledged is None or pd.isna(pledged) or pledged > config.MAX_PLEDGE_PCT:
            return False
        return True

    recommended = fundamentals[fundamentals.apply(is_recommended, axis=1)]["symbol"]
    return set(recommended)


def compute_post_tax_stats(trades_df, equity_df, capital, tax_rate_pct=20.8):
    """Approximates post-tax CAGR under Indian STCG rules (Section 111A):
    since this strategy's holding periods are almost always under 12
    months, virtually every gain is a Short-Term Capital Gain, taxed at a
    flat rate (20% + ~4% cess = 20.8% effective, current as of FY 2025-26).
    Losses offset gains within the same year (simplified to calendar year
    here — real filing uses financial year April-March, close enough for
    an estimate). No carry-forward across years is modeled.

    SIMPLIFICATION: tax is deducted once from the final equity rather than
    paid periodically through the year (real STCG requires quarterly
    advance tax). Paying tax earlier would leave slightly less capital to
    compound sooner, so this approximation is mildly OPTIMISTIC — real
    post-tax CAGR is likely a bit lower than what this shows. This is an
    estimate for planning purposes, not a tax return — consult a CA for
    actual liability, especially with STT/brokerage also in the mix.
    """
    if trades_df.empty:
        return None

    trades_df = trades_df.copy()
    trades_df["exit_year"] = pd.to_datetime(trades_df["exit_date"]).dt.year
    yearly_net = trades_df.groupby("exit_year")["pnl"].sum()
    total_tax = sum(max(0, net) * (tax_rate_pct / 100.0) for net in yearly_net)

    final_equity_pretax = equity_df["equity"].iloc[-1]
    final_equity_posttax = final_equity_pretax - total_tax
    years = (equity_df["date"].iloc[-1] - equity_df["date"].iloc[0]).days / 365.25
    cagr_posttax = ((final_equity_posttax / capital) ** (1 / years) - 1) * 100 if years > 0 else float("nan")

    return {
        "total_tax_paid_est": round(total_tax, 2),
        "final_equity_post_tax_est": round(final_equity_posttax, 2),
        "cagr_post_tax_est_pct": round(cagr_posttax, 2),
    }


def compute_stats(trades_df, equity_df, capital):
    if trades_df.empty:
        return {"error": "No trades were generated — check your data covers enough history."}

    wins = trades_df[trades_df["pnl"] > 0]
    losses = trades_df[trades_df["pnl"] <= 0]
    win_rate = len(wins) / len(trades_df) * 100

    years = (equity_df["date"].iloc[-1] - equity_df["date"].iloc[0]).days / 365.25
    final_equity = equity_df["equity"].iloc[-1]
    cagr = ((final_equity / capital) ** (1 / years) - 1) * 100 if years > 0 else float("nan")

    running_max = equity_df["equity"].cummax()
    drawdown = (equity_df["equity"] - running_max) / running_max * 100
    max_drawdown = drawdown.min()
    trough_idx = drawdown.idxmin()
    trough_date = equity_df["date"].iloc[trough_idx]
    peak_date = equity_df.loc[:trough_idx][equity_df.loc[:trough_idx, "equity"] == running_max.iloc[trough_idx]]["date"].iloc[-1]

    exit_counts = trades_df["exit_reason"].value_counts().to_dict()
    worst_trade = trades_df.loc[trades_df["return_pct"].idxmin()]
    best_trade = trades_df.loc[trades_df["return_pct"].idxmax()]

    return {
        "total_trades": len(trades_df),
        "win_rate_pct": round(win_rate, 2),
        "avg_return_per_trade_pct": round(trades_df["return_pct"].mean(), 2),
        "avg_win_pct": round(wins["return_pct"].mean(), 2) if len(wins) else None,
        "avg_loss_pct": round(losses["return_pct"].mean(), 2) if len(losses) else None,
        "worst_single_trade_pct": f"{round(worst_trade['return_pct'], 2)}% ({worst_trade['symbol']}, exited {pd.Timestamp(worst_trade['exit_date']).date()}, reason: {worst_trade['exit_reason']})",
        "best_single_trade_pct": f"{round(best_trade['return_pct'], 2)}% ({best_trade['symbol']}, exited {pd.Timestamp(best_trade['exit_date']).date()})",
        "worst_drawdown_period": f"{pd.Timestamp(peak_date).date()} (peak) to {pd.Timestamp(trough_date).date()} (trough)",
        "avg_holding_days": round(trades_df["holding_days"].mean(), 1),
        "target_hit_count": exit_counts.get("target", 0),
        "stop_loss_hit_count": exit_counts.get("stop_loss", 0),
        "trailing_stop_hit_count": exit_counts.get("trailing_stop", 0),
        "max_holding_days_hit_count": exit_counts.get("max_holding", 0),
        "years_tested": round(years, 1),
        "starting_capital": capital,
        "final_equity": round(final_equity, 2),
        "cagr_pct": round(cagr, 2),
        "max_drawdown_pct": round(max_drawdown, 2),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, default=None, help="YYYY-MM-DD, default: all available history")
    parser.add_argument("--end", type=str, default=None, help="YYYY-MM-DD, default: all available history")
    parser.add_argument("--capital", type=float, default=None, help="default: config.ACCOUNT_CAPITAL")
    parser.add_argument("--trailing-stop", action="store_true",
                         help="Enable breakeven trailing stop after config.TRAIL_STOP_AFTER_R_MULTIPLE x risk")
    parser.add_argument("--atr-multiplier", type=float, default=None,
                         help="Override config.ATR_STOP_MULTIPLIER for this run (e.g. 2.0 for a wider stop)")
    parser.add_argument("--reward-risk", type=float, default=None,
                         help="Override config.REWARD_RISK_RATIO for this run (e.g. 2.0)")
    parser.add_argument("--risk-pct", type=float, default=None,
                         help="Override config.RISK_PER_TRADE_PCT for this run (e.g. 1.5 for 1.5%%)")
    parser.add_argument("--compound", action="store_true",
                         help="Size each trade off CURRENT equity instead of fixed starting capital "
                              "(more realistic, generally higher CAGR AND higher absolute drawdown)")
    parser.add_argument("--max-per-order", type=float, default=None,
                         help="Flat rupee cap per position for this run (default: uses "
                              "config.MAX_PER_ORDER_PCT_OF_EQUITY instead, see --max-per-order-pct)")
    parser.add_argument("--max-per-order-pct", type=float, default=None,
                         help="Cap each position at this %% of CURRENT equity instead of a flat "
                              "rupee amount (e.g. 20 for 20%%) — safer with --compound than a flat cap")
    parser.add_argument("--apply-fundamentals", action="store_true",
                         help="Only allow symbols currently passing the ★ Recommended fundamental "
                              "filter. CAVEAT: uses TODAY's fundamentals for the whole history — "
                              "this is look-ahead bias, an optimistic approximation, not a clean test")
    parser.add_argument("--out-of-sample", action="store_true",
                         help="Split your data at --split-date: everything BEFORE is shown as the "
                              "'training' result, everything AT/AFTER is the real out-of-sample test. "
                              "If out-of-sample results are much worse, the settings are likely overfit.")
    parser.add_argument("--split-date", type=str, default=None,
                         help="YYYY-MM-DD split point for --out-of-sample (default: 75%% through your data)")
    parser.add_argument("--apply-tax", action="store_true",
                         help="Also show an estimated post-tax CAGR, assuming Indian STCG rules "
                              "(20%% + cess, since holding periods here are almost always <12 months)")
    parser.add_argument("--tax-rate", type=float, default=20.8,
                         help="Effective STCG tax rate %% to use (default 20.8 = 20%% + ~4%% cess)")
    parser.add_argument("--n-folds", type=int, default=None,
                         help="Split your full history into N contiguous chunks and run the SAME "
                              "settings on each independently — shows whether performance is stable "
                              "across several different periods, not just one lucky/unlucky split.")
    parser.add_argument("--min-score-percentile", type=float, default=None,
                         help="Override config.MIN_SCORE_PERCENTILE — how selective the score cutoff "
                              "is (e.g. 90 = only top 10%%, more selective than the default 80)")
    parser.add_argument("--min-volume-surge", type=float, default=None,
                         help="Override config.MIN_VOLUME_SURGE_RATIO — how strong the volume "
                              "confirmation must be (default 1.5x the 20-day average)")
    parser.add_argument("--max-holding-days", type=int, default=None,
                         help="Override config.MAX_HOLDING_DAYS — how long to hold before a forced "
                              "time exit if neither target nor stop is hit (default 20)")
    parser.add_argument("--regime-filter", action="store_true",
                         help="Only take NEW trades when the Nifty 50 index itself is above its own "
                              "200-day average (a healthy broader market) — sits out during weak/bear "
                              "regimes. Requires index data: run data_fetch.py --fetch-index first.")
    parser.add_argument("--regime-sma", type=int, default=200,
                         help="SMA period for the regime filter (default 200 days)")
    parser.add_argument("--choppiness-filter", action="store_true",
                         help="ALSO sit out when the Nifty is whipsawing with no clear trend "
                              "(Kaufman's Efficiency Ratio below --min-efficiency-ratio), even if "
                              "technically 'bullish' by the regime filter's 200-day average.")
    parser.add_argument("--choppiness-period", type=int, default=20,
                         help="Lookback window (days) for the efficiency ratio calc (default 20)")
    parser.add_argument("--min-efficiency-ratio", type=float, default=0.3,
                         help="Below this efficiency ratio (0-1), the market is considered too "
                              "choppy/directionless to trade (default 0.3)")
    parser.add_argument("--yearly", action="store_true",
                         help="Break results down by CALENDAR YEAR instead of N equal folds — "
                              "easier to read year-by-year, and lines up with how you'd naturally "
                              "think about annual performance.")
    parser.add_argument("--rolling-out-of-sample", type=int, default=None,
                         help="Repeats the out-of-sample test at MULTIPLE points instead of just "
                              "one — value is the test-window size in years (e.g. 3). At each of "
                              "several points, trains on everything before it and tests clean on "
                              "the N years after. More rigorous than a single train/test split — "
                              "one lucky/unlucky split can't carry the whole result.")
    parser.add_argument("--slippage-pct", type=float, default=0.0,
                         help="Simulate imperfect fills: worsens every entry/exit price by this %% "
                              "(e.g. 0.2 = entries cost 0.2%% more, exits get 0.2%% less) — every "
                              "backtest before this defaulted to 0, i.e. perfect fills at the exact "
                              "stop/target price, which is optimistic. Try 0.1-0.3 for a realistic "
                              "small-cap-heavy portfolio, more for less liquid names.")
    parser.add_argument("--max-per-sector-pct", type=float, default=None,
                         help="Cap total exposure to any single sector at this %% of equity (e.g. "
                              "40). Requires data/sector_map.csv — see find_price_anomalies.py-style "
                              "docs. Without this flag, positions can concentrate in one sector "
                              "with no limit, same as every backtest run before this feature.")
    args = parser.parse_args()

    capital = args.capital or config.ACCOUNT_CAPITAL

    print("Loading historical candles from your database...")
    raw = load_all_candles()
    if raw.empty:
        print("No candle data found. Run `python3 main.py --full-history` first.")
        raise SystemExit(1)

    print(f"Computing indicators for {raw['symbol'].nunique()} symbols...")
    ind_df = compute_indicators(raw)

    fundamentals_filter = None
    if args.apply_fundamentals:
        fundamentals_filter = load_recommended_symbols()
        print(f"★ Recommended filter (TODAY's fundamentals, look-ahead bias — see docstring): "
              f"{len(fundamentals_filter)} symbols currently qualify.")

    regime_filter = None
    if args.regime_filter:
        regime_filter = load_market_regime(sma_period=args.regime_sma)
        if regime_filter is None:
            print("  WARNING: --regime-filter requested but no index data found. Run:")
            print("    python3 data_fetch.py --fetch-index")
            print("  Continuing WITHOUT the regime filter for this run.")
        else:
            bull_days = int(regime_filter.sum())
            print(f"  Regime filter loaded: {bull_days} of {len(regime_filter)} days were a "
                  f"bull regime (Nifty 50 above its {args.regime_sma}-day average).")
            data_start, data_end = ind_df["date"].min(), ind_df["date"].max()
            regime_start, regime_end = regime_filter.index.min(), regime_filter.index.max()
            if regime_start > data_start:
                gap_years = (regime_start - data_start).days / 365.25
                print(f"  WARNING: your stock data starts {data_start.date()}, but index data only "
                      f"starts {regime_start.date()} ({gap_years:.1f} years later). Dates before "
                      f"{regime_start.date()} get NO regime filtering — results for early folds may "
                      f"not reflect the filter at all. Re-run data_fetch.py --fetch-index if this "
                      f"gap is unexpected.")

    choppiness_filter = None
    if args.choppiness_filter:
        choppiness_filter = load_market_choppiness(period=args.choppiness_period)
        if choppiness_filter is None:
            print("  WARNING: --choppiness-filter requested but no index data found. Run:")
            print("    python3 data_fetch.py --fetch-index")
            print("  Continuing WITHOUT the choppiness filter for this run.")
        else:
            choppy_days = int((choppiness_filter < args.min_efficiency_ratio).sum())
            print(f"  Choppiness filter loaded: {choppy_days} of {len(choppiness_filter)} days "
                  f"were choppy/directionless (efficiency ratio < {args.min_efficiency_ratio}).")

    label = []
    if args.trailing_stop:
        label.append("trailing-stop ON")
    if args.atr_multiplier:
        label.append(f"ATR x{args.atr_multiplier}")
    if args.reward_risk:
        label.append(f"RRR {args.reward_risk}:1")
    if args.compound:
        label.append("compound ON")
    if args.apply_fundamentals:
        label.append("fundamentals filter (approx.)")
    if regime_filter is not None:
        label.append("regime filter ON")
    if choppiness_filter is not None:
        label.append("choppiness filter ON")

    sector_map = None
    if args.max_per_sector_pct is not None:
        sector_map = load_sector_map()
        if sector_map is None:
            print("  WARNING: --max-per-sector-pct requested but data/sector_map.csv not found. "
                  "Continuing WITHOUT the sector cap for this run.")
        else:
            print(f"  Sector cap loaded: {len(sector_map)} symbols mapped, "
                  f"max {args.max_per_sector_pct}% of equity per sector.")
            label.append(f"sector cap {args.max_per_sector_pct}%")
    if args.slippage_pct:
        label.append(f"slippage {args.slippage_pct}%")

    label_str = f" [{', '.join(label)}]" if label else " [default config settings]"

    def run_one(start_date, end_date, tag):
        signals_df = generate_daily_signals(ind_df, start_date=start_date, end_date=end_date,
                                             fundamentals_filter=fundamentals_filter,
                                             min_score_percentile=args.min_score_percentile,
                                             min_volume_surge=args.min_volume_surge,
                                             regime_filter=regime_filter,
                                             choppiness_filter=choppiness_filter,
                                             min_efficiency_ratio=args.min_efficiency_ratio)
        sim_ind = ind_df[
            (ind_df["date"] >= (pd.Timestamp(start_date) if start_date else ind_df["date"].min()))
            & (ind_df["date"] <= (pd.Timestamp(end_date) if end_date else ind_df["date"].max()))
        ]
        trades_df, equity_df = simulate_trades(
            sim_ind, signals_df, capital, config.MAX_OPEN_POSITIONS,
            use_trailing_stop=args.trailing_stop, atr_multiplier=args.atr_multiplier,
            reward_risk_ratio=args.reward_risk, risk_pct=args.risk_pct,
            compound_sizing=args.compound, max_per_order=args.max_per_order,
            max_per_order_pct=args.max_per_order_pct, max_holding_days=args.max_holding_days,
            slippage_pct=args.slippage_pct, sector_map=sector_map,
            max_per_sector_pct=args.max_per_sector_pct,
        )
        stats = compute_stats(trades_df, equity_df, capital)
        print("\n" + "=" * 60)
        print(f" {tag}{label_str}")
        print("=" * 60)
        for k, v in stats.items():
            print(f"  {k}: {v}")
        if args.apply_tax and "error" not in stats:
            tax_stats = compute_post_tax_stats(trades_df, equity_df, capital, args.tax_rate)
            print(f"  --- estimated post-tax (STCG @ {args.tax_rate}%, see docstring for caveats) ---")
            for k, v in tax_stats.items():
                print(f"  {k}: {v}")
        print("=" * 60)
        return trades_df, equity_df, stats

    if args.yearly:
        all_dates = sorted(ind_df["date"].unique())
        years = sorted(set(d.year for d in all_dates))
        print(f"\nBreaking your full history down by calendar year ({years[0]}-{years[-1]}):")
        year_stats = []
        for yr in years:
            yr_start = pd.Timestamp(f"{yr}-01-01")
            yr_end = pd.Timestamp(f"{yr}-12-31")
            yr_trades, yr_equity, stats = run_one(yr_start, yr_end, f"YEAR {yr}")
            if "error" not in stats:
                stats["year"] = yr
                year_stats.append(stats)
                if not yr_trades.empty:
                    yr_trades.to_csv(config.OUTPUT_DIR / f"backtest_trades_{yr}.csv", index=False)
        print(f"\nPer-year trade logs saved: output/backtest_trades_<year>.csv")
        if year_stats:
            year_df = pd.DataFrame(year_stats)
            print("\n" + "=" * 60)
            print(" SUMMARY BY CALENDAR YEAR (cleaned data — splits/bad ticks already handled)")
            print("=" * 60)
            print(year_df[["year", "total_trades", "win_rate_pct", "cagr_pct", "max_drawdown_pct"]].to_string(index=False))
            print("\nNote: each year here is calculated independently (starting fresh from your")
            print("--capital each Jan 1), NOT compounding year to year — this shows what a given")
            print("calendar year looked like in isolation, not a running account balance.")
        raise SystemExit(0)

    if args.n_folds:
        all_dates = sorted(ind_df["date"].unique())
        fold_edges = [all_dates[min(int(len(all_dates) * i / args.n_folds), len(all_dates) - 1)] for i in range(args.n_folds + 1)]
        fold_edges[-1] = all_dates[-1]
        print(f"\nSplitting your full history into {args.n_folds} independent periods:")
        fold_stats = []
        for i in range(args.n_folds):
            fold_start = fold_edges[i] if i == 0 else fold_edges[i] + pd.Timedelta(days=1)
            fold_end = fold_edges[i + 1]
            fold_trades, fold_equity, stats = run_one(fold_start, fold_end, f"FOLD {i+1}/{args.n_folds} ({fold_start.date()} to {fold_end.date()})")
            if "error" not in stats:
                stats["fold"] = i + 1
                fold_stats.append(stats)
                if not fold_trades.empty:
                    fold_trades.to_csv(config.OUTPUT_DIR / f"backtest_trades_fold{i+1}.csv", index=False)
                    fold_equity.to_csv(config.OUTPUT_DIR / f"backtest_equity_fold{i+1}.csv", index=False)
        print(f"\nPer-fold trade logs saved: output/backtest_trades_fold1.csv through fold{args.n_folds}.csv")
        if fold_stats:
            fold_df = pd.DataFrame(fold_stats)
            print("\n" + "=" * 60)
            print(" SUMMARY ACROSS ALL FOLDS — this is the real robustness check")
            print("=" * 60)
            print(fold_df[["fold", "total_trades", "win_rate_pct", "cagr_pct", "max_drawdown_pct"]].to_string(index=False))
            print("\nIf CAGR and drawdown are reasonably consistent across folds (no fold wildly")
            print("negative or a huge outlier), that's real evidence of a stable pattern. If one or")
            print("more folds look very different from the rest, the strategy is sensitive to which")
            print("period you happen to trade through — worth knowing before relying on it.")
        raise SystemExit(0)

    if args.rolling_out_of_sample:
        window_years = args.rolling_out_of_sample
        all_dates = sorted(ind_df["date"].unique())
        start_date_overall = all_dates[0]
        end_date_overall = all_dates[-1]
        total_years = (end_date_overall - start_date_overall).days / 365.25

        # Pick several test-window start points, evenly spaced, leaving at
        # least 3 years of training data before the first one.
        n_windows = max(1, int((total_years - 3) / window_years))
        results = []
        print(f"\nRolling out-of-sample: {n_windows} separate test window(s) of "
              f"{window_years} year(s) each, each trained on everything before it.")

        for i in range(n_windows):
            test_start = start_date_overall + pd.Timedelta(days=int(365.25 * (3 + i * window_years)))
            test_end = test_start + pd.Timedelta(days=int(365.25 * window_years))
            if test_start >= end_date_overall:
                break
            test_end = min(test_end, pd.Timestamp(end_date_overall))
            _, _, stats = run_one(test_start, test_end, f"ROLLING WINDOW {i+1}/{n_windows} "
                                   f"(test: {test_start.date()} to {test_end.date()})")
            if "error" not in stats:
                stats["window"] = i + 1
                stats["test_start"] = test_start.date()
                stats["test_end"] = test_end.date()
                results.append(stats)

        if results:
            rdf = pd.DataFrame(results)
            print("\n" + "=" * 60)
            print(" ROLLING OUT-OF-SAMPLE SUMMARY — every window is genuinely unseen data")
            print(" relative to everything before it, not just one lucky/unlucky split")
            print("=" * 60)
            print(rdf[["window", "test_start", "test_end", "total_trades",
                        "win_rate_pct", "cagr_pct", "max_drawdown_pct"]].to_string(index=False))
            print("\nConsistent results across ALL windows = real evidence of a stable pattern.")
            print("One or two good windows carrying the average = treat with more caution.")
        raise SystemExit(0)

    if args.out_of_sample:
        all_dates = sorted(ind_df["date"].unique())
        split = pd.Timestamp(args.split_date) if args.split_date else all_dates[int(len(all_dates) * 0.75)]
        print(f"\nSplitting at {split.date()} — training period before, out-of-sample test at/after.")
        print(f"Simulating training period{label_str}...")
        run_one(args.start, split - pd.Timedelta(days=1), "TRAINING PERIOD (in-sample — expect this to look good)")
        print(f"Simulating out-of-sample period{label_str}...")
        trades_df, equity_df, stats = run_one(split, args.end, "OUT-OF-SAMPLE PERIOD (the real test)")
        print("\nIf the out-of-sample win_rate/cagr/drawdown are close to the training period's, the")
        print("settings likely reflect a real pattern. If out-of-sample is much worse, it's likely overfit.")
    else:
        print(f"Simulating trades{label_str} (max {config.MAX_OPEN_POSITIONS} open positions, capital ₹{capital:,.0f})...")
        trades_df, equity_df, stats = run_one(args.start, args.end, "APEX200 BACKTEST RESULTS")

    trades_df.to_csv(config.OUTPUT_DIR / "backtest_trades.csv", index=False)
    equity_df.to_csv(config.OUTPUT_DIR / "backtest_equity_curve.csv", index=False)
    print(f"\nFull trade log: {config.OUTPUT_DIR / 'backtest_trades.csv'}")
    print(f"Equity curve:   {config.OUTPUT_DIR / 'backtest_equity_curve.csv'}")
    print("\nReminder: this is a historical simulation, not a guarantee of future results.")
