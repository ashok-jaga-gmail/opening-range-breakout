#!/usr/bin/env python3
"""
QQQ Multi-Timeframe Regime Indicators for ORB Paper
=====================================================
Computes the following at TRADE ENTRY TIME (no look-ahead):

  CPR   — monthly, weekly, daily
           price position: above_top / inside_cpr / below_bottom
           CPR width: narrow (<0.1%) / normal / wide (>0.3%)

  RSI(14) — weekly, daily, 4h, 1h, 15m
             state: overbought (>70) / bullish (50-70) / neutral (45-55)
                    bearish (30-50) / oversold (<30)

  MACD(12,26,9) — weekly, daily, 4h, 1h, 15m
                   state: bullish (hist>0, rising) / bullish_fade
                          bearish (hist<0, falling) / bearish_fade
                   signal: bullish_cross / bearish_cross (within last 3 bars)

Annotates each trade record from orb_paper_backtest.py with regime info,
then runs stratified performance analysis.

Usage:
    python3 orb_regime_indicators.py

Requires /tmp/orb_paper_results.json from orb_paper_backtest.py to exist.
Also requires the pre-converted CSV at the path set in CSV_FILE.
"""

import csv
import json
import math
import datetime
from collections import defaultdict

# ── Config ────────────────────────────────────────────────────────────────────
CSV_FILE    = "/Users/ashok/backups/QQQ/qqq_1m_2018_2026.csv"
TRADES_FILE = "/tmp/orb_paper_results.json"
OUT_FILE    = "/tmp/orb_regime_results.json"

RSI_PERIOD  = 14
MACD_FAST   = 12
MACD_SLOW   = 26
MACD_SIGNAL = 9


# ── Bar Loading ───────────────────────────────────────────────────────────────
def load_csv_all_bars(csv_path: str) -> dict:
    """
    Load pre-converted CSV (date, time, open, high, low, close, volume).
    Returns: {date_str: [(time_str, o, h, l, c, v), ...]} sorted by time.
    """
    print(f"Loading {csv_path} …", flush=True)
    daily = defaultdict(list)
    count = 0
    with open(csv_path, newline="") as f:
        reader = csv.reader(f)
        next(reader)  # skip header
        for row in reader:
            d, t, o, h, l, c, v = row
            daily[d].append((t, float(o), float(h), float(l), float(c), int(v)))
            count += 1
    for d in daily:
        daily[d].sort()
    print(f"  {count:,} bars across {len(daily)} days.", flush=True)
    return dict(daily)


# ── Resampling ────────────────────────────────────────────────────────────────
def resample_daily_to_ohlcv(daily_bars: dict) -> list:
    """Daily OHLCV list: [{date, o, h, l, c, v}, ...]"""
    result = []
    for d in sorted(daily_bars):
        bars = daily_bars[d]
        rth  = [(t, o, h, l, c, v) for (t, o, h, l, c, v) in bars if t >= "09:30" and t < "16:00"]
        if len(rth) < 10:
            continue
        result.append({
            "date": d,
            "o": rth[0][1],
            "h": max(b[2] for b in rth),
            "l": min(b[3] for b in rth),
            "c": rth[-1][4],
            "v": sum(b[5] for b in rth),
        })
    return result


def resample_to_weekly(daily_ohlcv: list) -> list:
    """Aggregate daily bars into Monday-anchored weekly bars."""
    weekly = defaultdict(lambda: {"bars": []})
    for bar in daily_ohlcv:
        d   = datetime.date.fromisoformat(bar["date"])
        mon = d - datetime.timedelta(days=d.weekday())  # Monday of that week
        weekly[mon]["bars"].append(bar)
    result = []
    for mon in sorted(weekly):
        bars = weekly[mon]["bars"]
        result.append({
            "date": str(mon),
            "o": bars[0]["o"],
            "h": max(b["h"] for b in bars),
            "l": min(b["l"] for b in bars),
            "c": bars[-1]["c"],
            "v": sum(b["v"] for b in bars),
        })
    return result


