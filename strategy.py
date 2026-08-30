"""
myNSE200 / strategy.py
------------------------
The Apex200 strategy: turns scored, fundamentally-recommended stocks into
concrete, actionable trade plans (entry, stop-loss, target, quantity).

Design (documented so you can tune it — nothing here is "guaranteed profit"):

1. FILTER  — only stocks that pass the ★ Recommended fundamental rules
             (promoter < 40%, FII+DII >= 25%, pledge == 0, no unknown pledge)
             AND rank in the top MIN_SCORE_PERCENTILE of the scored universe.

2. TRIGGER — a technical confirmation so you're not just buying "on paper
             strength" but on actual price/volume confirmation:
                - close > SMA20 and close > SMA50 (uptrend intact)
                - today's volume >= MIN_VOLUME_SURGE_RATIO x its 20-day avg
                  (real buying interest, not a dead tape)

3. ENTRY   — next day's open (no look-ahead: signal is generated on
             today's close, per your old backtest rule). The report also
             shows a suggested LIMIT buy zone (close to close+0.5%) in case
             you're placing the order manually before the open print.

4. STOP    — max(entry - ATR_STOP_MULTIPLIER * ATR14, recent 10-day swing low)
             i.e. we take whichever stop is less aggressive is not what we
             want — we take the TIGHTER of the two so risk stays controlled,
             then size the position so the ₹ risk is fixed regardless of
             which stock you pick.

5. TARGET  — entry + REWARD_RISK_RATIO * (entry - stop). Default 3:1.

6. SIZING  — risk RISK_PER_TRADE_PCT of ACCOUNT_CAPITAL per trade, capped
             by MAX_PER_ORDER_PCT_OF_EQUITY and MAX_ORDERS_PER_DAY.

`status = True` in the output means: this stock cleared every filter AND
trigger tonight — it's on your buy list for tomorrow. `status = False`
means it's in the universe but didn't qualify tonight.
"""

import logging
from datetime import datetime

import pandas as pd

import config
import db
import scoring


def load_sector_map():
    """Manually compiled symbol -> sector mapping (see data/sector_map.csv's
    own header comment for the accuracy caveat). Used for the live sector
    concentration cap. Returns None if the file doesn't exist."""
    path = config.DATA_DIR / "sector_map.csv"
    if not path.exists():
        return None
    import pandas as pd
    df = pd.read_csv(path)
    return dict(zip(df["symbol"], df["sector"]))

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("strategy")


def _passes_trigger(row):
    reasons = []
    ok = True

    if config.REQUIRE_PRICE_ABOVE_SMA20 and not row.get("above_sma20", False):
        ok = False
        reasons.append("price below SMA20")
    if config.REQUIRE_PRICE_ABOVE_SMA50 and not row.get("above_sma50", False):
        ok = False
        reasons.append("price below SMA50")

    vol_ratio = row.get("vol_surge_ratio")
    if vol_ratio is None or pd.isna(vol_ratio) or vol_ratio < config.MIN_VOLUME_SURGE_RATIO:
        ok = False
        reasons.append(f"volume surge {vol_ratio:.2f}x < {config.MIN_VOLUME_SURGE_RATIO}x" if vol_ratio == vol_ratio else "no volume data")

    return ok, reasons


