"""
myNSE200 / main.py
--------------------
Nightly entrypoint. This is the ONE script your Mac automation should call.

What it does, in order:
  1. Update recent OHLCV data (last few days) for the Nifty 200 universe.
  2. Refresh fundamentals (throttled — only for symbols overdue for a
     refresh, so the whole universe isn't re-scraped every single night).
  3. Run the Apex200 strategy: score + filter + trigger + trade plan.
  4. Write a CSV + HTML report to output/.
  5. Send a Telegram notification if configured.

Usage:
    python main.py                  # normal nightly run
    python main.py --full-history   # first-time setup: backfill 12y history
    python main.py --skip-fundamentals   # faster re-run using cached fundamentals
"""

import sys
import signal
import argparse
import logging
from datetime import datetime, timedelta

import pandas as pd

import config
import db
import data_fetch
import fundamentals
import strategy
import notifier
import portfolio

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("main")


def _symbols_overdue_for_fundamentals(conn, max_age_days=7):
    """Only re-scrape Screener.in for symbols we haven't refreshed recently —
    keeps nightly runs fast and polite to Screener's servers."""
    cutoff = (datetime.now() - timedelta(days=max_age_days)).isoformat()
    df = pd.read_sql_query(
        "SELECT symbol FROM fundamentals WHERE last_updated < ? OR last_updated IS NULL",
        conn, params=[cutoff],
    )
    return df["symbol"].tolist()


def run_nightly(full_history=False, skip_fundamentals=False, seed_days=None):
    started = datetime.now()
    log.info(f"===== myNSE200 nightly run started: {started} =====")

    db.init_db()

    symbols = data_fetch.load_universe_snapshot()
    symbols = [s for s in symbols if not data_fetch.is_bse_debt_instrument(s)]
    log.info(f"Universe size: {len(symbols)} symbols.")

    # 1. Price data
    if full_history:
        print("Backfilling full history (this can take a while the first time)...")
        data_fetch.fetch_all_history(symbols)
    elif seed_days:
        # For a FRESH, empty database that only needs to support LIVE
        # signals (not full backtesting) — e.g. a cloud-hosted copy that
        # shouldn't carry the full multi-year archive. ~500-700 days is
        # enough for every indicator this strategy uses (SMA200, ATR14,
        # 6-month momentum, etc.) without the size of a full backtest DB.
        print(f"Seeding {seed_days} days of history (one-time, for a fresh live-only database)...")
        data_fetch.fetch_recent(symbols, days=seed_days)
    else:
        data_fetch.fetch_recent(symbols, days=10)

    # 1b. Market regime index (Nifty 50 itself) — cheap, one ticker, always
    # refresh so the regime filter never runs on stale data. Skipped during
    # manual --skip-fundamentals runs to keep those fast and frozen, same as
    # fundamentals — only the real nightly run updates this.
    if not skip_fundamentals and config.USE_REGIME_FILTER:
        data_fetch.fetch_index_data()

    # 2. Fundamentals (throttled)
    if not skip_fundamentals:
        conn = db.get_conn()
        overdue = _symbols_overdue_for_fundamentals(conn)
        conn.close()
        # Symbols never fetched at all take priority; still cap the nightly
        # batch so we don't hammer Screener.in — the rest catch up on
        # subsequent nights.
        batch = overdue[:80] if overdue else []
        if batch:
            print(f"Refreshing fundamentals for {len(batch)} overdue symbols...")
            fundamentals.fetch_all(batch)
        else:
            print("Fundamentals are all fresh — skipping.")
    else:
        print("Skipping fundamentals refresh (--skip-fundamentals).")

    # 3. Strategy run
    # NOTE: run_date includes the time (not just the date) so that if you
    # ever run this more than once in the same day (manual test, retry after
    # a failure, etc.) the earlier report is kept as its own file instead of
    # being overwritten. Nothing else about the strategy logic changed.
    run_date = datetime.now().strftime("%Y-%m-%d_%H%M")
    report_df = strategy.run(symbols, run_date=run_date)

    if report_df.empty:
        log.warning("Strategy produced no report — check that data/fundamentals loaded correctly.")
        print("No report generated. Check run_log.txt for details.")
        return

    # 4. Write reports — one timestamped copy (kept forever, for history)
    # plus a "latest" copy (always overwritten, for convenience).
    csv_path = config.OUTPUT_DIR / f"apex200_{run_date}.csv"
    report_df.to_csv(csv_path, index=False)
    report_df.to_csv(config.OUTPUT_DIR / "apex200_latest.csv", index=False)

    html_path = config.OUTPUT_DIR / f"apex200_{run_date}.html"
    _write_html_report(report_df, run_date, html_path)
    _write_html_report(report_df, run_date, config.OUTPUT_DIR / "apex200_latest.html")

    buy_count = int(report_df["status"].sum())
    print(f"\nApex200 run complete for {run_date}.")
    print(f"  {buy_count} BUY signal(s) out of {len(report_df)} scored stocks.")
    print(f"  CSV:  {csv_path}")
    print(f"  HTML: {html_path}")
    print(f"  (Also updated: apex200_latest.csv / apex200_latest.html)")

    if buy_count:
        cols = ["symbol", "overall_score", "entry_price", "stop_loss", "target_price", "quantity", "order_value"]
        print("\n" + report_df[report_df["status"]][cols].to_string(index=False))

    # 5. Position tracking: auto-track today's new signals, check existing
    # holdings for target/stop/time exits, then send a simple Telegram
    # message. None of this affects what strategy.py decided — it's purely
    # tracking what happens next.
    # 5. Position tracking: check exits FIRST (frees up slots the same
    # night), then process today's signals capacity-aware — re-checking
    # any stocks still waiting for a slot before promoting or dropping
    # them, and only taking on brand-new signals if room remains. None of
    # this affects what strategy.py decided — purely what happens next.
    just_closed = portfolio.check_holding_exits()
    new_signals, dropped_stale, still_waiting = portfolio.process_signals_with_capacity(
        report_df, run_date, config.MAX_OPEN_POSITIONS
    )
    notifier.notify_signals(run_date, new_signals, just_closed, dropped_stale, still_waiting)

    # 6. Keep a plain, readable full history — every signal ever generated,
    # open or closed. Just open output/signal_history.csv anytime to see
    # the complete list; no database tools needed.
    count = portfolio.export_history_csv(config.OUTPUT_DIR / "signal_history.csv")
    log.info(f"Exported {count} total signal(s) to output/signal_history.csv")

    elapsed = (datetime.now() - started).total_seconds()
    log.info(f"===== nightly run finished in {elapsed:.1f}s. {buy_count} buy signals. =====")


