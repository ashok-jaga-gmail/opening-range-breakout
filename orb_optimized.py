"""
orb_optimized.py — Grid search over filter + tranche parameters

Builds on orb_tranche_strategy.py findings:
  - LONG-only dominates (SHORT Calmar 0.36 vs LONG 6.22)
  - 70-80% alignment is the sweet spot
  - CPR above_top + RSI daily bullish/overbought are strong filters
  - Narrow/normal CPR width predicts trending days
  - Runner (T3) adds value; wider trail lets it run further

Grid tests:
  - alignment_min: 0.60, 0.70, 0.75
  - require_rsi_bull: True/False
  - require_narrow_cpr: True/False
  - t1_r / t2_r / trail_r combinations
  - tranche_weights: equal 1/3 vs front-load runner (1/4, 1/4, 1/2)

Outputs best configs ranked by Calmar, then by total P&L.
Saves full results to tmp/optimized_results.json.
"""

import csv
import json
import lzma
import math
import os
from collections import defaultdict
from itertools import product

_HERE       = os.path.dirname(os.path.abspath(__file__))
CSV_FILE    = os.path.join(_HERE, "qqq_1m_2018_2026.csv.xz")
TRADES_FILE = "/tmp/orb_paper_results.json"
REGIME_FILE = "/tmp/orb_regime_results.json"
OUT_FILE    = os.path.join(_HERE, "tmp", "optimized_results.json")

ORB_MAX = 2.25   # Q3, fixed — enough to keep reasonable trade count

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_csv_to_daily_bars(csv_path):
    print(f"Loading {csv_path} …", flush=True)
    daily = defaultdict(list)
    opener = lzma.open if csv_path.endswith(".xz") else open
    with opener(csv_path, "rt", newline="") as f:
        reader = csv.reader(f)
        next(reader)
        for row in reader:
            d, t, o, h, l, c, v = row
            daily[d].append((t, float(o), float(h), float(l), float(c), int(v)))
    for d in daily:
        daily[d].sort(key=lambda x: x[0])
    print(f"  {sum(len(v) for v in daily.values()):,} bars, {len(daily)} days.", flush=True)
    return dict(daily)


# ---------------------------------------------------------------------------
# Regime helpers
# ---------------------------------------------------------------------------
BULL_CPR  = {"above_top"}
BULL_RSI  = {"bullish", "overbought"}
BULL_MACD = {"bullish_cross", "bullish", "bullish_fade"}
BEAR_CPR  = {"below_bottom"}
BEAR_RSI  = {"bearish", "oversold"}
BEAR_MACD = {"bearish_cross", "bearish", "bearish_fade"}

INDICATOR_KEYS = [
    ("cpr_daily_state",  BULL_CPR,  BEAR_CPR),
    ("cpr_weekly_state", BULL_CPR,  BEAR_CPR),
    ("rsi_daily_state",  BULL_RSI,  BEAR_RSI),
    ("rsi_weekly_state", BULL_RSI,  BEAR_RSI),
    ("macd_daily",       BULL_MACD, BEAR_MACD),
    ("macd_weekly",      BULL_MACD, BEAR_MACD),
    ("rsi_1h_state",     BULL_RSI,  BEAR_RSI),
    ("macd_1h",          BULL_MACD, BEAR_MACD),
    ("rsi_4h_state",     BULL_RSI,  BEAR_RSI),
    ("macd_4h",          BULL_MACD, BEAR_MACD),
]

def compute_alignment(regime, direction):
    aligned = available = 0
    for key, bull_set, bear_set in INDICATOR_KEYS:
        val = regime.get(key)
        if val in (None, "unknown"):
            continue
        available += 1
        if direction == "LONG" and val in bull_set:
            aligned += 1
        elif direction == "SHORT" and val in bear_set:
            aligned += 1
    return aligned / available if available > 0 else None


def passes_filter(trade, regime, cfg):
    direction = trade["direction"]
    orb_range = trade["orb_range"]

    if cfg["long_only"] and direction != "LONG":
        return False, None
    if orb_range > ORB_MAX:
        return False, None

    # Daily CPR must align
    cpr_state = regime.get("cpr_daily_state")
    if cpr_state not in (None, "unknown"):
        if direction == "LONG" and cpr_state != "above_top":
            return False, None
        if direction == "SHORT" and cpr_state != "below_bottom":
            return False, None

    # Optional: RSI daily bullish/overbought
    if cfg["require_rsi_bull"]:
        rsi_state = regime.get("rsi_daily_state")
        if rsi_state not in (None, "unknown"):
            if direction == "LONG" and rsi_state not in BULL_RSI:
                return False, None
            if direction == "SHORT" and rsi_state not in BEAR_RSI:
                return False, None

    # Optional: CPR width narrow or normal (exclude wide/choppy)
    if cfg["require_narrow_cpr"]:
        width = regime.get("cpr_daily_width")
        if width not in (None, "unknown", "narrow", "normal"):
            return False, None

    # Alignment score
    score = compute_alignment(regime, direction)
    if score is None or score < cfg["align_min"]:
        return False, score

    return True, score


