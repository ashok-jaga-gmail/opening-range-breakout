"""
orb_tranche_strategy.py — High Win-Rate Tranche Exit Strategy

Builds on three prior analyses:
  - orb_paper_backtest.py   : baseline R2 trades (1,976 trades, 2018-2026)
  - orb_regime_indicators.py: CPR / RSI / MACD alignment per trade
  - orb_mae_mfe.py          : MAE/MFE behaviour of winners vs losers

═══════════════════════════════════════════════════════════════════
STRATEGY DESIGN
═══════════════════════════════════════════════════════════════════

Pre-trade filters (all must pass):
  1. Alignment score ≥ 60%  (≥6/10 regime indicators confirm direction)
  2. Daily CPR: price above_top for LONG, below_bottom for SHORT
  3. ORB range ≤ Q3 threshold ($2.25)  — exclude chaotic wide-range days

Entry:
  Same as baseline — first 1-min bar at/after 09:45 whose CLOSE breaks
  the 15-min ORB. Entry price = close of that bar.

Position: 3 equal tranches (1/3 each)

Tranche 1 — fast profit lock (1R target)
  MAE/MFE finding: 85.5% of R2 winners reach 1R in MFE.
  Exit 1/3 position at entry ± 1×ORB_range.
  → After T1 hit: move stop to BREAKEVEN on remaining 2/3.

Tranche 2 — core profit (1.5R target)
  69.1% of winners reach 1.5R in MFE.
  Exit next 1/3 at entry ± 1.5×ORB_range.
  → After T2 hit: begin trailing the runner with a 1R trailing stop.

Tranche 3 — runner (trailing or EOD)
  Trail at 1R below the highest high seen since entry (longs) or
  1R above the lowest low (shorts). Let it run to EOD if not stopped.
  If T1 and T2 both hit, minimum runner profit ≥ 1.5R (T2 level),
  so this tranche only adds, never takes back T1/T2 gains.

Stop management summary:
  Phase 0 (pre-T1):   stop at ORB opposite edge (-1R)
  Phase 1 (T1 hit):   stop moves to breakeven (0R)
  Phase 2 (T2 hit):   trailing stop at 1R below/above high-water mark
  Exit of last tranche: trail stop triggered, or EOD close

═══════════════════════════════════════════════════════════════════
OUTPUTS
═══════════════════════════════════════════════════════════════════
  stdout — strategy summary and comparison with baseline R2
  tmp/tranche_results.json — full per-trade breakdown
"""

import csv
import json
import lzma
import math
import os
from collections import defaultdict

# ---------------------------------------------------------------------------
_HERE       = os.path.dirname(os.path.abspath(__file__))
CSV_FILE    = os.path.join(_HERE, "qqq_1m_2018_2026.csv.xz")
TRADES_FILE = "/tmp/orb_paper_results.json"
REGIME_FILE = "/tmp/orb_regime_results.json"
OUT_FILE    = os.path.join(_HERE, "tmp", "tranche_results.json")

# Strategy parameters
ALIGN_MIN     = 0.60   # minimum alignment score to take a trade
ORB_MAX       = 2.25   # Q3 ORB range threshold — exclude wide days
T1_R          = 1.0    # Tranche 1 target in R-multiples
T2_R          = 1.5    # Tranche 2 target in R-multiples
TRAIL_R       = 1.0    # trailing stop distance (in R) after T2 hit


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_csv_to_daily_bars(csv_path: str) -> dict:
    print(f"Loading {csv_path} …", flush=True)
    daily: dict = defaultdict(list)
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
# Alignment score computation
# ---------------------------------------------------------------------------
BULL_CPR  = {"above_top"}
BEAR_CPR  = {"below_bottom"}
BULL_RSI  = {"bullish", "overbought"}
BEAR_RSI  = {"bearish", "oversold"}
BULL_MACD = {"bullish_cross", "bullish", "bullish_fade"}
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

def compute_alignment(regime: dict, direction: str) -> float | None:
    if not regime:
        return None
    aligned = 0
    available = 0
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


