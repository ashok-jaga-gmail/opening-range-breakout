#!/usr/bin/env python3
"""
Opening Range Breakout (ORB) Bot with VWAP Filter
Trades 0DTE options based on 15-minute opening range breakouts with VWAP confirmation
"""

from ib_insync import IB, Index, Option, Stock, util, MarketOrder, StopOrder, LimitOrder, TagValue
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import json
import os
import argparse

# ========================================
# CONFIGURATION VARIABLES (All configurable)
# ========================================

# Opening Range Period (EST)
OPENING_RANGE_START_HOUR = 9
OPENING_RANGE_START_MINUTE = 30
OPENING_RANGE_END_HOUR = 9
OPENING_RANGE_END_MINUTE = 45

# Trading hours (EST - trading starts AFTER opening range capture)
TRADING_START_HOUR = 9        # Start trading at 9:45 AM EST (after range captured)
TRADING_START_MINUTE = 45
TRADING_END_HOUR = 13         # End at 1:00 PM EST
TRADING_END_MINUTE = 0

# ORB Settings
OPENING_RANGE_MINUTES = 15    # 9:30-9:45 AM EST
VWAP_FILTER_STRICT = True     # Enforce VWAP filter (only long if price > VWAP, short if < VWAP)
BAR_SIZE_OPENING_RANGE = '1 min'  # 1-minute bars for range capture
BAR_SIZE_VWAP = '1 min'       # 1-minute bars for VWAP calculation

# Position Settings
CONTRACTS_PER_TRADE = 3
OTM_STRIKES_OUT = 3  # How many strikes OTM to trade

# Risk Management
MAX_DAILY_RISK = 1000  # Maximum $ loss per day
STOP_LOSS_PERCENT = 50  # Stop loss at 50% of entry price
HYBRID_STOP_ENABLED = True  # Use hybrid stop (50% OR range boundary, whichever closer)
POSITION_REVERSAL_ALLOWED = False  # No reversal - exit and wait for re-entry

# CPR (Central Pivot Range) - Profit taking levels
CPR_LEVEL_1_PROFIT_PERCENT = 50   # Close 1 contract at 50% profit
CPR_LEVEL_2_PROFIT_PERCENT = 100  # Close 1 contract at 100% profit
CPR_LEVEL_3_PROFIT_PERCENT = 150  # Close 1 contract at 150% profit

# Re-entry after stop out
REENTRY_WAIT_MINUTES = 15  # Wait 15 minutes before re-entering after stop out

# IBKR Connection
IBKR_HOST = '172.31.9.221'
IBKR_PORT = 4001
IBKR_CLIENT_ID = 21  # Different from EMA bot (20)

# Symbol Settings
SYMBOL = 'QQQ'
CONTRACT_TYPE = 'Stock'  # 'Stock' or 'Index'
TRADING_CLASS = ''  # Use '' for QQQ, 'SPXW' for SPX
EXCHANGE = 'SMART'  # 'SMART' for stocks, 'CBOE' for indices

# File paths
STATE_FILE = 'bot_state.json'
OUTPUT_HTML = 'bot.html'


# ========================================
# HELPER FUNCTIONS
# ========================================

def compute_vwap(bars):
    """
    Calculate Volume-Weighted Average Price
    Returns: Current VWAP value
    """
    if not bars or len(bars) == 0:
        return None

    cumulative_volume_price = 0
    cumulative_volume = 0

    for bar in bars:
        typical_price = (bar.high + bar.low + bar.close) / 3
        volume_price = typical_price * bar.volume
        cumulative_volume_price += volume_price
        cumulative_volume += bar.volume

    if cumulative_volume == 0:
        return None

    return cumulative_volume_price / cumulative_volume


def capture_opening_range(bars, range_start_time, range_end_time):
    """
    Extract high/low from 9:30-9:45 AM EST opening range
    Returns: (range_high, range_low, range_captured_successfully)
    """
    if not bars:
        return None, None, False

    # Filter bars within the opening range period
    range_bars = [bar for bar in bars
                  if range_start_time <= bar.date <= range_end_time]

    if not range_bars:
        return None, None, False

    range_high = max(bar.high for bar in range_bars)
    range_low = min(bar.low for bar in range_bars)

    return range_high, range_low, True