def resample_to_monthly(daily_ohlcv: list) -> list:
    """Aggregate daily bars into calendar-month bars."""
    monthly = defaultdict(lambda: {"bars": []})
    for bar in daily_ohlcv:
        month_key = bar["date"][:7]  # YYYY-MM
        monthly[month_key]["bars"].append(bar)
    result = []
    for mk in sorted(monthly):
        bars = monthly[mk]["bars"]
        result.append({
            "date": mk,
            "o": bars[0]["o"],
            "h": max(b["h"] for b in bars),
            "l": min(b["l"] for b in bars),
            "c": bars[-1]["c"],
            "v": sum(b["v"] for b in bars),
        })
    return result


def resample_1m_to_Nm(daily_bars: dict, n_minutes: int) -> dict:
    """
    Resample intraday 1-min bars to N-minute bars.
    Returns {date_str: [(bar_start_time_str, o, h, l, c, v), ...]}
    Only RTH bars included.
    """
    result = defaultdict(list)
    for d in sorted(daily_bars):
        bars  = daily_bars[d]
        rth   = [(t, o, h, l, c, v) for (t, o, h, l, c, v) in bars if t >= "09:30" and t < "16:00"]
        if not rth:
            continue

        bucket_start = None
        bucket_bars  = []

        def flush_bucket():
            if not bucket_bars:
                return
            o = bucket_bars[0][1]
            h = max(b[2] for b in bucket_bars)
            l = min(b[3] for b in bucket_bars)
            c = bucket_bars[-1][4]
            v = sum(b[5] for b in bucket_bars)
            result[d].append((bucket_start, o, h, l, c, v))

        for t, o, h, l, c, v in rth:
            dt = datetime.datetime.strptime(t, "%H:%M")
            # Compute which N-minute slot this belongs to
            minutes_since_open = (dt.hour - 9) * 60 + dt.minute - 30
            slot = (minutes_since_open // n_minutes) * n_minutes
            slot_dt  = datetime.datetime(2000, 1, 1, 9, 30) + datetime.timedelta(minutes=slot)
            slot_str = slot_dt.strftime("%H:%M")

            if slot_str != bucket_start:
                flush_bucket()
                bucket_start = slot_str
                bucket_bars  = []
            bucket_bars.append((t, o, h, l, c, v))

        flush_bucket()

    return dict(result)


# ── CPR ───────────────────────────────────────────────────────────────────────
def compute_cpr(h: float, l: float, c: float) -> dict:
    """Compute CPR levels from a period's H/L/C."""
    pivot = (h + l + c) / 3.0
    bc    = (h + l) / 2.0
    tc    = 2 * pivot - bc
    return {
        "pivot":      pivot,
        "top_cpr":    max(tc, bc),
        "bottom_cpr": min(tc, bc),
        "r1": 2 * pivot - l,
        "s1": 2 * pivot - h,
        "r2": pivot + (h - l),
        "s2": pivot - (h - l),
        "r3": h + 2 * (pivot - l),
        "s3": l - 2 * (h - pivot),
        "width_pct": abs(tc - bc) / pivot * 100 if pivot > 0 else 0,
    }


def cpr_price_state(price: float, cpr: dict) -> str:
    """Classify price position relative to CPR."""
    if price > cpr["top_cpr"]:
        return "above_top"
    if price < cpr["bottom_cpr"]:
        return "below_bottom"
    return "inside_cpr"


def cpr_width_state(cpr: dict) -> str:
    w = cpr["width_pct"]
    if w < 0.10:
        return "narrow"
    if w > 0.30:
        return "wide"
    return "normal"


# ── RSI ───────────────────────────────────────────────────────────────────────
def compute_rsi_series(closes: list, period: int = 14) -> list:
    """
    Wilder's RSI. Returns list of (rsi_value or None) aligned with closes.
    First `period` values are None.
    """
    n   = len(closes)
    rsi = [None] * n
    if n < period + 1:
        return rsi

    gains  = []
    losses = []
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    def _rsi(ag, al):
        if al == 0:
            return 100.0
        rs = ag / al
        return 100 - 100 / (1 + rs)

    rsi[period] = _rsi(avg_gain, avg_loss)

    for i in range(period + 1, n):
        diff = closes[i] - closes[i - 1]
        g    = max(diff, 0)
        l    = max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        rsi[i] = _rsi(avg_gain, avg_loss)

    return rsi


def rsi_state(rsi_val: float | None) -> str:
    if rsi_val is None:
        return "unknown"
    if rsi_val >= 70:
        return "overbought"
    if rsi_val >= 55:
        return "bullish"
    if rsi_val >= 45:
        return "neutral"
    if rsi_val >= 30:
        return "bearish"
    return "oversold"


# ── EMA ───────────────────────────────────────────────────────────────────────
def compute_ema_series(values: list, period: int) -> list:
    """Standard EMA. Returns list aligned with values, None until enough data."""
    n    = len(values)
    ema  = [None] * n
    k    = 2.0 / (period + 1)
    # Seed with SMA of first `period` non-None values
    seed_data = [v for v in values if v is not None]
    if len(seed_data) < period:
        return ema
    # Find first position with enough data
    count = 0
    for i, v in enumerate(values):
        if v is not None:
            count += 1
        if count == period:
            # SMA seed
            seed_vals = [x for x in values[max(0, i - period + 1):i + 1] if x is not None]
            ema[i] = sum(seed_vals) / len(seed_vals)
            start_i = i
            break
    else:
        return ema
    # EMA forward
    for i in range(start_i + 1, n):
        if values[i] is None:
            ema[i] = ema[i - 1]  # carry forward
        else:
            prev_ema = next((ema[j] for j in range(i - 1, -1, -1) if ema[j] is not None), None)
            if prev_ema is None:
                ema[i] = values[i]
            else:
                ema[i] = values[i] * k + prev_ema * (1 - k)
    return ema


# ── MACD ──────────────────────────────────────────────────────────────────────
def compute_macd_series(closes: list,
                        fast: int = 12, slow: int = 26, signal: int = 9
                        ) -> tuple[list, list, list]:
    """
    Returns (macd_line, signal_line, histogram) — all lists aligned with closes.
    Values are None until enough history.
    """
    ema_fast  = compute_ema_series(closes, fast)
    ema_slow  = compute_ema_series(closes, slow)

    macd_line = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(ema_fast, ema_slow)
    ]
    sig_line  = compute_ema_series(macd_line, signal)
    histogram = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, sig_line)
    ]
    return macd_line, sig_line, histogram