# ---------------------------------------------------------------------------
# Tranche simulation
# ---------------------------------------------------------------------------
def simulate_tranche(direction, entry_price, stop_price, entry_idx, day_bars, cfg):
    orb_range = abs(entry_price - stop_price)
    sign      = 1 if direction == "LONG" else -1
    t1_price  = entry_price + sign * cfg["t1_r"] * orb_range
    t2_price  = entry_price + sign * cfg["t2_r"] * orb_range
    w1, w2, w3 = cfg["weights"]  # tranche weights (sum to 1)

    stop      = stop_price
    t1_hit = t2_hit = False
    t1_pnl = t2_pnl = t3_pnl = 0.0
    trail_hw  = entry_price

    post_bars = day_bars[entry_idx + 1:]

    for t, o, h, l, c, v in post_bars:
        eod = t >= "15:59"

        # Update trail high-water mark
        trail_hw = max(trail_hw, h) if direction == "LONG" else min(trail_hw, l)

        if not t1_hit:
            t1_reached = (direction == "LONG" and h >= t1_price) or \
                         (direction == "SHORT" and l <= t1_price)
            stop_hit   = (direction == "LONG" and l <= stop) or \
                         (direction == "SHORT" and h >= stop)

            if stop_hit and t1_reached:
                stop_hit = False   # T1 takes priority on same bar

            if stop_hit:
                pnl = sign * (stop - entry_price)
                return {"pnl": round(pnl, 4), "pnl_r": round(pnl/orb_range, 4),
                        "phase": 0, "winner": False}

            if t1_reached:
                t1_hit = True
                t1_pnl = sign * (t1_price - entry_price)
                stop   = entry_price   # move to BE
                trail_hw = t1_price
                continue

        if t1_hit and not t2_hit:
            t2_reached = (direction == "LONG" and h >= t2_price) or \
                         (direction == "SHORT" and l <= t2_price)
            be_hit     = (direction == "LONG" and l <= stop) or \
                         (direction == "SHORT" and h >= stop)

            if be_hit and t2_reached:
                be_hit = False

            if be_hit:
                # Stopped at BE on T2+T3
                total = w1 * t1_pnl + w2 * 0.0 + w3 * 0.0
                return {"pnl": round(total, 4), "pnl_r": round(total/orb_range, 4),
                        "phase": 1, "winner": total > 0}

            if t2_reached:
                t2_hit = True
                t2_pnl = sign * (t2_price - entry_price)
                trail_hw = t2_price
                continue

        if t2_hit:
            trail_stop = (trail_hw - cfg["trail_r"] * orb_range) if direction == "LONG" \
                    else (trail_hw + cfg["trail_r"] * orb_range)
            trail_hit  = (direction == "LONG" and l <= trail_stop) or \
                         (direction == "SHORT" and h >= trail_stop)

            if trail_hit or eod:
                t3_exit = trail_stop if trail_hit else c
                t3_pnl  = sign * (t3_exit - entry_price)
                total   = w1 * t1_pnl + w2 * t2_pnl + w3 * t3_pnl
                return {"pnl": round(total, 4), "pnl_r": round(total/orb_range, 4),
                        "phase": 3, "winner": total > 0}

        if eod:
            eod_pnl = sign * (c - entry_price)
            if t1_hit:
                total = w1 * t1_pnl + (w2 + w3) * max(eod_pnl, 0.0)
            else:
                total = eod_pnl
            return {"pnl": round(total, 4), "pnl_r": round(total/orb_range, 4),
                    "phase": 2 if t1_hit else 0, "winner": total > 0}

    return {"pnl": 0.0, "pnl_r": 0.0, "phase": 0, "winner": False}


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def compute_stats(pnls):
    if not pnls:
        return {}
    n      = len(pnls)
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total  = sum(pnls)
    gw     = sum(wins)
    gl     = abs(sum(losses))
    pf     = gw / gl if gl > 0 else float("inf")
    expect = total / n
    mean_p = expect
    std    = math.sqrt(sum((p - mean_p)**2 for p in pnls) / n) if n > 1 else 0
    sharpe = (mean_p / std * math.sqrt(252)) if std > 0 else 0

    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        if equity > peak: peak = equity
        dd = peak - equity
        if dd > max_dd: max_dd = dd

    years   = n / 252
    ann_ret = total / years if years > 0 else 0
    calmar  = ann_ret / max_dd if max_dd > 0 else float("inf")

    return {
        "n": n, "wr": round(100*len(wins)/n,1), "pf": round(pf,2),
        "expect": round(expect,4), "total": round(total,2),
        "max_dd": round(max_dd,2), "sharpe": round(sharpe,2),
        "calmar": round(calmar,2), "ann_ret": round(ann_ret,2),
    }


