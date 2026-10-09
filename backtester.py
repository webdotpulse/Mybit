#!/usr/bin/env python3
"""
Quantitative Offline & Historical Backtesting Engine for Bybit V5 Strategies.
Simulates multi-timeframe regime scalping, Order Flow Imbalance, Maker-first execution,
ATR brackets, Breakeven advancement, Stagnant position exits, fee drag, and Kelly compounding.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from bybit_client import OrderBookL2
from config import AppConfig, CapitalTier, ExecutionConfig, RiskConfig, StrategyConfig, get_tier_for_equity
from strategy import Bar, MarketRegime, StrategyEngine, TimeframeSeries

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backtester")
console = Console()


@dataclass
class BacktestTrade:
    trade_id: int
    symbol: str
    side: str
    entry_time: int
    exit_time: int
    holding_mins: float
    entry_price: float
    exit_price: float
    qty: float
    notional: float
    pnl: float
    pnl_pct: float
    fees: float
    exit_reason: str  # "TP", "SL", "BREAKEVEN", "STAGNANT", "END_OF_DATA"
    regime: str
    atr: float


@dataclass
class BacktestResult:
    symbol: str
    start_time: int
    end_time: int
    total_bars: int
    initial_capital: float
    final_equity: float
    net_profit: float
    net_profit_pct: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    breakeven_trades: int
    win_rate: float
    profit_factor: float
    max_drawdown_usd: float
    max_drawdown_pct: float
    sharpe_ratio: float
    total_fees_paid: float
    trades: List[BacktestTrade] = field(default_factory=list)


class HistoricalDataFetcher:
    """Fetches historical klines directly from Bybit V5 public REST API."""

    BASE_URL = "https://api.bybit.com/v5/market/kline"

    @classmethod
    def fetch_klines(
        cls,
        symbol: str,
        interval: str = "1",
        limit_bars: int = 2000,
    ) -> List[Bar]:
        """
        Fetches up to `limit_bars` 1m candles paginating backwards.
        Bybit returns newest first, so we reverse to chronological order.
        """
        clean_interval = interval.replace("m", "")
        all_bars: List[Bar] = []
        end_cursor: Optional[int] = None

        console.print(f"[dim]Fetching {limit_bars} historical {interval} candles for {symbol} from Bybit V5...[/dim]")

        while len(all_bars) < limit_bars:
            batch_size = min(1000, limit_bars - len(all_bars))
            url = f"{cls.BASE_URL}?category=linear&symbol={symbol}&interval={clean_interval}&limit={batch_size}"
            if end_cursor is not None:
                url += f"&end={end_cursor}"

            req = urllib.request.Request(url, headers={"User-Agent": "MybitBacktester/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                logger.warning(f"Error fetching historical data: {e}")
                break

            if payload.get("retCode") != 0:
                logger.warning(f"Bybit API error: {payload.get('retMsg')}")
                break

            raw_list = payload.get("result", {}).get("list", [])
            if not raw_list:
                break

            for item in raw_list:
                # Bybit format: [start, open, high, low, close, volume, turnover]
                b = Bar(
                    timestamp=int(item[0]),
                    open=float(item[1]),
                    high=float(item[2]),
                    low=float(item[3]),
                    close=float(item[4]),
                    volume=float(item[5]),
                    turnover=float(item[6]) if len(item) > 6 else 0.0,
                )
                all_bars.append(b)

            # Oldest bar timestamp in this batch minus 1ms for pagination
            oldest_ts = int(raw_list[-1][0])
            end_cursor = oldest_ts - 1

            if len(raw_list) < batch_size:
                break

        # Sort chronologically (oldest -> newest)
        all_bars.sort(key=lambda x: x.timestamp)
        return all_bars


class QuantitativeBacktester:
    """
    Simulates strategy execution against historical 1m bars.
    Includes Maker-first execution, Breakeven advancement, Stagnant exits,
    exchange minimum contract sizing, and Kelly compounding.
    """

    def __init__(
        self,
        config: AppConfig,
        initial_capital: float = 100.0,
        maker_fee_rate: float = 0.0002,   # 0.02% Bybit maker fee
        taker_fee_rate: float = 0.00055,  # 0.055% Bybit taker fee
        slippage_bps: float = 1.5,        # 1.5 bps estimated execution slippage
    ):
        self.config = config
        self.initial_capital = initial_capital
        self.maker_fee = maker_fee_rate
        self.taker_fee = taker_fee_rate
        self.slippage_pct = (slippage_bps / 10000.0)

    def run(self, symbol: str, bars: List[Bar]) -> BacktestResult:
        if len(bars) < 100:
            raise ValueError(f"Need at least 100 bars for feature warm-up, got {len(bars)}")

        # Initialize fresh strategy engine with multi-timeframe structures
        strat = StrategyEngine(self.config)
        tf_1m = TimeframeSeries(symbol, "1m", max_bars=300)
        tf_5m = TimeframeSeries(symbol, "5m", max_bars=300)
        tf_15m = TimeframeSeries(symbol, "15m", max_bars=300)
        strat.series[symbol] = {"1m": tf_1m, "5m": tf_5m, "15m": tf_15m}

        equity = self.initial_capital
        hwm = equity
        max_drawdown_usd = 0.0
        max_drawdown_pct = 0.0
        total_fees = 0.0
        closed_trades: List[BacktestTrade] = []
        trade_id_counter = 1

        # Current open position simulation state
        open_pos: Optional[Dict[str, Any]] = None

        def aggregate_to_tf(series: TimeframeSeries, b: Bar, interval_ms: int) -> None:
            bucket_ts = (b.timestamp // interval_ms) * interval_ms
            if not series.bars:
                series.add_or_update_bar(Bar(timestamp=bucket_ts, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume, turnover=b.turnover))
                return
            last = series.bars[-1]
            if last.timestamp == bucket_ts:
                updated = Bar(
                    timestamp=bucket_ts,
                    open=last.open,
                    high=max(last.high, b.high),
                    low=min(last.low, b.low),
                    close=b.close,
                    volume=last.volume + b.volume,
                    turnover=last.turnover + b.turnover,
                )
                series.bars[-1] = updated
            else:
                series.add_or_update_bar(Bar(timestamp=bucket_ts, open=b.open, high=b.high, low=b.low, close=b.close, volume=b.volume, turnover=b.turnover))

        # Warm up technical indicators with first 60 bars
        warmup_bars = min(60, len(bars) // 4)
        for b in bars[:warmup_bars]:
            tf_1m.add_or_update_bar(b)
            aggregate_to_tf(tf_5m, b, 300_000)
            aggregate_to_tf(tf_15m, b, 900_000)

        # Simulation loop
        cooldown_until_ts = 0

        for i in range(warmup_bars, len(bars)):
            bar = bars[i]
            tf_1m.add_or_update_bar(bar)
            aggregate_to_tf(tf_5m, bar, 300_000)
            aggregate_to_tf(tf_15m, bar, 900_000)

            # Update drawdown tracking
            if equity > hwm:
                hwm = equity
            dd_usd = hwm - equity
            dd_pct = (dd_usd / hwm) * 100.0 if hwm > 0 else 0.0
            if dd_usd > max_drawdown_usd:
                max_drawdown_usd = dd_usd
            if dd_pct > max_drawdown_pct:
                max_drawdown_pct = dd_pct

            # ----------------------------------------------------
            # 1. Manage Active Position (Check TP, SL, BE, Stagnant)
            # Only evaluate on bars strictly after entry
            # ----------------------------------------------------
            if open_pos is not None and bar.timestamp > open_pos["opened_at"]:
                side = open_pos["side"]
                entry_px = open_pos["entry_price"]
                sl_px = open_pos["sl_price"]
                tp_px = open_pos["tp_price"]
                atr = open_pos["atr"]
                be_set = open_pos["breakeven_set"]
                opened_at = open_pos["opened_at"]
                duration_mins = (bar.timestamp - opened_at) / 60000.0
                qty = open_pos["qty"]
                notional = qty * entry_px

                # A0. Check 2-Tier "Lock & Run" Partial Take-Profit (+28 bps)
                exec_cfg = self.config.execution
                if exec_cfg.tiered_tp_enabled and not open_pos["tp1_filled"] and open_pos.get("tp1_price"):
                    tp1_hit = (bar.high >= open_pos["tp1_price"]) if side == "Buy" else (bar.low <= open_pos["tp1_price"])
                    if tp1_hit:
                        tp1_qty = open_pos["qty"] * exec_cfg.tp1_ratio
                        tp1_px = open_pos["tp1_price"]
                        tp1_gross = (tp1_px - entry_px) * tp1_qty if side == "Buy" else (entry_px - tp1_px) * tp1_qty
                        tp1_fee = (tp1_qty * tp1_px) * self.taker_fee
                        portion_entry_fee = open_pos["entry_fee"] * exec_cfg.tp1_ratio
                        open_pos["realized_tp1_pnl"] = tp1_gross - portion_entry_fee - tp1_fee
                        open_pos["realized_tp1_fee"] = portion_entry_fee + tp1_fee
                        open_pos["entry_fee"] -= portion_entry_fee
                        open_pos["qty"] -= tp1_qty
                        equity += open_pos["realized_tp1_pnl"]
                        total_fees += (portion_entry_fee + tp1_fee)
                        open_pos["tp1_filled"] = True
                        open_pos["breakeven_set"] = True
                        # Advance stop loss on remaining half to entry + buffer (risk-free run)
                        fee_buffer = entry_px * (exec_cfg.breakeven_buffer_bps / 10000.0)
                        open_pos["sl_price"] = entry_px + fee_buffer if side == "Buy" else entry_px - fee_buffer

                # A. Check Exit Triggers against Bar High/Low (prior to same-bar breakeven)
                exit_occurred = False
                exit_price = 0.0
                exit_reason = ""

                if side == "Buy":
                    # Check TP and SL with realistic intraday sequence
                    if bar.high >= tp_px:
                        exit_price = tp_px * (1.0 - self.slippage_pct)
                        exit_reason = "TP"
                        exit_occurred = True
                    elif bar.low <= sl_px:
                        exit_price = sl_px * (1.0 - self.slippage_pct)
                        exit_reason = "BREAKEVEN" if open_pos["breakeven_set"] else "SL"
                        exit_occurred = True
                else:  # Sell / Short
                    if bar.low <= tp_px:
                        exit_price = tp_px * (1.0 + self.slippage_pct)
                        exit_reason = "TP"
                        exit_occurred = True
                    elif bar.high >= sl_px:
                        exit_price = sl_px * (1.0 + self.slippage_pct)
                        exit_reason = "BREAKEVEN" if open_pos["breakeven_set"] else "SL"
                        exit_occurred = True

                # B. Advance Stop Loss to Breakeven for subsequent price action
                be_trigger = self.config.execution.breakeven_atr_trigger
                be_buffer = entry_px * (self.config.execution.breakeven_buffer_bps / 10000.0)
                if not exit_occurred and not be_set and be_trigger > 0:
                    if side == "Buy" and bar.high >= (entry_px + be_trigger * atr):
                        open_pos["sl_price"] = entry_px + be_buffer
                        open_pos["breakeven_set"] = True
                    elif side == "Sell" and bar.low <= (entry_px - be_trigger * atr):
                        open_pos["sl_price"] = entry_px - be_buffer
                        open_pos["breakeven_set"] = True

                # C. Check Stagnant Position Timeout
                if not exit_occurred and self.config.execution.stagnant_exit_mins > 0 and duration_mins >= self.config.execution.stagnant_exit_mins:
                    price_delta = abs(bar.close - entry_px)
                    if price_delta <= (self.config.execution.stagnant_atr_threshold * atr):
                        exit_price = bar.close
                        exit_reason = "STAGNANT"
                        exit_occurred = True

                # D. Process Position Closure
                if exit_occurred:
                    rem_qty = open_pos["qty"]
                    if side == "Buy":
                        gross_pnl = (exit_price - entry_px) * rem_qty
                    else:
                        gross_pnl = (entry_px - exit_price) * rem_qty

                    exit_fee = (rem_qty * exit_price) * (self.maker_fee if exit_reason == "TP" else self.taker_fee)
                    net_trade_pnl = gross_pnl - open_pos["entry_fee"] - exit_fee + open_pos["realized_tp1_pnl"]
                    total_fees += (open_pos["entry_fee"] + exit_fee)
                    equity += (gross_pnl - open_pos["entry_fee"] - exit_fee)
                    all_fees = open_pos["entry_fee"] + exit_fee + open_pos["realized_tp1_fee"]

                    if open_pos["tp1_filled"]:
                        exit_reason = f"TP1+{exit_reason}"

                    strat.record_trade_result(net_trade_pnl, regime=open_pos["regime"])

                    orig_qty = open_pos["original_qty"]
                    orig_notional = orig_qty * entry_px
                    closed_trades.append(
                        BacktestTrade(
                            trade_id=trade_id_counter,
                            symbol=symbol,
                            side=side,
                            entry_time=opened_at,
                            exit_time=bar.timestamp,
                            holding_mins=round(duration_mins, 1),
                            entry_price=entry_px,
                            exit_price=round(exit_price, 4),
                            qty=orig_qty,
                            notional=round(orig_notional, 2),
                            pnl=round(net_trade_pnl, 4),
                            pnl_pct=round((net_trade_pnl / max(1.0, orig_notional / self.config.risk.default_leverage)) * 100.0, 2),
                            fees=round(all_fees, 4),
                            exit_reason=exit_reason,
                            regime=open_pos["regime"],
                            atr=round(atr, 4),
                        )
                    )
                    trade_id_counter += 1
                    open_pos = None
                    cooldown_until_ts = bar.timestamp + (self.config.execution.trade_cooldown_mins * 60000)

            # ----------------------------------------------------
            # 2. Evaluate Strategy Signal on Completed Bar
            # ----------------------------------------------------
            if open_pos is None and bar.timestamp >= cooldown_until_ts:
                # Synthesize simulated top-of-book orderbook snapshot
                spread = max(0.0001, bar.close * 0.0002)  # ~2 bps spread
                best_bid = round(bar.close - (spread / 2.0), 5)
                best_ask = round(bar.close + (spread / 2.0), 5)
                ob = OrderBookL2(symbol)
                ob.bids = {best_bid: 500.0, best_bid - spread: 1000.0}
                ob.asks = {best_ask: 500.0, best_ask + spread: 1000.0}
                # Synthetic buyer/seller imbalance from candle delta
                candle_delta = bar.close - bar.open
                ob.ofi = 15.0 if candle_delta > 0 else (-15.0 if candle_delta < 0 else 0.0)

                signal = strat.generate_signal(symbol, ob)
                if signal:
                    # Half-Kelly position sizing
                    kelly_fraction = strat.calculate_kelly_fraction()
                    leverage = self.config.risk.default_leverage
                    desired_notional = equity * kelly_fraction * leverage

                    # Enforce exchange minimum floor
                    min_floor = 5.50
                    notional = max(min_floor, desired_notional)
                    qty = notional / signal.price

                    # Small accounts safety cap: do not allocate more than 35% margin to one trade
                    max_allowed_margin = equity * 0.35
                    if (notional / leverage) > max_allowed_margin:
                        notional = max_allowed_margin * leverage
                        qty = notional / signal.price

                    if qty > 0 and (notional / leverage) <= equity:
                        entry_fee = notional * (self.maker_fee if signal.time_in_force == "PostOnly" else self.taker_fee)
                        open_pos = {
                            "side": signal.side,
                            "entry_price": signal.price,
                            "tp_price": signal.tp_price,
                            "sl_price": signal.sl_price,
                            "atr": signal.atr,
                            "qty": qty,
                            "original_qty": qty,
                            "entry_fee": entry_fee,
                            "opened_at": bar.timestamp,
                            "breakeven_set": False,
                            "regime": signal.regime.value,
                            "tp1_price": signal.tp1_price,
                            "tp1_filled": False,
                            "realized_tp1_pnl": 0.0,
                            "realized_tp1_fee": 0.0,
                        }

        # Close open position at end of backtest if still open
        if open_pos is not None and bars:
            last_bar = bars[-1]
            side = open_pos["side"]
            entry_px = open_pos["entry_price"]
            exit_px = last_bar.close
            rem_qty = open_pos["qty"]
            gross_pnl = (exit_px - entry_px) * rem_qty if side == "Buy" else (entry_px - exit_px) * rem_qty
            exit_fee = (rem_qty * exit_px) * self.taker_fee
            net_trade_pnl = gross_pnl - open_pos["entry_fee"] - exit_fee + open_pos["realized_tp1_pnl"]
            equity += (gross_pnl - open_pos["entry_fee"] - exit_fee)
            all_fees = open_pos["entry_fee"] + exit_fee + open_pos["realized_tp1_fee"]
            total_fees += (open_pos["entry_fee"] + exit_fee)
            reason = "TP1+END_OF_DATA" if open_pos["tp1_filled"] else "END_OF_DATA"
            orig_qty = open_pos["original_qty"]
            orig_notional = orig_qty * entry_px
            closed_trades.append(
                BacktestTrade(
                    trade_id=trade_id_counter,
                    symbol=symbol,
                    side=side,
                    entry_time=open_pos["opened_at"],
                    exit_time=last_bar.timestamp,
                    holding_mins=round((last_bar.timestamp - open_pos["opened_at"]) / 60000.0, 1),
                    entry_price=entry_px,
                    exit_price=round(exit_px, 4),
                    qty=orig_qty,
                    notional=round(orig_notional, 2),
                    pnl=round(net_trade_pnl, 4),
                    pnl_pct=round((net_trade_pnl / max(1.0, orig_notional / self.config.risk.default_leverage)) * 100.0, 2),
                    fees=round(all_fees, 4),
                    exit_reason=reason,
                    regime=open_pos["regime"],
                    atr=round(open_pos["atr"], 4),
                )
            )

        # ----------------------------------------------------
        # 3. Compute Comprehensive Performance Statistics
        # ----------------------------------------------------
        total_trades = len(closed_trades)
        breakevens = [t for t in closed_trades if abs(t.pnl) <= 0.0005 or (t.exit_reason == "BREAKEVEN" and t.pnl <= 0)]
        wins = [t for t in closed_trades if t.pnl > 0.0005]
        losses = [t for t in closed_trades if t.pnl < -0.0005 and t not in breakevens]

        # Win rate counts winning trades over all non-scratch decisive trades
        decisive_trades = len(wins) + len(losses)
        win_rate = (len(wins) / decisive_trades) * 100.0 if decisive_trades > 0 else 0.0
        sum_wins = sum(t.pnl for t in wins)
        sum_losses = abs(sum(t.pnl for t in losses))
        profit_factor = (sum_wins / sum_losses) if sum_losses > 0 else (99.0 if sum_wins > 0 else 0.0)

        pnls = [t.pnl for t in closed_trades]
        pnl_mean = float(np.mean(pnls)) if pnls else 0.0
        pnl_std = float(np.std(pnls)) if pnls else 0.0
        sharpe = (pnl_mean / pnl_std * math.sqrt(252 * 14)) if pnl_std > 0 else 0.0

        net_profit = equity - self.initial_capital
        net_profit_pct = (net_profit / self.initial_capital) * 100.0

        return BacktestResult(
            symbol=symbol,
            start_time=bars[0].timestamp if bars else 0,
            end_time=bars[-1].timestamp if bars else 0,
            total_bars=len(bars),
            initial_capital=self.initial_capital,
            final_equity=round(equity, 2),
            net_profit=round(net_profit, 2),
            net_profit_pct=round(net_profit_pct, 2),
            total_trades=total_trades,
            winning_trades=len(wins),
            losing_trades=len(losses),
            breakeven_trades=len(breakevens),
            win_rate=round(win_rate, 1),
            profit_factor=round(profit_factor, 2),
            max_drawdown_usd=round(max_drawdown_usd, 2),
            max_drawdown_pct=round(max_drawdown_pct, 2),
            sharpe_ratio=round(sharpe, 2),
            total_fees_paid=round(total_fees, 2),
            trades=closed_trades,
        )


def render_backtest_report(result: BacktestResult) -> None:
    """Renders comprehensive, institutional-style backtest telemetry."""
    pnl_color = "green" if result.net_profit >= 0 else "red"
    pnl_sign = "+" if result.net_profit >= 0 else ""

    start_date = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(result.start_time / 1000))
    end_date = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(result.end_time / 1000))

    console.print()
    console.print(
        Panel(
            f"[bold cyan]BYBIT V5 QUANTITATIVE BACKTEST REPORT — {result.symbol}[/bold cyan]\n"
            f"[dim]Period: {start_date} → {end_date} | Total 1m Bars: {result.total_bars:,}[/dim]",
            border_style="cyan",
            padding=(0, 2),
        )
    )

    # Executive KPI Table
    kpi_table = Table(box=None, padding=(0, 2), show_header=False)
    kpi_table.add_column("Key", style="bold white")
    kpi_table.add_column("Value", style="cyan")
    kpi_table.add_column("Key2", style="bold white")
    kpi_table.add_column("Value2", style="cyan")

    kpi_table.add_row(
        "Initial Capital:", f"${result.initial_capital:,.2f}",
        "Net Realized PnL:", f"[{pnl_color}]{pnl_sign}${result.net_profit:,.2f} ({pnl_sign}{result.net_profit_pct:.2f}%)[/{pnl_color}]"
    )
    kpi_table.add_row(
        "Final Equity:", f"${result.final_equity:,.2f}",
        "Profit Factor:", f"[bold {pnl_color}]{result.profit_factor:.2f}[/bold {pnl_color}]"
    )
    kpi_table.add_row(
        "Total Trades:", str(result.total_trades),
        "Win Rate:", f"[bold {'green' if result.win_rate >= 50 else 'yellow'}]{result.win_rate:.1f}%[/] ({result.winning_trades}W / {result.losing_trades}L)"
    )
    kpi_table.add_row(
        "Breakeven Exits:", str(result.breakeven_trades),
        "Max Drawdown:", f"[red]-${result.max_drawdown_usd:,.2f} (-{result.max_drawdown_pct:.2f}%)[/red]"
    )
    kpi_table.add_row(
        "Sharpe Ratio (Ann.):", f"{result.sharpe_ratio:.2f}",
        "Exchange Fees Paid:", f"${result.total_fees_paid:,.2f}"
    )

    console.print(
        Panel(
            kpi_table,
            title="[bold yellow]Performance Summary[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )
    )

    # Recent Trade Journal sample
    if result.trades:
        trade_table = Table(
            title=f"Sample Trades Log (Recent {min(15, len(result.trades))} of {len(result.trades)})",
            border_style="dim",
            padding=(0, 1),
        )
        trade_table.add_column("#", style="dim", width=4)
        trade_table.add_column("Side", width=5)
        trade_table.add_column("Entry Px", justify="right")
        trade_table.add_column("Exit Px", justify="right")
        trade_table.add_column("Holding", justify="right")
        trade_table.add_column("Reason", width=10)
        trade_table.add_column("PnL ($)", justify="right")
        trade_table.add_column("Return %", justify="right")

        sample = result.trades[-15:]
        for t in sample:
            side_color = "green" if t.side == "Buy" else "red"
            t_color = "green" if t.pnl > 0 else ("red" if t.pnl < 0 else "dim")
            trade_table.add_row(
                str(t.trade_id),
                f"[{side_color}]{t.side}[/{side_color}]",
                f"${t.entry_price:.4f}",
                f"${t.exit_price:.4f}",
                f"{t.holding_mins}m",
                t.exit_reason,
                f"[{t_color}]{'+' if t.pnl > 0 else ''}${t.pnl:.2f}[/{t_color}]",
                f"[{t_color}]{'+' if t.pnl_pct > 0 else ''}{t.pnl_pct:.1f}%[/{t_color}]",
            )

        console.print(trade_table)
    console.print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit V5 Strategy Backtester")
    parser.add_argument("--symbol", default="DOGEUSDT", help="Symbol to backtest (default: DOGEUSDT)")
    parser.add_argument("--bars", type=int, default=3000, help="Number of 1m bars to fetch (default: 3000 ~ 2.1 days)")
    parser.add_argument("--capital", type=float, default=100.0, help="Starting capital pool in USD (default: 100.0)")
    parser.add_argument("--leverage", type=int, default=5, help="Leverage multiplier (default: 5)")
    parser.add_argument("--tp-mult", type=float, default=1.4, help="Take profit ATR multiplier (default: 1.4)")
    parser.add_argument("--sl-mult", type=float, default=0.8, help="Stop loss ATR multiplier (default: 0.8)")
    parser.add_argument("--be-trigger", type=float, default=1.2, help="Breakeven ATR trigger (default: 1.2)")
    parser.add_argument("--tp1-bps", type=float, default=50.0, help="Tier 1 take profit in bps (default: 50.0)")
    parser.add_argument("--no-tiered-tp", action="store_true", help="Disable 2-Tier Lock & Run take-profit")
    args = parser.parse_args()

    config = AppConfig()
    config.risk.allocated_capital_usd = args.capital
    config.risk.default_leverage = args.leverage
    config.execution.bracket_tp_atr_mult = args.tp_mult
    config.execution.bracket_sl_atr_mult = args.sl_mult
    config.execution.breakeven_atr_trigger = args.be_trigger
    config.execution.tp1_bps = args.tp1_bps
    config.execution.tiered_tp_enabled = not args.no_tiered_tp

    bars = HistoricalDataFetcher.fetch_klines(
        symbol=args.symbol,
        interval="1",
        limit_bars=args.bars,
    )

    if not bars:
        console.print("[bold red]✕ Failed to retrieve historical data from Bybit.[/bold red]")
        sys.exit(1)

    backtester = QuantitativeBacktester(
        config=config,
        initial_capital=args.capital,
    )

    console.print(f"[bold green]Running simulation on {len(bars)} bars with ${args.capital:.2f} capital...[/bold green]")
    result = backtester.run(args.symbol, bars)
    render_backtest_report(result)


if __name__ == "__main__":
    main()
