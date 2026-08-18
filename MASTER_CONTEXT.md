# myNSE200 / Apex200 — Master Context Document

Paste or upload this file at the start of a new chat, along with your current
`myNSE200` folder (or mention you have it), to resume exactly where this
conversation left off — no code or decisions lost.

## What this project is

A personal, rules-based NSE stock screening and trade-planning system for
Velayutham. Runs nightly, scores ~200 stocks (Nifty 200), and produces a
buy list with exact entry/stop-loss/target/quantity for each qualifying
stock. NOT investment advice, NOT a guarantee of profit — a disciplined,
evidence-tested tool. Paper/manual execution only — does not place live
broker orders.

## Environment

- Mac (primary) at `/Users/velayutham.saravanan/Documents/myNSE200`, also
  set up on a Windows laptop (same code, `venv\Scripts\activate` + `.bat`
  launcher instead of `.command`)
- Python 3, venv-based, `pip install -r requirements.txt`
- SQLite database (`market_data.db`), WAL mode
- Telegram bot configured for nightly push notifications (`.env` file,
  `MYNSE200_TG_TOKEN` / `MYNSE200_TG_CHAT_ID` — token has been rotated at
  least once already after being exposed in chat; if debugging Telegram
  again, get a FRESH token via BotFather `/mybots`, don't reuse old ones)
- `launchd` (Mac) / Task Scheduler (Windows) runs `main.py` nightly at 23:30
- `Run_myNSE200.command` (Mac) / `Run_myNSE200.bat` (Windows): double-click
  manual-run launchers that call `main.py --skip-fundamentals` (daytime
  manual checks intentionally skip the fundamentals refresh so results stay
  frozen and identical between checks — only the real nightly run updates
  fundamentals)

## File structure

```
myNSE200/
├── config.py           # ALL settings — current validated values above
├── db.py                 # SQLite schema, WAL mode, 30s busy timeout
├── data_fetch.py          # Nifty200 universe + OHLCV + NIFTY50 index download (yfinance)
├── fundamentals.py         # Screener.in scraping (promoter/FII/DII/pledge/growth)
├── scoring.py                # Cross-sectional z-score scoring + regime check (live signals)
├── strategy.py                 # Apex200: filter + trigger + entry/target/SL/sizing (LIVE)
├── screen.py                    # ad-hoc manual filters (price/PE/FII/DII/promoter)
├── notifier.py                    # Telegram nightly alert
├── main.py                          # nightly entrypoint (what automation calls)
├── backtest.py                        # historical simulator — replays Apex200 rules,
│                                       # supports --yearly, --n-folds, --out-of-sample,
│                                       # --regime-filter, --choppiness-filter, --apply-tax
├── sweep.py                             # tests many stop/target combos, one table
├── check_setup.py                        # ONE command full health check — run this first
│                                          # whenever something seems off
├── find_price_anomalies.py                # detects likely unadjusted splits/bad data
├── data/nifty200.csv                        # universe snapshot
├── output/                                    # nightly CSV/HTML reports, backtest results
├── automation/ (Mac) — launchd plist + setup script
├── Run_myNSE200.command / Run_myNSE200.bat      # double-click manual launchers
├── .env                                           # Telegram token/chat ID
├── MASTER_CONTEXT.md                                # this file
└── requirements.txt
```

## Current VALIDATED live settings (config.py) — as of this conversation

```python
ATR_STOP_MULTIPLIER = 3.0            # stop = entry - 3.0 x ATR14 (was 1.5, tested wider = better)
REWARD_RISK_RATIO = 1.0              # target = entry + 1.0 x risk (was 3.0 — 1:1 tested better)
USE_SWING_LOW_STOP_IF_TIGHTER = True
SWING_LOW_LOOKBACK_DAYS = 10
ACCOUNT_CAPITAL = 20000.0            # <-- user's real capital; MUST be updated manually and
                                      #     periodically for compounding to work live. At 20000,
                                      #     per-position cap is ~₹4,000 (20% of equity) — stocks
                                      #     priced above that per share get 0 shares / don't qualify,
                                      #     shown in reports as "risk sizing produced zero quantity."
                                      #     User explicitly chose this knowing that trade-off.
RISK_PER_TRADE_PCT = 1.0             # tested raising this — did NOT reliably help, only worsened
                                      # drawdown, so left at 1%
MAX_PER_ORDER_PCT_OF_EQUITY = 20.0   # replaced old flat MAX_PER_ORDER_INR (₹100,000), which was
                                      # silently capping trades regardless of account size — real bug
                                      # found via testing, now fixed
MAX_ORDERS_PER_DAY = 20
MAX_OPEN_POSITIONS = 20
MAX_HOLDING_DAYS = 20
MIN_SCORE_PERCENTILE = 90            # raised from 80 — real, tested improvement, see below
MIN_VOLUME_SURGE_RATIO = 1.5
USE_REGIME_FILTER = True             # ADDED — only take new trades when Nifty 50 index is above
                                      # its own 200-day SMA. Validated: improves both CAGR and
                                      # drawdown vs not having it (see below). LIVE-WIRED: main.py
                                      # refreshes NIFTY50 index data every real nightly run;
                                      # strategy.py blocks ALL new signals when regime is bearish,
                                      # with reason "market regime filter..." shown in reports.
REGIME_SMA_PERIOD = 200
USE_SECTOR_CAP = True                # ADDED — no single sector's currently-HELD positions can
                                      # exceed MAX_PER_SECTOR_PCT of equity. Validated on a 5-window
                                      # rolling out-of-sample test: 4/5 windows improved or stayed
                                      # identical, 1/5 showed a tiny CAGR dip alongside a drawdown
                                      # improvement. LIVE-WIRED: strategy.py checks real confirmed
                                      # holdings (via portfolio.py) before allowing a new signal,
                                      # reason shown as "sector cap: <sector> already at ₹X/₹Y...".
                                      # Uses data/sector_map.csv — MANUALLY COMPILED, not from an
                                      # official source, spot-check if precision matters.
MAX_PER_SECTOR_PCT = 40.0
UNIVERSE_INDEX = "nifty200"          # tested nifty50/100/500 — all worse, see below. Do not change.
SCORE_WEIGHTS = {"momentum": 0.35, "trend": 0.20, "quality": 0.15, "growth": 0.30}
MAX_PROMOTER_HOLDING = 40.0          # ★ Recommended filter
MIN_FII_PLUS_DII = 25.0
MAX_PLEDGE_PCT = 0.0
ALLOW_UNKNOWN_PLEDGE = False
```

## How the live strategy works (full mechanics)

1. **Score every stock nightly**: cross-sectional z-scores across Momentum
   (35%, 1M/3M/6M returns) + Trend (20%, above SMA50/SMA200) + Quality (15%,
   return/volatility) + Growth (30%, 3yr sales+profit growth, neutral/0 if
   fundamentals missing).
2. **★ Recommended filter**: promoter <40%, FII+DII ≥25%, pledge=0 AND known
   (unknown pledge = auto-disqualify).
3. **Entry trigger**: close > SMA20 AND > SMA50, volume ≥1.5x 20-day avg,
   score in top 20 percentile of that night's universe.
4. **Entry price** = signal day's close; meant to be acted on at next day's
   open (no look-ahead).
5. **Stop-loss** = tighter of (entry − 3.0×ATR14) or 10-day swing low.
6. **Target** = entry + 1.0 × (entry − stop).
7. **Time exit**: close at day 20 if neither hit.
8. **Sizing**: risk 1% of ACCOUNT_CAPITAL per trade, capped at 20% of
   ACCOUNT_CAPITAL per position.

## Known real bugs found and fixed during this project (don't reintroduce)

1. **`.env` wasn't being loaded** — `config.py` originally had no
   `load_dotenv()` call, so Telegram token/chat ID silently read as empty
   strings. Fixed by adding `python-dotenv` load at top of `config.py`.
2. **Today's still-forming candle was being used as if closed** — running
   the strategy mid-day (market open) used Yahoo Finance's live partial
   price as "today's close," causing entry/target/stop to visibly drift
   every time you checked. Fixed in `scoring.py`: drops the last candle if
   it's dated today and current IST time is before 15:35.
3. **Fundamentals were re-scraped on every manual run**, meaning even
   after fix #2, results could still shift mid-day from fundamentals
   changing underneath you. Fixed by making the double-click launchers use
   `--skip-fundamentals`; only the true nightly `main.py` run (no flag)
   refreshes fundamentals.
4. **Same-day report files were overwriting each other** — fixed by
   timestamping report filenames (`apex200_YYYY-MM-DD_HHMM.*`) plus adding
   an always-current `apex200_latest.*` convenience copy.
5. **`MAX_PER_ORDER_INR` (flat ₹100,000 cap) was silently overriding risk
   %-based sizing** for most trades regardless of account size — this is
   why an early `--compound` backtest test produced byte-identical results
   with and without compounding (the flat cap was binding either way).
   Fixed by replacing with `MAX_PER_ORDER_PCT_OF_EQUITY` (scales with
   account size).
6. **`config.STT_PCT` had a stray typo** (`xSTT_PCT`) introduced during a
   manual edit — caught and fixed, wasn't yet referenced anywhere so no
   live impact, but worth knowing the constant name is `STT_PCT`.
7. **`is_recommended()` used `is None` checks, which don't catch pandas
   `NaN`** — when fundamentals data was completely missing for a symbol,
   the filter silently let it PASS instead of correctly disqualifying it
   (the opposite of intended fail-safe behavior). Caught via
   `check_setup.py` showing "Unknown pledge data: 200/200" (i.e. the whole
   universe) while a live run still somehow produced buy signals. Fixed in
   both `scoring.py` and `backtest.py` with explicit `pd.isna()` checks.
8. **Pledge data: "no pledge row shown" was being treated as "unknown"
   instead of "confirmed zero"** — Screener.in only displays a pledge line
   item when there's something to disclose; most clean companies show
   nothing at all. This meant nearly every legitimate company was failing
   the ★ Recommended filter. Fixed in `fundamentals.py`: if the
   shareholding table was successfully parsed (promoter holding present)
   but no pledge row appeared, now correctly infers 0% pledged / known.
   Genuine fetch failures still correctly stay "unknown."
9. **`backtest.py` loaded ALL symbols ever downloaded, not just the
   current universe** — after testing Nifty 500 then reverting to Nifty
   200, ~300 leftover symbols stayed in `market_data.db` and were silently
   included in every subsequent "Nifty 200" backtest. Fixed:
   `load_all_candles()` now filters to `data/nifty200.csv`'s current
   contents by default, and prints how many symbols got excluded.
10. **`data_fetch.py`'s index fetch used `period=f"{HISTORY_YEARS}y"`**,
    which under-fetched the Nifty 50 index's available history relative to
    individual stocks (2949 rows / ~12 years vs the stocks' full 20).
    Two folds silently got ZERO regime filtering as a result (regime
    filter had no effect on them at all, invisible until compared against
    the no-filter baseline and noticing identical numbers). Fixed: index
    fetch now defaults to `period="max"`, and both `data_fetch.py` and
    `backtest.py`/`scoring.py`'s regime loaders print an explicit warning
    if the index data doesn't cover the full stock data range.
11. **The mark-to-market equity curve valued a position at ₹0 on any day
    its symbol had a missing candle row** (trading halt, data gap —
    common in older years) instead of using its last known price. This
    caused a mathematically impossible -93% to -98% "drawdown" in one
    fold that no single realized trade could explain (worst real trade
    was only -19.95%). Fixed: `simulate_trades()` now forward-fills each
    symbol's price for mark-to-market purposes when a day's row is
    missing, instead of implicitly treating it as worthless.
12. **Unadjusted stock splits/bonus issues showed as fake 50-90%+
    overnight price crashes** in raw Yahoo Finance data (`auto_adjust`
    was `False`). Fixed: `auto_adjust=True` in `data_fetch.py`, PLUS a
    defensive `clean_extreme_moves()` safeguard added to both
    `backtest.py` and `scoring.py` that detects any remaining implausible
    single-day jump and either flattens it (one-day bad tick, price
    reverts shortly after) or rescales the series for continuity
    (permanent level shift, price stays at the new level — the correct
    handling for a real but unadjusted split).

## Backtesting methodology and results (technical rules only — see caveat)

`backtest.py` replays Apex200's price/technical rules (NOT the fundamental
filter — no historical fundamentals data exists, so applying today's
fundamentals to past trades would be look-ahead bias; `--apply-fundamentals`
exists as an explicitly-labeled optimistic approximation only).

**Full 12-year sweep** (`sweep.py`) tested 30+ stop/target combinations.
Original config (ATR 1.5 / RRR 3.0) was one of the WEAKER combinations
tested: 13.59% CAGR, −64.89% drawdown, 35.95% win rate.

**Best validated combination found**: ATR 3.0 / RRR 1.0 / compounding ON /
20%-of-equity order cap:
- **Training period (9 years, ~2014-2023)**: 21.70% CAGR, 54.76% win rate,
  −31.57% max drawdown, 1,795 trades
- **Out-of-sample period (3 years, 2023-2026, never used for tuning)**:
  30.96% CAGR, 58.47% win rate, −25.08% max drawdown, 590 trades — EVERY
  metric held up or improved out-of-sample, strong evidence this is a real
  pattern, not overfitting. Caveat: this out-of-sample window was a
  favorable/bull period for Indian equities — some outperformance may
  reflect market conditions, not pure strategy edge.

**Post-tax reality** (`--apply-tax` flag, STCG since holdings are almost
always <12 months): effective ~20.8% tax (20% + ~4% cess, confirmed current
for FY2025-26 per July 2024 budget change) on net yearly gains, losses
offset gains same-year. Rough post-tax expectation: normal case ~11-14%
CAGR, best case ~21-24%, worst case still real losses (partially cushioned
by loss offset).

**Honest range communicated to user**: worst case -15 to -20% (bad year/
drawdown), normal case ~12-18% pre-tax / ~10-14% post-tax across a full
cycle, best case ~25-30% pre-tax / ~21-24% post-tax in favorable years —
NOT to be expected every year.

## FINAL validated year-by-year results (20 years, real cleaned data, current config)

With ATR 3.0 / RRR 1.0 / compounding / 20%-of-equity cap / score-90 /
**regime filter ON** (the actual live configuration):

```
year  cagr_pct  max_drawdown_pct
2006     7.41       -8.33
2007     8.82      -16.56
2008    (regime filter blocked ALL trading — real 2008 GFC crash, verified
         against real Nifty history: -55% to -60% actual crash magnitude,
         so 0% instead of a loss here is the filter working correctly)
2009    62.80      -13.35
2010    -3.57      -17.25
2011   -14.10      -16.12   <- worst full year; choppy/directionless, not
                                a clean bear market (regime filter barely
                                triggered); a "choppiness filter" add-on
                                was tried in myNSE200Test and FAILED at
                                two calibrations — see that project's
                                context doc, don't re-attempt here
2012    30.64       -7.77
2013     6.97      -16.38
2014    52.31      -12.26
2015    -4.05      -15.82
2016     0.38      -22.48
2017    49.81       -7.51
2018     7.66      -14.65
2019    -2.11      -18.47
2020    22.19      -10.98
2021    77.74      -12.95
2022    10.43      -17.58
2023    68.43       -8.41
2024    17.30      -15.71
2025    17.60       -8.22
2026    -12.05      -10.68  (partial year, YTD as of ~Aug 2026)
```
**Average across full years (excl. 2008/partial years): ~21.3% CAGR.**
Multi-year fold view (6 x ~3.3yr) also available if needed — same
conclusion, different granularity.

## Regime filter — validated addition, now LIVE

Added after noticing the strategy's worst multi-year folds (2008, 2009-13,
2016-19) all coincided with weak broader-market conditions. Rule: skip ALL
new entries when the Nifty 50 index itself is below its own 200-day SMA.