def check_breakout(current_price, range_high, range_low, vwap, current_position):
    """
    Detect valid breakout with VWAP filter
    Returns: 'call', 'put', 'exit', or None
    """
    # No position - check for entry
    if current_position is None:
        if current_price > range_high and current_price > vwap:
            return 'call'  # Bullish breakout with VWAP confirmation
        elif current_price < range_low and current_price < vwap:
            return 'put'   # Bearish breakout with VWAP confirmation
        return None

    # Have position - check for opposite breakout (exit signal)
    if current_position == 'call':
        if current_price < range_low:
            return 'exit'  # Price broke opposite side
    elif current_position == 'put':
        if current_price > range_high:
            return 'exit'  # Price broke opposite side

    return None


def calculate_hybrid_stop_loss(entry_price, stop_loss_percent=50):
    """
    Calculate stop loss as 50% of entry price
    (Range boundary logic can be added later for more sophistication)
    """
    sl_price = entry_price * (1 - stop_loss_percent / 100)
    return sl_price


def is_post_opening_range(current_time):
    """Check if we're past the opening range period (after 9:45 AM EST)"""
    est_time = current_time.astimezone(ZoneInfo("America/New_York"))
    opening_range_end = est_time.replace(
        hour=OPENING_RANGE_END_HOUR,
        minute=OPENING_RANGE_END_MINUTE,
        second=0,
        microsecond=0
    )
    return est_time >= opening_range_end


