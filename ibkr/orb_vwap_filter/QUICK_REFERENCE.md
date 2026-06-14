# ORB + VWAP Bot Quick Reference

## Quick Commands

```bash
# Test setup
./test_orb_vwap_filter.sh

# Run manually
python3 orb_vwap_filter.py

# View dashboard
open bot.html

# Watch logs
tail -f orb_vwap_filter.log

# Check state
cat bot_state.json | python3 -m json.tool

# Edit config
vim orb_vwap_filter.py  # Edit variables at top

# Setup cron
crontab -e
# Add: * * * * * /Users/ashok/.claude-worktrees/rekemend/sharp-ishizaka/ibkr/orb_vwap_filter/orb_vwap_filter.sh
```

## Key Settings (in orb_vwap_filter.py)

| Setting | Default | Description |
|---------|---------|-------------|
| `SYMBOL` | QQQ | Ticker to trade |
| `OPENING_RANGE_START_HOUR` | 9 | Range start (EST) |
| `OPENING_RANGE_START_MINUTE` | 30 | Range start minute |
| `OPENING_RANGE_END_HOUR` | 9 | Range end (EST) |
| `OPENING_RANGE_END_MINUTE` | 45 | Range end minute |
| `TRADING_START_HOUR` | 9 | Trading start (EST) |
| `TRADING_START_MINUTE` | 45 | Trading start minute |
| `TRADING_END_HOUR` | 13 | Trading end (EST) |
| `TRADING_END_MINUTE` | 0 | Trading end minute |
| `CONTRACTS_PER_TRADE` | 3 | Position size |
| `OTM_STRIKES_OUT` | 3 | Strikes OTM |
| `MAX_DAILY_RISK` | 1000 | Max daily loss $ |
| `STOP_LOSS_PERCENT` | 50 | SL % |
| `HYBRID_STOP_ENABLED` | True | Use hybrid SL |
| `POSITION_REVERSAL_ALLOWED` | False | No reversal |
| `CPR_LEVEL_1_PROFIT_PERCENT` | 50 | First exit % |
| `CPR_LEVEL_2_PROFIT_PERCENT` | 100 | Second exit % |
| `CPR_LEVEL_3_PROFIT_PERCENT` | 150 | Third exit % |
| `REENTRY_WAIT_MINUTES` | 15 | Wait after stop |
| `VWAP_FILTER_STRICT` | True | Enforce VWAP filter |
| `IBKR_CLIENT_ID` | 21 | Must be unique |

## Trading Logic

### Entry (ALL must be true)
- ✓ Post-opening range (after 9:45 AM EST)
- ✓ Trading hours (9:45 AM - 1:00 PM EST)
- ✓ Range captured (9:30-9:45 AM)
- ✓ Breakout detected (price > range_high + price > VWAP OR price < range_low + price < VWAP)
- ✓ Daily risk not exceeded
- ✓ Not in re-entry wait
- ✓ No existing position

### Exit Triggers
- **Stop Loss**: 50% of entry
- **Profit**: 50%, 100%, 150% (scale out)
- **Opposite Breakout**: Exit if price breaks opposite side
- **EOD**: 1:00 PM EST

## Files

| File | Purpose | Generated |
|------|---------|-----------|
| `orb_vwap_filter.py` | Main bot | Manual |
| `orb_vwap_filter.sh` | Cron runner | Manual |
| `bot_state.json` | State | Auto |
| `bot.html` | Dashboard | Auto |
| `orb_vwap_filter.log` | Logs | Auto |

## Dashboard Sections

1. **Bot Status** - Running/Stopped, symbol, price, trading hours, range period
2. **Opening Range & Indicators** - Range high/low, range size, VWAP, breakout price
3. **Position Details** - Type, contracts, entry/current price, P&L
4. **Open Orders** - Stop loss orders
5. **P&L Summary** - Realized, unrealized, total, risk remaining
6. **Last Action** - Latest bot message

## Troubleshooting

| Issue | Check |
|-------|-------|
| No entries | Range captured, VWAP calculated, breakout + VWAP alignment, risk limit |
| Orders fail | IBKR permissions, account balance, connection |
| Dashboard stale | Cron running, logs for errors |
| State corrupted | Reset bot_state.json |

## State File Fields

```json
{
  "position": "call|put|null",
  "entry_price": 12.50,
  "contracts_remaining": 3,
  "stop_loss_price": 6.25,
  "daily_pnl": 150.00,
  "opening_range_high": 520.50,
  "opening_range_low": 518.20,
  "range_captured": true,
  "current_vwap": 519.35,
  "vwap_at_entry": 519.00,
  "breakout_price": 520.75,
  "last_action": "message"
}
```

## Safety

- 🧪 Test in PAPER mode first
- 👀 Monitor actively
- 💰 Start small
- 📊 Review logs daily
- ⚠️ 0DTE options are very risky

## Typical Day

```
Pre-market:
- Check bot.html shows "AFTER TRADING HOURS"
- Review yesterday's logs
- Verify IBKR connection

9:30 AM EST:
- Bot captures opening range (9:30-9:45 AM)
- Range should be visible in dashboard by 9:45 AM

9:45 AM EST:
- Bot auto-starts trading
- Watch for breakout signal
- Monitor bot.html

During hours:
- tail -f orb_vwap_filter.log
- Refresh bot.html
- Check positions in IBKR

1:00 PM EST:
- Bot auto-closes positions
- Stops trading
- Review P&L
```

## Emergency Procedures

### Stop Trading Immediately
1. Comment out cron job: `crontab -e`
2. Close positions manually
3. Cancel all orders

### Reset Bot
1. Stop cron
2. `rm bot_state.json`
3. `rm bot.html`
4. `python3 orb_vwap_filter.py` to regenerate

---

**Last Updated**: 2026-01-17
**Version**: 1.0