- **6-fold test**: avg CAGR 20.07% (vs 21.66% without) — slightly lower
  average, but avg drawdown improved from -30.9% to -21.6%, and the worst
  fold (2008-2009) went from -52.83% to -16.84% drawdown — a dramatic
  improvement in the single worst historical stretch.
- **Out-of-sample (5yr split)**: WITHOUT filter: 22.28% CAGR/-25.91% DD.
  WITH filter: 22.67% CAGR/-22.21% DD — better on BOTH counts out-of-
  sample. Clean pass, genuine improvement, now part of the live system.
- **Live-wired**: `main.py` fetches fresh NIFTY50 index data every real
  nightly run; `strategy.py` blocks all signals when bearish. Confirmed
  working live: as of ~Aug 8 2026, regime flipped bearish and the live
  system correctly produced 0 signals with `check_setup.py` confirming
  `Current market regime: BEAR`. **Zero signals for a week or more during
  a confirmed bear regime is normal and expected, not a malfunction** —
  historically the regime filter has skipped 70-250+ days in a single
  year depending on conditions. Check `check_setup.py` section 5 anytime
  to see current regime status directly.

## Ideas tested and rejected (full list — don't re-test these without new evidence)

- Wider universe (Nifty 500): failed badly — real 2023-2025 mid/small-cap
  boom-bust cycle caused -7.24% CAGR / -51% drawdown out-of-sample
