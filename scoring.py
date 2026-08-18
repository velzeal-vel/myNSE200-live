"""
myNSE200 / scoring.py
-----------------------
Cross-sectional z-score scoring engine.

Overall score = 100 (base)
  + Momentum (35%): 1M / 3M / 6M returns
  + Trend    (20%): price above SMA50, price above SMA200
  + Quality  (15%): return / volatility (Sharpe-like)
  + Growth   (30%): 3y sales growth, 3y profit growth

Missing growth data -> treated as neutral (z-score 0), per your old rule.
"""

import logging
from datetime import datetime, time as dtime

try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    IST = None  # fall back to naive local time if tzdata isn't available

import numpy as np
import pandas as pd

import config
import db

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("scoring")

MARKET_CLOSE_TIME = dtime(15, 35)  # NSE closes 15:30; small buffer for data settling


def _today_candle_is_still_forming(last_candle_date):
    """True if the most recent row in the price history is for TODAY and
    the market hasn't closed yet — meaning it's a live, still-moving
    price, not a finished daily close. Using it would make every
    entry/target/stop-loss shift each time you check during the day."""
    now = datetime.now(IST) if IST else datetime.now()
    if last_candle_date.date() != now.date():
        return False
    return now.time() < MARKET_CLOSE_TIME


def _zscore(series):
    s = pd.Series(series, dtype="float64")
    mean, std = s.mean(), s.std(ddof=0)
    if std == 0 or np.isnan(std):
        return pd.Series([0.0] * len(s), index=s.index)
    z = (s - mean) / std
    return z.fillna(0.0)


def load_candles(conn, symbols):
    placeholders = ",".join("?" * len(symbols))
    query = f"""
        SELECT symbol, date, open, high, low, close, volume
        FROM candle
        WHERE symbol IN ({placeholders})
        ORDER BY symbol, date
    """
    df = pd.read_sql_query(query, conn, params=symbols)
    df["date"] = pd.to_datetime(df["date"])
    return clean_extreme_moves(df)


def clean_extreme_moves(df, threshold_pct=45, revert_check_days=3, revert_tolerance_pct=25):
    """Neutralizes implausible single-day price jumps — one-day bad ticks get
    flattened, permanent level shifts (unadjusted splits, bad adjustment
    factors) get rescaled for continuity. Same protection as backtest.py's
    version — see that file's docstring for the incident that made this
    necessary."""
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
                for col in ["open", "high", "low", "close"]:
                    g.loc[idx, col] = g[col].iloc[idx - 1]
                total_flattened += 1
            else:
                ratio = pre_jump_level / post_jump_level
                for col in ["open", "high", "low", "close"]:
                    g.loc[idx:, col] = g.loc[idx:, col] * ratio
                total_rescaled += 1
                close = g["close"]

        out.append(g)

    result = pd.concat(out, ignore_index=True) if out else df
    if total_flattened or total_rescaled:
        log.warning(f"Data cleaning: {total_flattened} one-day bad tick(s) flattened, "
                     f"{total_rescaled} permanent level shift(s) rescaled for continuity.")
    return result


def _compute_technicals(df_sym):
    """df_sym: single-symbol OHLCV, sorted by date ascending, last row = most recent."""
    df_sym = df_sym.tail(config.SCORING_LOOKBACK_DAYS).copy()

    # Drop today's candle if the market is still open — otherwise every
    # check during the day would use a half-finished, still-moving price
    # as if it were a real close, making entry/target/stop-loss drift
    # each time you re-run.
    if len(df_sym) and _today_candle_is_still_forming(df_sym["date"].iloc[-1]):
        df_sym = df_sym.iloc[:-1]

    if len(df_sym) < 60:
        return None  # not enough history

    close = df_sym["close"]
    volume = df_sym["volume"]

    sma20 = close.rolling(20).mean()
    sma50 = close.rolling(50).mean()
    sma200 = close.rolling(200).mean() if len(close) >= 200 else pd.Series([np.nan] * len(close))

    last_close = close.iloc[-1]

    def pct_return(days):
        if len(close) <= days:
            return np.nan
        return (last_close / close.iloc[-days - 1] - 1.0) * 100

    ret_1m = pct_return(21)
    ret_3m = pct_return(63)
    ret_6m = pct_return(126)

    daily_ret = close.pct_change().dropna()
    ann_vol = daily_ret.std() * np.sqrt(252) * 100 if len(daily_ret) > 5 else np.nan
    ann_ret = ((last_close / close.iloc[0]) ** (252 / max(len(close), 1)) - 1) * 100
    sharpe_like = ann_ret / ann_vol if ann_vol and ann_vol > 0 else np.nan

    # ATR14 (for strategy.py stop-loss use)
    high, low = df_sym["high"], df_sym["low"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr14 = tr.rolling(config.ATR_PERIOD).mean().iloc[-1]

    avg_vol_20 = volume.rolling(20).mean().iloc[-1]
    last_vol = volume.iloc[-1]
    vol_surge_ratio = (last_vol / avg_vol_20) if avg_vol_20 and avg_vol_20 > 0 else np.nan

    swing_low = close.tail(config.SWING_LOW_LOOKBACK_DAYS).min() if 'SWING_LOW_LOOKBACK_DAYS' in dir(config) else None

    return {
        "last_close": last_close,
        "sma20": sma20.iloc[-1],
        "sma50": sma50.iloc[-1],
        "sma200": sma200.iloc[-1] if len(sma200) else np.nan,
        "ret_1m": ret_1m,
        "ret_3m": ret_3m,
        "ret_6m": ret_6m,
        "sharpe_like": sharpe_like,
        "atr14": atr14,
        "vol_surge_ratio": vol_surge_ratio,
        "swing_low_recent": low.tail(config.SWING_LOW_LOOKBACK_DAYS).min(),
        "last_volume": last_vol,
        "above_sma50": bool(last_close > sma50.iloc[-1]) if not np.isnan(sma50.iloc[-1]) else False,
        "above_sma200": bool(last_close > sma200.iloc[-1]) if len(sma200) and not np.isnan(sma200.iloc[-1]) else False,
        "above_sma20": bool(last_close > sma20.iloc[-1]) if not np.isnan(sma20.iloc[-1]) else False,
    }


def check_market_regime(sma_period=None):
    """True if the Nifty 50 index is currently above its own N-day SMA (a
    healthy broader market), False if below, None if no index data is
    available yet. See config.USE_REGIME_FILTER for why this exists."""
    sma_period = sma_period or config.REGIME_SMA_PERIOD
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT date, close FROM candle WHERE symbol = 'NIFTY50' ORDER BY date", conn
    )
    conn.close()
    if len(df) < sma_period:
        return None
    df["sma"] = df["close"].rolling(sma_period).mean()
    latest = df.iloc[-1]
    if pd.isna(latest["sma"]):
        return None
    return bool(latest["close"] > latest["sma"])


