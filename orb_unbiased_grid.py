"""
orb_unbiased_grid.py — Comprehensive unbiased grid search, all breakouts, up to 4/day

Key differences from orb_options_grid.py:
  1. NO filters — all breakout signals taken
  2. UNBIASED entry: OPEN of bar *after* signal bar (next minute)
  3. Multi-breakout: up to 4 per day (re-fires when price re-enters ORB)
  4. Both directions: calls for LONG, puts for SHORT
  5. Wider grid: more PT2 steps, 4th SL bucket, 3 weight schemes
  6. Reports per trade tier (1st/2nd/3rd/4th breakout of day)

Data: QQQ 1-min CSV + options parquet
Output: tmp/orb_unbiased_grid_results.json
"""

import csv
import json
import lzma
import math
import os
from collections import defaultdict
from itertools import product

_HERE    = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(_HERE, "qqq_1m_2018_2026.csv.xz")
OUT_FILE = os.path.join(_HERE, "tmp", "orb_unbiased_grid_results.json")

OPT_DIRS = {
    "2025": os.path.expanduser("~/backups/QQQ/2025/Options-OHLC/thetadata-2025"),
    "2026": os.path.expanduser("~/backups/QQQ/2026/Options-OHLC/thetadata-2026"),
}

YEARS_ACTIVE = {"2025", "2026"}
MAX_TRADES_PER_DAY = 4

# EOD exit times to test
EOD_TIMES = ["15:45", "15:50", "15:55", "15:59"]

# Expanded grid
PT1_OPTIONS = [25, 50, 75, 100]
PT2_OPTIONS = [100, 125, 150, 175, 200]
PT3_OPTIONS = [200, 250, 300]
SL_OPTIONS  = [30, 50, 75, 100]
WEIGHT_OPTIONS = {
    "equal":    (1/3,  1/3,  1/3),
    "runner50": (0.25, 0.25, 0.50),
    "runner67": (1/6,  1/6,  2/3),
}
STRIKE_OFFSETS = {"ATM": 0, "OTM": 1}


# ---------------------------------------------------------------------------
# Step 1: Load QQQ 1-min bars
# ---------------------------------------------------------------------------
def load_daily_bars(csv_path):
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
    print(f"  Loaded {sum(len(v) for v in daily.values()):,} bars, {len(daily)} days", flush=True)
    return dict(daily)


# ---------------------------------------------------------------------------
# Step 2: Detect all breakout signals for a day (up to MAX_TRADES_PER_DAY)
# ---------------------------------------------------------------------------
def find_all_breakouts(day_bars, max_trades=MAX_TRADES_PER_DAY):
    """
    Returns list of {signal_time, direction, entry_price} for each breakout.
    - ORB = high/low of 09:30–09:44
    - Signal fires when bar CLOSE at 09:45+ exits ORB in a new direction
    - Resets when price closes back inside ORB
    - Tracks last_dir; new signal fires on any exit (same or opposite direction
      after re-entering ORB)
    """
    orb_h = orb_l = None
    for t, o, h, l, c, v in day_bars:
        if t < "09:30": continue
        if t > "09:44": break
        orb_h = max(orb_h, h) if orb_h is not None else h
        orb_l = min(orb_l, l) if orb_l is not None else l

    if orb_h is None or (orb_h - orb_l) < 0.10:
        return []

    signals = []
    last_dir = None  # last breakout direction; None = inside range

    for t, o, h, l, c, v in day_bars:
        if t < "09:45" or t > "15:00":
            continue
        if len(signals) >= max_trades:
            break

        if c > orb_h:
            if last_dir != "LONG":
                signals.append({
                    "signal_time": t,
                    "direction": "LONG",
                    "entry_price": c,   # underlying close (for strike calc)
                    "orb_high": orb_h,
                    "orb_low": orb_l,
                })
                last_dir = "LONG"
        elif c < orb_l:
            if last_dir != "SHORT":
                signals.append({
                    "signal_time": t,
                    "direction": "SHORT",
                    "entry_price": c,
                    "orb_high": orb_h,
                    "orb_low": orb_l,
                })
                last_dir = "SHORT"
        else:
            # Price back inside range — reset
            last_dir = None

    return signals