- Narrower universe (Nifty 100): calmer but slower, ~16.25% avg CAGR vs
  20.07% baseline at the time
- Shorter max holding (12 days): worse on both CAGR and drawdown
- Longer max holding (30 days): worse on both counts too
- More risk per trade (1.5%/2%): did not reliably improve CAGR, worsened
  drawdown
- Trailing stop: rarely activated, no meaningful net benefit
- Removing the stop-loss entirely: refused — breaks the entire
  position-sizing model (which is built FROM the stop distance), creates
  unbounded single-stock risk, and survivorship bias in the data (only
  currently-listed stocks are in the DB) would hide exactly the tail risk
  this introduces
- 30-cell sweep of ATR x reward:risk already found 3.0/1.0 as the genuine
  peak
- Market "choppiness" filter (Kaufman's Efficiency Ratio on the index, on
  top of the regime filter) — tested in the separate `myNSE200Test`
  sandbox at two thresholds, both net losses (~11.7% and ~14.9% avg CAGR
  vs 21.3% baseline) despite genuinely fixing 2011 at one threshold —
  gutted good years too much to be worth it. See myNSE200Test's own
  context doc for full detail; that experimentation continues in a
  separate chat, not this one.

## Explicitly NOT yet done — real open items, not just ideas

1. **Real slippage/execution modeling** — now BUILT and TESTED in backtest.py
   (`--slippage-pct`), not yet adopted into live sizing/reporting. Finding:
   even 0.2% slippage cut average CAGR roughly from ~20% to ~13% — a much
   bigger realistic haircut than tax alone. Worth treating ~13-15% as the
   more honest "expect this" number going forward, pre-tax.
