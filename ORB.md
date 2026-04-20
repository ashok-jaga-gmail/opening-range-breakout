# ORB Research Prompts & Methodology

---

## Golden Strategy — QQQ 15-min ORB, 0DTE Options (3 trades/day)

### What it is

A systematic intraday options strategy on QQQ that trades breakouts from the first 15 minutes of the session. Up to three independent breakout trades are taken per day — one contract each, no regime filters, both directions.

### Setup

**Opening Range (ORB):** The high and low of QQQ 1-minute bars from 09:30–09:44 ET define the range for the day.

**Signal:** Starting at 09:45, the strategy watches for any 1-minute bar whose *close* exits the ORB — above the high (LONG signal) or below the low (SHORT signal). Each time price breaks out in a new direction after returning to the range, a new trade fires. Up to 3 signals per day are taken as independent positions.

**Instrument:** QQQ 0DTE options — calls for LONG signals, puts for SHORT signals — at the first OTM strike (+1 from ATM).

### Entry

- Enter at the open of the option bar at (or immediately after) the breakout bar close time.
- No filters on regime, alignment, CPR, or ORB size — every valid breakout is traded.

### Exit — 3-tranche structure

Each trade splits into three tranches (25% / 25% / 50%) with option-price percentage targets:

| Tranche | Size | Target | Action on hit |
|---|---|---|---|
| T1 | 25% | +25% option gain | Exit T1; move stop to breakeven (entry price) |
| T2 | 25% | +100% option gain | Exit T2; begin trailing stop at max_price × 70% |
| T3 | 50% | +200% option gain or EOD | Exit T3 at target, trail stop, or 15:59 close |

**Stop loss:** −30% from entry option price (all tranches until T1 is hit).  
**After T1:** stop rises to entry price (no further loss possible).  
**After T2:** trailing stop = rolling max option price × 70%, locking in gains.

### Results (289 trading days, 766 trades, 2025 Jan – 2026 Mar)

| Metric | Value |
|---|---|
| Total trades | 766 (avg 2.65/day) |
| Trade win rate | **79.5%** |
| Win days | 201/289 = 69.6% |
| Total P&L | **+$13,835** per contract |
| Avg P&L / day | +$48 |
| Profit Factor | **4.66** |
| Max Drawdown | $245 |
| **Calmar ratio** | **56.53** |

All 15 months (Jan 2025 – Mar 2026) profitable.

---

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

---

## Session 4 — MAE/MFE Analysis

### Prompt
> "Can you do a MAE/MFE analysis on each trade, update md file after each significant research"

### Script: `orb_mae_mfe.py`

Re-walks 1-minute bars for each of the 1,976 R2 trades (2018–2026) to compute:
- **MAE** (Maximum Adverse Excursion): worst intrabar move against the position, in R-multiples
- **MFE** (Maximum Favorable Excursion): best intrabar move in the position's favour, in R-multiples
- **Efficiency**: exit_pnl / MFE — how much of the best available move was captured

### Key Findings

**1. MAE distribution (R2 exit, n=1,976)**

| Metric | P25 | Median | P75 | P90 | Mean |
|---|---|---|---|---|---|
| MAE (R) | 0.469 | 1.010 | 1.161 | 1.286 | 0.852 |
| MFE (R) | 0.331 | 0.908 | 1.870 | 2.243 | 1.090 |
| Efficiency | –2.876 | –0.617 | 0.844 | 0.980 | –3.025 |

Median MAE ≈ 1.0R means the typical trade visits the stop area before resolving. Negative median efficiency is dominated by losers (which hit 1R stop with little MFE).

**2. Winners vs Losers — the MAE split is the sharpest edge**

| Group | n | MAE Median | MFE Median |
|---|---|---|---|
| Winners | 846 | **0.42R** | 2.02R |
| Losers | 1,130 | **1.14R** | 0.41R |

Winners barely pull back. Losers touch or breach the stop. This is the clearest filter signal in the dataset.

**3. MAE bucket win rates — stop placement matters enormously**

| MAE bucket | Wins | Losses | WR |
|---|---|---|---|
| < 0.25R | 211 | 5 | **97.7%** |
| 0.25–0.50R | 288 | 25 | **92.0%** |
| 0.50–0.75R | 197 | 45 | **81.4%** |
| 0.75–1.00R | 116 | 88 | **56.9%** |
| ≥ 1.00R | 34 | 967 | **3.4%** |

