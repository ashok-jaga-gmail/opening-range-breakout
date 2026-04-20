#!/usr/bin/env python3
"""
QQQ 15-Minute Opening Range Breakout (ORB) — Research Backtest
==============================================================
Data : Databento XNAS.ITCH ohlcv-1m  2018-05-01 → 2026-03-13
Entry: First bar at/after 09:45 ET whose CLOSE breaks the 15-min ORB
       (09:30–09:44 inclusive).  Long if close > ORB high, Short if < ORB low.
Stop : ORB opposite edge  (i.e. the full ORB range as risk unit R)
Exits tested:
  R0.5 — target 0.5R above entry  (short time frame scalp)
  R1   — target 1R  (1:1 R/R)
  R2   — target 2R
  R3   — target 3R
  EOD  — hold to 15:59 with stop only
  T30  — exit at 30 min from entry OR stop, whichever first
  T60  — exit at 60 min from entry OR stop, whichever first

All prices are per-share QQQ (underlying, no leverage, no options).
All P&L is in $ per 1 share.  Scale to your position size as needed.

Usage:
    python3 orb_paper_backtest.py

Outputs:
    /tmp/orb_paper_results.json   — full trade list + stats
    stdout                        — formatted summary tables
"""

import sys
import csv
import json
import lzma
import math
import datetime
import os
from collections import defaultdict

# ── Configuration ─────────────────────────────────────────────────────────────
# Compressed data file ships with the repo; no external dependencies needed.
_HERE    = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(_HERE, "qqq_1m_2018_2026.csv.xz")
OUT_FILE = "/tmp/orb_paper_results.json"

ORB_END_TIME   = datetime.time(9, 44)   # last bar included in ORB (09:44 close)
ENTRY_MIN_TIME = datetime.time(9, 45)   # earliest entry bar
EOD_EXIT_TIME  = datetime.time(15, 59)  # end-of-day hard close
RTH_START      = datetime.time(9, 30)
RTH_END        = datetime.time(16, 0)

# Exit strategy configs: name -> target_R (None = no target, just stop + EOD)
# Stop is always placed at the ORB opposite edge for all strategies.
EXIT_CONFIGS = {
    "R0.5": 0.5,
    "R1":   1.0,
    "R2":   2.0,
    "R3":   3.0,
    "EOD":  None,   # no target — hold to EOD with stop
    "T30":  None,   # timed exit: 30 minutes
    "T60":  None,   # timed exit: 60 minutes
}
TIMED_EXITS = {"T30": 30, "T60": 60}

# Minimum ORB range to take a trade (filters micro-range / data-gap days)
MIN_ORB_RANGE = 0.10   # $0.10


# ── Step 1: Load CSV → {date: [(time, o, h, l, c, v), ...]} ─────────────────
def load_csv_to_daily_bars(csv_path: str) -> dict:
    """
    Read the CSV (plain or .xz compressed) with columns:
        date, time, open, high, low, close, volume
    Returns: dict[date_str → list[(time_str HH:MM, o, h, l, c, v)]]
    """
    print(f"Loading {csv_path} …", flush=True)
    daily: dict = defaultdict(list)
    total = 0

    opener = lzma.open if csv_path.endswith(".xz") else open
    with opener(csv_path, "rt", newline="") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            d, t, o, h, l, c, v = row
            daily[d].append((t, float(o), float(h), float(l), float(c), int(v)))
            total += 1

    for d in daily:
        daily[d].sort(key=lambda x: x[0])

    print(f"  Loaded {total:,} bars across {len(daily)} trading days.", flush=True)
    return dict(daily)


# ── Step 2: Compute 15-min ORB ────────────────────────────────────────────────
def compute_orb(day_bars: list) -> tuple | None:
    """Return (orb_high, orb_low) from bars 09:30–09:44 inclusive, or None."""
    orb_high = -1e18
    orb_low  =  1e18
    found = False
    for t, o, h, l, c, v in day_bars:
        if t >= "09:30" and t <= "09:44":
            orb_high = max(orb_high, h)
            orb_low  = min(orb_low,  l)
            found = True
    if not found:
        return None
    if orb_high - orb_low < MIN_ORB_RANGE:
        return None
    return orb_high, orb_low


