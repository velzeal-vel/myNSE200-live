"""
myNSE200 / test_capacity_scenario.py
---------------------------------------
A SAFE, one-time test: temporarily fills the portfolio to capacity with
clearly-fake test positions (prefixed TESTPOS), simulates a realistic
signal night (some still qualify, one doesn't, one is brand new), sends
you a REAL Telegram message so you can see exactly what the full
waiting-list / stale-drop behavior looks like — then immediately deletes
every test row it created. Your real tracked positions are never touched.

Run this manually, once, whenever you want to see this feature in
action without waiting for your portfolio to naturally fill up.
"""

from datetime import datetime

import pandas as pd

import config
import db
import notifier
import portfolio

TEST_PREFIX = "TESTPOS"


def run_test():
    db.init_db()
    conn = db.get_conn()
    inserted_ids = []

    print("Setting up a temporary full-portfolio scenario (20/20)...")
    for i in range(config.MAX_OPEN_POSITIONS):
        cur = conn.execute(
            """INSERT INTO positions (symbol, status, signal_date, signal_price, stop_loss,
               target_price, quantity_recommended, quantity_bought, buy_price, buy_date, last_updated)
               VALUES (?, 'holding', ?, 100.0, 90.0, 120.0, 5, 5, 100.0, ?, ?)""",
            (f"{TEST_PREFIX}{i}", "2026-01-01", "2026-01-01", datetime.now().isoformat()),
        )
        inserted_ids.append(cur.lastrowid)

    # One stock that was already waiting from a previous night
    cur = conn.execute(
        """INSERT INTO positions (symbol, status, signal_date, signal_price, stop_loss,
           target_price, quantity_recommended, last_updated)
           VALUES (?, 'pending', ?, 500.0, 470.0, 560.0, 10, ?)""",
        (f"{TEST_PREFIX}WAITING", "2026-01-05", datetime.now().isoformat()),
    )
    inserted_ids.append(cur.lastrowid)
    conn.commit()

    print("Simulating tonight's signals: the waiting stock still qualifies, "
          "plus one brand-new signal shows up too...")
    report_df = pd.DataFrame([
        {"symbol": f"{TEST_PREFIX}WAITING", "status": True, "entry_price": 505.0,
         "stop_loss": 475.0, "target_price": 565.0, "quantity": 10},
        {"symbol": f"{TEST_PREFIX}NEW", "status": True, "entry_price": 200.0,
         "stop_loss": 190.0, "target_price": 220.0, "quantity": 20},
    ])

    new_buys, dropped_stale, still_waiting = portfolio.process_signals_with_capacity(
        report_df, "TEST_RUN_" + datetime.now().strftime("%Y-%m-%d_%H%M"),
        max_positions=config.MAX_OPEN_POSITIONS,
    )

    # Find any newly-created pending row(s) too, so we clean those up as well
    conn2 = db.get_conn()
    extra = conn2.execute(
        "SELECT id FROM positions WHERE symbol LIKE ? AND id NOT IN ({})".format(
            ",".join("?" * len(inserted_ids)) or "0"
        ), (f"{TEST_PREFIX}%", *inserted_ids)
    ).fetchall()
    conn2.close()
    inserted_ids.extend(row[0] for row in extra)

    print(f"\nResult — with the portfolio full (20/20), the brand-new signal "
          f"({TEST_PREFIX}NEW) should be held back, NOT bought:")
    print(f"  New buys (should be empty, since no slot was free): {[s['symbol'] for s in new_buys]}")
    print(f"  Still waiting (should include both test stocks): {still_waiting}")
    print(f"  Dropped stale: {dropped_stale}")

    print("\nSending you a real Telegram message showing this scenario...")
    message = notifier.build_signal_message(
        "TEST — capacity scenario (safe to ignore, cleanup runs automatically)",
        new_buys, [], dropped_stale, still_waiting,
    )
    notifier.send_telegram(message)

    print("\nCleaning up ALL test rows now — your real positions are untouched...")
    conn = db.get_conn()
    conn.execute(
        "DELETE FROM positions WHERE id IN ({})".format(",".join("?" * len(inserted_ids))),
        inserted_ids,
    )
    conn.commit()
    conn.close()
    print(f"Removed {len(inserted_ids)} test row(s). Done — check your Telegram.")


if __name__ == "__main__":
    run_test()
