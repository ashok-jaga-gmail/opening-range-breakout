"""
orb_cpr_targets.py — ORB with CPR-Level Exits

Instead of fixed R-multiple targets, use the natural market structure
levels from the Central Pivot Range as profit targets:

  T1: Daily R1   (first resistance above entry, median 0.71R)
      → If daily R1 is already below entry (price in breakout mode,
        all daily resistance absorbed), skip to weekly R1 as T1.
      → After T1: stop moves to BREAKEVEN.

  T2: Weekly R1  (67% above entry when valid, median 2.44R)
      → If weekly R1 is below T1, use fixed 2R fallback.
      → After T2: begin 1.5R trailing stop.

  T3: Runner     (50% of position, trail or EOD)

Why CPR levels as targets?
  - Floor pivots are widely watched; price frequently stalls there
  - Dynamic targets adjust to market structure, not arbitrary multiples
  - Daily R1 naturally corresponds to ~0.71R (short capture, high WR)
  - Weekly R1 naturally corresponds to ~2.44R (aligns with our best fixed target)

Filters: same as optimised strategy.
  LONG only | CPR above_top | Alignment ≥ 70% | ORB% ≤ 0.64%

Tranche weights: 25% / 25% / 50%

Comparison: CPR targets vs fixed R targets (1R/2R/trail)

Outputs:
  stdout — comparison and analysis
  tmp/cpr_target_results.json
"""

import csv
import json
import lzma
import math
import os
from collections import defaultdict

_HERE       = os.path.dirname(os.path.abspath(__file__))
CSV_FILE    = os.path.join(_HERE, "qqq_1m_2018_2026.csv.xz")
TRADES_FILE = "/tmp/orb_paper_results.json"
REGIME_FILE = "/tmp/orb_regime_results.json"
OUT_FILE    = os.path.join(_HERE, "tmp", "cpr_target_results.json")

ALIGN_MIN   = 0.70
ORB_MAX_PCT = 0.64
TRAIL_R     = 1.5
W1, W2, W3  = 0.25, 0.25, 0.50

# Fixed-target fallbacks
FIXED_T1_R  = 1.0
FIXED_T2_R  = 2.0


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
    print(f"  {sum(len(v) for v in daily.values()):,} bars, {len(daily)} days.")
    return dict(daily)


# ---------------------------------------------------------------------------
# Regime / filter
# ---------------------------------------------------------------------------
BULL_CPR  = {"above_top"}; BULL_RSI = {"bullish","overbought"}
BULL_MACD = {"bullish_cross","bullish","bullish_fade"}
BEAR_CPR  = {"below_bottom"}; BEAR_RSI = {"bearish","oversold"}
BEAR_MACD = {"bearish_cross","bearish","bearish_fade"}

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
        if val in (None, "unknown"): continue
        available += 1
        if direction == "LONG" and val in bull_set: aligned += 1
        elif direction == "SHORT" and val in bear_set: aligned += 1
    return aligned / available if available else None

def passes_filter(trade, regime):
    if trade["direction"] != "LONG": return False, None
    if trade["orb_range"] / trade["entry_price"] * 100 > ORB_MAX_PCT: return False, None
    if regime.get("cpr_daily_state") not in (None,"unknown","above_top"): return False, None
    score = compute_alignment(regime, "LONG")
    if score is None or score < ALIGN_MIN: return False, score
    return True, score


# ---------------------------------------------------------------------------
# CPR target selection
# ---------------------------------------------------------------------------
def select_cpr_targets(entry_price, orb_range, regime):
    """
    Return (t1_price, t2_price, t1_label, t2_label) using CPR levels.
    Falls back to fixed R targets when CPR levels are unavailable or below entry.
    """
    daily_r1  = regime.get("cpr_daily_r1")
    weekly_r1 = regime.get("cpr_weekly_r1")

    fixed_t1  = entry_price + FIXED_T1_R * orb_range
    fixed_t2  = entry_price + FIXED_T2_R * orb_range

    # T1: prefer daily R1 if above entry by at least 0.25R (noise floor)
    if daily_r1 and daily_r1 > entry_price + 0.25 * orb_range:
        t1_price = daily_r1
        t1_label = "daily_R1"
    else:
        # Daily R1 already absorbed → price in strong trend, use weekly R1 as T1
        if weekly_r1 and weekly_r1 > entry_price + 0.25 * orb_range:
            t1_price = weekly_r1
            t1_label = "weekly_R1_as_T1"
        else:
            t1_price = fixed_t1
            t1_label = "fixed_1R"

    # T2: weekly R1 if above T1 by at least 0.5R
    if weekly_r1 and weekly_r1 > t1_price + 0.5 * orb_range:
        t2_price = weekly_r1
        t2_label = "weekly_R1"
    else:
        t2_price = max(fixed_t2, t1_price + 0.5 * orb_range)
        t2_label = "fixed_2R" if t2_price == fixed_t2 else "fixed_2R_adj"

    return t1_price, t2_price, t1_label, t2_label


