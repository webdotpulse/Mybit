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
    updated_at: float = field(default_factory=time.time)


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
        self.high_water_mark: float = self.risk_cfg.allocated_capital_usd
        self.last_day_reset: int = datetime.now(timezone.utc).day

        # Circuit Breaker States
        self.consecutive_losses: int = 0
        self.cooldown_until: float = 0.0
        self.daily_drawdown_tripped: bool = False
        self.volatility_kill_tripped: bool = False
        self.panic_triggered: bool = False

        # In-Memory Active Position State (symbol -> Position)
        self.positions: Dict[str, Position] = {}
        self.reconciliation_count: int = 0
        self.last_reconciliation_ts: float = 0.0

        # Rolling 24h ATR memory for volatility spike detection
        self.rolling_atr_history: Dict[str, List[float]] = {}

    def update_wallet_balance(self, total_equity: float) -> None:
        """Updates internal equity state and calculates daily drawdown."""
        current_day = datetime.now(timezone.utc).day
        if current_day != self.last_day_reset:
            logger.info("New UTC trading day. Resetting daily high-water mark and limits.")
            self.starting_equity = total_equity
            self.high_water_mark = total_equity
            self.daily_drawdown_tripped = False
            self.last_day_reset = current_day

        self.current_equity = total_equity
        if total_equity > self.high_water_mark:
            self.high_water_mark = total_equity

        # Calculate drawdown relative to day's starting equity / HWM
        drawdown_pct = ((self.high_water_mark - total_equity) / self.high_water_mark) * 100.0
        if drawdown_pct >= self.risk_cfg.max_daily_risk_pct and not self.daily_drawdown_tripped:
            self.daily_drawdown_tripped = True
            logger.critical(
                f"🚨 [CIRCUIT BREAKER] Max Daily Drawdown breached: {drawdown_pct:.2f}% >= "
                f"{self.risk_cfg.max_daily_risk_pct}%. Trading halted for the day!"
            )

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

    def evaluate_volatility_spike(self, symbol: str, current_atr: float) -> bool:
        """
        Evaluates whether ATR exceeds 3 standard deviations above its 24h mean.
        Triggers emergency halt if volatility explodes beyond risk boundaries.
        """
        history = self.rolling_atr_history.setdefault(symbol, [])
        history.append(current_atr)
        if len(history) > 1440:  # ~24h of 1m readings
            history.pop(0)

        if len(history) < 60:
            return False

        mean_atr = float(np.mean(history))
        std_atr = float(np.std(history))

        if std_atr > 0 and (current_atr - mean_atr) / std_atr > self.risk_cfg.atr_spike_threshold_std:
            self.volatility_kill_tripped = True
            logger.critical(
                f"⚡ [CIRCUIT BREAKER] Extreme volatility anomaly on {symbol}: "
                f"ATR {current_atr:.4f} > 3-Sigma threshold ({mean_atr + 3 * std_atr:.4f}). Kill switch activated."
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
            return False, "Volatility anomaly kill switch active."

        # Maximum concurrent open positions limit
        if symbol not in self.positions and len(self.positions) >= self.risk_cfg.max_open_positions:
            return False, f"Max open positions reached ({len(self.positions)}/{self.risk_cfg.max_open_positions})."

        # Position size cap against allocated capital
        max_notional = self.current_equity * (self.risk_cfg.max_position_equity_pct / 100.0) * self.risk_cfg.default_leverage
        if notional_value > max_notional * 1.05:  # small 5% buffer
            return False, f"Order notional ${notional_value:.2f} exceeds risk cap ${max_notional:.2f}."

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
                size = float(p.get("size", 0.0))
                symbol = p.get("symbol", "")
                if size > 0:
                    current_symbols.add(symbol)
                    self.positions[symbol] = Position(
                        symbol=symbol,
                        side=p.get("side", ""),
                        size=size,
                        entry_price=float(p.get("avgPrice", 0.0)),
                        mark_price=float(p.get("markPrice", 0.0)),
                        unrealised_pnl=float(p.get("unrealisedPnl", 0.0)),
                        leverage=int(p.get("leverage", 1)),
                        take_profit=float(p.get("takeProfit", 0.0)) or None,
                        stop_loss=float(p.get("stopLoss", 0.0)) or None,
                        trailing_stop=float(p.get("trailingStop", 0.0)) or None,
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
                created_time = float(o.get("createdTime", now_ms))
                order_id = o.get("orderId")
                symbol = o.get("symbol")
                # If an open limit order is older than 45 seconds without filling, cancel it
                if (now_ms - created_time) > 45000:
                    logger.info(f"Canceling stale unfilled maker order {order_id} on {symbol}.")
                    await self.client.cancel_order(symbol=symbol, order_id=order_id)

            self.reconciliation_count += 1
            self.last_reconciliation_ts = time.time()
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
