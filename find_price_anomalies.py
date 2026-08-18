"""
myNSE200 / find_price_anomalies.py
-------------------------------------
Scans your database for suspicious single-day price jumps — the classic
signature of an unadjusted stock split or bonus issue (e.g. a 1:5 split
makes the price look like it crashed 80% overnight, when nothing was
actually lost). Use this to confirm/diagnose a suspicious backtest
drawdown, or just as a general data-quality check.

Usage:
    python3 find_price_anomalies.py                  # default 40% threshold
    python3 find_price_anomalies.py --threshold 30
    python3 find_price_anomalies.py --symbol JUBLFOOD  # check one symbol only
"""

import argparse

import pandas as pd

import config
import db


def find_anomalies(threshold_pct=40, symbol=None):
    conn = db.get_conn()
    query = "SELECT symbol, date, close FROM candle"
    params = []
    if symbol:
        query += " WHERE symbol = ?"
        params.append(symbol)
    query += " ORDER BY symbol, date"
    df = pd.read_sql_query(query, conn, params=params)
    conn.close()

    df["date"] = pd.to_datetime(df["date"])
    results = []
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("date").reset_index(drop=True)
        pct_change = g["close"].pct_change() * 100
        big_moves = pct_change[pct_change.abs() >= threshold_pct]
        for idx in big_moves.index:
            results.append({
                "symbol": sym,
                "date": g["date"].iloc[idx].date(),
                "prev_close": round(g["close"].iloc[idx - 1], 2),
                "close": round(g["close"].iloc[idx], 2),
                "pct_change": round(pct_change.iloc[idx], 1),
            })

    return pd.DataFrame(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold", type=float, default=40,
                         help="Flag any single-day close-to-close change bigger than this %% (default 40)")
    parser.add_argument("--symbol", type=str, default=None, help="Check only this symbol")
    args = parser.parse_args()

    print(f"Scanning for single-day price moves >= {args.threshold}% "
          f"({'symbol ' + args.symbol if args.symbol else 'all symbols'})...")
    anomalies = find_anomalies(args.threshold, args.symbol)

    if anomalies.empty:
        print("No suspicious jumps found at this threshold.")
    else:
        anomalies = anomalies.sort_values("pct_change")
        print(f"\nFound {len(anomalies)} suspicious single-day moves — these are almost always")
        print("unadjusted stock splits/bonus issues, not real price crashes:\n")
        print(anomalies.to_string(index=False))
        print("\nIf you see a symbol here that was open in a position around a bad backtest")
        print("drawdown date, that's very likely your culprit. Re-download with the fixed")
        print("data_fetch.py (auto_adjust=True) to resolve this — then re-run your backtest.")
