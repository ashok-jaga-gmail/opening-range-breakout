"""
orb_gen_summary.py — Generate 2025 (and 2026) summary txt for the new Golden config.

Config: PT50/125/200 · SL100 · ATM · Up to 4 trades/day · No filters · Unbiased
"""

import csv
import json
import lzma
import os
from collections import defaultdict

_HERE    = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(_HERE, "qqq_1m_2018_2026.csv.xz")
OUT_FILE = os.path.join(_HERE, "tmp", "orb_2025_summary.txt")

OPT_DIRS = {
    "2025": os.path.expanduser("~/backups/QQQ/2025/Options-OHLC/thetadata-2025"),
    "2026": os.path.expanduser("~/backups/QQQ/2026/Options-OHLC/thetadata-2026"),
}

YEARS_ACTIVE = {"2025", "2026"}
MAX_TRADES   = 4

# Golden config
PT1, PT2, PT3, SL = 50, 125, 200, 100
WEIGHTS = (1/3, 1/3, 1/3)
STRIKE_OFFSET = 0   # ATM


# ── Load 1-min bars ──────────────────────────────────────────────────────────
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
    return dict(daily)


# ── Detect all breakout signals ───────────────────────────────────────────────
def find_all_breakouts(day_bars):
    orb_h = orb_l = None
    for t, o, h, l, c, v in day_bars:
        if t < "09:30": continue
        if t > "09:44": break
        orb_h = max(orb_h, h) if orb_h is not None else h
        orb_l = min(orb_l, l) if orb_l is not None else l

    if orb_h is None or (orb_h - orb_l) < 0.10:
        return []

    signals = []
    last_dir = None
    for t, o, h, l, c, v in day_bars:
        if t < "09:45" or t > "15:00": continue
        if len(signals) >= MAX_TRADES: break

        if c > orb_h:
            if last_dir != "LONG":
                signals.append({"signal_time": t, "direction": "LONG", "entry_price": c})
                last_dir = "LONG"
        elif c < orb_l:
            if last_dir != "SHORT":
                signals.append({"signal_time": t, "direction": "SHORT", "entry_price": c})
                last_dir = "SHORT"
        else:
            last_dir = None
    return signals


def next_minute(hhmm):
    h, m = int(hhmm[:2]), int(hhmm[3:5])
    m += 1
    if m >= 60: h += 1; m = 0
    return f"{h:02d}:{m:02d}"


# ── Load options bars ─────────────────────────────────────────────────────────
def load_option_bars(date_str, year, right="call"):
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
    right_vals = {"call","c"} if right=="call" else {"put","p"}
    for _, row in df.iterrows():
        r = str(row.get("right","")).strip().lower()
        if r not in right_vals: continue
        strike = int(row.get("strike",0))
        ts = str(row.get("timestamp",""))
        hhmm = ts[11:16] if len(ts)>=16 else ""
        if not hhmm: continue
        result.setdefault(strike,{})[hhmm] = {
            "open":  float(row.get("open") or 0),
            "high":  float(row.get("high") or 0),
            "low":   float(row.get("low")  or 0),
            "close": float(row.get("close")or 0),
        }
    return result


