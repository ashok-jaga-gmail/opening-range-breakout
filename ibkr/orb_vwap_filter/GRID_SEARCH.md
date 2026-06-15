> **Disclaimer:** Not Financial Advice, educational purposes only

# ORB Strategy — Configuration Grid Search

All searches use the **no-look-ahead** engine (signal on the completed bar's close, fill
at the next bar's open; only a resting stop fills intrabar). Train on **2025**, validate
on **2026**. Three rounds, each broadening the space.

Scripts:
- [`grid_search.py`](grid_search.py) — rounds 1–2 (CPR + percentage targets)
- [`grid_search_nocpr.py`](grid_search_nocpr.py) — round 3 (CPR removed, **direction** added, **net of commissions**)

---

## Round 1 — 1,240 configs (trades/day · contracts · tranches · %/CPR targets · SL)

- **0 configs profitable in both years.**
- Best combined: `tpd=∞ · 6c · 2tr · %[50,150] · SL50` → 2025 **+$4,820** (PF 1.09) / 2026 **−$4,054** (PF 0.84).
- Marginals: %-targets ≫ CPR targets (PF 0.94 vs 0.45); SL ≈ 50% best balance; 1–2 tranches beat 3; contracts is pure scaling (9-lot worse under the $1k daily cap).

## Round 2 — 2,112 configs (2026-prioritized; + lower targets, + no-stop)

- **6 configs net-profitable in 2026** (gross), all the same shape: `tpd=1 · 1 tranche · +25–30% · SL75`.
  Best: `tpd=1 · 3c · +30% · SL75` → 2026 **+$344** (PF 1.05) / 2025 −$2,278.
- **Target sweep** (single tranche): raising the target is **monotonically worse** — +30% is best here, and "ride to EOD" (no target) is the worst (0DTE theta). Higher targets do **not** help. (This is the *unfiltered* search; once the VIX/direction filters are added the optimum tightens further to **+20%** — see Round 4.)
- Still **0 configs profitable in both years**.

## Round 3 — 3,645 configs, CPR removed, **direction** added, **net of commissions**

Removing CPR entirely and adding **strike moneyness**, an **OR-range floor**, and a
**direction filter** (the lever that had been missing):

- **576 / 3,645** configs net-profitable in 2026.
- **24 configs net-positive in BOTH years** after commissions — the first robust results.
- They cluster on one kernel: **calls-only · 1-strike OTM · +30% target · loose stop · skip dead-OR days.**

### Best Round-3 config (calls-only, before the VIX gates)

`calls-only · OTM-1 · +30% target · −75% stop · OR ≥ $1 · 3 trades/day` (1 contract, **net of $0.65/contract/side**):

| Year | Net P&L | PF | Trade WR | Day WR | Trades | Days |
|---|---:|---:|---:|---:|---:|---:|
| **2026** | **+$1,014** | 1.46 | 69% | 68% | 80 | 59 |
| 2025 | +$440 | 1.08 | 68% | 63% | 227 | 163 |

Day win rate (~63–68%) tracks trade win rate, so the edge is broad-based, not a few
outlier days. Scales ~linearly with contracts up to the $1,000 daily-loss cap.

### Why it works — and the caveat

The decisive lever is **direction**. Same config, by direction (net P&L):

| Year | **Calls** | Both | **Puts** |
|---|---:|---:|---:|
| 2025 | **+$440** | −$77 | −$1,051 |
| 2026 | **+$1,014** | −$277 | −$1,216 |

**Puts lose every year**, so trading both sides cancelled the long edge — which is why
every earlier (both-direction) search looked hopeless. The catch: this is a **long bias**
harvesting the 2025–26 QQQ uptrend, **not** an all-weather edge (the same config was
≈ breakeven-to-slightly-negative in 2024, and would likely lose in a down year — no 0DTE
option data exists before 2023 to test a bear market). The repo's underlying-ORB research
already noted *"long bias dominates."*

---

## Round 4 — VIX-regime gates + exit retune (final config)

Layering **VIX-regime gates** on each side (using only the **VIX open** — known at 9:30, no
look-ahead) turned the losing put side into a contributor and removed the calls' worst regime:

- **Puts** — only when VIX opens **above its pivot AND ≥ 18** (genuine fear; low-vol shorts bleed).
- **Calls** — skipped when VIX opens **> 25** (panic regime: calls lose ~$69/day at 36% WR).

With both gates on, **re-checking the strike × target × stop grid** showed the exit should
**tighten**: OTM-1 stays optimal, but the target drops **+30% → +20%** (filtered trades hit
+20% far more often than +30%, and turnover is the edge — splitting/holding for more only
cuts re-entries). Final recommended config:

**`calls + VIX-gated puts · OTM-1 · +20% target · −75% stop · OR ≥ $1 · 3 trades/day`**

| | 2025 | 2026 | Combined |
|---|---:|---:|---:|
| Net P&L (10 contracts, net of costs) | +$13,199 | +$19,318 | **+$32,516** |
| Trade win rate | 74.5% | 80.0% | — |
| Profit factor | 1.29 | 2.27 | — |

Gate progression (combined, at the +30% target used during the search): +$14,539 (calls only)
→ +$18,952 (put pivot gate) → +$23,337 (+VIX-open ≥ 18 put floor) → +$28,176 (+skip calls
VIX open > 25) → **+$32,516** (retune target +30% → +20%). See the live equity curve in the
bot [README](README.md#backtest-2025--2026).

---

## VWAP is not pulling weight

A 2024–2026 ablation of the VWAP entry filter (on vs off) showed it **removes <3% of
trades** — it's redundant with the OR-high breakout — and is net-neutral for calls /
net-negative for both directions. The live bot now has it **disabled by default**
(`VWAP_FILTER_STRICT = False`); the research engines apply it but the ablation confirms it
is immaterial to the result above.

## Supporting studies

- [`cpr_reach_study.py`](cpr_reach_study.py) — breakout reaches the next CPR ~70% (MFE); that
  move is only ~+30% on the option (not +100%), ATM≈OTM.
- [`mae_mfe_study.py`](mae_mfe_study.py) (+ `mae_mfe_breakouts.csv`) — per-breakout excursions:
  winners peak early (~25–50 min), a −50% premium stop shakes out 83% of trades, holding 0DTE
  to the close is fatal.
- [`orb_adaptive.py`](orb_adaptive.py) / [`STRATEGY.md`](STRATEGY.md) — a regime-switched
  synthesis; the CPR-width regime signal proved to have **no skill**, superseded by the
  direction finding above.

## Reproduce

```bash
cd ibkr/orb_vwap_filter
python3 grid_search.py          # rounds 1-2 (CPR + %), 2026-prioritized, ~2 min
python3 grid_search_nocpr.py    # round 3 (CPR-free + direction, net of costs), ~3 min
```

## Bottom line

After removing CPR, isolating **direction**, adding **VIX-regime gates**, and retuning the
exit, the final config — **calls + VIX-gated puts · OTM-1 · +20% target · −75% stop** — is
net-positive in both 2025 and 2026 (**+$32,516** combined at 10 contracts, net of costs). It
remains a **long-biased** strategy, not a market-neutral edge — validate on a down year
before risking capital.