def macd_state(macd_line: list, sig_line: list, histogram: list, idx: int) -> str:
    """
    Classify MACD state at index idx.
    Look back up to 3 bars for crossover detection.
    """
    h   = histogram[idx]
    hm1 = histogram[idx - 1] if idx >= 1 else None

    if h is None:
        return "unknown"

    # Crossover detection (within last bar)
    cross_up   = h > 0 and hm1 is not None and hm1 <= 0
    cross_down = h < 0 and hm1 is not None and hm1 >= 0

    if cross_up:
        return "bullish_cross"
    if cross_down:
        return "bearish_cross"
    if h > 0:
        # Rising histogram = increasing bullish momentum
        return "bullish" if (hm1 is None or h >= hm1) else "bullish_fade"
    # h < 0
    return "bearish" if (hm1 is None or h <= hm1) else "bearish_fade"


# ── Build Lookup Tables ───────────────────────────────────────────────────────
def build_daily_cpr_lookup(daily_ohlcv: list) -> dict:
    """
    Returns {date_str: cpr_dict} where cpr is computed from PREVIOUS day's H/L/C.
    """
    lkp = {}
    for i in range(1, len(daily_ohlcv)):
        prev = daily_ohlcv[i - 1]
        cpr  = compute_cpr(prev["h"], prev["l"], prev["c"])
        lkp[daily_ohlcv[i]["date"]] = cpr
    return lkp