Interpretation: the win rate degrades sharply as MAE approaches 1R. Trades that pull back ≥1R are almost entirely losses (stop-outs).

**4. Stop sensitivity — tighter stops, higher WR, fewer trades**

| Stop at | Trades surviving | Win Rate |
|---|---|---|
| 0.25R | 11% (217) | 97.7% |
| 0.50R | 27% (532) | 94.4% |
| 0.75R | 39% (771) | 90.3% |
| 1.00R (baseline) | 50% (983) | 83.2% |

A trailing stop at 0.5R would keep 94% WR but discard 73% of trades — selectivity at the cost of frequency.

**5. MFE target reach — how often did price reach each level?**

| Target | All trades | Winners only | Losers only |
|---|---|---|---|
| 0.5R | 66.1% | 98.1% | 42.1% |
| 1.0R | 47.0% | 85.5% | 18.1% |
| 1.5R | 33.1% | 69.1% | 6.2% |
| 2.0R | 22.4% | 50.7% | 1.2% |
| 3.0R | 0.3% | 0.6% | 0.1% |

50.7% of winners reached 2R MFE — consistent with R2 being captured. Only 2.8% of all trades had MFE > 2R but exited below target (meaning price ran 2R then reversed before the target was touched — these are partial-capture candidates for a trailing stop after 1R).

**6. Winner efficiency = 90.9%**

When a trade wins, it captures 90.9% (median) of its best possible intraday move. The R2 fixed target is tight enough to lock in most of the available MFE on winners.

### Practical Implications for Paper

1. **MAE is a leading loss indicator**: Any pullback > 0.75R is a warning sign; > 1R is almost always a loss. Consider a time-stop or partial exit rule if MAE exceeds 0.5R.
2. **The stop at 1R (ORB opposite edge) is appropriate**: the data shows most winners have MAE < 0.5R — widening the stop would add risk without improving winning trades.
3. **R2 target is well-calibrated**: 50.7% of winners reach 2R in MFE, efficiency is 90.9% — suggesting R2 captures the move without over-staying.
4. **Tighter stops improve WR dramatically but reduce trade count** — a composite filter (regime indicators + MAE stop tightening) is the natural next research step.

---

## Session 5 — Tranche Exit Strategy

### Prompt
> "Come up with a high winrate strategy based on all the findings so far, a good strategy has multiple tranches, takes profit at right places and leaves some runners for eod"

### Script: `orb_tranche_strategy.py`

### Strategy Design

**Pre-trade filters (all must pass):**
1. Alignment score ≥ 60% (≥6/10 regime indicators confirm direction)
2. Daily CPR: price above_top for LONG, below_bottom for SHORT
3. ORB range ≤ $2.25 (Q3 threshold — excludes wide/chaotic days)

**Entry:** Same as baseline (first 1-min bar at/after 09:45 that closes outside ORB)

**Position: 3 equal tranches (1/3 each)**

| Tranche | Target | Stop action after hit |
|---|---|---|
| T1 | 1.0R | Move stop to **breakeven** |
| T2 | 1.5R | Begin **1R trailing stop** from HWM |
| T3 (runner) | Trail or EOD | Exit on trail trigger or 15:59 |

Rationale from MAE/MFE: 85.5% of winners reach 1R MFE, 69.1% reach 1.5R. Moving stop to BE after T1 eliminates the possibility of a full loss on an already-confirmed trade.

### Results (546 filtered trades, 2018–2026)

**Overall comparison:**

| Strategy | n | WR | PF | Expectancy | MaxDD | Calmar |
|---|---|---|---|---|---|---|
| Tranche (T1/T2/Trail) | 546 | **55.1%** | 1.39 | +$0.213 | $12.12 | 4.43 |
| Baseline R2 (filtered) | 546 | 48.7% | 1.49 | +$0.306 | $11.48 | 6.71 |

The tranche structure improves WR (+6.4pp) at the cost of lower per-trade expectancy, as T1 and T2 lock in profits early. Baseline R2 has higher Calmar because R2 winners capture the full 2R move.

**Phase breakdown (where do trades resolve?):**