# ---------------------------------------------------------------------------
# Tranche simulation with dynamic targets
# ---------------------------------------------------------------------------
def simulate_cpr_tranche(direction, entry_price, stop_price,
                          t1_target, t2_target, entry_idx, day_bars):
    orb_range = abs(entry_price - stop_price)
    sign      = 1  # LONG only

    stop     = stop_price
    trail_hw = entry_price
    t1_hit   = t2_hit = False
    t1_p = t2_p = t3_p = 0.0
    t3_reason = "OPEN"

    post_bars = day_bars[entry_idx + 1:]

    for t, o, h, l, c, v in post_bars:
        eod = t >= "15:59"
        trail_hw = max(trail_hw, h)

        if not t1_hit:
            stop_hit   = l <= stop
            t1_reached = h >= t1_target
            if stop_hit and t1_reached: stop_hit = False
            if stop_hit:
                pnl = stop - entry_price
                return _build(False, False, pnl, pnl, pnl, orb_range, "STOP")
            if t1_reached:
                t1_hit = True
                t1_p   = t1_target - entry_price
                stop   = entry_price
                trail_hw = t1_target
                if eod: break
                continue

        if t1_hit and not t2_hit:
            be_hit     = l <= stop
            t2_reached = h >= t2_target
            if be_hit and t2_reached: be_hit = False
            if be_hit:
                return _build(True, False, t1_p, 0.0, 0.0, orb_range, "BE_STOP")
            if t2_reached:
                t2_hit = True
                t2_p   = t2_target - entry_price
                trail_hw = t2_target
                if eod: break
                continue

        if t2_hit:
            trail_stop = trail_hw - TRAIL_R * orb_range
            trail_hit  = l <= trail_stop
            if trail_hit or eod:
                t3_exit = trail_stop if trail_hit else c
                t3_p    = t3_exit - entry_price
                t3_reason = "TRAIL" if trail_hit else "EOD"
                break

        if eod:
            if not t1_hit:
                pnl = c - entry_price
                return _build(False, False, pnl, pnl, pnl, orb_range, "EOD")
            if not t2_hit:
                eod_pnl = max(0.0, c - entry_price)  # stop is at BE, so min 0
                return _build(True, False, t1_p, eod_pnl, eod_pnl, orb_range, "EOD")
            break

    total = W1 * t1_p + W2 * t2_p + W3 * t3_p
    return {
        "t1_hit": t1_hit, "t2_hit": t2_hit,
        "combined_pnl": round(total, 4),
        "combined_pnl_r": round(total / orb_range if orb_range else 0, 4),
        "t3_reason": t3_reason, "winner": total > 0,
    }

def _build(t1h, t2h, t1p, t2p, t3p, orb_r, reason):
    total = W1 * t1p + W2 * t2p + W3 * t3p
    return {
        "t1_hit": t1h, "t2_hit": t2h,
        "combined_pnl": round(total, 4),
        "combined_pnl_r": round(total / orb_r if orb_r else 0, 4),
        "t3_reason": reason, "winner": total > 0,
    }


# ---------------------------------------------------------------------------
# Fixed tranche for comparison (replicates optimized strategy)
# ---------------------------------------------------------------------------
def simulate_fixed_tranche(entry_price, stop_price, entry_idx, day_bars):
    t1 = entry_price + FIXED_T1_R * abs(entry_price - stop_price)
    t2 = entry_price + FIXED_T2_R * abs(entry_price - stop_price)
    return simulate_cpr_tranche("LONG", entry_price, stop_price,
                                t1, t2, entry_idx, day_bars)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def compute_stats(pnls):
    if not pnls: return {}
    n      = len(pnls)
    wins   = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total  = sum(pnls)
    gw, gl = sum(wins), abs(sum(losses))
    pf     = gw / gl if gl > 0 else float("inf")
    expect = total / n
    std    = math.sqrt(sum((p-expect)**2 for p in pnls)/n) if n>1 else 0
    sharpe = (expect/std*math.sqrt(252)) if std>0 else 0
    eq = pk = mdd = 0.0
    for p in pnls:
        eq += p
        if eq > pk: pk = eq
        dd = pk - eq
        if dd > mdd: mdd = dd
    years   = n / 252
    ann_ret = total / years if years > 0 else 0
    calmar  = ann_ret / mdd if mdd > 0 else float("inf")
    return {
        "n": n, "wr": round(100*len(wins)/n,1),
        "pf": round(pf,2) if pf!=float("inf") else "∞",
        "expect": round(expect,4), "total": round(total,2),
        "max_dd": round(mdd,2), "sharpe": round(sharpe,2),
        "calmar": round(calmar,2) if calmar!=float("inf") else "∞",
    }