2. **A reminder mechanism for `ACCOUNT_CAPITAL` staleness** — discussed
   but not built; could add "days since last capital update" to the
   nightly report.
3. Multi-year rolling out-of-sample (`--rolling-out-of-sample`) is now
   BUILT and was the strongest validation result in the whole project —
   5 independent 3-year windows, all positive, ~20.5% average. Consider
   re-running periodically as more data accumulates, not a one-time check.

1. **Multi-period walk-forward validation** — only ONE train/test split has
   been run. More rigorous validation would use multiple rolling splits.
2. **Sector concentration cap** — no constraint currently prevents many of
   the 20 open positions from clustering in one correlated sector.
3. **Drawdown circuit-breaker** — no live mechanism to reduce position
   size automatically after a bad stretch; historical max drawdown
   (−25% to −45% depending on settings) could recur without any automatic
   dampening.
4. **Wider universe test** (beyond Nifty 200) — untested lever for CAGR.
5. **Real slippage/execution modeling** — backtest assumes fills at exact
   stop/target prices.

## User's stated goals and constraints (for tone/calibration)

- Non-technical — needs exact copy-paste terminal commands, plain-language
  explanations, no jargon without definition. Has repeatedly hit: wrong
  folder, missing `python3` prefix, stale files after partial copies,
  missing imports after edits — verify claims carefully, expect friction.
