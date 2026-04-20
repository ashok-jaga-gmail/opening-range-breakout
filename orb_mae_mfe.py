"""
orb_mae_mfe.py — Maximum Adverse / Favorable Excursion analysis

For each ORB trade (all exit configs, or a specific one), re-walk the
1-minute bars from entry to exit to record:

  MAE (Maximum Adverse Excursion)
      The worst intrabar move against the position from entry to exit.
      LONG : entry_price − min(bar lows)
      SHORT: max(bar highs) − entry_price
      Expressed as R-multiples (÷ ORB range).

  MFE (Maximum Favorable Excursion)
      The best intrabar move in the position's favour from entry to exit.
      LONG : max(bar highs) − entry_price
      SHORT: entry_price − min(bar lows)
      Expressed as R-multiples.

The R2 exit is used as the canonical config.

Analysis sections:
  1. Distribution of MAE/MFE in R-multiples (percentiles)
  2. MAE distribution: Winners vs Losers
  3. MFE distribution: Winners vs Losers
  4. Efficiency ratio: exit_pnl / MFE  (did we capture the move?)
  5. Stop placement sensitivity: what WR if stop at 0.5R / 0.75R / 1.0R / 1.5R?
  6. Optimal target from MFE: how often did price reach 1R / 1.5R / 2R / 3R?
  7. MAE vs exit outcome scatter summary (bucketed)

Outputs:
  stdout — formatted tables
  tmp/mae_mfe_results.json — full per-trade data + summary stats
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
OUT_FILE    = os.path.join(_HERE, "tmp", "mae_mfe_results.json")

CANONICAL_CFG = "R2"   # exit config to analyse in depth


# ---------------------------------------------------------------------------
# Data loading (identical to backtest)
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
# MAE / MFE computation for one trade
# ---------------------------------------------------------------------------
def compute_mae_mfe(trade: dict, day_bars: list, cfg: str) -> dict | None:
    """
    Walk bars from entry bar to exit bar and compute MAE / MFE in R-multiples.
    Returns dict with mae_r, mfe_r, efficiency, and bucketed excursion data,
    or None if data is missing.
    """
    direction   = trade["direction"]
    entry_price = trade["entry_price"]
    entry_time  = trade["entry_time"]
    orb_range   = trade["orb_range"]
    exit_info   = trade["exits"].get(cfg)
    if exit_info is None or orb_range <= 0:
        return None

    exit_time   = exit_info["exit_time"]
    exit_reason = exit_info["exit_reason"]
    exit_pnl    = exit_info["pnl"]

    # Find bars from entry (inclusive) to exit (inclusive)
    in_range = False
    max_adv  = 0.0   # worst move against  (always >= 0)
    max_fav  = 0.0   # best  move in favour (always >= 0)

    # Track MFE over time for "reach" analysis
    max_fav_at_exit = 0.0

    for t, o, h, l, c, v in day_bars:
        if t < entry_time:
            continue
        if t > exit_time:
            break

        if direction == "LONG":
            adv = max(0.0, entry_price - l)   # how far LOW fell below entry
            fav = max(0.0, h - entry_price)    # how far HIGH rose above entry
        else:
            adv = max(0.0, h - entry_price)    # how far HIGH rose above entry
            fav = max(0.0, entry_price - l)    # how far LOW fell below entry

        max_adv = max(max_adv, adv)
        max_fav = max(max_fav, fav)

    mae_r  = max_adv / orb_range
    mfe_r  = max_fav / orb_range

    # Efficiency: how much of the MFE did we capture?
    # exit_pnl is already directional (+/-), always positive for winner
    exit_r = exit_pnl / orb_range if orb_range > 0 else 0.0
    efficiency = exit_r / mfe_r if mfe_r > 0 else (1.0 if exit_pnl > 0 else 0.0)

    return {
        "date":       trade["date"],
        "year":       trade["year"],
        "direction":  direction,
        "orb_range":  round(orb_range, 4),
        "exit_reason": exit_reason,
        "exit_pnl":   round(exit_pnl, 4),
        "exit_r":     round(exit_r, 4),
        "mae_r":      round(mae_r, 4),
        "mfe_r":      round(mfe_r, 4),
        "efficiency": round(efficiency, 4),
        "winner":     exit_pnl > 0,
    }


# ---------------------------------------------------------------------------
# Percentile helper
# ---------------------------------------------------------------------------
def percentile(data: list[float], p: float) -> float:
    if not data:
        return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def mean(data: list[float]) -> float:
    return sum(data) / len(data) if data else 0.0


def median(data: list[float]) -> float:
    return percentile(data, 50)


# ---------------------------------------------------------------------------
# Bucket a value into a label
# ---------------------------------------------------------------------------
def r_bucket(r: float) -> str:
    if r < 0.25: return "< 0.25R"
    if r < 0.50: return "0.25–0.50R"
    if r < 0.75: return "0.50–0.75R"
    if r < 1.00: return "0.75–1.00R"
    if r < 1.50: return "1.00–1.50R"
    if r < 2.00: return "1.50–2.00R"
    if r < 3.00: return "2.00–3.00R"
    return ">= 3.00R"


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------
SEP = "=" * 80

def section(title: str):
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def analyse_mae_mfe(records: list[dict]) -> dict:
    mae_all  = [r["mae_r"]    for r in records]
    mfe_all  = [r["mfe_r"]    for r in records]
    eff_all  = [r["efficiency"] for r in records]

    winners = [r for r in records if r["winner"]]
    losers  = [r for r in records if not r["winner"]]

    mae_w = [r["mae_r"] for r in winners]
    mae_l = [r["mae_r"] for r in losers]
    mfe_w = [r["mfe_r"] for r in winners]
    mfe_l = [r["mfe_r"] for r in losers]

    # ── 1. Overall distribution ───────────────────────────────────────────────
    section(f"MAE/MFE DISTRIBUTION — {CANONICAL_CFG} (n={len(records)})")
    print(f"  {'Metric':<30} {'P10':>7} {'P25':>7} {'P50':>7} {'P75':>7} {'P90':>7} {'Mean':>7}")
    print(f"  {'-'*30} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")
    for label, data in [("MAE (R)", mae_all), ("MFE (R)", mfe_all), ("Efficiency", eff_all)]:
        print(f"  {label:<30} "
              f"{percentile(data, 10):>7.3f} "
              f"{percentile(data, 25):>7.3f} "
              f"{percentile(data, 50):>7.3f} "
              f"{percentile(data, 75):>7.3f} "
              f"{percentile(data, 90):>7.3f} "
              f"{mean(data):>7.3f}")

    # ── 2. MAE: Winners vs Losers ─────────────────────────────────────────────
    section(f"MAE — WINNERS vs LOSERS")
    print(f"  {'Group':<10} {'n':>5} {'P25':>7} {'Median':>7} {'P75':>7} {'P90':>7} {'Mean':>7}")
    print(f"  {'-'*10} {'-'*5} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")
    for label, data in [("Winners", mae_w), ("Losers", mae_l)]:
        if not data: continue
        print(f"  {label:<10} {len(data):>5} "
              f"{percentile(data, 25):>7.3f} "
              f"{median(data):>7.3f} "
              f"{percentile(data, 75):>7.3f} "
              f"{percentile(data, 90):>7.3f} "
              f"{mean(data):>7.3f}")

    # ── 3. MFE: Winners vs Losers ─────────────────────────────────────────────
    section(f"MFE — WINNERS vs LOSERS")
    print(f"  {'Group':<10} {'n':>5} {'P25':>7} {'Median':>7} {'P75':>7} {'P90':>7} {'Mean':>7}")
    print(f"  {'-'*10} {'-'*5} {'-'*7} {'-'*7} {'-'*7} {'-'*7} {'-'*7}")
    for label, data in [("Winners", mfe_w), ("Losers", mfe_l)]:
        if not data: continue
        print(f"  {label:<10} {len(data):>5} "
              f"{percentile(data, 25):>7.3f} "
              f"{median(data):>7.3f} "
              f"{percentile(data, 75):>7.3f} "
              f"{percentile(data, 90):>7.3f} "
              f"{mean(data):>7.3f}")

    # ── 4. Stop sensitivity: how many trades survive tighter stops? ───────────
    section("STOP SENSITIVITY — WR if stop tightened")
    print(f"  (Trades where MAE exceeded threshold would have been stopped out early)")
    print(f"  {'Stop at':<14} {'Survive %':>11} {'Hypothetical WR':>16} {'Trades surviving':>18}")
    print(f"  {'-'*14} {'-'*11} {'-'*16} {'-'*18}")
    for stop_r in [0.25, 0.50, 0.75, 1.00]:
        survive = [r for r in records if r["mae_r"] <= stop_r]
        survive_w = [r for r in survive if r["winner"]]
        pct_survive = 100.0 * len(survive) / len(records)
        hyp_wr = 100.0 * len(survive_w) / len(survive) if survive else 0.0
        print(f"  {stop_r:.2f}R{'':<10} {pct_survive:>10.1f}% {hyp_wr:>15.1f}% {len(survive):>18}")

    # ── 5. Target reach: how often did MFE hit a given R? ────────────────────
    section("MFE TARGET REACH — % of trades whose MFE >= threshold")
    print(f"  {'Target':<10} {'All':>8} {'Winners':>10} {'Losers':>10}")
    print(f"  {'-'*10} {'-'*8} {'-'*10} {'-'*10}")
    for target_r in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]:
        all_pct = 100.0 * sum(1 for r in records if r["mfe_r"] >= target_r) / len(records)
        win_pct = 100.0 * sum(1 for r in winners if r["mfe_r"] >= target_r) / len(winners) if winners else 0
        los_pct = 100.0 * sum(1 for r in losers  if r["mfe_r"] >= target_r) / len(losers)  if losers  else 0
        print(f"  {target_r:.1f}R{'':<7} {all_pct:>7.1f}% {win_pct:>9.1f}% {los_pct:>9.1f}%")

    # ── 6. MAE bucket × Winner/Loser cross-tab ───────────────────────────────
    section("MAE BUCKET vs OUTCOME")
    buckets = ["< 0.25R", "0.25–0.50R", "0.50–0.75R", "0.75–1.00R", ">= 1.00R"]
    def mae_bucket(r):
        if r < 0.25: return "< 0.25R"
        if r < 0.50: return "0.25–0.50R"
        if r < 0.75: return "0.50–0.75R"
        if r < 1.00: return "0.75–1.00R"
        return ">= 1.00R"

    by_bucket: dict = defaultdict(lambda: {"w": 0, "l": 0})
    for rec in records:
        bkt = mae_bucket(rec["mae_r"])
        if rec["winner"]:
            by_bucket[bkt]["w"] += 1
        else:
            by_bucket[bkt]["l"] += 1

    print(f"  {'MAE bucket':<15} {'Wins':>6} {'Losses':>7} {'Total':>7} {'WR':>8}")
    print(f"  {'-'*15} {'-'*6} {'-'*7} {'-'*7} {'-'*8}")
    for bkt in buckets:
        d = by_bucket[bkt]
        w, l = d["w"], d["l"]
        tot = w + l
        wr = 100.0 * w / tot if tot else 0
        print(f"  {bkt:<15} {w:>6} {l:>7} {tot:>7} {wr:>7.1f}%")

    # ── 7. Efficiency summary ─────────────────────────────────────────────────
    section("CAPTURE EFFICIENCY (exit_pnl / MFE)")
    print(f"  Median efficiency for winners: {median([r['efficiency'] for r in winners]):.2%}")
    print(f"  Median efficiency for losers:  {median([r['efficiency'] for r in losers]):.2%}  (always ≤0 for losers)")
    print(f"  Trades with MFE>2R but exited <2R target: ", end="")
    missed = [r for r in records if r["mfe_r"] >= 2.0 and r["exit_r"] < 2.0]
    print(f"{len(missed)} ({100.0*len(missed)/len(records):.1f}%)")

    # ── 8. Year breakdown ─────────────────────────────────────────────────────
    section("ANNUAL MAE/MFE SUMMARY")
    by_year: dict = defaultdict(list)
    for rec in records:
        by_year[rec["year"]].append(rec)
    print(f"  {'Year':<6} {'n':>5} {'MedMAE':>8} {'MedMFE':>8} {'WR':>7} {'Eff(med)':>10}")
    print(f"  {'-'*6} {'-'*5} {'-'*8} {'-'*8} {'-'*7} {'-'*10}")
    for yr in sorted(by_year):
        yr_recs = by_year[yr]
        yr_mae = [r["mae_r"] for r in yr_recs]
        yr_mfe = [r["mfe_r"] for r in yr_recs]
        yr_wr  = 100.0 * sum(1 for r in yr_recs if r["winner"]) / len(yr_recs)
        yr_eff = [r["efficiency"] for r in yr_recs if r["winner"]]
        print(f"  {yr:<6} {len(yr_recs):>5} "
              f"{median(yr_mae):>8.3f} "
              f"{median(yr_mfe):>8.3f} "
              f"{yr_wr:>6.1f}% "
              f"{median(yr_eff) if yr_eff else 0:>9.3f}")

    # ── Return stats dict ──────────────────────────────────────────────────────
    return {
        "n": len(records),
        "mae": {
            "mean":   round(mean(mae_all), 4),
            "p25":    round(percentile(mae_all, 25), 4),
            "median": round(median(mae_all), 4),
            "p75":    round(percentile(mae_all, 75), 4),
            "p90":    round(percentile(mae_all, 90), 4),
        },
        "mfe": {
            "mean":   round(mean(mfe_all), 4),
            "p25":    round(percentile(mfe_all, 25), 4),
            "median": round(median(mfe_all), 4),
            "p75":    round(percentile(mfe_all, 75), 4),
            "p90":    round(percentile(mfe_all, 90), 4),
        },
        "mae_winner_median": round(median(mae_w), 4) if mae_w else None,
        "mae_loser_median":  round(median(mae_l), 4) if mae_l else None,
        "mfe_winner_median": round(median(mfe_w), 4) if mfe_w else None,
        "mfe_loser_median":  round(median(mfe_l), 4) if mfe_l else None,
        "efficiency_winner_median": round(median([r["efficiency"] for r in winners]), 4) if winners else None,
        "target_reach": {
            str(r): round(100.0 * sum(1 for rec in records if rec["mfe_r"] >= r) / len(records), 1)
            for r in [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
        },
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Load data
    daily_bars = load_csv_to_daily_bars(CSV_FILE)

    with open(TRADES_FILE) as f:
        data = json.load(f)
    all_trades = data["trades"]
    print(f"Loaded {len(all_trades)} trades from {TRADES_FILE}\n")

    # Compute MAE/MFE for each trade
    print(f"Computing MAE/MFE for cfg={CANONICAL_CFG} …", flush=True)
    records = []
    skipped = 0
    for trade in all_trades:
        date = trade["date"]
        day_bars = daily_bars.get(date)
        if day_bars is None:
            skipped += 1
            continue
        rec = compute_mae_mfe(trade, day_bars, CANONICAL_CFG)
        if rec is not None:
            records.append(rec)
        else:
            skipped += 1

    print(f"  {len(records)} records computed, {skipped} skipped.\n")

    # Run analysis
    summary = analyse_mae_mfe(records)

    # Save
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    output = {
        "config":  CANONICAL_CFG,
        "summary": summary,
        "trades":  records,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\n  Results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