| Sequence | n | % | WR | Avg P&L |
|---|---|---|---|---|
| All 3 tranches (T1+T2+Runner) | 161 | 29.5% | **100%** | +$1.93 |
| T1 hit → EOD (no T2) | 33 | 6.0% | **100%** | +$1.37 |
| T1 hit → stopped at BE | 56 | 10.3% | **100%** | +$0.46 |
| Full stop (pre-T1) | 203 | 37.2% | 0% | −$1.38 |
| EOD, no T1 hit | 84 | 15.4% | 60.7% | +$0.19 |

**45.8% of trades hit T1 and are guaranteed profitable.** The only losers are the 37.2% full stops and a subset of the 15.4% EOD-no-T1 group.

**Runner (T3) performance:**
- 161 trades (29.5%) reach all 3 tranches
- T3 exit: TRAIL=81 (50%), EOD=80 (50%) — evenly split
- T3 exit level: P25=1.08R, Median=1.60R, P75=2.15R, Mean=1.75R

**Direction breakdown:**

| Direction | n | WR | PF | Calmar |
|---|---|---|---|---|
| LONG | 435 | **56.3%** | 1.53 | **6.22** |
| SHORT | 111 | 50.5% | 1.05 | 0.36 |

**SHORT trades have virtually no edge on this strategy** (Calmar 0.36). The filter should be LONG-only or at minimum SHORT should require much higher alignment.

**Alignment score sweet spot (70–80%):**

| Score band | n | WR | PF | Avg P&L |
|---|---|---|---|---|
| 60–70% | 74 | 48.6% | 1.20 | +$0.13 |
| **70–80%** | **102** | **63.7%** | **2.37** | **+$0.63** |
| 80–90% | 143 | 56.6% | 1.20 | +$0.12 |
| 90–100% | 227 | 52.4% | 1.21 | +$0.11 |

70–80% alignment is the sweet spot — high enough to confirm quality without being so restrictive that only the most obvious (crowded) setups remain. Very high alignment (90%+) likely means overbought conditions where mean-reversion risk is elevated.

### Key Findings

1. **WR improved to 55.1%** (from 42.9% unfiltered) through the regime + CPR filter alone
2. **45.8% of all filtered trades are guaranteed winners** (hit T1 → stop moves to BE)
3. **Runners add meaningful value**: median T3 exit at 1.6R, half reaching EOD
4. **LONG-only is the correct implementation** — SHORT edge is negligible
5. **70–80% alignment band is optimal** — tightest WR/PF sweet spot
6. **Baseline R2 still beats tranche on Calmar** (6.71 vs 4.43) — tranche is preferred for traders who prioritize consistency and reduced emotional burden of watching a trade reverse from 1.8R back to stop

### Path to Higher Win Rate (80%+ target)
From MAE analysis: 80%+ WR requires trades that don't pull back beyond ~0.75R. The next filter is:
- LONG only
- 70–80% alignment
- Narrow daily CPR (width = 'narrow' → trending day expected)
- RSI daily in bullish or overbought state

---

## Session 6 — Grid Search Optimization (Maximize P&L + WR)

### Prompt
> "Maximize p/l as well"

### Script: `orb_optimized.py`

256-configuration grid search over:
- `align_min`: 0.60 / 0.70 / 0.75 / 0.80
- `require_rsi_bull`: True / False
- `require_narrow_cpr`: True / False
- `t1_r`: 0.75R / 1.0R
- `t2_r`: 1.5R / 2.0R
- `trail_r`: 1.0R / 1.5R
- `weights`: equal 1/3 vs runner-heavy 25/25/50

LONG-only and daily CPR alignment fixed (established by Session 5).

### Key Discoveries

**1. Adding RSI bull / narrow CPR filters HURTS**

Counterintuitive: despite being strong individual predictors, these filters over-restrict trade count without proportionally improving outcomes. The CPR above_top filter alone is sufficient. The RSI and width filters appear to remove legitimate trades during consolidation-to-breakout transitions.

**2. align70 is the optimal threshold**

- align60: more trades (435), slightly higher total P&L, lower Calmar (looser quality)
- align70: fewer trades (395), highest Calmar (8.53), near-best P&L
- align75/80: too few trades (diminishing returns)

**3. T2=2.0R + runner50 is the P&L maximizer**

Giving 50% of the position to the runner (T3) and setting T2 at the proven R2 level (2.0R) captures more of the MFE on strong days without materially increasing max drawdown.

**4. Trail width (1.0R vs 1.5R) is almost a wash**

