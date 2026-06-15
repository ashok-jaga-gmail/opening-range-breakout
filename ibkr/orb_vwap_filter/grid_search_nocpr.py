"""
grid_search_nocpr.py — Is there a CPR-free strategy that is profitable in 2026?

Searches a percentage-only space (NO CPR targets, NO CPR regime) with the same
no-look-ahead engine, optimised for 2026 and scored NET of commissions.
New non-CPR levers added: strike moneyness (OTM offset), OR-range floor,
direction filter (calls/puts/both).
"""
import math, os
from grid_search import (load_all_underlying, daily_ohlc_and_cpr, load_option_day,
                         to_min, OR_START, OR_END_EXCL, ENTRY_START, TRADING_END)

MAX_DAILY_RISK = 1000
REENTRY_WAIT = 15
COMMISSION = 0.65          # $/contract/side  -> ~1.30 round trip per contract


def build_ctx(year, days):
    ctxs = []
    for d in sorted(days):
        if not d.startswith(year) or len(days[d]) < 16:
            continue
        bars = days[d]
        ob = [b for b in bars if OR_START <= b[0] < OR_END_EXCL]
        if len(ob) < 10:
            continue
        rhigh = max(b[2] for b in ob); rlow = min(b[3] for b in ob)
        orr = rhigh - rlow
        if orr < 0.10:
            continue
        times = [b[0] for b in bars]; close = [b[4] for b in bars]
        vwap = []; cvp = cv = 0.0
        for _, o, h, l, c, v in bars:
            cvp += (h + l + c) / 3.0 * v; cv += v
            vwap.append(cvp / cv if cv > 0 else None)
        want = set()
        for i, t in enumerate(times):
            if ENTRY_START <= t < TRADING_END:
                base = round(close[i])
                for k in (1, 2, 3):
                    want.add(base + k); want.add(base - k)
        opt = load_option_day(year, d, want)
        if opt is None:
            continue
        idx = {t: i for i, t in enumerate(times)}
        def align(side):
            out = {}
            for strike, series in side.items():
                arr = [None] * len(times)
                for t, o in series.items():
                    if t in idx:
                        arr[idx[t]] = o
                out[strike] = arr
            return out
        ctxs.append({"date": d, "times": times, "close": close, "vwap": vwap, "rhigh": rhigh,
                     "rlow": rlow, "orr": orr, "call": align(opt["call"]), "put": align(opt["put"]),
                     "e0": next((i for i, t in enumerate(times) if t >= ENTRY_START), len(times)),
                     "eod": next((i for i, t in enumerate(times) if t >= TRADING_END), len(times))})
    return ctxs


def sim(ctx, c):
    times, close, vwap = ctx["times"], ctx["close"], ctx["vwap"]
    rhigh, rlow, orr = ctx["rhigh"], ctx["rlow"], ctx["orr"]
    n = len(times); eod = ctx["eod"]
    if orr < c["or_floor"]:
        return []
    tpd, split, tgts, sl, otm, dirf = (c["tpd"], c["split"], c["tgts"], c["sl"] / 100.0,
                                       c["otm"], c["dir"])
    pnls = []; pos = None; day = 0.0; reset = False; laststop = None; nt = 0; blocked = False
    for i in range(n):
        if i < ctx["e0"]:
            continue
        t = times[i]; px = close[i]; cm = to_min(t); feod = i >= eod
        nxt = i + 1 if i + 1 < n else None
        if reset and pos is None and rlow <= px <= rhigh:
            reset = False
        if pos is not None:
            arr = pos["arr"]; ent = pos["ent"]; ob = arr[i]
            fo = (arr[nxt][0] if (nxt is not None and arr[nxt] and arr[nxt][0] > 0) else None)
            if ob is not None:
                oo, oh, ol, oc = ob; rem = pos["rem"]
                if sl < 1.0 and rem > 0 and ol <= ent * (1 - sl):
                    pos["g"] += (ent * (1 - sl) - ent) * 100.0 * rem; rem = 0
                if rem > 0:
                    opp = (pos["d"] == "call" and px < rlow) or (pos["d"] == "put" and px > rhigh)
                    if opp:
                        p = fo if fo else oc; pos["g"] += (p - ent) * 100.0 * rem; rem = 0
                if rem > 0 and not feod and pos["ti"] < len(pos["tl"]):
                    q, trig = pos["tl"][pos["ti"]]
                    if (oc - ent) / ent * 100.0 >= trig:
                        p = fo if fo else oc; s = min(q, rem)
                        pos["g"] += (p - ent) * 100.0 * s; rem -= s; pos["ti"] += 1
                if feod and rem > 0:
                    pos["g"] += (oc - ent) * 100.0 * rem; rem = 0
                pos["rem"] = rem; pos["last"] = oc
            elif feod and pos["rem"] > 0:
                pos["g"] += (pos["last"] - ent) * 100.0 * pos["rem"]; pos["rem"] = 0
            if pos["rem"] == 0:
                net = pos["g"] - COMMISSION * 2 * pos["n0"]    # entry + exit legs
                day += net; pnls.append(net)
                if pos["stopped"]:
                    laststop = cm
                reset = True; pos = None
        if day <= -MAX_DAILY_RISK:
            blocked = True
        if feod or blocked or nxt is None:
            continue
        if pos is None and not reset and nt < tpd:
            if laststop is not None and (cm - laststop) < REENTRY_WAIT:
                continue
            vw = vwap[i]
            if vw is None:
                continue
            if px > rhigh and px > vw and dirf in ("both", "call"):
                d_ = "call"; strike = round(px) + otm; arr = ctx["call"].get(strike)
            elif px < rlow and px < vw and dirf in ("both", "put"):
                d_ = "put"; strike = round(px) - otm; arr = ctx["put"].get(strike)
            else:
                continue
            if arr is None:
                continue
            nb = arr[nxt]
            if nb is None or nb[0] <= 0:
                continue
            ent = nb[0]
            pos = {"d": d_, "arr": arr, "ent": ent, "rem": sum(split), "n0": sum(split),
                   "tl": [(split[k], tgts[k]) for k in range(len(split))], "ti": 0,
                   "g": 0.0, "last": ent, "stopped": (sl < 1.0)}
            nt += 1
    return pnls


