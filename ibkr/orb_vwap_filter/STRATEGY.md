# ORB-Adaptive — A Strategy Built From the MAE/MFE + Grid-Search Insights

> **Update / superseded:** this regime-switched synthesis concluded "no edge," but that was
> before the **direction** lever was isolated. A later CPR-free search found a profitable,
> both-years-positive **calls-only +30% scalp** — see [`GRID_SEARCH.md`](GRID_SEARCH.md)
> (Round 3). The "no skill in the CPR-width regime" finding below still stands; the
> resolution was direction, not regime.

This documents an attempt to **synthesize every insight** from the research into one
strategy, and the honest result of backtesting it (no look-ahead, train 2025 /
validate 2026, before costs). Implementation: [`orb_adaptive.py`](orb_adaptive.py).

## The insights it encodes

| Finding | Mechanism in the strategy |
|---|---|
| A −50% premium stop shakes out 83% of trades on premium noise | Drop the tight premium stop; use **regime-specific exits** + loose −85% catastrophe only |
| Optimum exit **inverts by year** (2025 ride to +100%, 2026 scalp +30%) | **Regime switch** on CPR width: trend→ride +100% no-stop, chop→scalp +30%/−75% |
| Next-CPR **too close = tiny reward**, move runs further | **Room filter**: skip unless next CPR ≥ 0.40 × OR range |
| Wider OR → bigger MFE; dead days are noise | **OR floor** ≥ $0.50 |
| 0DTE decays to zero; winners peak early (~25–50 min) | Intraday **force-flat at 13:00** |
| ATM ≈ OTM; OTM slightly cheaper/higher MFE | **OTM-2 strike** |

## Results (per position, before costs)

| Variant | 2025 $ | PF | WR | 2026 $ | PF | WR | Combined |
|---|---:|---:|---:|---:|---:|---:|---:|
| ride-all (+100 / no stop) | −3,227 | 0.90 | 38% | −3,949 | 0.70 | 38% | −7,176 |
| scalp-all (+30 / −75) | −3,419 | 0.86 | 59% | −277 | 0.97 | 64% | −3,696 |
| **REGIME switch** | −4,334 | 0.85 | 48% | −1,816 | 0.83 | 55% | −6,150 |
| *reference: live bot [150/100/50, −50%]* | *+2,892* | *1.09* | *41%* | *−3,426* | *0.84* | *43%* | *−534* |
| *reference: grid best [6c,2tr,50/150,SL50]* | *+4,820* | *1.09* | *41%* | *−4,054* | *0.84* | *43%* | *+766* |

**The constructed strategy underperforms the baseline in every variant.** The components
added from the insights — room filter, CPR-width regime, OTM-2, loose catastrophe stop —
were net **harmful** versus the simple kernel. Only "small target + loose stop" (the
scalp-all variant) helped, and only in 2026 (to near-breakeven).

## Why the regime switch failed: the CPR signal has no skill

The whole synthesis hinged on a regime signal that separates trend days (ride) from chop
days (scalp). Direct skill test — 2025 daily P&L of the ride policy, split by predicted
regime:

| CPR-width regime | n | mean daily $ | median daily $ |
|---|---:|---:|---:|
| narrow (predicted **trend**) | 118 | **−$28** | −$64 |
| wide (predicted **chop**) | 115 | **$0** | −$57 |

If the signal had skill, narrow should dominate wide. They are **statistically identical**.
CPR width does not predict follow-through. (The repo's underlying-ORB research found CPR
width helpful on the *underlying*; it does **not** survive translation to 0DTE option P&L,
where theta dominates.)

## Honest verdict

Across four escalating attempts — a 1,240-config grid, a 2,112-config expanded grid, an
MAE/MFE-informed redesign, and this regime-switched synthesis — **no configuration is
robustly profitable in both years before costs**, and the one regime signal tested has no
skill. The conclusion is consistent and now well-supported:

> The ORB+VWAP 0DTE setup has **no structural edge**. Its profitability is regime-
> dependent noise: it pays in trend-heavy stretches (2025) and bleeds otherwise (2026),
> and the available day-classification signals cannot tell the two apart in advance.

## If you nonetheless want to trade it

The least-bad, simplest configuration found anywhere in the search — and the only kernel
that helped the hard year — is **not** this elaborate strategy but a plain scalp:

- **One trade/day, single tranche, OTM strike, +30% target, −75% stop, flat by 13:00.**
- Grid result (1 contract): 2026 **+$115** (PF 1.05, 66% win), 2025 −$759 (PF 0.87).
- It is **near-breakeven at best and dies to commissions** (~$1.30/contract round-trip
  vs ~$1.40 gross edge). Treat it as "not worth trading," not "the answer."

The genuine path forward is a **real entry edge** (an external signal that actually
predicts follow-through — order-flow, breadth, news/catalyst, or a multi-day trend
context), not further tuning of exits on this setup. Every exit-side lever has now been
exhausted.

## Reproduce

```bash
cd ibkr/orb_vwap_filter
python3 orb_adaptive.py     # variant comparison + CPR-width chop threshold (trained on 2025)
```
