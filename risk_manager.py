"""
Mission-Critical Risk Management & Failsafe Controller.
Enforces daily drawdown circuit breakers, consecutive loss cooldowns,
volatility spike kill switches, 10-second active Bybit position reconciliation,
and zero-latency panic emergency liquidation.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from bybit_client import BybitV5Client
from config import AppConfig, RiskConfig

logger = logging.getLogger("risk_manager")


def _to_float(val: Any, default: float = 0.0) -> float:
    if val in (None, "", "null", "None"):
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _to_int(val: Any, default: int = 1) -> int:
    if val in (None, "", "null", "None"):
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def format_qty(symbol: str, raw_qty: float) -> float:
    """Rounds quantity to symbol lot size precision on Bybit V5."""
    sym = symbol.upper()
    if "BTC" in sym:
        return max(0.001, round(raw_qty, 3))
    elif "ETH" in sym:
        return max(0.01, round(raw_qty, 2))
    elif any(k in sym for k in ("SOL", "AVAX", "LINK", "NEAR", "APT", "DOT", "ATOM", "XRP")):
        return max(0.1, round(raw_qty, 1))
    elif "SUI" in sym:
        # Bybit SUIUSDT linear contract: minOrderQty = 10, qtyStep = 10
        steps = max(1, int(round(raw_qty / 10.0)))
        return float(steps * 10)
    elif any(k in sym for k in ("DOGE", "ADA", "TRX", "MATIC", "POL")):
        return float(max(1, int(round(raw_qty))))
    return round(raw_qty, 2)


@dataclass
class Position:
    symbol: str
    side: str  # "Buy" or "Sell"
    size: float
    entry_price: float
    mark_price: float
    unrealised_pnl: float
    leverage: int
    take_profit: Optional[float] = None
    stop_loss: Optional[float] = None
    trailing_stop: Optional[float] = None
    entry_atr: float = 0.0
    breakeven_set: bool = False
    opened_at: float = field(default_factory=time.time)
    entry_features: Dict[str, Any] = field(default_factory=dict)
    updated_at: float = field(default_factory=time.time)
    tp1_price: Optional[float] = None
    tp1_filled: bool = False
    original_size: float = 0.0



class RiskManager:
    """
    Central risk overseer enforcing capital preservation, account-level
    circuit breakers, and deterministic state reconciliation against Bybit.
    """

    def __init__(self, config: AppConfig, client: BybitV5Client):
        self.config = config
        self.risk_cfg: RiskConfig = config.risk
        self.client = client

        # Account Equity Tracking
        self.starting_equity: float = self.risk_cfg.allocated_capital_usd
        self.current_equity: float = self.risk_cfg.allocated_capital_usd
        self.high_water_mark: Optional[float] = None
        self.last_day_reset: int = datetime.now(timezone.utc).day

        # Circuit Breaker States
        self.consecutive_losses: int = 0
        self.cooldown_until: float = 0.0
        self.daily_drawdown_tripped: bool = False
        self.volatility_kill_tripped: bool = False
        self.volatility_cooldown_until: float = 0.0
        self.volatility_spike_symbol: Optional[str] = None
        self.panic_triggered: bool = False

        # In-Memory Active Position State (symbol -> Position)
        self.positions: Dict[str, Position] = {}
        self.reconciliation_count: int = 0
        self.last_reconciliation_ts: float = 0.0

        # Rolling 24h ATR memory for volatility spike detection
        self.rolling_atr_history: Dict[str, List[float]] = {}

    def update_wallet_balance(self, total_equity: float, is_initial: bool = False) -> None:
        """Updates internal equity state and calculates daily drawdown."""
        current_day = datetime.now(timezone.utc).day
        if current_day != self.last_day_reset or self.high_water_mark is None or is_initial:
            if current_day != self.last_day_reset and self.high_water_mark is not None:
                logger.info("New UTC trading day. Resetting daily high-water mark and limits.")
            self.starting_equity = total_equity
            self.high_water_mark = total_equity
            self.daily_drawdown_tripped = False
            self.last_day_reset = current_day

        self.current_equity = total_equity
        if total_equity > (self.high_water_mark or 0.0):
            self.high_water_mark = total_equity

        # Calculate drawdown relative to day's starting equity / HWM
        if self.high_water_mark and self.high_water_mark > 0 and not is_initial:
            drawdown_pct = ((self.high_water_mark - total_equity) / self.high_water_mark) * 100.0
            if drawdown_pct >= self.risk_cfg.max_daily_risk_pct and not self.daily_drawdown_tripped:
                self.daily_drawdown_tripped = True
                logger.critical(
                    f"🚨 [CIRCUIT BREAKER] Max Daily Drawdown breached: {drawdown_pct:.2f}% >= "
                    f"{self.risk_cfg.max_daily_risk_pct}%. Trading halted for the day!"
                )

    def reset_circuit_breakers(self) -> None:
        """Resets all circuit breaker trips and resumes normal trading."""
        self.daily_drawdown_tripped = False
        self.volatility_kill_tripped = False
        self.volatility_cooldown_until = 0.0
        self.volatility_spike_symbol = None
        self.consecutive_losses = 0
        self.cooldown_until = 0.0
        self.panic_triggered = False
        self.high_water_mark = self.current_equity
        self.starting_equity = self.current_equity
        logger.info("All circuit breakers reset to clean state.")

    def record_trade_fill(self, pnl: float) -> None:
        """Tracks consecutive loss cutoff."""
        if pnl < 0:
            self.consecutive_losses += 1
            logger.warning(f"Recorded loss of ${pnl:.2f}. Consecutive losses: {self.consecutive_losses}")
            if self.consecutive_losses >= self.risk_cfg.max_consecutive_losses:
                pause_secs = self.risk_cfg.consecutive_loss_cooldown_mins * 60
                self.cooldown_until = time.time() + pause_secs
                logger.critical(
                    f"🛑 [CIRCUIT BREAKER] {self.consecutive_losses} consecutive losses. "
                    f"Pausing new trades for {self.risk_cfg.consecutive_loss_cooldown_mins} minutes."
                )
        else:
            if self.consecutive_losses > 0:
                logger.info(f"Profitable trade (+${pnl:.2f}) reset consecutive loss counter.")
            self.consecutive_losses = 0

    def seed_atr_history(self, symbol: str, atr_values: List[float]) -> None:
        """Seeds the rolling ATR history from historical bars."""
        if not atr_values:
            return
        history = self.rolling_atr_history.setdefault(symbol, [])
        for val in atr_values:
            if val > 0:
                history.append(float(val))
        if len(history) > 1440:
            self.rolling_atr_history[symbol] = history[-1440:]

    def evaluate_volatility_spike(self, symbol: str, current_atr: float, is_closed_bar: bool = True) -> bool:
        """
        Evaluates whether ATR exceeds 3 standard deviations above its rolling mean.
        Triggers emergency halt if volatility explodes beyond risk boundaries.
        Only appends to history when is_closed_bar is True to prevent tick spam distortion.
        """
        history = self.rolling_atr_history.setdefault(symbol, [])
        if is_closed_bar:
            history.append(current_atr)
            if len(history) > 1440:  # ~24h of 1m readings
                history.pop(0)

        # If already tripped and still within cooldown, do not re-trip or reset the cooldown timer
        if self.volatility_kill_tripped:
            if time.time() < self.volatility_cooldown_until:
                return False
            else:
                self.volatility_kill_tripped = False
                self.volatility_spike_symbol = None
                self.volatility_cooldown_until = 0.0
                logger.info("✅ [CIRCUIT BREAKER] Volatility spike cooldown elapsed. Resuming normal trading.")

        if len(history) < 60:
            return False

        mean_atr = float(np.mean(history))
        std_atr = float(np.std(history))

        # Require significant statistical divergence AND meaningful relative expansion (>= 50% above mean)
        # to prevent microscopic dispersion (std ~ 0) on low-priced coins from triggering false alarms.
        is_std_spike = std_atr > 0 and (current_atr - mean_atr) / std_atr > self.risk_cfg.atr_spike_threshold_std
        is_relative_spike = (current_atr - mean_atr) >= (0.5 * mean_atr)

        if is_std_spike and is_relative_spike:
            self.volatility_kill_tripped = True
            self.volatility_spike_symbol = symbol
            cooldown_mins = getattr(self.risk_cfg, "volatility_kill_cooldown_mins", 30)
            self.volatility_cooldown_until = time.time() + (cooldown_mins * 60)
            threshold = mean_atr + self.risk_cfg.atr_spike_threshold_std * std_atr
            logger.critical(
                f"⚡ [CIRCUIT BREAKER] Extreme volatility anomaly on {symbol}: "
                f"ATR {current_atr:.4f} > 3-Sigma threshold ({threshold:.4f}, mean={mean_atr:.4f}, std={std_atr:.6f}). "
                f"Kill switch activated. Pausing new trades for {cooldown_mins} minutes."
            )
            return True
        return False

    def is_order_permitted(self, symbol: str, notional_value: float) -> Tuple[bool, str]:
        """Validates all circuit breakers and exposure limits before order execution."""
        if self.panic_triggered:
            return False, "Emergency panic mode active."

        if self.daily_drawdown_tripped:
            return False, f"Max daily drawdown circuit breaker tripped ({self.risk_cfg.max_daily_risk_pct}%)."

        if time.time() < self.cooldown_until:
            remaining = int(self.cooldown_until - time.time())
            return False, f"In consecutive loss cooldown ({remaining}s remaining)."

        if self.volatility_kill_tripped:
            if time.time() < self.volatility_cooldown_until:
                remaining = int(self.volatility_cooldown_until - time.time())
                return False, f"In volatility anomaly cooldown ({remaining}s remaining)."
            else:
                self.volatility_kill_tripped = False
                self.volatility_spike_symbol = None
                self.volatility_cooldown_until = 0.0
                logger.info("✅ [CIRCUIT BREAKER] Volatility spike cooldown elapsed. Resuming normal trading.")

        # Maximum concurrent open positions limit
        if symbol not in self.positions and len(self.positions) >= self.risk_cfg.max_open_positions:
            return False, f"Max open positions reached ({len(self.positions)}/{self.risk_cfg.max_open_positions})."

        # Position size cap against allocated capital
        max_notional = self.current_equity * (self.risk_cfg.max_position_equity_pct / 100.0) * self.risk_cfg.default_leverage
        # Allow at least Bybit exchange minimum threshold (e.g. $16 for SOL 0.1, $11 for SUI 10, $6 for DOGE)
        # provided margin requirement does not exceed 25% of total equity
        exchange_min_notional = min(16.0, self.current_equity * self.risk_cfg.default_leverage * 0.25)
        effective_cap = max(max_notional, exchange_min_notional)
        if notional_value > effective_cap * 1.05:  # small 5% buffer
            return False, f"Order notional ${notional_value:.2f} exceeds risk cap ${effective_cap:.2f}."

        return True, "Permitted"

    async def reconcile(self) -> None:
        """
        Active 10-Second State Reconciliation Loop.
        Compares Bybit's exchange state against local memory to:
        1. Discover external fills or closures.
        2. Detect and terminate orphaned orders.
        3. Sync unrealized PnL and active margin levels.
        """
        try:
            raw_positions = await self.client.get_positions()
            current_symbols = set()

            for p in raw_positions:
                size = _to_float(p.get("size"))
                symbol = p.get("symbol", "")
                if size > 0:
                    current_symbols.add(symbol)
                    existing = self.positions.get(symbol)
                    self.positions[symbol] = Position(
                        symbol=symbol,
                        side=p.get("side", ""),
                        size=size,
                        entry_price=_to_float(p.get("avgPrice")),
                        mark_price=_to_float(p.get("markPrice")),
                        unrealised_pnl=_to_float(p.get("unrealisedPnl")),
                        leverage=_to_int(p.get("leverage"), 1),
                        take_profit=_to_float(p.get("takeProfit")) or None,
                        stop_loss=_to_float(p.get("stopLoss")) or None,
                        trailing_stop=_to_float(p.get("trailingStop")) or None,
                        entry_atr=existing.entry_atr if existing else 0.0,
                        breakeven_set=existing.breakeven_set if existing else False,
                        opened_at=existing.opened_at if existing else time.time(),
                        entry_features=existing.entry_features if existing else {},
                        tp1_price=existing.tp1_price if existing else None,
                        tp1_filled=existing.tp1_filled if existing else False,
                        original_size=existing.original_size if (existing and existing.original_size > 0) else size,
                    )
                else:
                    self.positions.pop(symbol, None)

            # Clean up locally cached positions that no longer exist on Bybit
            for sym in list(self.positions.keys()):
                if sym not in current_symbols:
                    logger.info(f"Position on {sym} closed on exchange. Updating local state.")
                    self.positions.pop(sym, None)

            # Query open orders and purge stale unfilled maker orders (>30s old)
            open_orders = await self.client.get_open_orders()
            now_ms = time.time() * 1000
            for o in open_orders:
                created_time = _to_float(o.get("createdTime"), now_ms)
                order_id = o.get("orderId")
                symbol = o.get("symbol")
                # If an open limit order is older than 45 seconds without filling, cancel it
                if (now_ms - created_time) > 45000:
                    logger.info(f"Canceling stale unfilled maker order {order_id} on {symbol}.")
                    await self.client.cancel_order(symbol=symbol, order_id=order_id)

            self.reconciliation_count += 1
            self.last_reconciliation_ts = time.time()

            # Auto-clear volatility spike kill switch if cooldown period has elapsed
            if self.volatility_kill_tripped and time.time() >= self.volatility_cooldown_until:
                self.volatility_kill_tripped = False
                self.volatility_spike_symbol = None
                self.volatility_cooldown_until = 0.0
                logger.info("✅ [CIRCUIT BREAKER] Volatility spike cooldown elapsed. Resuming normal trading.")

            logger.debug(f"State reconciled successfully. Active positions: {len(self.positions)}")
        except Exception as e:
            logger.error(f"Error during state reconciliation: {e}")

    async def execute_panic_stop(self) -> Dict[str, Any]:
        """
        EMERGENCY PANIC KILL SWITCH:
        1. Immediately sets panic flag to stop all incoming signals.
        2. Cancels all active orders across all pairs.
        3. Submits Market reduce-only orders to flatten all open positions.
        """
        self.panic_triggered = True
        logger.critical("🚨🚨🚨 PANIC TRIGGERED! Liquidating positions and cancelling all orders! 🚨🚨🚨")

        results = {"orders_cancelled": 0, "positions_closed": []}
        try:
            # 1. Cancel all open orders
            cancel_res = await self.client.cancel_all_orders()
            results["cancel_result"] = cancel_res

            # 2. Query all active positions to market close
            raw_positions = await self.client.get_positions()
            for p in raw_positions:
                size = float(p.get("size", 0.0))
                symbol = p.get("symbol", "")
                side = p.get("side", "")
                if size > 0:
                    close_side = "Sell" if side == "Buy" else "Buy"
                    logger.warning(f"Closing {side} position of {size} on {symbol} via Market order...")
                    close_res = await self.client.create_order(
                        symbol=symbol,
                        side=close_side,
                        order_type="Market",
                        qty=size,
                        time_in_force="IOC",
                        reduce_only=True,
                    )
                    results["positions_closed"].append({symbol: close_res})

            self.positions.clear()
            logger.info("Panic liquidation completed.")
        except Exception as e:
            logger.critical(f"Fatal error during panic stop: {e}")
            results["error"] = str(e)
        return results

    async def evaluate_position_stops(self) -> List[Dict[str, Any]]:
        """
        Evaluates active positions for 2-Tier "Lock & Run" Partial Take-Profit and Breakeven advancement.
        1. When price reaches Tier 1 (+28 bps), market closes 50% to bank cash and locks
           remaining 50% stop loss at Entry + fee buffer to ride wave risk-free.
        2. When unrealized profit reaches +breakeven_atr_trigger * entry_atr,
           advances Stop Loss to entry price + fee buffer (Breakeven).
        """
        updates = []
        exec_cfg = self.config.execution

        for symbol, pos in list(self.positions.items()):
            if pos.size <= 0 or pos.entry_price <= 0:
                continue

            fee_buffer = pos.entry_price * (exec_cfg.breakeven_buffer_bps / 10000.0)

            # 1. 2-Tier "Lock & Run" Partial Take-Profit (+28 bps)
            if exec_cfg.tiered_tp_enabled and pos.tp1_price is not None and not pos.tp1_filled:
                tp1_hit = (pos.mark_price >= pos.tp1_price) if pos.side == "Buy" else (pos.mark_price <= pos.tp1_price)
                if tp1_hit:
                    raw_close_qty = pos.size * exec_cfg.tp1_ratio
                    close_qty = format_qty(pos.symbol, raw_close_qty)
                    new_sl = round(pos.entry_price + fee_buffer, 4) if pos.side == "Buy" else round(pos.entry_price - fee_buffer, 4)

                    if 0 < close_qty < pos.size:
                        close_side = "Sell" if pos.side == "Buy" else "Buy"
                        logger.info(
                            f"🎯 [{symbol}] 2-Tier Lock & Run: Mark ${pos.mark_price:.4f} crossed TP1 ${pos.tp1_price:.4f}. "
                            f"Closing {close_qty} (50%) to lock profit..."
                        )
                        res = await self.client.create_order(
                            symbol=symbol,
                            side=close_side,
                            order_type="Market",
                            qty=close_qty,
                            time_in_force="IOC",
                            reduce_only=True,
                        )
                        if res.get("retCode") == 0:
                            pos.tp1_filled = True
                            pos.size = max(0.0, round(pos.size - close_qty, 4))
                            # Advance Stop Loss on remaining half to entry + buffer (risk-free run)
                            sl_res = await self.client.set_trading_stop(
                                symbol=symbol,
                                stop_loss=new_sl,
                            )
                            if sl_res.get("retCode") == 0:
                                pos.stop_loss = new_sl
                                pos.breakeven_set = True
                            pnl_est = (pos.mark_price - pos.entry_price) * close_qty if pos.side == "Buy" else (pos.entry_price - pos.mark_price) * close_qty
                            updates.append({
                                "symbol": symbol,
                                "type": "TIER1_PROFIT_LOCKED",
                                "side": pos.side,
                                "close_qty": close_qty,
                                "remaining_size": pos.size,
                                "new_sl": new_sl,
                                "mark_price": pos.mark_price,
                                "pnl_estimate": pnl_est,
                            })
                        else:
                            logger.warning(f"Failed to submit Tier 1 partial take profit on {symbol}: {res.get('retMsg')}")
                    else:
                        # Qty too small to split on exchange. Advance SL to Breakeven so entire trade is risk-free!
                        pos.tp1_filled = True
                        sl_res = await self.client.set_trading_stop(
                            symbol=symbol,
                            stop_loss=new_sl,
                        )
                        if sl_res.get("retCode") == 0:
                            pos.stop_loss = new_sl
                            pos.breakeven_set = True
                        updates.append({
                            "symbol": symbol,
                            "type": "BREAKEVEN_SET",
                            "side": pos.side,
                            "new_sl": new_sl,
                            "mark_price": pos.mark_price,
                        })

            # 2. Breakeven Stop-Loss Check (via ATR trigger)
            if not pos.breakeven_set and exec_cfg.breakeven_atr_trigger > 0:
                atr = pos.entry_atr
                if atr > 0:
                    trigger_dist = exec_cfg.breakeven_atr_trigger * atr

                    if pos.side == "Buy":
                        # For Long: if mark_price >= entry_price + trigger_dist
                        if pos.mark_price >= (pos.entry_price + trigger_dist):
                            new_sl = round(pos.entry_price + fee_buffer, 4)
                            if pos.stop_loss is None or new_sl > pos.stop_loss:
                                logger.info(
                                    f"🛡️ [{symbol}] Advancing Stop Loss to Breakeven (+${fee_buffer:.4f} fee buffer): "
                                    f"Mark ${pos.mark_price:.4f} >= Trigger ${pos.entry_price + trigger_dist:.4f}"
                                )
                                res = await self.client.set_trading_stop(
                                    symbol=symbol,
                                    stop_loss=new_sl,
                                )
                                if res.get("retCode") == 0:
                                    pos.stop_loss = new_sl
                                    pos.breakeven_set = True
                                    updates.append({
                                        "symbol": symbol,
                                        "type": "BREAKEVEN_SET",
                                        "side": pos.side,
                                        "new_sl": new_sl,
                                        "mark_price": pos.mark_price,
                                    })
                                else:
                                    logger.warning(f"Failed to set breakeven stop on {symbol}: {res.get('retMsg')}")
                    elif pos.side == "Sell":
                        # For Short: if mark_price <= entry_price - trigger_dist
                        if pos.mark_price <= (pos.entry_price - trigger_dist):
                            new_sl = round(pos.entry_price - fee_buffer, 4)
                            if pos.stop_loss is None or new_sl < pos.stop_loss:
                                logger.info(
                                    f"🛡️ [{symbol}] Advancing Short Stop Loss to Breakeven (-${fee_buffer:.4f} fee buffer): "
                                    f"Mark ${pos.mark_price:.4f} <= Trigger ${pos.entry_price - trigger_dist:.4f}"
                                )
                                res = await self.client.set_trading_stop(
                                    symbol=symbol,
                                    stop_loss=new_sl,
                                )
                                if res.get("retCode") == 0:
                                    pos.stop_loss = new_sl
                                    pos.breakeven_set = True
                                    updates.append({
                                        "symbol": symbol,
                                        "type": "BREAKEVEN_SET",
                                        "side": pos.side,
                                        "new_sl": new_sl,
                                        "mark_price": pos.mark_price,
                                    })
                                else:
                                    logger.warning(f"Failed to set breakeven stop on {symbol}: {res.get('retMsg')}")

        return updates

    async def check_stagnant_positions(self) -> List[Dict[str, Any]]:
        """
        Detects positions open longer than stagnant_exit_mins where price has not moved
        meaningfully (within stagnant_atr_threshold * ATR), indicating market chop.
        Liquidates stagnant positions to free capital and prevent overnight decay.
        """
        closed_positions = []
        exec_cfg = self.config.execution
        if exec_cfg.stagnant_exit_mins <= 0:
            return closed_positions

        timeout_sec = exec_cfg.stagnant_exit_mins * 60

        for symbol, pos in list(self.positions.items()):
            if pos.size <= 0 or pos.entry_price <= 0:
                continue

            duration = time.time() - pos.opened_at
            if duration >= timeout_sec:
                atr = pos.entry_atr
                price_delta = abs(pos.mark_price - pos.entry_price)
                is_stagnant = False

                if atr > 0:
                    is_stagnant = price_delta <= (exec_cfg.stagnant_atr_threshold * atr)
                else:
                    is_stagnant = (price_delta / pos.entry_price) < 0.002

                if is_stagnant:
                    duration_mins = int(duration / 60)
                    logger.warning(
                        f"⏳ [{symbol}] Position stagnant for {duration_mins}m with price delta "
                        f"${price_delta:.4f} <= {exec_cfg.stagnant_atr_threshold}x ATR. Closing position..."
                    )
                    close_side = "Sell" if pos.side == "Buy" else "Buy"
                    res = await self.client.create_order(
                        symbol=symbol,
                        side=close_side,
                        order_type="Market",
                        qty=pos.size,
                        time_in_force="IOC",
                        reduce_only=True,
                    )
                    if res.get("retCode") == 0:
                        closed_positions.append({
                            "symbol": symbol,
                            "side": pos.side,
                            "size": pos.size,
                            "duration_mins": duration_mins,
                            "mark_price": pos.mark_price,
                        })
                        logger.info(f"✅ Stagnant position on {symbol} closed successfully.")
                    else:
                        logger.error(f"Failed to close stagnant position on {symbol}: {res.get('retMsg')}")

        return closed_positions