def stats(pnls):
    if not pnls:
        return None
    n = len(pnls); wins = [p for p in pnls if p > 0]
    gl = abs(sum(p for p in pnls if p <= 0)); gw = sum(wins)
    return {"n": n, "wr": round(100 * len(wins) / n), "net": round(sum(pnls)),
            "pf": round(gw / gl, 2) if gl else 99}


def configs():
    cs = []
    ladders = [[[25]], [[30]], [[40]], [[50]], [[75]], [[100]],
               [[30, 75]], [[40, 100]], [[50, 150]]]
    for L in ladders:
        tg = L[0]; split = [1] * len(tg)
        for tpd in (1, 2, 3):
            for sl in (40, 50, 60, 75, 100):
                for otm in (1, 2, 3):
                    for of in (0.0, 1.0, 1.5):
                        for dr in ("both", "call", "put"):
                            cs.append({"tpd": tpd, "split": split, "tgts": tg, "sl": sl,
                                       "otm": otm, "or_floor": of, "dir": dr})
    return cs


def label(c):
    return (f"tpd{c['tpd']} otm{c['otm']} {c['dir'][:4]} t{'/'.join(map(str,c['tgts']))} "
            f"sl{c['sl']} orf{c['or_floor']}")


def main():
    days = load_all_underlying()
    print("building contexts ...", flush=True)
    c25 = build_ctx("2025", days); c26 = build_ctx("2026", days)
    print(f"  2025 {len(c25)} sessions, 2026 {len(c26)} sessions", flush=True)
    cs = configs()
    print(f"evaluating {len(cs)} CPR-free configs (net of ${COMMISSION}/contract/side) ...", flush=True)
    rows = []
    for j, c in enumerate(cs):
        s26 = stats([p for ctx in c26 for p in sim(ctx, c)])
        if not s26:
            continue
        s25 = stats([p for ctx in c25 for p in sim(ctx, c)])
        rows.append((c, s25, s26))
        if (j + 1) % 500 == 0:
            print(f"  {j+1}/{len(cs)}", flush=True)
    pos26 = [r for r in rows if r[2]["net"] > 0]
    both = [r for r in rows if r[1] and r[1]["net"] > 0 and r[2]["net"] > 0]
    print(f"\nNET-profitable in 2026: {len(pos26)}/{len(rows)}   |   net-positive BOTH years: {len(both)}")
    def show(title, rk, k=15):
        print("\n" + "=" * 92 + f"\n  {title}\n" + "=" * 92)
        print(f"  {'config':<46}{'26net':>7}{'26pf':>6}{'26wr':>5}{'26n':>5}{'25net':>8}{'25pf':>6}")
        for c, s25, s26 in rk[:k]:
            print(f"  {label(c):<46}{s26['net']:>7}{s26['pf']:>6}{s26['wr']:>5}{s26['n']:>5}"
                  f"{(s25['net'] if s25 else 0):>8}{(s25['pf'] if s25 else 0):>6}")
    show("TOP 2026 NET (CPR-free, after commissions)", sorted(rows, key=lambda r: -r[2]["net"]))
    if both:
        show(f"NET-POSITIVE IN BOTH YEARS ({len(both)})", sorted(both, key=lambda r: -(r[1]["net"]+r[2]["net"])))


if __name__ == "__main__":
    main()
