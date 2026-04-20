"""
orb_options_grid.py — Grid search on option price % targets and stops

Rather than using underlying R-multiples, exit based on the option's
own price movement from entry:

  Profit Targets (PT): option price × (1 + PT%)   e.g. buy $1.00 → sell at $1.50 for PT50
  Stop Loss      (SL): option price × (1 − SL%)   e.g. buy $1.00 → exit at $0.50 for SL50

3-tranche structure:
  T1: PT1%  (25%)  → stop moves to BREAKEVEN (entry option price)
  T2: PT2%  (25%)  → stop trails at max_opt_price × (1 − SL%)
  T3: PT3%  (50%)  → same trail, or EOD

Grid dimensions:
  pt1_pct : [25, 50, 75, 100]
  pt2_pct : [100, 150, 200]
  pt3_pct : [200, 300]          (runner target — or EOD if not reached)
  sl_pct  : [30, 50, 75]
  weights : equal (1/3) or runner-heavy (25/25/50)
  strikes : ATM (+0), OTM (+1), OTM+1 (+2)

Exit logic (bar by bar):
  - PT hit:   option HIGH >= entry × (1 + PT/100)  → fill at PT price
  - SL hit:   option LOW  <= current_stop          → fill at SL price
  - EOD:      exit remaining at bar close at 15:59

Data: 2025 + 2026 filtered trades (LONG, align≥70%, CPR above_top, ORB%≤0.64%)
All P&L in $ per contract (option price × 100).
"""

import json
import math
import os
from collections import defaultdict
from itertools import product

_HERE       = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = "/tmp/orb_paper_results.json"
REGIME_FILE = "/tmp/orb_regime_results.json"
OUT_FILE    = os.path.join(_HERE, "tmp", "options_grid_results.json")

OPT_DIRS = {
    "2025": os.path.expanduser("~/backups/QQQ/2025/Options-OHLC/thetadata-2025"),
    "2026": os.path.expanduser("~/backups/QQQ/2026/Options-OHLC/thetadata-2026"),
}

ALIGN_MIN   = 0.70
ORB_MAX_PCT = 0.64

# Grid
PT1_OPTIONS = [25, 50, 75, 100]
PT2_OPTIONS = [100, 150, 200]
PT3_OPTIONS = [200, 300]
SL_OPTIONS  = [30, 50, 75]
WEIGHT_OPTIONS = [
    (1/3, 1/3, 1/3),
    (0.25, 0.25, 0.50),
]
STRIKE_OFFSETS = {"ATM": 0, "OTM": 1, "OTM+1": 2}


# ---------------------------------------------------------------------------
# Regime filter
# ---------------------------------------------------------------------------
BULL_CPR  = {"above_top"};  BULL_RSI  = {"bullish","overbought"}
BULL_MACD = {"bullish_cross","bullish","bullish_fade"}
BEAR_CPR  = {"below_bottom"}; BEAR_RSI = {"bearish","oversold"}
BEAR_MACD = {"bearish_cross","bearish","bearish_fade"}

