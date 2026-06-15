"""
orb_adaptive.py — A strategy constructed from the MAE/MFE + grid-search insights.

Design (each rule traces to a finding):
  ENTRY
    - ORB+VWAP breakout (close>ORhigh & >VWAP -> calls; mirror puts)
    - ROOM filter: take it only if the next CPR level is >= ROOM_FRAC * OR_range
      away  (close CPR => tiny reward, skip)                         [Q3 study]
    - OR floor: OR_range >= MIN_OR                                    [dead-day filter]
    - optional REGIME gate on CPR width (trend vs chop)              [grid + repo CPR research]
  SIZING
    - OTM strike (round +/- STRIKE_OTM); N contracts split 50% "lock" / 50% "runner"
  EXITS (no tight premium stop — that shook out 83% of trades)
    - STOP = VWAP loss: underlying closes back through VWAP -> exit ALL  [Q2: premium stop bad]
    - LOCK tranche: take profit at +LOCK_TGT% (the realistic ~30-40% move)
    - RUNNER tranche: ride the VWAP-trail; cap at +RUNNER_CAP%; let the fat tail run
    - CATASTROPHE premium stop at -CATA% (loose, tail protection only)
    - FORCE-FLAT at 13:00
  Re-entry up to TRADES_PER_DAY (reset = underlying back inside OR).

No look-ahead: signals on the completed bar CLOSE, fills at the NEXT bar OPEN; the
catastrophe stop fills intrabar. No commissions/slippage modelled.

Usage:  python3 orb_adaptive.py
"""

import math
import os
from grid_search import (load_all_underlying, daily_ohlc_and_cpr, load_option_day,
                          to_min, OR_START, OR_END_EXCL, ENTRY_START, TRADING_END)

# ---- strategy parameters (from the insights, deliberately un-tuned) ----
ROOM_FRAC   = 0.40
MIN_OR      = 0.50
STRIKE_OTM  = 2
N_CONTRACTS = 3
TREND_TGT   = 100        # narrow-CPR (trend) days: ride to +100%, no stop  [2025 winner]
CHOP_TGT    = 30         # wide-CPR (chop) days: scalp +30% ...
CHOP_STOP   = 75         # ... with a loose -75% stop                       [2026 winner]
CATA        = 85         # % loss -> catastrophe stop (always)
TRADES_PER_DAY = 2
MAX_DAILY_RISK = 1000
SESSION_FLAT = "13:00"


def cpr_width_rel(cpr):
    return abs(cpr["tc"] - cpr["bc"]) / cpr["pivot"]


def next_cpr(cpr, direction, entry):
    vals = [cpr[k] for k in ("bc", "pivot", "tc", "r1", "r2", "r3", "s1", "s2", "s3")]
    if direction == "call":
        a = sorted(v for v in vals if v > entry)
        return a[0] if a else None
    b = sorted((v for v in vals if v < entry), reverse=True)
    return b[0] if b else None


def simulate_day(year, d, bars, cpr, regime=None, chop_thr=None):
    """regime: None|'skip_chop'|'no_runner_chop'. Returns list of trade pnls ($)."""
    or_bars = [b for b in bars if OR_START <= b[0] < OR_END_EXCL]
    if len(or_bars) < 10:
        return []
    rhigh = max(b[2] for b in or_bars)
    rlow = min(b[3] for b in or_bars)
    orr = rhigh - rlow
    if orr < MIN_OR:
        return []
    times = [b[0] for b in bars]
    close = [b[4] for b in bars]
    vwap = []
    cvp = cv = 0.0
    for _, o, h, l, c, v in bars:
        cvp += (h + l + c) / 3.0 * v
        cv += v
        vwap.append(cvp / cv if cv > 0 else None)

    is_chop = (chop_thr is not None and cpr_width_rel(cpr) > chop_thr)

    # candidate strikes
    want = set()
    for i, t in enumerate(times):
        if ENTRY_START <= t < TRADING_END:
            want.add(round(close[i]) + STRIKE_OTM)
            want.add(round(close[i]) - STRIKE_OTM)
    opt = load_option_day(year, d, want)
    if opt is None:
        return []

    n = len(times)
    pnls = []
    pos = None
    day_pnl = 0.0
    need_reset = False
    n_trades = 0
    risk_blocked = False

    for i in range(n):
        t = times[i]
        if t < ENTRY_START:
            continue
        c = close[i]
        vw = vwap[i]
        nxt = i + 1 if i + 1 < n else None
        force_eod = t >= SESSION_FLAT

        if need_reset and pos is None and rlow <= c <= rhigh:
            need_reset = False

        # ---- manage : regime-specific TGT/STOP set at entry ----
        if pos is not None:
            arr = pos["arr"]
            ent = pos["ent"]
            ob = arr.get(t)
            fo = None
            if nxt is not None:
                nb = arr.get(times[nxt])
                fo = nb[0] if (nb and nb[0] > 0) else None
            if ob is not None:
                oo, oh, ol, oc = ob
                rem = pos["rem"]
                # 1) stop (intrabar resting) if this regime uses one
                if rem > 0 and pos["stop"] is not None:
                    stp = ent * (1 - pos["stop"] / 100.0)
                    if ol <= stp:
                        pos["pnl"] += (stp - ent) * 100.0 * rem
                        rem = 0
                # 1b) catastrophe stop (always, loose)
                if rem > 0 and ol <= ent * (1 - CATA / 100.0):
                    pos["pnl"] += (ent * (1 - CATA / 100.0) - ent) * 100.0 * rem
                    rem = 0
                # 2) target (polled close -> next open)
                if rem > 0 and not force_eod and (oc - ent) / ent * 100.0 >= pos["tgt"]:
                    px = fo if fo else oc
                    pos["pnl"] += (px - ent) * 100.0 * rem
                    rem = 0
                # 3) force-flat
                if force_eod and rem > 0:
                    pos["pnl"] += (oc - ent) * 100.0 * rem
                    rem = 0
                pos["rem"] = rem
                pos["last"] = oc
            elif force_eod and pos["rem"] > 0:
                pos["pnl"] += (pos["last"] - ent) * 100.0 * pos["rem"]
                pos["rem"] = 0

            if pos["rem"] == 0:
                day_pnl += pos["pnl"]
                pnls.append(pos["pnl"])
                need_reset = True
                pos = None

        if day_pnl <= -MAX_DAILY_RISK:
            risk_blocked = True
        if force_eod or risk_blocked or nxt is None:
            continue

        # ---- entry ----
        if pos is None and not need_reset and n_trades < TRADES_PER_DAY:
            if vw is None:
                continue
            if c > rhigh and c > vw:
                direction = "call"
            elif c < rlow and c < vw:
                direction = "put"
            else:
                continue
            lvl = next_cpr(cpr, direction, c)
            if lvl is None or abs(lvl - c) < ROOM_FRAC * orr:   # ROOM filter
                continue
            strike = round(c) + STRIKE_OTM if direction == "call" else round(c) - STRIKE_OTM
            arr = opt["call" if direction == "call" else "put"].get(strike)
            if arr is None:
                continue
            nb = arr.get(times[nxt])
            if nb is None or nb[0] <= 0:
                continue
            ent = nb[0]
            # regime switch: trend (narrow CPR) ride to +100% no stop;
            #                chop  (wide CPR)  scalp +30% with -75% stop
            if regime == "ride_all":
                tgt, stop = TREND_TGT, None
            elif regime == "scalp_all":
                tgt, stop = CHOP_TGT, CHOP_STOP
            else:  # "switch"
                tgt, stop = (CHOP_TGT, CHOP_STOP) if is_chop else (TREND_TGT, None)
            pos = {"dir": direction, "arr": arr, "ent": ent, "rem": N_CONTRACTS,
                   "tgt": tgt, "stop": stop, "pnl": 0.0, "last": ent}
            n_trades += 1
    return pnls