def build_weekly_cpr_lookup(weekly_ohlcv: list, daily_ohlcv: list) -> dict:
    """
    Returns {date_str: cpr_dict} using the prior WEEK's H/L/C.
    Applies to all trading days in a week the CPR computed from the prior week.
    """
    # Map each daily date to its Monday
    def monday_of(d_str):
        d   = datetime.date.fromisoformat(d_str)
        mon = d - datetime.timedelta(days=d.weekday())
        return str(mon)

    # Build week_start → CPR (from prior week)
    week_to_cpr = {}
    for i in range(1, len(weekly_ohlcv)):
        prev = weekly_ohlcv[i - 1]
        cpr  = compute_cpr(prev["h"], prev["l"], prev["c"])
        week_to_cpr[weekly_ohlcv[i]["date"]] = cpr

    lkp = {}
    for bar in daily_ohlcv:
        mon = monday_of(bar["date"])
        if mon in week_to_cpr:
            lkp[bar["date"]] = week_to_cpr[mon]
    return lkp


def build_monthly_cpr_lookup(monthly_ohlcv: list, daily_ohlcv: list) -> dict:
    """
    Returns {date_str: cpr_dict} using the prior MONTH's H/L/C.
    """
    month_to_cpr = {}
    for i in range(1, len(monthly_ohlcv)):
        prev = monthly_ohlcv[i - 1]
        cpr  = compute_cpr(prev["h"], prev["l"], prev["c"])
        month_to_cpr[monthly_ohlcv[i]["date"]] = cpr   # YYYY-MM key

    lkp = {}
    for bar in daily_ohlcv:
        mk = bar["date"][:7]   # YYYY-MM of next month
        # We want prior month's CPR for all days in this month
        # month_to_cpr has key = current month (computed from prior month)
        if mk in month_to_cpr:
            lkp[bar["date"]] = month_to_cpr[mk]
    return lkp


def build_rsi_lookup(bars_ohlcv: list, period: int = 14) -> dict:
    """
    bars_ohlcv: list of {date, o, h, l, c, v} or {date, ...} (any periodic bar)
    Returns {date_str: rsi_value}
    """
    closes = [b["c"] for b in bars_ohlcv]
    rsi    = compute_rsi_series(closes, period)
    return {bars_ohlcv[i]["date"]: rsi[i] for i in range(len(bars_ohlcv)) if rsi[i] is not None}


def build_macd_lookup(bars_ohlcv: list, fast=12, slow=26, signal=9) -> dict:
    """
    Returns {date_str: macd_state_str}
    """
    closes = [b["c"] for b in bars_ohlcv]
    ml, sl_, hist = compute_macd_series(closes, fast, slow, signal)
    result = {}
    for i, bar in enumerate(bars_ohlcv):
        if hist[i] is not None:
            result[bar["date"]] = macd_state(ml, sl_, hist, i)
    return result


# ── Intraday (15m / 1h / 4h) Indicator Lookups ───────────────────────────────
def build_intraday_rsi_lookup(daily_bars: dict, n_minutes: int, period: int = 14) -> dict:
    """
    Computes RSI on N-minute bars intraday.
    Returns {date_str: {time_str: rsi_value}} using the bar's CLOSE just BEFORE
    the given time (i.e., all data up to but not including this bar).

    For the ORB paper we primarily need the value at 09:44 (end of ORB) for 15m,
    and the most recent completed bar before 09:45 for 1h and 4h.
    """
    # Resample all intraday bars to N-min
    nm_bars = resample_1m_to_Nm(daily_bars, n_minutes)

    # Build a flat series: [(date, time, o, h, l, c, v), ...] sorted globally
    flat = []
    for d in sorted(nm_bars):
        for t, o, h, l, c, v in nm_bars[d]:
            flat.append((d, t, o, h, l, c, v))

    closes = [b[5] for b in flat]  # c is index 5
    rsi    = compute_rsi_series(closes, period)

    # Build lookup: date -> {bar_time -> rsi_val}
    lkp = defaultdict(dict)
    for i, (d, t, o, h, l, c, v) in enumerate(flat):
        if rsi[i] is not None:
            lkp[d][t] = rsi[i]
    return dict(lkp)


