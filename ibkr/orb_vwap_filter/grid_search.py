"""
grid_search.py — Optimize the ORB + VWAP Filter 0DTE strategy over its key knobs.

Searches, with the SAME no-look-ahead engine as backtest_vwap_filter.py
(signals on a completed bar's close, fills at the next bar's open, only the
resting -50%-style stop fills intrabar):

  - trades/day        max entries per session            {1, 2, 3, unlimited}
  - contracts         total per trade  (<= 9)            {tranches x per}
  - tranches          scale-out legs   (<= 4)            {1, 2, 3, 4}
  - profit targets    pt1..pt4, two flavours:
        PCT  -> premium thresholds, e.g. [50,100,150] (% gain on entry premium)
        CPR  -> underlying reaching pivot levels (direction-aware r/s/tc levels)
  - stop loss %       premium stop                       {30,40,50,60,75}

Methodology: TRAIN on 2025, VALIDATE on 2026 (partial, out-of-sample-ish).
Option data is loaded once and pre-aligned to per-day arrays; ~1.2k configs
are then replayed in memory.

Usage:  python3 grid_search.py
"""

import csv
import glob
import json
import lzma
import math
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_CSV = os.path.join(_HERE, "..", "..", "qqq_1m_2018_2026.csv.xz")
UNDERLYING_DIRS = {
    "2024": os.path.expanduser("~/backups/QQQ/2025/1m"),   # Dec-2024 file lives here
    "2025": os.path.expanduser("~/backups/QQQ/2025/1m"),
    "2026": os.path.expanduser("~/backups/QQQ/2026/1m"),
}
OPTIONS_DIRS = {
    "2025": os.path.expanduser("~/backups/QQQ/2025/Options-OHLC/thetadata-2025"),
    "2026": os.path.expanduser("~/backups/QQQ/2026/Options-OHLC/thetadata-2026"),
}

OR_START, OR_END_EXCL = "09:30", "09:45"
ENTRY_START, TRADING_END = "09:45", "13:00"
MAX_DAILY_RISK = 1000
REENTRY_WAIT_MIN = 15
MIN_OR_RANGE = 0.10
OTM = 3  # strikes out


