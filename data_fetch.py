"""
myNSE200 / data_fetch.py
-------------------------
Universe loading + OHLCV historical/candle fetching via yfinance.
Batched, multithreaded downloads. Stores into SQLite (candle table).
"""

import re
import csv
import logging
import argparse
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yfinance as yf
import pandas as pd

import config
import db

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("data_fetch")


# ----------------------------------------------------------------------
# Universe
# ----------------------------------------------------------------------
def fetch_live_universe():
    """Try to pull the latest Nifty 200 list straight from NSE. Falls back
    to the bundled CSV snapshot if NSE blocks/errors (common without
    browser-like headers/cookies)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "Accept": "text/csv",
    }
    try:
        resp = requests.get(config.NIFTY200_LIVE_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        lines = resp.text.splitlines()
        reader = csv.DictReader(lines)
        symbols = [row["Symbol"].strip() for row in reader if row.get("Symbol")]
        if symbols:
            _save_universe_snapshot(symbols)
            return symbols
    except Exception as e:
        log.warning(f"Live universe fetch failed, using bundled snapshot: {e}")
    return load_universe_snapshot()


def _save_universe_snapshot(symbols):
    config.DATA_DIR.mkdir(exist_ok=True)
    with open(config.UNIVERSE_CSV, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Symbol"])
        for s in symbols:
            w.writerow([s])


def load_universe_snapshot():
    if not config.UNIVERSE_CSV.exists():
        raise FileNotFoundError(
            f"No universe snapshot at {config.UNIVERSE_CSV}. "
            "Run with --update-universe on a machine with NSE access, "
            "or manually create data/nifty200.csv with a 'Symbol' column."
        )
    with open(config.UNIVERSE_CSV) as f:
        reader = csv.DictReader(f)
        return [row["Symbol"].strip() for row in reader if row.get("Symbol")]


def to_yf_ticker(symbol, exchange="NSE"):
    suffix = config.EXCHANGE_SUFFIX.get(exchange, ".NS")
    return f"{symbol}{suffix}"


def is_bse_debt_instrument(name_or_symbol):
    return bool(re.search(config.BSE_DEBT_REGEX, name_or_symbol or ""))


# ----------------------------------------------------------------------
# OHLCV batch download
# ----------------------------------------------------------------------
def _download_batch(tickers, period, interval="1d"):
    """Download one batch via yfinance. Returns dict[ticker] -> DataFrame."""
    try:
        data = yf.download(
            tickers=tickers,
            period=period,
            interval=interval,
            group_by="ticker",
            threads=True,
            progress=False,
            auto_adjust=True,  # split/dividend-adjusted prices — unadjusted data makes stock splits
                               # look like fake overnight price crashes, corrupting backtest equity
                               # curves (found via a -93% "drawdown" in 3 weeks that didn't match any
                               # single real trade's loss — a split-adjustment bug, not real risk)
        )
    except Exception as e:
        log.error(f"Batch download failed for {tickers[:3]}...: {e}")
        return {}

    out = {}
    if len(tickers) == 1:
        out[tickers[0]] = data
    else:
        for t in tickers:
            try:
                out[t] = data[t]
            except Exception:
                continue
    return out


def _store_candles(conn, symbol, df):
    if df is None or df.empty:
        return 0
    rows = []
    for idx, row in df.iterrows():
        if any(row[c] != row[c] for c in ["Open", "High", "Low", "Close"]):  # NaN check
            continue
        rows.append((
            symbol,
            idx.strftime("%Y-%m-%d"),
            float(row["Open"]), float(row["High"]),
            float(row["Low"]), float(row["Close"]),
            int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
        ))
    if rows:
        conn.executemany(
            "INSERT OR REPLACE INTO candle (symbol, date, open, high, low, close, volume) "
            "VALUES (?,?,?,?,?,?,?)",
            rows,
        )
    return len(rows)


def fetch_all_history(symbols, years=None, max_workers=8):
    """Full historical backfill (run once, then use fetch_recent for nightly updates)."""
    years = years or config.HISTORY_YEARS
    period = f"{years}y"
    return _fetch(symbols, period=period, max_workers=max_workers)


def fetch_recent(symbols, days=10, max_workers=8):
    """Nightly incremental update — just the last N days."""
    return _fetch(symbols, period=f"{days}d", max_workers=max_workers)


def _fetch(symbols, period, max_workers=8):
    conn = db.get_conn()
    tickers = [to_yf_ticker(s) for s in symbols]
    batches = [tickers[i:i + config.BATCH_SIZE_YF] for i in range(0, len(tickers), config.BATCH_SIZE_YF)]

    total_rows = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_download_batch, batch, period): batch for batch in batches}
        for fut in as_completed(futures):
            batch_data = fut.result()
            for ticker, df in batch_data.items():
                symbol = ticker.replace(".NS", "").replace(".BO", "")
                n = _store_candles(conn, symbol, df)
                total_rows += n
                if n:
                    conn.execute(
                        "INSERT INTO instruments (symbol, exchange, yf_ticker, last_updated) "
                        "VALUES (?, 'NSE', ?, ?) "
                        "ON CONFLICT(symbol) DO UPDATE SET last_updated=excluded.last_updated",
                        (symbol, ticker, datetime.now().isoformat()),
                    )
    conn.commit()
    conn.close()
    log.info(f"Stored {total_rows} candle rows across {len(symbols)} symbols.")
    return total_rows


def fetch_index_data(index_ticker="^NSEI", period=None):
    """Downloads the Nifty 50 index itself (not a tradeable stock) for use
    as a market regime filter — see backtest.py's --regime-filter. Stored
    under symbol 'NIFTY50' in the candle table; load_all_candles()'s
    universe filter naturally excludes it from the tradeable universe since
    it won't appear in data/nifty200.csv.

    Defaults to period="max" (Yahoo's full available history for the index)
    rather than config.HISTORY_YEARS — an index ticker's available range on
    Yahoo doesn't necessarily match individual stocks', and under-fetching
    silently leaves early years with no regime data at all (found this way:
    two folds showed byte-identical results with/without the filter, which
    turned out to mean the index data simply didn't reach back that far)."""
    period = period or "max"
    conn = db.get_conn()
    try:
        data = yf.download(tickers=index_ticker, period=period, interval="1d",
                            progress=False, auto_adjust=True)
    except Exception as e:
        log.error(f"Index fetch failed: {e}")
        print(f"  Index fetch failed with an error: {e}")
        return 0
    if data.empty:
        log.error("Index fetch returned no data.")
        print("  Index fetch returned no data at all — check your internet connection "
              "or try again; NSE/Yahoo endpoints occasionally rate-limit.")
        return 0

    # yfinance sometimes returns MultiIndex columns (ticker, field) even for a
    # single-ticker download, depending on version — flatten to plain field
    # names ("Open", "Close", etc.) so the row access below works either way.
    if isinstance(data.columns, pd.MultiIndex):
        data.columns = data.columns.get_level_values(0)

    required_cols = {"Open", "High", "Low", "Close", "Volume"}
    missing = required_cols - set(data.columns)
    if missing:
        print(f"  Index fetch got unexpected columns: {list(data.columns)} — missing {missing}.")
        print("  This is a data-format issue, not a network issue. Reporting so it's never silent.")
        return 0

    rows = []
    skipped = 0
    for idx, row in data.iterrows():
        try:
            rows.append((
                "NIFTY50", idx.strftime("%Y-%m-%d"),
                float(row["Open"]), float(row["High"]),
                float(row["Low"]), float(row["Close"]),
                int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
            ))
        except Exception as e:
            skipped += 1
            continue

    if not rows:
        print(f"  Parsed 0 usable rows out of {len(data)} fetched (all {skipped} failed to parse). "
              f"Sample of raw data:\n{data.head(3)}")
        return 0
    if skipped:
        print(f"  Note: {skipped} of {len(data)} rows failed to parse and were skipped.")

    conn.executemany(
        "INSERT OR REPLACE INTO candle (symbol, date, open, high, low, close, volume) "
        "VALUES (?,?,?,?,?,?,?)", rows,
    )
    conn.commit()
    conn.close()
    log.info(f"Stored {len(rows)} NIFTY50 index rows for regime filtering.")
    first_date = min(r[1] for r in rows)
    last_date = max(r[1] for r in rows)
    print(f"  Index data covers {first_date} to {last_date} ({len(rows)} rows). "
          f"Any backtest date range OUTSIDE this window gets NO regime filtering "
          f"for those dates — check this covers what you intend to test.")
    return len(rows)
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--update-universe", action="store_true", help="Refresh Nifty200 list from NSE")
    parser.add_argument("--full-history", action="store_true", help="Backfill full N-year history")
    parser.add_argument("--recent", action="store_true", help="Fetch only recent days (nightly use)")
    parser.add_argument("--days", type=int, default=10)
    parser.add_argument("--fetch-index", action="store_true",
                         help="Download the Nifty 50 INDEX itself (not a tradeable stock) for use "
                              "as a market regime filter in backtest.py --regime-filter")
    args = parser.parse_args()

    db.init_db()

    if args.fetch_index:
        n = fetch_index_data()
        print(f"Index data stored: {n} rows.")

    if args.update_universe:
        syms = fetch_live_universe()
        print(f"Universe updated: {len(syms)} symbols.")
    else:
        syms = load_universe_snapshot()

    syms = [s for s in syms if not is_bse_debt_instrument(s)]

    if args.full_history:
        n = fetch_all_history(syms)
        print(f"Full history stored: {n} rows.")
    elif args.recent:
        n = fetch_recent(syms, days=args.days)
        print(f"Recent data stored: {n} rows.")
    else:
        print("Nothing to do. Use --full-history, --recent, or --update-universe.")
