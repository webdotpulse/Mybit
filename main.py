"""
Main Autonomous Trading Engine Orchestrator for Bybit V5 (Unified Trading Account).
Binds asynchronous WebSocket streams, multi-timeframe feature calculation,
maker-first execution, risk management circuit breakers, and status IPC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from rich.logging import RichHandler

from bybit_client import BybitV5Client
from config import (
    AppConfig,
    BybitCredentials,
    CapitalTier,
    get_tier_for_equity,
    load_config,
    load_credentials,
    save_config,
)
from risk_manager import Position, RiskManager, _to_float, _to_int
from strategy import MarketRegime, Signal, StrategyEngine, TimeframeSeries
from telegram_notifier import TelegramNotifier
from trade_journal import ParameterAutoTuner, TradeJournal, TradeRecord

# Setup structured logging
logger = logging.getLogger("engine")


class TradingEngine:
    """Core Trading Engine Orchestrator."""

    def __init__(self, config: AppConfig, credentials: BybitCredentials):
        self.config = config
        self.creds = credentials

        self.client = BybitV5Client(
            credentials=credentials,
            trading_mode=config.trading_mode,
        )
        self.strategy = StrategyEngine(config)
        self.risk_manager = RiskManager(config, self.client)

        db_path = config.data_dir / "trading_journal.db"
        self.journal = TradeJournal(db_path)
        self.auto_tuner = ParameterAutoTuner(self.journal, config)

        self.telegram = TelegramNotifier(
            config.telegram,
            panic_callback=self.panic_stop,
            status_callback=self.get_status_text,
        )

        self._running = False
        self._background_tasks: list[asyncio.Task] = []
        self._pending_orders: Dict[str, Dict[str, Any]] = {}
        self._last_pos_heartbeat: float = 0.0
        self.status_file = self.config.data_dir / "engine_status.json"

    async def apply_capital_tier(self, total_equity: float, force: bool = False) -> None:
        """
        Dynamically adapts trading pairs, position limits, daily risk,
        and lot sizing based strictly on live available funds.
        """
        if not self.config.risk.auto_tier_by_equity:
            return

        tier, tier_cfg = get_tier_for_equity(total_equity)
        current_tier = getattr(self.config.risk, "current_tier", None)

        if tier == current_tier and not force:
            return

        prev_tier_name = current_tier.value if current_tier else "INITIAL"
        self.config.risk.current_tier = tier
        self.config.risk.allocated_capital_usd = total_equity
        self.config.risk.max_daily_risk_pct = tier_cfg["max_daily_risk_pct"]
        self.config.risk.max_open_positions = tier_cfg["max_open_positions"]
        self.config.risk.min_position_equity_pct = tier_cfg["min_position_equity_pct"]
        self.config.risk.max_position_equity_pct = tier_cfg["max_position_equity_pct"]
        self.config.risk.default_leverage = tier_cfg["default_leverage"]
        self.config.strategy.auto_tuning_trade_interval = tier_cfg["auto_tuning_interval"]
        self.config.strategy.symbols = tier_cfg["symbols"]

        logger.info(
            f"⚡ [AUTO-CONFIG] Detected Available Funds: ${total_equity:,.2f} USD\n"
            f"  • Capital Tier Transition: {prev_tier_name} -> {tier.value}\n"
            f"  • Description: {tier_cfg['description']}\n"
            f"  • Active Pairs: {', '.join(tier_cfg['symbols'])}\n"
            f"  • Max Concurrent Positions: {tier_cfg['max_open_positions']}\n"
            f"  • Max Daily Risk: {tier_cfg['max_daily_risk_pct']}%\n"
            f"  • Kelly Position Sizing: {tier_cfg['min_position_equity_pct']}% - {tier_cfg['max_position_equity_pct']}%\n"
            f"  • Default Leverage: {tier_cfg['default_leverage']}x"
        )

        # Ensure all tier symbols are initialized in the strategy series and subscribed
        for symbol in tier_cfg["symbols"]:
            if symbol not in self.strategy.series:
                self.strategy.series[symbol] = {
                    tf: TimeframeSeries(symbol, tf) for tf in self.config.strategy.timeframes
                }
                for tf in self.config.strategy.timeframes:
                    klines = await self.client.get_klines(symbol=symbol, interval=tf, limit=100)
                    for k in klines:
                        self.strategy.update_kline(symbol, tf, k)
                self.client.subscribe_orderbook(symbol)
                for tf in self.config.strategy.timeframes:
                    self.client.subscribe_kline(symbol, tf)
            if self.config.trading_mode.value == "linear":
                await self.client.set_leverage(symbol, tier_cfg["default_leverage"])

        # Persist updated configuration
        save_config(self.config)

        if self._running:
            await self.telegram.send_message(
                f"⚡ *Autonomous Capital Tier Updated: {tier.value}*\n"
                f"• *Detected Balance*: `${total_equity:,.2f}`\n"
                f"• *Active Pairs*: `{', '.join(tier_cfg['symbols'])}`\n"
                f"• *Max Positions*: `{tier_cfg['max_open_positions']}`\n"
                f"• *Daily Risk Limit*: `{tier_cfg['max_daily_risk_pct']}%`\n"
                f"• *Leverage*: `{tier_cfg['default_leverage']}x`"
            )

    async def initialize(self) -> None:
        """Bootstraps connection pools, historical klines, and active state."""
        logger.info("Initializing Bybit V5 Autonomous Engine...")
        await self.client.initialize()
        await self.telegram.initialize()

        # 1. Fetch initial wallet balance and auto-adapt parameters to available funds
        wallet_res = {}
        try:
            wallet_res = await self.client.get_wallet_balance() or {}
        except Exception as e:
            logger.error(f"Error fetching initial wallet balance: {e}")

        total_equity: Optional[float] = None
        if wallet_res.get("retCode") == 0:
            result_obj = wallet_res.get("result") or {}
            item_list = result_obj.get("list") or []
            if item_list:
                item0 = item_list[0] or {}
                # 1. Check UTA totalEquity
                raw_equity = item0.get("totalEquity")
                if raw_equity not in (None, ""):
                    try:
                        val = float(raw_equity)
                        if val > 0:
                            total_equity = val
                    except (ValueError, TypeError):
                        pass

                # 2. Check coin breakdown (for Classic CONTRACT accounts)
                if total_equity is None:
                    for coin_info in (item0.get("coin") or []):
                        if coin_info.get("coin") in ("USDT", "USDC"):
                            c_eq = coin_info.get("equity") or coin_info.get("walletBalance")
                            if c_eq not in (None, ""):
                                try:
                                    val = float(c_eq)
                                    if val > 0:
                                        total_equity = val
                                        break
                                except (ValueError, TypeError):
                                    pass

        if total_equity is not None:
            self.risk_manager.update_wallet_balance(total_equity, is_initial=True)
            logger.info(f"Connected to Bybit account. Current Equity: ${total_equity:,.2f}")
            await self.apply_capital_tier(total_equity, force=True)
        else:
            ret_msg = wallet_res.get("retMsg", "No balance returned")
            logger.warning(
                f"Could not fetch live wallet balance ({ret_msg}). "
                f"Using allocated capital default (${self.config.risk.allocated_capital_usd:,.2f})."
            )
            await self.apply_capital_tier(self.config.risk.allocated_capital_usd, force=True)

        # 2. Configure leverage for linear perpetuals
        if self.config.trading_mode.value == "linear":
            for symbol in self.config.strategy.symbols:
                await self.client.set_leverage(symbol, self.config.risk.default_leverage)

        # 3. Prime multi-timeframe indicators with historical klines
        logger.info("Priming multi-timeframe technical indicator series...")
        for symbol in self.config.strategy.symbols:
            for tf in self.config.strategy.timeframes:
                klines = await self.client.get_klines(symbol=symbol, interval=tf, limit=100)
                for k in klines:
                    self.strategy.update_kline(symbol, tf, k)
            self.client.subscribe_orderbook(symbol)
            for tf in self.config.strategy.timeframes:
                self.client.subscribe_kline(symbol, tf)

        # 4. Attach event callbacks
        self.client.kline_callbacks.append(self._on_kline_update)
        self.client.execution_callbacks.append(self._on_execution_update)
        self.client.order_callbacks.append(self._on_order_update)
        self.client.wallet_callbacks.append(self._on_wallet_update)
        self.client.position_callbacks.append(self._on_position_update)

        # 5. Perform initial state reconciliation
        await self.risk_manager.reconcile()

        # 6. Start WebSocket streaming
        await self.client.start_streams()
        self._running = True

        await self.telegram.send_message(
            f"🚀 *Bybit Autonomous Engine Started*\n"
            f"• Mode: `{self.config.trading_mode.value.upper()}`\n"
            f"• Equity: `${self.risk_manager.current_equity:,.2f}`\n"
            f"• Pairs: `{', '.join(self.config.strategy.symbols)}`\n"
            f"• Testnet: `{self.creds.testnet}`"
        )

    async def run(self) -> None:
        """Starts main loops: strategy execution, reconciliation, and status IPC."""
        self._background_tasks.append(asyncio.create_task(self._reconciliation_loop()))
        self._background_tasks.append(asyncio.create_task(self._strategy_decision_loop()))
        self._background_tasks.append(asyncio.create_task(self._status_publisher_loop()))

        logger.info("Trading engine execution loops active. Awaiting regime opportunities...")
        while self._running:
            await asyncio.sleep(1)

    async def _on_kline_update(self, symbol: str, interval: str, kline: Dict[str, Any]) -> None:
        """Ingests live kline updates into the feature engine."""
        self.strategy.update_kline(symbol, interval, kline)
        # Check for volatility spike on 1m bars
        if interval in ("1", "1m"):
            atr = self.strategy.series[symbol]["1m"].calculate_atr()
            if atr > 0:
                is_spike = self.risk_manager.evaluate_volatility_spike(symbol, atr)
                if is_spike:
                    await self.telegram.send_circuit_breaker_alert(
                        "Volatility Spike Kill Switch",
                        f"Extreme ATR spike detected on {symbol} (ATR={atr:.4f}). Halting new orders.",
                    )

    async def _on_execution_update(self, exec_data: Dict[str, Any]) -> None:
        """Processes real-time fills, logs trade metrics, and tunes parameters."""
        try:
            symbol = exec_data.get("symbol", "")
            side = exec_data.get("side", "")
            exec_price = float(exec_data.get("execPrice", 0.0))
            exec_qty = float(exec_data.get("execQty", 0.0))
            exec_fee = float(exec_data.get("execFee", 0.0))
            order_type = exec_data.get("orderType", "Limit")
            order_id = exec_data.get("orderId", "")
            order_link_id = exec_data.get("orderLinkId", "")
            is_maker = exec_data.get("isMaker", False)
            exec_time = int(exec_data.get("execTime", time.time() * 1000))

            # Query realized PnL if this is a closing fill
            closed_pnl = float(exec_data.get("closedPnl", 0.0))
            if closed_pnl != 0.0:
                self.risk_manager.record_trade_fill(closed_pnl)
                self.strategy.record_trade_result(closed_pnl)

                # Create trade journal entry
                tr = TradeRecord(
                    trade_id=order_link_id or order_id,
                    symbol=symbol,
                    side=side,
                    entry_time=exec_time - 30000,  # approximate
                    exit_time=exec_time,
                    holding_time_sec=30.0,
                    entry_price=exec_price,
                    exit_price=exec_price,
                    qty=exec_qty,
                    realized_pnl=closed_pnl,
                    realized_pnl_pct=(closed_pnl / max(1.0, exec_price * exec_qty)) * 100.0,
                    fee_paid=exec_fee,
                    slippage_bps=0.0,
                    order_type="Maker" if is_maker else "Taker",
                    regime="ACTIVE",
                    conviction=0.8,
                    atr_at_entry=0.0,
                    ema_spread_at_entry=0.0,
                    vwap_delta_at_entry=0.0,
                    ofi_at_entry=0.0,
                )
                self.journal.record_trade(tr)

                # Send Telegram fill alert
                await self.telegram.send_trade_exit(
                    symbol=symbol,
                    side=side,
                    exit_price=exec_price,
                    pnl=closed_pnl,
                    pnl_pct=tr.realized_pnl_pct,
                    holding_sec=30.0,
                )

                # Trigger periodic auto-tuning
                tuning_res = self.auto_tuner.evaluate_and_tune()
                if tuning_res:
                    await self.telegram.send_message(
                        f"⚙️ *Self-Adaptive Tuning Activated*\n{tuning_res['rationale']}"
                    )
            elif exec_qty > 0:
                logger.info(
                    f"🎉 [FILLED] {symbol} {side} {exec_qty} filled @ ${exec_price:.4f} "
                    f"({'Maker Rebate' if is_maker else 'Taker'})"
                )
        except Exception as e:
            logger.error(f"Error handling execution update: {e}")

    async def _on_order_update(self, order_data: Dict[str, Any]) -> None:
        """Updates internal order tracking."""
        status = order_data.get("orderStatus")
        order_id = order_data.get("orderId")
        if status in ("Filled", "Cancelled", "Rejected", "Deactivated") and order_id in self._pending_orders:
            self._pending_orders.pop(order_id, None)

    async def _on_position_update(self, pos_data: Dict[str, Any]) -> None:
        """Updates in-memory positions instantly via WebSocket stream."""
        try:
            size = _to_float(pos_data.get("size"))
            symbol = pos_data.get("symbol", "")
            if not symbol:
                return
            if size > 0:
                self.risk_manager.positions[symbol] = Position(
                    symbol=symbol,
                    side=pos_data.get("side", ""),
                    size=size,
                    entry_price=_to_float(pos_data.get("entryPrice") or pos_data.get("avgPrice")),
                    mark_price=_to_float(pos_data.get("markPrice")),
                    unrealised_pnl=_to_float(pos_data.get("unrealisedPnl")),
                    leverage=_to_int(pos_data.get("leverage"), 1),
                    take_profit=_to_float(pos_data.get("takeProfit")) or None,
                    stop_loss=_to_float(pos_data.get("stopLoss")) or None,
                    trailing_stop=_to_float(pos_data.get("trailingStop")) or None,
                )
            else:
                self.risk_manager.positions.pop(symbol, None)
        except Exception as e:
            logger.debug(f"Position stream parse error: {e}")

    async def _on_wallet_update(self, wallet_data: Dict[str, Any]) -> None:
        """Updates equity in real-time from WebSocket wallet stream and auto-adapts tier."""
        try:
            total_equity = float(wallet_data.get("totalEquity", 0.0))
            if total_equity > 0:
                self.risk_manager.update_wallet_balance(total_equity)
                await self.apply_capital_tier(total_equity)
        except Exception as e:
            logger.debug(f"Wallet stream parse error: {e}")

    async def _strategy_decision_loop(self) -> None:
        """
        Micro-Scalping Signal Dispatcher:
        Scans pairs every 500ms, evaluates regime and orderbook OFI,
        and submits Maker-first post-only bracket orders.
        """
        while self._running:
            try:
                now = time.time()
                total_active = len(self.risk_manager.positions) + len(self._pending_orders)

                # Periodic heartbeat while holding positions or pending orders (every 15s)
                if total_active > 0 and (now - self._last_pos_heartbeat > 15):
                    self._last_pos_heartbeat = now
                    for sym, pos in self.risk_manager.positions.items():
                        pnl_sign = "+" if pos.unrealised_pnl >= 0 else ""
                        logger.info(
                            f"🛡️ [HOLDING POSITION] {pos.symbol} {pos.side} {pos.size} @ ${pos.entry_price:.4f} "
                            f"| Mark: ${pos.mark_price:.4f} | uPnL: {pnl_sign}${pos.unrealised_pnl:.2f} "
                            f"| TP: {pos.take_profit or 'Bracket'} | SL: {pos.stop_loss or 'Bracket'}"
                        )
                    for oid, po in self._pending_orders.items():
                        logger.info(
                            f"⏳ [PENDING ORDER] {po['symbol']} {po['side']} {po['qty']} @ ${po['price']} "
                            f"(Awaiting fill on Bybit book)"
                        )

                # If at maximum position capacity, do not submit any new orders
                if total_active >= self.config.risk.max_open_positions:
                    await asyncio.sleep(1.0)
                    continue

                for symbol in self.config.strategy.symbols:
                    # Enforce strict capacity limit per tick
                    if (len(self.risk_manager.positions) + len(self._pending_orders)) >= self.config.risk.max_open_positions:
                        break

                    ob = self.client.orderbooks.get(symbol)
                    if not ob:
                        continue

                    # Don't open if already in position for this symbol
                    if symbol in self.risk_manager.positions:
                        continue

                    # Don't submit new order if there's already an active open/pending order for this symbol
                    if any(o.get("symbol") == symbol for o in self._pending_orders.values()):
                        continue

                    # Generate signal
                    signal = self.strategy.generate_signal(symbol, ob)
                    if signal:
                        await self._execute_signal(signal, ob)

                await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in strategy decision loop: {e}")
                await asyncio.sleep(1.0)

    async def _execute_signal(self, sig: Signal, ob: Any) -> None:
        """Sizes position and submits Maker-First post-only limit order with brackets."""
        # 1. Kelly Sizing
        kelly_pct = self.strategy.calculate_kelly_fraction()
        capital_pool = self.risk_manager.current_equity
        leverage = self.config.risk.default_leverage
        notional = capital_pool * kelly_pct * leverage

        # Ensure notional meets Bybit minimum order floor (5.00 USDT hard floor)
        MIN_NOTIONAL = 5.50
        if notional < MIN_NOTIONAL:
            notional = MIN_NOTIONAL

        qty = self._format_qty(sig.symbol, notional / sig.price)
        if qty <= 0:
            return

        # Ensure actual formatted value satisfies Bybit min notional
        actual_notional = qty * sig.price
        if actual_notional < 5.0:
            qty = self._format_qty(sig.symbol, 5.5 / sig.price)
            actual_notional = qty * sig.price

        # Permission check with risk manager using actual formatted notional
        permitted, reason = self.risk_manager.is_order_permitted(sig.symbol, actual_notional)
        if not permitted:
            logger.debug(f"[{sig.symbol}] Order rejected by RiskManager: {reason}")
            return

        tp_price = self._format_price(sig.symbol, sig.tp_price)
        sl_price = self._format_price(sig.symbol, sig.sl_price)

        logger.info(
            f"⚡ Signal on {sig.symbol}: {sig.side} {qty} @ ${sig.price} "
            f"(Regime: {sig.regime.value}, TIF: {sig.time_in_force}, Notional: ${actual_notional:.2f})"
        )

        # 2. Maker-First Order Submission
        res = await self.client.create_order(
            symbol=sig.symbol,
            side=sig.side,
            order_type=sig.order_type,
            qty=qty,
            price=sig.price,
            time_in_force=sig.time_in_force,
            take_profit=tp_price,
            stop_loss=sl_price,
        )

        ret_code = res.get("retCode", -1)
        ret_msg = res.get("retMsg", "Unknown")

        # RetCode 110007 = PostOnly will take liquidity (order crossed spread)
        if ret_code == 110007:
            logger.info(
                f"⚡ [{sig.symbol}] PostOnly crossed spread (retCode 110007). "
                f"Executing Market taker order to capture {sig.regime.value} momentum..."
            )
            mkt_res = await self.client.create_order(
                symbol=sig.symbol,
                side=sig.side,
                order_type="Market",
                qty=qty,
                take_profit=tp_price,
                stop_loss=sl_price,
            )
            mkt_ret_code = mkt_res.get("retCode", -1)
            if mkt_ret_code == 0:
                order_id = (mkt_res.get("result") or {}).get("orderId", "")
                self._pending_orders[order_id] = {
                    "symbol": sig.symbol,
                    "side": sig.side,
                    "qty": qty,
                    "price": sig.price,
                    "placed_at": time.time(),
                }
                logger.info(
                    f"✅ [{sig.symbol}] Market order executed! OrderID: {order_id} "
                    f"({sig.side} {qty}, TP: {tp_price}, SL: {sl_price})"
                )
                await self.telegram.send_trade_entry(
                    symbol=sig.symbol,
                    side=sig.side,
                    price=sig.price,
                    qty=qty,
                    tp=tp_price,
                    sl=sl_price,
                    regime=sig.regime.value,
                )
            else:
                logger.warning(
                    f"❌ [{sig.symbol}] Market fallback order failed: {mkt_res.get('retMsg')} "
                    f"(retCode: {mkt_ret_code})"
                )
        elif ret_code == 0:
            order_id = (res.get("result") or {}).get("orderId", "")
            self._pending_orders[order_id] = {
                "symbol": sig.symbol,
                "side": sig.side,
                "qty": qty,
                "price": sig.price,
                "placed_at": time.time(),
            }
            logger.info(
                f"✅ [{sig.symbol}] Maker PostOnly order placed on book! OrderID: {order_id} "
                f"({sig.side} {qty} @ ${sig.price}, Notional: ${actual_notional:.2f})"
            )
            await self.telegram.send_trade_entry(
                symbol=sig.symbol,
                side=sig.side,
                price=sig.price,
                qty=qty,
                tp=tp_price,
                sl=sl_price,
                regime=sig.regime.value,
            )
        else:
            logger.warning(
                f"❌ [{sig.symbol}] Order rejected by Bybit (retCode {ret_code}): {ret_msg} "
                f"| Attempted: {sig.side} {qty} @ ${sig.price} (Notional: ${actual_notional:.2f})"
            )

    def _format_price(self, symbol: str, price: Optional[float]) -> Optional[float]:
        """Rounds price to symbol tick size precision on Bybit V5."""
        if price is None or price <= 0:
            return None
        sym = symbol.upper()
        if "BTC" in sym:
            return round(price, 1)
        elif "ETH" in sym or "SOL" in sym:
            return round(price, 2)
        elif "AVAX" in sym or "LINK" in sym:
            return round(price, 3)
        elif "SUI" in sym or "XRP" in sym or "ADA" in sym:
            return round(price, 4)
        elif "DOGE" in sym:
            return round(price, 5)
        return round(price, 4)

    def _format_qty(self, symbol: str, raw_qty: float) -> float:
        """Rounds quantity to symbol lot size precision on Bybit V5."""
        sym = symbol.upper()
        if "BTC" in sym:
            return max(0.001, round(raw_qty, 3))
        elif "ETH" in sym:
            return max(0.01, round(raw_qty, 2))
        elif "SOL" in sym or "AVAX" in sym or "LINK" in sym:
            return max(0.1, round(raw_qty, 1))
        elif "SUI" in sym:
            # Bybit SUIUSDT linear contract: minOrderQty = 10, qtyStep = 10
            steps = max(1, int(round(raw_qty / 10.0)))
            return float(steps * 10)
        elif "DOGE" in sym or "ADA" in sym or "TRX" in sym:
            return float(max(1, int(round(raw_qty))))
        elif "XRP" in sym:
            return max(0.1, round(raw_qty, 1))
        return round(raw_qty, 2)

    async def _reconciliation_loop(self) -> None:
        """Active 10-Second State Reconciliation Loop."""
        while self._running:
            try:
                await asyncio.sleep(10)
                await self.risk_manager.reconcile()
                # Expire stale pending orders from memory (>45s)
                now_sec = time.time()
                for oid in list(self._pending_orders.keys()):
                    if now_sec - self._pending_orders[oid].get("placed_at", now_sec) > 45:
                        self._pending_orders.pop(oid, None)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Reconciliation error: {e}")

    async def _status_publisher_loop(self) -> None:
        """Periodically dumps operational metrics to engine_status.json for manage.sh."""
        while self._running:
            try:
                status_dict = self.get_status_dict()
                temp_file = self.status_file.with_suffix(".tmp")
                with open(temp_file, "w", encoding="utf-8") as f:
                    json.dump(status_dict, f, indent=2)
                temp_file.replace(self.status_file)
                await asyncio.sleep(1.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Status publish error: {e}")
                await asyncio.sleep(2.0)

    def get_status_dict(self) -> Dict[str, Any]:
        """Collects live state snapshot for terminal dashboard & Telegram."""
        summary = self.journal.get_summary_metrics()
        pos_list = [
            {
                "symbol": p.symbol,
                "side": p.side,
                "size": p.size,
                "entry_price": p.entry_price,
                "mark_price": p.mark_price,
                "unrealised_pnl": p.unrealised_pnl,
                "leverage": p.leverage,
            }
            for p in self.risk_manager.positions.values()
        ]
        return {
            "timestamp": int(time.time()),
            "status": "RUNNING" if not self.risk_manager.panic_triggered else "PANIC_HALTED",
            "trading_mode": self.config.trading_mode.value,
            "testnet": self.creds.testnet,
            "equity": round(self.risk_manager.current_equity, 2),
            "capital_tier": self.config.risk.current_tier.value,
            "auto_tier_enabled": self.config.risk.auto_tier_by_equity,
            "symbols": self.config.strategy.symbols,
            "max_open_positions": self.config.risk.max_open_positions,
            "high_water_mark": round(self.risk_manager.high_water_mark, 2),
            "daily_drawdown_pct": round(
                (
                    (self.risk_manager.high_water_mark - self.risk_manager.current_equity)
                    / max(1.0, self.risk_manager.high_water_mark)
                )
                * 100.0,
                2,
            ),
            "circuit_breakers": {
                "daily_drawdown_tripped": self.risk_manager.daily_drawdown_tripped,
                "consecutive_losses": self.risk_manager.consecutive_losses,
                "in_cooldown": time.time() < self.risk_manager.cooldown_until,
                "cooldown_remaining_sec": max(
                    0, int(self.risk_manager.cooldown_until - time.time())
                ),
                "volatility_kill": self.risk_manager.volatility_kill_tripped,
            },
            "metrics": summary,
            "positions": pos_list,
            "pending_orders": len(self._pending_orders),
            "parameters": {
                "tp_atr_mult": self.config.execution.bracket_tp_atr_mult,
                "sl_atr_mult": self.config.execution.bracket_sl_atr_mult,
                "kelly_scale": self.config.strategy.kelly_scale,
            },
        }

    async def get_status_text(self) -> str:
        """Formats status for Telegram bot `/status` command."""
        s = self.get_status_dict()
        m = s["metrics"]
        pos_lines = []
        for p in s["positions"]:
            sign = "+" if p["unrealised_pnl"] >= 0 else ""
            pos_lines.append(
                f"• {p['symbol']} ({p['side']}): {p['size']} @ ${p['entry_price']} | uPnL: `{sign}${p['unrealised_pnl']:.2f}`"
            )
        pos_str = "\n".join(pos_lines) if pos_lines else "None"

        return (
            f"📊 *Engine Status: {s['status']}*\n"
            f"• *Equity*: `${s['equity']:,.2f}`\n"
            f"• *Daily Drawdown*: `{s['daily_drawdown_pct']}%`\n"
            f"• *Total PnL*: `${m['total_pnl']:,.2f}` (WinRate: `{m['win_rate']}%`)\n"
            f"• *Active Positions* ({len(s['positions'])}):\n{pos_str}\n"
            f"• *Pending Orders*: {s['pending_orders']}"
        )

    async def panic_stop(self) -> Dict[str, Any]:
        """Triggers emergency liquidation and order cancellation."""
        return await self.risk_manager.execute_panic_stop()

    async def shutdown(self) -> None:
        """Gracefully shuts down engine."""
        logger.info("Gracefully stopping trading engine...")
        self._running = False
        for task in self._background_tasks:
            task.cancel()
        # Clean up any open un-filled maker orders
        try:
            await self.client.cancel_all_orders()
        except Exception:
            pass
        await self.client.close()
        await self.telegram.close()
        logger.info("Trading engine terminated cleanly.")


def setup_engine_logging(log_dir: Path) -> None:
    """Configures structured terminal and rotating file logging."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "engine.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            RichHandler(rich_tracebacks=True, show_time=True, show_path=False),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )


async def main_async() -> None:
    """Async entrypoint loading configuration and starting engine."""
    config = load_config()
    setup_engine_logging(config.log_dir)

    try:
        credentials = load_credentials()
    except FileNotFoundError:
        logger.critical(
            "Encrypted credentials not found. Run ./installer.py or install.sh first!"
        )
        sys.exit(1)

    engine = TradingEngine(config, credentials)

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def handle_signal():
        logger.info("Termination signal received.")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, handle_signal)

    try:
        await engine.initialize()
        engine_task = asyncio.create_task(engine.run())
        await stop_event.wait()
    finally:
        await engine.shutdown()


def main() -> None:
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
