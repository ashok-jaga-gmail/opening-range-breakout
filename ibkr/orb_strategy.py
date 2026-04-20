#!/usr/bin/env python3
"""
orb_strategy.py — QQQ 15-min ORB Golden Strategy (Live Trading)
================================================================

Strategy summary:
  Opening Range  : 09:30–09:44 ET high/low from 1-min QQQ bars
  Signal         : First 1-min bar at/after 09:45 whose CLOSE exits the ORB
                   LONG → buy OTM call (+1 strike from ATM)
                   SHORT → buy OTM put  (−1 strike from ATM)
  Trades/day     : Up to 3 independent signals (each new direction after ORB re-entry)
  Contracts      : DEFAULT_CONTRACTS (default 3) split as T1=1 / T2=1 / T3=remainder
  Exit structure :
    T1 (+25%)  → exit T1 qty, move stop to breakeven (entry option price)
    T2 (+100%) → exit T2 qty, begin trailing stop at max_price × 70%
    T3 (+200%) → exit T3 qty (or hold to EOD at 15:59)
    Stop (−30%): applied to all open tranches; rises to BE after T1; trails after T2
  EOD            : Force-close all remaining positions at 15:59 ET

Connection:
  IBKR Gateway on localhost port 4001 (live) or 4002 (paper)
  Uses ib_insync library.  Install: pip install ib_insync

Usage:
  python3 orb_strategy.py                    # paper (port 4002)
  python3 orb_strategy.py --port 4001        # live
  python3 orb_strategy.py --contracts 4      # 4 contracts (cleaner runner split: 1/1/2)
  python3 orb_strategy.py --dry-run          # log signals only, no orders placed
"""

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional
from zoneinfo import ZoneInfo

from ib_insync import IB, Stock, Option, MarketOrder, LimitOrder, StopOrder, util

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("orb_strategy.log"),
    ],
)
log = logging.getLogger("ORB")

ET = ZoneInfo("America/New_York")

# ── Config ────────────────────────────────────────────────────────────────────
SYMBOL            = "QQQ"
EXCHANGE          = "SMART"
CURRENCY          = "USD"

ORB_START         = "09:30"   # first bar included in ORB
ORB_END           = "09:44"   # last bar included in ORB
ENTRY_START       = "09:45"   # earliest signal bar
SIGNAL_CUTOFF     = "15:00"   # no new entries after this time
EOD_CLOSE_TIME    = "15:55"   # force-close all positions (avoid spurious closing prints)

DEFAULT_CONTRACTS = 3         # total contracts per trade
HOST              = "127.0.0.1"
CLIENT_ID         = 10        # use a unique client ID to avoid conflicts

# Exit thresholds (option price percentages)
PT1_PCT   = 0.25   # +25%  → exit T1, stop → breakeven
PT2_PCT   = 1.00   # +100% → exit T2, start trailing stop
PT3_PCT   = 2.00   # +200% → exit T3
SL_PCT    = 0.30   # −30%  → stop loss on open tranches
TRAIL_PCT = 0.30   # trailing stop = max_price × (1 − TRAIL_PCT) after T2

MIN_ORB_RANGE = 0.10   # ignore micro-range days

# ── Trade state ───────────────────────────────────────────────────────────────
@dataclass
class Tranche:
    qty: int
    order_id: Optional[int] = None   # IBKR order ID for the exit order
    filled: bool = False
    fill_price: Optional[float] = None

@dataclass
class ActiveTrade:
    signal_num: int          # 1, 2, or 3
    direction: str           # "LONG" or "SHORT"
    entry_time: str          # HH:MM
    contract: object         # ib_insync Option contract
    entry_opt: float         # option price at entry (per share)
    total_qty: int

    # Tranche quantities
    t1: Tranche = field(default_factory=lambda: Tranche(0))
    t2: Tranche = field(default_factory=lambda: Tranche(0))
    t3: Tranche = field(default_factory=lambda: Tranche(0))

    # Stop tracking
    stop_order_id: Optional[int] = None
    stop_price: float = 0.0

    # Phase tracking
    phase: str = "pre_t1"     # pre_t1 → pre_t2 → pre_t3 → closed
    max_opt: float = 0.0
    trailing_stop: float = 0.0

    # P&L tracking
    realized_pnl: float = 0.0