def load_state():
    """Load bot state from JSON file"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {
        # Existing fields from EMA bot
        'position': None,  # 'call' or 'put' or None
        'entry_price': None,
        'entry_time': None,
        'contracts_remaining': 0,
        'stop_loss_price': None,
        'daily_pnl': 0.0,
        'daily_trades': 0,
        'last_stop_out_time': None,
        'contracts_closed': [],  # Track which contracts were closed
        'last_action': 'Bot initialized',
        'option_contract': None,  # Store contract details
        # New ORB-specific fields
        'opening_range_high': None,
        'opening_range_low': None,
        'range_captured': False,
        'range_capture_time': None,
        'vwap_at_entry': None,
        'current_vwap': None,
        'breakout_price': None
    }


def save_state(state):
    """Save bot state to JSON file"""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, default=str)


def is_trading_hours():
    """Check if current time is within trading hours (EST)"""
    now = datetime.now(ZoneInfo("America/New_York"))

    # Check if it's a weekday
    if now.weekday() >= 5:  # Saturday or Sunday
        return False

    # Check if within trading hours (9:45 AM - 1:00 PM EST)
    start_time = now.replace(hour=TRADING_START_HOUR, minute=TRADING_START_MINUTE, second=0, microsecond=0)
    end_time = now.replace(hour=TRADING_END_HOUR, minute=TRADING_END_MINUTE, second=0, microsecond=0)

    return start_time <= now <= end_time


def calculate_cpr_levels(prev_high, prev_low, prev_close):
    """Calculate CPR levels for profit taking"""
    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = (pivot - bc) + pivot

    return {
        'pivot': pivot,
        'bc': bc,
        'tc': tc,
        'r1': (2 * pivot) - prev_low,
        'r2': pivot + (prev_high - prev_low),
        's1': (2 * pivot) - prev_high,
        's2': pivot - (prev_high - prev_low)
    }


# ========================================
# HTML GENERATION
# ========================================

def generate_html_dashboard(bot_status, current_price, range_high, range_low,
                            range_captured, vwap, breakout_price, portfolio, open_orders,
                            daily_pnl, unrealized_pnl, total_pnl, action_message):
    """Generate HTML dashboard for ORB+VWAP bot - all values pre-formatted to avoid f-string issues"""

    # Get current time
    now_str = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M:%S %Z")

    # Pre-format ALL values as strings
    current_price_str = "{:,.2f}".format(current_price) if current_price else "N/A"
    range_high_str = "{:.2f}".format(range_high) if range_high else "N/A"
    range_low_str = "{:.2f}".format(range_low) if range_low else "N/A"
    range_size_str = "{:.2f}".format(range_high - range_low) if (range_high and range_low) else "N/A"
    range_status_str = "YES" if range_captured else "NO"
    vwap_str = "{:.2f}".format(vwap) if vwap else "N/A"
    breakout_price_str = "{:.2f}".format(breakout_price) if breakout_price else "N/A"
    daily_pnl_str = "${:.2f}".format(daily_pnl)
    unrealized_pnl_str = "${:.2f}".format(unrealized_pnl)
    total_pnl_str = "${:.2f}".format(total_pnl)
    max_risk_str = "${:.2f}".format(MAX_DAILY_RISK)
    risk_remaining_str = "${:.2f}".format(max(0, MAX_DAILY_RISK + total_pnl))
    opening_range_str = str(OPENING_RANGE_START_HOUR) + ":" + "{:02d}".format(OPENING_RANGE_START_MINUTE) + " - " + str(OPENING_RANGE_END_HOUR) + ":" + "{:02d}".format(OPENING_RANGE_END_MINUTE) + " EST"
    trading_hours_str = str(TRADING_START_HOUR) + ":" + "{:02d}".format(TRADING_START_MINUTE) + " - " + str(TRADING_END_HOUR) + ":" + "{:02d}".format(TRADING_END_MINUTE) + " EST"

    # CSS classes
    bot_status_class = "running" if bot_status == "RUNNING" else "stopped"
    range_status_class = "bullish" if range_captured else "bearish"
    daily_pnl_class = "bullish" if daily_pnl > 0 else ("bearish" if daily_pnl < 0 else "neutral")
    unrealized_pnl_class = "bullish" if unrealized_pnl > 0 else ("bearish" if unrealized_pnl < 0 else "neutral")
    total_pnl_class = "bullish" if total_pnl > 0 else ("bearish" if total_pnl < 0 else "neutral")

    # Build HTML using simple string concatenation
    html = []
    html.append("<!DOCTYPE html>")
    html.append("<html lang=\"en\">")
    html.append("<head>")
    html.append("    <meta charset=\"UTF-8\">")
    html.append("    <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">")
    html.append("    <meta http-equiv=\"refresh\" content=\"60\">")
    html.append("    <title>ORB+VWAP Bot Status</title>")
    html.append("    <style>")
    html.append("        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f5f6f5; color: #1a1a1a; margin: 0; padding: 20px; }")
    html.append("        .container { max-width: 1100px; margin: 0 auto; background: white; padding: 30px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); }")
    html.append("        h1 { color: #0d47a1; text-align: center; margin-bottom: 8px; }")
    html.append("        .timestamp { text-align: center; color: #555; font-size: 1.1em; margin-bottom: 20px; }")
    html.append("        .section { margin-bottom: 30px; }")
    html.append("        h2 { color: #1565c0; border-bottom: 2px solid #e0e0e0; padding-bottom: 8px; margin-bottom: 16px; }")
    html.append("        table { width: 100%; border-collapse: collapse; }")
    html.append("        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #e8ecef; }")
    html.append("        th { background: #e3f2fd; color: #0d47a1; font-weight: 600; }")
    html.append("        .bullish { color: #2e7d32; font-weight: bold; }")
    html.append("        .bearish { color: #c62828; font-weight: bold; }")
    html.append("        .neutral { color: #f57c00; font-weight: bold; }")
    html.append("        .running { color: #2e7d32; font-weight: bold; }")
    html.append("        .stopped { color: #c62828; font-weight: bold; }")
    html.append("        .action-box { background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px; margin-top: 20px; }")
    html.append("    </style>")
    html.append("</head>")
    html.append("<body>")
    html.append("    <div class=\"container\">")
    html.append("        <h1>ORB + VWAP Filter Bot</h1>")
    html.append("        <div class=\"timestamp\">Last updated: " + now_str + "</div>")

    # Bot Status Section
    html.append("        <div class=\"section\">")
    html.append("            <h2>Bot Status</h2>")
    html.append("            <table>")
    html.append("                <tr><th>Item</th><th>Value</th></tr>")
    html.append("                <tr><td>Status</td><td><span class=\"" + bot_status_class + "\">" + str(bot_status) + "</span></td></tr>")
    html.append("                <tr><td>Symbol</td><td>" + str(SYMBOL) + "</td></tr>")
    html.append("                <tr><td>Current Price</td><td>$" + current_price_str + "</td></tr>")
    html.append("                <tr><td>Opening Range Period</td><td>" + opening_range_str + "</td></tr>")
    html.append("                <tr><td>Trading Hours</td><td>" + trading_hours_str + "</td></tr>")
    html.append("            </table>")
    html.append("        </div>")

    # Opening Range & Indicators Section
    html.append("        <div class=\"section\">")
    html.append("            <h2>Opening Range & Indicators</h2>")
    html.append("            <table>")
    html.append("                <tr><th>Indicator</th><th>Value</th></tr>")
    html.append("                <tr><td>Range Captured</td><td><span class=\"" + range_status_class + "\">" + range_status_str + "</span></td></tr>")
    html.append("                <tr><td>Opening Range High</td><td>$" + range_high_str + "</td></tr>")
    html.append("                <tr><td>Opening Range Low</td><td>$" + range_low_str + "</td></tr>")
    html.append("                <tr><td>Range Size</td><td>$" + range_size_str + "</td></tr>")
    html.append("                <tr><td>VWAP</td><td>$" + vwap_str + "</td></tr>")
    html.append("                <tr><td>Breakout Price</td><td>$" + breakout_price_str + "</td></tr>")
    html.append("            </table>")
    html.append("        </div>")

    # Position Section
    html.append("        <div class=\"section\">")
    html.append("            <h2>Position</h2>")
    html.append("            <table>")
    html.append("                <tr><th>Item</th><th>Value</th></tr>")

    if portfolio and any(item.contract.secType == 'OPT' for item in portfolio):
        for item in portfolio:
            if item.contract.secType == 'OPT':
                position_type = "CALL" if item.contract.right == 'C' else "PUT"
                strike_str = "{:.0f}".format(item.contract.strike)
                qty_str = str(abs(int(item.position)))
                entry_price_str = "${:.2f}".format(item.averageCost / 100)
                current_price_str2 = "${:.2f}".format(item.marketPrice)
                pos_pnl_str = "${:.2f}".format(item.unrealizedPNL)
                pos_pnl_class = "bullish" if item.unrealizedPNL > 0 else "bearish"

                html.append("                <tr><td>Type</td><td>" + position_type + " @ " + strike_str + "</td></tr>")
                html.append("                <tr><td>Contracts</td><td>" + qty_str + "</td></tr>")
                html.append("                <tr><td>Entry Price</td><td>" + entry_price_str + "</td></tr>")
                html.append("                <tr><td>Current Price</td><td>" + current_price_str2 + "</td></tr>")
                html.append("                <tr><td>Unrealized P&L</td><td><span class=\"" + pos_pnl_class + "\">" + pos_pnl_str + "</span></td></tr>")
    else:
        html.append("                <tr><td colspan=\"2\">No active position</td></tr>")

    html.append("            </table>")
    html.append("        </div>")

    # Open Orders Section
    html.append("        <div class=\"section\">")
    html.append("            <h2>Open Orders</h2>")
    html.append("            <table>")
    html.append("                <tr><th>Type</th><th>Action</th><th>Quantity</th><th>Price</th></tr>")

    if open_orders:
        for trade in open_orders:
            order = trade.order
            order_type_str = str(order.orderType)
            action_str = str(order.action)
            qty_str = str(order.totalQuantity)

            if hasattr(order, 'lmtPrice') and order.lmtPrice:
                price_str = "${:.2f}".format(order.lmtPrice)
            elif hasattr(order, 'auxPrice') and order.auxPrice:
                price_str = "${:.2f}".format(order.auxPrice)
            else:
                price_str = "Market"

            html.append("                <tr><td>" + order_type_str + "</td><td>" + action_str + "</td><td>" + qty_str + "</td><td>" + price_str + "</td></tr>")
    else:
        html.append("                <tr><td colspan=\"4\">No open orders</td></tr>")

    html.append("            </table>")
    html.append("        </div>")

    # P&L Section
    html.append("        <div class=\"section\">")
    html.append("            <h2>P&L Summary</h2>")
    html.append("            <table>")
    html.append("                <tr><th>Metric</th><th>Value</th></tr>")
    html.append("                <tr><td>Today's Realized P&L</td><td><span class=\"" + daily_pnl_class + "\">" + daily_pnl_str + "</span></td></tr>")
    html.append("                <tr><td>Unrealized P&L</td><td><span class=\"" + unrealized_pnl_class + "\">" + unrealized_pnl_str + "</span></td></tr>")
    html.append("                <tr><td>Total P&L</td><td><span class=\"" + total_pnl_class + "\">" + total_pnl_str + "</span></td></tr>")
    html.append("                <tr><td>Max Daily Risk</td><td>" + max_risk_str + "</td></tr>")
    html.append("                <tr><td>Risk Remaining</td><td>" + risk_remaining_str + "</td></tr>")
    html.append("            </table>")
    html.append("        </div>")

    # Action Message
    if action_message:
        html.append("        <div class=\"action-box\">")
        html.append("            <strong>Last Action:</strong> " + str(action_message))
        html.append("        </div>")

    html.append("    </div>")
    html.append("</body>")
    html.append("</html>")

    # Write to file
    try:
        with open(OUTPUT_HTML, 'w', encoding='utf-8') as f:
            f.write('\n'.join(html))
        print("HTML dashboard written to: " + OUTPUT_HTML)
    except Exception as e:
        print("Error writing HTML: " + str(e))


# ========================================
# MAIN BOT LOGIC
# ========================================

async def main():
    state = load_state()

    action_message = ""

    ib = IB()
    ib.RequestTimeout = 60

    try:
        # Connect to IBKR
        print("Connecting to TWS/Gateway...")
        await asyncio.wait_for(
            ib.connectAsync(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=60),
            timeout=60
        )
        print("Connected successfully")

        ib.reqMarketDataType(1)  # Live data

        # Check trading hours
        trading_hours = is_trading_hours()
        bot_status = "RUNNING" if trading_hours else "AFTER TRADING HOURS"

        # Get underlying contract
        if CONTRACT_TYPE == 'Index':
            underlying = Index(SYMBOL, EXCHANGE, 'USD')
        else:
            underlying = Stock(SYMBOL, EXCHANGE, 'USD')

        qual = await ib.qualifyContractsAsync(underlying)
        if not qual:
            raise RuntimeError("Failed to qualify " + str(SYMBOL))
        underlying = qual[0]

        # Get current price
        t_und = ib.reqMktData(underlying, '', False, False)
        await asyncio.sleep(8)
        current_price = t_und.marketPrice()

        if util.isNan(current_price) or current_price is None:
            # Use last close
            daily_bars = await ib.reqHistoricalDataAsync(
                underlying, '', '5 D', '1 day', 'TRADES', True, 1
            )
            if daily_bars:
                current_price = daily_bars[-1].close

        print(SYMBOL + " current price: " + "{:,.2f}".format(current_price))

        # Fetch 1-minute bars for opening range and VWAP calculation
        bars_1min = await ib.reqHistoricalDataAsync(
            underlying,
            endDateTime='',
            durationStr='1 D',
            barSizeSetting='1 min',
            whatToShow='TRADES',
            useRTH=True,  # Regular trading hours for ORB
            formatDate=1
        )

        if len(bars_1min) < 15:
            raise RuntimeError("Not enough bars for range capture (need at least 15, got " + str(len(bars_1min)) + ")")

        # Log bar information
        if bars_1min:
            first_bar_time = bars_1min[0].date
            last_bar_time = bars_1min[-1].date
            print("Fetched " + str(len(bars_1min)) + " 1-minute bars from " + str(first_bar_time) + " to " + str(last_bar_time))

        # Check current time and opening range status
        est_now = datetime.now(ZoneInfo("America/New_York"))
        range_start = est_now.replace(hour=OPENING_RANGE_START_HOUR, minute=OPENING_RANGE_START_MINUTE, second=0, microsecond=0)
        range_end = est_now.replace(hour=OPENING_RANGE_END_HOUR, minute=OPENING_RANGE_END_MINUTE, second=0, microsecond=0)

        # Try to capture opening range if not already captured today
        range_high = state.get('opening_range_high')
        range_low = state.get('opening_range_low')
        range_captured = state.get('range_captured', False)

        # Check if we need to reset range for new day
        if range_captured and state.get('range_capture_time'):
            capture_time = datetime.fromisoformat(state['range_capture_time'])
            if capture_time.date() < est_now.date():
                # New day - reset range
                range_high = None
                range_low = None
                range_captured = False
                state['opening_range_high'] = None
                state['opening_range_low'] = None
                state['range_captured'] = False
                state['range_capture_time'] = None

        # Capture range if we're past the range period and haven't captured yet
        if not range_captured and est_now >= range_end:
            range_high, range_low, captured = capture_opening_range(bars_1min, range_start, range_end)
            if captured:
                state['opening_range_high'] = range_high
                state['opening_range_low'] = range_low
                state['range_captured'] = True
                state['range_capture_time'] = str(est_now)
                print("Opening range captured: High=" + "{:.2f}".format(range_high) + ", Low=" + "{:.2f}".format(range_low))

        # Calculate VWAP
        vwap = compute_vwap(bars_1min)
        state['current_vwap'] = vwap
        if vwap:
            print("Current VWAP: " + "{:.2f}".format(vwap))

        # Get previous day data for CPR
        daily_bars = await ib.reqHistoricalDataAsync(
            underlying, '', '5 D', '1 day', 'TRADES', True, 1
        )

        cpr_levels = None
        if len(daily_bars) >= 2:
            prev_day = daily_bars[-2]
            cpr_levels = calculate_cpr_levels(prev_day.high, prev_day.low, prev_day.close)

        # Get portfolio and open orders
        portfolio = ib.portfolio()
        open_orders = ib.openTrades()

        # Calculate current P&L from closed trades
        executions = await ib.reqExecutionsAsync()
        daily_pnl = 0.0

        # Simple P&L calculation - sum all today's executions
        est_now = datetime.now(ZoneInfo("America/New_York"))
        today_start = est_now.replace(hour=0, minute=0, second=0, microsecond=0)

        buy_total = 0.0
        sell_total = 0.0
        for exec_detail in executions:
            exec_time = exec_detail.execution.time.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
            if exec_time >= today_start:
                if exec_detail.execution.side == 'BOT':
                    buy_total += exec_detail.execution.price * exec_detail.execution.shares * 100
                elif exec_detail.execution.side == 'SLD':
                    sell_total += exec_detail.execution.price * exec_detail.execution.shares * 100

        daily_pnl = sell_total - buy_total

        # Get unrealized P&L from current positions
        unrealized_pnl = sum([item.unrealizedPNL for item in portfolio])
        total_pnl = daily_pnl + unrealized_pnl

        # Check if max daily risk is hit
        max_risk_hit = total_pnl <= -MAX_DAILY_RISK

        # ========================================
        # TRADING LOGIC
        # ========================================

        if trading_hours and not max_risk_hit:
            # Check if we need to wait after stop out
            can_reenter = True
            if state.get('last_stop_out_time'):
                last_stop_time = datetime.fromisoformat(state['last_stop_out_time'])
                minutes_since_stop = (datetime.now(ZoneInfo("America/New_York")) - last_stop_time).total_seconds() / 60
                if minutes_since_stop < REENTRY_WAIT_MINUTES:
                    can_reenter = False
                    action_message = "Waiting " + str(REENTRY_WAIT_MINUTES - int(minutes_since_stop)) + " more minutes before re-entry"

            # Check if we have a position
            has_position = len(portfolio) > 0 and any(item.contract.secType == 'OPT' for item in portfolio)

            if has_position:
                # We have a position - manage it
                opt_position = next((item for item in portfolio if item.contract.secType == 'OPT'), None)

                if opt_position:
                    current_option_price = opt_position.marketPrice
                    avg_cost = opt_position.averageCost / 100
                    position_qty = abs(int(opt_position.position))

                    # Update state
                    state['contracts_remaining'] = position_qty
                    state['entry_price'] = avg_cost

                    # Check for opposite breakout (exit signal, no reversal per requirements)
                    state_direction = state.get('position')
                    if range_captured and range_high and range_low:
                        breakout_signal = check_breakout(current_price, range_high, range_low, vwap, state_direction)

                        if breakout_signal == 'exit':
                            # Price broke opposite side of range - exit all positions
                            action_message = "Price broke opposite side of range - exiting " + str(state_direction).upper() + " position"

                            # Close all positions
                            for item in portfolio:
                                if item.contract.secType == 'OPT':
                                    close_order = MarketOrder('SELL', abs(int(item.position)))
                                    close_order.algoStrategy = 'Adaptive'
                                    close_order.algoParams = [TagValue('adaptivePriority', 'Urgent')]
                                    ib.placeOrder(item.contract, close_order)

                            await asyncio.sleep(3)

                            # Cancel all open orders
                            for trade in ib.openTrades():
                                ib.cancelOrder(trade.order)

                            state['position'] = None
                            state['contracts_remaining'] = 0
                        else:
                            # Check profit levels for partial exits
                            profit_pct = ((current_option_price - avg_cost) / avg_cost) * 100 if avg_cost > 0 else 0

                            # Implement CPR-based profit taking
                            if profit_pct >= CPR_LEVEL_3_PROFIT_PERCENT and position_qty == 3:
                                # Close 1 contract at level 3
                                action_message = "Hit CPR Level 3 (" + str(CPR_LEVEL_3_PROFIT_PERCENT) + "% profit) - Closing 1/3 contracts"
                                close_order = MarketOrder('SELL', 1)
                                close_order.algoStrategy = 'Adaptive'
                                close_order.algoParams = [TagValue('adaptivePriority', 'Normal')]
                                ib.placeOrder(opt_position.contract, close_order)

                            elif profit_pct >= CPR_LEVEL_2_PROFIT_PERCENT and position_qty == 2:
                                # Close 1 contract at level 2
                                action_message = "Hit CPR Level 2 (" + str(CPR_LEVEL_2_PROFIT_PERCENT) + "% profit) - Closing 1/3 contracts"
                                close_order = MarketOrder('SELL', 1)
                                close_order.algoStrategy = 'Adaptive'
                                close_order.algoParams = [TagValue('adaptivePriority', 'Normal')]
                                ib.placeOrder(opt_position.contract, close_order)

                            elif profit_pct >= CPR_LEVEL_1_PROFIT_PERCENT and position_qty == 1:
                                # Close last contract at level 1
                                action_message = "Hit CPR Level 1 (" + str(CPR_LEVEL_1_PROFIT_PERCENT) + "% profit) - Closing final contract"
                                close_order = MarketOrder('SELL', 1)
                                close_order.algoStrategy = 'Adaptive'
                                close_order.algoParams = [TagValue('adaptivePriority', 'Normal')]
                                ib.placeOrder(opt_position.contract, close_order)

                            # Hybrid stop loss management (no dynamic updates - set once at entry)
                            # Stop loss is placed at entry and managed via stop orders

            elif can_reenter:
                # No position - check for entry signal
                # Check if range is captured and we're post-opening range
                if range_captured and is_post_opening_range(est_now) and vwap:
                    breakout_signal = check_breakout(current_price, range_high, range_low, vwap, None)

                    if breakout_signal in ['call', 'put']:
                        # Entry signal confirmed with VWAP filter
                        direction = breakout_signal
                        state['breakout_price'] = current_price
                        state['vwap_at_entry'] = vwap

                        # Get expiration (0DTE)
                        chains = await ib.reqSecDefOptParamsAsync(
                            underlying.symbol, '', underlying.secType, underlying.conId
                        )
                    if not chains:
                        raise RuntimeError("No option chains for " + str(SYMBOL))

                    chain = max(chains, key=lambda c: len(c.expirations))
                    all_expirations = sorted(chain.expirations)

                    est_now_date = datetime.now(ZoneInfo("America/New_York")).date()
                    expiry_str = None
                    for exp in all_expirations:
                        exp_date = datetime.strptime(exp, '%Y%m%d').date()
                        if exp_date >= est_now_date:
                            expiry_str = exp
                            break

                    if not expiry_str:
                        raise RuntimeError("No expiration found")

                    # Find OTM strike
                    all_strikes = sorted(chain.strikes)

                    if direction == 'call':
                        # Find strikes above current price
                        otm_strikes = [s for s in all_strikes if s > current_price]
                        if len(otm_strikes) >= OTM_STRIKES_OUT:
                            target_strike = otm_strikes[OTM_STRIKES_OUT - 1]
                        else:
                            target_strike = otm_strikes[-1] if otm_strikes else all_strikes[-1]

                        contract = Option(SYMBOL, expiry_str, target_strike, 'C', 'SMART')
                        if TRADING_CLASS:
                            contract.tradingClass = TRADING_CLASS
                    else:
                        # Find strikes below current price
                        otm_strikes = [s for s in all_strikes if s < current_price]
                        otm_strikes.reverse()
                        if len(otm_strikes) >= OTM_STRIKES_OUT:
                            target_strike = otm_strikes[OTM_STRIKES_OUT - 1]
                        else:
                            target_strike = otm_strikes[-1] if otm_strikes else all_strikes[0]

                        contract = Option(SYMBOL, expiry_str, target_strike, 'P', 'SMART')
                        if TRADING_CLASS:
                            contract.tradingClass = TRADING_CLASS

                    # Qualify contract
                    qualified = await ib.qualifyContractsAsync(contract)
                    if qualified:
                        contract = qualified[0]

                        # Place entry order
                        entry_order = MarketOrder('BUY', CONTRACTS_PER_TRADE)
                        entry_order.algoStrategy = 'Adaptive'
                        entry_order.algoParams = [TagValue('adaptivePriority', 'Normal')]

                        trade = ib.placeOrder(contract, entry_order)
                        action_message = "Entering " + direction.upper() + " position: " + str(CONTRACTS_PER_TRADE) + " contracts at strike " + str(target_strike)

                        # Wait for fill
                        await asyncio.sleep(5)

                        # Update state
                        state['position'] = direction
                        state['contracts_remaining'] = CONTRACTS_PER_TRADE
                        state['entry_time'] = datetime.now(ZoneInfo("America/New_York")).isoformat()

                        # Place stop loss orders
                        if trade.orderStatus.status == 'Filled':
                            fill_price = trade.orderStatus.avgFillPrice
                            state['entry_price'] = fill_price

                            # Calculate hybrid stop loss (50% of entry)
                            sl_price = calculate_hybrid_stop_loss(fill_price, STOP_LOSS_PERCENT)
                            state['stop_loss_price'] = sl_price

                            # Place stop order for all contracts
                            stop_order = StopOrder('SELL', CONTRACTS_PER_TRADE, sl_price)
                            ib.placeOrder(contract, stop_order)

                            action_message += " | Hybrid SL set at " + "{:.2f}".format(sl_price)
                    else:
                        if not range_captured:
                            action_message = "Waiting for opening range capture (9:30-9:45 AM EST)"
                        elif not is_post_opening_range(est_now):
                            action_message = "Range captured. Waiting until 9:45 AM EST to trade"
                        elif not vwap:
                            action_message = "Waiting for VWAP calculation"
                        else:
                            action_message = "Waiting for breakout signal (price > " + "{:.2f}".format(range_high) + " + above VWAP OR price < " + "{:.2f}".format(range_low) + " + below VWAP)"

        elif max_risk_hit:
            action_message = "Max daily risk of $" + str(MAX_DAILY_RISK) + " hit. Trading stopped for today."
            bot_status = "MAX RISK HIT"

        # Save state
        state['daily_pnl'] = daily_pnl
        state['last_action'] = action_message
        save_state(state)

        # Print status
        print("Bot status: " + str(bot_status))
        if range_captured:
            print("Opening Range: High=" + "{:.2f}".format(range_high) + ", Low=" + "{:.2f}".format(range_low))
        else:
            print("Opening Range: Not yet captured")
        if vwap:
            print("VWAP: " + "{:.2f}".format(vwap))
        print("Daily P&L: $" + "{:.2f}".format(daily_pnl))
        if action_message:
            print("Action: " + str(action_message))

        # Generate HTML output
        generate_html_dashboard(bot_status, current_price, range_high, range_low,
                               range_captured, vwap, state.get('breakout_price'), portfolio, open_orders,
                               daily_pnl, unrealized_pnl, total_pnl, action_message)

    except Exception as e:
        print("="*60)
        print("ERROR OCCURRED")
        print("="*60)
        print("Error: " + str(e))
        print("Error type: " + str(type(e)))
        import traceback
        print("\nFull traceback:")
        traceback.print_exc()
        print("="*60)

    finally:
        if ib.isConnected():
            ib.disconnect()
            print("Disconnected from IBKR")


if __name__ == "__main__":
    asyncio.run(main())