def _compute_trade_plan(row):
    entry = float(row["last_close"])  # signal close; actual buy = next day's open
    atr = row.get("atr14")
    swing_low = row.get("swing_low_recent")

    atr_stop = entry - config.ATR_STOP_MULTIPLIER * atr if atr and atr == atr else None
    candidates = [s for s in [atr_stop, swing_low] if s is not None and s == s]
    if not candidates:
        return None

    # Tighter (higher) stop = lower risk per share, controlled loss
    stop = max(candidates) if config.USE_SWING_LOW_STOP_IF_TIGHTER else atr_stop
    if stop is None or stop >= entry:
        return None

    risk_per_share = entry - stop
    target = entry + config.REWARD_RISK_RATIO * risk_per_share

    # Position sizing: fixed % risk per trade, capped at % of current capital
    risk_capital = config.ACCOUNT_CAPITAL * (config.RISK_PER_TRADE_PCT / 100.0)
    qty_by_risk = int(risk_capital // risk_per_share) if risk_per_share > 0 else 0
    max_order_value = config.ACCOUNT_CAPITAL * (config.MAX_PER_ORDER_PCT_OF_EQUITY / 100.0)
    qty_by_cap = int(max_order_value // entry) if entry > 0 else 0
    quantity = max(0, min(qty_by_risk, qty_by_cap))

    order_value = round(quantity * entry, 2)

    return {
        "entry_price": round(entry, 2),
        "buy_zone_low": round(entry, 2),
        "buy_zone_high": round(entry * 1.005, 2),
        "stop_loss": round(stop, 2),
        "target_price": round(target, 2),
        "risk_per_share": round(risk_per_share, 2),
        "reward_risk_ratio": config.REWARD_RISK_RATIO,
        "quantity": quantity,
        "order_value": order_value,
        "max_holding_days": config.MAX_HOLDING_DAYS,
        "trail_stop_trigger_price": round(entry + config.TRAIL_STOP_AFTER_R_MULTIPLE * risk_per_share, 2),
    }


def filter_by_liquidity(symbols):
    """VALIDATED (Aug 2026): excludes the bottom config.EXCLUDE_BOTTOM_LIQUIDITY_PCT% of
    the given symbols by average daily traded value (price x volume) — directly
    motivated by survivorship_check.py's finding that marginal constituents already
    underperform larger ones. Matches backtest.py's --exclude-bottom-liquidity-pct
    exactly: filtering happens BEFORE scoring, so percentile-based cutoffs apply to
    the reduced set, same as what was validated."""
    if not config.EXCLUDE_BOTTOM_LIQUIDITY_PCT:
        return symbols
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT symbol, close, volume FROM candle WHERE symbol IN ({})".format(
            ",".join("?" * len(symbols))
        ), conn, params=symbols,
    )
    conn.close()
    if df.empty:
        return symbols
    df["traded_value"] = df["close"] * df["volume"]
    avg_value = df.groupby("symbol")["traded_value"].mean().sort_values(ascending=False)
    cutoff = int(len(avg_value) * (1 - config.EXCLUDE_BOTTOM_LIQUIDITY_PCT / 100))
    keep = set(avg_value.index[:cutoff])
    filtered = [s for s in symbols if s in keep]
    log.info(f"Liquidity filter: {len(symbols)} -> {len(filtered)} symbols "
             f"(excluded bottom {config.EXCLUDE_BOTTOM_LIQUIDITY_PCT}% by traded value).")
    return filtered


