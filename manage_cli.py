#!/usr/bin/env python3
"""
Operational Management CLI for Bybit V5 Autonomous Engine.
Powers status dashboard, emergency panic liquidation, and database queries.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from bybit_client import BybitV5Client  # noqa: E402
from config import load_config, load_credentials  # noqa: E402
from trade_journal import TradeJournal  # noqa: E402

console = Console()


def render_status() -> None:
    """Renders comprehensive terminal dashboard of live engine metrics."""
    config = load_config(BASE_DIR)
    status_file = config.data_dir / "engine_status.json"
    db_file = config.data_dir / "trading_journal.db"

    # Header
    console.print(
        Panel(
            "[bold cyan]BYBIT V5 AUTONOMOUS ENGINE — LIVE MONITOR[/bold cyan]\n"
            f"[dim]Mode: {config.trading_mode.value.upper()} | Strategy: Multi-Timeframe OFI Trend Scalping[/dim]",
            border_style="cyan",
            padding=(0, 2),
        )
    )

    data: Dict[str, Any] = {}
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text())
        except Exception:
            pass

    # Status & Account Card
    engine_state = data.get("status", "STOPPED / OFFLINE")
    state_style = "bold green" if engine_state == "RUNNING" else "bold red"

    equity = data.get("equity", config.risk.allocated_capital_usd)
    hwm = data.get("high_water_mark", equity)
    drawdown = data.get("daily_drawdown_pct", 0.0)

    acc_table = Table(show_header=False, box=None, padding=(0, 2))
    acc_table.add_column("Key", style="bold yellow")
    acc_table.add_column("Val")
    acc_table.add_column("Key2", style="bold yellow")
    acc_table.add_column("Val2")

    acc_table.add_row(
        "Engine Daemon:",
        Text(engine_state, style=state_style),
        "Account Equity:",
        f"[bold white]${equity:,.2f}[/bold white]",
    )
    acc_table.add_row(
        "Environment:",
        f"[magenta]{'TESTNET' if data.get('testnet', True) else 'MAINNET'}[/magenta]",
        "High Water Mark:",
        f"${hwm:,.2f}",
    )
    raw_tier = data.get("capital_tier") or getattr(config.risk, "current_tier", "STANDARD")
    tier_name = raw_tier.value if hasattr(raw_tier, "value") else str(raw_tier).replace("CapitalTier.", "")
    auto_active = data.get("auto_tier_enabled", config.risk.auto_tier_by_equity)
    tier_display = f"[bold cyan]{tier_name}[/bold cyan]" + (" [green](Auto-Config)[/green]" if auto_active else "")

    acc_table.add_row(
        "Trading Mode:",
        f"[cyan]{data.get('trading_mode', config.trading_mode.value).upper()}[/cyan]",
        "Daily Drawdown:",
        f"[{'red' if drawdown >= 2.0 else 'green'}]{drawdown:.2f}% (Limit: {config.risk.max_daily_risk_pct}%)[/{'red' if drawdown >= 2.0 else 'green'}]",
    )
    acc_table.add_row(
        "Capital Tier:",
        tier_display,
        "Active Pairs:",
        f"[bold white]{', '.join(data.get('symbols', config.strategy.symbols))}[/bold white]",
    )

    console.print(Panel(acc_table, title="Account & Risk Overview", border_style="blue"))

    # Circuit Breakers
    cb = data.get("circuit_breakers", {})
    cb_table = Table(show_header=True, header_style="bold yellow")
    cb_table.add_column("Circuit Breaker", style="cyan")
    cb_table.add_column("Status")
    cb_table.add_column("Details", style="dim")

    # 1. Daily Drawdown
    dd_tripped = cb.get("daily_drawdown_tripped", False)
    cb_table.add_row(
        "Daily Drawdown Cutoff",
        "[bold red]TRIPPED[/bold red]" if dd_tripped else "[bold green]OK[/bold green]",
        f"Threshold: {config.risk.max_daily_risk_pct}%",
    )

    # 2. Consecutive Losses
    losses = cb.get("consecutive_losses", 0)
    in_cooldown = cb.get("in_cooldown", False)
    rem_sec = cb.get("cooldown_remaining_sec", 0)
    loss_status = f"[bold red]COOLDOWN ({rem_sec}s)[/bold red]" if in_cooldown else f"[bold green]{losses}/4 losses[/bold green]"
    cb_table.add_row(
        "Consecutive Loss Cutoff",
        loss_status,
        f"Cooldown: {config.risk.consecutive_loss_cooldown_mins} mins",
    )

    # 3. Volatility Spike
    vol_kill = cb.get("volatility_kill", False)
    cb_table.add_row(
        "3-Sigma Volatility Spike",
        "[bold red]HALTED[/bold red]" if vol_kill else "[bold green]NORMAL[/bold green]",
        f"Threshold: > {config.risk.atr_spike_threshold_std} Std Dev",
    )

    console.print(Panel(cb_table, title="Circuit Breakers & Protection Failsafes", border_style="yellow"))

    # Active Positions Table
    positions = data.get("positions", [])
    pos_table = Table(title="Active Open Positions", show_header=True, header_style="bold magenta")
    pos_table.add_column("Symbol", style="bold cyan")
    pos_table.add_column("Side")
    pos_table.add_column("Size", justify="right")
    pos_table.add_column("Entry Price", justify="right")
    pos_table.add_column("Mark Price", justify="right")
    pos_table.add_column("Unrealized PnL", justify="right")
    pos_table.add_column("Leverage", justify="center")

    if positions:
        for p in positions:
            side_str = f"[bold green]{p['side'].upper()}[/bold green]" if p["side"] == "Buy" else f"[bold red]{p['side'].upper()}[/bold red]"
            pnl = p["unrealised_pnl"]
            pnl_style = "bold green" if pnl >= 0 else "bold red"
            sign = "+" if pnl >= 0 else ""
            pos_table.add_row(
                p["symbol"],
                side_str,
                str(p["size"]),
                f"${p['entry_price']:,.4f}",
                f"${p['mark_price']:,.4f}",
                f"[{pnl_style}]{sign}${pnl:.2f}[/{pnl_style}]",
                f"{p['leverage']}x",
            )
    else:
        pos_table.add_row("[dim]None[/dim]", "-", "-", "-", "-", "-", "-")

    console.print(pos_table)

    # Performance Journal Summary
    journal = TradeJournal(db_file)
    summary = journal.get_summary_metrics()

    perf_table = Table(show_header=True, header_style="bold cyan")
    perf_table.add_column("Total Trades", justify="center")
    perf_table.add_column("Win Rate", justify="center")
    perf_table.add_column("Profit Factor", justify="center")
    perf_table.add_column("Total Realized PnL", justify="center")
    perf_table.add_column("Avg Trade PnL", justify="center")

    pnl_val = summary["total_pnl"]
    pnl_col = "bold green" if pnl_val >= 0 else "bold red"
    sign = "+" if pnl_val >= 0 else ""

    perf_table.add_row(
        str(summary["total_trades"]),
        f"{summary['win_rate']:.1f}%",
        f"{summary['profit_factor']:.2f}",
        f"[{pnl_col}]{sign}${pnl_val:.2f}[/{pnl_col}]",
        f"${summary['avg_trade_pnl']:.2f}",
    )

    console.print(Panel(perf_table, title="Rolling Performance Journal", border_style="cyan"))


async def execute_panic_async() -> None:
    """Cancels all open orders and liquidates all positions via market orders."""
    console.print("[bold red]🚨 INITIATING EMERGENCY PANIC STOP 🚨[/bold red]")
    console.print("Cancelling all open orders and submitting market close orders...")

    config = load_config(BASE_DIR)
    creds = load_credentials(BASE_DIR)
    client = BybitV5Client(creds, config.trading_mode)
    await client.initialize()

    try:
        # 1. Cancel all open orders
        console.print("[yellow]• Canceling all open orders on Bybit...[/yellow]")
        cancel_res = await client.cancel_all_orders()
        console.print(f"[green]✓ Orders cancelled: {cancel_res.get('retMsg')}[/green]")

        # 2. Query and market close active positions
        console.print("[yellow]• Querying active positions for market liquidation...[/yellow]")
        positions = await client.get_positions()
        closed_count = 0

        for p in positions:
            size = float(p.get("size", 0.0))
            symbol = p.get("symbol", "")
            side = p.get("side", "")
            if size > 0:
                close_side = "Sell" if side == "Buy" else "Buy"
                console.print(f"[red]• Closing {side} {size} on {symbol} via Market order...[/red]")
                res = await client.create_order(
                    symbol=symbol,
                    side=close_side,
                    order_type="Market",
                    qty=size,
                    time_in_force="IOC",
                    reduce_only=True,
                )
                if res.get("retCode") == 0:
                    console.print(f"[green]✓ Market closed {symbol}[/green]")
                    closed_count += 1
                else:
                    console.print(f"[bold red]✕ Failed to close {symbol}: {res.get('retMsg')}[/bold red]")

        console.print(
            Panel(
                f"[bold green]PANIC STOP COMPLETE[/bold green]\n"
                f"Positions Closed: {closed_count}\n"
                f"All open orders purged.",
                border_style="red",
            )
        )
    finally:
        await client.close()


def main() -> None:
    if len(sys.argv) < 2:
        render_status()
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd == "status":
        render_status()
    elif cmd == "panic":
        asyncio.run(execute_panic_async())
    else:
        console.print(f"[red]Unknown command: {cmd}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