def daily_cpr_aligned(regime: dict, direction: str) -> bool:
    state = regime.get("cpr_daily_state")
    if state in (None, "unknown"):
        return True  # no data — don't filter
    if direction == "LONG":
        return state == "above_top"
    return state == "below_bottom"


# ---------------------------------------------------------------------------
# Trade filter
# ---------------------------------------------------------------------------
def passes_filter(trade: dict, regime: dict) -> tuple[bool, float | None]:
    """Returns (passes, alignment_score)."""
    direction = trade["direction"]
    orb_range = trade["orb_range"]

    # Filter 1: ORB range ≤ Q3
    if orb_range > ORB_MAX:
        return False, None

    # Filter 2: Daily CPR aligned
    if not daily_cpr_aligned(regime, direction):
        return False, None

    # Filter 3: Alignment score
    score = compute_alignment(regime, direction)
    if score is None or score < ALIGN_MIN:
        return False, score

    return True, score


# ---------------------------------------------------------------------------
# Tranche exit simulation
# ---------------------------------------------------------------------------
def simulate_tranche(
    direction: str,
    entry_price: float,
    stop_price: float,
    entry_bar_idx: int,
    day_bars: list,
) -> dict:
    """
    Walk bars forward from entry, simulating 3-tranche exit.

    Returns dict with:
        t1_exit, t1_time, t1_hit
        t2_exit, t2_time, t2_hit
        t3_exit, t3_time, t3_reason
        combined_pnl_r  (weighted: 1/3 each)
        combined_pnl    (in $, per-share equivalent: sum of tranche P&Ls / 3)
        phase_reached   (0=stopped before T1, 1=T1+stop, 2=T1+T2+stop, 3=all hit)
    """
    orb_range = abs(entry_price - stop_price)

    if direction == "LONG":
        t1_price = entry_price + T1_R * orb_range
        t2_price = entry_price + T2_R * orb_range
        sign     = 1
    else:
        t1_price = entry_price - T1_R * orb_range
        t2_price = entry_price - T2_R * orb_range
        sign     = -1

    stop     = stop_price   # moves to breakeven after T1
    trailing = False        # True after T2 hit
    trail_hw = entry_price  # high-water mark for trailing stop

    t1_hit   = False
    t2_hit   = False
    t1_exit  = t1_time = None
    t2_exit  = t2_time = None
    t3_exit  = t3_time = None
    t3_reason = "OPEN"

    post_bars = day_bars[entry_bar_idx + 1:]

    for t, o, h, l, c, v in post_bars:
        eod = t >= "15:59"

        # Update high-water mark for trail
        if direction == "LONG":
            trail_hw = max(trail_hw, h)
        else:
            trail_hw = min(trail_hw, l)

        # --- T1 check ---
        if not t1_hit:
            t1_reached = (direction == "LONG" and h >= t1_price) or \
                         (direction == "SHORT" and l <= t1_price)
            stop_hit   = (direction == "LONG" and l <= stop) or \
                         (direction == "SHORT" and h >= stop)

            if stop_hit and t1_reached:
                # Same bar — treat as stop (conservative)
                t1_reached = False

            if stop_hit:
                # Stopped before T1 — all 3 tranches exit at stop
                pnl = sign * (stop - entry_price)
                return {
                    "t1_hit": False, "t2_hit": False,
                    "t1_exit": stop, "t1_time": t,
                    "t2_exit": stop, "t2_time": t,
                    "t3_exit": stop, "t3_time": t, "t3_reason": "STOP",
                    "combined_pnl":   round(pnl, 4),
                    "combined_pnl_r": round(pnl / orb_range, 4),
                    "phase_reached":  0,
                    "stop_sequence": "full_stop",
                }

            if t1_reached:
                t1_hit  = True
                t1_exit = t1_price
                t1_time = t
                stop    = entry_price  # move to breakeven
                continue  # check T2 this same bar? use next bar for cleanliness

        # --- T2 check (only after T1) ---
        if t1_hit and not t2_hit:
            t2_reached = (direction == "LONG" and h >= t2_price) or \
                         (direction == "SHORT" and l <= t2_price)
            be_hit     = (direction == "LONG" and l <= stop) or \
                         (direction == "SHORT" and h >= stop)

            if be_hit and t2_reached:
                be_hit = False  # T2 priority if same bar

            if be_hit:
                # Stopped at breakeven — T2 + T3 exit at BE
                pnl_t1 = sign * (t1_price - entry_price)
                pnl_t2 = 0.0   # breakeven
                pnl_t3 = 0.0
                return {
                    "t1_hit": True, "t2_hit": False,
                    "t1_exit": t1_price, "t1_time": t1_time,
                    "t2_exit": stop,  "t2_time": t,
                    "t3_exit": stop,  "t3_time": t, "t3_reason": "BE_STOP",
                    "combined_pnl":   round((pnl_t1 + pnl_t2 + pnl_t3) / 3, 4),
                    "combined_pnl_r": round((pnl_t1/orb_range + 0 + 0) / 3, 4),
                    "phase_reached":  1,
                    "stop_sequence": "t1_then_be",
                }

            if t2_reached:
                t2_hit  = True
                t2_exit = t2_price
                t2_time = t
                trailing = True
                # Initialise trail from T2 price
                trail_hw = t2_price
                continue

        # --- T3 / runner check (only after T2, trailing stop) ---
        if t2_hit:
            if direction == "LONG":
                trail_stop = trail_hw - TRAIL_R * orb_range
                trail_hit  = l <= trail_stop
            else:
                trail_stop = trail_hw + TRAIL_R * orb_range
                trail_hit  = h >= trail_stop

            if trail_hit or eod:
                t3_exit   = trail_stop if trail_hit else c
                t3_time   = t
                t3_reason = "TRAIL" if trail_hit else "EOD"
                pnl_t1    = sign * (t1_price - entry_price)
                pnl_t2    = sign * (t2_price - entry_price)
                pnl_t3    = sign * (t3_exit  - entry_price)
                return {
                    "t1_hit": True, "t2_hit": True,
                    "t1_exit": t1_price, "t1_time": t1_time,
                    "t2_exit": t2_price, "t2_time": t2_time,
                    "t3_exit": round(t3_exit, 4), "t3_time": t3_time,
                    "t3_reason": t3_reason,
                    "combined_pnl":   round((pnl_t1 + pnl_t2 + pnl_t3) / 3, 4),
                    "combined_pnl_r": round((pnl_t1 + pnl_t2 + pnl_t3) / orb_range / 3, 4),
                    "phase_reached":  3,
                    "stop_sequence": "all_tranches",
                }

        if eod:
            # EOD exit — wherever we are
            if t1_hit and t2_hit:
                pass  # handled above
            elif t1_hit:
                pnl_t1 = sign * (t1_price - entry_price)
                pnl_t23 = sign * (c - entry_price)  # BE or better
                return {
                    "t1_hit": True, "t2_hit": False,
                    "t1_exit": t1_price, "t1_time": t1_time,
                    "t2_exit": c, "t2_time": t,
                    "t3_exit": c, "t3_time": t, "t3_reason": "EOD",
                    "combined_pnl":   round((pnl_t1 + pnl_t23 + pnl_t23) / 3, 4),
                    "combined_pnl_r": round((pnl_t1 + pnl_t23 + pnl_t23) / orb_range / 3, 4),
                    "phase_reached":  2,
                    "stop_sequence": "t1_then_eod",
                }
            else:
                pnl = sign * (c - entry_price)
                return {
                    "t1_hit": False, "t2_hit": False,
                    "t1_exit": c, "t1_time": t,
                    "t2_exit": c, "t2_time": t,
                    "t3_exit": c, "t3_time": t, "t3_reason": "EOD",
                    "combined_pnl":   round(pnl, 4),
                    "combined_pnl_r": round(pnl / orb_range, 4),
                    "phase_reached":  0,
                    "stop_sequence": "eod_no_t1",
                }

    # Fallback — should not reach here
    return {
        "t1_hit": False, "t2_hit": False,
        "t1_exit": entry_price, "t1_time": "15:59",
        "t2_exit": entry_price, "t2_time": "15:59",
        "t3_exit": entry_price, "t3_time": "15:59", "t3_reason": "EOD",
        "combined_pnl": 0.0, "combined_pnl_r": 0.0,
        "phase_reached": 0, "stop_sequence": "fallback",
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def compute_stats(pnls: list[float], label: str = "") -> dict:
    if not pnls:
        return {}
    n       = len(pnls)
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]
    total   = sum(pnls)
    gw      = sum(wins)
    gl      = abs(sum(losses))
    pf      = gw / gl if gl > 0 else float("inf")
    expect  = total / n
    mean_p  = expect
    var     = sum((p - mean_p) ** 2 for p in pnls) / n
    std     = math.sqrt(var) if var > 0 else 0
    sharpe  = (mean_p / std * math.sqrt(252)) if std > 0 else 0

    equity  = 0.0
    peak    = 0.0
    max_dd  = 0.0
    for p in pnls:
        equity += p
        if equity > peak:
            peak = equity
        dd = peak - equity
        if dd > max_dd:
            max_dd = dd

    years   = n / 252
    ann_ret = total / years if years > 0 else 0
    calmar  = ann_ret / max_dd if max_dd > 0 else float("inf")

    return {
        "n":       n,
        "wins":    len(wins),
        "wr":      round(100.0 * len(wins) / n, 1),
        "pf":      round(pf, 2),
        "expect":  round(expect, 4),
        "total":   round(total, 2),
        "max_dd":  round(max_dd, 2),
        "sharpe":  round(sharpe, 2),
        "calmar":  round(calmar, 2),
        "ann_ret": round(ann_ret, 2),
    }