def run(symbols, run_date=None):
    run_date = run_date or datetime.now().strftime("%Y-%m-%d")
    symbols = filter_by_liquidity(symbols)
    scored = scoring.compute_scores(symbols, run_date=run_date)
    if scored.empty:
        log.warning("No scored data — did you run data_fetch and fundamentals first?")
        return pd.DataFrame()

    # Market regime gate: if the broader market itself is weak, no NEW
    # positions are opened tonight, regardless of individual stock scores.
    # See config.USE_REGIME_FILTER for the validation behind this.
    market_is_bullish = True
    regime_reason = None
    if config.USE_REGIME_FILTER:
        regime_status = scoring.check_market_regime()
        if regime_status is None:
            log.warning("Regime filter enabled but no NIFTY50 index data available — "
                        "run data_fetch.py --fetch-index. Proceeding WITHOUT the filter tonight.")
        elif not regime_status:
            market_is_bullish = False
            regime_reason = (f"market regime filter: Nifty 50 is below its "
                              f"{config.REGIME_SMA_PERIOD}-day average — no new entries tonight")

    score_cutoff = scored["overall_score"].quantile(config.MIN_SCORE_PERCENTILE / 100.0)

    # Sector concentration gate: sum up what's ALREADYheld in each sector
    # (real confirmed holdings, from portfolio.py) plus whatever this same
    # run has already accepted, so several signals in one sector on the
    # same night can't collectively blow past the cap either.
    sector_map = None
    sector_exposure = {}
    if config.USE_SECTOR_CAP:
        sector_map = load_sector_map()
        if sector_map is None:
            log.warning("USE_SECTOR_CAP is on but data/sector_map.csv is missing — "
                        "proceeding WITHOUT the sector cap tonight.")
        else:
            import portfolio
            holding = portfolio.get_holding()
            for _, h in holding.iterrows():
                sec = sector_map.get(h["symbol"])
                if sec:
                    sector_exposure[sec] = sector_exposure.get(sec, 0.0) + h["buy_price"] * h["quantity_bought"]

    conn = db.get_conn()
    signal_rows = []
    report_rows = []

    for symbol, row in scored.iterrows():
        reasons = []
        status = True

        if not market_is_bullish:
            status = False
            reasons.append(regime_reason)

        if not row["recommended"]:
            status = False
            reasons.append("fails ★ Recommended fundamental filter")

        if row["overall_score"] < score_cutoff:
            status = False
            reasons.append(f"score below top {100 - config.MIN_SCORE_PERCENTILE}% cutoff")

        trigger_ok, trigger_reasons = _passes_trigger(row)
        if not trigger_ok:
            status = False
            reasons.extend(trigger_reasons)

        plan = None
        if status:
            plan = _compute_trade_plan(row)
            if plan is None or plan["quantity"] <= 0:
                status = False
                reasons.append("risk sizing produced zero quantity (stop too close / capital too small)")

        if status and plan and sector_map is not None:
            sym_sector = sector_map.get(symbol)
            if sym_sector is not None:
                current = sector_exposure.get(sym_sector, 0.0)
                cap_value = config.ACCOUNT_CAPITAL * (config.MAX_PER_SECTOR_PCT / 100.0)
                if current + plan["order_value"] > cap_value:
                    status = False
                    reasons.append(f"sector cap: {sym_sector} already at "
                                    f"₹{current:,.0f}/₹{cap_value:,.0f} of the {config.MAX_PER_SECTOR_PCT}% limit")
                else:
                    sector_exposure[sym_sector] = current + plan["order_value"]

        record = {
            "symbol": symbol,
            "status": status,
            "overall_score": round(float(row["overall_score"]), 2),
            "reasons": "; ".join(reasons) if reasons else "OK",
        }
        if plan:
            record.update(plan)

        report_rows.append(record)

        signal_rows.append((
            symbol, run_date, int(status), float(row["overall_score"]),
            plan["entry_price"] if plan else None,
            plan["stop_loss"] if plan else None,
            plan["target_price"] if plan else None,
            plan["risk_per_share"] if plan else None,
            plan["quantity"] if plan else None,
            plan["order_value"] if plan else None,
            plan["reward_risk_ratio"] if plan else None,
            record["reasons"],
        ))

    conn.executemany(
        """INSERT OR REPLACE INTO signals
           (symbol, run_date, status, overall_score, entry_price, stop_loss,
            target_price, risk_per_share, quantity, order_value,
            reward_risk_ratio, reasons)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        signal_rows,
    )
    conn.commit()
    conn.close()

    report_df = pd.DataFrame(report_rows)
    # Guarantee these columns always exist, even if zero rows produced a plan
    plan_cols = [
        "entry_price", "buy_zone_low", "buy_zone_high", "stop_loss",
        "target_price", "risk_per_share", "reward_risk_ratio", "quantity",
        "order_value", "max_holding_days", "trail_stop_trigger_price",
    ]
    for c in plan_cols:
        if c not in report_df.columns:
            report_df[c] = None
    report_df = report_df.sort_values(
        ["status", "overall_score"], ascending=[False, False]
    ).reset_index(drop=True)

    # Enforce daily order cap on the BUY list only (status True), keep the rest for visibility
    buy_list = report_df[report_df["status"]].copy()
    if len(buy_list) > config.MAX_ORDERS_PER_DAY:
        overflow_symbols = buy_list.iloc[config.MAX_ORDERS_PER_DAY:]["symbol"]
        report_df.loc[report_df["symbol"].isin(overflow_symbols), "status"] = False
        report_df.loc[report_df["symbol"].isin(overflow_symbols), "reasons"] += \
            f"; exceeds MAX_ORDERS_PER_DAY ({config.MAX_ORDERS_PER_DAY}), trimmed to top scores"

    log.info(
        f"Apex200 run {run_date}: {report_df['status'].sum()} BUY signals "
        f"out of {len(report_df)} scored."
    )
    return report_df


if __name__ == "__main__":
    import data_fetch
    db.init_db()
    syms = data_fetch.load_universe_snapshot()
    df = run(syms)
    cols = ["symbol", "status", "overall_score", "entry_price", "stop_loss",
            "target_price", "quantity", "order_value", "reasons"]
    print(df[cols].head(30).to_string(index=False))
