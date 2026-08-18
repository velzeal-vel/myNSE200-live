"""
myNSE200 / screen.py
----------------------
Manual/ad-hoc screening filters (price range, P/E, min FII, min DII,
promoter holding) — for when you want to explore the universe outside
of the nightly Apex200 run. Reuses the same DB the nightly job writes to.
"""

import pandas as pd
import db


def screen(
    price_min=None,
    price_max=None,
    pe_max=None,
    min_fii=None,
    min_dii=None,
    max_promoter=None,
    min_promoter=None,
    run_date=None,
):
    conn = db.get_conn()

    latest_scores = pd.read_sql_query(
        """
        SELECT s.* FROM scores s
        INNER JOIN (
            SELECT symbol, MAX(run_date) AS max_date FROM scores GROUP BY symbol
        ) latest ON s.symbol = latest.symbol AND s.run_date = latest.max_date
        """,
        conn,
    ) if run_date is None else pd.read_sql_query(
        "SELECT * FROM scores WHERE run_date = ?", conn, params=[run_date]
    )

    fundamentals = pd.read_sql_query("SELECT * FROM fundamentals", conn)

    latest_close = pd.read_sql_query(
        """
        SELECT c.symbol, c.close FROM candle c
        INNER JOIN (
            SELECT symbol, MAX(date) AS max_date FROM candle GROUP BY symbol
        ) latest ON c.symbol = latest.symbol AND c.date = latest.max_date
        """,
        conn,
    )
    conn.close()

    df = latest_scores.merge(fundamentals, on="symbol", how="left").merge(latest_close, on="symbol", how="left")

    if price_min is not None:
        df = df[df["close"] >= price_min]
    if price_max is not None:
        df = df[df["close"] <= price_max]
    if pe_max is not None:
        df = df[df["pe_ratio"].notna() & (df["pe_ratio"] <= pe_max)]
    if min_fii is not None:
        df = df[df["fii_holding"].fillna(0) >= min_fii]
    if min_dii is not None:
        df = df[df["dii_holding"].fillna(0) >= min_dii]
    if max_promoter is not None:
        df = df[df["promoter_holding"].notna() & (df["promoter_holding"] <= max_promoter)]
    if min_promoter is not None:
        df = df[df["promoter_holding"].fillna(0) >= min_promoter]

    return df.sort_values("overall_score", ascending=False).reset_index(drop=True)


if __name__ == "__main__":
    result = screen(pe_max=25, min_fii=5, max_promoter=40)
    print(result[["symbol", "close", "pe_ratio", "promoter_holding", "fii_holding", "overall_score"]].head(25).to_string(index=False))