# ── Step 3: Find first breakout bar ───────────────────────────────────────────
def find_breakout(day_bars: list, orb_high: float, orb_low: float) -> tuple | None:
    """
    Return (direction, bar_index, bar_tuple) for the first bar at/after 09:45
    whose CLOSE breaks outside the ORB.
    """
    for i, (t, o, h, l, c, v) in enumerate(day_bars):
        if t < "09:45":
            continue
        if c > orb_high:
            return "LONG", i, (t, o, h, l, c, v)
        if c < orb_low:
            return "SHORT", i, (t, o, h, l, c, v)
    return None


# ── Step 4: Simulate a single trade for all exit configs ─────────────────────
def simulate_all_exits(
    direction: str,
    entry_price: float,
    stop_price: float,
    entry_bar_idx: int,
    day_bars: list,
    entry_time_str: str,
) -> dict:
    """
    Walk bars from entry bar +1 forward.
    For each exit config, record exit_price, exit_time, exit_reason.
    Returns dict: config_name -> {exit_price, exit_time, exit_reason, pnl}
    """
    orb_range = abs(entry_price - stop_price)
    results = {}

    # Pre-compute entry bar index offset for timed exits
    entry_bar_time = datetime.datetime.strptime(entry_time_str, "%H:%M").time()

    # Initialise result slots
    for cfg in EXIT_CONFIGS:
        results[cfg] = None  # will be filled

    # Track which configs are still open
    open_configs = set(EXIT_CONFIGS.keys())

    # Walk forward bar by bar
    post_bars = day_bars[entry_bar_idx + 1:]

    for bar_offset, (t, o, h, l, c, v) in enumerate(post_bars, start=1):
        if not open_configs:
            break

        bar_time = datetime.datetime.strptime(t, "%H:%M").time()
        minutes_elapsed = bar_offset  # 1 bar = 1 minute elapsed since entry

        for cfg in list(open_configs):
            target_R = EXIT_CONFIGS[cfg]
            timed_limit = TIMED_EXITS.get(cfg)

            # Compute target price
            if target_R is not None:
                if direction == "LONG":
                    target_price = entry_price + target_R * orb_range
                else:
                    target_price = entry_price - target_R * orb_range
            else:
                target_price = None

            # --- Stop hit? (check low for long, high for short) ---
            stop_hit = False
            if direction == "LONG" and l <= stop_price:
                stop_hit = True
            elif direction == "SHORT" and h >= stop_price:
                stop_hit = True

            # --- Target hit? ---
            target_hit = False
            if target_price is not None:
                if direction == "LONG" and h >= target_price:
                    target_hit = True
                elif direction == "SHORT" and l <= target_price:
                    target_hit = True

            # --- Timed exit? ---
            timed_exit = timed_limit is not None and minutes_elapsed >= timed_limit

            # --- EOD? ---
            eod_exit = bar_time >= EOD_EXIT_TIME

            # Determine exit (priority: stop > target > timed/EOD)
            exit_price = None
            exit_reason = None

            if stop_hit and target_hit:
                # Ambiguous — use mid of bar as proxy; treat as stop
                exit_price  = stop_price
                exit_reason = "STOP"
            elif stop_hit:
                exit_price  = stop_price
                exit_reason = "STOP"
            elif target_hit:
                exit_price  = target_price
                exit_reason = "TARGET"
            elif timed_exit or eod_exit:
                exit_price  = c  # exit at bar close
                exit_reason = "TIME" if timed_exit else "EOD"

            if exit_price is not None:
                if direction == "LONG":
                    pnl = exit_price - entry_price
                else:
                    pnl = entry_price - exit_price

                results[cfg] = {
                    "exit_price":  exit_price,
                    "exit_time":   t,
                    "exit_reason": exit_reason,
                    "pnl":         round(pnl, 4),
                }
                open_configs.discard(cfg)

    # Any configs still open at end of day → close at last bar's close
    if open_configs:
        last_t, last_o, last_h, last_l, last_c, last_v = post_bars[-1] if post_bars else (entry_time_str, entry_price, entry_price, entry_price, entry_price, 0)
        for cfg in open_configs:
            if direction == "LONG":
                pnl = last_c - entry_price
            else:
                pnl = entry_price - last_c
            results[cfg] = {
                "exit_price":  last_c,
                "exit_time":   last_t,
                "exit_reason": "CLOSE",
                "pnl":         round(pnl, 4),
            }

    return results