def next_minute(hhmm):
    """Increment HH:MM by 1 minute."""
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    m += 1
    if m >= 60:
        h += 1
        m = 0
    return f"{h:02d}:{m:02d}"


# ---------------------------------------------------------------------------
# Step 3: Load options bars for a date (calls OR puts)
# ---------------------------------------------------------------------------
def load_option_bars(date_str, year, right="call"):
    opt_dir = OPT_DIRS.get(year)
    if not opt_dir:
        return {}
    path = os.path.join(opt_dir, f"qqq-options-1m-{date_str.replace('-','')}.parquet")
    if not os.path.exists(path):
        return {}
    try:
        import pandas as pd
        df = pd.read_parquet(path)
    except Exception:
        return {}

    result = {}
    right_vals = {"call", "c"} if right == "call" else {"put", "p"}
    for _, row in df.iterrows():
        r = str(row.get("right", "")).strip().lower()
        if r not in right_vals:
            continue
        strike = int(row.get("strike", 0))
        ts = str(row.get("timestamp", ""))
        hhmm = ts[11:16] if len(ts) >= 16 else ""
        if not hhmm:
            continue
        result.setdefault(strike, {})[hhmm] = {
            "open":  float(row.get("open")  or 0),
            "high":  float(row.get("high")  or 0),
            "low":   float(row.get("low")   or 0),
            "close": float(row.get("close") or 0),
        }
    return result


# ---------------------------------------------------------------------------
# Step 4: Simulate one option trade (unbiased: enter at next-bar OPEN)
# ---------------------------------------------------------------------------
def simulate_trade(strike_bars, signal_time, direction, pt1, pt2, pt3, sl, weights, eod_time="15:55"):
    """
    Enter at OPEN of bar at/after next_minute(signal_time).
    Conservative: if same bar hits both PT and stop, stop wins (pre-T1 phase).
    Returns P&L per contract ($) or None.
    """
    entry_time = next_minute(signal_time)
    times = sorted(strike_bars.keys())

    # Find entry bar
    entry_opt = None
    start_idx = None
    for i, t in enumerate(times):
        if t >= entry_time and strike_bars[t]["open"] > 0:
            entry_opt = strike_bars[t]["open"]
            start_idx = i
            break

    if entry_opt is None or entry_opt <= 0:
        return None

    pt1_price = entry_opt * (1 + pt1 / 100)
    pt2_price = entry_opt * (1 + pt2 / 100)
    pt3_price = entry_opt * (1 + pt3 / 100)
    sl_price  = entry_opt * (1 - sl  / 100)

    W1, W2, W3 = weights
    stop = sl_price
    max_opt = entry_opt
    phase = "pre_t1"
    t1_exit = t2_exit = t3_exit = None

    for t in times[start_idx:]:
        bar = strike_bars[t]
        if bar["close"] <= 0:
            continue
        eod = t >= eod_time

        h, l, c = bar["high"], bar["low"], bar["close"]
        max_opt = max(max_opt, h)

        if phase == "pre_t1":
            sl_hit = l <= stop
            pt1_hit = h >= pt1_price
            if sl_hit and pt1_hit:
                sl_hit = False  # conservative: stop wins unless both hit — actually PT wins per updated rule
                # Actually: in unbiased version, stop wins when both same bar
                sl_hit = True
                pt1_hit = False

            if sl_hit:
                pnl = (stop - entry_opt) * 100
                return W1 * pnl + W2 * pnl + W3 * pnl

            if pt1_hit:
                t1_exit = pt1_price
                stop = entry_opt  # move to breakeven
                phase = "post_t1"
                if eod:
                    t2_exit = t3_exit = c
                    break
                continue

            if eod:
                pnl = (c - entry_opt) * 100
                return W1 * pnl + W2 * pnl + W3 * pnl

        elif phase == "post_t1":
            be_hit = l <= stop
            pt2_hit = h >= pt2_price
            if be_hit and pt2_hit:
                be_hit = True
                pt2_hit = False

            if be_hit:
                t2_exit = t3_exit = entry_opt
                break

            if pt2_hit:
                t2_exit = pt2_price
                stop = max_opt * (1 - sl / 100)
                phase = "post_t2"
                if eod:
                    t3_exit = c
                    break
                continue

            if eod:
                t2_exit = t3_exit = max(c, entry_opt)
                break

        elif phase == "post_t2":
            stop = max_opt * (1 - sl / 100)
            trail_hit = l <= stop
            pt3_hit = h >= pt3_price
            if trail_hit and pt3_hit:
                trail_hit = True
                pt3_hit = False

            if trail_hit:
                t3_exit = stop
                break

            if pt3_hit:
                t3_exit = pt3_price
                break

            if eod:
                t3_exit = c
                break

    if t1_exit is None:
        return None
    if t2_exit is None:
        t2_exit = entry_opt
    if t3_exit is None:
        t3_exit = entry_opt

    return (W1 * (t1_exit - entry_opt) + W2 * (t2_exit - entry_opt) + W3 * (t3_exit - entry_opt)) * 100


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------
def compute_stats(pnls):
    if not pnls:
        return {}
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    total = sum(pnls)
    gw = sum(wins)
    gl = abs(sum(losses))
    pf = gw / gl if gl > 0 else float("inf")
    expect = total / n
    std = math.sqrt(sum((p - expect) ** 2 for p in pnls) / n) if n > 1 else 0
    sharpe = (expect / std * math.sqrt(252)) if std > 0 else 0
    eq = pk = mdd = 0.0
    for p in pnls:
        eq += p
        if eq > pk:
            pk = eq
        dd = pk - eq
        if dd > mdd:
            mdd = dd
    years = n / 252
    ann_ret = total / years if years > 0 else 0
    calmar = ann_ret / mdd if mdd > 0 else float("inf")
    return {
        "n": n,
        "wr": round(100 * len(wins) / n, 1),
        "pf": round(pf, 2) if pf != float("inf") else "inf",
        "expect": round(expect, 2),
        "total": round(total, 2),
        "max_dd": round(mdd, 2),
        "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2) if calmar != float("inf") else "inf",
    }


