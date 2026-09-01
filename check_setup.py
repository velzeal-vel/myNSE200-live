"""
myNSE200 / check_setup.py
----------------------------
One command that tells you EVERYTHING currently true about your setup —
config settings, universe, actual data coverage, fundamentals freshness,
and Telegram config — so you can verify before trusting any backtest
result or going live. Run this any time you're not 100% sure what state
your folder is in.

Usage:
    python3 check_setup.py
"""

import sqlite3
from datetime import datetime

import pandas as pd

import config
import db
import scoring


def section(title):
    print("\n" + "=" * 60)
    print(f" {title}")
    print("=" * 60)


def check_config():
    section("1. CONFIG.PY — key settings currently active")
    print(f"  UNIVERSE_INDEX:              {config.UNIVERSE_INDEX}")
    print(f"  UNIVERSE URL in use:         {config.NIFTY200_LIVE_URL}")
    print(f"  HISTORY_YEARS (requested):   {config.HISTORY_YEARS}")
    print(f"  ATR_STOP_MULTIPLIER:         {config.ATR_STOP_MULTIPLIER}")
    print(f"  REWARD_RISK_RATIO:           {config.REWARD_RISK_RATIO}")
    print(f"  ACCOUNT_CAPITAL:             ₹{config.ACCOUNT_CAPITAL:,.2f}")
    print(f"  RISK_PER_TRADE_PCT:          {config.RISK_PER_TRADE_PCT}%")
    print(f"  MAX_PER_ORDER_PCT_OF_EQUITY: {config.MAX_PER_ORDER_PCT_OF_EQUITY}%")
    print(f"  MAX_ORDERS_PER_DAY:          {config.MAX_ORDERS_PER_DAY}")
    print(f"  MAX_OPEN_POSITIONS:          {config.MAX_OPEN_POSITIONS}")
    print(f"  MAX_HOLDING_DAYS:            {config.MAX_HOLDING_DAYS}")
    print(f"  MIN_SCORE_PERCENTILE:        {config.MIN_SCORE_PERCENTILE}")
    print(f"  MIN_VOLUME_SURGE_RATIO:      {config.MIN_VOLUME_SURGE_RATIO}")
    print(f"  USE_REGIME_FILTER:           {getattr(config, 'USE_REGIME_FILTER', 'NOT SET (old config.py)')}")
    print(f"  REGIME_SMA_PERIOD:           {getattr(config, 'REGIME_SMA_PERIOD', 'NOT SET (old config.py)')}")
    print(f"  PAPER_TRADING:               {config.PAPER_TRADING}")

    if config.UNIVERSE_INDEX != "nifty200":
        print(f"\n  ⚠️  WARNING: UNIVERSE_INDEX is '{config.UNIVERSE_INDEX}', not 'nifty200'.")
        print("     If you intended to run the validated Nifty 200 setup, fix this in config.py.")

    # Compare against the actual validated settings from this project's testing —
    # flags if this config.py has drifted from what was proven to work. Only
    # STRATEGY-SHAPE settings belong here (things validated by real backtesting) —
    # MAX_PER_ORDER_PCT_OF_EQUITY and ACCOUNT_CAPITAL are deliberately NOT included,
    # since those are meant to be personally customized for your own capital and
    # would otherwise get wrongly flagged as "drift" every time you tune them.
    validated = {
        "ATR_STOP_MULTIPLIER": 3.0, "REWARD_RISK_RATIO": 2.0,
        "MIN_SCORE_PERCENTILE": 90, "UNIVERSE_INDEX": "nifty200",
        "MAX_PER_SECTOR_PCT": 40.0, "EXCLUDE_BOTTOM_LIQUIDITY_PCT": 25.0,
    }
    drifted = []
    for key, expected in validated.items():
        actual = getattr(config, key, None)
        if actual != expected:
            drifted.append(f"{key}: expected {expected}, found {actual}")
    if not getattr(config, "USE_REGIME_FILTER", False):
        drifted.append("USE_REGIME_FILTER: expected True, found False/missing")
    if not getattr(config, "USE_SECTOR_CAP", False):
        drifted.append("USE_SECTOR_CAP: expected True, found False/missing")
    if drifted:
        print(f"\n  ⚠️  DRIFT FROM VALIDATED SETTINGS:")
        for d in drifted:
            print(f"     - {d}")
    else:
        print(f"\n  ✅ All settings match the validated configuration from testing.")


def check_env():
    section("2. .ENV — Telegram config (values hidden, presence only)")
    token_set = bool(config.TELEGRAM_BOT_TOKEN)
    chat_set = bool(config.TELEGRAM_CHAT_ID)
    print(f"  TELEGRAM_BOT_TOKEN loaded:   {'YES' if token_set else 'NO — .env missing or not loaded'}")
    print(f"  TELEGRAM_CHAT_ID loaded:     {'YES' if chat_set else 'NO — .env missing or not loaded'}")
    if not token_set or not chat_set:
        print("\n  ⚠️  Telegram notifications will silently NOT send until both are set.")