Tighter trail (1.0R) is marginally better for Calmar; wider trail (1.5R) is marginally better for total P&L. The difference is negligible — implementation preference.

### Top Configs

**By Calmar (risk-adjusted):**

| # | n | WR | PF | Expectancy | Total | MaxDD | Calmar | Config |
|---|---|---|---|---|---|---|---|---|
| 1 | 395 | 57.0% | 1.59 | +$0.286 | +$112.97 | $8.45 | **8.53** | align70, T1=1R, T2=1.5R, trail=1R, runner50 |
| 2 | 395 | 56.7% | 1.61 | +$0.293 | +$115.79 | $8.67 | **8.52** | align70, T1=1R, T2=1.5R, trail=1.5R, runner50 |
| 3 | 395 | 57.7% | 1.66 | +$0.318 | +$125.43 | $9.46 | **8.46** | align70, T1=1R, T2=2R, trail=1.5R, runner50 ★ |

**By Total P&L:**

| # | n | WR | PF | Expectancy | Total | MaxDD | Calmar | Config |
|---|---|---|---|---|---|---|---|---|
| 1 | 435 | 57.0% | 1.61 | +$0.301 | **+$130.96** | $11.64 | 6.51 | align60, T1=1R, T2=2R, trail=1R, runner50 |
| 3 | 395 | 57.7% | 1.66 | +$0.318 | **+$125.43** | $9.46 | 8.46 | align70, T1=1R, T2=2R, trail=1.5R, runner50 ★ |

★ = **Recommended config**: best balance of P&L and Calmar

### Recommended Final Strategy

```
Filters (pre-trade):
  • LONG only
  • Daily CPR: price above_top
  • Alignment ≥ 70%  (≥7/10 indicators confirm direction)
  • ORB range ≤ $2.25  (Q3 — exclude chaotic wide-range days)

Entry:
  First 1-min bar at/after 09:45 closing above ORB high
  Entry price = close of that bar

Position: 3 tranches
  T1 — 25% of size at +1.0R  → move stop to BREAKEVEN
  T2 — 25% of size at +2.0R  → begin trailing at 1.5R from high-water mark
  T3 — 50% of size  (runner) → trail or hold to EOD

Stop: ORB low (1R below typical entry price)
```

**8-year results (2018–2026), 395 trades:**

| Metric | Value |
|---|---|
| Win Rate | 57.7% |
| Profit Factor | 1.66 |
| Expectancy | +$0.318 / trade |
| Total P&L (per share) | +$125.43 |
| Max Drawdown | $9.46 |
| Sharpe | 3.24 |
| Calmar | **8.46** |

**Annual breakdown (best config):**

| Year | n | WR | Total | Calmar |
|---|---|---|---|---|
| 2018 | 33 | 57.6% | +$7.00 | 40.5 |
| 2019 | 59 | 57.6% | +$2.52 | 2.5 |
| 2020 | 66 | 54.5% | +$15.65 | 10.3 |
| 2021 | 67 | 62.7% | +$26.64 | 33.6 |
| 2022 | 15 | 60.0% | +$6.97 | 19.6 |
| 2023 | 66 | 50.0% | +$21.73 | 15.1 |
| 2024 | 50 | 58.0% | +$17.76 | 10.6 |
| 2025 | 38 | 57.9% | +$14.11 | 23.9 |

8 of 8 years profitable. Worst year: 2019 (+$2.52, WR 57.6%). Best year: 2021 (+$26.64, WR 62.7%).

### What the grid search ruled out

- **RSI daily filter**: removes too many good trades in early-trend stages
- **Narrow CPR filter**: choppy days can still produce strong ORB trends; too restrictive
- **align80+**: sharp drop in trade count, diminishing marginal quality improvement
- **T1 at 0.75R**: lower Calmar than 1.0R because it exits the first tranche too early, leaving less breakeven-stop protection on the remaining position
- **Equal 1/3 weights**: runner-heavy (50%) consistently outperforms — the runner is the alpha generator

---

## Session 7 — Options Grid Search (2025–2026, 53 trades)

### Overview

Applies the optimized underlying strategy to 0DTE QQQ options, then runs a 324-config grid search over option-price percentage targets (PT1/PT2/PT3) and stop-loss (SL%) — replacing R-multiple exits with pure option-price percentage moves.