def to_min(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


# ---------------------------------------------------------------- underlying
def load_all_underlying():
    """date -> sorted list of (hhmm,o,h,l,c,v) RTH bars; needs volume."""
    days = {}
    seen = set()

    def add(d, t, o, h, l, c, v):
        if (d, t) in seen or t < "09:30" or t > "16:00":
            return
        seen.add((d, t))
        days.setdefault(d, []).append((t, o, h, l, c, v))

    if os.path.exists(REPO_CSV):
        with lzma.open(REPO_CSV, "rt") as f:
            for row in csv.DictReader(f):
                if not row.get("volume"):
                    continue
                y = row["date"][:4]
                if y in ("2024", "2025", "2026"):
                    add(row["date"], row["time"], float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"]), float(row["volume"]))
    for dpath in set(UNDERLYING_DIRS.values()):
        for path in sorted(glob.glob(os.path.join(dpath, "qqq_1m_*.csv"))):
            with open(path) as f:
                r = csv.DictReader(f)
                if "volume" not in (r.fieldnames or []):
                    continue
                for row in r:
                    if not row.get("volume"):
                        continue
                    y = row["date"][:4]
                    if y in ("2024", "2025", "2026"):
                        add(row["date"], row["time"], float(row["open"]), float(row["high"]),
                            float(row["low"]), float(row["close"]), float(row["volume"]))
    for d in days:
        days[d].sort()
    return days


def daily_ohlc_and_cpr(days):
    """Return cpr_by_date: date -> dict of pivot levels from PRIOR day's OHLC."""
    dohlc = {}
    for d, bars in days.items():
        hi = max(b[2] for b in bars)
        lo = min(b[3] for b in bars)
        cl = bars[-1][4]
        dohlc[d] = (hi, lo, cl)
    dates = sorted(dohlc)
    cpr = {}
    for i, d in enumerate(dates):
        if i == 0:
            continue
        ph, pl, pc = dohlc[dates[i - 1]]
        pivot = (ph + pl + pc) / 3
        bc = (ph + pl) / 2
        tc = (pivot - bc) + pivot
        rng = ph - pl
        cpr[d] = {
            "pivot": pivot, "bc": bc, "tc": tc,
            "r1": 2 * pivot - pl, "r2": pivot + rng, "r3": (2 * pivot - pl) + rng,
            "s1": 2 * pivot - ph, "s2": pivot - rng, "s3": (2 * pivot - ph) - rng,
        }
    return cpr


# ---------------------------------------------------------------- options
def load_option_day(year, date_str, want_strikes):
    import pandas as pd
    path = os.path.join(OPTIONS_DIRS[year], f"qqq-options-1m-{date_str.replace('-', '')}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path, columns=["open", "high", "low", "close", "timestamp", "strike", "right"])
    df = df[(df["right"].isin(["C", "P"])) & (df["strike"].isin(want_strikes))]
    out = {"call": {}, "put": {}}
    for o, h, l, c, ts, strike, right in zip(
        df["open"], df["high"], df["low"], df["close"], df["timestamp"], df["strike"], df["right"]
    ):
        r = "call" if right == "C" else "put"
        out[r].setdefault(int(strike), {})[str(ts)[11:16]] = (float(o), float(h), float(l), float(c))
    return out


# ---------------------------------------------------------------- per-day context
def build_contexts(year, days, cpr_by_date):
    """Pre-compute everything config-independent into fast arrays."""
    ctxs = []
    n_missing = 0
    for d in sorted(days):
        if not d.startswith(year):
            continue
        bars = days[d]
        if len(bars) < 16:
            continue
        or_bars = [b for b in bars if OR_START <= b[0] < OR_END_EXCL]
        if len(or_bars) < 10:
            continue
        rhigh = max(b[2] for b in or_bars)
        rlow = min(b[3] for b in or_bars)
        if rhigh - rlow < MIN_OR_RANGE:
            continue

        times = [b[0] for b in bars]
        close = [b[4] for b in bars]
        # session VWAP
        vwap = []
        cvp = cv = 0.0
        for _, o, h, l, c, v in bars:
            cvp += (h + l + c) / 3.0 * v
            cv += v
            vwap.append(cvp / cv if cv > 0 else None)

        # candidate entry strikes across the post-09:45 window
        want = set()
        for i, t in enumerate(times):
            if t < ENTRY_START or t >= TRADING_END:
                continue
            c = close[i]
            want.add(math.floor(c) + OTM)   # call strike
            want.add(math.ceil(c) - OTM)    # put strike
        opt = load_option_day(year, d, want)
        if opt is None:
            n_missing += 1
            continue

        # align option OHLC to the underlying time index for each needed strike
        idx = {t: i for i, t in enumerate(times)}
        ncall = {}
        for strike, series in opt["call"].items():
            arr = [None] * len(times)
            for t, ohlc in series.items():
                if t in idx:
                    arr[idx[t]] = ohlc
            ncall[strike] = arr
        nput = {}
        for strike, series in opt["put"].items():
            arr = [None] * len(times)
            for t, ohlc in series.items():
                if t in idx:
                    arr[idx[t]] = ohlc
            nput[strike] = arr

        ctxs.append({
            "date": d, "times": times, "close": close, "vwap": vwap,
            "rhigh": rhigh, "rlow": rlow, "cpr": cpr_by_date.get(d),
            "call": ncall, "put": nput,
            "entry_i0": next((i for i, t in enumerate(times) if t >= ENTRY_START), len(times)),
            "eod_i": next((i for i, t in enumerate(times) if t >= TRADING_END), len(times)),
        })
    return ctxs, n_missing


# ---------------------------------------------------------------- simulate one config
def sim_day(ctx, cfg):
    """Return list of trade PnLs ($, per the full position) for one day."""
    times, close, vwap = ctx["times"], ctx["close"], ctx["vwap"]
    rhigh, rlow, cpr = ctx["rhigh"], ctx["rlow"], ctx["cpr"]
    opt_call, opt_put = ctx["call"], ctx["put"]
    n = len(times)
    eod_i = ctx["eod_i"]

    tpd = cfg["tpd"]
    split = cfg["split"]            # contracts per tranche
    mode = cfg["mode"]
    targets = cfg["targets"]        # pct numbers OR cpr level names
    sl = cfg["sl"] / 100.0

    pnls = []
    pos = None
    day_pnl = 0.0
    need_reset = False
    last_stop_min = None
    n_trades = 0
    risk_blocked = False

    for i in range(n):
        t = times[i]
        if i < ctx["entry_i0"]:
            continue
        cur_min = to_min(t)
        c = close[i]
        force_eod = (i >= eod_i)
        nxt = i + 1 if i + 1 < n else None

        if need_reset and pos is None and rlow <= c <= rhigh:
            need_reset = False

        # ---- manage ----
        if pos is not None:
            arr = pos["arr"]
            ob = arr[i]
            ent = pos["ent"]
            stop_px = pos["stop_px"]
            fill_open = (arr[nxt][0] if (nxt is not None and arr[nxt] and arr[nxt][0] > 0) else None)
            if ob is not None:
                oo, oh, ol, oc = ob
                pos["last"] = oc
                rem = pos["rem"]
                # 1) resting stop (intrabar)
                if rem > 0 and ol <= stop_px:
                    pos["pnl"] += (stop_px - ent) * 100.0 * rem
                    pos["stopped"] = True
                    rem = 0
                # 2) opposite breakout
                if rem > 0:
                    opp = (pos["dir"] == "call" and c < rlow) or (pos["dir"] == "put" and c > rhigh)
                    if opp:
                        px = fill_open if fill_open else oc
                        pos["pnl"] += (px - ent) * 100.0 * rem
                        rem = 0
                # 3) tranche targets (one tranche per poll, in order)
                if rem > 0 and not force_eod:
                    ti = pos["ti"]
                    if ti < len(pos["tlist"]):
                        qty, trig = pos["tlist"][ti]
                        hit = False
                        if mode == "pct":
                            if (oc - ent) / ent * 100.0 >= trig:
                                hit = True
                        else:  # cpr: underlying reaches level
                            if pos["dir"] == "call":
                                hit = c >= trig
                            else:
                                hit = c <= trig
                        if hit:
                            px = fill_open if fill_open else oc
                            sell = min(qty, rem)
                            pos["pnl"] += (px - ent) * 100.0 * sell
                            rem -= sell
                            pos["ti"] += 1
                # 4) EOD
                if force_eod and rem > 0:
                    pos["pnl"] += (oc - ent) * 100.0 * rem
                    rem = 0
                pos["rem"] = rem
            elif force_eod and pos["rem"] > 0:
                pos["pnl"] += (pos["last"] - ent) * 100.0 * pos["rem"]
                pos["rem"] = 0

            if pos["rem"] == 0:
                day_pnl += pos["pnl"]
                pnls.append(pos["pnl"])
                if pos["stopped"]:
                    last_stop_min = cur_min
                need_reset = True
                pos = None

        if day_pnl <= -MAX_DAILY_RISK:
            risk_blocked = True
        if force_eod or risk_blocked or nxt is None:
            continue

        # ---- entry ----
        if pos is None and not need_reset and n_trades < tpd:
            if last_stop_min is not None and (cur_min - last_stop_min) < REENTRY_WAIT_MIN:
                continue
            vw = vwap[i]
            if vw is None:
                continue
            if c > rhigh and c > vw:
                d_ = "call"
                strike = math.floor(c) + OTM
                arr = opt_call.get(strike)
            elif c < rlow and c < vw:
                d_ = "put"
                strike = math.ceil(c) - OTM
                arr = opt_put.get(strike)
            else:
                continue
            if arr is None:
                continue
            nb = arr[nxt]
            if nb is None or nb[0] <= 0:
                continue
            ent = nb[0]
            # build tranche list (qty, trigger)
            if mode == "pct":
                tlist = [(split[k], targets[k]) for k in range(len(split))]
            else:
                if cpr is None:
                    continue
                levels = [cpr[name] for name in targets]
                if d_ == "call":
                    eff = sorted([lv for lv in levels if lv > c])
                else:
                    eff = sorted([lv for lv in levels if lv < c], reverse=True)
                # map tranches onto reachable levels; extras ride to EOD/stop
                tlist = []
                for k in range(len(split)):
                    if k < len(eff):
                        tlist.append((split[k], eff[k]))
                # remaining contracts (unmapped tranches) have no target
            total = sum(split)
            pos = {
                "dir": d_, "arr": arr, "ent": ent,
                "stop_px": ent * (1 - sl), "rem": total, "pnl": 0.0,
                "tlist": tlist, "ti": 0, "stopped": False, "last": ent,
            }
            n_trades += 1
    return pnls


# ---------------------------------------------------------------- stats
def evaluate(ctxs, cfg):
    allp = []
    for ctx in ctxs:
        allp.extend(sim_day(ctx, cfg))
    if not allp:
        return None
    n = len(allp)
    wins = [p for p in allp if p > 0]
    gl = abs(sum(p for p in allp if p <= 0))
    gw = sum(wins)
    cum = peak = mdd = 0.0
    for p in allp:
        cum += p
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    mean = cum / n
    var = sum((p - mean) ** 2 for p in allp) / n
    std = math.sqrt(var) if var > 0 else 0.0
    return {
        "trades": n,
        "win_rate": round(100 * len(wins) / n, 1),
        "total": round(cum, 0),
        "pf": round(gw / gl, 3) if gl > 0 else 99.0,
        "mdd": round(mdd, 0),
        "sharpe": round(mean / std, 3) if std > 0 else 0.0,
        "calmar": round(cum / abs(mdd), 2) if mdd < 0 else 99.0,
    }


# ---------------------------------------------------------------- config space
def build_configs():
    tpd_opts = [1, 2, 3, 99]
    sl_opts = [30, 40, 50, 60, 75, 100]   # 100 = effectively NO stop (premium can't go <0)
    # 9999 = "ride to EOD/stop" (target never realistically hit)
    pct_ladders = {
        1: [[30], [50], [100], [150], [200], [300], [400], [500], [750], [1000], [9999]],
        2: [[50, 150], [50, 300], [100, 200], [100, 400], [150, 500], [200, 600], [100, 9999], [50, 9999]],
        3: [[50, 100, 150], [50, 150, 300], [50, 150, 500], [100, 300, 750], [50, 200, 600], [100, 200, 9999]],
        4: [[50, 100, 150, 200], [50, 100, 200, 300], [50, 150, 300, 600], [100, 250, 500, 1000], [50, 150, 400, 9999]],
    }
    cpr_ladders = {
        1: [["r1"]],
        2: [["r1", "r2"]],
        3: [["r1", "r2", "r3"]],
        4: [["tc", "r1", "r2", "r3"]],
    }
    per_opts = {1: [1, 2, 3], 2: [1, 2, 3], 3: [1, 2, 3], 4: [1, 2]}  # contracts = T*per <= 9

    cfgs = []
    for T in (1, 2, 3, 4):
        ladders = [("pct", L) for L in pct_ladders[T]] + [("cpr", L) for L in cpr_ladders[T]]
        for per in per_opts[T]:
            split = [per] * T            # even split
            contracts = per * T
            for mode, L in ladders:
                for tpd in tpd_opts:
                    for sl in sl_opts:
                        cfgs.append({
                            "tpd": tpd, "tranches": T, "contracts": contracts,
                            "split": split, "mode": mode, "targets": L, "sl": sl,
                        })
    return cfgs


def cfg_label(c):
    tgt = ",".join(str(x) for x in c["targets"])
    tpd = "inf" if c["tpd"] >= 99 else c["tpd"]
    return (f"tpd={tpd} ctr={c['contracts']} tr={c['tranches']} "
            f"{c['mode']}[{tgt}] sl={c['sl']}")


def main():
    print("Loading underlying ...", flush=True)
    days = load_all_underlying()
    cpr = daily_ohlc_and_cpr(days)
    print("Building 2025 contexts (loading option data once) ...", flush=True)
    ctx25, miss25 = build_contexts("2025", days, cpr)
    print(f"  2025: {len(ctx25)} sessions ({miss25} missing options)", flush=True)
    print("Building 2026 contexts ...", flush=True)
    ctx26, miss26 = build_contexts("2026", days, cpr)
    print(f"  2026: {len(ctx26)} sessions ({miss26} missing options)", flush=True)

    cfgs = build_configs()
    print(f"Evaluating {len(cfgs)} configs on 2025 (train) + 2026 (validate) ...", flush=True)

    rows = []
    for j, c in enumerate(cfgs):
        s25 = evaluate(ctx25, c)
        s26 = evaluate(ctx26, c)
        if s25 and s26:
            rows.append({"cfg": c, "label": cfg_label(c), "y2025": s25, "y2026": s26,
                         "combined": round(s25["total"] + s26["total"], 0)})
        if (j + 1) % 200 == 0:
            print(f"  {j + 1}/{len(cfgs)}", flush=True)

    def show(title, ranked, k=12):
        print("\n" + "=" * 100)
        print(f"  {title}")
        print("=" * 100)
        print(f"  {'config':<52}{'2025 $':>9}{'PF':>6}{'2026 $':>9}{'PF':>6}{'comb $':>9}{'25DD':>8}")
        print("  " + "-" * 96)
        for r in ranked[:k]:
            print(f"  {r['label']:<52}{r['y2025']['total']:>9.0f}{r['y2025']['pf']:>6.2f}"
                  f"{r['y2026']['total']:>9.0f}{r['y2026']['pf']:>6.2f}"
                  f"{r['combined']:>9.0f}{r['y2025']['mdd']:>8.0f}")

    # ---- 2026 is the priority objective (more representative) ----
    pos26 = [r for r in rows if r["y2026"]["total"] > 0]
    show(f"*** PROFITABLE IN 2026 ({len(pos26)} configs) — ranked by 2026 P&L ***",
         sorted(pos26, key=lambda r: -r["y2026"]["total"]), k=25)
    show("TOP BY 2026 PROFIT FACTOR (min 40 trades in 2026)",
         sorted([r for r in rows if r["y2026"]["trades"] >= 40], key=lambda r: -r["y2026"]["pf"]))
    robust = [r for r in rows if r["y2025"]["total"] > 0 and r["y2026"]["total"] > 0]
    show(f"ROBUST: POSITIVE IN BOTH YEARS ({len(robust)} configs) — by 2026 then combined",
         sorted(robust, key=lambda r: (-r["y2026"]["total"], -r["combined"])))
    show("TOP BY 2026 TOTAL P&L (all configs, incl. negative)",
         sorted(rows, key=lambda r: -r["y2026"]["total"]))
    show("(reference) TOP BY 2025 TOTAL P&L (train)", sorted(rows, key=lambda r: -r["y2025"]["total"]))

    # ---- marginal effect of each dimension (avg combined PF & avg 2025/2026 $) ----
    def marginals(key, getter):
        groups = {}
        for r in rows:
            g = getter(r["cfg"])
            groups.setdefault(g, []).append(r)
        print("\n  " + "-" * 70)
        print(f"  MARGINAL: {key:<14}{'n':>5}{'avg25$':>9}{'avg26$':>9}{'avg25PF':>9}{'avg26PF':>9}")
        for g in sorted(groups, key=lambda x: (str(type(x)), x)):
            rs = groups[g]
            a25 = sum(r["y2025"]["total"] for r in rs) / len(rs)
            a26 = sum(r["y2026"]["total"] for r in rs) / len(rs)
            p25 = sum(r["y2025"]["pf"] for r in rs) / len(rs)
            p26 = sum(r["y2026"]["pf"] for r in rs) / len(rs)
            print(f"    {str(g):<12}{len(rs):>5}{a25:>9.0f}{a26:>9.0f}{p25:>9.3f}{p26:>9.3f}")

    print("\n" + "=" * 100)
    print("  MARGINAL EFFECTS (averaged over all other dimensions)")
    print("=" * 100)
    marginals("trades/day", lambda c: ("inf" if c["tpd"] >= 99 else c["tpd"]))
    marginals("contracts", lambda c: c["contracts"])
    marginals("tranches", lambda c: c["tranches"])
    marginals("target mode", lambda c: c["mode"])
    marginals("stop loss %", lambda c: c["sl"])

    # ---- isolated target sweep: single tranche, 1 ctr, tpd=1, vary target & SL ----
    print("\n" + "=" * 100)
    print("  TARGET SWEEP — single tranche, 1 contract, tpd=1 (isolates target size)")
    print("=" * 100)
    print(f"  {'target%':>8} {'SL':>4} | {'2025 $':>9}{'25 PF':>7}{'25 WR':>7} | {'2026 $':>9}{'26 PF':>7}{'26 WR':>7}")
    print("  " + "-" * 78)
    sweep = [r for r in rows if r["cfg"]["tranches"] == 1 and r["cfg"]["contracts"] == 1
             and r["cfg"]["tpd"] == 1 and r["cfg"]["mode"] == "pct" and r["cfg"]["sl"] in (50, 75, 100)]
    for r in sorted(sweep, key=lambda r: (r["cfg"]["sl"], r["cfg"]["targets"][0])):
        tg = r["cfg"]["targets"][0]
        tgs = "ride/EOD" if tg >= 9999 else str(tg)
        print(f"  {tgs:>8} {r['cfg']['sl']:>4} | {r['y2025']['total']:>9.0f}{r['y2025']['pf']:>7.2f}"
              f"{r['y2025']['win_rate']:>7.1f} | {r['y2026']['total']:>9.0f}{r['y2026']['pf']:>7.2f}"
              f"{r['y2026']['win_rate']:>7.1f}")

    out = os.path.join(_HERE, "grid_search_results.json")
    with open(out, "w") as f:
        json.dump({"n_configs": len(rows),
                   "n_profitable_2026": len(pos26),
                   "by_2026": sorted(rows, key=lambda r: -r["y2026"]["total"])[:60],
                   "profitable_in_2026": sorted(pos26, key=lambda r: -r["y2026"]["total"]),
                   "robust_both_positive": sorted(robust, key=lambda r: -r["y2026"]["total"])},
                  f, indent=2)
    print(f"\n  Saved -> {out}")


if __name__ == "__main__":
    main()
