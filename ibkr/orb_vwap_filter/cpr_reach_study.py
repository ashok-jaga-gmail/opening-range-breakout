"""
cpr_reach_study.py — Test the hypothesis:

  "When the ORB+VWAP breakout fires, it reaches at least the NEXT CPR level
   (underlying) — which is ~100% on an OTM option vs less on an ATM option."

This is a FAVORABLE-EXCURSION / reachability study on the FIRST valid breakout
of each day (one observation per day). It is descriptive (it uses intrabar
highs/lows to characterise the move), so it is NOT a tradeable P&L — but it
also reports the decision-relevant "target reached BEFORE the -50%/-75% stop",
because reachability alone (ignoring the stop) is optimistic.

For each first breakout it records, over [entry .. 13:00]:
  - underlying max favourable move; whether it reaches the next 1/2/3 CPR levels
  - per strike moneyness (ATM, OTM1, OTM2, OTM3): option premium MFE %, whether
    it reaches +50/100/150/200%, and whether +100% (or the CPR level) is hit
    BEFORE the stop
  - the option premium gain at the moment the underlying first touches the next
    CPR level (ATM vs OTM) -> tests the "CPR≈100% for OTM" half of the claim

Usage:  python3 cpr_reach_study.py
"""

import math
import os
from grid_search import (load_all_underlying, daily_ohlc_and_cpr, load_option_day,
                          OR_START, OR_END_EXCL, ENTRY_START, TRADING_END, MIN_OR_RANGE)

STRIKE_OFFSETS = [("ATM", 0), ("OTM1", 1), ("OTM2", 2), ("OTM3", 3)]
PCT_TARGETS = [50, 100, 150, 200]


def first_breakout(bars):
    """Return (entry_idx, direction, entry_under, rhigh, rlow) or None."""
    or_bars = [b for b in bars if OR_START <= b[0] < OR_END_EXCL]
    if len(or_bars) < 10:
        return None
    rhigh = max(b[2] for b in or_bars)
    rlow = min(b[3] for b in or_bars)
    if rhigh - rlow < MIN_OR_RANGE:
        return None
    cvp = cv = 0.0
    for i, (t, o, h, l, c, v) in enumerate(bars):
        cvp += (h + l + c) / 3.0 * v
        cv += v
        if t < ENTRY_START or t >= TRADING_END:
            continue
        vw = cvp / cv if cv > 0 else None
        if vw is None:
            continue
        if c > rhigh and c > vw:
            return (i, "call", c, rhigh, rlow)
        if c < rlow and c < vw:
            return (i, "put", c, rhigh, rlow)
    return None


def next_cpr_levels(cpr, direction, entry):
    """Sorted CPR levels beyond entry in the trade direction (nearest first)."""
    vals = [cpr[k] for k in ("bc", "pivot", "tc", "r1", "r2", "r3", "s1", "s2", "s3")]
    if direction == "call":
        return sorted(v for v in vals if v > entry)
    return sorted((v for v in vals if v < entry), reverse=True)


def study(year, days, cpr_by_date):
    rows = []
    n_days = n_breakout = 0
    for d in sorted(days):
        if not d.startswith(year):
            continue
        bars = days[d]
        if len(bars) < 16 or d not in cpr_by_date:
            continue
        n_days += 1
        fb = first_breakout(bars)
        if fb is None:
            continue
        ei, direction, entry_u, rhigh, rlow = fb
        if ei + 1 >= len(bars):
            continue
        n_breakout += 1
        cpr = cpr_by_date[d]
        levels = next_cpr_levels(cpr, direction, entry_u)

        atm = round(entry_u)
        want = set()
        for _, k in STRIKE_OFFSETS:
            want.add(atm + k if direction == "call" else atm - k)
        opt = load_option_day(year, d, want)
        if opt is None:
            continue
        side = opt["call"] if direction == "call" else opt["put"]

        times = [b[0] for b in bars]
        win = range(ei + 1, len(bars))   # entry fills next bar; window to EOD

        # underlying favourable excursion & CPR reach
        if direction == "call":
            umfe = max(bars[j][2] for j in win)        # max high
            reach = [umfe >= lv for lv in levels[:3]]
            # first bar index touching level1
            t1_idx = next((j for j in win if bars[j][2] >= levels[0]), None) if levels else None
        else:
            umfe = min(bars[j][3] for j in win)        # min low
            reach = [umfe <= lv for lv in levels[:3]]
            t1_idx = next((j for j in win if bars[j][3] <= levels[0]), None) if levels else None
        reach += [False] * (3 - len(reach))

        row = {"date": d, "dir": direction, "entry_u": entry_u,
               "lvl1_dist": (abs(levels[0] - entry_u) if levels else None),
               "or_range": rhigh - rlow,
               "reach1": reach[0], "reach2": reach[1], "reach3": reach[2],
               "strikes": {}}

        for name, k in STRIKE_OFFSETS:
            strike = atm + k if direction == "call" else atm - k
            arr = side.get(strike)
            ent_bar = arr.get(times[ei + 1]) if arr else None
            if not ent_bar or ent_bar[0] <= 0:
                row["strikes"][name] = None
                continue
            ent = ent_bar[0]
            # premium MFE over window
            highs = [arr[times[j]][1] for j in win if times[j] in arr]
            if not highs:
                row["strikes"][name] = None
                continue
            mfe = (max(highs) - ent) / ent * 100.0
            # target-before-stop (path), stops at -50% and -75%
            def target_before_stop(target_pct, stop_pct, cpr_idx=None):
                tgt = ent * (1 + target_pct / 100.0) if target_pct is not None else None
                stp = ent * (1 - stop_pct / 100.0)
                for j in win:
                    ob = arr.get(times[j])
                    if ob is None:
                        continue
                    oo, oh, ol, ocl = ob
                    hit_stop = ol <= stp
                    if cpr_idx is not None and levels:
                        und = bars[j][2] if direction == "call" else bars[j][3]
                        hit_tgt = (und >= levels[0]) if direction == "call" else (und <= levels[0])
                    else:
                        hit_tgt = (oh >= tgt) if tgt else False
                    if hit_stop and hit_tgt:
                        return "stop"      # ambiguous bar -> conservative
                    if hit_tgt:
                        return "target"
                    if hit_stop:
                        return "stop"
                return "eod"
            # premium at the moment underlying first touches CPR level 1
            prem_at_cpr1 = None
            if t1_idx is not None and times[t1_idx] in arr:
                ob = arr[times[t1_idx]]
                prem_at_cpr1 = (ob[1] - ent) / ent * 100.0   # option high gain% at that bar
            row["strikes"][name] = {
                "ent": round(ent, 2), "mfe": round(mfe, 1),
                "hit": {p: (mfe >= p) for p in PCT_TARGETS},
                "t100_vs_stop50": target_before_stop(100, 50),
                "t100_vs_stop75": target_before_stop(100, 75),
                "cpr1_vs_stop50": target_before_stop(None, 50, cpr_idx=0),
                "cpr1_vs_stop75": target_before_stop(None, 75, cpr_idx=0),
                "prem_at_cpr1": (round(prem_at_cpr1, 1) if prem_at_cpr1 is not None else None),
            }
        rows.append(row)
    return rows, n_days, n_breakout