def _write_html_report(report_df, run_date, path):
    buys = report_df[report_df["status"]]
    rest = report_df[~report_df["status"]]

    def table_html(df, cols):
        return df[cols].to_html(index=False, classes="tbl", border=0) if not df.empty else "<p>None.</p>"

    buy_cols = ["symbol", "overall_score", "entry_price", "buy_zone_low", "buy_zone_high",
                "stop_loss", "target_price", "risk_per_share", "quantity", "order_value"]
    buy_cols = [c for c in buy_cols if c in buys.columns]
    rest_cols = ["symbol", "overall_score", "reasons"]

    html = f"""
    <html><head><meta charset="utf-8"><title>Apex200 — {run_date}</title>
    <style>
      body {{ font-family: -apple-system, Arial, sans-serif; margin: 24px; color: #1a1a1a; }}
      h1 {{ font-size: 20px; }}
      .tbl {{ border-collapse: collapse; width: 100%; margin-bottom: 32px; font-size: 13px; }}
      .tbl th, .tbl td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: right; }}
      .tbl th {{ background: #f5f5f5; text-align: right; }}
      .tbl td:first-child, .tbl th:first-child {{ text-align: left; }}
      .disclaimer {{ font-size: 12px; color: #777; margin-top: 24px; }}
    </style></head><body>
    <h1>Apex200 — Nightly Report — {run_date}</h1>
    <h2>✅ Buy signals ({len(buys)})</h2>
    {table_html(buys, buy_cols)}
    <h2>Scored but not qualifying ({len(rest)})</h2>
    {table_html(rest, rest_cols)}
    <p class="disclaimer">
      Generated by myNSE200 / Apex200 strategy. This is a rules-based screening
      tool, not investment advice. Verify prices before placing any order.
      Paper trading mode: {config.PAPER_TRADING}.
    </p>
    </body></html>
    """
    path.write_text(html)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="myNSE200 nightly runner")
    parser.add_argument("--full-history", action="store_true", help="Backfill full history (first run)")
    parser.add_argument("--skip-fundamentals", action="store_true", help="Skip fundamentals refresh")
    parser.add_argument("--seed-days", type=int, default=None,
                         help="One-time larger backfill (e.g. 500) for a FRESH database that only "
                              "needs to support live signals, not full backtesting — smaller than "
                              "--full-history, enough for every live indicator to work correctly.")
    parser.add_argument("--max-runtime", type=int, default=None,
                         help="Override config.MAX_NIGHTLY_RUNTIME_SECONDS for this run "
                              "(--full-history ignores this cap — that one's meant to run long)")
    args = parser.parse_args()

    class NightlyTimeout(Exception):
        pass

    def _alarm_handler(signum, frame):
        raise NightlyTimeout()

    timeout_seconds = args.max_runtime or config.MAX_NIGHTLY_RUNTIME_SECONDS
    use_timeout = hasattr(signal, "SIGALRM") and not args.full_history

    if use_timeout:
        signal.signal(signal.SIGALRM, _alarm_handler)
        signal.alarm(timeout_seconds)

    try:
        run_nightly(full_history=args.full_history, skip_fundamentals=args.skip_fundamentals,
                    seed_days=args.seed_days)
        if use_timeout:
            signal.alarm(0)  # cancel — finished cleanly, no need to fire later
    except NightlyTimeout:
        log.error(f"Nightly run EXCEEDED the {timeout_seconds}s safety cap — aborting instead of "
                   f"hanging (this is what protects against an all-night run on a bad network).")
        print(f"ERROR: Run exceeded {timeout_seconds}s and was stopped. See run_log.txt.")
        try:
            # Best-effort short alert — the regular send_telegram already has
            # its own 15s timeout, so this can't itself hang the process.
            import notifier
            notifier.send_telegram(
                f"⚠️ Tonight's Apex200 run was stopped after exceeding {timeout_seconds // 60} "
                f"minutes (likely a network issue) — no report was completed. Check run_log.txt."
            )
        except Exception:
            pass
        sys.exit(1)
    except Exception as e:
        if use_timeout:
            signal.alarm(0)
        log.exception(f"Nightly run failed: {e}")
        print(f"ERROR: {e}\nSee run_log.txt for the full traceback.")
        sys.exit(1)