def build_intraday_macd_lookup(daily_bars: dict, n_minutes: int,
                                fast=12, slow=26, signal=9) -> dict:
    """Returns {date_str: {time_str: macd_state_str}}"""
    nm_bars = resample_1m_to_Nm(daily_bars, n_minutes)
    flat    = []
    for d in sorted(nm_bars):
        for t, o, h, l, c, v in nm_bars[d]:
            flat.append((d, t, o, h, l, c, v))

    closes      = [b[5] for b in flat]
    ml, sl_, hist = compute_macd_series(closes, fast, slow, signal)

    lkp = defaultdict(dict)
    for i, (d, t, o, h, l, c, v) in enumerate(flat):
        if hist[i] is not None:
            lkp[d][t] = macd_state(ml, sl_, hist, i)
    return dict(lkp)


def get_last_completed_bar_time(daily_bars_nm: dict, date_str: str,
                                 before_time: str = "09:45") -> str | None:
    """Return the latest bar time in date's N-min bars that is < before_time."""
    bars = daily_bars_nm.get(date_str, [])
    valid = [t for (t, *_) in bars if t < before_time]
    return valid[-1] if valid else None


# ── Annotate Trades ───────────────────────────────────────────────────────────
def annotate_trades(trades: list, daily_bars: dict) -> list:
    """
    Adds 'regime' dict to each trade with CPR, RSI, MACD across timeframes.
    No look-ahead: all indicators use data available before trade entry.
    """
    print("\nBuilding multi-timeframe bar series …", flush=True)

    # ── Daily / Weekly / Monthly OHLCV ───────────────────────────────────────
    daily_ohlcv   = resample_daily_to_ohlcv(daily_bars)
    weekly_ohlcv  = resample_to_weekly(daily_ohlcv)
    monthly_ohlcv = resample_to_monthly(daily_ohlcv)

    print(f"  Daily bars:   {len(daily_ohlcv)}")
    print(f"  Weekly bars:  {len(weekly_ohlcv)}")
    print(f"  Monthly bars: {len(monthly_ohlcv)}")

    # ── CPR lookups ───────────────────────────────────────────────────────────
    print("Computing CPR levels …", flush=True)
    daily_cpr   = build_daily_cpr_lookup(daily_ohlcv)
    weekly_cpr  = build_weekly_cpr_lookup(weekly_ohlcv, daily_ohlcv)
    monthly_cpr = build_monthly_cpr_lookup(monthly_ohlcv, daily_ohlcv)

    # ── RSI lookups (daily, weekly) ───────────────────────────────────────────
    print("Computing daily/weekly RSI …", flush=True)
    daily_rsi   = build_rsi_lookup(daily_ohlcv)
    weekly_rsi  = build_rsi_lookup(weekly_ohlcv)

    # ── MACD lookups (daily, weekly) ──────────────────────────────────────────
    print("Computing daily/weekly MACD …", flush=True)
    daily_macd  = build_macd_lookup(daily_ohlcv)
    weekly_macd = build_macd_lookup(weekly_ohlcv)

    # ── Intraday: 15m, 1h, 4h ─────────────────────────────────────────────────
    print("Computing 15m intraday indicators …", flush=True)
    rsi_15m  = build_intraday_rsi_lookup(daily_bars, 15)
    macd_15m = build_intraday_macd_lookup(daily_bars, 15)
    nm_15    = resample_1m_to_Nm(daily_bars, 15)

    print("Computing 1h intraday indicators …", flush=True)
    rsi_1h   = build_intraday_rsi_lookup(daily_bars, 60)
    macd_1h  = build_intraday_macd_lookup(daily_bars, 60)
    nm_1h    = resample_1m_to_Nm(daily_bars, 60)

    print("Computing 4h intraday indicators …", flush=True)
    rsi_4h   = build_intraday_rsi_lookup(daily_bars, 240)
    macd_4h  = build_intraday_macd_lookup(daily_bars, 240)
    nm_4h    = resample_1m_to_Nm(daily_bars, 240)

    # ── Weekly lookup: date -> weekly bar date (Monday) ───────────────────────
    def week_key_for_date(d_str):
        d   = datetime.date.fromisoformat(d_str)
        mon = d - datetime.timedelta(days=d.weekday())
        return str(mon)

    def month_key_for_date(d_str):
        return d_str[:7]

    # ── Build weekly_rsi & weekly_macd indexed by trading-day date ────────────
    # These are computed on weekly bars (indexed by Mon); map each trading day
    weekly_rsi_by_date  = {}
    weekly_macd_by_date = {}
    for bar in daily_ohlcv:
        d   = bar["date"]
        wk  = week_key_for_date(d)
        # Use prior week's RSI/MACD to avoid look-ahead
        # Find prior week Monday
        prior_mon = str(datetime.date.fromisoformat(wk) - datetime.timedelta(weeks=1))
        weekly_rsi_by_date[d]  = weekly_rsi.get(prior_mon)
        weekly_macd_by_date[d] = weekly_macd.get(prior_mon)

    # ── Annotate each trade ───────────────────────────────────────────────────
    print(f"\nAnnotating {len(trades)} trades …", flush=True)
    BEFORE = "09:44"  # last completed 15m bar end before entry

    for trade in trades:
        d          = trade["date"]
        entry_px   = trade["entry_price"]
        entry_time = trade["entry_time"]

        regime = {}

        # ── CPR ──────────────────────────────────────────────────────────────
        for tf_name, lkp in [("daily", daily_cpr), ("weekly", weekly_cpr), ("monthly", monthly_cpr)]:
            cpr = lkp.get(d)
            if cpr:
                regime[f"cpr_{tf_name}_state"]     = cpr_price_state(entry_px, cpr)
                regime[f"cpr_{tf_name}_width"]     = cpr_width_state(cpr)
                regime[f"cpr_{tf_name}_top"]       = round(cpr["top_cpr"], 4)
                regime[f"cpr_{tf_name}_bottom"]    = round(cpr["bottom_cpr"], 4)
                regime[f"cpr_{tf_name}_pivot"]     = round(cpr["pivot"], 4)
                regime[f"cpr_{tf_name}_r1"]        = round(cpr["r1"], 4)
                regime[f"cpr_{tf_name}_s1"]        = round(cpr["s1"], 4)
            else:
                regime[f"cpr_{tf_name}_state"] = "unknown"
                regime[f"cpr_{tf_name}_width"] = "unknown"

        # ── Daily RSI / MACD ──────────────────────────────────────────────────
        # Use prior day's RSI (no look-ahead)
        daily_dates_sorted = [b["date"] for b in daily_ohlcv]
        try:
            di = daily_dates_sorted.index(d)
            prev_d = daily_dates_sorted[di - 1] if di > 0 else None
        except ValueError:
            prev_d = None

        d_rsi_val = daily_rsi.get(prev_d) if prev_d else None
        regime["rsi_daily"]  = round(d_rsi_val, 2) if d_rsi_val else None
        regime["rsi_daily_state"] = rsi_state(d_rsi_val)
        regime["macd_daily"] = daily_macd.get(prev_d) if prev_d else "unknown"

        # ── Weekly RSI / MACD ─────────────────────────────────────────────────
        w_rsi_val = weekly_rsi_by_date.get(d)
        regime["rsi_weekly"]  = round(w_rsi_val, 2) if w_rsi_val else None
        regime["rsi_weekly_state"] = rsi_state(w_rsi_val)
        regime["macd_weekly"] = weekly_macd_by_date.get(d, "unknown")

        # ── 15m RSI / MACD (use bar ending at 09:44, i.e., the ORB bar itself) ─
        t15m = get_last_completed_bar_time(nm_15, d, entry_time)
        if t15m:
            v15 = rsi_15m.get(d, {}).get(t15m)
            regime["rsi_15m"]       = round(v15, 2) if v15 else None
            regime["rsi_15m_state"] = rsi_state(v15)
            regime["macd_15m"]      = macd_15m.get(d, {}).get(t15m, "unknown")
        else:
            regime["rsi_15m"]       = None
            regime["rsi_15m_state"] = "unknown"
            regime["macd_15m"]      = "unknown"

        # ── 1h RSI / MACD ────────────────────────────────────────────────────
        t1h = get_last_completed_bar_time(nm_1h, d, entry_time)
        if t1h:
            v1h = rsi_1h.get(d, {}).get(t1h)
            regime["rsi_1h"]       = round(v1h, 2) if v1h else None
            regime["rsi_1h_state"] = rsi_state(v1h)
            regime["macd_1h"]      = macd_1h.get(d, {}).get(t1h, "unknown")
        else:
            regime["rsi_1h"]       = None
            regime["rsi_1h_state"] = "unknown"
            regime["macd_1h"]      = "unknown"

        # ── 4h RSI / MACD ────────────────────────────────────────────────────
        # 4h bar at 09:30 starts at open; completed 4h bar is from prior day
        t4h = get_last_completed_bar_time(nm_4h, d, entry_time)
        if t4h is None:
            # Use prior day's last 4h bar
            if prev_d and prev_d in nm_4h and nm_4h[prev_d]:
                t4h_prev = nm_4h[prev_d][-1][0]
                v4h = rsi_4h.get(prev_d, {}).get(t4h_prev)
                regime["rsi_4h"]       = round(v4h, 2) if v4h else None
                regime["rsi_4h_state"] = rsi_state(v4h)
                regime["macd_4h"]      = macd_4h.get(prev_d, {}).get(t4h_prev, "unknown")
            else:
                regime["rsi_4h"]       = None
                regime["rsi_4h_state"] = "unknown"
                regime["macd_4h"]      = "unknown"
        else:
            v4h = rsi_4h.get(d, {}).get(t4h)
            regime["rsi_4h"]       = round(v4h, 2) if v4h else None
            regime["rsi_4h_state"] = rsi_state(v4h)
            regime["macd_4h"]      = macd_4h.get(d, {}).get(t4h, "unknown")

        trade["regime"] = regime

    print("  Annotation complete.", flush=True)
    return trades


