# ORB Research Prompts & Methodology

This document records the Claude Code prompts used to design and build this research project, along with methodology decisions and the reasoning behind them.

---

## Session 1 — Initial Backtest Design

### Prompt
> "I am looking to write a paper on 15m Opening range breakout on $QQQ. Data file: ~/backups/QQQ/2024/XNAS-20260315-V375LGUJ7A/xnas-itch-20180501-20260313.ohlcv-1m.dbn.zst"

### Methodology Decisions Made

**1. Pure underlying, no options**
The existing codebase had ORB logic coupled to 0DTE options (see `orb_cpr_backtest_full.py`). For a paper we want the cleanest signal — pure equity ORB on QQQ shares — to separate strategy alpha from options pricing effects.

**2. Exit strategy matrix**
Rather than picking one exit, we compare 7 configurations to empirically identify the optimal R-multiple:
- R0.5, R1, R2, R3 (fixed ratio targets)
- EOD (end-of-day hold)
- T30, T60 (time-based)

**3. Stop = ORB opposite edge**
The natural and most widely used stop for ORB: if price breaks back into the ORB range, the thesis is invalidated. This makes stop distance exactly 1R (the ORB range).

**4. Entry on close of breakout bar**
The first bar at/after 09:45 whose CLOSE crosses the ORB boundary. This is a confirmed signal (not a mid-bar touch) and avoids false breakouts on spikes.

**5. Min ORB range filter ($0.10)**
Eliminates near-zero range days (holidays, data gaps) that would produce spurious breakout signals.

**6. Data loading via Databento DBN format**
Used `databento` Python library directly. Key fix: `OHLCVMsg` objects expose `rec.ts_event` (not `rec.hd.ts_event`) and prices as fixed-point integers requiring `/1e9` scaling.

---

## Session 2 — Regime Indicators

### Prompt
> "I want you to use the technical indicators to identify regimes, identify turnaround points across timeframes:
> CPR(monthly, weekly, daily)
> RSI(weekly, daily, 4h, 1h, 15m)
> MACD(weekly, daily, 4h, 1h, 15m)"

### Indicator Design Decisions

**CPR (Central Pivot Range)**

The CPR is computed from prior period's H/L/C:
```
Pivot = (H + L + C) / 3
BC    = (H + L) / 2
TC    = 2 × Pivot − BC
Top CPR    = max(TC, BC)
Bottom CPR = min(TC, BC)
```

Three timeframes:
- **Daily CPR**: from prior trading day's RTH bars
- **Weekly CPR**: from prior calendar week (Monday anchor)
- **Monthly CPR**: from prior calendar month

Price is classified as:
- `above_top` — bullish, CPR acts as support
- `inside_cpr` — consolidation, narrow battle zone
- `below_bottom` — bearish, CPR acts as resistance

CPR width is classified as:
- `narrow` (<0.10% of pivot) — trending day expected
- `normal` (0.10%–0.30%) — mixed
- `wide` (>0.30%) — choppy/range day expected

