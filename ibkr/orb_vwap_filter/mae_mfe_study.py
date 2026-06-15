"""
mae_mfe_study.py — Per-breakout, per-strike MAE/MFE dataset for the ORB+VWAP 0DTE setup.

For the FIRST valid ORB+VWAP breakout each day, for each strike moneyness
(ATM, OTM1, OTM2, OTM3), it tracks the option-premium path and records favourable
/adverse excursions and first-touch timing in two intraday windows:

   W1300 : entry -> 13:00   (the live bot's force-flat window)
   W1555 : entry -> 15:55   (extended, to test "the move comes late" — but BEFORE
                             the 0DTE expiry collapse; holding to 16:00 = ~ -100%)

It answers:
  Q1  WHEN do winners happen?            (MFE size & timing; extra MFE after 13:00)
  Q2  Do STOPS help or hurt?             (clean flat-at-13:00 policy compare + recovery)
  Q3  Is the next CPR target too close?  (reward at CPR vs MFE, by CPR distance)
  +   WHERE are the winners?             (segment by entry time / direction / OR range)

NOTE on 0DTE: a long option decays toward 0 by expiry, so excursions are measured
INTRADAY with a hard flat well before the close. MAE/MFE use intrabar high/low
(descriptive, not a tradeable fill).

Outputs mae_mfe_breakouts.csv (one row per breakout per strike).
Usage:  python3 mae_mfe_study.py
"""

import csv
import os
from grid_search import (load_all_underlying, daily_ohlc_and_cpr, load_option_day,
                          to_min, OR_START, OR_END_EXCL, ENTRY_START, TRADING_END, MIN_OR_RANGE)

T1300, T1555 = "13:00", "15:55"
STRIKE_OFFSETS = [("ATM", 0), ("OTM1", 1), ("OTM2", 2), ("OTM3", 3)]
_HERE = os.path.dirname(os.path.abspath(__file__))


def first_breakout(bars):
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


def next_cpr(cpr, direction, entry):
    vals = [cpr[k] for k in ("bc", "pivot", "tc", "r1", "r2", "r3", "s1", "s2", "s3")]
    if direction == "call":
        above = sorted(v for v in vals if v > entry)
        return above[0] if above else None
    below = sorted((v for v in vals if v < entry), reverse=True)
    return below[0] if below else None


def build_rows(year, days, cpr_by_date):
    rows = []
    for d in sorted(days):
        if not d.startswith(year) or d not in cpr_by_date:
            continue
        bars = days[d]
        if len(bars) < 16:
            continue
        fb = first_breakout(bars)
        if fb is None:
            continue
        ei, direction, entry_u, rhigh, rlow = fb
        if ei + 1 >= len(bars):
            continue
        cpr = cpr_by_date[d]
        lvl = next_cpr(cpr, direction, entry_u)
        atm = round(entry_u)
        want = {(atm + k if direction == "call" else atm - k) for _, k in STRIKE_OFFSETS}
        opt = load_option_day(year, d, want)
        if opt is None:
            continue
        side = opt["call"] if direction == "call" else opt["put"]
        times = [b[0] for b in bars]
        ent_t = times[ei + 1]
        ent_min = to_min(ent_t)
        win = [j for j in range(ei + 1, len(bars)) if times[j] <= T1555]
        cpr_idx = None
        if lvl is not None:
            cpr_idx = next((j for j in win if (bars[j][2] >= lvl if direction == "call"
                                               else bars[j][3] <= lvl)), None)
        or_range = rhigh - rlow
        cpr_dist = abs(lvl - entry_u) if lvl is not None else None

        for name, k in STRIKE_OFFSETS:
            strike = atm + k if direction == "call" else atm - k
            arr = side.get(strike)
            eb = arr.get(ent_t) if arr else None
            if not eb or eb[0] <= 0:
                continue
            ent = eb[0]
            mfe13 = mae13 = mfe15 = mae15 = 0.0
            mfe13_min = mfe15_min = 0
            close13 = close15 = None
            first = {30: None, 50: None, 100: None}   # minutes to first reach +X% (W1300)
            stop = {50: None, 75: None}               # minutes to first reach -X% (W1300)
            poststop_mfe13 = None
            for j in win:
                ob = arr.get(times[j])
                if ob is None:
                    continue
                oo, oh, ol, ocl = ob
                up = (oh - ent) / ent * 100.0
                dn = (ol - ent) / ent * 100.0
                mn = to_min(times[j]) - ent_min
                if up > mfe15:
                    mfe15, mfe15_min = up, mn
                if dn < mae15:
                    mae15 = dn
                close15 = (ocl - ent) / ent * 100.0
                if times[j] <= T1300:
                    if up > mfe13:
                        mfe13, mfe13_min = up, mn
                    if dn < mae13:
                        mae13 = dn
                    close13 = (ocl - ent) / ent * 100.0
                    for tgt in first:
                        if first[tgt] is None and up >= tgt:
                            first[tgt] = mn
                    for s in stop:
                        if stop[s] is None and dn <= -s:
                            stop[s] = mn
                    if stop[50] is not None and mn > stop[50]:
                        poststop_mfe13 = max(poststop_mfe13 if poststop_mfe13 is not None else -1e9, up)
            prem_at_cpr = None
            if cpr_idx is not None and arr and times[cpr_idx] in arr:
                prem_at_cpr = (arr[times[cpr_idx]][1] - ent) / ent * 100.0
            rows.append({
                "year": year, "date": d, "dir": direction, "brk_time": times[ei],
                "entry_u": round(entry_u, 2), "or_range": round(or_range, 2),
                "moneyness": name, "strike": strike, "ent_prem": round(ent, 2),
                "cpr_dist": round(cpr_dist, 2) if cpr_dist is not None else "",
                "cpr_dist_pct_or": round(100 * cpr_dist / or_range) if cpr_dist else "",
                "mfe13": round(mfe13, 1), "mfe13_min": mfe13_min, "mae13": round(mae13, 1),
                "close13": round(close13, 1) if close13 is not None else "",
                "mfe15": round(mfe15, 1), "mfe15_min": mfe15_min, "mae15": round(mae15, 1),
                "close15": round(close15, 1) if close15 is not None else "",
                "t_tgt30": first[30] if first[30] is not None else "",
                "t_tgt50": first[50] if first[50] is not None else "",
                "t_tgt100": first[100] if first[100] is not None else "",
                "t_stop50": stop[50] if stop[50] is not None else "",
                "t_stop75": stop[75] if stop[75] is not None else "",
                "poststop_mfe13": round(poststop_mfe13, 1) if poststop_mfe13 is not None else "",
                "reached_cpr": int(cpr_idx is not None),
                "cpr_reach_min": (to_min(times[cpr_idx]) - ent_min) if cpr_idx is not None else "",
                "prem_at_cpr": round(prem_at_cpr, 1) if prem_at_cpr is not None else "",
            })
    return rows


