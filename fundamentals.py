"""
myNSE200 / fundamentals.py
----------------------------
Fetches fundamental data from Screener.in:
P/E, promoter holding, FII holding, DII holding, 3y sales growth,
3y profit growth, pledged shares.

NOTE: Screener.in has no official public API — this scrapes their public
pages. Be respectful: keep request rates low, cache results, and re-check
their terms of use periodically. If you have a Screener.in premium account,
consider using their export feature instead for more reliable data.
"""

import re
import time
import logging
from datetime import datetime

import requests
from bs4 import BeautifulSoup

import config
import db

logging.basicConfig(
    filename=config.LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("fundamentals")

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
SCREENER_URL = "https://www.screener.in/company/{symbol}/"
REQUEST_DELAY_SEC = 1.5  # be polite


def _parse_number(text):
    if text is None:
        return None
    text = text.strip().replace(",", "").replace("%", "")
    try:
        return float(text)
    except ValueError:
        return None


def fetch_one(symbol):
    """Scrape a single symbol's fundamentals page. Returns a dict or None."""
    url = SCREENER_URL.format(symbol=symbol)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        if resp.status_code != 200:
            log.warning(f"{symbol}: HTTP {resp.status_code}")
            return None
        soup = BeautifulSoup(resp.text, "html.parser")

        result = {
            "symbol": symbol,
            "pe_ratio": None,
            "promoter_holding": None,
            "fii_holding": None,
            "dii_holding": None,
            "sales_growth_3y": None,
            "profit_growth_3y": None,
            "pledged_pct": None,
            "pledge_known": False,
            "growth_known": False,
        }

        # Top ratios block (P/E lives here)
        for li in soup.select("#top-ratios li"):
            name_el = li.select_one(".name")
            val_el = li.select_one(".value")
            if not name_el or not val_el:
                continue
            name = name_el.text.strip().lower()
            val = _parse_number(val_el.text)
            if "stock p/e" in name:
                result["pe_ratio"] = val

        # Shareholding pattern table — look for the latest quarter column
        shp_table = soup.find("section", {"id": "shareholding"})
        if shp_table:
            rows = shp_table.select("table tbody tr")
            for row in rows:
                cells = row.find_all("td")
                if not cells:
                    continue
                label = cells[0].text.strip().lower()
                latest_val = _parse_number(cells[-1].text)
                if "promoter" in label:
                    result["promoter_holding"] = latest_val
                elif "fii" in label or "foreign institutions" in label:
                    result["fii_holding"] = latest_val
                elif "dii" in label or "domestic institutions" in label:
                    result["dii_holding"] = latest_val
                elif "pledge" in label:
                    result["pledged_pct"] = latest_val if latest_val is not None else 0.0
                    result["pledge_known"] = latest_val is not None

        # Growth figures — sales/profit 3yr CAGR from the "Compounded" tables
        for section_title, key in [("sales growth", "sales_growth_3y"), ("profit growth", "profit_growth_3y")]:
            header = soup.find(string=re.compile(section_title, re.I))
            if header:
                container = header.find_parent("table")
                if container:
                    for row in container.select("tr"):
                        cells = row.find_all("td")
                        if len(cells) >= 2 and "3 year" in cells[0].text.lower():
                            val = _parse_number(cells[-1].text)
                            if val is not None:
                                result[key] = val
                                result["growth_known"] = True

        # If the shareholding table was found and successfully parsed (proven
        # by promoter_holding being populated) but NO pledge row appeared,
        # that means zero pledged shares — Screener only shows a pledge line
        # when there's something to disclose. Treat this as a confirmed
        # zero, not "unknown". This was a real bug: it was disqualifying
        # nearly every clean company (200/200 showing "unknown pledge" in
        # practice) by conflating "nothing to report" with "couldn't check".
        # Only genuinely missing/unparseable data stays unknown.
        if result["pledged_pct"] is None and shp_table and result["promoter_holding"] is not None:
            result["pledged_pct"] = 0.0
            result["pledge_known"] = True
        elif result["pledged_pct"] is None:
            result["pledge_known"] = False

        return result

    except Exception as e:
        log.error(f"{symbol}: fundamentals fetch failed: {e}")
        return None


def fetch_all(symbols, delay=REQUEST_DELAY_SEC):
    conn = db.get_conn()
    fetched = 0
    for symbol in symbols:
        data = fetch_one(symbol)
        time.sleep(delay)
        if data is None:
            # Missing entirely -> neutral / unknown, per your rule:
            # "If growth data is missing, treat it as neutral (partial Z-score)"
            conn.execute(
                "INSERT OR REPLACE INTO fundamentals "
                "(symbol, pledge_known, growth_known, last_updated) VALUES (?, 0, 0, ?)",
                (symbol, datetime.now().isoformat()),
            )
            continue
        conn.execute(
            """INSERT OR REPLACE INTO fundamentals
               (symbol, pe_ratio, promoter_holding, fii_holding, dii_holding,
                sales_growth_3y, profit_growth_3y, pledged_pct, pledge_known,
                growth_known, last_updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data["symbol"], data["pe_ratio"], data["promoter_holding"],
                data["fii_holding"], data["dii_holding"], data["sales_growth_3y"],
                data["profit_growth_3y"], data["pledged_pct"],
                int(data["pledge_known"]), int(data["growth_known"]),
                datetime.now().isoformat(),
            ),
        )
        fetched += 1
    conn.commit()
    conn.close()
    log.info(f"Fundamentals updated for {fetched}/{len(symbols)} symbols.")
    return fetched


if __name__ == "__main__":
    import data_fetch
    db.init_db()
    syms = data_fetch.load_universe_snapshot()
    n = fetch_all(syms)
    print(f"Fundamentals fetched for {n} symbols.")
