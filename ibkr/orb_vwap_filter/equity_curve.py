"""
equity_curve.py — Portfolio (equity) curve for the recommended strategy at 10 contracts.

Strategy: ORB +30% scalp, OTM-1, -75% stop, OR>=$1, 3 trades/day.
  - CALLS  : upside opening-range breakout, but SKIPPED when VIX opens > 25 (panic
             regime: calls lose ~$69/day at 36% win rate there)
  - PUTS   : taken only on days where VIX OPENED ABOVE its pivot AND VIX open >= 18
             (a genuine risk-off open, known at 9:30 -> no look-ahead). The >=18 floor
             removes low-vol "false fear" days where shorts just bleed; per-VIX-regime
             analysis showed puts lose below ~16-18 and earn their keep at higher vol.
             VIX pivot = (prevH+prevL+prevC)/3 from the prior session, read from
             vix_daily.csv.
Sized at 10 contracts; the $1,000/day loss cap is scaled with size to $1,000 x contracts
(keeping it at $1,000 would throttle a 10-lot to ~1 trade/day). Net of $0.65/contract/side.

Produces:
  - printed per-year + combined stats
  - equity_curve.png  (continuous cumulative net P&L across 2025 -> 2026)

Usage:  python3 equity_curve.py
"""
import csv
import os
from datetime import datetime
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

from grid_search import load_all_underlying
from grid_search_nocpr import build_ctx, to_min, COMMISSION

_HERE = os.path.dirname(os.path.abspath(__file__))
CONTRACTS = 10
DAILY_CAP = 1000 * CONTRACTS
VIX_MIN_OPEN = 18.0          # put floor:  only take gated puts when VIX open >= this
VIX_MAX_CALLS = 25.0         # call ceiling: skip CALLS when VIX open > this (panic regime)
CFG = {"tpd": 3, "split": [CONTRACTS], "tgts": [30], "sl": 75, "otm": 1,
       "or_floor": 1.0, "dir": "both"}


def _vix_rows(path=os.path.join(_HERE, "vix_daily.csv")):
    rows = [(r["date"], float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]))
            for r in csv.DictReader(open(path))]
    rows.sort()
    return rows


def vix_put_days(vix_min=VIX_MIN_OPEN):
    """Dates where VIX opened ABOVE its (prior-day) CPR pivot AND VIX open >= vix_min."""
    rows = _vix_rows()
    out = set()
    for i in range(1, len(rows)):
        d, o, h, l, c = rows[i]
        ph, pl, pc = rows[i - 1][2], rows[i - 1][3], rows[i - 1][4]
        if o > (ph + pl + pc) / 3.0 and o >= vix_min:
            out.add(d)
    return out


def vix_call_skip_days(vix_max=VIX_MAX_CALLS):
    """Dates where VIX opened above vix_max -> skip CALLS (panic regime, -69$/day, 36% WR)."""
    return {d for d, o, h, l, c in _vix_rows() if o > vix_max}


VIX_PUT_DAYS = vix_put_days()
VIX_CALL_SKIP_DAYS = vix_call_skip_days()