# ── Main strategy class ───────────────────────────────────────────────────────
class ORBStrategy:
    def __init__(self, port: int, n_contracts: int, dry_run: bool):
        self.port        = port
        self.n_contracts = n_contracts
        self.dry_run     = dry_run
        self.ib          = IB()

        # ORB state
        self.orb_high: Optional[float] = None
        self.orb_low:  Optional[float] = None
        self.orb_built = False

        # Breakout tracking (to avoid re-entering same direction consecutively)
        self.last_breakout_dir: Optional[str] = None

        # Active trades (up to 3)
        self.trades: list[ActiveTrade] = []

        # Underlying live bars
        self.underlying: Optional[object] = None
        self.bars = None   # BarDataList from reqHistoricalData keepUpToDate

        # Option market data subscriptions {conId: ticker}
        self.opt_tickers: dict = {}

    # ── Connection ────────────────────────────────────────────────────────────
    def connect(self):
        for attempt in range(5):
            try:
                self.ib.connect(HOST, self.port, clientId=CLIENT_ID)
                log.info(f"Connected to IBKR on port {self.port}")
                return
            except Exception as e:
                log.warning(f"Connection attempt {attempt+1} failed: {e}")
                time.sleep(5)
        raise ConnectionError("Failed to connect to IBKR after 5 attempts")

    def disconnect(self):
        try:
            self.ib.disconnect()
            log.info("Disconnected from IBKR")
        except Exception:
            pass

    # ── Underlying contract ───────────────────────────────────────────────────
    def setup_underlying(self):
        self.underlying = Stock(SYMBOL, EXCHANGE, CURRENCY)
        self.ib.qualifyContracts(self.underlying)
        log.info(f"Underlying: {self.underlying.localSymbol or SYMBOL}")

    # ── Live 1-min bars via keepUpToDate ──────────────────────────────────────
    def start_bars(self):
        self.bars = self.ib.reqHistoricalData(
            self.underlying,
            endDateTime="",
            durationStr="3600 S",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,
            keepUpToDate=True,
        )
        self.bars.updateEvent += self.on_bar_update
        log.info("Subscribed to live 1-min bars")

    # ── Bar update callback ───────────────────────────────────────────────────
    def on_bar_update(self, bars, has_new_bar):
        if not has_new_bar or len(bars) < 2:
            return

        # The last completed bar is bars[-2] (bars[-1] is the forming bar)
        bar = bars[-2]
        t   = bar.date.strftime("%H:%M")
        o, h, l, c = bar.open, bar.high, bar.low, bar.close

        now_et = datetime.now(ET)
        today  = now_et.date()

        # ── Build ORB ─────────────────────────────────────────────────────────
        if ORB_START <= t <= ORB_END:
            if self.orb_high is None:
                self.orb_high = h
                self.orb_low  = l
                log.info(f"ORB started: bar {t}  H={h:.2f}  L={l:.2f}")
            else:
                self.orb_high = max(self.orb_high, h)
                self.orb_low  = min(self.orb_low,  l)
            if t == ORB_END:
                orb_range = self.orb_high - self.orb_low
                log.info(f"ORB complete: HIGH={self.orb_high:.2f}  LOW={self.orb_low:.2f}  Range={orb_range:.2f}")
                if orb_range < MIN_ORB_RANGE:
                    log.warning(f"ORB range {orb_range:.2f} < minimum {MIN_ORB_RANGE} — skipping today")
                    self.orb_high = self.orb_low = None
                else:
                    self.orb_built = True
            return

        if not self.orb_built:
            return

        # ── Check EOD ─────────────────────────────────────────────────────────
        if t >= EOD_CLOSE_TIME:
            self.close_all_eod()
            return

        # ── Update open trade exits from bar H/L ──────────────────────────────
        for trade in self.trades:
            if trade.phase != "closed":
                self.update_trade_exits(trade, h, l, c, t)

        # ── Check for new breakout signal ─────────────────────────────────────
        if t < ENTRY_START or t > SIGNAL_CUTOFF:
            return
        if len(self.trades) >= 3:
            return

        direction = None
        if c > self.orb_high:
            direction = "LONG"
        elif c < self.orb_low:
            direction = "SHORT"

        if direction is None:
            # Price returned inside ORB — reset last direction so next break fires
            self.last_breakout_dir = None
            return

        if direction == self.last_breakout_dir:
            return   # already in this direction, wait for re-entry

        self.last_breakout_dir = direction
        signal_num = len(self.trades) + 1
        log.info(f"=== SIGNAL #{signal_num}: {direction} at {t}  QQQ close={c:.2f} ===")
        self.enter_trade(direction, c, today, t, signal_num)

    # ── Enter a trade ─────────────────────────────────────────────────────────
    def enter_trade(self, direction: str, qqq_price: float, expiry: date, bar_time: str, signal_num: int):
        # Find OTM strike
        option_contract = self.find_otm_option(direction, qqq_price, expiry)
        if option_contract is None:
            log.error(f"  Could not find OTM option for {direction} at {qqq_price:.2f}")
            return

        # Get current option price
        opt_price = self.get_option_price(option_contract)
        if opt_price is None or opt_price <= 0:
            log.error(f"  Could not get price for {option_contract.localSymbol}")
            return

        log.info(f"  Option: {option_contract.localSymbol}  price=${opt_price:.2f}")

        # Compute tranche quantities (T1=1, T2=1, T3=remainder)
        t1_qty = 1
        t2_qty = 1
        t3_qty = max(1, self.n_contracts - t1_qty - t2_qty)
        total  = t1_qty + t2_qty + t3_qty

        # Compute exit prices
        entry_opt = opt_price
        pt1_price = entry_opt * (1 + PT1_PCT)
        pt2_price = entry_opt * (1 + PT2_PCT)
        pt3_price = entry_opt * (1 + PT3_PCT)
        sl_price  = entry_opt * (1 - SL_PCT)

        log.info(f"  Entry=${entry_opt:.2f}  T1=${pt1_price:.2f}  T2=${pt2_price:.2f}  "
                 f"T3=${pt3_price:.2f}  SL=${sl_price:.2f}  Qty={total}({t1_qty}/{t2_qty}/{t3_qty})")

        trade = ActiveTrade(
            signal_num  = signal_num,
            direction   = direction,
            entry_time  = bar_time,
            contract    = option_contract,
            entry_opt   = entry_opt,
            total_qty   = total,
            t1          = Tranche(qty=t1_qty),
            t2          = Tranche(qty=t2_qty),
            t3          = Tranche(qty=t3_qty),
            stop_price  = sl_price,
            max_opt     = entry_opt,
            trailing_stop = sl_price,
            phase       = "pre_t1",
        )

        if self.dry_run:
            log.info(f"  [DRY RUN] Would BUY {total} x {option_contract.localSymbol}")
            self.trades.append(trade)
            return

        # ── Place entry order ─────────────────────────────────────────────────
        entry_order = MarketOrder("BUY", total)
        entry_trade = self.ib.placeOrder(option_contract, entry_order)
        self.ib.sleep(2)

        # Confirm fill
        fill = entry_trade.orderStatus
        if fill.status not in ("Filled", "Submitted", "PreSubmitted"):
            log.error(f"  Entry order status: {fill.status} — aborting trade")
            return

        log.info(f"  Entry order placed (status={fill.status})")

        # ── Place initial stop (all contracts) ────────────────────────────────
        stop_ord = StopOrder("SELL", total, sl_price)
        stop_t   = self.ib.placeOrder(option_contract, stop_ord)
        self.ib.sleep(1)
        trade.stop_order_id = stop_t.order.orderId
        log.info(f"  Stop placed: {total} contracts @ ${sl_price:.2f} (orderId={trade.stop_order_id})")

        # ── Place T1 limit ────────────────────────────────────────────────────
        lim_t1 = LimitOrder("SELL", t1_qty, round(pt1_price, 2))
        t1_t   = self.ib.placeOrder(option_contract, lim_t1)
        self.ib.sleep(1)
        trade.t1.order_id = t1_t.order.orderId
        log.info(f"  T1 limit: {t1_qty} @ ${pt1_price:.2f} (orderId={trade.t1.order_id})")

        # ── Place T2 limit ────────────────────────────────────────────────────
        lim_t2 = LimitOrder("SELL", t2_qty, round(pt2_price, 2))
        t2_t   = self.ib.placeOrder(option_contract, lim_t2)
        self.ib.sleep(1)
        trade.t2.order_id = t2_t.order.orderId
        log.info(f"  T2 limit: {t2_qty} @ ${pt2_price:.2f} (orderId={trade.t2.order_id})")

        # ── Place T3 limit ────────────────────────────────────────────────────
        lim_t3 = LimitOrder("SELL", t3_qty, round(pt3_price, 2))
        t3_t   = self.ib.placeOrder(option_contract, lim_t3)
        self.ib.sleep(1)
        trade.t3.order_id = t3_t.order.orderId
        log.info(f"  T3 limit: {t3_qty} @ ${pt3_price:.2f} (orderId={trade.t3.order_id})")

        # Subscribe to option market data for price tracking
        ticker = self.ib.reqMktData(option_contract, "", False, False)
        self.opt_tickers[option_contract.conId] = ticker

        self.trades.append(trade)
        log.info(f"  Trade #{signal_num} active. Total trades today: {len(self.trades)}")

    # ── Update trade exits on each bar ────────────────────────────────────────
    def update_trade_exits(self, trade: ActiveTrade, bar_h: float, bar_l: float, bar_c: float, t: str):
        """
        Check bar H/L against trade targets and stops.
        In live trading IBKR handles limit/stop fills automatically.
        This loop handles:
          - Promoting stop to breakeven after T1 fills
          - Managing trailing stop after T2 fills
          - Detecting fills from IBKR order status
        """
        if self.dry_run:
            self._dry_run_update(trade, bar_h, bar_l, bar_c, t)
            return

        # Check fill status of T1/T2/T3 orders via IBKR
        self._sync_fill_status(trade)

        if trade.phase == "closed":
            return

        # Update max option price from live ticker
        ticker = self.opt_tickers.get(trade.contract.conId)
        if ticker:
            last = ticker.last or ticker.close or 0
            if last > 0:
                trade.max_opt = max(trade.max_opt, last)

        # ── After T1 fills: promote stop to breakeven ─────────────────────────
        if trade.t1.filled and not trade.t2.filled and trade.phase == "pre_t1":
            trade.phase = "pre_t2"
            new_stop    = trade.entry_opt
            remaining   = trade.t2.qty + trade.t3.qty
            log.info(f"  Trade #{trade.signal_num}: T1 filled @ ${trade.t1.fill_price:.2f} "
                     f"→ stop promoted to BE=${new_stop:.2f}")
            self._replace_stop(trade, remaining, new_stop)

        # ── After T2 fills: begin trailing stop ───────────────────────────────
        if trade.t2.filled and not trade.t3.filled and trade.phase == "pre_t2":
            trade.phase       = "pre_t3"
            trade.trailing_stop = trade.max_opt * (1 - TRAIL_PCT)
            log.info(f"  Trade #{trade.signal_num}: T2 filled @ ${trade.t2.fill_price:.2f} "
                     f"→ trailing stop=${trade.trailing_stop:.2f}")
            self._replace_stop(trade, trade.t3.qty, trade.trailing_stop)

        # ── Update trailing stop if max_opt moved up ──────────────────────────
        if trade.phase == "pre_t3" and not trade.t3.filled:
            new_trail = trade.max_opt * (1 - TRAIL_PCT)
            if new_trail > trade.trailing_stop + 0.01:   # move up at least $0.01
                trade.trailing_stop = new_trail
                self._replace_stop(trade, trade.t3.qty, new_trail)
                log.info(f"  Trade #{trade.signal_num}: trail raised to ${new_trail:.2f}")

        # ── T3 filled / all done ──────────────────────────────────────────────
        if trade.t3.filled:
            trade.phase = "closed"
            pnl = (
                (trade.t1.fill_price - trade.entry_opt) * trade.t1.qty * 100 +
                (trade.t2.fill_price - trade.entry_opt) * trade.t2.qty * 100 +
                (trade.t3.fill_price - trade.entry_opt) * trade.t3.qty * 100
            )
            log.info(f"  Trade #{trade.signal_num}: CLOSED  P&L=${pnl:+.2f}")

    # ── Dry-run simulation using bar H/L (mirrors backtest logic) ─────────────
    def _dry_run_update(self, trade: ActiveTrade, bar_h: float, bar_l: float, bar_c: float, t: str):
        """Simulate fills using bar high/low — for dry-run mode only."""
        if trade.phase == "closed":
            return

        eod = t >= EOD_CLOSE_TIME

        # Treat bar H/L as option H/L proxy (not accurate but illustrative in dry-run)
        # In reality you'd use the option ticker's H/L
        opt_h = bar_h   # placeholder
        opt_l = bar_l   # placeholder
        opt_c = bar_c

        trade.max_opt = max(trade.max_opt, opt_h)

        entry  = trade.entry_opt
        pt1p   = entry * (1 + PT1_PCT)
        pt2p   = entry * (1 + PT2_PCT)
        pt3p   = entry * (1 + PT3_PCT)

        if trade.phase == "pre_t1":
            if opt_l <= trade.stop_price:
                log.info(f"  [DRY] Trade #{trade.signal_num}: STOPPED @ ${trade.stop_price:.2f}")
                trade.phase = "closed"
            elif opt_h >= pt1p or eod:
                ep = pt1p if not eod else opt_c
                log.info(f"  [DRY] Trade #{trade.signal_num}: T1 hit @ ${ep:.2f}")
                trade.t1.filled = True; trade.t1.fill_price = ep
                trade.stop_price = entry; trade.phase = "pre_t2"

        elif trade.phase == "pre_t2":
            if opt_l <= trade.stop_price or eod:
                ep = max(opt_c, trade.stop_price) if eod else trade.stop_price
                log.info(f"  [DRY] Trade #{trade.signal_num}: T1-exit + remaining stopped/EOD @ ${ep:.2f}")
                trade.phase = "closed"
            elif opt_h >= pt2p:
                log.info(f"  [DRY] Trade #{trade.signal_num}: T2 hit @ ${pt2p:.2f}")
                trade.t2.filled = True; trade.t2.fill_price = pt2p
                trade.trailing_stop = trade.max_opt * (1 - TRAIL_PCT)
                trade.phase = "pre_t3"

        elif trade.phase == "pre_t3":
            trade.trailing_stop = max(trade.trailing_stop, trade.max_opt * (1 - TRAIL_PCT))
            if opt_l <= trade.trailing_stop or eod:
                ep = max(opt_c, trade.trailing_stop) if eod else trade.trailing_stop
                log.info(f"  [DRY] Trade #{trade.signal_num}: T3 trail/EOD exit @ ${ep:.2f}")
                trade.t3.filled = True; trade.t3.fill_price = ep; trade.phase = "closed"
            elif opt_h >= pt3p:
                log.info(f"  [DRY] Trade #{trade.signal_num}: T3 target hit @ ${pt3p:.2f}")
                trade.t3.filled = True; trade.t3.fill_price = pt3p; trade.phase = "closed"

    # ── Sync fill status from IBKR ────────────────────────────────────────────
    def _sync_fill_status(self, trade: ActiveTrade):
        """Check IBKR order statuses and mark tranches as filled."""
        for tranche, label in [(trade.t1, "T1"), (trade.t2, "T2"), (trade.t3, "T3")]:
            if tranche.filled or tranche.order_id is None:
                continue
            for t in self.ib.trades():
                if t.order.orderId == tranche.order_id:
                    if t.orderStatus.status == "Filled":
                        tranche.filled     = True
                        tranche.fill_price = t.orderStatus.avgFillPrice
                        log.info(f"  Trade #{trade.signal_num}: {label} filled @ ${tranche.fill_price:.2f}")

    # ── Replace stop order ────────────────────────────────────────────────────
    def _replace_stop(self, trade: ActiveTrade, qty: int, new_price: float):
        if self.dry_run:
            trade.stop_price = new_price
            return
        # Cancel old stop
        if trade.stop_order_id is not None:
            self.ib.cancelOrder(self.ib.trade(trade.stop_order_id).order)
            self.ib.sleep(1)
        # Place new stop
        stop_ord = StopOrder("SELL", qty, round(new_price, 2))
        t = self.ib.placeOrder(trade.contract, stop_ord)
        self.ib.sleep(1)
        trade.stop_order_id = t.order.orderId
        trade.stop_price    = new_price

    # ── EOD force-close all positions ─────────────────────────────────────────
    def close_all_eod(self):
        open_trades = [tr for tr in self.trades if tr.phase != "closed"]
        if not open_trades:
            return
        log.info(f"EOD: force-closing {len(open_trades)} open trade(s)")
        for trade in open_trades:
            remaining = sum(
                t.qty for t in [trade.t1, trade.t2, trade.t3]
                if not t.filled
            )
            if remaining <= 0:
                trade.phase = "closed"
                continue
            log.info(f"  Closing trade #{trade.signal_num}: {remaining} contracts of "
                     f"{trade.contract.localSymbol}")
            if not self.dry_run:
                # Cancel outstanding limit/stop orders first
                for oid in [trade.t1.order_id, trade.t2.order_id,
                            trade.t3.order_id, trade.stop_order_id]:
                    if oid is not None:
                        try:
                            for t in self.ib.trades():
                                if t.order.orderId == oid and t.orderStatus.status not in ("Filled","Cancelled"):
                                    self.ib.cancelOrder(t.order)
                        except Exception as e:
                            log.warning(f"  Could not cancel order {oid}: {e}")
                self.ib.sleep(1)
                close_ord = MarketOrder("SELL", remaining)
                self.ib.placeOrder(trade.contract, close_ord)
                self.ib.sleep(2)
            trade.phase = "closed"
            log.info(f"  Trade #{trade.signal_num} closed at EOD")

    # ── Find OTM option (+1 strike from ATM) ──────────────────────────────────
    def find_otm_option(self, direction: str, qqq_price: float, expiry: date) -> Optional[object]:
        expiry_str = expiry.strftime("%Y%m%d")
        right      = "C" if direction == "LONG" else "P"

        # Pull full option chain for today's expiry
        chains = self.ib.reqSecDefOptParams(
            SYMBOL, "", "STK", self.underlying.conId
        )
        strikes = None
        for chain in chains:
            if expiry_str in chain.expirations:
                strikes = sorted(chain.strikes)
                break

        if not strikes:
            log.error(f"  No 0DTE chain found for {expiry_str}")
            return None

        # ATM = closest strike to current price
        atm = min(strikes, key=lambda s: abs(s - qqq_price))
        atm_idx = strikes.index(atm)

        if direction == "LONG":
            otm_idx = atm_idx + 1   # one strike above for calls
        else:
            otm_idx = atm_idx - 1   # one strike below for puts

        if otm_idx < 0 or otm_idx >= len(strikes):
            log.error(f"  OTM index out of range")
            return None

        otm_strike = strikes[otm_idx]
        contract   = Option(SYMBOL, expiry_str, otm_strike, right, EXCHANGE)

        qualified  = self.ib.qualifyContracts(contract)
        if not qualified:
            log.error(f"  Could not qualify {SYMBOL} {expiry_str} {otm_strike} {right}")
            return None

        return qualified[0]

    # ── Get option mid price ──────────────────────────────────────────────────
    def get_option_price(self, contract: object) -> Optional[float]:
        self.ib.reqMarketDataType(1)   # live; use 3 for delayed in paper account
        ticker = self.ib.reqMktData(contract, "", False, False)
        self.ib.sleep(3)

        # Try mid, then last, then close
        if ticker.bid and ticker.ask and not math.isnan(ticker.bid) and not math.isnan(ticker.ask):
            price = (ticker.bid + ticker.ask) / 2
        elif ticker.last and not math.isnan(ticker.last):
            price = ticker.last
        elif ticker.close and not math.isnan(ticker.close):
            price = ticker.close
        else:
            price = None

        self.ib.cancelMktData(contract)
        return price

    # ── Print daily summary ───────────────────────────────────────────────────
    def print_summary(self):
        log.info("=" * 60)
        log.info("DAILY SUMMARY")
        log.info(f"  Trades taken : {len(self.trades)}")
        for trade in self.trades:
            t1p = f"${trade.t1.fill_price:.2f}" if trade.t1.filled else "—"
            t2p = f"${trade.t2.fill_price:.2f}" if trade.t2.filled else "—"
            t3p = f"${trade.t3.fill_price:.2f}" if trade.t3.filled else "—"
            log.info(f"  #{trade.signal_num} {trade.direction:5s} {trade.contract.localSymbol}  "
                     f"entry=${trade.entry_opt:.2f}  T1={t1p}  T2={t2p}  T3={t3p}")
        log.info("=" * 60)

    # ── Main run loop ─────────────────────────────────────────────────────────
    def run(self):
        self.connect()
        self.setup_underlying()
        self.start_bars()

        log.info(f"Strategy running — {SYMBOL} ORB  contracts={self.n_contracts}  "
                 f"{'DRY RUN' if self.dry_run else 'LIVE'}")
        log.info(f"ORB: {ORB_START}–{ORB_END}  Entry: {ENTRY_START}–{SIGNAL_CUTOFF}  EOD: {EOD_CLOSE_TIME}")
        log.info(f"Exits: T1=+{PT1_PCT*100:.0f}%  T2=+{PT2_PCT*100:.0f}%  T3=+{PT3_PCT*100:.0f}%  SL=-{SL_PCT*100:.0f}%")

        try:
            # Run until EOD
            while True:
                self.ib.sleep(15)   # process events every 15 seconds
                now_et = datetime.now(ET)
                t = now_et.strftime("%H:%M")

                # Hard stop after EOD close
                if t >= "16:05":
                    log.info("Market closed — shutting down")
                    break

                # Also poll open trade exit status every 15s (belt-and-suspenders)
                for trade in self.trades:
                    if trade.phase != "closed" and not self.dry_run:
                        self._sync_fill_status(trade)

        except KeyboardInterrupt:
            log.info("Interrupted by user")
            self.close_all_eod()
        finally:
            self.print_summary()
            self.disconnect()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="QQQ ORB Golden Strategy — IBKR live trading")
    parser.add_argument("--port",      type=int, default=4002,
                        help="IBKR Gateway port: 4001=live, 4002=paper (default: 4002)")
    parser.add_argument("--contracts", type=int, default=DEFAULT_CONTRACTS,
                        help=f"Contracts per trade (default: {DEFAULT_CONTRACTS}). "
                             "Use 4 for exact runner50 split (1/1/2).")
    parser.add_argument("--dry-run",   action="store_true",
                        help="Log signals only — no orders placed")
    args = parser.parse_args()

    strategy = ORBStrategy(
        port        = args.port,
        n_contracts = args.contracts,
        dry_run     = args.dry_run,
    )
    strategy.run()


if __name__ == "__main__":
    main()