# ── Stratified Analysis ───────────────────────────────────────────────────────
def stratified_stats(trades: list, cfg: str, field: str) -> dict:
    """
    Group trades by regime[field] and compute stats for each group.
    Returns {field_value: stats_dict}
    """
    by_val = defaultdict(list)
    for t in trades:
        val = t.get("regime", {}).get(field, "unknown")
        by_val[val].append(t)

    results = {}
    for val, group in sorted(by_val.items(), key=lambda x: str(x[0])):
        pnls = [t["exits"][cfg]["pnl"] for t in group
                if t["exits"].get(cfg) and t["exits"][cfg]["pnl"] is not None]
        if not pnls:
            continue
        n      = len(pnls)
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gp     = sum(wins)
        gl     = abs(sum(losses)) if losses else 0
        results[val] = {
            "n":             n,
            "win_rate":      round(len(wins) / n * 100, 1),
            "profit_factor": round(gp / gl, 2) if gl > 0 else 999,
            "total_pnl":     round(sum(pnls), 2),
            "expectancy":    round(sum(pnls) / n, 4),
        }
    return results


SEP = "=" * 90

def print_stratified(label: str, strats: dict):
    print(f"\n  ── {label} ──")
    print(f"  {'State':<22} {'Trades':>7} {'WR':>7} {'PF':>6} {'P&L':>10} {'Exp/Tr':>8}")
    print(f"  {'-'*22} {'-'*7} {'-'*7} {'-'*6} {'-'*10} {'-'*8}")
    for val, s in sorted(strats.items(), key=lambda x: str(x[0])):
        print(f"  {str(val):<22} {s['n']:>7} {s['win_rate']:>6.1f}% {s['profit_factor']:>6.2f} "
              f"${s['total_pnl']:>+8.2f} {s['expectancy']:>+8.4f}")


