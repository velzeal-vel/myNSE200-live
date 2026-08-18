"""
myNSE200 / sweep.py
----------------------
Tests a RANGE of stop-loss/target combinations against your real historical
data, so you can see the actual trade-off between win rate, average win/loss,
CAGR, and max drawdown — instead of picking a target win rate and hoping.

This directly demonstrates why "90% win rate" isn't a design goal you can
just dial in for free: as the reward:risk ratio shrinks (tighter target
relative to stop), win rate goes up — but so does your exposure to a
catastrophic single loss. Look at ALL the columns together, not just
win_rate — a strategy with a high win rate and a terrible max_drawdown is
worse, not better.

Usage:
    python3 sweep.py                          # default grid
    python3 sweep.py --capital 500000
    python3 sweep.py --atr-range 1.0,1.5,2.0,2.5,3.0 --rrr-range 0.5,1,1.5,2,3,4
"""

import argparse

import pandas as pd

import config
import backtest


def run_sweep(atr_values, rrr_values, capital, start_date=None, trailing=False,
              risk_pct=None, compound=False):
    print("Loading historical candles...")
    raw = backtest.load_all_candles()
    if raw.empty:
        print("No candle data found. Run `python3 main.py --full-history` first.")
        raise SystemExit(1)

    print(f"Computing indicators for {raw['symbol'].nunique()} symbols...")
    ind_df = backtest.compute_indicators(raw)

    print("Generating signals once (entry trigger doesn't depend on stop/target settings)...")
    signals_df = backtest.generate_daily_signals(ind_df, start_date=start_date)

    sim_ind_df = ind_df[ind_df["date"] >= (pd.Timestamp(start_date) if start_date else ind_df["date"].min())]

    results = []
    total_runs = len(atr_values) * len(rrr_values)
    run_num = 0
    for atr_mult in atr_values:
        for rrr in rrr_values:
            run_num += 1
            print(f"  [{run_num}/{total_runs}] ATR x{atr_mult}, target {rrr}:1 risk...")
            trades_df, equity_df = backtest.simulate_trades(
                sim_ind_df, signals_df, capital, config.MAX_OPEN_POSITIONS,
                use_trailing_stop=trailing, atr_multiplier=atr_mult, reward_risk_ratio=rrr,
                risk_pct=risk_pct, compound_sizing=compound,
            )
            stats = backtest.compute_stats(trades_df, equity_df, capital)
            if "error" in stats:
                continue
            stats["atr_multiplier"] = atr_mult
            stats["reward_risk_ratio"] = rrr
            results.append(stats)

    return pd.DataFrame(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=str, default=None)
    parser.add_argument("--capital", type=float, default=None)
    parser.add_argument("--trailing-stop", action="store_true")
    parser.add_argument("--atr-range", type=str, default="1.0,1.5,2.0,2.5,3.0",
                         help="Comma-separated ATR stop multipliers to test")
    parser.add_argument("--rrr-range", type=str, default="0.5,1,1.5,2,3,4",
                         help="Comma-separated reward:risk ratios to test")
    parser.add_argument("--risk-pct", type=float, default=None,
                         help="Override config.RISK_PER_TRADE_PCT for the whole sweep (e.g. 1.5)")
    parser.add_argument("--compound", action="store_true",
                         help="Size each trade off CURRENT equity instead of fixed starting capital")
    args = parser.parse_args()

    capital = args.capital or config.ACCOUNT_CAPITAL
    atr_values = [float(x) for x in args.atr_range.split(",")]
    rrr_values = [float(x) for x in args.rrr_range.split(",")]

    df = run_sweep(atr_values, rrr_values, capital, start_date=args.start, trailing=args.trailing_stop,
                    risk_pct=args.risk_pct, compound=args.compound)

    cols = ["atr_multiplier", "reward_risk_ratio", "total_trades", "win_rate_pct",
            "avg_win_pct", "avg_loss_pct", "avg_return_per_trade_pct",
            "cagr_pct", "max_drawdown_pct", "avg_holding_days"]
    df = df[[c for c in cols if c in df.columns]]
    df = df.sort_values("win_rate_pct", ascending=False)

    print("\n" + "=" * 100)
    print(" SWEEP RESULTS — sorted by win rate (highest first)")
    print(" Look at max_drawdown_pct and cagr_pct alongside win_rate_pct, not in isolation.")
    print("=" * 100)
    pd.set_option("display.width", 140)
    pd.set_option("display.max_columns", 20)
    print(df.to_string(index=False))

    out_path = config.OUTPUT_DIR / "sweep_results.csv"
    df.to_csv(out_path, index=False)
    print(f"\nFull results: {out_path}")
    print("\nReminder: this is a historical simulation, not a guarantee of future results.")
    print("A high win_rate row with a large negative max_drawdown_pct is a WARNING sign, not a win.")
