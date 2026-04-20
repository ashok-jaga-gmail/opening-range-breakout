"""
orb_options_2026.py — Options P&L comparison for 2026 ORB trades

Uses the R2 exit results from orb_paper_backtest.py and 0DTE QQQ options OHLC
(from Thetadata parquet files) to compare three strike offsets:

  ATM   = round(entry_price)          (closest strike to underlying entry)
  OTM   = ATM + 1 (calls) / ATM - 1 (puts)
  OTM+1 = ATM + 2 (calls) / ATM - 2 (puts)

Long ORB → buy calls.  Short ORB → buy puts.

Entry price  = option OPEN at the breakout bar (same bar as underlying entry)
Exit price   = option CLOSE at the R2 exit bar  (or last bar if EOD)

Output:
  - Comparison table printed to stdout
  - tmp/options_2026_results.json
"""

import csv
import json
import math
import os
import struct
from collections import defaultdict

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE       = os.path.dirname(os.path.abspath(__file__))
TRADES_FILE = "/tmp/orb_paper_results.json"
OPTIONS_DIR = os.path.expanduser(
    "~/backups/QQQ/2026/Options-OHLC/thetadata-2026"
)
OUT_FILE    = os.path.join(_HERE, "tmp", "options_2026_results.json")

STRIKE_LABELS = ["ATM", "OTM", "OTM+1"]


# ---------------------------------------------------------------------------
# Minimal Parquet reader (no pandas / pyarrow dependency)
# We shell out to python -c with pandas only if it's available, otherwise
# we use a stdlib-based CSV fallback.  But since pandas IS available on most
# research machines we use it via importlib so it's not a hard requirement.
# ---------------------------------------------------------------------------
def load_parquet(path: str) -> list[dict]:
    """Return list of row dicts from a parquet file.  Requires pandas/pyarrow."""
    try:
        import pandas as pd
        df = pd.read_parquet(path)
        return df.to_dict("records")
    except ImportError:
        raise RuntimeError(
            "pandas/pyarrow required to read parquet files.\n"
            "Install with: pip install pandas pyarrow"
        )


def normalize_right(r) -> str:
    if r is None:
        return ""
    r = str(r).strip().lower()
    if r in ("call", "c"):
        return "call"
    if r in ("put", "p"):
        return "put"
    return r


def extract_hhmm(ts: str) -> str:
    """'2026-01-02T09:45:00.000' → '09:45'"""
    return str(ts)[11:16]


# ---------------------------------------------------------------------------
# Build option price lookup: date → {right → {strike → {hhmm → {open, close}}}}
# ---------------------------------------------------------------------------
def build_option_lookup(date_str: str) -> dict:
    """Load one day's parquet and return nested price dict."""
    date_compact = date_str.replace("-", "")
    path = os.path.join(OPTIONS_DIR, f"qqq-options-1m-{date_compact}.parquet")
    if not os.path.exists(path):
        return {}

    rows = load_parquet(path)
    lookup: dict = {}  # right → strike → hhmm → {open, close}
    for row in rows:
        right  = normalize_right(row.get("right"))
        strike = int(row.get("strike", 0))
        hhmm   = extract_hhmm(row.get("timestamp", ""))
        open_  = float(row.get("open") or 0)
        close_ = float(row.get("close") or 0)
        if not right or not hhmm:
            continue
        lookup.setdefault(right, {}).setdefault(strike, {})[hhmm] = {
            "open":  open_,
            "close": close_,
        }
    return lookup


# ---------------------------------------------------------------------------
# Strike selection
# ---------------------------------------------------------------------------
def select_strikes(direction: str, entry_price: float) -> dict[str, tuple[str, int]]:
    """Return {label: (right, strike)} for ATM / OTM / OTM+1."""
    atm = int(round(entry_price))
    if direction == "LONG":
        right = "call"
        return {
            "ATM":   (right, atm),
            "OTM":   (right, atm + 1),
            "OTM+1": (right, atm + 2),
        }
    else:  # SHORT
        right = "put"
        return {
            "ATM":   (right, atm),
            "OTM":   (right, atm - 1),
            "OTM+1": (right, atm - 2),
        }


