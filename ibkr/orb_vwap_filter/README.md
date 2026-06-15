> **Disclaimer:** Not Financial Advice, educational purposes only

# ORB + VWAP Filter Bot

## Overview

A fully automated 0DTE options trading bot based on the Opening Range Breakout (ORB) strategy.

> **Note:** the VWAP entry filter is **disabled by default** (`VWAP_FILTER_STRICT = False`).
> An ablation over 2024–2026 showed it removes <3% of trades — it is redundant with the
> OR-high breakout — and is net-neutral for calls / net-negative when trading both
> directions. VWAP is still computed and shown on the dashboard. Set the flag to `True`
> to restore the original VWAP-confirmed entries.

## Strategy

- **Opening Range**: Captures 15-minute range (9:30-9:45 AM EST)
- **Entry**: Breakout above range high (calls) OR below range low (puts). *(VWAP confirmation optional — off by default.)*
- **VIX regime gate**: calls skipped when VIX opens > 25; puts only when VIX opens above its
  prior-day pivot **and** ≥ 18 (uses the VIX open → no look-ahead). On by default.
- **Exit**: CPR-based profit taking at 50%, 100%, 150%
- **Stop Loss**: Hybrid approach - 50% of entry price
- **Position Management**: 3 contracts, scale out 1/3 at each profit level
- **No Reversal**: Exits on opposite breakout, waits for re-entry

