"""
orb_options_tranche_2025.py — 2025 Tranche Strategy: Underlying vs Options

Applies the optimized strategy (Session 6 best config) to 2025:
  Filters : LONG only | CPR above_top | Alignment ≥ 70% | ORB ≤ $2.25
  Tranches: T1=1.0R (25%) → BE stop | T2=2.0R (25%) → 1.5R trail | T3=50% runner

For each filtered trade:
  1. Simulate tranche exits on the underlying (1-min bars), capture exit times.
  2. Look up option prices (ATM / OTM / OTM+1) at those same exit times.
  3. Compute P&L for both underlying and options side by side.

Options path: ~/backups/QQQ/2025/Options-OHLC/thetadata-2025/qqq-options-1m-YYYYMMDD.parquet
Underlying  : qqq_1m_2018_2026.csv.xz (same as all other scripts)
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
OPTIONS_DIR = os.path.expanduser(
    "~/backups/QQQ/2025/Options-OHLC/thetadata-2025"
)
OUT_FILE    = os.path.join(_HERE, "tmp", "options_tranche_2025.json")

# ── Optimized strategy params (Session 6 best config) ────────────────────────
ALIGN_MIN = 0.70
ORB_MAX_PCT = 0.64   # Q3 of ORB% across 2018-2026; price-normalised
T1_R      = 1.0
T2_R      = 2.0
TRAIL_R   = 1.5
W1, W2, W3 = 0.25, 0.25, 0.50   # tranche weights

STRIKE_OFFSETS = {"ATM": 0, "OTM": 1, "OTM+1": 2}   # calls: ATM+N for LONG


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


def load_option_day(date_str):
    """Return {right: {strike: {hhmm: {open, close}}}} for one day."""
    date_compact = date_str.replace("-", "")
    path = os.path.join(OPTIONS_DIR, f"qqq-options-1m-{date_compact}.parquet")
    if not os.path.exists(path):
        return {}
    try:
        import pandas as pd
        df = pd.read_parquet(path)
    except Exception:
        return {}

    lookup = {}
    for _, row in df.iterrows():
        r = str(row.get("right", "")).strip().lower()
        right = "call" if r in ("call", "c") else ("put" if r in ("put", "p") else None)
        if not right:
            continue
        strike = int(row.get("strike", 0))
        ts     = str(row.get("timestamp", ""))
        hhmm   = ts[11:16] if len(ts) >= 16 else ""
        if not hhmm:
            continue
        open_  = float(row.get("open")  or 0)
        close_ = float(row.get("close") or 0)
        lookup.setdefault(right, {}).setdefault(strike, {})[hhmm] = {
            "open": open_, "close": close_
        }
    return lookup


# ---------------------------------------------------------------------------
# Regime / filter
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

def passes_filter(trade, regime):
    if trade["direction"] != "LONG":
        return False, None
    if trade["orb_range"] / trade["entry_price"] * 100 > ORB_MAX_PCT:
        return False, None
    if regime.get("cpr_daily_state") not in (None, "unknown", "above_top"):
        return False, None
    score = compute_alignment(regime, trade["direction"])
    if score is None or score < ALIGN_MIN:
        return False, score
    return True, score


# ---------------------------------------------------------------------------
# Tranche simulation — returns exit times per tranche
# ---------------------------------------------------------------------------
def simulate_tranche(direction, entry_price, stop_price, entry_idx, day_bars):
    """Returns dict with T1/T2/T3 exit prices, times, reasons, and combined P&L."""
    orb_range = abs(entry_price - stop_price)
    sign      = 1  # LONG only in this script
    t1_target = entry_price + T1_R * orb_range
    t2_target = entry_price + T2_R * orb_range
    stop      = stop_price
    trail_hw  = entry_price

    t1_hit = t2_hit = False
    t1_price = t1_time = None
    t2_price = t2_time = None
    t3_price = t3_time = None
    t3_reason = "OPEN"

    post_bars = day_bars[entry_idx + 1:]

    for t, o, h, l, c, v in post_bars:
        eod = t >= "15:59"
        trail_hw = max(trail_hw, h)

        if not t1_hit:
            stop_hit  = l <= stop
            t1_reached = h >= t1_target
            if stop_hit and t1_reached:
                stop_hit = False
            if stop_hit:
                return _exit_all(entry_price, stop, t, orb_range, "STOP")
            if t1_reached:
                t1_hit   = True
                t1_price = t1_target
                t1_time  = t
                stop     = entry_price
                trail_hw = t1_target
                if eod:
                    break
                continue

        if t1_hit and not t2_hit:
            be_hit     = l <= stop
            t2_reached = h >= t2_target
            if be_hit and t2_reached:
                be_hit = False
            if be_hit:
                t2_price = stop; t2_time = t
                t3_price = stop; t3_time = t; t3_reason = "BE_STOP"
                break
            if t2_reached:
                t2_hit   = True
                t2_price = t2_target
                t2_time  = t
                trail_hw = t2_target
                if eod:
                    break
                continue

        if t2_hit:
            trail_stop = trail_hw - TRAIL_R * orb_range
            trail_hit  = l <= trail_stop
            if trail_hit or eod:
                t3_price  = trail_stop if trail_hit else c
                t3_time   = t
                t3_reason = "TRAIL" if trail_hit else "EOD"
                break

        if eod:
            if not t1_hit:
                return _exit_all(entry_price, c, t, orb_range, "EOD")
            if not t2_hit:
                t2_price = c; t2_time = t
                t3_price = c; t3_time = t; t3_reason = "EOD"
            break

    # Build result
    if t1_price is None:
        t1_price = stop; t1_time = post_bars[-1][0] if post_bars else "15:59"
    if t2_price is None:
        t2_price = t1_price; t2_time = t1_time; t3_reason = "NO_T2"
    if t3_price is None:
        t3_price = t2_price; t3_time = t2_time; t3_reason = "NO_T3"

    pnl_t1 = (t1_price - entry_price)
    pnl_t2 = (t2_price - entry_price)
    pnl_t3 = (t3_price - entry_price)
    combined = W1 * pnl_t1 + W2 * pnl_t2 + W3 * pnl_t3

    return {
        "t1_hit": t1_hit, "t1_price": round(t1_price, 4), "t1_time": t1_time,
        "t2_hit": t2_hit, "t2_price": round(t2_price, 4), "t2_time": t2_time,
        "t3_price": round(t3_price, 4), "t3_time": t3_time, "t3_reason": t3_reason,
        "combined_pnl": round(combined, 4),
        "combined_pnl_r": round(combined / orb_range, 4),
        "winner": combined > 0,
    }

def _exit_all(entry_price, exit_price, t, orb_range, reason):
    pnl = exit_price - entry_price
    return {
        "t1_hit": False, "t1_price": round(exit_price, 4), "t1_time": t,
        "t2_hit": False, "t2_price": round(exit_price, 4), "t2_time": t,
        "t3_price": round(exit_price, 4), "t3_time": t, "t3_reason": reason,
        "combined_pnl": round(pnl, 4),
        "combined_pnl_r": round(pnl / orb_range if orb_range > 0 else 0, 4),
        "winner": pnl > 0,
    }


# ---------------------------------------------------------------------------
# Option price lookup
# ---------------------------------------------------------------------------
def get_option_price(opt_lookup, right, strike, hhmm, price_type="close"):
    """Get option price at a given time; walk back if bar missing."""
    strike_data = opt_lookup.get(right, {}).get(strike, {})
    if not strike_data:
        return None
    bar = strike_data.get(hhmm)
    if bar and bar[price_type] > 0:
        return bar[price_type]
    # Walk back to find nearest available bar
    avail = sorted(k for k in strike_data if k <= hhmm and strike_data[k][price_type] > 0)
    if avail:
        return strike_data[avail[-1]][price_type]
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
    gw     = sum(wins)
    gl     = abs(sum(losses))
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
        "n": n, "wr": round(100*len(wins)/n, 1), "pf": round(pf, 2),
        "expect": round(expect, 4), "total": round(total, 2),
        "max_dd": round(max_dd, 2), "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2) if calmar != float("inf") else "∞",
        "ann_ret": round(ann_ret, 2),
    }

def percentile(data, p):
    if not data: return 0.0
    s = sorted(data)
    k = (len(s) - 1) * p / 100.0
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    daily_bars = load_csv_to_daily_bars(CSV_FILE)

    with open(TRADES_FILE) as f:
        all_trades = json.load(f)["trades"]
    trades_2025 = [t for t in all_trades if t["year"] == "2025"]

    with open(REGIME_FILE) as f:
        regime_by_date = {t["date"]: t.get("regime", {}) for t in json.load(f)["trades"]}

    print(f"2025 ORB trades: {len(trades_2025)}")

    # ── Run strategy + collect option P&Ls ───────────────────────────────────
    records = []
    no_options = 0
    missing_entry = 0

    for trade in trades_2025:
        date    = trade["date"]
        regime  = regime_by_date.get(date, {})
        passed, score = passes_filter(trade, regime)
        if not passed:
            continue

        day_bars = daily_bars.get(date)
        if not day_bars:
            continue

        entry_time  = trade["entry_time"]
        entry_price = trade["entry_price"]
        stop_price  = trade["stop_price"]
        orb_range   = trade["orb_range"]

        entry_idx = next((i for i, (t, *_) in enumerate(day_bars) if t == entry_time), None)
        if entry_idx is None:
            continue

        # Underlying tranche simulation
        sim = simulate_tranche("LONG", entry_price, stop_price, entry_idx, day_bars)

        # Load options
        opt_lookup = load_option_day(date)
        if not opt_lookup:
            no_options += 1
            records.append({"date": date, "score": score, "sim": sim,
                            "options": {}, "orb_range": orb_range,
                            "entry_price": entry_price})
            continue

        atm_strike = int(round(entry_price))
        opt_records = {}

        for lbl, offset in STRIKE_OFFSETS.items():
            strike = atm_strike + offset
            # Entry price: OPEN at entry bar
            opt_entry = get_option_price(opt_lookup, "call", strike, entry_time, "open")
            if opt_entry is None:
                missing_entry += 1
                continue

            # Exit each tranche at the underlying exit time
            # T1: use close at t1_time
            opt_t1 = get_option_price(opt_lookup, "call", strike,
                                      sim["t1_time"] or entry_time, "close")
            # T2: use close at t2_time
            opt_t2 = get_option_price(opt_lookup, "call", strike,
                                      sim["t2_time"] or sim["t1_time"] or entry_time, "close")
            # T3: use close at t3_time
            opt_t3 = get_option_price(opt_lookup, "call", strike,
                                      sim["t3_time"] or sim["t2_time"] or entry_time, "close")

            if opt_t1 is None or opt_t2 is None or opt_t3 is None:
                continue

            # P&L per contract ($) — weighted by tranche
            pnl_t1  = (opt_t1 - opt_entry) * 100
            pnl_t2  = (opt_t2 - opt_entry) * 100
            pnl_t3  = (opt_t3 - opt_entry) * 100
            combined = W1 * pnl_t1 + W2 * pnl_t2 + W3 * pnl_t3

            opt_records[lbl] = {
                "strike": strike,
                "opt_entry": opt_entry,
                "opt_t1": opt_t1, "opt_t2": opt_t2, "opt_t3": opt_t3,
                "pnl_t1": round(pnl_t1, 2),
                "pnl_t2": round(pnl_t2, 2),
                "pnl_t3": round(pnl_t3, 2),
                "combined_pnl": round(combined, 2),
                "winner": combined > 0,
            }

        records.append({
            "date": date, "score": score, "orb_range": orb_range,
            "entry_price": entry_price, "entry_time": entry_time,
            "sim": sim, "options": opt_records,
        })

    print(f"Filtered trades: {len(records)}")
    print(f"No options data: {no_options}")
    print(f"Missing entry prices (per strike): {missing_entry}\n")

    # ── Print results ─────────────────────────────────────────────────────────
    SEP = "=" * 85

    # 1. Underlying performance
    ul_pnls = [r["sim"]["combined_pnl"] for r in records]
    ul_stats = compute_stats(ul_pnls)

    print(SEP)
    print("  2025 UNDERLYING TRANCHE STRATEGY")
    print(SEP)
    s = ul_stats
    cal_s = str(s['calmar'])
    print(f"  n={s['n']}  WR={s['wr']}%  PF={s['pf']}  "
          f"Exp=${s['expect']:+.4f}  Total=${s['total']:+.2f}  "
          f"MaxDD=${s['max_dd']:.2f}  Sharpe={s['sharpe']}  Calmar={cal_s}")

    # Phase breakdown
    phases = defaultdict(lambda: {"n": 0, "pnls": []})
    for r in records:
        reason = r["sim"]["t3_reason"]
        phases[reason]["n"] += 1
        phases[reason]["pnls"].append(r["sim"]["combined_pnl"])
    print(f"\n  Phase breakdown:")
    for reason in sorted(phases):
        d = phases[reason]
        s2 = compute_stats(d["pnls"])
        if s2:
            print(f"    {reason:<12} n={d['n']:>3}  WR={s2['wr']:>5.1f}%  Avg=${s2['expect']:>+7.4f}")

    # 2. Options comparison
    print(f"\n{SEP}")
    print("  2025 OPTIONS TRANCHE P&L — Per Contract ($) — Weighted Tranche Exit")
    print(SEP)
    print(f"  {'Strike':<8} {'n':>5} {'WR':>7} {'PF':>6} {'Exp/trade':>10} "
          f"{'Total':>9} {'MaxDD':>8} {'Sharpe':>7} {'Calmar':>7}")
    print(f"  {'-'*8} {'-'*5} {'-'*7} {'-'*6} {'-'*10} "
          f"{'-'*9} {'-'*8} {'-'*7} {'-'*7}")

    opt_stats_all = {}
    for lbl in ["ATM", "OTM", "OTM+1"]:
        pnls = [r["options"][lbl]["combined_pnl"]
                for r in records if lbl in r.get("options", {})]
        if not pnls:
            continue
        s = compute_stats(pnls)
        opt_stats_all[lbl] = s
        pf_s  = f"{s['pf']:.2f}" if s["pf"] != float("inf") else "∞"
        cal_s = str(s["calmar"])
        print(f"  {lbl:<8} {s['n']:>5} {s['wr']:>6.1f}% {pf_s:>6} "
              f"${s['expect']:>+9.2f} ${s['total']:>+8.2f} "
              f"${s['max_dd']:>7.2f} {s['sharpe']:>7.2f} {cal_s:>7}")

    # Underlying for comparison (matched sample)
    ul_pnls_matched = []
    for r in records:
        if "ATM" in r.get("options", {}):
            ul_pnls_matched.append(r["sim"]["combined_pnl"])
    s_ul = compute_stats(ul_pnls_matched)
    pf_s = f"{s_ul['pf']:.2f}" if s_ul and s_ul["pf"] != float("inf") else "∞"
    cal_s = str(s_ul.get("calmar",""))
    if s_ul:
        print(f"  {'Underly.':8} {s_ul['n']:>5} {s_ul['wr']:>6.1f}% {pf_s:>6} "
              f"${s_ul['expect']:>+9.4f} ${s_ul['total']:>+8.2f} "
              f"${s_ul['max_dd']:>7.2f} {s_ul['sharpe']:>7.2f} {cal_s:>7}")

    # 3. Tranche-level option P&L breakdown
    print(f"\n{SEP}")
    print("  OPTION P&L BY TRANCHE EXIT (ATM calls)")
    print(SEP)
    print(f"  {'Tranche':<10} {'n':>5} {'WR':>7} {'Avg P&L':>10} {'Best':>8} {'Worst':>8}")
    print(f"  {'-'*10} {'-'*5} {'-'*7} {'-'*10} {'-'*8} {'-'*8}")
    for tranche_key, tranche_label in [("pnl_t1","T1 (+1R)"),("pnl_t2","T2 (+2R)"),("pnl_t3","T3 (runner)")]:
        pnls = [r["options"]["ATM"][tranche_key]
                for r in records if "ATM" in r.get("options", {})]
        if pnls:
            wins = sum(1 for p in pnls if p > 0)
            print(f"  {tranche_label:<10} {len(pnls):>5} {100*wins/len(pnls):>6.1f}% "
                  f"${sum(pnls)/len(pnls):>+9.2f} ${max(pnls):>+7.2f} ${min(pnls):>+7.2f}")

    # 4. Best individual trades (options ATM)
    print(f"\n{SEP}")
    print("  TOP 10 TRADES — ATM Option Combined P&L")
    print(SEP)
    atm_trades = [(r["date"], r["entry_time"], r["entry_price"],
                   r["options"]["ATM"]["opt_entry"],
                   r["options"]["ATM"]["combined_pnl"],
                   r["sim"]["t3_reason"])
                  for r in records if "ATM" in r.get("options", {})]
    atm_trades.sort(key=lambda x: -x[4])
    print(f"  {'Date':<12} {'Entry':>6} {'UndEntry':>9} {'OptEntry':>9} "
          f"{'OptPnL':>8} {'T3Exit'}")
    for date, et, ue, oe, pnl, reason in atm_trades[:10]:
        print(f"  {date:<12} {et:>6}   ${ue:>7.2f}   ${oe:>6.2f}  ${pnl:>+7.2f}   {reason}")

    print(f"\n{SEP}")
    print("  WORST 10 TRADES — ATM Option Combined P&L")
    print(SEP)
    print(f"  {'Date':<12} {'Entry':>6} {'UndEntry':>9} {'OptEntry':>9} "
          f"{'OptPnL':>8} {'T3Exit'}")
    for date, et, ue, oe, pnl, reason in atm_trades[-10:]:
        print(f"  {date:<12} {et:>6}   ${ue:>7.2f}   ${oe:>6.2f}  ${pnl:>+7.2f}   {reason}")

    # 5. Monthly breakdown (ATM options)
    print(f"\n{SEP}")
    print("  MONTHLY BREAKDOWN — ATM Options")
    print(SEP)
    by_month = defaultdict(list)
    for r in records:
        if "ATM" in r.get("options", {}):
            month = r["date"][:7]
            by_month[month].append(r["options"]["ATM"]["combined_pnl"])
    print(f"  {'Month':<9} {'n':>4} {'WR':>7} {'Total':>9} {'Avg':>8}")
    print(f"  {'-'*9} {'-'*4} {'-'*7} {'-'*9} {'-'*8}")
    for month in sorted(by_month):
        pnls = by_month[month]
        wins = sum(1 for p in pnls if p > 0)
        wr   = 100 * wins / len(pnls)
        print(f"  {month:<9} {len(pnls):>4} {wr:>6.1f}% "
              f"${sum(pnls):>+8.2f} ${sum(pnls)/len(pnls):>+7.2f}")

    # ── Save ──────────────────────────────────────────────────────────────────
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump({
            "strategy": {"align_min": ALIGN_MIN, "orb_max_pct": ORB_MAX_PCT,
                         "t1_r": T1_R, "t2_r": T2_R, "trail_r": TRAIL_R,
                         "weights": [W1, W2, W3]},
            "underlying_stats": ul_stats,
            "options_stats": {k: v for k, v in opt_stats_all.items()},
            "trades": [{
                "date": r["date"], "entry_price": r["entry_price"],
                "entry_time": r.get("entry_time"), "orb_range": r["orb_range"],
                "alignment": r["score"],
                "sim": r["sim"],
                "options": r.get("options", {}),
            } for r in records]
        }, f, indent=2)
    print(f"\n  Results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