def check_universe_file():
    section("3. UNIVERSE FILE — data/nifty200.csv")
    if not config.UNIVERSE_CSV.exists():
        print(f"  ⚠️  No universe snapshot found at {config.UNIVERSE_CSV}")
        print("     Run: python3 data_fetch.py --update-universe")
        return
    with open(config.UNIVERSE_CSV) as f:
        symbols = [line.strip() for line in f.readlines()[1:] if line.strip()]
    print(f"  Symbols in universe file:    {len(symbols)}")
    print(f"  First 5:                     {symbols[:5]}")
    print(f"  Last updated:                {datetime.fromtimestamp(config.UNIVERSE_CSV.stat().st_mtime)}")


def check_database():
    section("4. DATABASE — what data you actually have (the real proof, not just config)")
    if not config.DB_PATH.exists():
        print(f"  ⚠️  No database found at {config.DB_PATH}. Run: python3 main.py --full-history")
        return

    conn = db.get_conn()

    # Exclude NIFTY50 (the regime index, not a tradeable stock) from all
    # counts below — it would otherwise misleadingly show up as a "stock
    # with short history" or inflate the symbol count.
    candle_count = pd.read_sql_query(
        "SELECT COUNT(*) as n FROM candle WHERE symbol != 'NIFTY50'", conn
    )["n"].iloc[0]
    if candle_count == 0:
        print("  ⚠️  candle table is EMPTY. Run: python3 main.py --full-history")
        conn.close()
        return

    date_range = pd.read_sql_query(
        "SELECT MIN(date) as min_d, MAX(date) as max_d FROM candle WHERE symbol != 'NIFTY50'", conn
    )
    min_date, max_date = date_range["min_d"].iloc[0], date_range["max_d"].iloc[0]
    years_span = (pd.Timestamp(max_date) - pd.Timestamp(min_date)).days / 365.25

    symbol_count = pd.read_sql_query(
        "SELECT COUNT(DISTINCT symbol) as n FROM candle WHERE symbol != 'NIFTY50'", conn
    )["n"].iloc[0]

    print(f"  Total candle rows:           {candle_count:,}")
    print(f"  Distinct symbols with data:  {symbol_count}")
    print(f"  Earliest date in database:   {min_date}")
    print(f"  Latest date in database:     {max_date}")
    print(f"  Actual span:                 {years_span:.1f} years")

    if years_span < config.HISTORY_YEARS - 1:
        print(f"\n  ⚠️  You requested {config.HISTORY_YEARS} years but only {years_span:.1f} years are")
        print("     actually in the database. This is common — many stocks don't have that much")
        print("     history on Yahoo Finance (newer listings, data gaps). See per-symbol breakdown below.")

    # Per-symbol coverage — flags symbols with much shorter history than the rest
    per_symbol = pd.read_sql_query(
        "SELECT symbol, MIN(date) as first_date, COUNT(*) as n_rows FROM candle "
        "WHERE symbol != 'NIFTY50' GROUP BY symbol", conn
    )
    per_symbol["first_date"] = pd.to_datetime(per_symbol["first_date"])
    cutoff = pd.Timestamp(max_date) - pd.Timedelta(days=int((config.HISTORY_YEARS - 2) * 365.25))
    short_history = per_symbol[per_symbol["first_date"] > cutoff]
    print(f"\n  Symbols with LESS than ~{config.HISTORY_YEARS - 2} years of history: {len(short_history)} / {symbol_count}")
    if len(short_history) > 0:
        print("  (These will only contribute to more recent folds/periods in backtests — normal for")
        print("   newer listings, but worth knowing when interpreting older-period backtest results)")
        print(f"  Examples: {short_history['symbol'].head(10).tolist()}")

    conn.close()


def check_fundamentals():
    section("6. FUNDAMENTALS — freshness")
    conn = db.get_conn()
    try:
        df = pd.read_sql_query("SELECT symbol, last_updated, pledge_known, growth_known FROM fundamentals", conn)
    except Exception:
        print("  ⚠️  No fundamentals table / no data yet. Run: python3 fundamentals.py")
        conn.close()
        return
    if df.empty:
        print("  ⚠️  fundamentals table is EMPTY.")
        conn.close()
        return

    df["last_updated"] = pd.to_datetime(df["last_updated"], errors="coerce")
    stale_cutoff = pd.Timestamp.now() - pd.Timedelta(days=30)
    stale = df[df["last_updated"] < stale_cutoff]
    unknown_pledge = df[df["pledge_known"] == 0]

    print(f"  Symbols with fundamentals:   {len(df)}")
    print(f"  Stale (>30 days old):        {len(stale)}")
    print(f"  Unknown pledge data:         {len(unknown_pledge)} (these auto-fail ★ Recommended filter)")
    conn.close()


