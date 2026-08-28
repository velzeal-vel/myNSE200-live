"""
myNSE200 / portfolio.py
--------------------------
Tracks what happens AFTER Apex200 generates a signal — this is a layer on
top of the strategy, not a change to it. strategy.py still decides what
qualifies exactly as before; this module just remembers it and watches
for the exit (target/stop/time).

SIMPLIFIED (Aug 2026): no confirm/BUY-SKIP-reply workflow — signals are
auto-tracked. UPDATED (Aug 2026): now capacity-aware — when the portfolio
is at MAX_OPEN_POSITIONS, new qualifying signals wait as 'pending' rather
than being silently dropped or double-counted, and get re-validated
against fresh scoring every night before actually being bought.

Statuses: pending (waiting for a slot) -> holding -> closed_target /
          closed_stop / closed_time
          pending -> dropped_stale (no longer qualifies once re-checked)

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


def process_signals_with_capacity(report_df, run_date, max_positions=None):
    """The capacity-aware version: never lets total open positions exceed
    max_positions. Order of priority each night:
      1. Existing 'pending' signals (waiting for a slot) get RE-CHECKED
         against tonight's fresh scoring — if they no longer qualify,
         they're dropped (not silently bought stale later). If they still
         qualify and a slot is free, they get promoted using TONIGHT's
         fresh entry/stop/target, not the old numbers from when they
         first signaled.
      2. Brand-new signals from tonight then fill any remaining slots.
      3. Anything left over (still qualifying, still no room) stays/goes
         'pending' for another night.

    Returns (new_buys, dropped_stale, still_waiting) — new_buys is what
    just became actionable (promoted or brand-new with room), for the
    BUY SIGNAL section; dropped_stale and still_waiting are for their own
    message sections."""
    max_positions = max_positions or config.MAX_OPEN_POSITIONS
    conn = db.get_conn()
    today = datetime.now().strftime("%Y-%m-%d")

    holding_count = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status='holding'"
    ).fetchone()[0]

    qualifies_today = {}
    if not report_df.empty:
        for _, row in report_df[report_df["status"] == True].iterrows():
            qualifies_today[row["symbol"]] = row

    new_buys, dropped_stale, still_waiting = [], [], []

    # 1. Re-check existing pending signals first, oldest first (fair order)
    pending = pd.read_sql_query(
        "SELECT * FROM positions WHERE status='pending' ORDER BY signal_date ASC, id ASC", conn
    )
    for _, pos in pending.iterrows():
        if pos["symbol"] in qualifies_today:
            row = qualifies_today.pop(pos["symbol"])  # also removes it from "brand new" pool
            if holding_count < max_positions:
                conn.execute(
                    "UPDATE positions SET status='holding', signal_price=?, stop_loss=?, "
                    "target_price=?, quantity_recommended=?, quantity_bought=?, buy_price=?, "
                    "buy_date=?, last_updated=? WHERE id=?",
                    (row["entry_price"], row["stop_loss"], row["target_price"],
                     int(row["quantity"]), int(row["quantity"]), row["entry_price"],
                     today, datetime.now().isoformat(), pos["id"]),
                )
                holding_count += 1
                new_buys.append({"symbol": pos["symbol"], "entry_price": row["entry_price"],
                                  "stop_loss": row["stop_loss"], "target_price": row["target_price"]})
            else:
                still_waiting.append(pos["symbol"])
        else:
            conn.execute(
                "UPDATE positions SET status='dropped_stale', last_updated=? WHERE id=?",
                (datetime.now().isoformat(), pos["id"]),
            )
            dropped_stale.append(pos["symbol"])

    # 2. Brand-new signals from tonight (whatever's left in qualifies_today
    # after pending symbols were already popped out above)
    existing_tracked = pd.read_sql_query(
        "SELECT symbol FROM positions WHERE status IN ('holding', 'pending')", conn
    )["symbol"].tolist()

    for symbol, row in qualifies_today.items():
        if symbol in existing_tracked:
            continue  # already tracked from an earlier night, don't duplicate
        if holding_count < max_positions:
            conn.execute(
                """INSERT INTO positions
                   (symbol, status, signal_date, signal_price, stop_loss, target_price,
                    quantity_recommended, quantity_bought, buy_price, buy_date, last_updated)
                   VALUES (?, 'holding', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (symbol, run_date, row["entry_price"], row["stop_loss"], row["target_price"],
                 int(row["quantity"]), int(row["quantity"]), row["entry_price"], today,
                 datetime.now().isoformat()),
            )
            holding_count += 1
            new_buys.append({"symbol": symbol, "entry_price": row["entry_price"],
                              "stop_loss": row["stop_loss"], "target_price": row["target_price"]})
        else:
            conn.execute(
                """INSERT INTO positions
                   (symbol, status, signal_date, signal_price, stop_loss, target_price,
                    quantity_recommended, last_updated)
                   VALUES (?, 'pending', ?, ?, ?, ?, ?, ?)""",
                (symbol, run_date, row["entry_price"], row["stop_loss"], row["target_price"],
                 int(row["quantity"]), datetime.now().isoformat()),
            )
            still_waiting.append(symbol)

    conn.commit()
    conn.close()
    return new_buys, dropped_stale, still_waiting


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


def get_pending():
    conn = db.get_conn()
    df = pd.read_sql_query(
        "SELECT * FROM positions WHERE status='pending' ORDER BY signal_date ASC", conn
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
        "pending": "Pending — waiting for a portfolio slot to open",
        "dropped_stale": "Dropped — no longer qualified when re-checked",
    }
    df["status"] = df["status"].map(status_labels).fillna(df["status"])
    df.to_csv(path, index=False)
    return len(df)