# ── Step 5: Run full backtest ─────────────────────────────────────────────────
def run_backtest(daily_bars: dict) -> list:
    """
    Iterate every trading day, find ORB, find breakout, simulate exits.
    Returns list of trade records.
    """
    all_trades = []
    trading_days = sorted(daily_bars.keys())
    n = len(trading_days)

    print(f"\nRunning ORB backtest on {n} trading days …", flush=True)

    skipped_no_orb   = 0
    skipped_no_break = 0
    traded_days      = 0

    for i, date_str in enumerate(trading_days):
        if i % 250 == 0:
            print(f"  {i}/{n} days processed …", flush=True)

        day_bars = daily_bars[date_str]

        # Compute ORB
        orb = compute_orb(day_bars)
        if orb is None:
            skipped_no_orb += 1
            continue
        orb_high, orb_low = orb
        orb_range = orb_high - orb_low

        # Find breakout
        breakout = find_breakout(day_bars, orb_high, orb_low)
        if breakout is None:
            skipped_no_break += 1
            continue

        direction, bo_idx, bo_bar = breakout
        bo_time, bo_o, bo_h, bo_l, bo_c, bo_v = bo_bar

        entry_price = bo_c  # fill at close of breakout bar

        # Stop = ORB opposite edge
        if direction == "LONG":
            stop_price = orb_low
        else:
            stop_price = orb_high

        # Simulate all exits
        exit_results = simulate_all_exits(
            direction, entry_price, stop_price, bo_idx, day_bars, bo_time
        )

        # Build trade record
        record = {
            "date":        date_str,
            "year":        date_str[:4],
            "direction":   direction,
            "orb_high":    round(orb_high, 4),
            "orb_low":     round(orb_low,  4),
            "orb_range":   round(orb_range, 4),
            "entry_time":  bo_time,
            "entry_price": round(entry_price, 4),
            "stop_price":  round(stop_price,  4),
            "exits":       exit_results,
        }
        all_trades.append(record)
        traded_days += 1

    print(f"\n  Traded days:     {traded_days}")
    print(f"  No ORB / thin:   {skipped_no_orb}")
    print(f"  No breakout:     {skipped_no_break}")
    return all_trades


