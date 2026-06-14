"""
backtest_vwap_filter.py — Historical backtest of the ORB + VWAP Filter 0DTE bot.

Faithfully replays the live bot's rules (orb_vwap_filter.py) on real data:
  - QQQ 1-minute underlying bars   -> opening range, session VWAP, breakouts
  - QQQ 0DTE option 1-minute OHLC  -> real option-premium path for exits

NO LOOK-AHEAD. The live bot is a 1-minute *poller*: every minute it reads the
last completed bar and the current option marketPrice, then submits MARKET orders.
So every decision here is made on a COMPLETED bar's CLOSE, and the resulting fill
happens at the NEXT bar's OPEN — never on the same bar that produced the signal,
and never using a bar's intrabar high to "capture" a price the poller never saw.
The only intrabar fill is the -50% stop, because the bot places a real resting
StopOrder at entry (it triggers when the market trades through the stop level).

Strategy (matches orb_vwap_filter.py code, not just the README):
  - Opening range: 09:30-09:44 ET (15 one-minute bars); session VWAP from 09:30
  - Entry (post-09:45), no position, VWAP filter STRICT, evaluated on bar close:
        close > range_high AND close > VWAP  -> buy CALLs
        close < range_low  AND close < VWAP  -> buy PUTs
    -> entry fills at the NEXT bar's option OPEN.
  - Strike: 3rd listed strike strictly beyond the breakout price ($1 spacing)
        calls -> floor(price)+3 ;  puts -> ceil(price)-3
  - Size 3 contracts. Profit ladder is gated by REMAINING qty (as coded):
        qty==3 & premium>=+150%  -> sell 1
        qty==2 & premium>=+100%  -> sell 1
        qty==1 & premium>= +50%  -> sell 1
    i.e. the FIRST contract only comes off at +150%; one contract per poll;
    sells fill at the NEXT bar's option OPEN.
  - Resting hybrid stop: sell ALL remaining at -50% of entry premium (intrabar).
  - Opposite breakout (polled on close): sell ALL remaining at next bar open.
  - EOD: force-flat at 13:00 ET (at the 13:00 observed close).
  - Daily risk: once realized P&L <= -$1000, stop opening NEW trades that day.
  - Re-entry: after any flat, underlying must trade back inside the OR before a
    new entry; after a STOP, also wait REENTRY_WAIT_MINUTES (15).

No commissions or slippage are modelled (results are optimistic on that axis).

Usage:  python3 backtest_vwap_filter.py 2025 2026
"""

import csv
import glob
import json
import math
import os
import sys

# ---- strategy constants (mirror orb_vwap_filter.py) ----
OR_START, OR_END_EXCL = "09:30", "09:45"   # OR = bars 09:30..09:44
ENTRY_START          = "09:45"
TRADING_END          = "13:00"             # force-flat
CONTRACTS            = 3
OTM_STRIKES_OUT      = 3
STOP_LOSS_PCT        = 50
PROFIT_LEVELS        = [50, 100, 150]      # one contract each
MAX_DAILY_RISK       = 1000
REENTRY_WAIT_MIN     = 15
MIN_OR_RANGE         = 0.10                 # filter data-gap/holiday sessions
STRIKE_SPACING       = 1                    # QQQ 0DTE strikes are $1 apart

_HERE = os.path.dirname(os.path.abspath(__file__))
REPO_CSV = os.path.join(_HERE, "..", "..", "qqq_1m_2018_2026.csv.xz")  # has volume, RTH

UNDERLYING_DIRS = {
    "2025": os.path.expanduser("~/backups/QQQ/2025/1m"),
    "2026": os.path.expanduser("~/backups/QQQ/2026/1m"),
}
OPTIONS_DIRS = {
    "2025": os.path.expanduser("~/backups/QQQ/2025/Options-OHLC/thetadata-2025"),
    "2026": os.path.expanduser("~/backups/QQQ/2026/Options-OHLC/thetadata-2026"),
}