def percentile(data, p):
    if not data: return 0.0
    s = sorted(data)
    k = (len(s)-1)*p/100.0
    lo, hi = int(k), min(int(k)+1, len(s)-1)
    return s[lo]+(s[hi]-s[lo])*(k-lo)

SEP = "=" * 95

def print_stats(label, s, w=30):
    if not s: return
    print(f"  {label:<{w}} n={s['n']:>5}  WR={s['wr']:>5.1f}%  PF={str(s['pf']):>5}  "
          f"Exp=${s['expect']:>+8.4f}  Tot=${s['total']:>+8.2f}  "
          f"MaxDD=${s['max_dd']:>6.2f}  Sharpe={s['sharpe']:>5.2f}  Calmar={str(s['calmar'])}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    daily_bars = load_csv_to_daily_bars(CSV_FILE)
    with open(TRADES_FILE) as f:
        base_trades = json.load(f)["trades"]
    with open(REGIME_FILE) as f:
        regime_by_date = {t["date"]: t.get("regime",{}) for t in json.load(f)["trades"]}

    cpr_pnls   = []
    fixed_pnls = []
    records    = []
    t1_labels  = defaultdict(int)
    t2_labels  = defaultdict(int)

    for trade in base_trades:
        date    = trade["date"]
        regime  = regime_by_date.get(date, {})
        passed, score = passes_filter(trade, regime)
        if not passed: continue
        day_bars = daily_bars.get(date)
        if not day_bars: continue

        entry_price = trade["entry_price"]
        stop_price  = trade["stop_price"]
        orb_range   = trade["orb_range"]
        entry_time  = trade["entry_time"]

        entry_idx = next((i for i,(t,*_) in enumerate(day_bars) if t==entry_time), None)
        if entry_idx is None: continue

        # CPR targets
        t1_p, t2_p, t1_lbl, t2_lbl = select_cpr_targets(entry_price, orb_range, regime)
        t1_labels[t1_lbl] += 1
        t2_labels[t2_lbl] += 1

        cpr_sim   = simulate_cpr_tranche("LONG", entry_price, stop_price,
                                          t1_p, t2_p, entry_idx, day_bars)
        fixed_sim = simulate_fixed_tranche(entry_price, stop_price, entry_idx, day_bars)

        cpr_pnls.append(cpr_sim["combined_pnl"])
        fixed_pnls.append(fixed_sim["combined_pnl"])

        records.append({
            "date":        date,
            "year":        trade["year"],
            "entry_price": entry_price,
            "orb_range":   orb_range,
            "t1_label":    t1_lbl,
            "t2_label":    t2_lbl,
            "t1_target_r": round((t1_p - entry_price) / orb_range, 2),
            "t2_target_r": round((t2_p - entry_price) / orb_range, 2),
            "cpr_pnl":     cpr_sim["combined_pnl"],
            "fixed_pnl":   fixed_sim["combined_pnl"],
            "cpr_t3":      cpr_sim["t3_reason"],
            "fixed_t3":    fixed_sim["t3_reason"],
            "cpr_winner":  cpr_sim["winner"],
            "fixed_winner":fixed_sim["winner"],
            "alignment":   score,
        })

    # ── Print ─────────────────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  CPR-LEVEL TARGETS vs FIXED R TARGETS — Filtered LONG Trades")
    print(SEP)
    print_stats("CPR targets (dR1→wR1)", compute_stats(cpr_pnls))
    print_stats("Fixed targets (1R→2R)", compute_stats(fixed_pnls))

    # T1/T2 label breakdown
    print(f"\n  T1 target source:")
    for lbl, cnt in sorted(t1_labels.items(), key=lambda x: -x[1]):
        pct = 100*cnt/len(records)
        sub = [r["cpr_pnl"] for r in records if r["t1_label"]==lbl]
        s   = compute_stats(sub)
        print(f"    {lbl:<22} {cnt:>4} ({pct:>4.0f}%)  WR={s['wr']:.1f}%  "
              f"Exp=${s['expect']:>+7.4f}  Tot=${s['total']:>+7.2f}")

    print(f"\n  T2 target source:")
    for lbl, cnt in sorted(t2_labels.items(), key=lambda x: -x[1]):
        pct = 100*cnt/len(records)
        sub = [r["cpr_pnl"] for r in records if r["t2_label"]==lbl]
        s   = compute_stats(sub)
        print(f"    {lbl:<22} {cnt:>4} ({pct:>4.0f}%)  WR={s['wr']:.1f}%  "
              f"Exp=${s['expect']:>+7.4f}  Tot=${s['total']:>+7.2f}")

    # T1 target distance distribution
    t1_dists = [r["t1_target_r"] for r in records]
    t2_dists = [r["t2_target_r"] for r in records]
    print(f"\n  T1 target in R — P25={percentile(t1_dists,25):.2f}  "
          f"Median={percentile(t1_dists,50):.2f}  P75={percentile(t1_dists,75):.2f}")
    print(f"  T2 target in R — P25={percentile(t2_dists,25):.2f}  "
          f"Median={percentile(t2_dists,50):.2f}  P75={percentile(t2_dists,75):.2f}")

    # Annual breakdown
    print(f"\n{SEP}")
    print("  ANNUAL BREAKDOWN")
    print(SEP)
    by_year_c: dict = defaultdict(list)
    by_year_f: dict = defaultdict(list)
    for r in records:
        by_year_c[r["year"]].append(r["cpr_pnl"])
        by_year_f[r["year"]].append(r["fixed_pnl"])
    print(f"  {'Year':<6}  {'n':>4}  {'CPR WR':>7} {'CPR Tot':>8}  {'Fix WR':>7} {'Fix Tot':>8}  {'Diff':>7}")
    print(f"  {'-'*6}  {'-'*4}  {'-'*7} {'-'*8}  {'-'*7} {'-'*8}  {'-'*7}")
    for yr in sorted(by_year_c):
        cp = by_year_c[yr]; fp = by_year_f[yr]
        c_wr = 100*sum(1 for p in cp if p>0)/len(cp)
        f_wr = 100*sum(1 for p in fp if p>0)/len(fp)
        diff = sum(cp) - sum(fp)
        print(f"  {yr:<6}  {len(cp):>4}  {c_wr:>6.1f}% ${sum(cp):>+7.2f}  "
              f"{f_wr:>6.1f}% ${sum(fp):>+7.2f}  ${diff:>+6.2f}")

    # Trade-level comparison: when does CPR beat fixed?
    print(f"\n{SEP}")
    print("  CPR vs FIXED — trade-level comparison")
    print(SEP)
    cpr_better  = [r for r in records if r["cpr_pnl"] > r["fixed_pnl"]]
    fixed_better = [r for r in records if r["fixed_pnl"] > r["cpr_pnl"]]
    same         = [r for r in records if r["cpr_pnl"] == r["fixed_pnl"]]
    print(f"  CPR better : {len(cpr_better):>4} ({100*len(cpr_better)/len(records):.1f}%)  "
          f"avg diff = ${sum(r['cpr_pnl']-r['fixed_pnl'] for r in cpr_better)/len(cpr_better):>+.4f}")
    print(f"  Fixed better:{len(fixed_better):>4} ({100*len(fixed_better)/len(records):.1f}%)  "
          f"avg diff = ${sum(r['fixed_pnl']-r['cpr_pnl'] for r in fixed_better)/len(fixed_better):>+.4f}")
    print(f"  Same        :{len(same):>4} ({100*len(same)/len(records):.1f}%)")

    # When daily R1 is used as T1: does lower target help or hurt?
    dr1_trades = [r for r in records if r["t1_label"] == "daily_R1"]
    if dr1_trades:
        dr1_cpr_pnl   = [r["cpr_pnl"]   for r in dr1_trades]
        dr1_fixed_pnl = [r["fixed_pnl"] for r in dr1_trades]
        print(f"\n  When daily R1 used as T1 (n={len(dr1_trades)}, median target={percentile([r['t1_target_r'] for r in dr1_trades],50):.2f}R):")
        print(f"    CPR total: ${sum(dr1_cpr_pnl):>+.2f}  WR={100*sum(1 for p in dr1_cpr_pnl if p>0)/len(dr1_cpr_pnl):.1f}%")
        print(f"    Fixed tot: ${sum(dr1_fixed_pnl):>+.2f}  WR={100*sum(1 for p in dr1_fixed_pnl if p>0)/len(dr1_fixed_pnl):.1f}%")

    # Save
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump({
            "cpr_stats":   compute_stats(cpr_pnls),
            "fixed_stats": compute_stats(fixed_pnls),
            "trades":      records,
        }, f, indent=2)
    print(f"\n  Results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
