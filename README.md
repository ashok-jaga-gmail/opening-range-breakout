# QQQ 15-Minute Opening Range Breakout — Research Paper

A systematic backtest and regime analysis of the **15-minute Opening Range Breakout (ORB)** strategy on **QQQ (Nasdaq-100 ETF)** covering **2018–2026**, plus multi-timeframe regime filters using CPR, RSI, and MACD.

---

## Overview

The Opening Range Breakout is one of the oldest and most studied intraday patterns. This repository provides:

1. **`orb_paper_backtest.py`** — Pure underlying backtest across 7 exit strategies, 1,976 trading days
2. **`orb_regime_indicators.py`** — Multi-timeframe regime classification (CPR, RSI, MACD) and stratified performance analysis

---

## Strategy Definition

| Parameter | Value |
|---|---|
| **Instrument** | QQQ (Nasdaq-100 ETF) |
| **ORB Window** | 09:30–09:44 ET (15 bars) |
| **Entry Signal** | First bar at/after 09:45 whose CLOSE breaks the ORB high (LONG) or ORB low (SHORT) |
| **Entry Price** | Close of the breakout bar |
| **Stop Loss** | ORB opposite edge (full ORB range = 1R) |
| **Minimum ORB Range** | $0.10 (filters data-gap / holiday sessions) |

### Exit Strategies Compared

| Config | Description |
|---|---|
| **R0.5** | Target = 0.5× ORB range above/below entry |
| **R1** | Target = 1× ORB range (1:1 R/R) |
| **R2** | Target = 2× ORB range (canonical) |
| **R3** | Target = 3× ORB range |
| **EOD** | Hold to 15:59 ET with stop only |
| **T30** | Exit after 30 minutes or stop, whichever first |
| **T60** | Exit after 60 minutes or stop, whichever first |

---

## Data