def sim_eq(ctx, c, cap):
    """Same engine as grid_search_nocpr.sim, with a configurable daily cap.
    Returns list of per-trade NET pnls (chronological)."""
    times, close, vwap = ctx["times"], ctx["close"], ctx["vwap"]
    rhigh, rlow, orr = ctx["rhigh"], ctx["rlow"], ctx["orr"]
    n = len(times); eod = ctx["eod"]
    if orr < c["or_floor"]:
        return []
    tpd, split, tgts, sl, otm, dirf = c["tpd"], c["split"], c["tgts"], c["sl"] / 100.0, c["otm"], c["dir"]
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
                    pos["g"] += (ent * (1 - sl) - ent) * 100 * rem; rem = 0
                if rem > 0:
                    opp = (pos["d"] == "call" and px < rlow) or (pos["d"] == "put" and px > rhigh)
                    if opp:
                        p = fo if fo else oc; pos["g"] += (p - ent) * 100 * rem; rem = 0
                if rem > 0 and not feod and pos["ti"] < len(pos["tl"]):
                    q, trig = pos["tl"][pos["ti"]]
                    if (oc - ent) / ent * 100 >= trig:
                        p = fo if fo else oc; s = min(q, rem)
                        pos["g"] += (p - ent) * 100 * s; rem -= s; pos["ti"] += 1
                if feod and rem > 0:
                    pos["g"] += (oc - ent) * 100 * rem; rem = 0
                pos["rem"] = rem; pos["last"] = oc
            elif feod and pos["rem"] > 0:
                pos["g"] += (pos["last"] - ent) * 100 * pos["rem"]; pos["rem"] = 0
            if pos["rem"] == 0:
                net = pos["g"] - COMMISSION * 2 * pos["n0"]
                day += net; pnls.append(net)
                if pos["stopped"]:
                    laststop = cm
                reset = True; pos = None
        if day <= -cap:
            blocked = True
        if feod or blocked or nxt is None:
            continue
        if pos is None and not reset and nt < tpd:
            if laststop is not None and (cm - laststop) < 15:
                continue
            vw = vwap[i]
            if vw is None:
                continue
            if (px > rhigh and px > vw and dirf in ("both", "call")
                    and ctx["date"] not in VIX_CALL_SKIP_DAYS):     # no calls when VIX open > 25
                d_ = "call"; strike = round(px) + otm; arr = ctx["call"].get(strike)
            elif (px < rlow and px < vw and dirf in ("both", "put")
                  and ctx["date"] in VIX_PUT_DAYS):                # puts only on VIX open>pivot & >=18
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


def main():
    days = load_all_underlying()
    series = []          # (datetime, cumulative_net)
    cum = 0.0
    year_stats = {}
    bounds = {}
    for year in ("2025", "2026"):
        ctxs = build_ctx(year, days)
        y_start = cum
        trades = 0; daypos = 0; daytot = 0
        for ctx in ctxs:
            ps = sim_eq(ctx, CFG, DAILY_CAP)
            if not ps:
                continue
            daytot += 1
            if sum(ps) > 0:
                daypos += 1
            d = datetime.strptime(ctx["date"], "%Y-%m-%d")
            for p in ps:
                cum += p; trades += 1
                series.append((d, cum))
        year_stats[year] = {"net": round(cum - y_start), "trades": trades,
                            "days": daytot, "daywr": round(100 * daypos / daytot) if daytot else 0}
        bounds[year] = (series[0][0] if series else None, series[-1][0] if series else None, cum)

    for y in ("2025", "2026"):
        s = year_stats[y]
        print(f"  {y}: net ${s['net']:>7}  ({s['trades']} trades, {s['days']} days, dayWR {s['daywr']}%)")
    print(f"  COMBINED net: ${round(cum)}   (10 contracts, daily cap ${DAILY_CAP})")

    # ---- plot ----
    xs = [d for d, _ in series]; ys = [v for _, v in series]
    fig, ax = plt.subplots(figsize=(10, 4.8))
    ax.plot(xs, ys, lw=1.6, color="#1565c0")
    ax.fill_between(xs, 0, ys, where=[v >= 0 for v in ys], color="#1565c0", alpha=0.10)
    ax.axhline(0, color="#888", lw=0.8)
    yb = datetime(2026, 1, 1)
    ax.axvline(yb, color="#c62828", ls="--", lw=1, alpha=0.7)
    ax.text(yb, ax.get_ylim()[1] * 0.92, " 2026", color="#c62828", fontsize=9)
    ax.set_title("ORB +30% scalp — calls[VIX open≤25] + puts[VIX open>pivot & ≥18] — 10 contracts",
                 fontsize=10)
    ax.set_ylabel("Cumulative net P&L ($)")
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v:,.0f}"))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    fig.autofmt_xdate()
    ax.grid(True, alpha=0.25)
    ax.annotate(f"${round(cum):,}", xy=(xs[-1], ys[-1]), xytext=(-44, 6),
                textcoords="offset points", fontsize=10, fontweight="bold", color="#1565c0")
    fig.tight_layout()
    out = os.path.join(_HERE, "equity_curve.png")
    fig.savefig(out, dpi=130)
    print(f"  saved -> {out}")


if __name__ == "__main__":
    main()