**RSI (14-period, Wilder's smoothing)**

Computed on close series using Wilder's EMA:
```
RS = Avg(14 gains) / Avg(14 losses)
RSI = 100 − 100/(1 + RS)
```

States:
- `overbought` (RSI > 70)
- `bullish` (55–70)
- `neutral` (45–55)
- `bearish` (30–50)
- `oversold` (< 30)

For intraday timeframes (15m, 1h, 4h), bars are resampled from 1-minute OHLCV using RTH-anchored slots (09:30 as slot 0).

**Look-ahead prevention**: For each trade, we use the RSI value of the *last completed bar before entry time*. For 15m, this is the bar closing at 09:44 (the ORB bar itself). For 1h, it's the 09:30 bar (closes at 10:29, but the trade enters at 09:45 or later — so we use prior day's last 1h bar if today's 1h bar is still open).

**MACD (12/26/9 EMA)**

```
MACD line  = EMA(12) − EMA(26) of closes
Signal     = EMA(9) of MACD line
Histogram  = MACD line − Signal
```

States:
- `bullish_cross`  — histogram just turned positive (previous bar ≤ 0)
- `bullish`        — histogram positive and rising
- `bullish_fade`   — histogram positive but declining
- `bearish_cross`  — histogram just turned negative
- `bearish`        — histogram negative and falling
- `bearish_fade`   — histogram negative but rising (toward zero)

Crossovers are detected as a one-bar transition. States are more nuanced than simple sign-of-MACD to capture momentum acceleration vs. deceleration.

**Alignment Score**

10 indicator readings (2 CPR + 4 RSI + 4 MACD on daily/weekly) are checked for directional agreement with the breakout. Score = % of available indicators aligned with trade direction, bucketed to 20% increments.

---

## Session 3 — Repository Setup

### Prompt
> "Lets use the git repo https://github.com/ashok-jaga-gmail/opening-range-breakout for it. Document the prompts in ORB.md, create detailed documentation in README etc."

### Repository Structure Decisions

- Main scripts kept clean and self-contained with `DBN_FILE` constant at top for easy path override
- `README.md` contains all results tables so the paper's key numbers are reproducible
- `ORB.md` (this file) documents the iterative prompt→decision process for academic transparency
- No Jupyter notebooks — pure Python scripts for reproducibility and version control compatibility

---

## Data Pipeline

```
Databento DBN file (ohlcv-1m, 2018–2026)
    │
    ▼
orb_paper_backtest.py
    ├── load_dbn_to_daily_bars()      — DBN → {date: [1m bars]}
    ├── compute_orb()                 — ORB high/low from 09:30–09:44
    ├── find_breakout()               — first close outside ORB at 09:45+
    ├── simulate_all_exits()          — bar-by-bar simulation for 7 exits
    └── → /tmp/orb_paper_results.json
    
    ▼
orb_regime_indicators.py
    ├── resample to daily/weekly/monthly OHLCV
    ├── resample to 4h/1h/15m intraday bars
    ├── compute CPR lookups (no look-ahead)
    ├── compute RSI series per timeframe
    ├── compute MACD series per timeframe
    ├── annotate_trades()             — joins regime data onto each trade
    ├── run_regime_analysis()         — stratified stats by indicator state
    └── → /tmp/orb_regime_results.json
```

---

## Key Statistical Definitions

| Metric | Formula |
|---|---|
| **Win Rate** | Wins / Total × 100 |
| **Profit Factor** | Gross Win / Gross Loss |
| **Expectancy** | Mean P&L per trade |
| **Max Drawdown** | Peak equity − trough (sequential, per-share) |
| **Sharpe** | (Mean daily P&L / Std Dev) × √252 |
| **Calmar** | Annualised P&L / Max Drawdown |

---

## Anti-Bias Checklist

- [x] Entry on *close* of breakout bar (no intra-bar touches)
- [x] Stop at ORB *edge* (not below/above entry — symmetric risk unit)
- [x] All indicators computed from data *before* the entry bar
- [x] CPR uses *prior* period's data (day/week/month)
- [x] Daily RSI uses *prior trading day's* value
- [x] Weekly RSI uses *prior week's* value
- [x] Intraday RSI/MACD uses *last completed bar before entry time*
- [x] No resampling artifacts: 15m bars are RTH-anchored from 09:30
- [x] DBN timestamps converted to NY timezone accounting for DST
- [x] Minimum ORB range filter eliminates near-zero range anomalies

---

## Paper Outline (Suggested)

1. **Abstract** — ORB edge, data, key results
2. **Introduction** — Why ORB? Auction market theory, order flow rationale
3. **Literature Review** — Prior ORB studies (Tomasini, Kaufman, etc.)
4. **Data & Methodology**
   - Dataset description (Databento XNAS.ITCH, 2018–2026)
   - ORB definition and entry rules
   - Exit strategy matrix
   - Stop-loss rationale
5. **Core Results**
   - Exit strategy comparison table
   - Annual performance breakdown
   - Long vs. short analysis
   - ORB range quartile analysis
6. **Regime Analysis**
   - CPR as trend identifier
   - RSI extremes vs. neutral
   - MACD crossover timing
   - Composite alignment score
7. **Practical Filter Rules**
   - High-quality setup criteria
   - Regimes to avoid
8. **Risk & Limitations**
   - Transaction costs
   - Slippage at breakout bar
   - Price normalization across 8 years
   - Regime filter needs out-of-sample validation
9. **Conclusion**
10. **Appendix** — Full trade tables, monthly breakdown

---

## Notes on Turnaround Points

The indicators identify **potential regime change / turnaround signals** as follows:

### CPR Turnaround Signals
- Price enters CPR from above → potential short-term resistance / range compression
- Price breaks back below CPR after being above → bearish regime shift
- Narrow CPR followed by wide CPR day → volatility expansion expected
- Monthly CPR inside weekly CPR → major confluence zone (mean reversion magnet)

### RSI Turnaround Signals
- Weekly RSI drops from overbought (>70) to neutral → first leg of correction
- Weekly RSI bounces from oversold (<30) → reversal from capitulation
- 15m RSI divergence (price makes new ORB low but RSI makes higher low) → bearish ORB may fail
- 1h RSI crossing 50 (neutral → bullish) = intraday trend change aligned with ORB direction

### MACD Turnaround Signals
- **Weekly MACD bullish_cross** = macro regime shift to uptrend (historically strongest signal; +$0.379 expectancy per trade vs +$0.009 for bullish_fade)
- **4h MACD bearish_cross** = short-term top forming; short ORB setups strengthen
- **1h MACD bullish** = local trend supporting long ORBs ($0.338 expectancy)
- **1h/4h MACD fade states** = avoid (momentum is stalling; breakouts likely to fail)