# ---------------------------------------------------------------- underlying
def load_underlying(year):
    """date -> list of (hhmm, o,h,l,c,v) RTH bars 09:30..16:00, sorted.

    Volume is required (VWAP filter depends on it). Primary source is the repo
    CSV (volume + RTH); monthly backup files supplement later months. Rows
    without a volume column are skipped.
    """
    import lzma
    days = {}
    seen = set()

    def add(d, t, o, h, l, c, v):
        if (d, t) in seen:
            return
        if t < "09:30" or t > "16:00":
            return
        seen.add((d, t))
        days.setdefault(d, []).append((t, o, h, l, c, v))

    # primary: repo CSV (has volume; covers 2026 through ~Mar 13)
    if os.path.exists(REPO_CSV):
        with lzma.open(REPO_CSV, "rt") as f:
            for row in csv.DictReader(f):
                if not row["date"].startswith(year) or not row.get("volume"):
                    continue
                add(row["date"], row["time"], float(row["open"]), float(row["high"]),
                    float(row["low"]), float(row["close"]), float(row["volume"]))

    # supplement: monthly backup files that include a volume column
    for path in sorted(glob.glob(os.path.join(UNDERLYING_DIRS[year], f"qqq_1m_{year}*.csv"))):
        with open(path) as f:
            r = csv.DictReader(f)
            if "volume" not in (r.fieldnames or []):
                continue
            for row in r:
                if not row["date"].startswith(year) or not row.get("volume"):
                    continue
                add(row["date"], row["time"], float(row["open"]), float(row["high"]),
                    float(row["low"]), float(row["close"]), float(row["volume"]))

    for d in days:
        days[d].sort()
    return days


def to_min(hhmm):
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