# ------------------------------------------------------------------ helpers
def med(xs):
    xs = sorted(v for v in xs if v is not None)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def pc(x, n):
    return f"{100*x/n:.0f}%" if n else "n/a"


def num(r, k):
    return float(r[k]) if r[k] != "" else None


def analyze(rows):
    by = {nm: [r for r in rows if r["moneyness"] == nm] for nm, _ in STRIKE_OFFSETS}

    print("\n" + "#" * 90)
    print("  Q1. WHEN do winners happen?  (MFE size & timing; extra MFE available after 13:00)")
    print("#" * 90)
    print(f"  {'strike':<7}{'medMFE13':>9}{'medMFEmin':>11}{'medMFE15':>10}{'peakAfter13':>13}{'+>=25pp after13':>16}")
    for nm, _ in STRIKE_OFFSETS:
        rs = by[nm]
        n = len(rs)
        after = sum(1 for r in rs if num(r, "mfe15") > num(r, "mfe13") + 1)
        big = sum(1 for r in rs if num(r, "mfe15") - num(r, "mfe13") >= 25)
        print(f"  {nm:<7}{med([num(r,'mfe13') for r in rs]):>8.0f}%{med([num(r,'mfe13_min') for r in rs]):>10.0f}m"
              f"{med([num(r,'mfe15') for r in rs]):>9.0f}%{pc(after, n):>13}{pc(big, n):>16}")

    print("\n" + "#" * 90)
    print("  Q2. Do STOPS help or hurt?  (all flat at 13:00; intrabar order from first-touch times)")
    print("#" * 90)
    rs = by["OTM2"]
    def policy(stop_pct, target_pct):
        tot = 0.0
        for r in rs:
            cl = num(r, "close13")
            if cl is None:
                continue
            ts = num(r, f"t_stop{stop_pct}") if stop_pct else None
            tt = num(r, f"t_tgt{target_pct}") if target_pct else None
            if ts is not None and (tt is None or ts <= tt):
                tot += -stop_pct
            elif tt is not None:
                tot += target_pct
            else:
                tot += cl
        return round(tot)
    print("  OTM2, single-contract premium-% summed over all breakouts:")
    print(f"    stop50 + target30 : {policy(50,30):>6}%")
    print(f"    stop75 + target30 : {policy(75,30):>6}%")
    print(f"    NOstop + target30 : {policy(None,30):>6}%")
    print(f"    stop50 + target100: {policy(50,100):>6}%")
    print(f"    NOstop + target100: {policy(None,100):>6}%")
    print(f"    NOstop, no target (flat 13:00 close): {policy(None,None):>6}%")
    dipped = [r for r in rs if num(r, "t_stop50") is not None]
    nd = len(dipped)
    rec30 = sum(1 for r in dipped if num(r, "poststop_mfe13") is not None and num(r, "poststop_mfe13") >= 30)
    rec50 = sum(1 for r in dipped if num(r, "poststop_mfe13") is not None and num(r, "poststop_mfe13") >= 50)
    print(f"  Of trades that dip to -50% intraday ({nd}/{len(rs)}={pc(nd,len(rs))}), if NOT stopped they later reach:")
    print(f"    +30% again: {pc(rec30, nd)}   |   +50% again: {pc(rec50, nd)}   (the rest keep decaying)")

    print("\n" + "#" * 90)
    print("  Q3. Is the next CPR target too close?  (OTM2: reward AT cpr vs MFE, by CPR distance)")
    print("#" * 90)
    rs = [r for r in by["OTM2"] if r["cpr_dist_pct_or"] != ""]
    print(f"  {'CPRdist(%OR)':<13}{'n':>4}{'medPremAtCPR':>14}{'medMFE13':>10}{'%MFE>=100':>11}{'medReachMin':>13}")
    for lbl, lo, hi in [("<25%", 0, 25), ("25-50%", 25, 50), ("50-75%", 50, 75),
                        ("75-100%", 75, 100), (">=100%", 100, 1e9)]:
        b = [r for r in rs if lo <= float(r["cpr_dist_pct_or"]) < hi]
        n = len(b)
        if not n:
            continue
        pac = med([num(r, "prem_at_cpr") for r in b if num(r, "prem_at_cpr") is not None])
        mfe100 = sum(1 for r in b if num(r, "mfe13") >= 100)
        rm = med([num(r, "cpr_reach_min") for r in b if num(r, "cpr_reach_min") is not None])
        print(f"  {lbl:<13}{n:>4}{(round(pac) if pac is not None else 'n/a'):>13}%"
              f"{med([num(r,'mfe13') for r in b]):>9.0f}%{pc(mfe100, n):>11}{(round(rm) if rm is not None else 'n/a'):>13}")
    print("  (premAtCPR = option gain if you exit when underlying first touches next CPR;")
    print("   when CPR is close it's a small reward, yet MFE shows the move usually runs much further)")

    print("\n" + "#" * 90)
    print("  WHERE ARE THE WINNERS?  (OTM2: median MFE13 / MAE13 / %that ever reach +50% by 13:00)")
    print("#" * 90)
    rs = by["OTM2"]
    def seg(label, keyfn, order):
        groups = {}
        for r in rs:
            groups.setdefault(keyfn(r), []).append(r)
        print(f"  by {label}:")
        for g in order:
            b = groups.get(g, [])
            n = len(b)
            if not n:
                continue
            r50 = sum(1 for r in b if num(r, "t_tgt50") is not None)
            print(f"    {str(g):<11} n={n:<4} medMFE {med([num(r,'mfe13') for r in b]):>4.0f}%  "
                  f"medMAE {med([num(r,'mae13') for r in b]):>5.0f}%  reach+50 {pc(r50,n)}")
    seg("entry time", lambda r: ("<10:00" if r["brk_time"] < "10:00" else "10-11" if r["brk_time"] < "11:00"
                                 else "11-12" if r["brk_time"] < "12:00" else ">=12:00"),
        ["<10:00", "10-11", "11-12", ">=12:00"])
    seg("direction", lambda r: r["dir"], ["call", "put"])
    seg("OR range", lambda r: ("narrow<2" if num(r, "or_range") < 2 else "mid2-4"
                               if num(r, "or_range") < 4 else "wide>=4"),
        ["narrow<2", "mid2-4", "wide>=4"])


def main():
    days = load_all_underlying()
    cpr = daily_ohlc_and_cpr(days)
    allrows = []
    for year in ("2025", "2026"):
        allrows += build_rows(year, days, cpr)
    out_csv = os.path.join(_HERE, "mae_mfe_breakouts.csv")
    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(allrows[0].keys()))
        w.writeheader()
        w.writerows(allrows)
    print(f"Wrote {len(allrows)} rows ({len(allrows)//len(STRIKE_OFFSETS)} breakouts x {len(STRIKE_OFFSETS)} strikes) -> {out_csv}")
    for year in ("2025", "2026"):
        yr = [r for r in allrows if r["year"] == year]
        print("\n" + "=" * 90 + f"\n  YEAR {year}\n" + "=" * 90)
        analyze(yr)


if __name__ == "__main__":
    main()
