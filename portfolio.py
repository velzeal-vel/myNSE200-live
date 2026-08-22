"""
myNSE200 / portfolio.py
--------------------------
Tracks what happens AFTER Apex200 generates a signal — this is a layer on
top of the strategy, not a change to it. strategy.py still decides what
qualifies exactly as before; this module just remembers it and watches
for the exit (target/stop/time).

SIMPLIFIED (Aug 2026): no more confirm/pending workflow — every new
signal is auto-tracked as 'holding' immediately, using the calculated
entry_price as the assumed buy price. No BUY/SKIP replies needed.

Statuses: holding -> closed_target / closed_stop / closed_time

Nothing here affects entry_price/stop_loss/target_price/quantity — those
still come entirely from strategy.py's calculation, unchanged.
"""

import logging
from datetime import datetime

import pandas as pd

import config
import db

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("portfolio")


def create_holdings_from_signals(report_df, run_date):
    """Takes today's strategy.run() output and auto-tracks every status=True
    signal as a new 'holding' immediately — using the calculated
    entry_price as the assumed buy price. Returns the list of newly
    tracked positions (as dicts) for the Telegram BUY SIGNAL section."""
    if report_df.empty:
        return []

    conn = db.get_conn()
    existing = pd.read_sql_query(
        "SELECT symbol FROM positions WHERE status = 'holding'", conn
    )["symbol"].tolist()

    new_signals = []
    buys = report_df[report_df["status"] == True]
    today = datetime.now().strftime("%Y-%m-%d")
    for _, row in buys.iterrows():
        if row["symbol"] in existing:
            continue  # already tracked, don't duplicate
        conn.execute(
            """INSERT INTO positions
               (symbol, status, signal_date, signal_price, stop_loss, target_price,
                quantity_recommended, quantity_bought, buy_price, buy_date, last_updated)
               VALUES (?, 'holding', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (row["symbol"], run_date, row["entry_price"], row["stop_loss"],
             row["target_price"], int(row["quantity"]), int(row["quantity"]),
             row["entry_price"], today, datetime.now().isoformat()),
        )
        new_signals.append({
            "symbol": row["symbol"], "entry_price": row["entry_price"],
            "stop_loss": row["stop_loss"], "target_price": row["target_price"],
        })
    conn.commit()
    conn.close()
    return new_signals


def get_latest_price(conn, symbol):
    row = conn.execute(
        "SELECT close FROM candle WHERE symbol=? ORDER BY date DESC LIMIT 1", (symbol,)
    ).fetchone()
    return row[0] if row else None


def check_holding_exits(max_holding_days=None):
    """For every currently-tracked position, checks today's price against
    its stop-loss, target, and the max-holding-days time exit — same
    three exit rules strategy.py's backtest logic uses. Returns the list
    of positions that just closed, for the Telegram EXIT section."""
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


def get_holding():
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT * FROM positions WHERE status='holding' ORDER BY buy_date DESC", conn
    )
    conn.close()
    return df


def export_history_csv(path):
    """Writes every signal ever generated — open or closed — to a plain,
    readable CSV. This is the full history you can just open and read,
    no database tools needed. Updates automatically every nightly run."""
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT signal_date AS date, symbol, signal_price AS entry, "
        "stop_loss AS sl, target_price AS target, status, "
        "exit_price, exit_date, exit_reason "
        "FROM positions ORDER BY signal_date DESC, id DESC", conn
    )
    conn.close()

    status_labels = {
        "holding": "Open — waiting for target/SL/time exit",
        "closed_target": "Closed — TARGET HIT",
        "closed_stop": "Closed — STOP HIT",
        "closed_time": "Closed — TIME EXIT (20-day limit)",
    }
    df["status"] = df["status"].map(status_labels).fillna(df["status"])
    df.to_csv(path, index=False)
    return len(df)