# ---------------------------------------------------------------------------
# Stats helpers
# ---------------------------------------------------------------------------
def max_drawdown(equity: list[float]) -> float:
    """Maximum peak-to-trough drawdown (negative number)."""
    if not equity:
        return 0.0
    peak = equity[0]
    dd   = 0.0
    for v in equity:
        if v > peak:
            peak = v
        dd = min(dd, v - peak)
    return dd


def compute_stats(pnls: list[float], n_trading_days: int = 52) -> dict:
    """Win rate, PF, expectancy, max drawdown, Sharpe, Calmar."""
    if not pnls:
        return {}
    n       = len(pnls)
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p <= 0]
    wr      = 100.0 * len(wins) / n
    gross_w = sum(wins)
    gross_l = abs(sum(losses))
    pf      = gross_w / gross_l if gross_l > 0 else float("inf")
    expect  = sum(pnls) / n

    # Equity curve (cumulative)
    equity  = []
    cum     = 0.0
    for p in pnls:
        cum += p
        equity.append(cum)

    total_pnl = cum
    mdd       = max_drawdown(equity)

    # Annualised return: scale up from 49 trades over ~52 trading days to 252
    ann_return = total_pnl * (252.0 / n_trading_days)
    calmar     = ann_return / abs(mdd) if mdd != 0 else float("inf")

    # Sharpe (per-trade basis × √252)
    mean_pnl = expect
    var      = sum((p - mean_pnl) ** 2 for p in pnls) / n
    std      = math.sqrt(var) if var > 0 else 0.0
    sharpe   = (mean_pnl / std * math.sqrt(252)) if std > 0 else float("inf")

    return {
        "n":           n,
        "win_rate":    round(wr, 1),
        "profit_factor": round(pf, 2),
        "expectancy":  round(expect, 2),
        "total_pnl":   round(total_pnl, 2),
        "max_drawdown": round(mdd, 2),
        "ann_return":  round(ann_return, 2),
        "sharpe":      round(sharpe, 2),
        "calmar":      round(calmar, 2),
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # Load ORB trades
    with open(TRADES_FILE) as f:
        data = json.load(f)
    trades_2026 = [t for t in data["trades"] if t["year"] == "2026"]
    print(f"Loaded {len(trades_2026)} ORB trades for 2026\n")

    # Collect per-label P&Ls
    pnls: dict[str, list[float]] = {lbl: [] for lbl in STRIKE_LABELS}

    # Detailed trade log
    trade_log: list[dict] = []

    missing_entry = 0
    missing_exit  = 0

    for trade in trades_2026:
        date        = trade["date"]
        direction   = trade["direction"]
        entry_time  = trade["entry_time"]   # e.g. "09:47"
        entry_price = trade["entry_price"]
        r2          = trade["exits"]["R2"]
        exit_time   = r2["exit_time"]       # e.g. "10:12" or "15:59"
        exit_reason = r2["exit_reason"]

        option_lookup = build_option_lookup(date)
        if not option_lookup:
            print(f"  WARNING: no option data for {date}")
            continue

        strikes = select_strikes(direction, entry_price)

        row = {"date": date, "direction": direction,
               "entry_time": entry_time, "exit_time": exit_time,
               "exit_reason": exit_reason}

        for lbl, (right, strike) in strikes.items():
            strike_data = option_lookup.get(right, {}).get(strike, {})

            # Entry: open of entry_time bar
            entry_bar = strike_data.get(entry_time)
            if entry_bar is None or entry_bar["open"] == 0:
                missing_entry += 1
                row[lbl] = None
                continue

            opt_entry = entry_bar["open"]

            # Exit: close of exit_time bar (walk back if EOD/missing)
            exit_bar = strike_data.get(exit_time)
            if exit_bar is None or exit_bar["close"] == 0:
                # Try last available bar before exit_time
                avail = sorted(
                    k for k in strike_data if k <= exit_time and strike_data[k]["close"] > 0
                )
                if not avail:
                    missing_exit += 1
                    row[lbl] = None
                    continue
                exit_bar = strike_data[avail[-1]]

            opt_exit = exit_bar["close"]

            # P&L per contract = (exit - entry) × 100
            pnl = (opt_exit - opt_entry) * 100.0
            pnls[lbl].append(pnl)
            row[lbl] = {
                "right": right, "strike": strike,
                "opt_entry": opt_entry, "opt_exit": opt_exit,
                "pnl": round(pnl, 2),
            }

        trade_log.append(row)

    if missing_entry > 0:
        print(f"  Missing entry prices (skipped): {missing_entry}")
    if missing_exit > 0:
        print(f"  Missing exit prices (skipped):  {missing_exit}")
    print()

    # Compute stats per label
    stats: dict[str, dict] = {}
    for lbl in STRIKE_LABELS:
        stats[lbl] = compute_stats(pnls[lbl])

    # ---------------------------------------------------------------------------
    # Print comparison table
    # ---------------------------------------------------------------------------
    w = 10
    header = f"{'Metric':<20}" + "".join(f"{lbl:>{w}}" for lbl in STRIKE_LABELS)
    sep    = "=" * len(header)
    print(sep)
    print("  QQQ 0DTE Options — 2026 ORB R2 Backtest (per contract, $ per trade)")
    print(sep)
    print(header)
    print("-" * len(header))

    metrics = [
        ("Trades (n)",     "n",             "{}"),
        ("Win Rate (%)",   "win_rate",      "{:.1f}"),
        ("Profit Factor",  "profit_factor", "{:.2f}"),
        ("Expectancy ($)", "expectancy",    "{:.2f}"),
        ("Total P&L ($)",  "total_pnl",     "{:.2f}"),
        ("Max Drawdown ($)","max_drawdown", "{:.2f}"),
        ("Ann. Return ($)", "ann_return",   "{:.2f}"),
        ("Sharpe",         "sharpe",        "{:.2f}"),
        ("Calmar",         "calmar",        "{:.2f}"),
    ]

    for label, key, fmt in metrics:
        row_str = f"  {label:<18}"
        for lbl in STRIKE_LABELS:
            val = stats[lbl].get(key, "N/A")
            if val == float("inf"):
                row_str += f"{'∞':>{w}}"
            elif isinstance(val, float):
                row_str += f"{fmt.format(val):>{w}}"
            else:
                row_str += f"{str(val):>{w}}"
        print(row_str)

    print(sep)
    print()

    # Per-label equity summary
    print("  Strike notes:")
    for lbl in STRIKE_LABELS:
        if pnls[lbl]:
            best  = max(pnls[lbl])
            worst = min(pnls[lbl])
            print(f"    {lbl}: best trade ${best:.2f}  worst trade ${worst:.2f}"
                  f"  ({len(pnls[lbl])} trades with complete data)")
    print()

    # Save output
    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    output = {
        "description": "QQQ 0DTE Options — 2026 ORB R2 backtest by strike offset",
        "strike_logic": {
            "LONG":  "buy calls: ATM=round(entry), OTM=ATM+1, OTM+1=ATM+2",
            "SHORT": "buy puts:  ATM=round(entry), OTM=ATM-1, OTM+1=ATM-2",
        },
        "entry_price": "option OPEN at entry_time bar",
        "exit_price":  "option CLOSE at R2 exit_time bar (or last available)",
        "pnl_unit":    "dollars per contract (option price × 100)",
        "stats":       stats,
        "trades":      trade_log,
    }
    with open(OUT_FILE, "w") as f:
        json.dump(output, f, indent=2)
    print(f"  Results saved → {OUT_FILE}")


if __name__ == "__main__":
    main()