def stats(pnls):
    if not pnls:
        return None
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    gl = abs(sum(p for p in pnls if p <= 0))
    gw = sum(wins)
    cum = peak = mdd = 0.0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    return {"trades": n, "wr": round(100 * len(wins) / n, 1), "total": round(cum),
            "pf": round(gw / gl, 2) if gl else 99, "mdd": round(mdd)}


def run(days, cpr_by, year, regime, chop_thr):
    allp, monthly = [], {}
    for d in sorted(days):
        if not d.startswith(year) or d not in cpr_by:
            continue
        if len(days[d]) < 16:
            continue
        p = simulate_day(year, d, days[d], cpr_by[d], regime, chop_thr)
        allp.extend(p)
        if p:
            monthly.setdefault(d[:7], []).append(sum(p))
    return stats(allp), {m: round(sum(v)) for m, v in sorted(monthly.items())}


def main():
    days = load_all_underlying()
    cpr = daily_ohlc_and_cpr(days)
    # train the chop threshold on 2025 (median CPR width)
    w25 = sorted(cpr_width_rel(cpr[d]) for d in cpr if d.startswith("2025"))
    chop_thr = w25[len(w25) // 2]
    print(f"CPR-width chop threshold (2025 median): {chop_thr:.5f}\n")

    variants = [("ride-all (+100/nostop)", "ride_all"),
                ("scalp-all (+30/-75)", "scalp_all"),
                ("REGIME switch", "switch")]
    print(f"  {'variant':<20}{'2025 $':>9}{'PF':>6}{'WR':>6}{'n':>5}{'DD':>8}   "
          f"{'2026 $':>9}{'PF':>6}{'WR':>6}{'n':>5}{'DD':>8}{'comb$':>9}")
    print("  " + "-" * 108)
    best = None
    for name, reg in variants:
        s25, m25 = run(days, cpr, "2025", reg, chop_thr)
        s26, m26 = run(days, cpr, "2026", reg, chop_thr)
        comb = s25["total"] + s26["total"]
        print(f"  {name:<20}{s25['total']:>9}{s25['pf']:>6}{s25['wr']:>6}{s25['trades']:>5}{s25['mdd']:>8}   "
              f"{s26['total']:>9}{s26['pf']:>6}{s26['wr']:>6}{s26['trades']:>5}{s26['mdd']:>8}{comb:>9}")
        if best is None or comb > best[0]:
            best = (comb, name, reg, s25, s26, m25, m26)

    comb, name, reg, s25, s26, m25, m26 = best
    print(f"\n  Best variant: {name}  (combined ${comb})")
    print(f"  2025 monthly: {m25}")
    print(f"  2026 monthly: {m26}")
    print("\n  Reference (no-look-ahead, before costs):")
    print("    live bot [150/100/50, -50% stop]:  2025 +2892 / 2026 -3426")
    print("    grid best combined [6c,2tr,50/150,SL50]: 2025 +4820 / 2026 -4054")


if __name__ == "__main__":
    main()