def pct(x, n):
    return f"{100.0 * x / n:.0f}%" if n else "n/a"


def median(xs):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def report(year, rows, n_days, n_breakout):
    print("\n" + "=" * 86)
    print(f"  {year}: {n_days} sessions, {n_breakout} had a valid first breakout "
          f"({pct(n_breakout, n_days)})")
    print("=" * 86)
    nb = len(rows)
    # --- underlying reach of CPR levels ---
    r1 = sum(r["reach1"] for r in rows)
    r2 = sum(r["reach2"] for r in rows)
    r3 = sum(r["reach3"] for r in rows)
    print(f"\n  UNDERLYING follow-through (max favourable excursion to 13:00):")
    print(f"    reaches next CPR level (1st): {r1}/{nb}  {pct(r1, nb)}")
    print(f"    reaches 2nd CPR level       : {r2}/{nb}  {pct(r2, nb)}")
    print(f"    reaches 3rd CPR level       : {r3}/{nb}  {pct(r3, nb)}")
    md = median([r["lvl1_dist"] for r in rows])
    mo = median([r["or_range"] for r in rows])
    print(f"    median dist entry->next CPR : ${md:.2f}   (median OR range ${mo:.2f})")

    # --- option premium MFE by strike ---
    print(f"\n  OPTION premium — max favourable excursion (%) by strike moneyness:")
    print(f"    {'strike':<7}{'n':>5}{'medMFE':>8}{'>=50%':>7}{'>=100%':>8}{'>=150%':>8}{'>=200%':>8}")
    for name, _ in STRIKE_OFFSETS:
        sd = [r["strikes"][name] for r in rows if r["strikes"].get(name)]
        n = len(sd)
        if not n:
            continue
        med = median([s["mfe"] for s in sd])
        h = {p: sum(s["hit"][p] for s in sd) for p in PCT_TARGETS}
        print(f"    {name:<7}{n:>5}{med:>7.0f}%{pct(h[50], n):>7}{pct(h[100], n):>8}"
              f"{pct(h[150], n):>8}{pct(h[200], n):>8}")

    # --- target BEFORE stop (decision-relevant) ---
    print(f"\n  Does it hit the target BEFORE the stop?  (share target / stop / EOD)")
    for label, key in [("+100% prem  vs -50% stop", "t100_vs_stop50"),
                       ("+100% prem  vs -75% stop", "t100_vs_stop75"),
                       ("next CPR lvl vs -50% stop", "cpr1_vs_stop50"),
                       ("next CPR lvl vs -75% stop", "cpr1_vs_stop75")]:
        print(f"    {label}:")
        for name, _ in STRIKE_OFFSETS:
            sd = [r["strikes"][name] for r in rows if r["strikes"].get(name)]
            n = len(sd)
            if not n:
                continue
            tg = sum(1 for s in sd if s[key] == "target")
            st = sum(1 for s in sd if s[key] == "stop")
            eo = sum(1 for s in sd if s[key] == "eod")
            print(f"      {name:<6} target {pct(tg, n):>5} | stop {pct(st, n):>5} | eod {pct(eo, n):>5}")

    # --- premium gain when underlying AT next CPR (ATM vs OTM) ---
    print(f"\n  Option gain (%) at the moment underlying first reaches next CPR level:")
    print(f"    (tests 'CPR move ~= 100% for OTM vs less for ATM')")
    for name, _ in STRIKE_OFFSETS:
        sd = [r["strikes"][name] for r in rows if r["strikes"].get(name)]
        vals = [s["prem_at_cpr1"] for s in sd if s.get("prem_at_cpr1") is not None]
        med = median(vals)
        print(f"    {name:<6} median {med if med is None else round(med):>4}%   (n={len(vals)})")


def main():
    days = load_all_underlying()
    cpr = daily_ohlc_and_cpr(days)
    for year in ("2025", "2026"):
        rows, nd, nb = study(year, days, cpr)
        report(year, rows, nd, nb)


if __name__ == "__main__":
    main()