def compute_scores(symbols, run_date=None):
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    conn = db.get_conn()

    candles = load_candles(conn, symbols)
    fundamentals = pd.read_sql_query("SELECT * FROM fundamentals", conn)
    fundamentals = fundamentals.set_index("symbol")

    tech_rows = {}
    for symbol, df_sym in candles.groupby("symbol"):
        tech = _compute_technicals(df_sym.sort_values("date"))
        if tech:
            tech_rows[symbol] = tech

    if not tech_rows:
        log.warning("No symbols had enough history to score.")
        conn.close()
        return pd.DataFrame()

    tech_df = pd.DataFrame.from_dict(tech_rows, orient="index")

    # ---- Momentum (35%) ----
    mom_z = (
        _zscore(tech_df["ret_1m"]).fillna(0) +
        _zscore(tech_df["ret_3m"]).fillna(0) +
        _zscore(tech_df["ret_6m"]).fillna(0)
    ) / 3.0

    # ---- Trend (20%) ----
    trend_raw = tech_df["above_sma50"].astype(int) + tech_df["above_sma200"].astype(int)
    trend_z = _zscore(trend_raw)

    # ---- Quality (15%) ----
    qual_z = _zscore(tech_df["sharpe_like"])

    # ---- Growth (30%) ----
    growth = fundamentals.reindex(tech_df.index)
    sales_g = growth["sales_growth_3y"]
    profit_g = growth["profit_growth_3y"]
    growth_known = growth["growth_known"].fillna(0).astype(bool)

    sales_z = _zscore(sales_g)
    profit_z = _zscore(profit_g)
    growth_z = (sales_z + profit_z) / 2.0
    # Missing growth data -> neutral (0), per your rule
    growth_z = growth_z.where(growth_known, 0.0)

    overall = (
        100
        + config.SCORE_WEIGHTS["momentum"] * mom_z * 10
        + config.SCORE_WEIGHTS["trend"] * trend_z * 10
        + config.SCORE_WEIGHTS["quality"] * qual_z * 10
        + config.SCORE_WEIGHTS["growth"] * growth_z * 10
    )

    result = tech_df.copy()
    result["momentum_z"] = mom_z
    result["trend_z"] = trend_z
    result["quality_z"] = qual_z
    result["growth_z"] = growth_z
    result["overall_score"] = overall

    # ---- ★ Recommended filter (your old rule set) ----
    # NaN-safe: pandas represents missing data as NaN, not None. Checking only
    # "is None" let stocks with completely missing fundamentals silently PASS
    # this filter instead of being correctly disqualified — a real bug found
    # via check_setup.py showing an empty fundamentals table still producing
    # BUY signals. pd.isna() catches both None and NaN correctly.
    def is_recommended(sym):
        row = growth.loc[sym] if sym in growth.index else None
        if row is None:
            return False
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

    result["recommended"] = [is_recommended(s) for s in result.index]

    # persist
    rows = []
    for sym, r in result.iterrows():
        rows.append((
            sym, run_date,
            float(r["momentum_z"]), float(r["trend_z"]),
            float(r["quality_z"]), float(r["growth_z"]),
            float(r["overall_score"]), int(bool(r["recommended"])),
        ))
    conn.executemany(
        """INSERT OR REPLACE INTO scores
           (symbol, run_date, momentum_z, trend_z, quality_z, growth_z, overall_score, recommended)
           VALUES (?,?,?,?,?,?,?,?)""",
        rows,
    )
    conn.commit()
    conn.close()

    result = result.sort_values("overall_score", ascending=False)
    log.info(f"Scored {len(result)} symbols for {run_date}.")
    return result


if __name__ == "__main__":
    import data_fetch
    db.init_db()
    syms = data_fetch.load_universe_snapshot()
    scores = compute_scores(syms)
    print(scores[["overall_score", "recommended"]].head(20))