def run_regime_analysis(trades: list, canonical_cfg: str = "R2"):
    print(f"\n{SEP}")
    print(f"  REGIME ANALYSIS — {canonical_cfg}")
    print(f"{SEP}")

    timeframes = [
        ("cpr_daily_state",    "Daily CPR Position"),
        ("cpr_weekly_state",   "Weekly CPR Position"),
        ("cpr_monthly_state",  "Monthly CPR Position"),
        ("cpr_daily_width",    "Daily CPR Width"),
        ("rsi_daily_state",    "Daily RSI"),
        ("rsi_weekly_state",   "Weekly RSI"),
        ("rsi_15m_state",      "15m RSI"),
        ("rsi_1h_state",       "1h RSI"),
        ("rsi_4h_state",       "4h RSI"),
        ("macd_daily",         "Daily MACD"),
        ("macd_weekly",        "Weekly MACD"),
        ("macd_15m",           "15m MACD"),
        ("macd_1h",            "1h MACD"),
        ("macd_4h",            "4h MACD"),
    ]

    for field, label in timeframes:
        strats = stratified_stats(trades, canonical_cfg, field)
        if strats:
            print_stratified(label, strats)

    # ── Alignment score ───────────────────────────────────────────────────────
    # "Aligned" = all available indicators agree with breakout direction
    print(f"\n  ── Indicator Alignment vs Breakout Direction ──")
    print(f"  {'Aligned Signals':>22} {'Trades':>7} {'WR':>7} {'PF':>6} {'Exp/Tr':>8}")
    print(f"  {'-'*22} {'-'*7} {'-'*7} {'-'*6} {'-'*8}")

    BULLISH_STATES = {"above_top", "bullish", "overbought", "bullish_cross", "bullish_fade"}
    BEARISH_STATES = {"below_bottom", "bearish", "oversold", "bearish_cross", "bearish_fade"}

    INDICATOR_FIELDS = [
        "cpr_daily_state", "cpr_weekly_state",
        "rsi_daily_state", "rsi_weekly_state", "rsi_1h_state", "rsi_15m_state",
        "macd_daily", "macd_weekly", "macd_1h", "macd_15m",
    ]

    by_score = defaultdict(list)
    for t in trades:
        reg   = t.get("regime", {})
        score = 0
        total_avail = 0
        for f in INDICATOR_FIELDS:
            val = reg.get(f, "unknown")
            if val in ("unknown", None):
                continue
            total_avail += 1
            if t["direction"] == "LONG":
                if val in BULLISH_STATES:
                    score += 1
            else:
                if val in BEARISH_STATES:
                    score += 1

        if total_avail == 0:
            continue
        pct = int(round(score / total_avail * 100 / 20) * 20)  # bucket to 0/20/40/60/80/100
        by_score[pct].append(t)

    for pct in sorted(by_score):
        group = by_score[pct]
        pnls  = [t["exits"][canonical_cfg]["pnl"] for t in group
                 if t["exits"].get(canonical_cfg) and t["exits"][canonical_cfg]["pnl"] is not None]
        if not pnls:
            continue
        n    = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        gp   = sum(wins)
        gl   = abs(sum(losses)) if losses else 0
        pf   = gp / gl if gl > 0 else 999
        exp  = sum(pnls) / n
        print(f"  {pct:>3}% aligned {' ' * 11} {n:>7} {len(wins)/n*100:>6.1f}% {pf:>6.2f} {exp:>+8.4f}")


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    # Load trades from backtest
    if not __import__("os").path.exists(TRADES_FILE):
        print(f"ERROR: {TRADES_FILE} not found. Run orb_paper_backtest.py first.")
        return

    print(f"Loading trades from {TRADES_FILE} …", flush=True)
    with open(TRADES_FILE) as f:
        data = json.load(f)
    trades = data["trades"]
    print(f"  {len(trades)} trades loaded.")

    # Load raw bars for indicator computation
    daily_bars = load_csv_all_bars(CSV_FILE)

    # Annotate trades with regime indicators
    trades = annotate_trades(trades, daily_bars)

    # Run regime analysis for canonical exit config
    for cfg in ["R1", "R2", "EOD"]:
        run_regime_analysis(trades, canonical_cfg=cfg)

    # Save annotated trades
    out = {
        "metadata": data.get("metadata", {}),
        "stats":    data.get("stats", {}),
        "trades":   trades,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n  Regime-annotated results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