# ---------------------------------------------------------------------------
# Run one config
# ---------------------------------------------------------------------------
def run_config(base_trades, regime_by_date, daily_bars, cfg):
    pnls = []
    for trade in base_trades:
        date    = trade["date"]
        regime  = regime_by_date.get(date, {})
        passed, score = passes_filter(trade, regime, cfg)
        if not passed:
            continue
        day_bars = daily_bars.get(date)
        if not day_bars:
            continue
        entry_time  = trade["entry_time"]
        entry_price = trade["entry_price"]
        stop_price  = trade["stop_price"]
        entry_idx   = next((i for i, (t,*_) in enumerate(day_bars) if t == entry_time), None)
        if entry_idx is None:
            continue
        result = simulate_tranche(trade["direction"], entry_price, stop_price,
                                  entry_idx, day_bars, cfg)
        pnls.append(result["pnl"])
    return compute_stats(pnls)


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------
GRID = {
    "long_only":        [True],                     # SHORT edge too small
    "align_min":        [0.60, 0.70, 0.75, 0.80],
    "require_rsi_bull": [False, True],
    "require_narrow_cpr": [False, True],
    "t1_r":             [0.75, 1.0],
    "t2_r":             [1.5, 2.0],
    "trail_r":          [1.0, 1.5],
    "weights":          [(1/3, 1/3, 1/3), (0.25, 0.25, 0.50)],  # equal vs runner-heavy
}


def grid_configs():
    keys = list(GRID.keys())
    vals = list(GRID.values())
    for combo in product(*vals):
        cfg = dict(zip(keys, combo))
        # Skip invalid: t2 must be > t1
        if cfg["t2_r"] <= cfg["t1_r"]:
            continue
        yield cfg