# ── Step 6: Statistics ────────────────────────────────────────────────────────
def compute_stats(trades: list, cfg: str) -> dict:
    """Compute performance stats for one exit config across a list of trades."""
    pnls = []
    for t in trades:
        ex = t["exits"].get(cfg)
        if ex is not None and ex["pnl"] is not None:
            pnls.append(ex["pnl"])

    if not pnls:
        return {}

    n      = len(pnls)
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    total_pnl   = sum(pnls)
    gross_win   = sum(wins)
    gross_loss  = abs(sum(losses)) if losses else 0
    win_rate    = len(wins) / n * 100
    profit_factor = gross_win / gross_loss if gross_loss > 0 else float("inf")
    avg_win     = gross_win  / len(wins)   if wins   else 0
    avg_loss    = -gross_loss / len(losses) if losses else 0
    expectancy  = total_pnl / n

    # Max drawdown (sequential equity curve)
    equity  = 0.0
    peak    = 0.0
    max_dd  = 0.0
    equity_curve = []
    for p in pnls:
        equity += p
        equity_curve.append(equity)
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    # Daily Sharpe (annualised, assuming 252 trading days, risk-free = 0)
    mean_p  = total_pnl / n
    std_p   = math.sqrt(sum((p - mean_p) ** 2 for p in pnls) / n) if n > 1 else 0
    sharpe  = (mean_p / std_p * math.sqrt(252)) if std_p > 0 else 0

    # Calmar = annualised return / max drawdown
    # Annualised: assume ~252 trading days/year
    years   = n / 252
    ann_ret = total_pnl / years if years > 0 else 0
    calmar  = ann_ret / max_dd if max_dd > 0 else float("inf")

    # Exit reason breakdown
    reasons = defaultdict(int)
    for t in trades:
        ex = t["exits"].get(cfg)
        if ex:
            reasons[ex["exit_reason"]] += 1

    return {
        "n":             n,
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      round(win_rate, 1),
        "total_pnl":     round(total_pnl, 2),
        "gross_win":     round(gross_win, 2),
        "gross_loss":    round(gross_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "avg_win":       round(avg_win, 4),
        "avg_loss":      round(avg_loss, 4),
        "expectancy":    round(expectancy, 4),
        "max_dd":        round(max_dd, 2),
        "sharpe":        round(sharpe, 2),
        "calmar":        round(calmar, 2),
        "ann_ret":       round(ann_ret, 2),
        "exit_reasons":  dict(reasons),
    }


def compute_annual_stats(trades: list, cfg: str) -> dict:
    """Break down stats by year for one config."""
    by_year = defaultdict(list)
    for t in trades:
        by_year[t["year"]].append(t)
    return {yr: compute_stats(trades_yr, cfg) for yr, trades_yr in sorted(by_year.items())}


def compute_direction_stats(trades: list, cfg: str) -> dict:
    """Break down stats by direction (LONG/SHORT)."""
    by_dir = defaultdict(list)
    for t in trades:
        by_dir[t["direction"]].append(t)
    return {d: compute_stats(trades_d, cfg) for d, trades_d in sorted(by_dir.items())}


def compute_orb_quartile_stats(trades: list, cfg: str) -> dict:
    """
    Split trades into ORB-range quartiles and compute stats per quartile.
    Narrow ORB = low volatility day, Wide ORB = high volatility day.
    """
    ranges = sorted(t["orb_range"] for t in trades)
    n = len(ranges)
    q_size = n // 4
    thresholds = [
        ranges[q_size - 1],
        ranges[2 * q_size - 1],
        ranges[3 * q_size - 1],
        ranges[-1],
    ]

    def label(r):
        if r <= thresholds[0]: return "Q1 (Narrow)"
        if r <= thresholds[1]: return "Q2"
        if r <= thresholds[2]: return "Q3"
        return "Q4 (Wide)"

    by_q = defaultdict(list)
    for t in trades:
        by_q[label(t["orb_range"])].append(t)

    return {q: compute_stats(trades_q, cfg) for q, trades_q in sorted(by_q.items())}


# ── Step 7: Pretty-print ──────────────────────────────────────────────────────
SEP = "=" * 90

def fmt_pct(v): return f"{v:>6.1f}%"
def fmt_usd(v): return f"${v:>+8.2f}"
def fmt_num(v): return f"{v:>6.2f}"


def print_summary_table(all_trades: list):
    print(f"\n{SEP}")
    print("  QQQ 15-MIN ORB BACKTEST — FULL PERIOD 2018-2026")
    print(f"{SEP}")
    print(f"  {'Config':<8} {'Trades':>7} {'WR':>7} {'PF':>6} {'Tot P&L':>10} {'Exp/Tr':>8} {'MaxDD':>8} {'Sharpe':>7} {'Calmar':>7}")
    print(f"  {'-'*8} {'-'*7} {'-'*7} {'-'*6} {'-'*10} {'-'*8} {'-'*8} {'-'*7} {'-'*7}")
    for cfg in EXIT_CONFIGS:
        s = compute_stats(all_trades, cfg)
        if not s:
            continue
        print(f"  {cfg:<8} {s['n']:>7} {fmt_pct(s['win_rate'])} {fmt_num(s['profit_factor'])} "
              f"{fmt_usd(s['total_pnl'])} {s['expectancy']:>+8.4f} "
              f"{fmt_usd(s['max_dd'])} {fmt_num(s['sharpe'])} {fmt_num(s['calmar'])}")


def print_annual_table(all_trades: list, cfg: str):
    print(f"\n{SEP}")
    print(f"  ANNUAL BREAKDOWN — {cfg}")
    print(f"{SEP}")
    annual = compute_annual_stats(all_trades, cfg)
    print(f"  {'Year':<6} {'Trades':>7} {'WR':>7} {'PF':>6} {'P&L':>10} {'MaxDD':>8} {'Sharpe':>7}")
    print(f"  {'-'*6} {'-'*7} {'-'*7} {'-'*6} {'-'*10} {'-'*8} {'-'*7}")
    for yr, s in annual.items():
        if not s: continue
        print(f"  {yr:<6} {s['n']:>7} {fmt_pct(s['win_rate'])} {fmt_num(s['profit_factor'])} "
              f"{fmt_usd(s['total_pnl'])} {fmt_usd(s['max_dd'])} {fmt_num(s['sharpe'])}")


def print_direction_table(all_trades: list, cfg: str):
    print(f"\n{SEP}")
    print(f"  LONG vs SHORT — {cfg}")
    print(f"{SEP}")
    dstats = compute_direction_stats(all_trades, cfg)
    for d, s in dstats.items():
        if not s: continue
        print(f"  {d:<6}  Trades={s['n']}  WR={fmt_pct(s['win_rate'])}  "
              f"PF={fmt_num(s['profit_factor'])}  P&L={fmt_usd(s['total_pnl'])}  "
              f"MaxDD={fmt_usd(s['max_dd'])}")


def print_quartile_table(all_trades: list, cfg: str):
    print(f"\n{SEP}")
    print(f"  ORB RANGE QUARTILES — {cfg}")
    print(f"{SEP}")
    qstats = compute_orb_quartile_stats(all_trades, cfg)
    print(f"  {'Quartile':<14} {'Trades':>7} {'WR':>7} {'PF':>6} {'P&L':>10} {'AvgWin':>8} {'AvgLoss':>8}")
    print(f"  {'-'*14} {'-'*7} {'-'*7} {'-'*6} {'-'*10} {'-'*8} {'-'*8}")
    for q, s in qstats.items():
        if not s: continue
        print(f"  {q:<14} {s['n']:>7} {fmt_pct(s['win_rate'])} {fmt_num(s['profit_factor'])} "
              f"{fmt_usd(s['total_pnl'])} {s['avg_win']:>+8.4f} {s['avg_loss']:>+8.4f}")


def print_exit_reason_table(all_trades: list, cfg: str):
    s = compute_stats(all_trades, cfg)
    if not s: return
    reasons = s.get("exit_reasons", {})
    total   = sum(reasons.values())
    print(f"\n  Exit reasons ({cfg}): ", end="")
    for r, cnt in sorted(reasons.items()):
        print(f"{r}={cnt} ({cnt/total*100:.0f}%)  ", end="")
    print()


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # 1. Load data
    daily_bars = load_csv_to_daily_bars(CSV_FILE)

    # 2. Run backtest
    all_trades = run_backtest(daily_bars)

    if not all_trades:
        print("No trades found. Check data path and configuration.")
        sys.exit(1)

    # 3. Print results
    print_summary_table(all_trades)

    # Best config for deep dives — pick R2 as the canonical showcase
    canonical = "R2"
    print_annual_table(all_trades, canonical)
    print_direction_table(all_trades, canonical)
    print_quartile_table(all_trades, canonical)
    print_exit_reason_table(all_trades, canonical)

    # Also print annual for R1 and EOD
    for cfg in ["R1", "EOD", "T60"]:
        print_annual_table(all_trades, cfg)
        print_direction_table(all_trades, cfg)
        print_exit_reason_table(all_trades, cfg)

    # 4. Save full results
    # Compute all stats for JSON output
    all_stats = {}
    for cfg in EXIT_CONFIGS:
        all_stats[cfg] = {
            "overall":   compute_stats(all_trades, cfg),
            "annual":    compute_annual_stats(all_trades, cfg),
            "direction": compute_direction_stats(all_trades, cfg),
            "quartile":  compute_orb_quartile_stats(all_trades, cfg),
        }

    output = {
        "metadata": {
            "data_file":    CSV_FILE,
            "orb_minutes":  15,
            "entry_bar":    "09:45",
            "entry_price":  "close of first breakout bar",
            "stop":         "ORB opposite edge",
            "min_orb_range": MIN_ORB_RANGE,
            "total_trades": len(all_trades),
        },
        "stats":  all_stats,
        "trades": all_trades,
    }

    with open(OUT_FILE, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"\n  Full results saved → {OUT_FILE}")
    print(f"  Total trades in dataset: {len(all_trades)}")


if __name__ == "__main__":
    main()
