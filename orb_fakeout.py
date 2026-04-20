"""
orb_fakeout.py — ORB with Reversal (Fakeout) Entry

The standard ORB takes the first breakout. This script adds a second trade:
if the first breakout stops out (price returns through the ORB opposite edge),
watch for a close back through that opposite edge → enter in the REVERSE direction.

Why fakeouts work:
  - Traders trapped on the first breakout are panic-exiting, adding momentum
  - Stop hunts sweep liquidity at the ORB edge before the real move
  - The confirmed close (not just a touch) filters out noise

Trade structure:
  Trade 1 : first close outside ORB at/after 09:45 (standard ORB)
  Trade 2 : if Trade 1 stops out, first CLOSE back through the opposite ORB edge
            Entry = close of that bar, direction = opposite of Trade 1
            Stop  = ORB edge in the new direction (same 1R structure)

Applies the optimised tranche exit to BOTH trades:
  T1=1R (25%) → stop to BE | T2=2R (25%) → 1.5R trail | T3=50% runner

Filters: same as optimised strategy (align ≥70%, CPR above_top for LONG,
         ORB_MAX_PCT=0.64%)
         For the REVERSAL trade the CPR/alignment filter is relaxed —
         the fakeout itself is the signal.

Outputs:
  stdout — comparison tables
  tmp/fakeout_results.json
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
OUT_FILE    = os.path.join(_HERE, "tmp", "fakeout_results.json")

ALIGN_MIN   = 0.70
ORB_MAX_PCT = 0.64
T1_R        = 1.0
T2_R        = 2.0
TRAIL_R     = 1.5
W1, W2, W3  = 0.25, 0.25, 0.50


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
        if val in (None, "unknown"): continue
        available += 1
        if direction == "LONG"  and val in bull_set: aligned += 1
        elif direction == "SHORT" and val in bear_set: aligned += 1
    return aligned / available if available else None

def passes_primary_filter(trade, regime):
    """Filter for the first (primary) ORB trade."""
    if trade["direction"] != "LONG":
        return False, None
    if trade["orb_range"] / trade["entry_price"] * 100 > ORB_MAX_PCT:
        return False, None
    if regime.get("cpr_daily_state") not in (None, "unknown", "above_top"):
        return False, None
    score = compute_alignment(regime, "LONG")
    if score is None or score < ALIGN_MIN:
        return False, score
    return True, score


# ---------------------------------------------------------------------------
# Tranche simulation — returns exit time and whether trade stopped out
# ---------------------------------------------------------------------------
def simulate_tranche(direction, entry_price, stop_price, entry_idx, day_bars):
    orb_range = abs(entry_price - stop_price)
    sign      = 1 if direction == "LONG" else -1
    t1_target = entry_price + sign * T1_R * orb_range
    t2_target = entry_price + sign * T2_R * orb_range
    stop      = stop_price
    trail_hw  = entry_price

    t1_hit = t2_hit = False
    t1_p = t2_p = t3_p = 0.0
    t3_reason = "OPEN"
    stopped_out = False
    stop_out_idx = None
    stop_out_time = None

    post_bars = day_bars[entry_idx + 1:]

    for bi, (t, o, h, l, c, v) in enumerate(post_bars):
        eod = t >= "15:59"
        trail_hw = max(trail_hw, h) if direction == "LONG" else min(trail_hw, l)

        if not t1_hit:
            stop_hit   = (direction == "LONG" and l <= stop) or \
                         (direction == "SHORT" and h >= stop)
            t1_reached = (direction == "LONG" and h >= t1_target) or \
                         (direction == "SHORT" and l <= t1_target)

            if stop_hit and t1_reached:
                stop_hit = False

            if stop_hit:
                stopped_out   = True
                stop_out_idx  = entry_idx + 1 + bi
                stop_out_time = t
                pnl = sign * (stop - entry_price)
                return {
                    "t1_hit": False, "t2_hit": False,
                    "combined_pnl": round(pnl, 4),
                    "combined_pnl_r": round(pnl / orb_range, 4),
                    "t3_reason": "STOP",
                    "winner": pnl > 0,
                    "stopped_out": True,
                    "stop_out_idx": stop_out_idx,
                    "stop_out_time": stop_out_time,
                    "stop_price": stop,
                }

            if t1_reached:
                t1_hit = True
                t1_p   = sign * (t1_target - entry_price)
                stop   = entry_price
                trail_hw = t1_target
                if eod:
                    break
                continue

        if t1_hit and not t2_hit:
            be_hit     = (direction == "LONG" and l <= stop) or \
                         (direction == "SHORT" and h >= stop)
            t2_reached = (direction == "LONG" and h >= t2_target) or \
                         (direction == "SHORT" and l <= t2_target)
            if be_hit and t2_reached:
                be_hit = False
            if be_hit:
                t2_p = t3_p = 0.0
                t3_reason = "BE_STOP"
                break
            if t2_reached:
                t2_hit = True
                t2_p   = sign * (t2_target - entry_price)
                trail_hw = t2_target
                if eod:
                    break
                continue

        if t2_hit:
            trail_stop = (trail_hw - TRAIL_R * orb_range) if direction == "LONG" \
                    else (trail_hw + TRAIL_R * orb_range)
            trail_hit  = (direction == "LONG" and l <= trail_stop) or \
                         (direction == "SHORT" and h >= trail_stop)
            if trail_hit or eod:
                t3_exit   = trail_stop if trail_hit else c
                t3_p      = sign * (t3_exit - entry_price)
                t3_reason = "TRAIL" if trail_hit else "EOD"
                break

        if eod:
            eod_pnl = sign * (c - entry_price)
            if not t1_hit:
                t1_p = t2_p = t3_p = eod_pnl / 3
            elif not t2_hit:
                t2_p = t3_p = max(eod_pnl, 0.0)
            t3_reason = "EOD"
            break

    total = W1 * t1_p + W2 * t2_p + W3 * t3_p
    return {
        "t1_hit": t1_hit, "t2_hit": t2_hit,
        "combined_pnl": round(total, 4),
        "combined_pnl_r": round(total / orb_range, 4),
        "t3_reason": t3_reason,
        "winner": total > 0,
        "stopped_out": False,
        "stop_out_idx": None,
        "stop_out_time": None,
        "stop_price": stop,
    }


# ---------------------------------------------------------------------------
# Find reversal entry after a failed first trade
# ---------------------------------------------------------------------------
def find_reversal_entry(failed_direction, orb_high, orb_low,
                        stop_out_idx, day_bars):
    """
    After the primary trade stops out, look for the first 1-min bar whose
    CLOSE crosses the ORB edge in the OPPOSITE direction.

    failed_direction = "LONG"  → looking for close < orb_low  (SHORT entry)
    failed_direction = "SHORT" → looking for close > orb_high (LONG  entry)

    Returns (reversal_direction, bar_idx, entry_price) or None.
    """
    rev_dir = "SHORT" if failed_direction == "LONG" else "LONG"

    for i in range(stop_out_idx, len(day_bars)):
        t, o, h, l, c, v = day_bars[i]
        if t > "15:58":
            break
        if rev_dir == "SHORT" and c < orb_low:
            return rev_dir, i, c
        if rev_dir == "LONG" and c > orb_high:
            return rev_dir, i, c
    return None


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
    gw, gl = sum(wins), abs(sum(losses))
    pf     = gw / gl if gl > 0 else float("inf")
    expect = total / n
    std    = math.sqrt(sum((p - expect)**2 for p in pnls) / n) if n > 1 else 0
    sharpe = (expect / std * math.sqrt(252)) if std > 0 else 0
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
        "n": n, "wins": len(wins), "losses": len(losses),
        "wr": round(100*len(wins)/n, 1),
        "pf": round(pf, 2) if pf != float("inf") else "∞",
        "expect": round(expect, 4), "total": round(total, 2),
        "max_dd": round(max_dd, 2), "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2) if calmar != float("inf") else "∞",
        "ann_ret": round(ann_ret, 2),
    }


def print_stats(label, s, width=28):
    if not s: return
    pf  = str(s["pf"])
    cal = str(s["calmar"])
    print(f"  {label:<{width}} n={s['n']:>5}  WR={s['wr']:>5.1f}%  PF={pf:>5}  "
          f"Exp=${s['expect']:>+8.4f}  Tot=${s['total']:>+8.2f}  "
          f"MaxDD=${s['max_dd']:>7.2f}  Sharpe={s['sharpe']:>5.2f}  Calmar={cal}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    daily_bars = load_csv_to_daily_bars(CSV_FILE)

    with open(TRADES_FILE) as f:
        base_trades = json.load(f)["trades"]

    with open(REGIME_FILE) as f:
        regime_by_date = {t["date"]: t.get("regime", {})
                          for t in json.load(f)["trades"]}

    print(f"\nLoaded {len(base_trades)} base trades.\n")

    primary_pnls   = []
    reversal_pnls  = []
    combined_pnls  = []   # primary + reversal on same day (when both occur)

    primary_records  = []
    reversal_records = []

    reversal_total = reversal_success = 0

    for trade in base_trades:
        date    = trade["date"]
        regime  = regime_by_date.get(date, {})

        passed, score = passes_primary_filter(trade, regime)
        if not passed:
            continue

        day_bars = daily_bars.get(date)
        if not day_bars:
            continue

        orb_high    = trade["orb_high"]
        orb_low     = trade["orb_low"]
        entry_time  = trade["entry_time"]
        entry_price = trade["entry_price"]
        stop_price  = trade["stop_price"]
        direction   = trade["direction"]  # always LONG after filter

        entry_idx = next((i for i, (t,*_) in enumerate(day_bars)
                          if t == entry_time), None)
        if entry_idx is None:
            continue

        # ── Primary trade ────────────────────────────────────────────────────
        sim = simulate_tranche(direction, entry_price, stop_price,
                               entry_idx, day_bars)
        primary_pnls.append(sim["combined_pnl"])
        primary_records.append({
            "date": date, "year": trade["year"],
            "direction": direction,
            "entry_time": entry_time, "entry_price": entry_price,
            "orb_range": trade["orb_range"],
            "pnl": sim["combined_pnl"],
            "t3_reason": sim["t3_reason"],
            "winner": sim["winner"],
            "alignment": score,
        })

        day_combined = sim["combined_pnl"]

        # ── Reversal trade (only if primary stopped out) ──────────────────────
        if sim["stopped_out"]:
            reversal_total += 1
            rev = find_reversal_entry(
                direction, orb_high, orb_low,
                sim["stop_out_idx"], day_bars
            )
            if rev is not None:
                rev_dir, rev_idx, rev_entry = rev
                rev_stop = orb_high if rev_dir == "SHORT" else orb_low
                rev_sim  = simulate_tranche(rev_dir, rev_entry, rev_stop,
                                            rev_idx, day_bars)
                reversal_pnls.append(rev_sim["combined_pnl"])
                reversal_records.append({
                    "date": date, "year": trade["year"],
                    "direction": rev_dir,
                    "primary_direction": direction,
                    "entry_time": day_bars[rev_idx][0],
                    "entry_price": rev_entry,
                    "orb_range": trade["orb_range"],
                    "pnl": rev_sim["combined_pnl"],
                    "t3_reason": rev_sim["t3_reason"],
                    "winner": rev_sim["winner"],
                })
                day_combined += rev_sim["combined_pnl"]
                reversal_success += 1

        combined_pnls.append(day_combined)

    SEP = "=" * 100

    # ── Overall comparison ────────────────────────────────────────────────────
    print(SEP)
    print("  ORB FAKEOUT ANALYSIS — Primary + Reversal Trades")
    print(SEP)
    print_stats("Primary trade only",    compute_stats(primary_pnls))
    print_stats("Reversal trade only",   compute_stats(reversal_pnls))
    print_stats("Combined (per day)",    compute_stats(combined_pnls))

    n_stopped = sum(1 for p in primary_pnls if p < 0)
    print(f"\n  Primary trades that stopped out:           {n_stopped} / {len(primary_pnls)} "
          f"({100*n_stopped/len(primary_pnls):.1f}%)")
    print(f"  Reversal entries triggered:                {reversal_total} / {n_stopped} "
          f"({100*reversal_total/n_stopped:.1f}% of stops)" if n_stopped else "")
    print(f"  Reversal entries found (close through ORB):{reversal_success} / {reversal_total} "
          f"({100*reversal_success/reversal_total:.1f}%)" if reversal_total else "")

    # ── Reversal trade phase breakdown ────────────────────────────────────────
    print(f"\n{SEP}")
    print("  REVERSAL TRADE — Phase Breakdown")
    print(SEP)
    phases = defaultdict(list)
    for r in reversal_records:
        phases[r["t3_reason"]].append(r["pnl"])
    print(f"  {'Exit':<12} {'n':>5} {'WR':>7} {'Avg P&L':>10}")
    print(f"  {'-'*12} {'-'*5} {'-'*7} {'-'*10}")
    for reason in sorted(phases):
        pnls = phases[reason]
        wr   = 100 * sum(1 for p in pnls if p > 0) / len(pnls)
        print(f"  {reason:<12} {len(pnls):>5} {wr:>6.1f}% ${sum(pnls)/len(pnls):>+9.4f}")

    # ── Annual breakdown ───────────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  ANNUAL BREAKDOWN")
    print(SEP)
    by_year_p: dict = defaultdict(list)
    by_year_r: dict = defaultdict(list)
    for rec in primary_records:
        by_year_p[rec["year"]].append(rec["pnl"])
    for rec in reversal_records:
        by_year_r[rec["year"]].append(rec["pnl"])

    print(f"  {'Year':<6}  {'Primary':>6}  {'WR':>6}  {'P Tot':>8}  |  "
          f"{'Reversal':>8}  {'WR':>6}  {'R Tot':>8}  |  {'DayTot':>8}")
    print(f"  {'-'*6}  {'-'*6}  {'-'*6}  {'-'*8}  |  "
          f"{'-'*8}  {'-'*6}  {'-'*8}  |  {'-'*8}")
    all_years = sorted(set(list(by_year_p) + list(by_year_r)))
    for yr in all_years:
        pp   = by_year_p.get(yr, [])
        rr   = by_year_r.get(yr, [])
        p_wr = 100 * sum(1 for p in pp if p > 0) / len(pp) if pp else 0
        r_wr = 100 * sum(1 for p in rr if p > 0) / len(rr) if rr else 0
        # Combined day total: sum primary + reversal for that year
        day_total = sum(pp) + sum(rr)
        print(f"  {yr:<6}  {len(pp):>6}  {p_wr:>5.1f}%  ${sum(pp):>+7.2f}  |  "
              f"{len(rr):>8}  {r_wr:>5.1f}%  ${sum(rr):>+7.2f}  |  ${day_total:>+7.2f}")

    print(f"\n  Totals:")
    print(f"    Primary  : {len(primary_pnls)} trades, "
          f"${sum(primary_pnls):+.2f}, "
          f"WR {100*sum(1 for p in primary_pnls if p>0)/len(primary_pnls):.1f}%")
    print(f"    Reversal : {len(reversal_pnls)} trades, "
          f"${sum(reversal_pnls):+.2f}, "
          f"WR {100*sum(1 for p in reversal_pnls if p>0)/len(reversal_pnls):.1f}%")
    print(f"    Combined : ${sum(primary_pnls)+sum(reversal_pnls):+.2f} total P&L")

    # ── Reversal-only stats ───────────────────────────────────────────────────
    print(f"\n{SEP}")
    print("  REVERSAL TRADE STANDALONE STATS")
    print(SEP)
    print_stats("All reversal trades", compute_stats(reversal_pnls))

    # ── Save ──────────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump({
            "primary_stats":  compute_stats(primary_pnls),
            "reversal_stats": compute_stats(reversal_pnls),
            "combined_stats": compute_stats(combined_pnls),
            "primary_trades":  primary_records,
            "reversal_trades": reversal_records,
        }, f, indent=2)
    print(f"\n  Results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