def cfg_label(cfg):
    w = cfg["weights"]
    wstr = "equal" if abs(w[0] - 1/3) < 0.01 else "runner50"
    return (
        f"align{int(cfg['align_min']*100)}"
        f"_rsi{'Y' if cfg['require_rsi_bull'] else 'N'}"
        f"_cpr{'Y' if cfg['require_narrow_cpr'] else 'N'}"
        f"_T1={cfg['t1_r']}R_T2={cfg['t2_r']}R_trail={cfg['trail_r']}R_{wstr}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    daily_bars  = load_csv_to_daily_bars(CSV_FILE)

    with open(TRADES_FILE) as f:
        base_trades = json.load(f)["trades"]

    with open(REGIME_FILE) as f:
        regime_by_date = {t["date"]: t.get("regime", {}) for t in json.load(f)["trades"]}

    print(f"\nRunning grid search …", flush=True)
    all_results = []
    configs = list(grid_configs())
    n_configs = len(configs)

    for i, cfg in enumerate(configs):
        if i % 20 == 0:
            print(f"  {i}/{n_configs} …", flush=True)
        s = run_config(base_trades, regime_by_date, daily_bars, cfg)
        if not s or s.get("n", 0) < 50:   # skip configs with too few trades
            continue
        all_results.append({"cfg": cfg, "label": cfg_label(cfg), "stats": s})

    # Sort by Calmar (primary), then total P&L (secondary)
    def sort_key(r):
        s = r["stats"]
        cal = s["calmar"] if s["calmar"] != float("inf") else 999
        return (-cal, -s["total"])

    all_results.sort(key=sort_key)

    # ── Print top 20 by Calmar ────────────────────────────────────────────────
    SEP = "=" * 110
    print(f"\n{SEP}")
    print("  TOP CONFIGS BY CALMAR  (LONG-only, ORB ≤ Q3=$2.25)")
    print(SEP)
    print(f"  {'#':>3}  {'n':>5}  {'WR':>6}  {'PF':>5}  {'Exp':>7}  {'Total':>8}  "
          f"{'MaxDD':>7}  {'Sharpe':>6}  {'Calmar':>6}  Config")
    print(f"  {'-'*3}  {'-'*5}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*8}  "
          f"{'-'*7}  {'-'*6}  {'-'*6}  {'-'*40}")

    for rank, r in enumerate(all_results[:20], 1):
        s = r["stats"]
        pf_s  = f"{s['pf']:.2f}"   if s["pf"] != float("inf") else "∞"
        cal_s = f"{s['calmar']:.2f}" if s["calmar"] != float("inf") else "∞"
        print(f"  {rank:>3}  {s['n']:>5}  {s['wr']:>5.1f}%  {pf_s:>5}  "
              f"{s['expect']:>+7.4f}  ${s['total']:>+7.2f}  "
              f"${s['max_dd']:>6.2f}  {s['sharpe']:>6.2f}  {cal_s:>6}  {r['label']}")

    # ── Top 10 by Total P&L ──────────────────────────────────────────────────
    by_pnl = sorted(all_results, key=lambda r: -r["stats"]["total"])
    print(f"\n{SEP}")
    print("  TOP CONFIGS BY TOTAL P&L")
    print(SEP)
    print(f"  {'#':>3}  {'n':>5}  {'WR':>6}  {'PF':>5}  {'Exp':>7}  {'Total':>8}  "
          f"{'MaxDD':>7}  {'Sharpe':>6}  {'Calmar':>6}  Config")
    print(f"  {'-'*3}  {'-'*5}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*8}  "
          f"{'-'*7}  {'-'*6}  {'-'*6}  {'-'*40}")
    for rank, r in enumerate(by_pnl[:10], 1):
        s = r["stats"]
        pf_s  = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "∞"
        cal_s = f"{s['calmar']:.2f}" if s["calmar"] != float("inf") else "∞"
        print(f"  {rank:>3}  {s['n']:>5}  {s['wr']:>5.1f}%  {pf_s:>5}  "
              f"{s['expect']:>+7.4f}  ${s['total']:>+7.2f}  "
              f"${s['max_dd']:>6.2f}  {s['sharpe']:>6.2f}  {cal_s:>6}  {r['label']}")

    # ── Best config deep-dive ────────────────────────────────────────────────
    best = all_results[0]
    print(f"\n{SEP}")
    print(f"  BEST CONFIG ANNUAL BREAKDOWN")
    print(SEP)
    print(f"  {best['label']}")
    print()

    # Re-run best config collecting per-year P&Ls
    bcfg = best["cfg"]
    by_year: dict = defaultdict(list)
    for trade in base_trades:
        date   = trade["date"]
        regime = regime_by_date.get(date, {})
        passed, _ = passes_filter(trade, regime, bcfg)
        if not passed:
            continue
        day_bars = daily_bars.get(date)
        if not day_bars:
            continue
        entry_idx = next((i for i, (t,*_) in enumerate(day_bars) if t == trade["entry_time"]), None)
        if entry_idx is None:
            continue
        result = simulate_tranche(trade["direction"], trade["entry_price"],
                                  trade["stop_price"], entry_idx, day_bars, bcfg)
        by_year[trade["year"]].append(result["pnl"])

    print(f"  {'Year':<6} {'n':>5} {'WR':>7} {'PF':>6} {'Total':>9} {'Exp':>8} {'Sharpe':>7} {'Calmar':>7}")
    print(f"  {'-'*6} {'-'*5} {'-'*7} {'-'*6} {'-'*9} {'-'*8} {'-'*7} {'-'*7}")
    for yr in sorted(by_year):
        s = compute_stats(by_year[yr])
        if not s: continue
        pf_s  = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "∞"
        cal_s = f"{s['calmar']:.2f}" if s["calmar"] != float("inf") else "∞"
        print(f"  {yr:<6} {s['n']:>5} {s['wr']:>6.1f}% {pf_s:>6} "
              f"${s['total']:>+8.2f} {s['expect']:>+8.4f} {s['sharpe']:>7.2f} {cal_s:>7}")

    # ── Save ──────────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    save_data = {
        "top_by_calmar": [{"label": r["label"], "stats": r["stats"], "cfg": {
            k: (list(v) if isinstance(v, tuple) else v) for k, v in r["cfg"].items()
        }} for r in all_results[:20]],
        "top_by_pnl": [{"label": r["label"], "stats": r["stats"], "cfg": {
            k: (list(v) if isinstance(v, tuple) else v) for k, v in r["cfg"].items()
        }} for r in by_pnl[:10]],
        "best_cfg": {k: (list(v) if isinstance(v, tuple) else v) for k, v in bcfg.items()},
        "best_annual": {yr: compute_stats(pnls) for yr, pnls in by_year.items()},
    }
    with open(OUT_FILE, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"\n  Results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
