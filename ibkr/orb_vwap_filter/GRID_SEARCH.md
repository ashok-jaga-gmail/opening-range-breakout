# ORB + VWAP Filter — Configuration Grid Search

Optimization of the strategy's key knobs with the **same no-look-ahead engine** as the
backtest (signals on a completed bar's close, fills at the next bar's open, only the
resting stop fills intrabar). Reproduced by [`grid_search.py`](grid_search.py).

**Train on 2025 (full year), validate on 2026 (Jan 2 – Jun 12).** P&L is per the full
position, in dollars, **before commissions/slippage.**

## Search space (1,240 configs)

| Knob | Values |
|---|---|
| Trades / day | 1, 2, 3, unlimited |
| Contracts (≤9) | tranches × {1,2,3} → 1,2,3,4,6,8,9 (even split) |
| Tranches (≤4) | 1, 2, 3, 4 |
| Profit targets | **%-premium** ladders e.g. `[50,150]`, `[50,100,150]` **·or· CPR-level** ladders e.g. `[r1,r2]`, `[tc,r1,r2]` (underlying reaching pivot levels) |
| Stop loss % | 30, 40, 50, 60, 75 |

---

## The headline finding: nothing generalizes

> **0 of 1,240 configurations were profitable in *both* 2025 and 2026.**

Every config that makes money in 2025 loses in 2026. The handful that lose only a little
in 2026 do so by barely trading (1 contract, 75% stop → rarely triggers). This is the
classic signature of an **overfit, regime-dependent** strategy: 2025 had tradeable trend
months, 2026 (so far) did not, and no parameter set bridges them.

## Best configurations (ranked by combined 2025+2026 P&L)

| Config | 2025 $ | 2025 PF | 2026 $ | 2026 PF | Combined |
|---|---:|---:|---:|---:|---:|
| **tpd=∞, 6 ctr, 2 tr, %[50,150], SL 50** | +4,820 | 1.09 | −4,054 | 0.84 | **+766** |
| tpd=∞, 6 ctr, 3 tr, %[50,100,150], SL 50 | +4,986 | 1.09 | −4,511 | 0.84 | +475 |
| tpd=3, 6 ctr, 2 tr, %[50,150], SL 50 | +4,479 | 1.08 | −4,090 | 0.84 | +389 |
| tpd=∞, 6 ctr, 2 tr, %[50,100], SL 40 | +5,348 | 1.10 | −5,335 | 0.79 | +13 |

Only **four** configs even net positive *combined* — and they do so only because a full
year of 2025 gains outweighs a partial year of 2026 losses. **Every one still loses money
in 2026**, and all are before costs.

### "Best" config — train vs. validation

`trades/day = unlimited · contracts = 6 · tranches = 2 · targets = +50% / +150% · SL = 50%`

| | Trades | Win rate | Total $ | PF | Max DD | Calmar |
|---|---:|---:|---:|---:|---:|---:|
| 2025 (train) | 481 | 41.0% | **+4,820** | 1.09 | −3,502 | 1.38 |
| 2026 (validate) | 154 | 42.9% | **−4,054** | 0.84 | −6,334 | −0.64 |

Note this two-tranche `[50,150]` split lifts the win rate to ~41% (vs. 25% for the live
bot's `[150,100,50]` ladder) by banking half the position at +50% — but the validation
year still collapses.

---

## What each knob is worth (marginal effects)

Averaged over all other dimensions. PF = profit factor (>1 = profitable).

| Knob | Value | avg 2025 PF | avg 2026 PF | Read |
|---|---|---:|---:|---|
| **Target mode** | **%-premium** | **0.94** | **0.70** | decisively better |
| | CPR levels | 0.45 | 0.31 | **CPR targeting is bad** — levels are often unreachable or already passed |
| **Trades/day** | unlimited | 0.83 | 0.62 | more is better |
| | 3 | 0.82 | 0.61 | |
| | 1 | 0.73 | 0.49 | over-selective concentrates losses |
| **Tranches** | 1–2 | 0.79–0.80 | 0.57 | simpler is better |
| | 3 | 0.75 | 0.56 | 3-way split hurts |
| **Stop loss %** | 40 | 0.82 | 0.54 | best in 2025 |
| | 50 | 0.81 | **0.60** | best balance across both |
| | 30 | 0.78 | 0.51 | too tight |
| **Contracts** | 1 | 0.80 | 0.58 | PF ~flat; just scales $ |
| | 9 | 0.74 | 0.57 | **worse** — the fixed $1,000 daily cap bites largest size first |

**Takeaways that are stable across both years:**
1. **Use %-premium targets, not CPR levels** — this is the single biggest lever (PF 0.94 vs 0.45).
2. **Stop loss ≈ 50%** is the best-balanced setting.
3. **1–2 tranches** beat 3; keep the scale-out simple.
4. **Don't over-restrict trades/day**; 3-to-unlimited is best.
5. **Contracts is just a scaling knob** with no risk-adjusted edge — and 9 is actively
   counterproductive because the fixed $1,000 daily-loss cap truncates the largest size
   first. Size to risk tolerance, not to P&L.

---

## Reproduce

```bash
cd ibkr/orb_vwap_filter
python3 grid_search.py    # ~75s; prints rankings + marginals, writes grid_search_results.json
```

## Bottom line

The grid search did **not** find a robust profitable configuration. The best knob settings
— **%-premium targets, ~50% stop, 1–2 tranches, 3+ trades/day** — improve the *training*
year (2025 PF up to ~1.12) but **none survive into 2026**, and all figures are before
commissions and slippage. The strategy's apparent edge is **regime-dependent**, not
structural: it pays off in trend-heavy stretches and bleeds otherwise. The actionable
conclusions are about what to *avoid* (CPR targets, tight stops, 9-lot sizing under the
daily cap) rather than a config to deploy. Before risking capital this needs a genuine
edge source (e.g. entry-quality / regime filter), then re-validation on fresh out-of-sample
data with costs included.