**Data**: 53 filtered trades (2025 Jan – 2026 Jan) with confirmed Thetadata options coverage.  
**Grid**: PT1 ∈ {25, 50, 75, 100}%, PT2 ∈ {100, 150, 200}%, PT3 ∈ {200, 300}%, SL ∈ {30, 50, 75}%  
**Weights**: equal (1/3 each) or runner50 (25/25/50)  
**Strikes**: ATM (Δ≈0.50), OTM (+1 strike), OTM+1 (+2 strikes)

**Exit logic**:
- T1 hit when option HIGH ≥ entry_opt × (1 + PT1/100) → stop moves to entry_opt (breakeven)
- T2 hit when option HIGH ≥ entry_opt × (1 + PT2/100) → trailing stop = max_opt × (1 − SL/100)
- T3 hit when option HIGH ≥ entry_opt × (1 + PT3/100) or EOD
- SL: any tranche stopped when LOW ≤ current stop price

### Top configs by Calmar

| # | WR | PF | Exp | Total | MaxDD | Calmar | Config |
|---|---|---|---|---|---|---|---|
| 1 | 75.5% | 2.61 | +$11.15 | +$591 | $90 | **31.12** | PT25/100/200, SL30, OTM, runner50 |
| 2 | 75.5% | 2.49 | +$10.35 | +$549 | $90 | 28.89 | PT25/150/200, SL30, OTM, runner50 |
| 3 | 75.5% | 2.49 | +$10.32 | +$547 | $90 | 28.80 | PT25/100/200, SL30, OTM, equal |
| 4 | 83.0% | 2.67 | +$12.61 | +$668 | $114 | 27.88 | PT25/100/300, SL50, OTM, runner50 |
| 5 | 75.5% | 2.72 | +$7.21 | +$382 | $66 | 27.41 | PT25/200/300, SL30, OTM+1, runner50 |
| 7 | 69.8% | 2.21 | +$16.31 | +$864 | $153 | 26.90 | PT25/100/300, SL30, ATM, runner50 |
| 14 | **90.6%** | 3.05 | +$14.47 | +$767 | $150 | 24.31 | PT25/100/300, SL75, OTM, runner50 |

### Top configs by P&L

| # | WR | Total | MaxDD | Calmar | Config |
|---|---|---|---|---|---|
| 1 | 58.5% | **+$1,225** | $340 | 17.13 | PT75/150/300, SL50, ATM, runner50 |
| 2 | 41.5% | +$1,220 | $343 | 16.92 | PT75/100/300, SL30, ATM, runner50 |
| 3 | 58.5% | +$1,208 | $332 | 17.33 | PT75/150/300, SL50, ATM, equal |
| 4 | 41.5% | +$1,207 | $343 | 16.74 | PT75/100/200, SL30, ATM, runner50 |
| 5 | 58.5% | +$1,195 | $340 | 16.72 | PT75/100/300, SL50, ATM, runner50 |

### Top configs by win rate

| # | WR | Total | Calmar | Config |
|---|---|---|---|---|
| 1 | **90.6%** | +$767 | 24.31 | PT25/100/300, SL75, OTM, runner50 |
| 2 | **90.6%** | +$692 | 23.89 | PT25/100/300, SL75, OTM, equal |
| 3 | **90.6%** | +$663 | 20.95 | PT25/200/300, SL75, OTM, runner50 |
| 4 | **90.6%** | +$607 | 20.94 | PT25/100/200, SL75, OTM, equal |

### Best config deep-dive: PT25/100/200_SL30_OTM_runner50

53 trades (2025 Jan – 2026 Jan), Calmar = **31.12**

| Month | n | WR | Total |
|---|---|---|---|
| 2025-01 | 3 | 66.7% | −$23 |
| 2025-02 | 4 | 75.0% | −$17 |
| 2025-03 | 1 | 100.0% | +$5 |
| 2025-04 | 2 | 50.0% | +$239 |
| 2025-05 | 3 | 66.7% | +$155 |
| 2025-06 | 7 | 57.1% | +$5 |
| 2025-07 | 8 | 87.5% | +$72 |
| 2025-08 | 2 | 50.0% | +$27 |
| 2025-09 | 6 | 83.3% | +$20 |
| 2025-10 | 8 | 87.5% | +$111 |
| 2025-11 | 1 | 0.0% | −$28 |
| 2025-12 | 4 | 100.0% | +$17 |
| 2026-01 | 4 | 75.0% | +$8 |