IND_KEYS = [
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
    al = av = 0
    for key, bull_set, bear_set in IND_KEYS:
        val = regime.get(key)
        if val in (None, "unknown"): continue
        av += 1
        if direction == "LONG" and val in bull_set: al += 1
        elif direction == "SHORT" and val in bear_set: al += 1
    return al / av if av else None

def passes_filter(trade, regime):
    if trade["direction"] != "LONG": return False
    if trade["orb_range"] / trade["entry_price"] * 100 > ORB_MAX_PCT: return False
    if regime.get("cpr_daily_state") not in (None, "unknown", "above_top"): return False
    score = compute_alignment(regime, "LONG")
    return score is not None and score >= ALIGN_MIN


# ---------------------------------------------------------------------------
# Options data loading
# ---------------------------------------------------------------------------
def load_option_bars(date_str, year):
    """Return {strike: {hhmm: {open,high,low,close}}} for calls."""
    opt_dir = OPT_DIRS.get(year)
    if not opt_dir: return {}
    path = os.path.join(opt_dir, f"qqq-options-1m-{date_str.replace('-','')}.parquet")
    if not os.path.exists(path): return {}
    try:
        import pandas as pd
        df = pd.read_parquet(path)
    except Exception:
        return {}

    result = {}
    for _, row in df.iterrows():
        r = str(row.get("right","")).strip().lower()
        if r not in ("call","c"): continue
        strike = int(row.get("strike", 0))
        ts     = str(row.get("timestamp",""))
        hhmm   = ts[11:16] if len(ts) >= 16 else ""
        if not hhmm: continue
        result.setdefault(strike, {})[hhmm] = {
            "open":  float(row.get("open")  or 0),
            "high":  float(row.get("high")  or 0),
            "low":   float(row.get("low")   or 0),
            "close": float(row.get("close") or 0),
        }
    return result


def get_bar(strike_bars, hhmm, fallback_dir="back"):
    """Get bar at hhmm, or nearest available."""
    bar = strike_bars.get(hhmm)
    if bar and bar["close"] > 0:
        return bar
    times = sorted(strike_bars.keys())
    if fallback_dir == "back":
        avail = [t for t in times if t <= hhmm and strike_bars[t]["close"] > 0]
        return strike_bars[avail[-1]] if avail else None
    else:
        avail = [t for t in times if t >= hhmm and strike_bars[t]["close"] > 0]
        return strike_bars[avail[0]] if avail else None


# ---------------------------------------------------------------------------
# Simulate one trade for one (pt1, pt2, pt3, sl, weights) config
# ---------------------------------------------------------------------------
def simulate_option_trade(strike_bars, entry_time, pt1, pt2, pt3, sl, weights):
    """
    Walk option bars from entry forward.
    Returns combined P&L per contract ($) or None if no entry price.
    """
    # Entry price: open of entry bar
    entry_bar = get_bar(strike_bars, entry_time, "forward")
    if entry_bar is None or entry_bar["open"] <= 0:
        return None
    entry_opt = entry_bar["open"]

    pt1_price = entry_opt * (1 + pt1 / 100)
    pt2_price = entry_opt * (1 + pt2 / 100)
    pt3_price = entry_opt * (1 + pt3 / 100)
    sl_price  = entry_opt * (1 - sl  / 100)

    W1, W2, W3 = weights

    t1_hit = t2_hit = False
    t1_exit = t2_exit = t3_exit = None
    stop = sl_price           # absolute option price stop
    max_opt = entry_opt       # high-water for trailing stop post-T2
    phase = "pre_t1"

    # Walk all bars from entry_time onward
    all_times = sorted(strike_bars.keys())
    start_idx = next((i for i, t in enumerate(all_times) if t >= entry_time), None)
    if start_idx is None:
        return None

    for t in all_times[start_idx:]:
        bar = strike_bars[t]
        if bar["close"] <= 0:
            continue
        eod = t >= "15:59"

        h = bar["high"]
        l = bar["low"]
        c = bar["close"]

        max_opt = max(max_opt, h)

        if phase == "pre_t1":
            # SL: option LOW touches stop
            sl_hit = l <= stop
            # PT1: option HIGH touches pt1_price
            t1_hit_now = h >= pt1_price

            if sl_hit and t1_hit_now:
                sl_hit = False   # PT1 takes priority same bar

            if sl_hit:
                # All 3 tranches exit at stop
                pnl = (stop - entry_opt) * 100
                return W1 * pnl + W2 * pnl + W3 * pnl

            if t1_hit_now:
                t1_exit = pt1_price
                t1_hit  = True
                stop    = entry_opt   # move stop to BE (entry option price)
                phase   = "post_t1"
                if eod:
                    t2_exit = t3_exit = c
                    break
                continue

            if eod:
                pnl_all = (c - entry_opt) * 100
                return W1 * pnl_all + W2 * pnl_all + W3 * pnl_all

        elif phase == "post_t1":
            be_hit     = l <= stop          # stopped at breakeven
            t2_hit_now = h >= pt2_price

            if be_hit and t2_hit_now:
                be_hit = False

            if be_hit:
                t2_exit = t3_exit = entry_opt   # exit at BE
                break

            if t2_hit_now:
                t2_exit = pt2_price
                t2_hit  = True
                # Trailing stop: max_opt × (1 − sl%)
                stop  = max_opt * (1 - sl / 100)
                phase = "post_t2"
                if eod:
                    t3_exit = c
                    break
                continue

            if eod:
                t2_exit = t3_exit = max(c, entry_opt)  # min BE since stop at entry
                break

        elif phase == "post_t2":
            # Update trailing stop
            stop = max_opt * (1 - sl / 100)

            trail_hit  = l <= stop
            t3_hit_now = h >= pt3_price

            if trail_hit and t3_hit_now:
                trail_hit = False   # PT3 priority

            if trail_hit:
                t3_exit = stop
                break

            if t3_hit_now:
                t3_exit = pt3_price
                break

            if eod:
                t3_exit = c
                break

    # Build combined P&L
    if not t1_hit:
        # Should have been caught above; fallback
        return None
    if t2_exit is None:
        t2_exit = entry_opt
    if t3_exit is None:
        t3_exit = entry_opt

    pnl1 = (t1_exit - entry_opt) * 100
    pnl2 = (t2_exit - entry_opt) * 100
    pnl3 = (t3_exit - entry_opt) * 100

    return W1 * pnl1 + W2 * pnl2 + W3 * pnl3


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
    std    = math.sqrt(sum((p-expect)**2 for p in pnls)/n) if n > 1 else 0
    sharpe = (expect / std * math.sqrt(252)) if std > 0 else 0
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
        "n": n, "wr": round(100*len(wins)/n, 1),
        "pf": round(pf, 2) if pf != float("inf") else "∞",
        "expect": round(expect, 2), "total": round(total, 2),
        "max_dd": round(mdd, 2), "sharpe": round(sharpe, 2),
        "calmar": round(calmar, 2) if calmar != float("inf") else "∞",
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Load trades and regime
    with open(TRADES_FILE) as f:
        all_trades = json.load(f)["trades"]
    with open(REGIME_FILE) as f:
        regime_by_date = {t["date"]: t.get("regime", {})
                          for t in json.load(f)["trades"]}

    # Filter and load option bars for 2025+2026
    print("Loading option bars for filtered trades …", flush=True)
    trade_data = []   # list of {date, entry_time, entry_price, strike_data}

    for trade in all_trades:
        year = trade["year"]
        if year not in OPT_DIRS: continue
        date   = trade["date"]
        regime = regime_by_date.get(date, {})
        if not passes_filter(trade, regime): continue

        entry_price = trade["entry_price"]
        atm_strike  = int(round(entry_price))

        opt_bars = load_option_bars(date, year)
        if not opt_bars: continue

        # Collect bars for ATM, OTM, OTM+1
        strike_data = {}
        for lbl, offset in STRIKE_OFFSETS.items():
            s = atm_strike + offset
            if s in opt_bars:
                strike_data[lbl] = opt_bars[s]

        if not strike_data: continue

        trade_data.append({
            "date":        date,
            "year":        year,
            "entry_time":  trade["entry_time"],
            "entry_price": entry_price,
            "strikes":     strike_data,
        })

    print(f"  {len(trade_data)} trades with option data\n")

    # Build grid
    grid_results = []
    configs = []
    for pt1, pt2, pt3, sl, wt, lbl in product(
        PT1_OPTIONS, PT2_OPTIONS, PT3_OPTIONS, SL_OPTIONS, WEIGHT_OPTIONS,
        list(STRIKE_OFFSETS.keys())
    ):
        if pt2 <= pt1: continue     # T2 must be beyond T1
        if pt3 <= pt2: continue     # T3 must be beyond T2
        configs.append((pt1, pt2, pt3, sl, wt, lbl))

    print(f"Running {len(configs)} configs × {len(trade_data)} trades …", flush=True)

    for i, (pt1, pt2, pt3, sl, wt, strike_lbl) in enumerate(configs):
        if i % 50 == 0:
            print(f"  {i}/{len(configs)} …", flush=True)

        pnls = []
        for td in trade_data:
            bars = td["strikes"].get(strike_lbl)
            if bars is None: continue
            pnl = simulate_option_trade(
                bars, td["entry_time"], pt1, pt2, pt3, sl, wt
            )
            if pnl is not None:
                pnls.append(pnl)

        if len(pnls) < 20: continue

        s = compute_stats(pnls)
        if not s: continue

        wt_lbl = "runner50" if abs(wt[2] - 0.5) < 0.01 else "equal"
        label  = f"PT{pt1}/{pt2}/{pt3}_SL{sl}_{strike_lbl}_{wt_lbl}"
        grid_results.append({"label": label, "pt1": pt1, "pt2": pt2, "pt3": pt3,
                              "sl": sl, "strike": strike_lbl, "weights": wt_lbl,
                              "stats": s})

    # Sort by Calmar
    def sort_key(r):
        cal = r["stats"]["calmar"]
        tot = r["stats"]["total"]
        if cal == "∞": cal = 9999
        return (-cal, -tot)

    grid_results.sort(key=sort_key)

    SEP = "=" * 115
    def print_row(rank, r):
        s = r["stats"]
        pf_s  = str(s["pf"])
        cal_s = str(s["calmar"])
        print(f"  {rank:>3}  {s['n']:>4}  {s['wr']:>5.1f}%  {pf_s:>5}  "
              f"${s['expect']:>+7.2f}  ${s['total']:>+8.2f}  "
              f"${s['max_dd']:>7.2f}  {s['sharpe']:>5.2f}  {cal_s:>6}  {r['label']}")

    hdr = f"  {'#':>3}  {'n':>4}  {'WR':>6}  {'PF':>5}  {'Exp':>8}  {'Total':>9}  {'MaxDD':>8}  {'Sharpe':>6}  {'Calmar':>6}  Config"
    div = f"  {'-'*3}  {'-'*4}  {'-'*6}  {'-'*5}  {'-'*8}  {'-'*9}  {'-'*8}  {'-'*6}  {'-'*6}  {'-'*50}"

    print(f"\n{SEP}")
    print("  TOP 20 BY CALMAR — Option % Targets (per contract $)")
    print(SEP)
    print(hdr); print(div)
    for rank, r in enumerate(grid_results[:20], 1):
        print_row(rank, r)

    # Top by total P&L
    by_pnl = sorted(grid_results, key=lambda r: -(r["stats"]["total"]
                    if isinstance(r["stats"]["total"], (int,float)) else 0))
    print(f"\n{SEP}")
    print("  TOP 10 BY TOTAL P&L")
    print(SEP)
    print(hdr); print(div)
    for rank, r in enumerate(by_pnl[:10], 1):
        print_row(rank, r)

    # Top by WR
    by_wr = sorted(grid_results, key=lambda r: -r["stats"]["wr"])
    print(f"\n{SEP}")
    print("  TOP 10 BY WIN RATE")
    print(SEP)
    print(hdr); print(div)
    for rank, r in enumerate(by_wr[:10], 1):
        print_row(rank, r)

    # Best config deep-dive: per-trade breakdown
    best = grid_results[0]
    print(f"\n{SEP}")
    print(f"  BEST CONFIG BREAKDOWN: {best['label']}")
    print(SEP)
    pnls_best = []
    for td in trade_data:
        bars = td["strikes"].get(best["strike"])
        if bars is None: continue
        pnl = simulate_option_trade(
            bars, td["entry_time"],
            best["pt1"], best["pt2"], best["pt3"], best["sl"],
            (1/3,1/3,1/3) if best["weights"]=="equal" else (0.25,0.25,0.50)
        )
        if pnl is not None:
            pnls_best.append((td["date"], pnl))

    pnls_best.sort(key=lambda x: -x[1])
    print(f"  Top 10 trades:")
    for date, pnl in pnls_best[:10]:
        print(f"    {date}  ${pnl:>+8.2f}")
    print(f"  Worst 10 trades:")
    for date, pnl in pnls_best[-10:]:
        print(f"    {date}  ${pnl:>+8.2f}")

    # Monthly breakdown of best config
    print(f"\n  Monthly breakdown:")
    by_month = defaultdict(list)
    for date, pnl in pnls_best:
        by_month[date[:7]].append(pnl)
    for month in sorted(by_month):
        ps = by_month[month]
        wr = 100 * sum(1 for p in ps if p > 0) / len(ps)
        print(f"    {month}  n={len(ps):>2}  WR={wr:>5.1f}%  "
              f"Total=${sum(ps):>+8.2f}  Avg=${sum(ps)/len(ps):>+7.2f}")

    # Save
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w") as f:
        json.dump({
            "top_by_calmar": grid_results[:30],
            "top_by_pnl":    by_pnl[:10],
            "top_by_wr":     by_wr[:10],
        }, f, indent=2)
    print(f"\n  Results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