def sort_key(r):
    cal = r["stats"].get("calmar", 0)
    tot = r["stats"].get("total", 0)
    if cal == "inf":
        cal = 9999
    return (-cal, -tot)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    daily_bars = load_daily_bars(CSV_FILE)

    # -- Generate all signals for 2025+2026 ----------------------------------
    print("\nGenerating breakout signals …", flush=True)
    # signal_data: list of {date, year, tier (1-4), signal_time, direction, entry_price, strike_bars_call, strike_bars_put}
    signal_data = []

    sorted_dates = sorted(d for d in daily_bars if d[:4] in YEARS_ACTIVE)
    opt_cache = {}  # (date, right) -> {strike: {hhmm: bar}}

    for date in sorted_dates:
        year = date[:4]
        bars = daily_bars[date]
        signals = find_all_breakouts(bars, max_trades=MAX_TRADES_PER_DAY)
        if not signals:
            continue

        # Load options once per day
        call_key = (date, "call")
        put_key  = (date, "put")
        if call_key not in opt_cache:
            opt_cache[call_key] = load_option_bars(date, year, right="call")
        if put_key not in opt_cache:
            opt_cache[put_key]  = load_option_bars(date, year, right="put")

        calls = opt_cache[call_key]
        puts  = opt_cache[put_key]

        if not calls and not puts:
            continue

        for tier, sig in enumerate(signals, start=1):
            atm = int(round(sig["entry_price"]))
            strike_data = {}
            for lbl, offset in STRIKE_OFFSETS.items():
                if sig["direction"] == "LONG":
                    s = atm + offset   # calls: ATM or ATM+1
                    if s in calls:
                        strike_data[lbl] = calls[s]
                else:
                    s = atm - offset   # puts: ATM or ATM-1
                    if s in puts:
                        strike_data[lbl] = puts[s]

            if not strike_data:
                continue

            signal_data.append({
                "date":        date,
                "year":        year,
                "tier":        tier,
                "signal_time": sig["signal_time"],
                "direction":   sig["direction"],
                "entry_price": sig["entry_price"],
                "strikes":     strike_data,
            })

    # Clear opt cache to free memory
    del opt_cache

    total_sigs = len(signal_data)
    by_tier = defaultdict(int)
    for s in signal_data:
        by_tier[s["tier"]] += 1
    print(f"  Total signals: {total_sigs}")
    for tier in sorted(by_tier):
        print(f"    Trade #{tier}: {by_tier[tier]} signals")

    # -- Build grid configs --------------------------------------------------
    configs = []
    for pt1, pt2, pt3, sl, (wt_lbl, wt), strike_lbl in product(
        PT1_OPTIONS, PT2_OPTIONS, PT3_OPTIONS, SL_OPTIONS,
        list(WEIGHT_OPTIONS.items()), list(STRIKE_OFFSETS.keys())
    ):
        if pt2 <= pt1: continue
        if pt3 <= pt2: continue
        configs.append((pt1, pt2, pt3, sl, wt_lbl, wt, strike_lbl))

    print(f"\nRunning {len(configs)} configs × {total_sigs} signals × {len(EOD_TIMES)} EOD times …", flush=True)

    SEP = "=" * 120
    HDR = f"  {'#':>3}  {'n':>4}  {'WR%':>5}  {'PF':>5}  {'Exp':>7}  {'Total':>9}  {'MaxDD':>8}  {'Sharpe':>6}  {'Calmar':>6}  Config"
    DIV = f"  {'-'*3}  {'-'*4}  {'-'*5}  {'-'*5}  {'-'*7}  {'-'*9}  {'-'*8}  {'-'*6}  {'-'*6}  {'-'*55}"

    def print_row(rank, r):
        s = r["stats"]
        print(f"  {rank:>3}  {s['n']:>4}  {s['wr']:>5.1f}  {str(s['pf']):>5}  "
              f"${s['expect']:>+6.2f}  ${s['total']:>+8.2f}  "
              f"${s['max_dd']:>7.2f}  {s['sharpe']:>6.2f}  {str(s['calmar']):>6}  {r['label']}")

    all_eod_results = {}   # eod_time -> sorted list of results
    best_by_eod = {}       # eod_time -> best result

    for eod_time in EOD_TIMES:
        print(f"\n{'='*60}", flush=True)
        print(f"  EOD exit: {eod_time}", flush=True)
        print(f"{'='*60}", flush=True)

        all_results = []

        for i, (pt1, pt2, pt3, sl, wt_lbl, wt, strike_lbl) in enumerate(configs):
            if i % 200 == 0:
                print(f"  {i}/{len(configs)} …", flush=True)

            pnls_all = []
            pnls_by_tier = defaultdict(list)

            for sd in signal_data:
                bars = sd["strikes"].get(strike_lbl)
                if bars is None:
                    continue
                pnl = simulate_trade(
                    bars, sd["signal_time"], sd["direction"],
                    pt1, pt2, pt3, sl, wt, eod_time=eod_time
                )
                if pnl is not None:
                    pnls_all.append(pnl)
                    pnls_by_tier[sd["tier"]].append(pnl)

            if len(pnls_all) < 50:
                continue

            s_all = compute_stats(pnls_all)
            if not s_all:
                continue

            tier_stats = {}
            for tier in range(1, MAX_TRADES_PER_DAY + 1):
                ps = pnls_by_tier.get(tier, [])
                if ps:
                    tier_stats[f"trade{tier}"] = compute_stats(ps)

            label = f"PT{pt1}/{pt2}/{pt3}_SL{sl}_{strike_lbl}_{wt_lbl}"
            all_results.append({
                "label":  label,
                "pt1": pt1, "pt2": pt2, "pt3": pt3,
                "sl": sl, "strike": strike_lbl, "weights": wt_lbl,
                "stats": s_all,
                "by_tier": tier_stats,
            })

        all_results.sort(key=sort_key)
        all_eod_results[eod_time] = all_results
        if all_results:
            best_by_eod[eod_time] = all_results[0]

        # Top 10 by Calmar for this EOD time
        print(f"\n  TOP 10 BY CALMAR — EOD {eod_time}")
        print(f"  {HDR}")
        print(f"  {DIV}")
        for rank, r in enumerate(all_results[:10], 1):
            print_row(rank, r)

        # Top 5 by P&L
        by_pnl = sorted(all_results, key=lambda r: -(r["stats"]["total"] if isinstance(r["stats"]["total"], (int, float)) else 0))
        print(f"\n  TOP 5 BY TOTAL P&L — EOD {eod_time}")
        print(f"  {HDR}")
        print(f"  {DIV}")
        for rank, r in enumerate(by_pnl[:5], 1):
            print_row(rank, r)

        # Best config tier breakdown
        if all_results:
            best = all_results[0]
            tier_hdr = f"    {'Tier':>6}  {'n':>4}  {'WR%':>5}  {'Total':>9}  {'MaxDD':>8}  {'Calmar':>7}"
            print(f"\n  BEST ({best['label']}) tier breakdown:")
            print(tier_hdr)
            for tier in sorted(best["by_tier"]):
                ts = best["by_tier"][tier]
                print(f"    {tier:>6}  {ts['n']:>4}  {ts['wr']:>5.1f}  "
                      f"${ts['total']:>+8.2f}  ${ts['max_dd']:>7.2f}  {str(ts['calmar']):>7}")

    # -- Cross-EOD comparison summary ----------------------------------------
    print(f"\n{SEP}")
    print(f"  CROSS-EOD COMPARISON — Best config per EOD time")
    print(SEP)
    print(f"  {'EOD':>5}  {'WR%':>5}  {'Total':>10}  {'MaxDD':>8}  {'Calmar':>7}  Config")
    print(f"  {'-'*5}  {'-'*5}  {'-'*10}  {'-'*8}  {'-'*7}  {'-'*50}")
    for eod_time in EOD_TIMES:
        if eod_time in best_by_eod:
            b = best_by_eod[eod_time]
            s = b["stats"]
            print(f"  {eod_time}  {s['wr']:>5.1f}  ${s['total']:>+9,.2f}  "
                  f"${s['max_dd']:>7,.2f}  {str(s['calmar']):>7}  {b['label']}")

    # -- Monthly breakdown for the overall best config -----------------------
    # Find the single best config across all EOD times by Calmar
    overall_best_eod = max(best_by_eod, key=lambda e: (
        best_by_eod[e]["stats"]["calmar"] if best_by_eod[e]["stats"]["calmar"] != "inf" else 9999
    ))
    overall_best = best_by_eod[overall_best_eod]
    print(f"\n{SEP}")
    print(f"  OVERALL BEST: {overall_best['label']} @ EOD {overall_best_eod}")
    print(SEP)

    wt = WEIGHT_OPTIONS[overall_best["weights"]]
    by_month = defaultdict(list)
    for sd in signal_data:
        bars = sd["strikes"].get(overall_best["strike"])
        if bars is None:
            continue
        pnl = simulate_trade(
            bars, sd["signal_time"], sd["direction"],
            overall_best["pt1"], overall_best["pt2"], overall_best["pt3"],
            overall_best["sl"], wt, eod_time=overall_best_eod
        )
        if pnl is not None:
            by_month[sd["date"][:7]].append(pnl)

    print(f"  {'Month':>7}  {'n':>4}  {'WR%':>5}  {'Total':>9}  {'Avg':>7}")
    for month in sorted(by_month):
        ps = by_month[month]
        wr = 100 * sum(1 for p in ps if p > 0) / len(ps)
        print(f"  {month}  {len(ps):>4}  {wr:>5.1f}  ${sum(ps):>+8.2f}  ${sum(ps)/len(ps):>+6.2f}")

    # -- Save ----------------------------------------------------------------
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    save_data = {
        "signal_counts": {f"trade{k}": v for k, v in by_tier.items()},
        "eod_times": EOD_TIMES,
        "best_by_eod": {
            eod: {
                "label": best_by_eod[eod]["label"],
                "stats": best_by_eod[eod]["stats"],
            }
            for eod in EOD_TIMES if eod in best_by_eod
        },
    }
    for eod_time in EOD_TIMES:
        results = all_eod_results.get(eod_time, [])
        by_pnl_eod = sorted(results, key=lambda r: -(r["stats"]["total"] if isinstance(r["stats"]["total"], (int, float)) else 0))
        save_data[f"eod_{eod_time.replace(':','')}"] = {
            "top_by_calmar": results[:20],
            "top_by_pnl": by_pnl_eod[:10],
        }
    with open(OUT_FILE, "w") as f:
        json.dump(save_data, f, indent=2)
    print(f"\n  Results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