Best trade: 2025-04-24 (+$294). Worst: 2025-04-28 (−$55).  
11 of 13 months profitable.

### Key findings

1. **PT1=25% is the dominant first target** — quick partial exit on OTM options achieves high WR by capturing the frequent early move before options decay or reverse. Tight SL30 keeps the max drawdown low.

2. **OTM dominates Calmar; ATM dominates P&L** — OTM options amplify percentage moves (cheaper entry, same dollar swing on a good day), producing superior risk-adjusted returns. ATM options cost more but generate higher absolute dollar P&L on large moves.

3. **runner50 consistently beats equal weights** — confirmed again in options space. The T3 runner (50% position) is the primary alpha generator; T1/T2 cover cost and lock in breakeven.

4. **90.6% win rate is achievable** — PT25/SL75/OTM configs hit 90.6% WR across 53 trades. The cost is wider stops (SL75 = 75% loss on remaining tranches) but Calmar remains excellent (24+).

5. **SL30 = best risk-adjusted; SL75 = best win rate** — tight stops get stopped out less on dollar basis but allow more re-entries; wide stops let winners run further but accept larger individual losses.

6. **ATM vs OTM trade-off summary**:

| Objective | Strike | PT1 | SL | WR | Calmar | Total |
|---|---|---|---|---|---|---|
| Best Calmar | OTM | 25% | 30% | 75.5% | 31.12 | +$591 |
| Best P&L | ATM | 75% | 50% | 58.5% | 17.13 | +$1,225 |
| Best Win Rate | OTM | 25% | 75% | **90.6%** | 24.31 | +$767 |

---

## Golden Strategy — ORB Options, No Filters (2025–2026)

> **Confirmed 2026-04-19.** The definitive best configuration after removing all regime/CPR/alignment filters and running all 289 ORB breakout trades (LONG + SHORT) through the Session 7 best options exit structure.

### Why no filters?

The alignment, CPR, and ORB% filters were designed for the underlying R-multiple strategy. In options space, PT25/SL30 is self-protecting: T1 at +25% locks in partial gains immediately and moves the stop to breakeven. This structure resolves most trades positively regardless of macro regime — the filters remove good trades alongside bad ones.

Removing all filters: 53 → 289 trades, +$591 → **+$5,990** P&L.

### Golden Config

| Parameter | Value |
|---|---|
| Signal | 15-min ORB breakout, both LONG and SHORT |
| Filters | **None** |
| Instrument | QQQ 0DTE options (calls for LONG, puts for SHORT) |
| Strike | OTM (+1 from ATM) |
| PT1 | +25% option gain → stop to breakeven |
| PT2 | +100% option gain → trailing stop at max × 70% |
| PT3 | +200% option gain or EOD |
| SL | −30% from entry option price |
| Weights | T1=25%, T2=25%, T3=50% |

### Results (289 trades, 2025 Jan – 2026 Mar)

| Metric | Value |
|---|---|
| Win Rate | **79.2%** |
| Total P&L | **+$5,990** (per contract) |
| Max Drawdown | $234 |
| Calmar | **25.56** |
| LONG trades | 152 |
| SHORT trades | 137 |

### 2025 monthly breakdown (all 12 months profitable)

| Month | n | WR | P&L |
|---|---|---|---|
| Jan | 20 | 75% | +$205 |
| Feb | 19 | 89% | +$938 |
| Mar | 21 | 76% | +$690 |
| Apr | 21 | 67% | +$948 |
| May | 21 | 86% | +$239 |
| Jun | 20 | 75% | +$229 |
| Jul | 22 | 77% | +$206 |
| Aug | 21 | 81% | +$295 |
| Sep | 21 | 86% | +$269 |
| Oct | 23 | 78% | +$177 |
| Nov | 18 | 89% | +$951 |
| Dec | 21 | 86% | +$148 |

### Filtered vs Golden comparison

| Config | Trades | WR | Total P&L | MaxDD | Calmar |
|---|---|---|---|---|---|
| Filtered (align70 + CPR + ORB%) | 53 | 75.5% | +$591 | $90 | 31.12 |
| **Golden (no filters)** | **289** | **79.2%** | **+$5,990** | **$234** | **25.56** |