# ── Simulate one trade ────────────────────────────────────────────────────────
def simulate_trade(strike_bars, signal_time):
    entry_time = next_minute(signal_time)
    times = sorted(strike_bars.keys())

    entry_opt = None
    start_idx = None
    for i, t in enumerate(times):
        if t >= entry_time and strike_bars[t]["open"] > 0:
            entry_opt = strike_bars[t]["open"]
            start_idx = i
            break

    if entry_opt is None or entry_opt <= 0:
        return None

    pt1_price = entry_opt * (1 + PT1 / 100)
    pt2_price = entry_opt * (1 + PT2 / 100)
    pt3_price = entry_opt * (1 + PT3 / 100)
    sl_price  = entry_opt * (1 - SL  / 100)

    W1, W2, W3 = WEIGHTS
    stop = sl_price
    max_opt = entry_opt
    phase = "pre_t1"
    t1_exit = t2_exit = t3_exit = None

    for t in times[start_idx:]:
        bar = strike_bars[t]
        if bar["close"] <= 0: continue
        eod = t >= "15:55"
        h, l, c = bar["high"], bar["low"], bar["close"]
        max_opt = max(max_opt, h)

        if phase == "pre_t1":
            sl_hit  = l <= stop
            pt1_hit = h >= pt1_price
            if sl_hit and pt1_hit: sl_hit, pt1_hit = True, False   # stop wins

            if sl_hit:
                pnl = (stop - entry_opt) * 100
                return W1*pnl + W2*pnl + W3*pnl

            if pt1_hit:
                t1_exit = pt1_price
                stop = entry_opt
                phase = "post_t1"
                if eod: t2_exit = t3_exit = c; break
                continue

            if eod:
                pnl = (c - entry_opt) * 100
                return W1*pnl + W2*pnl + W3*pnl

        elif phase == "post_t1":
            be_hit  = l <= stop
            pt2_hit = h >= pt2_price
            if be_hit and pt2_hit: be_hit, pt2_hit = True, False

            if be_hit: t2_exit = t3_exit = entry_opt; break

            if pt2_hit:
                t2_exit = pt2_price
                stop = max_opt * (1 - SL / 100)
                phase = "post_t2"
                if eod: t3_exit = c; break
                continue

            if eod: t2_exit = t3_exit = max(c, entry_opt); break

        elif phase == "post_t2":
            stop = max_opt * (1 - SL / 100)
            trail_hit = l <= stop
            pt3_hit   = h >= pt3_price
            if trail_hit and pt3_hit: trail_hit, pt3_hit = True, False

            if trail_hit: t3_exit = stop; break
            if pt3_hit:   t3_exit = pt3_price; break
            if eod:       t3_exit = c; break

    if t1_exit is None:
        return None
    if t2_exit is None: t2_exit = entry_opt
    if t3_exit is None: t3_exit = entry_opt

    return (W1*(t1_exit-entry_opt) + W2*(t2_exit-entry_opt) + W3*(t3_exit-entry_opt)) * 100


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    daily_bars = load_daily_bars(CSV_FILE)

    # Collect per-day, per-trade P&Ls for 2025+2026
    # day_results: {date: [pnl_trade1, pnl_trade2, ...]}
    day_results = {}

    sorted_dates = sorted(d for d in daily_bars if d[:4] in YEARS_ACTIVE)
    print(f"Simulating {len(sorted_dates)} days …", flush=True)

    for date in sorted_dates:
        year = date[:4]
        bars = daily_bars[date]
        signals = find_all_breakouts(bars)
        if not signals:
            continue

        calls = load_option_bars(date, year, "call")
        puts  = load_option_bars(date, year, "put")

        if not calls and not puts:
            continue

        day_pnls = []
        for sig in signals:
            atm = int(round(sig["entry_price"]))
            if sig["direction"] == "LONG":
                s = atm + STRIKE_OFFSET
                opt_bars = calls.get(s)
            else:
                s = atm - STRIKE_OFFSET
                opt_bars = puts.get(s)

            if opt_bars is None:
                day_pnls.append(None)
                continue

            pnl = simulate_trade(opt_bars, sig["signal_time"])
            day_pnls.append(pnl)

        if any(p is not None for p in day_pnls):
            day_results[date] = day_pnls

    # ── Build output ──────────────────────────────────────────────────────────
    lines = []

    def h(s): lines.append(s)

    h("QQQ 15-min ORB Strategy — 2025–2026 Full Summary (Unbiased)")
    h("=" * 70)
    h(f"Strategy : PT{PT1}/{PT2}/{PT3} · SL{SL} · ATM · Up to {MAX_TRADES} trades/day · No filters")
    h( "Entry    : Open of bar AFTER signal bar (no look-ahead bias)")
    h( "Same-bar : Stop wins over PT target when both hit in same bar (conservative)")
    W1, W2, W3 = WEIGHTS
    h(f"Per contract (×100 multiplier). T1={int(W1*100)}% size, T2={int(W2*100)}%, T3={int(W3*100)}%.")

    # All trades overall
    all_pnls = [p for pnls in day_results.values() for p in pnls if p is not None]
    total_overall = sum(all_pnls)
    wr_overall = 100 * sum(1 for p in all_pnls if p > 0) / len(all_pnls) if all_pnls else 0
    win_days   = sum(1 for pnls in day_results.values() if sum(p for p in pnls if p is not None) > 0)
    total_days = len(day_results)

    by_year = defaultdict(list)
    for date, pnls in day_results.items():
        by_year[date[:4]].extend(p for p in pnls if p is not None)

    h("")
    h(f"OVERALL  {len(all_pnls)} trades · WR={wr_overall:.1f}% · Days={win_days}/{total_days} · Total=${total_overall:+,.2f}")

    # Month-by-month
    by_month = defaultdict(list)
    for date, pnls in day_results.items():
        by_month[date[:7]].extend(p for p in pnls if p is not None)

    months_2025 = sorted(m for m in by_month if m.startswith("2025"))
    months_2026 = sorted(m for m in by_month if m.startswith("2026"))

    def month_section(dates_in_year, year_label):
        year_pnls = []
        year_win_days = 0
        year_total_days = 0

        month_names = {
            "01":"January","02":"February","03":"March","04":"April",
            "05":"May","06":"June","07":"July","08":"August",
            "09":"September","10":"October","11":"November","12":"December"
        }

        sorted_month_dates = sorted(d for d in dates_in_year)
        months_in_year = sorted(set(d[:7] for d in sorted_month_dates))

        for month in months_in_year:
            month_num = month[5:7]
            month_dates = sorted(d for d in sorted_month_dates if d[:7] == month)
            month_pnls = []
            for d in month_dates:
                if d in day_results:
                    month_pnls.extend(p for p in day_results[d] if p is not None)

            month_total = sum(month_pnls)
            month_wr = 100 * sum(1 for p in month_pnls if p > 0) / len(month_pnls) if month_pnls else 0

            win_d = sum(1 for d in month_dates if d in day_results and
                        sum(p for p in day_results[d] if p is not None) > 0)
            tot_d = sum(1 for d in month_dates if d in day_results)

            sign = "+" if month_total >= 0 else ""
            h("")
            h(f"{month_names[month_num]} {year_label} daily breakdown — {win_d}W / {tot_d - win_d}L ({int(100*win_d/tot_d) if tot_d else 0}% win days), ${sign}{month_total:,.2f}:")
            h("")

            # Column header — up to MAX_TRADES trades
            max_t = max((len(day_results.get(d, [])) for d in month_dates if d in day_results), default=1)
            t_cols = "".join(f"  {'T'+str(i):>8}" for i in range(1, max_t+1))
            h(f"Date        {t_cols}    Day Total")
            h("-" * 70)

            for d in month_dates:
                if d not in day_results:
                    continue
                day_ps = day_results[d]
                valid = [p for p in day_ps if p is not None]
                day_total = sum(valid)

                date_lbl = d[5:]  # MM-DD
                cols = ""
                for i in range(max_t):
                    if i < len(day_ps) and day_ps[i] is not None:
                        cols += f"  ${day_ps[i]:>+7.0f}"
                    else:
                        cols += f"  {'—':>8}"

                tag = "WIN" if day_total > 0 else "LOSS"
                h(f"{date_lbl}     {cols}    ${day_total:>+8.0f}  {tag}")

            h("-" * 70)
            sign = "+" if month_total >= 0 else ""
            h(f"Month Total                                      ${sign}{month_total:,.2f}")

            year_pnls.extend(month_pnls)
            year_win_days  += win_d
            year_total_days += tot_d

        return year_pnls, year_win_days, year_total_days

    all_dates_2025 = sorted(d for d in day_results if d.startswith("2025"))
    all_dates_2026 = sorted(d for d in day_results if d.startswith("2026"))

    h("")
    y25_pnls, y25_wd, y25_td = month_section(all_dates_2025, "2025")

    h("")
    h("=" * 70)
    h("2025 FULL YEAR SUMMARY")
    h("=" * 70)
    y25_total = sum(y25_pnls)
    y25_wr = 100 * sum(1 for p in y25_pnls if p > 0) / len(y25_pnls) if y25_pnls else 0
    h(f"  Trading days  : {y25_td}")
    h(f"  Win days      : {y25_wd} / {y25_td} ({int(100*y25_wd/y25_td) if y25_td else 0}%)")
    h(f"  Loss days     : {y25_td - y25_wd}")
    h(f"  Total trades  : {len(y25_pnls)}")
    h(f"  Trade win rate: {sum(1 for p in y25_pnls if p>0)}/{len(y25_pnls)} ({y25_wr:.1f}%)")
    h(f"  Total P&L     : ${y25_total:+,.2f} per contract")
    h(f"  Avg / day     : ${y25_total/y25_td:+.2f}" if y25_td else "")
    best_m = max(months_2025, key=lambda m: sum(by_month[m]))
    worst_m = min(months_2025, key=lambda m: sum(by_month[m]))
    month_names2 = {"01":"January","02":"February","03":"March","04":"April",
                    "05":"May","06":"June","07":"July","08":"August",
                    "09":"September","10":"October","11":"November","12":"December"}
    h(f"  Best month    : {month_names2[best_m[5:]]}")
    h(f"  Worst month   : {month_names2[worst_m[5:]]}")
    h("")
    h(f"  Note: Forward bias removed. Entry at next-bar open.")
    h(f"  New Golden config: PT{PT1}/{PT2}/{PT3} SL{SL} ATM equal-weight, up to {MAX_TRADES} trades/day.")
    h(f"  Old biased result: WR=79.5% / +$13,835 (phantom, entry at signal bar open).")
    h(f"  Old unbiased result (3 trades): WR=63% / +$6,205.")

    if all_dates_2026:
        h("")
        y26_pnls, y26_wd, y26_td = month_section(all_dates_2026, "2026")
        h("")
        h("=" * 70)
        h("2026 YTD SUMMARY (Jan–Mar)")
        h("=" * 70)
        y26_total = sum(y26_pnls)
        y26_wr = 100 * sum(1 for p in y26_pnls if p > 0) / len(y26_pnls) if y26_pnls else 0
        h(f"  Trading days  : {y26_td}")
        h(f"  Win days      : {y26_wd} / {y26_td} ({int(100*y26_wd/y26_td) if y26_td else 0}%)")
        h(f"  Total trades  : {len(y26_pnls)}")
        h(f"  Trade win rate: {sum(1 for p in y26_pnls if p>0)}/{len(y26_pnls)} ({y26_wr:.1f}%)")
        h(f"  Total P&L     : ${y26_total:+,.2f} per contract")

        h("")
        h("=" * 70)
        h("2025+2026 COMBINED SUMMARY")
        h("=" * 70)
        combined = y25_pnls + y26_pnls
        comb_total = sum(combined)
        comb_wr = 100 * sum(1 for p in combined if p > 0) / len(combined) if combined else 0
        import math
        eq = pk = mdd = 0.0
        for p in combined:
            eq += p
            if eq > pk: pk = eq
            dd = pk - eq
            if dd > mdd: mdd = dd
        h(f"  Total trades  : {len(combined)}")
        h(f"  Trade WR      : {comb_wr:.1f}%")
        h(f"  Total P&L     : ${comb_total:+,.2f} per contract")
        h(f"  Max Drawdown  : ${mdd:,.2f}")
        years = len(combined) / 252
        ann = comb_total / years if years else 0
        calmar = ann / mdd if mdd else float("inf")
        h(f"  Calmar ratio  : {calmar:.2f}")

    output = "\n".join(lines)
    with open(OUT_FILE, "w") as f:
        f.write(output + "\n")
    print(f"\nWritten → {OUT_FILE}")
    print(output[:3000])  # preview


if __name__ == "__main__":
    main()