def check_signals():
    section("7. LATEST LIVE SIGNAL RUN")
    conn = db.get_conn()
    try:
        latest = pd.read_sql_query(
            "SELECT run_date, COUNT(*) as total, SUM(status) as buys FROM signals GROUP BY run_date ORDER BY run_date DESC LIMIT 1",
            conn,
        )
    except Exception:
        print("  No signals table yet — run python3 main.py at least once.")
        conn.close()
        return
    if latest.empty:
        print("  No signal runs recorded yet.")
    else:
        row = latest.iloc[0]
        print(f"  Most recent run:             {row['run_date']}")
        print(f"  Stocks scored:                {int(row['total'])}")
        print(f"  BUY signals:                  {int(row['buys'])}")
    conn.close()


def check_index_data():
    section("5. MARKET REGIME INDEX — Nifty 50 data for the regime filter")
    if not getattr(config, "USE_REGIME_FILTER", False):
        print("  USE_REGIME_FILTER is off — skipping this check.")
        return
    if not config.DB_PATH.exists():
        print("  ⚠️  No database found — can't check.")
        return
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT MIN(date) as first, MAX(date) as last, COUNT(*) as n FROM candle WHERE symbol='NIFTY50'", conn
    )
    stock_range = pd.read_sql_query("SELECT MIN(date) as first FROM candle WHERE symbol != 'NIFTY50'", conn)
    conn.close()

    if df["n"].iloc[0] == 0:
        print("  ⚠️  NO index data found. Regime filter will silently do nothing until you run:")
        print("     python3 data_fetch.py --fetch-index")
        return

    print(f"  Index rows:                  {int(df['n'].iloc[0])}")
    print(f"  Index data covers:           {df['first'].iloc[0]} to {df['last'].iloc[0]}")

    stock_first = stock_range["first"].iloc[0]
    if stock_first and pd.Timestamp(df["first"].iloc[0]) > pd.Timestamp(stock_first):
        gap_years = (pd.Timestamp(df["first"].iloc[0]) - pd.Timestamp(stock_first)).days / 365.25
        print(f"\n  ⚠️  Your stock data starts {stock_first}, but index data starts "
              f"{df['first'].iloc[0]} ({gap_years:.1f} years later). Backtests covering dates "
              f"before this get NO regime filtering. Re-run --fetch-index if unexpected.")
    else:
        print("  ✅ Index data covers your full stock history — no coverage gap.")

    is_bull = scoring.check_market_regime()
    if is_bull is not None:
        status = "BULL (above 200-day average)" if is_bull else "BEAR (below 200-day average)"
        print(f"\n  Current market regime: {status}")
        if not is_bull:
            print("  This is why you may be seeing 0 buy signals — expected behavior, not a bug.")


def check_positions():
    section("8. POSITION TRACKING — pending/holding/closed & Telegram reply state")
    conn = db.get_conn()
    try:
        counts = pd.read_sql_query(
            "SELECT status, COUNT(*) as n FROM positions GROUP BY status", conn
        )
    except Exception:
        print("  ⚠️  No positions table found. Run: python3 -c \"import db; db.init_db()\"")
        conn.close()
        return

    if counts.empty:
        print("  No positions tracked yet (normal if you haven't had a BUY signal, or")
        print("  haven't confirmed one, since upgrading to the interactive format).")
    else:
        for _, row in counts.iterrows():
            print(f"  {row['status']:<14} {int(row['n'])}")

    pending = pd.read_sql_query(
        "SELECT symbol, signal_date FROM positions WHERE status='pending' ORDER BY signal_date DESC", conn
    )
    if not pending.empty:
        print(f"\n  Pending symbols awaiting BUY/SKIP: {', '.join(pending['symbol'].tolist())}")

    holding = pd.read_sql_query(
        "SELECT symbol, buy_date FROM positions WHERE status='holding' ORDER BY buy_date DESC", conn
    )
    if not holding.empty:
        print(f"  Currently holding: {', '.join(holding['symbol'].tolist())}")

    state = conn.execute("SELECT value FROM telegram_state WHERE key='last_update_id'").fetchone()
    conn.close()
    if state:
        print(f"\n  Last processed Telegram update_id: {state[0]}")
        print("  (If this never changes across runs, replies aren't being picked up — see")
        print("   the reply-testing steps to diagnose.)")
    else:
        print("\n  No Telegram replies processed yet (normal if you haven't replied BUY/SKIP)")
        print("  to anything since upgrading).")


if __name__ == "__main__":
    print("myNSE200 — full setup diagnostic")
    check_config()
    check_env()
    check_universe_file()
    check_database()
    check_index_data()
    check_fundamentals()
    check_signals()
    check_positions()
    print("\n" + "=" * 60)
    print(" Review any ⚠️ warnings above before trusting backtest results or going live.")
    print("=" * 60)
