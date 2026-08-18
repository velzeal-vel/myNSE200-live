"""
myNSE200 / config.py
---------------------
Central configuration for the Apex200 nightly strategy.

IMPORTANT (read this first):
This is a rules-based screening + risk-management tool, NOT a guarantee of
profit. Markets carry risk of loss. Every threshold below is a starting
point you should review and adjust to your own risk tolerance before
trading real money. Nothing here is investment advice.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass  # python-dotenv not installed — fall back to real environment variables only

# ----------------------------------------------------------------------
# Paths
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
OUTPUT_DIR = BASE_DIR / "output"
DB_PATH = BASE_DIR / "market_data.db"
UNIVERSE_CSV = DATA_DIR / "nifty200.csv"
LOG_FILE = BASE_DIR / "run_log.txt"

DATA_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# ----------------------------------------------------------------------
# Universe
# ----------------------------------------------------------------------
# Which NSE index to use as your trading universe. Nifty 200 already
# includes midcaps (Nifty 100 large-cap + Nifty Midcap 100) — if you want
# to go further, Nifty 500 adds smallcaps too. Broader universe = more
# growth potential AND more volatility/lower liquidity — untested until
# you actually backtest it, don't assume it's automatically better.
UNIVERSE_INDEX = "nifty200"   # options: "nifty200", "nifty500", "niftymidcap150", "niftysmallcap100"
# REVERTED (Aug 2026): tested "nifty500" — failed out-of-sample badly (-7.24% CAGR,
# -51.12% drawdown over the most recent 3 years, vs +19.7%/-43.49% in training).
# The out-of-sample window captured the 2023-2024 mid/small-cap boom followed by the
# severe Sep 2024-Feb 2025 correction — real evidence broadening the universe increased
# regime risk here, not just noise. Stick with nifty200 (already includes Nifty Midcap
# 100) unless a future, more careful test says otherwise.

UNIVERSE_INDEX_URLS = {
    "nifty50": "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
    "nifty100": "https://archives.nseindia.com/content/indices/ind_nifty100list.csv",
    "nifty200": "https://archives.nseindia.com/content/indices/ind_nifty200list.csv",
    "nifty500": "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
    "niftymidcap150": "https://archives.nseindia.com/content/indices/ind_niftymidcap150list.csv",
    "niftysmallcap100": "https://archives.nseindia.com/content/indices/ind_niftysmallcap100list.csv",
}

# Live source (kept for backward compatibility — derived from UNIVERSE_INDEX above).
# If this fetch fails (NSE blocks a lot of non-browser traffic), we fall back to the
# bundled data/nifty200.csv snapshot. Run `python data_fetch.py --update-universe`
# periodically (monthly) to refresh the snapshot.
NIFTY200_LIVE_URL = UNIVERSE_INDEX_URLS.get(UNIVERSE_INDEX, UNIVERSE_INDEX_URLS["nifty200"])

EXCHANGE_SUFFIX = {
    "NSE": ".NS",
    "BSE": ".BO",
}

# Exclude BSE bond/debt instruments (from your old rule set)
BSE_DEBT_REGEX = r"(?i)(NCD|BOND|DEBT|SDL|GSEC|TBILL)"

# ----------------------------------------------------------------------
# Historical data
# ----------------------------------------------------------------------
HISTORY_YEARS = 12
SCORING_LOOKBACK_DAYS = 450   # only load recent window for scoring, per perf rules
BATCH_SIZE_YF = 60            # symbols per batch, multithreaded

# ----------------------------------------------------------------------
# Fundamental filter thresholds (★ Recommended badge — from your old rules)
# ----------------------------------------------------------------------
MAX_PROMOTER_HOLDING = 40.0     # promoter holding must be < this
MIN_FII_PLUS_DII = 25.0         # FII% + DII% must be >= this
MAX_PLEDGE_PCT = 0.0            # pledged shares must equal this (0 = none)
ALLOW_UNKNOWN_PLEDGE = False    # unknown pledge data disqualifies (per old rule)

# ----------------------------------------------------------------------
# Scoring weights (Base score = 100)
# ----------------------------------------------------------------------
SCORE_WEIGHTS = {
    "momentum": 0.35,
    "trend": 0.20,
    "quality": 0.15,
    "growth": 0.30,
}

# ----------------------------------------------------------------------
# Apex200 strategy — entry trigger / risk management
# ----------------------------------------------------------------------
# This is the NEW strategy layer. It sits on top of the score + recommendation
# filter and decides which names are actually "actionable" tomorrow, plus the
# exact entry/target/stop-loss/quantity for each.

STRATEGY_NAME = "Apex200"

# --- Entry trigger (technical confirmation, checked on last completed candle) ---
REQUIRE_PRICE_ABOVE_SMA20 = True
REQUIRE_PRICE_ABOVE_SMA50 = True
MIN_VOLUME_SURGE_RATIO = 1.5
# VALIDATED (Aug 2026): a market regime filter — only take new trades when
# the Nifty 50 index itself is above its own 200-day average — was tested
# after noticing the strategy's worst periods (2008 crisis, 2009-2013,
# 2016-2019) all coincided with weak broader-market conditions. Result:
# clean pass on both a 6-fold test AND a genuine out-of-sample split.
# Training: 19.27% CAGR / -29.73% drawdown (vs 17.92%/-52.83% without the
# filter — better on both). Out-of-sample: 22.67% CAGR / -22.21% drawdown
# (vs 22.28%/-25.91% without — similar CAGR, better drawdown). The 2008
# crisis fold specifically improved from -52.83% to -16.84% drawdown.
USE_REGIME_FILTER = True
REGIME_SMA_PERIOD = 200        # today's volume >= 1.5x the 20-day avg volume
# VALIDATED (Aug 2026): raised from 80 to 90 after a real finding — testing 80/90/95
# across 6 independent 20-year folds showed 90 as the genuine CAGR peak (avg 21.66%
# vs 18.49% at 80), while ALSO improving worst-case drawdown (-52.83% vs -58.58%).
# Pushing to 95 traded CAGR away for extra safety instead of improving both, which is
# how we know 90 is the peak rather than "higher is always better." Confirmed on a
# genuine out-of-sample split (5 years never used for tuning): both CAGR and drawdown
# held up or improved out-of-sample (22.28% CAGR, -25.91% DD vs 17.92%/-52.83% training).
MIN_SCORE_PERCENTILE = 90           # only top 10% of scored universe qualify

# --- Risk management ---
# VALIDATED (Aug 2026): tested against 12 years of NSE data, 30+ combinations
# tried, this setting confirmed on a genuine out-of-sample split (settings
# tuned on 9 years, tested clean on 3 years never used for tuning) —
# out-of-sample win rate, CAGR, AND drawdown all held up or improved versus
# the training period. See output/backtest_trades.csv for the full trade
# log this was validated against. Past performance still doesn't guarantee
# future results — re-validate periodically as more data accumulates.
ATR_PERIOD = 14
ATR_STOP_MULTIPLIER = 3.0           # stop loss = entry - 3.0 * ATR14 (wider stop, fewer whipsaws)
REWARD_RISK_RATIO = 1.0             # target = entry + 1.0 * risk (was 3:1 — 1:1 tested meaningfully better)
USE_SWING_LOW_STOP_IF_TIGHTER = True  # use max(ATR stop, 10-day swing low) as the actual stop
SWING_LOW_LOOKBACK_DAYS = 10

# --- Position sizing (defaults reused from your old trading-safety rules) ---
# --- Position sizing ---
# VALIDATED alongside the stop/target settings above. Compounding (sizing
# off current capital rather than a number frozen at setup) is what made
# the out-of-sample result hold up — but this system can't see your real
# brokerage balance automatically. To get the benefit of compounding, you
# need to manually update ACCOUNT_CAPITAL below periodically (e.g. monthly)
# to reflect your actual current portfolio value. If you never update it,
# you're effectively running the older, non-compounding version.
ACCOUNT_CAPITAL = 10000.0           # <-- UPDATE THIS regularly to your real current capital (INR)
RISK_PER_TRADE_PCT = 1.0            # risk 1% of capital per trade — tested; raising this did NOT
                                     # reliably improve results, only worsened drawdown, so left as-is
MAX_PER_ORDER_PCT_OF_EQUITY = 20.0  # no single position over 20% of ACCOUNT_CAPITAL — replaces the
                                     # old flat rupee cap, which was silently capping most trades
                                     # regardless of account size (see README for why this changed)
MAX_ORDERS_PER_DAY = 20             # hard cap, from old safety rules
MAX_OPEN_POSITIONS = 20

# --- Holding / exit ---
MAX_HOLDING_DAYS = 20               # time-based exit if neither target nor stop hit

# ----------------------------------------------------------------------
# Reliability — nightly run safety cap
# ----------------------------------------------------------------------
# A normal run takes a few minutes. Added after a real bad-network night
# where fundamentals fetching + Telegram retries dragged the run out to
# 6+ hours (each individual request has its own short timeout, but many
# sequential retries across ~80 symbols can still add up badly). This is
# a hard ceiling on the WHOLE run — if exceeded, it aborts cleanly, logs
# clearly, and tries a brief Telegram alert instead of running all night.
MAX_NIGHTLY_RUNTIME_SECONDS = 900   # 15 minutes — generous vs a normal
                                     # few-minute run, tight vs an all-night hang
TRAIL_STOP_AFTER_R_MULTIPLE = 1.5   # once price moves 1.5R in favor, trail stop to breakeven

# ----------------------------------------------------------------------
# Costs (used only if you later wire this into the backtester — kept for parity
# with your old backtesting rules; not applied to live entry/target calc)
# ----------------------------------------------------------------------
BROKERAGE_PCT = 0.0003
STT_PCT = 0.001
EXCHANGE_CHARGE_PCT = 0.0000345
SEBI_CHARGE_PCT = 0.0000010
GST_PCT = 0.18
STAMP_DUTY_PCT = 0.00015
DP_CHARGES_FLAT = 13.5

# ----------------------------------------------------------------------
# Live trading safety (paper trading is ALWAYS the default — from old rules)
# ----------------------------------------------------------------------
PAPER_TRADING = os.environ.get("MYNSE200_PAPER_TRADING", "true").lower() != "false"
ARM_CONFIRMATION_PHRASE = "I CONFIRM LIVE TRADING"
ARM_EXPIRY_MINUTES = 15

# ----------------------------------------------------------------------
# Notifications (optional — leave blank to disable)
# ----------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("MYNSE200_TG_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("MYNSE200_TG_CHAT_ID", "")
EMAIL_TO = os.environ.get("MYNSE200_EMAIL_TO", "")