# ---------------------------------------------------------------- options
def load_option_day(year, date_str):
    """right('call'/'put') -> strike(int) -> hhmm -> (open,high,low,close)."""
    import pandas as pd
    path = os.path.join(OPTIONS_DIRS[year], f"qqq-options-1m-{date_str.replace('-', '')}.parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path, columns=["open", "high", "low", "close", "timestamp", "strike", "right"])
    df = df[df["right"].isin(["C", "P"])]          # one encoding only (widest coverage)
    out = {"call": {}, "put": {}}
    for o, h, l, c, ts, strike, right in zip(
        df["open"], df["high"], df["low"], df["close"], df["timestamp"], df["strike"], df["right"]
    ):
        r = "call" if right == "C" else "put"
        hhmm = str(ts)[11:16]
        out[r].setdefault(int(strike), {})[hhmm] = (float(o), float(h), float(l), float(c))
    return out


# ---------------------------------------------------------------- per-day sim
def opt_open_at(opt, right, strike, hhmm):
    """Option OPEN at a given minute (the price a market order placed on the
    prior bar's close would realistically fill at). None if unavailable."""
    b = opt[right].get(strike, {}).get(hhmm)
    if b is None:
        return None
    o = b[0]
    return o if o > 0 else None


def simulate_day(date_str, bars, opt):
    """Return list of trade dicts for the day. No look-ahead: signals on a
    completed bar's CLOSE, fills on the NEXT bar's OPEN (stop is intrabar)."""
    if len(bars) < 16:
        return []
    or_bars = [b for b in bars if OR_START <= b[0] < OR_END_EXCL]
    if len(or_bars) < 10:
        return []
    range_high = max(b[2] for b in or_bars)
    range_low  = min(b[3] for b in or_bars)
    if range_high - range_low < MIN_OR_RANGE:
        return []

    # cumulative session VWAP per minute
    vwap = {}
    cvp = cv = 0.0
    for t, o, h, l, c, v in bars:
        tp = (h + l + c) / 3.0
        cvp += tp * v
        cv  += v
        vwap[t] = (cvp / cv) if cv > 0 else None

    trades = []
    pos = None
    day_pnl = 0.0
    need_reset = False         # must return inside OR before new entry
    last_stop_min = None
    risk_blocked = False       # daily loss cap hit -> no new entries

    n = len(bars)
    for i in range(n):
        t, o, h, l, c, v = bars[i]
        if t < ENTRY_START:
            continue
        cur_min = to_min(t)
        nxt_t = bars[i + 1][0] if i + 1 < n else None   # next bar -> fill time
        force_eod = (t >= TRADING_END)

        # ---- reset gate: underlying back inside OR re-arms entries ----
        if need_reset and pos is None and range_low <= c <= range_high:
            need_reset = False

        # ---- manage open position (this bar = the just-completed poll bar) ----
        if pos is not None:
            ent = pos["opt_entry"]
            stop_px = pos["stop_px"]
            obar = opt[pos["right"]].get(pos["strike"], {}).get(t)
            fill_open = opt_open_at(opt, pos["right"], pos["strike"], nxt_t) if nxt_t else None
            if obar is not None:
                oo, oh, ol, oc = obar
                pos["last_close"] = oc
                # 1) resting STOP (real StopOrder) -> intrabar low fill at stop price
                if pos["contracts"] > 0 and ol <= stop_px:
                    pos["realized"] += (stop_px - ent) * 100.0 * pos["contracts"]
                    pos["exits"].append(("stop", t, round(stop_px, 2), pos["contracts"]))
                    pos["contracts"] = 0
                # 2) opposite breakout (polled on close -> fill next open)
                if pos["contracts"] > 0:
                    opp = (pos["right"] == "call" and c < range_low) or \
                          (pos["right"] == "put"  and c > range_high)
                    if opp:
                        px = fill_open if fill_open else oc
                        pos["realized"] += (px - ent) * 100.0 * pos["contracts"]
                        pos["exits"].append(("opp_breakout", t, round(px, 2), pos["contracts"]))
                        pos["contracts"] = 0
                # 3) profit ladder, gated by REMAINING qty, 1 contract per poll,
                #    filled at next bar open (no intrabar high capture)
                if pos["contracts"] > 0 and not force_eod:
                    q = pos["contracts"]
                    prem_pct = (oc - ent) / ent * 100.0
                    need = {3: 150, 2: 100, 1: 50}[q]
                    if prem_pct >= need:
                        px = fill_open if fill_open else oc
                        pos["realized"] += (px - ent) * 100.0 * 1
                        pos["exits"].append((f"tp_q{q}", t, round(px, 2), 1))
                        pos["contracts"] -= 1
                # 4) EOD force flat at the 13:00 observed close
                if force_eod and pos["contracts"] > 0:
                    pos["realized"] += (oc - ent) * 100.0 * pos["contracts"]
                    pos["exits"].append(("eod", t, round(oc, 2), pos["contracts"]))
                    pos["contracts"] = 0
            elif force_eod and pos["contracts"] > 0:
                # no option bar at EOD -> close at last observed option close
                px = pos.get("last_close", ent)
                pos["realized"] += (px - ent) * 100.0 * pos["contracts"]
                pos["exits"].append(("eod_nobar", t, round(px, 2), pos["contracts"]))
                pos["contracts"] = 0

            if pos["contracts"] == 0:
                stopped = any(e[0] == "stop" for e in pos["exits"])
                day_pnl += pos["realized"]
                pos["pnl"] = round(pos["realized"], 2)
                trades.append(pos)
                if stopped:
                    last_stop_min = cur_min
                need_reset = True
                pos = None

        # ---- daily risk cap: block NEW entries (existing pos still managed) ----
        if day_pnl <= -MAX_DAILY_RISK:
            risk_blocked = True
        if force_eod or risk_blocked or nxt_t is None:
            continue

        # ---- new entry: signal on close[t], FILL at next bar option open ----
        if pos is None and not need_reset:
            if last_stop_min is not None and (cur_min - last_stop_min) < REENTRY_WAIT_MIN:
                continue
            vw = vwap[t]
            if vw is None:
                continue
            direction = None
            if c > range_high and c > vw:
                direction = "call"
            elif c < range_low and c < vw:
                direction = "put"
            if direction is None:
                continue
            # 3rd listed strike strictly beyond the breakout price ($1 spacing)
            strike = (math.floor(c) + OTM_STRIKES_OUT) if direction == "call" \
                else (math.ceil(c) - OTM_STRIKES_OUT)
            opt_entry = opt_open_at(opt, direction, strike, nxt_t)  # fill next bar open
            if not opt_entry:
                continue
            pos = {
                "date": date_str, "direction": direction, "strike": strike,
                "right": direction, "signal_time": t, "entry_time": nxt_t,
                "entry_under": round(c, 2), "vwap": round(vw, 2),
                "or_high": round(range_high, 2), "or_low": round(range_low, 2),
                "opt_entry": round(opt_entry, 2),
                "stop_px": round(opt_entry * (1 - STOP_LOSS_PCT / 100.0), 2),
                "contracts": CONTRACTS, "exits": [], "realized": 0.0,
                "last_close": opt_entry,
            }
    return trades


# ---------------------------------------------------------------- stats
def stats(pnls):
    if not pnls:
        return {}
    n = len(pnls)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw, gl = sum(wins), abs(sum(losses))
    eq = []
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for p in pnls:
        cum += p
        eq.append(cum)
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    mean = cum / n
    var = sum((p - mean) ** 2 for p in pnls) / n
    std = math.sqrt(var)
    return {
        "trades": n,
        "win_rate": round(100 * len(wins) / n, 1),
        "total_pnl": round(cum, 2),
        "avg_trade": round(mean, 2),
        "profit_factor": round(gw / gl, 2) if gl > 0 else float("inf"),
        "max_drawdown": round(mdd, 2),
        "best": round(max(pnls), 2),
        "worst": round(min(pnls), 2),
        "sharpe_per_trade": round(mean / std, 2) if std > 0 else float("inf"),
    }


def main():
    years = sys.argv[1:] or ["2025", "2026"]
    all_out = {}
    for year in years:
        under = load_underlying(year)
        dates = sorted(under)
        year_trades = []
        days_traded = set()
        no_opt = 0
        for d in dates:
            opt = load_option_day(year, d)
            if opt is None:
                no_opt += 1
                continue
            t = simulate_day(d, under[d], opt)
            if t:
                days_traded.add(d)
                year_trades.extend(t)
        pnls = [tr["pnl"] for tr in year_trades]
        s = stats(pnls)
        # monthly breakdown
        months = {}
        for tr in year_trades:
            m = tr["date"][:7]
            months.setdefault(m, []).append(tr["pnl"])
        monthly = {m: {"trades": len(v), "pnl": round(sum(v), 2)} for m, v in sorted(months.items())}
        all_out[year] = {
            "sessions_available": len(dates),
            "sessions_missing_options": no_opt,
            "days_with_trades": len(days_traded),
            "date_range": f"{dates[0]} .. {dates[-1]}" if dates else "n/a",
            "stats": s,
            "monthly": monthly,
            "trades": [
                {k: (list(v) if isinstance(v, set) else v) for k, v in tr.items()}
                for tr in year_trades
            ],
        }
        # ---- print ----
        print("=" * 64)
        print(f"  ORB + VWAP Filter — 0DTE QQQ — {year}")
        print(f"  ({all_out[year]['date_range']}, {len(days_traded)} trading days, "
              f"{no_opt} sessions missing option data)")
        print("=" * 64)
        if s:
            for k in ["trades", "win_rate", "total_pnl", "avg_trade", "profit_factor",
                      "max_drawdown", "best", "worst", "sharpe_per_trade"]:
                print(f"    {k:<18} {s[k]}")
            print("    monthly:")
            for m, mv in monthly.items():
                print(f"      {m}: {mv['trades']:>3} trades   ${mv['pnl']:>9.2f}")
        else:
            print("    no trades")
        print()

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backtest_results_2025_2026.json")
    with open(out_path, "w") as f:
        json.dump(all_out, f, indent=2, default=lambda o: list(o) if isinstance(o, set) else o)
    print(f"  Saved -> {out_path}")


if __name__ == "__main__":
    main()