**Source:** [Databento](https://databento.com) — XNAS.ITCH feed, schema `ohlcv-1m`  
**File:** `xnas-itch-20180501-20260313.ohlcv-1m.dbn.zst`  
**Period:** 2018-05-01 → 2026-03-13  
**Symbol:** QQQ  
**Bars loaded:** 769,808 (RTH only, 09:30–15:59 ET)  
**Trading days:** 1,978  

> **Data not included in this repo.** Purchase from [Databento](https://databento.com) or substitute your own 1-minute OHLCV CSV loader.

---

## Full-Period Results (2018–2026)

### Exit Strategy Comparison (per 1 share)

| Config | Trades | Win Rate | Prof. Factor | Total P&L | Exp/Trade | Max DD | Sharpe | Calmar |
|---|---|---|---|---|---|---|---|---|
| R0.5 | 1,976 | 64.9% | 0.98 | –$23.86 | –$0.012 | $99.00 | –0.13 | –0.03 |
| **R1** | **1,976** | **51.3%** | **1.05** | **+$88.77** | **+$0.045** | **$97.74** | **0.35** | **0.12** |
| **R2** | **1,976** | **42.8%** | **1.13** | **+$242.54** | **+$0.123** | **$63.27** | **0.77** | **0.49** |
| R3 | 1,976 | 41.4% | 1.15 | +$274.34 | +$0.139 | $72.12 | 0.80 | 0.49 |
| EOD | 1,976 | 41.0% | 1.13 | +$248.19 | +$0.126 | $85.39 | 0.71 | 0.37 |
| T30 | 1,976 | 50.2% | 1.01 | +$11.32 | +$0.006 | $60.43 | 0.07 | 0.02 |
| T60 | 1,976 | 50.4% | 1.07 | +$83.14 | +$0.042 | $41.89 | 0.40 | 0.25 |

**Key finding:** R2 and R3 offer the best risk-adjusted returns (Sharpe ~0.80). R0.5 is the only losing configuration. Stop discipline at the ORB edge is critical.

---

### Annual Breakdown — R2

| Year | Trades | Win Rate | P. Factor | P&L | Max DD | Sharpe |
|---|---|---|---|---|---|---|
| 2018 | 169 | 36.1% | 0.83 | –$15.11 | $28.47 | –1.22 |
| 2019 | 251 | 42.2% | 1.10 | +$8.72 | $8.93 | 0.61 |
| 2020 | 253 | 39.5% | 0.86 | –$36.38 | $51.71 | –0.98 |
| 2021 | 252 | 50.0% | 1.31 | +$62.09 | $17.62 | 1.80 |
| 2022 | 250 | 41.2% | 1.08 | +$27.85 | $47.68 | 0.52 |
| 2023 | 250 | 39.6% | 1.10 | +$22.83 | $21.85 | 0.69 |
| 2024 | 252 | 42.9% | 1.11 | +$29.54 | $18.16 | 0.75 |
| 2025 | 250 | 45.6% | 1.31 | +$98.07 | $33.65 | 1.67 |
| 2026 | 49 | 59.2% | 1.83 | +$44.92 | $16.60 | 4.01 |

> 2020 (COVID) and 2018 (late-year crash) were the two losing years. The strategy works best in trending, momentum-driven markets.

---

### Direction: Long vs Short (R2)

| Direction | Trades | Win Rate | P. Factor | Total P&L | Max DD |
|---|---|---|---|---|---|
| **LONG** | 1,026 | 45.5% | 1.17 | +$150.83 | $32.13 |
| SHORT | 950 | 39.9% | 1.10 | +$91.72 | $66.86 |

Long breakouts significantly outperform short breakouts — consistent with QQQ's long-term bullish bias and the tendency for FOMO-driven opening gaps upward.

---

### ORB Range Quartile Analysis (R2)

| Quartile | Trades | Win Rate | P. Factor | Total P&L | Avg Win | Avg Loss |
|---|---|---|---|---|---|---|
| Q1 Narrow | 494 | 38.5% | 1.00 | –$0.75 | +$1.11 | –$0.70 |
| **Q2** | **497** | **46.3%** | **1.37** | **+$129.50** | **+$2.07** | **–$1.30** |
| Q3 | 492 | 42.3% | 1.17 | +$85.40 | +$2.82 | –$1.76 |
| Q4 Wide | 493 | 44.2% | 1.04 | +$28.39 | +$3.79 | –$2.90 |

**Finding:** Q2 ORB range (moderate volatility) is the sweet spot. Narrow ORBs offer poor R/R; wide ORBs have explosive wins but large stops eat into PF.

---

## Regime Indicator Analysis

### Indicator Stack

| Indicator | Timeframes | Source |
|---|---|---|
| **CPR** (Central Pivot Range) | Monthly, Weekly, Daily | Prior period H/L/C |
| **RSI** (14-period, Wilder's) | Weekly, Daily, 4h, 1h, 15m | Closing prices |
| **MACD** (12/26/9 EMA) | Weekly, Daily, 4h, 1h, 15m | Closing prices |

All indicators are computed **strictly from data available before trade entry** — no look-ahead bias.

### CPR Price Position vs Performance (R2)

#### Daily CPR
| Position | Trades | Win Rate | P. Factor | Exp/Trade |
|---|---|---|---|---|
| **Above CPR top** | 997 | 45.8% | **1.23** | +$0.183 |
| Inside CPR | 270 | 41.5% | 1.14 | +$0.133 |
| Below CPR bottom | 708 | 39.0% | 1.03 | +$0.032 |

**Insight:** Breakouts occurring when price is already **above the daily CPR** have significantly higher profit factors — the CPR acts as a confirmed support zone below the breakout.

#### CPR Width
| Width | Trades | Win Rate | P. Factor | Exp/Trade |
|---|---|---|---|---|
| **Narrow** (<0.10%) | 418 | 45.9% | **1.34** | +$0.233 |
| **Normal** | 748 | 44.0% | **1.18** | +$0.152 |
| Wide (>0.30%) | 809 | 40.0% | 1.03 | +$0.037 |

**Insight:** Narrow CPR = trending day ahead. Wide CPR = choppy, reversal-prone. Avoid wide-CPR days.

---

### RSI Regime vs Performance (R2)

#### 1h RSI (strongest discriminator)
| RSI State | Trades | Win Rate | P. Factor | Exp/Trade |
|---|---|---|---|---|
| **Overbought (>70)** | 274 | 47.8% | **1.31** | +$0.199 |
| **Oversold (<30)** | 112 | 45.5% | **1.36** | +$0.471 |
| Bullish (50–70) | 690 | 43.5% | 1.13 | +$0.104 |
| Bearish (30–50) | 459 | 39.2% | 1.05 | +$0.064 |

**Insight:** Both RSI extremes outperform — overbought conditions support long breakouts (momentum continuation), while oversold conditions support short breakouts (momentum reversal into ORB setup).

#### Weekly RSI
| RSI State | Trades | Win Rate | P. Factor | Exp/Trade |
|---|---|---|---|---|
| **Oversold (<30)** | 15 | 46.7% | **2.22** | +$2.28 |
| Overbought (>70) | 280 | 44.6% | 1.27 | +$0.199 |
| Bullish (50–70) | 1,027 | 43.4% | 1.14 | +$0.121 |
| Neutral (45–55) | 340 | 39.4% | 0.90 | –$0.117 |

**Insight:** Weekly neutral RSI (45–55) is the worst environment — avoid ORB trades when the weekly RSI is flat/directionless.

---

### MACD Regime vs Performance (R2)

#### 1h MACD (strongest timeframe)
| MACD State | Trades | Win Rate | P. Factor | Exp/Trade |
|---|---|---|---|---|
| **Bullish** (MACD>signal, rising hist) | 419 | 49.2% | **1.42** | +$0.338 |
| **Bearish cross** (fresh cross below) | 109 | 48.6% | **1.82** | +$0.612 |
| Bullish cross (fresh cross above) | 140 | 49.3% | 1.46 | +$0.390 |
| Bearish fade (MACD<signal, narrowing) | 444 | 38.3% | 0.91 | –$0.092 |

**Insight:** Bullish 1h MACD strongly supports long ORB trades. Bearish crossovers on 1h often mark capitulation — a sharp short ORB follows. **Avoid bearish_fade (MACD underwater but losing momentum) — worst state.**

#### 4h MACD
| MACD State | Trades | Win Rate | P. Factor | Exp/Trade |
|---|---|---|---|---|
| **Bullish cross** | 87 | 54.0% | **1.64** | +$0.458 |
| **Bearish cross** | 92 | 44.6% | **1.65** | +$0.546 |
| Bullish | 435 | 48.7% | 1.31 | +$0.245 |
| **Bullish fade** | 454 | 37.0% | 0.85 | –$0.137 |

**Insight:** 4h MACD crossovers (both directions) are strong entry signals. Bullish_fade on 4h is the worst regime — avoid.

---

### Indicator Alignment Score (R2)

Alignment = % of available indicators that agree with the ORB breakout direction.

| Alignment | Trades | Win Rate | P. Factor | Exp/Trade |
|---|---|---|---|---|
| 0% (all against) | 223 | 42.2% | **1.30** | +$0.284 |
| 20% | 226 | 38.5% | 0.96 | –$0.037 |
| 40% | 606 | 38.0% | 0.92 | –$0.092 |
| 60% | 238 | 41.2% | 1.24 | +$0.225 |
| **80% (most aligned)** | **596** | **50.0%** | **1.35** | **+$0.292** |
| 100% (fully aligned) | 86 | 44.2% | 1.21 | +$0.170 |

**Key finding:** 80% alignment (but not 100%) is the sweet spot. Fully aligned setups may be "too obvious" and cause slippage/crowding. Completely contrary setups also work — likely because extreme counter-trend readings mark turning points.

---

## Regime-Based Trade Filters (Recommended)

Based on the analysis, the highest-quality ORB setups combine:

**BUY THE BREAKOUT WHEN:**
- Price is **above daily CPR top** (confirmed bullish bias)
- Daily CPR width is **narrow or normal** (trending day expected)
- 1h RSI is **overbought** OR **oversold** (avoid neutral 45-55)
- 1h MACD is **bullish** or **bullish_cross**
- 4h MACD is NOT in **bullish_fade**
- Weekly RSI is NOT in **neutral** range (45-55)
- Indicator alignment ≥ 60%

**SELL THE BREAKDOWN WHEN:**
- Price is **below daily CPR bottom**
- 1h MACD shows **bearish_cross** (fresh capitulation)
- 4h MACD **bearish_cross**
- Weekly RSI **bearish or oversold**

---

## Repository Structure

```
opening-range-breakout/
├── orb_paper_backtest.py       # Main backtest: ORB detection + 7 exit strategies
├── orb_regime_indicators.py    # Multi-TF CPR, RSI, MACD computation + stratified analysis
├── README.md                   # This file: full documentation and results
└── ORB.md                      # Research prompts and methodology notes
```

---

## Setup & Usage

### Requirements

```bash
pip install databento
```

### Run the Backtest

```bash
# Step 1: Core backtest (generates /tmp/orb_paper_results.json)
python3 orb_paper_backtest.py

# Step 2: Regime analysis (reads results, annotates with indicators)
python3 orb_regime_indicators.py
```

### Output Files

| File | Contents |
|---|---|
| `/tmp/orb_paper_results.json` | Trade list + stats for all 7 exit configs |
| `/tmp/orb_regime_results.json` | Same trades annotated with CPR/RSI/MACD regime |

### Adapting to Your Data

Edit the `DBN_FILE` path at the top of each script:

```python
DBN_FILE = "/path/to/your/xnas-itch-YYYYMMDD-*.ohlcv-1m.dbn.zst"
```

To use CSV data instead, replace `load_dbn_to_daily_bars()` in `orb_paper_backtest.py` with a CSV reader returning the same `{date_str: [(time_str, o, h, l, c, v), ...]}` dict format.

---

## Key Findings for the Paper

1. **The 15-min ORB has statistically significant edge on QQQ** over 2018–2026 (Sharpe 0.77–0.80 for R2/R3). Not a random walk.

2. **R2 target (2× ORB range) is the optimal exit.** R0.5 destroys edge by exiting too early. R3 marginally improves Sharpe but at cost of fewer target hits (19%).

3. **Long bias dominates.** Longs: +$150.83, Shorts: +$91.72 over the period. Scale short exposure down in bull-trending markets.

4. **Q2 ORB range (moderate) is the sweet spot.** Narrow ORBs → no room. Wide ORBs → stops too large. Target the middle quintile.

5. **CPR above daily top = strongest filter.** Win rate jumps 6 points and PF goes from 1.03 (below CPR) to 1.23 (above CPR).

6. **Narrow/normal CPR width = trending day.** Wide CPR = avoid (PF 1.03 barely positive).

7. **1h and 4h MACD are the most predictive timeframes.** Crossovers in either direction are high-quality signals. Fade states (MACD moving against the trend while still on the right side) are the worst.

8. **RSI extremes (overbought OR oversold) both outperform neutral RSI.** Neutral weekly RSI (45–55) is a regime to avoid entirely (PF 0.90, negative expectancy).

9. **2020 and 2018 were losing years** — COVID-driven volatility and late-2018 crash created whipsaw conditions that violated stop loss reliability. Strategy benefits from calmer trending environments.

10. **80% indicator alignment beats 100%.** Fully aligned setups may attract excessive participation, eroding edge.

---

## Caveats & Limitations

- All results are **per 1 share, no leverage, no transaction costs**. Add spread + commissions for realistic P&L.
- **Slippage** at the 09:45 breakout bar can be significant on high-volatility days — treat close-of-bar entries as optimistic.
- The **data period includes QQQ at vastly different price levels** ($160 in 2018 → $530+ in 2026). ORB range in absolute dollars grows with price. A % normalization might improve cross-period comparison.
- No **regime filter** was applied in the core backtest — it is pure vanilla ORB for scientific validity.
- Regime filters **must be re-verified out-of-sample** before live trading.

---

## License

MIT — free to use, share, and modify. Not financial advice.
