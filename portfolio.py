"""
myNSE200 / portfolio.py
--------------------------
Tracks the lifecycle of each signal AFTER Apex200 generates it — this is a
new layer on top of the strategy, not a change to it. strategy.py still
decides what qualifies exactly as before; this module just remembers what
happened next: did you buy it, are you still holding it, did it hit its
target/stop/time exit.

Statuses: pending -> holding -> closed_target / closed_stop / closed_time
                   -> skipped (if you reply SKIP)
                   -> expired (if left unconfirmed too long)

Nothing here affects entry_price/stop_loss/target_price/quantity — those
still come entirely from strategy.py's calculation, unchanged.
"""

import logging
from datetime import datetime, timedelta

import pandas as pd

import config
import db

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("portfolio")

MAX_PENDING_DAYS = 3  # a signal not confirmed within this many runs auto-expires


def create_pending_from_signals(report_df, run_date):
    """Takes today's strategy.run() output and inserts a new 'pending' row
    for every status=True signal that doesn't already have an open
    pending/holding entry for that symbol."""
    if report_df.empty:
        return []

    conn = db.get_conn()
    existing = pd.read_sql_query(
        "SELECT symbol FROM positions WHERE status IN ('pending', 'holding')", conn
    )["symbol"].tolist()

    new_symbols = []
    buys = report_df[report_df["status"] == True]
    for _, row in buys.iterrows():
        if row["symbol"] in existing:
            continue  # already tracked, don't duplicate
        conn.execute(
            """INSERT INTO positions
               (symbol, status, signal_date, signal_price, stop_loss, target_price,
                quantity_recommended, last_updated)
               VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (row["symbol"], run_date, row["entry_price"], row["stop_loss"],
             row["target_price"], int(row["quantity"]), datetime.now().isoformat()),
        )
        new_symbols.append(row["symbol"])
    conn.commit()
    conn.close()
    return new_symbols


def expire_stale_pending(max_pending_days=MAX_PENDING_DAYS):
    """Signals sitting unconfirmed for too long get auto-expired rather
    than cluttering the pending list forever with stale prices."""
    conn = db.get_conn()
    cutoff = (datetime.now() - timedelta(days=max_pending_days)).strftime("%Y-%m-%d")
    conn.execute(
        "UPDATE positions SET status='expired', last_updated=? "
        "WHERE status='pending' AND signal_date < ?",
        (datetime.now().isoformat(), cutoff),
    )
    conn.commit()
    conn.close()


def get_latest_price(conn, symbol):
    row = conn.execute(
        "SELECT close FROM candle WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,)
    ).fetchone()
    return row[0] if row else None


def check_holding_exits(max_holding_days=None):
    """For every currently-held position, checks today's price against its
    stop-loss, target, and the max-holding-days time exit — same three
    exit rules strategy.py's backtest logic uses, applied here to real
    confirmed positions. Returns the list of positions that just closed,
    for the Telegram EXIT section."""
    max_holding_days = max_holding_days or config.MAX_HOLDING_DAYS
    conn = db.get_conn()
    holding = pd.read_sql_query("SELECT * FROM positions WHERE status='holding'", conn)

    just_closed = []
    today = datetime.now().strftime("%Y-%m-%d")
    for _, pos in holding.iterrows():
        price = get_latest_price(conn, pos["symbol"])
        if price is None:
            continue

        exit_price, exit_reason = None, None
        if price <= pos["stop_loss"]:
            exit_price, exit_reason = pos["stop_loss"], "closed_stop"
        elif price >= pos["target_price"]:
            exit_price, exit_reason = pos["target_price"], "closed_target"
        else:
            buy_date = pos["buy_date"]
            if buy_date:
                days_held = (datetime.now() - datetime.fromisoformat(buy_date)).days
                if days_held >= max_holding_days:
                    exit_price, exit_reason = price, "closed_time"

        if exit_price is not None:
            conn.execute(
                "UPDATE positions SET status=?, exit_price=?, exit_date=?, exit_reason=?, "
                "last_updated=? WHERE id=?",
                (exit_reason, exit_price, today, exit_reason, datetime.now().isoformat(), pos["id"]),
            )
            pos_dict = pos.to_dict()
            pos_dict.update({"exit_price": exit_price, "exit_reason": exit_reason})
            just_closed.append(pos_dict)

    conn.commit()
    conn.close()
    return just_closed


def get_pending():
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT * FROM positions WHERE status='pending' ORDER BY signal_date DESC", conn
    )
    conn.close()
    return df


def get_holding():
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT * FROM positions WHERE status='holding' ORDER BY buy_date DESC", conn
    )
    conn.close()
    return df


def compute_portfolio_summary(account_capital=None):
    """Approximate running portfolio value: starting capital, minus cash
    currently locked in open holdings, plus/minus realized P&L from every
    closed position ever. This is a tracking approximation for your own
    visibility (paper/manual trading) — not exact brokerage accounting
    (doesn't model brokerage/STT/slippage on entries/exits)."""
    account_capital = account_capital or config.ACCOUNT_CAPITAL
    conn = db.get_conn()

    holding = pd.read_sql_query("SELECT * FROM positions WHERE status='holding'", conn)
    closed = pd.read_sql_query(
        "SELECT * FROM positions WHERE status IN ('closed_target','closed_stop','closed_time')", conn
    )
    pending_count = conn.execute("SELECT COUNT(*) FROM positions WHERE status='pending'").fetchone()[0]

    locked_in_holdings = float((holding["buy_price"] * holding["quantity_bought"]).sum()) if not holding.empty else 0.0
    realized_pnl = float(((closed["exit_price"] - closed["buy_price"]) * closed["quantity_bought"]).sum()) if not closed.empty else 0.0

    cash = account_capital - locked_in_holdings + realized_pnl

    market_value = 0.0
    for _, pos in holding.iterrows():
        price = get_latest_price(conn, pos["symbol"]) or pos["buy_price"]
        market_value += price * pos["quantity_bought"]

    conn.close()

    equity = cash + market_value
    change = equity - account_capital
    change_pct = (change / account_capital * 100) if account_capital else 0.0

    return {
        "cash": round(cash, 2),
        "equity": round(equity, 2),
        "change": round(change, 2),
        "change_pct": round(change_pct, 2),
        "open_count": len(holding),
        "pending_count": pending_count,
        "max_positions": config.MAX_OPEN_POSITIONS,
    }