def percentile(data, p):
    if not data: return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# Print helpers
# ---------------------------------------------------------------------------
SEP = "=" * 85

def section(title):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def print_stats_row(label, s, width=22):
    if not s: return
    wr_str = f"{s['wr']:.1f}%"
    pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "∞"
    print(f"  {label:<{width}} n={s['n']:>5}  WR={wr_str:>6}  PF={pf_str:>5}  "
          f"Exp={s['expect']:>+7.4f}  Tot=${s['total']:>+8.2f}  "
          f"MaxDD=${s['max_dd']:>7.2f}  Sharpe={s['sharpe']:>5.2f}  Calmar={s['calmar']:>5.2f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Load data
    daily_bars = load_csv_to_daily_bars(CSV_FILE)

    with open(TRADES_FILE) as f:
        base_data = json.load(f)
    base_trades = base_data["trades"]

    with open(REGIME_FILE) as f:
        regime_data = json.load(f)
    # Build regime lookup by date
    regime_by_date = {t["date"]: t.get("regime", {}) for t in regime_data["trades"]}

    print(f"\nLoaded {len(base_trades)} base trades.")

    # ── Run strategy ─────────────────────────────────────────────────────────
    results = []
    filter_counts = defaultdict(int)

    for trade in base_trades:
        date      = trade["date"]
        direction = trade["direction"]
        regime    = regime_by_date.get(date, {})

        # Alignment score (always compute for analysis)
        alignment = compute_alignment(regime, direction)

        # Apply filter
        passed, _ = passes_filter(trade, regime)
        if not passed:
            filter_counts["filtered"] += 1
            if trade["orb_range"] > ORB_MAX:
                filter_counts["orb_too_wide"] += 1
            elif not daily_cpr_aligned(regime, direction):
                filter_counts["cpr_fail"] += 1
            else:
                filter_counts["alignment_fail"] += 1
            continue

        filter_counts["passed"] += 1

        # Get bars and simulate
        day_bars = daily_bars.get(date)
        if day_bars is None:
            filter_counts["no_bars"] += 1
            continue

        # Find entry bar index
        entry_time  = trade["entry_time"]
        entry_price = trade["entry_price"]
        stop_price  = trade["stop_price"]
        entry_idx   = next(
            (i for i, (t, *_) in enumerate(day_bars) if t == entry_time),
            None
        )
        if entry_idx is None:
            filter_counts["no_entry_bar"] += 1
            continue

        result = simulate_tranche(direction, entry_price, stop_price, entry_idx, day_bars)

        results.append({
            "date":       date,
            "year":       trade["year"],
            "direction":  direction,
            "orb_range":  trade["orb_range"],
            "entry_time": entry_time,
            "entry_price": entry_price,
            "alignment":  round(alignment, 3) if alignment is not None else None,
            "baseline_r2_pnl": trade["exits"]["R2"]["pnl"],
            **result,
        })

    print(f"\n  Passed filter: {filter_counts['passed']}")
    print(f"  Filtered out : {filter_counts['filtered']}")
    print(f"    ↳ ORB too wide : {filter_counts['orb_too_wide']}")
    print(f"    ↳ CPR mismatch : {filter_counts['cpr_fail']}")
    print(f"    ↳ Low alignment: {filter_counts['alignment_fail']}")

    # ── Analysis ──────────────────────────────────────────────────────────────
    pnls_tranche  = [r["combined_pnl"] for r in results]
    pnls_baseline = [r["baseline_r2_pnl"] for r in results]

    section("TRANCHE STRATEGY vs BASELINE R2 — Filtered Trades")
    s_tranche  = compute_stats(pnls_tranche)
    s_baseline = compute_stats(pnls_baseline)
    print_stats_row("Tranche (T1/T2/Trail)", s_tranche)
    print_stats_row("Baseline R2",           s_baseline)

    section("TRANCHE STRATEGY — ANNUAL BREAKDOWN")
    by_year: dict = defaultdict(list)
    for r in results:
        by_year[r["year"]].append(r["combined_pnl"])
    print(f"  {'Year':<6} {'n':>5} {'WR':>7} {'PF':>6} {'Total':>9} {'Exp':>8} {'Sharpe':>7}")
    print(f"  {'-'*6} {'-'*5} {'-'*7} {'-'*6} {'-'*9} {'-'*8} {'-'*7}")
    for yr in sorted(by_year):
        s = compute_stats(by_year[yr])
        if not s: continue
        pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "∞"
        print(f"  {yr:<6} {s['n']:>5} {s['wr']:>6.1f}% {pf_str:>6} "
              f"${s['total']:>+8.2f} {s['expect']:>+8.4f} {s['sharpe']:>7.2f}")

    section("PHASE BREAKDOWN — How far did each trade progress?")
    phase_counts = defaultdict(lambda: {"n": 0, "pnls": []})
    for r in results:
        ph = r["phase_reached"]
        seq = r["stop_sequence"]
        phase_counts[seq]["n"] += 1
        phase_counts[seq]["pnls"].append(r["combined_pnl"])

    seq_labels = {
        "full_stop":    "Full stop (pre-T1)",
        "t1_then_be":   "T1 hit → stopped at BE",
        "t1_then_eod":  "T1 hit → EOD without T2",
        "all_tranches": "All 3 tranches (T1+T2+Runner)",
        "eod_no_t1":    "EOD, no T1 hit",
        "fallback":     "Fallback",
    }
    print(f"  {'Sequence':<32} {'n':>5} {'%':>6} {'WR':>7} {'AvgPnL':>9}")
    print(f"  {'-'*32} {'-'*5} {'-'*6} {'-'*7} {'-'*9}")
    n_total = len(results)
    for seq in ["all_tranches", "t1_then_eod", "t1_then_be", "full_stop", "eod_no_t1"]:
        d = phase_counts[seq]
        if not d["n"]: continue
        pct = 100.0 * d["n"] / n_total
        s   = compute_stats(d["pnls"])
        print(f"  {seq_labels.get(seq, seq):<32} {d['n']:>5} {pct:>5.1f}% "
              f"{s['wr']:>6.1f}% ${s.get('expect', 0):>+8.4f}")

    section("DIRECTION BREAKDOWN")
    for dirn in ["LONG", "SHORT"]:
        grp = [r["combined_pnl"] for r in results if r["direction"] == dirn]
        s   = compute_stats(grp)
        print_stats_row(dirn, s)

    section("ALIGNMENT SCORE BREAKDOWN (among filtered trades)")
    buckets = [
        ("60–70%",  0.60, 0.70),
        ("70–80%",  0.70, 0.80),
        ("80–90%",  0.80, 0.90),
        ("90–100%", 0.90, 1.01),
    ]
    print(f"  {'Alignment':>10} {'n':>5} {'WR':>7} {'PF':>6} {'AvgPnL':>9}")
    print(f"  {'-'*10} {'-'*5} {'-'*7} {'-'*6} {'-'*9}")
    for label, lo, hi in buckets:
        grp = [r["combined_pnl"] for r in results
               if r["alignment"] is not None and lo <= r["alignment"] < hi]
        s   = compute_stats(grp)
        if not s: continue
        pf_str = f"{s['pf']:.2f}" if s['pf'] != float('inf') else "∞"
        print(f"  {label:>10} {s['n']:>5} {s['wr']:>6.1f}% {pf_str:>6} ${s['expect']:>+8.4f}")

    section("RUNNER (T3) ANALYSIS — trades that reached all 3 tranches")
    all3 = [r for r in results if r["phase_reached"] == 3]
    t3_pnls_r = [(r["t3_exit"] - r["entry_price"]) * (1 if r["direction"]=="LONG" else -1) / r["orb_range"]
                 for r in all3]
    t3_reasons = defaultdict(int)
    for r in all3:
        t3_reasons[r["t3_reason"]] += 1
    print(f"  Trades reaching runner: {len(all3)} ({100*len(all3)/len(results):.1f}%)")
    print(f"  T3 exit reasons: ", end="")
    for reason, cnt in sorted(t3_reasons.items()):
        print(f"{reason}={cnt} ({100*cnt/len(all3):.0f}%)  ", end="")
    print()
    if t3_pnls_r:
        print(f"  T3 exit in R — P25={percentile(t3_pnls_r,25):.2f}  "
              f"Median={percentile(t3_pnls_r,50):.2f}  "
              f"P75={percentile(t3_pnls_r,75):.2f}  "
              f"Mean={sum(t3_pnls_r)/len(t3_pnls_r):.2f}")

    section("RISK/REWARD SUMMARY")
    full_stops  = [r for r in results if r["stop_sequence"] == "full_stop"]
    any_profit  = [r for r in results if r["combined_pnl"] > 0]
    avg_win     = sum(r["combined_pnl"] for r in any_profit) / len(any_profit) if any_profit else 0
    avg_loss    = sum(r["combined_pnl"] for r in full_stops) / len(full_stops) if full_stops else 0
    print(f"  Trades with any profit (T1+ hit or EOD+):  {len(any_profit)} ({100*len(any_profit)/len(results):.1f}%)")
    print(f"  Full stops (loss = −1R):                   {len(full_stops)} ({100*len(full_stops)/len(results):.1f}%)")
    print(f"  Avg winning trade P&L:  ${avg_win:>+7.4f}")
    print(f"  Avg full-stop loss:     ${avg_loss:>+7.4f}  "
          f"({avg_loss/results[0]['orb_range']:.2f}R on one day's range — varies)")
    print(f"  After T1 hit → max loss is $0 (breakeven stop)")
    print(f"  After T2 hit → min T3 gain ≥ trailing floor")

    # ── Save ──────────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    output = {
        "strategy": {
            "filters": {
                "alignment_min": ALIGN_MIN,
                "orb_max":       ORB_MAX,
                "daily_cpr":     "must align with direction",
            },
            "tranches": {
                "T1": f"{T1_R}R — then stop to BE",
                "T2": f"{T2_R}R — then trail at {TRAIL_R}R",
                "T3": f"Trail {TRAIL_R}R below/above HWM, or EOD",
            },
        },
        "summary": {
            "tranche": compute_stats(pnls_tranche),
            "baseline_r2": compute_stats(pnls_baseline),
        },
        "trades": results,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