- Explicitly wanted maximum CAGR, pushed hard on this multiple times even
  after repeated honest evidence (including "20% guaranteed every year" —
  corrected using real data: even the best Indian mutual funds, ~20-22%
  10yr CAGR, don't clear that bar in every individual year either).
  Eventually accepted the evidence-based ~21% average as genuinely
  competitive with top funds, and agreed to split further experimentation
  into a separate `myNSE200Test` sandbox/chat rather than keep altering
  the validated live system.
- Real capital: `ACCOUNT_CAPITAL = 20000`, chosen deliberately for a
  ~₹3,000-5,000 per-stock range; understands and accepted that stocks
  priced above ~₹4,000/share will show 0 qualifying quantity as a result.
- Wants taxes and charges factored into realistic return expectations
  (STCG ~20.8% effective, since holdings are almost always <12 months).
- Has repeatedly asked for real backtested evidence over guesses — respond
  in kind; don't assert a config is "final" without validation. Has also
  caught real bugs by attentively reading tool output (e.g. noticed
  "Unknown pledge data: 200" in a diagnostic and asked about it, which is
  what surfaced bug #7/#8 above) — take their observations seriously.

## THIS chat's scope going forward

This chat is now scoped to the **working, live `myNSE200` folder only** —
health checks, live signal troubleshooting, understanding results,
capital/config updates. Further strategy EXPERIMENTATION (new filters,
new ideas trying to beat the validated baseline) happens in a separate
chat using `myNSE200Test`'s own context document — don't restart that
work here.

## Suggested next steps, if resuming

The open items below are still genuinely unaddressed. Otherwise, the
system is live, validated, and running nightly — the main ongoing task is
periodic health checks (`check_setup.py`) and remembering to update
`ACCOUNT_CAPITAL` as real capital changes.

1. **Drawdown circuit-breaker** — no live mechanism to reduce position
   size automatically after a bad stretch. Only remaining structural gap.
2. **A reminder mechanism for `ACCOUNT_CAPITAL` staleness** — discussed
   but not built; could add "days since last capital update" to the
   nightly report.
3. **Adopt slippage into live expectations** — `--slippage-pct` exists in
   backtest.py and showed a real ~20%→~13% CAGR haircut at just 0.2%; not
   wired into anything live (there's nothing to "wire" — it's a backtest-
   only realism check), but worth remembering as the more honest number
   when setting expectations, more than the pre-slippage figures.
4. Rolling out-of-sample, sector cap: DONE, live-wired, see sections above
   — no longer open items, kept here crossed off for continuity.
   nightly report.
