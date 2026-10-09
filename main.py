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
from risk_manager import Position, RiskManager, _to_float, _to_int, format_qty
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
        db_path = config.data_dir / "trading_journal.db"
        self.journal = TradeJournal(db_path)
        self.strategy = StrategyEngine(config, learner=self.journal.learner)
        self.risk_manager = RiskManager(config, self.client)
        self.auto_tuner = ParameterAutoTuner(self.journal, config)

        self.telegram = TelegramNotifier(
            config.telegram,
            panic_callback=self.panic_stop,
            status_callback=self.get_status_text,
        )

        self._running = False
        self._background_tasks: list[asyncio.Task] = []
        self._pending_orders: Dict[str, Dict[str, Any]] = {}
        self._position_context: Dict[str, Dict[str, Any]] = {}
        self._order_context: Dict[str, Dict[str, Any]] = {}
        self._funding_rates: Dict[str, float] = {}
        self._last_funding_fetch: float = 0.0
        self._last_trade_exit: Dict[str, float] = {}
        self._last_pos_heartbeat: float = 0.0
        self._processed_closed_trades: set[str] = set()
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

        # 5. Perform initial state reconciliation and hydrate past trade memory
        await self.risk_manager.reconcile()
        await self._sync_closed_pnl()

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
        self._background_tasks.append(asyncio.create_task(self._position_guardian_loop()))
        self._background_tasks.append(asyncio.create_task(self._status_publisher_loop()))
        if self.config.strategy.auto_scan_symbols:
            self._background_tasks.append(asyncio.create_task(self._volatility_scanner_loop()))

        logger.info("Trading engine execution loops active. Awaiting regime opportunities...")
        while self._running:
            await asyncio.sleep(1)

    async def _position_guardian_loop(self) -> None:
        """
        Active Position Guardian:
        1. Evaluates 2-Tier Lock & Run profit taking (+28 bps partial lock) and Breakeven advancement.
        2. Detects stagnant scalps open longer than stagnant_exit_mins and liquidates them.
        """
        while self._running:
            try:
                # 1. Evaluate Tier 1 Partial Take-Profit and Breakeven stops
                stop_updates = await self.risk_manager.evaluate_position_stops()
                for update in stop_updates:
                    sym = update["symbol"]
                    update_type = update.get("type")
                    if update_type == "TIER1_PROFIT_LOCKED":
                        close_qty = update.get("close_qty")
                        new_sl = update.get("new_sl")
                        pnl_est = update.get("pnl_estimate", 0.0)
                        pnl_sign = "+" if pnl_est >= 0 else ""
                        await self.telegram.send_message(
                            f"🎯 *2-Tier Lock & Run Banked: {sym}*\n"
                            f"• Side: `{update['side']}` | Banked 50%: `{close_qty}`\n"
                            f"• Banked Profit (Est): `{pnl_sign}${pnl_est:.4f}`\n"
                            f"• Stop-Loss Locked at: `${new_sl}` (Entry + Fee Buffer)\n"
                            f"• Remaining 50% riding wave risk-free!"
                        )
                        asyncio.create_task(self._sync_closed_pnl(sym))
                    else:
                        new_sl = update["new_sl"]
                        await self.telegram.send_message(
                            f"🛡️ *Breakeven Protected: {sym}*\n"
                            f"• Side: `{update['side']}`\n"
                            f"• Stop-Loss Advanced to: `${new_sl}`\n"
                            f"• Current Mark: `${update['mark_price']:.4f}`\n"
                            f"• Status: Risk-free trade locked in!"
                        )

                # 2. Check for stagnant positions
                stagnant_closed = await self.risk_manager.check_stagnant_positions()
                for closed in stagnant_closed:
                    sym = closed["symbol"]
                    dur = closed["duration_mins"]
                    await self.telegram.send_message(
                        f"⏳ *Stagnant Position Exited: {sym}*\n"
                        f"• Position open for `{dur}` minutes without price expansion.\n"
                        f"• Closed at market (${closed['mark_price']:.4f}) to protect margin."
                    )

                await asyncio.sleep(2.0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in position guardian loop: {e}")
                await asyncio.sleep(5.0)

    async def _on_kline_update(self, symbol: str, interval: str, kline: Dict[str, Any]) -> None:
        """Ingests live kline updates into the feature engine."""
        self.strategy.update_kline(symbol, interval, kline)
        # Check for volatility spike on 1m bars
        clean_int = interval.replace("m", "")
        if clean_int == "1":
            sym_series = self.strategy.series.get(symbol, {})
            tf_1m = sym_series.get("1m") or sym_series.get("1")
            if tf_1m:
                atr = tf_1m.calculate_atr()
                if atr > 0:
                    is_spike = self.risk_manager.evaluate_volatility_spike(symbol, atr)
                    if is_spike:
                        await self.telegram.send_circuit_breaker_alert(
                            "Volatility Spike Kill Switch",
                            f"Extreme ATR spike detected on {symbol} (ATR={atr:.4f}). Halting new orders.",
                        )

    async def _on_execution_update(self, exec_data: Dict[str, Any]) -> None:
        """Processes real-time fills, logs trade metrics, and triggers PnL synchronization."""
        try:
            symbol = exec_data.get("symbol", "")
            side = exec_data.get("side", "")
            exec_price = float(exec_data.get("execPrice", 0.0))
            exec_qty = float(exec_data.get("execQty", 0.0))
            is_maker = exec_data.get("isMaker", False)

            if exec_qty > 0:
                logger.info(
                    f"🎉 [FILLED] {symbol} {side} {exec_qty} filled @ ${exec_price:.4f} "
                    f"({'Maker Rebate' if is_maker else 'Taker'})"
                )
                # Immediately synchronize closed PnL if this was an exit fill
                asyncio.create_task(self._sync_closed_pnl(symbol))
        except Exception as e:
            logger.error(f"Error handling execution update: {e}")

    async def _sync_closed_pnl(self, symbol: Optional[str] = None) -> None:
        """
        Fetches realized closed PnL records directly from Bybit /v5/position/closed-pnl.
        Reliably updates TradeJournal, OnlineStrategyLearner, RiskManager, and AutoTuner.
        """
        try:
            records = await self.client.get_closed_pnl(symbol=symbol, limit=20)
            if not records:
                return

            for rec in records:
                order_id = str(rec.get("orderId") or rec.get("id") or "")
                rec_sym = rec.get("symbol", "")
                if not rec_sym:
                    continue

                updated_time = int(rec.get("updatedTime") or rec.get("createdTime") or (time.time() * 1000))
                record_key = f"{rec_sym}_{order_id}_{updated_time}"
                if record_key in self._processed_closed_trades:
                    continue

                closed_pnl = float(rec.get("closedPnl", 0.0))
                if closed_pnl == 0.0:
                    continue

                self._processed_closed_trades.add(record_key)
                if len(self._processed_closed_trades) > 1000:
                    self._processed_closed_trades = set(list(self._processed_closed_trades)[-500:])

                exit_price = float(rec.get("avgExitPrice") or rec.get("orderPrice") or 0.0)
                entry_price = float(rec.get("avgEntryPrice") or 0.0)
                qty = float(rec.get("closedSize") or rec.get("qty") or 0.0)
                side = rec.get("side", "")
                order_type = rec.get("orderType", "Limit")
                fee_paid = float(rec.get("execFee", 0.0))

                # Context from order or position placement
                ctx = self._position_context.pop(rec_sym, {})
                if not entry_price and ctx.get("price"):
                    entry_price = float(ctx["price"])
                entry_ts = int(ctx.get("placed_at", (updated_time / 1000.0) - 30.0) * 1000)
                holding_sec = max(1.0, round((updated_time - entry_ts) / 1000.0, 1))

                notional = max(1.0, entry_price * qty)
                pnl_pct = (closed_pnl / notional) * 100.0
                regime_str = str(ctx.get("regime", "TREND_PULLBACK"))

                # 1. Update risk manager metrics
                self.risk_manager.record_trade_fill(closed_pnl)

                # 2. Update strategy engine memory & online reinforcement learner
                self.strategy.record_trade_result(
                    pnl=closed_pnl,
                    regime=regime_str,
                    metadata=ctx,
                )

                # 3. Create persistent trade journal record
                tr = TradeRecord(
                    trade_id=order_id or f"closed_{updated_time}",
                    symbol=rec_sym,
                    side=side,
                    entry_time=entry_ts,
                    exit_time=updated_time,
                    holding_time_sec=holding_sec,
                    entry_price=entry_price,
                    exit_price=exit_price,
                    qty=qty,
                    realized_pnl=closed_pnl,
                    realized_pnl_pct=pnl_pct,
                    fee_paid=fee_paid,
                    slippage_bps=round(abs(exit_price - entry_price) / max(0.0001, entry_price) * 10000.0, 1) if entry_price > 0 else 0.0,
                    order_type=order_type,
                    regime=regime_str,
                    conviction=float(ctx.get("conviction", 0.8)),
                    atr_at_entry=float(ctx.get("atr", 0.0)),
                    ema_spread_at_entry=float(ctx.get("ema_spread", 0.0)),
                    vwap_delta_at_entry=float(ctx.get("vwap_delta", 0.0)),
                    ofi_at_entry=float(ctx.get("ofi", 0.0)),
                )
                self.journal.record_trade(tr)
                self._last_trade_exit[rec_sym] = time.time()

                # 4. Dispatch Telegram exit alert
                await self.telegram.send_trade_exit(
                    symbol=rec_sym,
                    side=side,
                    exit_price=exit_price,
                    pnl=closed_pnl,
                    pnl_pct=pnl_pct,
                    holding_sec=holding_sec,
                )

                # 5. Evaluate dynamic parameter auto-tuning
                tuning_res = self.auto_tuner.evaluate_and_tune()
                if tuning_res:
                    await self.telegram.send_message(
                        f"⚙️ *Self-Adaptive Tuning Activated*\n{tuning_res['rationale']}"
                    )
        except Exception as e:
            logger.error(f"Error syncing closed PnL for {symbol}: {e}")

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
                existing = self.risk_manager.positions.get(symbol)
                ctx = self._position_context.get(symbol, {})
                entry_atr = existing.entry_atr if (existing and existing.entry_atr > 0) else float(ctx.get("atr", 0.0))
                opened_at = existing.opened_at if (existing and existing.opened_at > 0) else float(ctx.get("placed_at", time.time()))
                breakeven_set = existing.breakeven_set if existing else False
                entry_features = existing.entry_features if existing else ctx
                tp1_price = existing.tp1_price if (existing and existing.tp1_price) else ctx.get("tp1_price")
                tp1_filled = existing.tp1_filled if existing else False
                original_size = existing.original_size if (existing and existing.original_size > 0) else (ctx.get("size") or size)

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
                    entry_atr=entry_atr,
                    breakeven_set=breakeven_set,
                    opened_at=opened_at,
                    entry_features=entry_features,
                    tp1_price=tp1_price,
                    tp1_filled=tp1_filled,
                    original_size=original_size,
                )
            else:
                if symbol in self.risk_manager.positions:
                    self.risk_manager.positions.pop(symbol, None)
                    asyncio.create_task(self._sync_closed_pnl(symbol))
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

                # Periodically update funding rates (every 3 minutes)
                if now - self._last_funding_fetch > 180:
                    self._last_funding_fetch = now
                    for s in self.config.strategy.symbols:
                        try:
                            self._funding_rates[s] = await self.client.get_funding_rate(s)
                        except Exception:
                            pass

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

                    # Don't open if in post-trade cooldown for this symbol
                    cooldown_sec = float(self.config.execution.trade_cooldown_mins) * 60.0
                    if (now - self._last_trade_exit.get(symbol, 0.0)) < cooldown_sec:
                        continue

                    # Don't submit new order if there's already an active open/pending order for this symbol
                    if any(o.get("symbol") == symbol for o in self._pending_orders.values()):
                        continue

                    # Generate signal with adverse funding filter
                    fr = self._funding_rates.get(symbol, 0.0)
                    signal = self.strategy.generate_signal(symbol, ob, funding_rate=fr)
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
                ctx = {
                    "symbol": sig.symbol,
                    "side": sig.side,
                    "price": sig.price,
                    "qty": qty,
                    "size": qty,
                    "placed_at": time.time(),
                    "regime": sig.regime.value,
                    "conviction": sig.conviction,
                    "atr": sig.atr,
                    "ema_spread": sig.metadata.get("ema9", 0.0) - sig.metadata.get("ema50", 0.0),
                    "vwap_delta": sig.price - sig.metadata.get("vwap", sig.price),
                    "ofi": sig.metadata.get("ofi", 0.0),
                    "r2": sig.metadata.get("r2_1m", 0.0),
                    "tp1_price": sig.tp1_price,
                    "tp2_price": sig.tp2_price,
                }
                self._position_context[sig.symbol] = ctx
                self._order_context[order_id] = ctx
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
            ctx = {
                "symbol": sig.symbol,
                "side": sig.side,
                "price": sig.price,
                "qty": qty,
                "size": qty,
                "placed_at": time.time(),
                "regime": sig.regime.value,
                "conviction": sig.conviction,
                "atr": sig.atr,
                "ema_spread": sig.metadata.get("ema9", 0.0) - sig.metadata.get("ema50", 0.0),
                "vwap_delta": sig.price - sig.metadata.get("vwap", sig.price),
                "ofi": sig.metadata.get("ofi", 0.0),
                "r2": sig.metadata.get("r2_1m", 0.0),
                "tp1_price": sig.tp1_price,
                "tp2_price": sig.tp2_price,
            }
            self._position_context[sig.symbol] = ctx
            self._order_context[order_id] = ctx
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
        return format_qty(symbol, raw_qty)

    async def _volatility_scanner_loop(self) -> None:
        """Periodic background task executing the Autonomous Volatility & Volume Scanner."""
        interval_sec = max(60, int(self.config.strategy.scanner_interval_mins * 60))
        # Initial sleep of 20s to allow startup routines to complete
        await asyncio.sleep(20)
        while self._running:
            try:
                await self._scan_and_update_symbols()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in volatility scanner loop: {e}")
            await asyncio.sleep(interval_sec)

    async def _scan_and_update_symbols(self) -> None:
        """
        Autonomous Volatility & Volume Scanner:
        Queries Bybit V5 market tickers, filters liquid linear perpetuals (turnover > $30M),
        ranks by combined 24h volatility & volume momentum, and dynamically rotates active symbols.
        """
        if not self.config.strategy.auto_scan_symbols:
            return

        try:
            logger.info("🔍 [SCANNER] Scanning Bybit linear perpetuals for high-volatility & liquid pairs...")
            tickers = await self.client.get_market_tickers(category="linear")
            if not tickers:
                logger.warning("Scanner received empty ticker response from Bybit.")
                return

            candidates = []
            min_turnover = float(self.config.strategy.scanner_min_turnover_usd)
            excluded = {"USDCUSDT", "FDUSDUSDT", "USDEUSDT", "USDYUSDT", "BUSDUSDT"}

            for t in tickers:
                sym = t.get("symbol", "")
                if not sym.endswith("USDT") or sym in excluded:
                    continue

                turnover = _to_float(t.get("turnover24h"))
                if turnover < min_turnover:
                    continue

                last_price = _to_float(t.get("lastPrice"))
                if last_price <= 0:
                    continue

                high = _to_float(t.get("highPrice24h"))
                low = _to_float(t.get("lowPrice24h"))
                price_pct = abs(_to_float(t.get("price24hPcnt")))
                range_pct = ((high - low) / last_price) if last_price > 0 else 0.0
                volatility_score = max(price_pct, range_pct)

                score = volatility_score * (turnover ** 0.3)
                candidates.append({
                    "symbol": sym,
                    "turnover": turnover,
                    "volatility": volatility_score,
                    "last_price": last_price,
                    "score": score,
                })

            if not candidates:
                logger.info(f"No candidates satisfied turnover >= ${min_turnover:,.0f}. Preserving current symbols.")
                return

            candidates.sort(key=lambda x: x["score"], reverse=True)
            top_n = self.config.strategy.scanner_top_n
            selected_symbols = [c["symbol"] for c in candidates[:top_n]]

            # Ensure any active open position symbols are preserved so we never orphan an active trade
            for open_sym in self.risk_manager.positions.keys():
                if open_sym not in selected_symbols:
                    selected_symbols.append(open_sym)

            current_symbols = list(self.config.strategy.symbols)
            if set(selected_symbols) != set(current_symbols):
                added = [s for s in selected_symbols if s not in current_symbols]
                removed = [s for s in current_symbols if s not in selected_symbols]
                logger.info(
                    f"🔄 [VOLATILITY SCANNER] Symbol rotation: Old: {current_symbols} -> "
                    f"New: {selected_symbols} (Added: {added}, Removed: {removed})"
                )

                # Initialize leverage, klines, and subscriptions for new pairs
                for sym in added:
                    if self.config.trading_mode.value == "linear":
                        try:
                            await self.client.set_leverage(sym, self.config.risk.default_leverage)
                        except Exception as e:
                            logger.debug(f"Failed to set leverage for {sym}: {e}")

                    for tf in self.config.strategy.timeframes:
                        try:
                            klines = await self.client.get_klines(symbol=sym, interval=tf, limit=100)
                            for k in klines:
                                self.strategy.update_kline(sym, tf, k)
                        except Exception as e:
                            logger.debug(f"Failed to prime klines for {sym} {tf}: {e}")

                    # Subscribe to orderbook and klines
                    topics = [f"orderbook.50.{sym}"]
                    for tf in self.config.strategy.timeframes:
                        clean_tf = tf.replace("m", "")
                        topics.append(f"kline.{clean_tf}.{sym}")
                    await self.client.add_public_subscriptions(topics)

                self.config.strategy.symbols = selected_symbols
                await self.telegram.send_message(
                    f"📡 *Autonomous Volatility Scanner Rotated Pairs*\n"
                    f"• Active Universe: `{', '.join(selected_symbols)}`\n"
                    f"• Top Liquid Candidates: `{', '.join(added or ['None'])}`\n"
                    f"• Selection: Top 24h Volatility + Turnover > ${min_turnover / 1_000_000:.0f}M"
                )
            else:
                logger.debug(f"Scanner confirmed active universe optimal: {selected_symbols}")

        except Exception as e:
            logger.error(f"Error in volatility scanner: {e}")

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

                # Periodically sync realized closed PnL from Bybit exchange
                await self._sync_closed_pnl()
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