> **Shipped vs. research-optimal:** the **VIX regime gating is now wired into the bot**
> (calls/puts gated by the VIX open, above). The *rest* of the research-optimal config —
> **OTM-1 strike, +20% target, −75% stop, single tranche** — is **not** yet: the bot still
> ships with the [150/100/50] ladder and −50% stop. So the live bot won't reproduce the
> [Backtest](#backtest-2025--2026) figures until those exit parameters are also adopted.

## Key Features

- **Timezone**: EST (9:30 AM - 1:00 PM EST trading window)
- **Opening Range**: 9:30-9:45 AM EST
- **Trading Hours**: 9:45 AM - 1:00 PM EST (post-range)
- **VWAP Filter**: Optional, **off by default** (computed for the dashboard; not used to gate entries)
- **VIX Regime Gate**: on by default — calls stand down on panic opens (VIX > 25); puts only on genuine fear opens (VIX open > pivot & ≥ 18)
- **Risk Management**: Max $1,000 daily loss, 15-min re-entry cooldown
- **State Persistence**: Fully stateless design with JSON state file
- **HTML Dashboard**: Auto-updating dashboard with range/VWAP metrics
- **Cron Ready**: Runs every minute via shell script

## Entry Conditions (ALL must be true)

1. Post-opening range (after 9:45 AM EST)
2. Trading hours (9:45 AM - 1:00 PM EST)
3. Range successfully captured
4. Breakout detected **and VIX regime gate passes** (gate active when `VIX_GATING_ENABLED = True`):
   - **Calls**: price > range_high, and VIX open ≤ 25 *(AND price > VWAP only if `VWAP_FILTER_STRICT = True`)*
   - **Puts**: price < range_low, and VIX open > prior-day pivot & ≥ 18 *(AND price < VWAP only if `VWAP_FILTER_STRICT = True`)*
5. No existing position
6. Risk limit not exceeded ($1,000 max daily loss)
7. Not in re-entry cooldown (15 minutes after stop out)

## Exit Conditions

1. **CPR Profit Taking**: Scale out 1/3 at 50%, 100%, 150% profit
2. **Hybrid Stop Loss**: 50% of entry price
3. **Opposite Breakout**: Exit all if price breaks opposite side of range
4. **End of Day**: Force close all positions at 1:00 PM EST

## Configuration

All settings in `orb_vwap_filter.py`:

```python
# Opening Range Period (EST)
OPENING_RANGE_START_HOUR = 9
OPENING_RANGE_START_MINUTE = 30
OPENING_RANGE_END_HOUR = 9
OPENING_RANGE_END_MINUTE = 45

# Trading Hours (EST)
TRADING_START_HOUR = 9        # 9:45 AM EST
TRADING_START_MINUTE = 45
TRADING_END_HOUR = 13         # 1:00 PM EST
TRADING_END_MINUTE = 0

# Position Settings
CONTRACTS_PER_TRADE = 3
OTM_STRIKES_OUT = 3

# VIX Regime Gating (uses the VIX open; no look-ahead)
VIX_GATING_ENABLED = True
VIX_CALL_MAX_OPEN = 25.0   # skip calls when VIX opens above this
VIX_PUT_MIN_OPEN = 18.0    # puts only when VIX open >= this AND above its prior-day pivot

# Risk Management
MAX_DAILY_RISK = 1000
STOP_LOSS_PERCENT = 50
HYBRID_STOP_ENABLED = True
POSITION_REVERSAL_ALLOWED = False

# CPR Profit Levels
CPR_LEVEL_1_PROFIT_PERCENT = 50
CPR_LEVEL_2_PROFIT_PERCENT = 100
CPR_LEVEL_3_PROFIT_PERCENT = 150

# Re-entry Wait
REENTRY_WAIT_MINUTES = 15
```

## Files

- **orb_vwap_filter.py**: Main bot (800+ lines)
- **orb_vwap_filter.sh**: Cron runner script
- **backtest_vwap_filter.py** / **BACKTEST_2025_2026.md**: live-bot backtest + methodology
- **grid_search.py**, **grid_search_nocpr.py** / **GRID_SEARCH.md**: configuration search
- **equity_curve.py** / **equity_curve.png**: recommended-config equity curve (uses `vix_daily.csv`)
- **vix_daily.csv**: CBOE VIX daily OHLC (2023–2026) for the VIX gates
- **cpr_reach_study.py**, **mae_mfe_study.py**, **STRATEGY.md**: supporting research
- **bot_state.json**, **bot.html**, **orb_vwap_filter.log**: runtime artifacts (auto-generated)

## Backtest (2025 & 2026)

A **look-ahead-free** historical replay on **real QQQ 0DTE option 1-minute prices** —
signals on the completed bar's close, fills at the next bar's open. Figures below are for
the **recommended VIX-gated strategy** (calls + puts, +20% scalp — see the next section
and [GRID_SEARCH.md](GRID_SEARCH.md)), at **10 contracts** (with a $4,000 daily-loss
circuit breaker), **net of $0.65/contract/side**. The original live-bot config
([150/100/50] ladder, −50% stop) is documented separately in
[BACKTEST_2025_2026.md](BACKTEST_2025_2026.md).

| | 2025 (full year) | 2026 (Jan–Jun 12) |
|---|---:|---:|
| Trades | 263 | 100 |
| Win rate | 74.5% | 80.0% |
| Total P&L | +$13,199 | +$19,318 |
| Profit factor | 1.29 | 2.27 |

Net-positive in **both** years after costs, with a ~75–80% win rate — the VIX gates carry the
calls in calm/trending vol and switch the puts on only in genuine fear. Still a
**long-biased** strategy harvesting the 2025–26 uptrend; validate on a down market before
risking capital.

### Recommended config — portfolio equity curve (10 contracts)

The configuration grid search ([GRID_SEARCH.md](GRID_SEARCH.md)) found a CPR-free
**+20% scalp** (`OTM-1 · +20% target · −75% stop · OR ≥ $1 · 3 trades/day`) that is
net-positive (after commissions) in both years. (+20% beats the original +30% once the
VIX/direction filters are in place — the strike × target × stop grid was re-checked with
the filters on; OTM-1 stays optimal, the target tightens to +20%.)

VIX (from [`vix_daily.csv`](vix_daily.csv), pivot = `(prevH+prevL+prevC)/3`) gates **both**
sides, using only the **VIX open** (known at 9:30 → no look-ahead):

- **Calls** — upside opening-range breakout, **skipped when VIX opens > 25** (the panic
  regime: calls lose ~$69/day at a 36% win rate there — it's the entire drag on the call side).
- **Puts** — taken **only when VIX opens above its pivot AND VIX open ≥ 18** (a genuine
  risk-off open). Unfiltered puts lose every year and even pivot-gated puts bleed in low
  vol; shorts only earn their keep at VIX ≥ ~18, so the floor removes "false fear" days.

The two gates are complementary: calls carry the calm/trending regimes, puts hedge the
high-vol fear regime where calls fail. Sized at **10 contracts** with a **$4,000 daily-loss
circuit breaker** — it sits above the worst realized day (−$2,486), so it never actually
binds here and is only a tail backstop. Net of $0.65/contract/side — reproduce with
[`equity_curve.py`](equity_curve.py):

![Portfolio equity curve and drawdown — VIX-gated calls + puts, 10 contracts](equity_curve.png)

| | 2025 | 2026 (Jan–Jun) | Combined |
|---|---:|---:|---:|
| **Net P&L** | **+$13,199** | **+$19,318** | **+$32,516** |
| Trades / days | 263 / 169 | 100 / 66 | — |
| Day win rate | 68% | 76% | — |

Progression of the combined 2-year result as each gate is added (at the +30% target used
during the gate search): **+$14,539** (calls only) → **+$18,952** (puts: VIX open > pivot)
→ **+$23,337** (+ VIX-open ≥ 18 put floor) → **+$28,176** (+ skip calls when VIX open > 25).
Finally, re-optimising the exit with all gates on (target **+30% → +20%**) gives the
**+$32,516** above.

⚠️ Still a **long-biased** strategy harvesting the 2025–26 QQQ uptrend; the VIX gate adds a
short side only on risk-off days. It was ≈ breakeven-to-negative in 2024 and would likely
underperform in a sustained down market. Validate out-of-sample before risking capital.

## Quick Start

```bash
# 1. Navigate to bot directory
cd /path/to/ibkr/orb_vwap_filter

# 2. Test setup
./test_orb_vwap_filter.sh

# 3. Run manually
python3 orb_vwap_filter.py

# 4. View dashboard
open bot.html

# 5. Set up cron
crontab -e
# Add: * * * * * /path/to/orb_vwap_filter/orb_vwap_filter.sh
```

## Monitoring

```bash
# Watch logs
tail -f orb_vwap_filter.log

# Check state
cat bot_state.json | python3 -m json.tool

# View dashboard
open bot.html  # Auto-refreshes every 60 seconds
```

## Safety

- Test in paper trading for at least 1 week
- Monitor actively during market hours
- Understand all configuration options
- Start with small position sizes
- 0DTE options are extremely risky

## Support

See [../SETUP.md](../SETUP.md) for IB Gateway + Python setup instructions.
See [QUICK_REFERENCE.md](QUICK_REFERENCE.md) for command reference.

---

**Version**: 1.0
**Created**: 2026-01-17
**Trading Hours**: 9:45 AM - 1:00 PM EST
**Opening Range**: 9:30-9:45 AM EST
