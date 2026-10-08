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
from config import load_config, load_credentials, save_credentials  # noqa: E402
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


async def verify_api_connectivity_async() -> None:
    """Performs deep diagnostic check of Bybit credentials, IP whitelist, and endpoints."""
    console.print(
        Panel(
            "[bold cyan]BYBIT V5 API DIAGNOSTIC & CONNECTIVITY CHECK[/bold cyan]\n"
            "[dim]Validates encrypted secrets, outbound IP whitelist, REST latency, and account balances.[/dim]",
            border_style="cyan",
            padding=(0, 2),
        )
    )

    # 1. Credentials
    try:
        config = load_config(BASE_DIR)
        creds = load_credentials(BASE_DIR)
        masked_key = creds.api_key[:4] + "..." + creds.api_key[-4:] if len(creds.api_key) > 8 else "***"
        console.print(f"[green]✓ Encrypted credentials loaded successfully.[/green]")
        console.print(f"  • API Key: [bold white]{masked_key}[/bold white]")
        console.print(f"  • Environment: [magenta]{'TESTNET' if creds.testnet else 'LIVE MAINNET'}[/magenta]")
        console.print(f"  • Mode: [cyan]{config.trading_mode.value.upper()}[/cyan]")
    except Exception as e:
        console.print(f"[bold red]✕ Failed to load credentials: {e}[/bold red]")
        return

    # 2. Public IP detection
    client = BybitV5Client(creds, config.trading_mode)
    await client.initialize()

    public_ip = "Unknown"
    try:
        if client.session:
            async with client.session.get("https://api.ipify.org?format=json", timeout=3.0) as resp:
                if resp.status == 200:
                    ip_data = await resp.json()
                    public_ip = ip_data.get("ip", "Unknown")
    except Exception:
        pass

    console.print(f"  • Server Outbound IP: [bold yellow]{public_ip}[/bold yellow]")

    try:
        # 3. Public REST Connectivity
        console.print("\n[yellow]Testing public Bybit V5 endpoint connectivity...[/yellow]")
        t0 = time.perf_counter()
        time_res = await client.request("GET", "/v5/market/time", auth_required=False)
        lat = (time.perf_counter() - t0) * 1000
        if time_res.get("retCode") == 0:
            console.print(f"[green]✓ Public REST endpoint reachable (Latency: {lat:.1f}ms)[/green]")
        else:
            console.print(f"[red]✕ Public REST check failed: {time_res.get('retMsg')}[/red]")

        # 4. Authenticated Wallet Query
        console.print("[yellow]Testing authenticated Bybit V5 wallet endpoint...[/yellow]")
        wallet_res = await client.get_wallet_balance(account_type="UNIFIED")
        ret_code = wallet_res.get("retCode")
        ret_msg = wallet_res.get("retMsg", "")

        if ret_code == 0:
            result_obj = wallet_res.get("result") or {}
            item_list = result_obj.get("list") or []
            item0 = item_list[0] if item_list else {}
            acc_type = item0.get("accountType", "UNIFIED")
            eq = item0.get("totalEquity") or ""
            console.print(f"[bold green]✓ Authenticated API connection successful![/bold green]")
            console.print(f"  • Account Type: [cyan]{acc_type}[/cyan]")
            console.print(f"  • Live Equity: [bold white]${float(eq):,.2f}[/bold white]" if eq != "" else "  • Equity: N/A")
            console.print("\n[bold green]STATUS: READY FOR AUTONOMOUS LIVE TRADING[/bold green]")
        else:
            console.print(f"[bold red]✕ Authenticated request failed with retCode {ret_code}: {ret_msg}[/bold red]")
            if ret_code in (10003, 401) or "10003" in str(ret_msg):
                alt_testnet = not creds.testnet
                alt_env_name = "TESTNET" if alt_testnet else "LIVE MAINNET"
                alt_creds = creds.model_copy(update={"testnet": alt_testnet})
                alt_client = BybitV5Client(alt_creds, config.trading_mode)
                await alt_client.initialize()
                alt_res = await alt_client.get_wallet_balance(account_type="UNIFIED")
                await alt_client.close()
                alt_code = alt_res.get("retCode")
                if alt_code not in (10003, 401):
                    # The other environment recognized the key!
                    console.print(
                        Panel(
                            f"[bold red]⚡ ENVIRONMENT MISMATCH DETECTED![/bold red]\n\n"
                            f"Your API key was rejected by [bold magenta]{'TESTNET' if creds.testnet else 'LIVE MAINNET'}[/bold magenta] (retCode 10003: API key is invalid),\n"
                            f"but it was recognized by [bold green]{alt_env_name}[/bold green]!\n\n"
                            f"To switch your configuration to {alt_env_name}, run:\n"
                            f"  [bold cyan]./manage.sh {'testnet' if alt_testnet else 'mainnet'}[/bold cyan]\n"
                            f"  [bold cyan]./manage.sh restart[/bold cyan]",
                            border_style="yellow",
                        )
                    )
                else:
                    console.print(
                        Panel(
                            "[bold red]ACTION REQUIRED: API KEY INVALID / RECHECK CREDENTIALS[/bold red]\n\n"
                            "Bybit returned 'API key is invalid'. Common causes:\n"
                            "1. Typo in API Key or API Secret when running setup.\n"
                            "2. The API key was deleted or regenerated in Bybit API Management.\n"
                            "3. You can update your credentials anytime by running:\n"
                            "   [cyan]./manage.sh web[/cyan] (and opening http://localhost:8080/setup.html)\n"
                            "   or [cyan]python3 installer.py[/cyan]",
                            border_style="red",
                        )
                    )
            elif ret_code == 10010:
                console.print(
                    Panel(
                        f"[bold red]ACTION REQUIRED: IP WHITELIST MISMATCH[/bold red]\n\n"
                        f"Your Bybit API key is restricted by IP address, but this server's IP is not in the whitelist.\n\n"
                        f"1. Open your Bybit API Key Management page: https://www.bybit.com/user/api-management\n"
                        f"2. Edit your API key settings.\n"
                        f"3. Add your server's public IP: [bold yellow]{public_ip}[/bold yellow]\n"
                        f"4. Save changes and re-run [cyan]./manage.sh verify[/cyan].",
                        border_style="red",
                    )
                )
            elif ret_code in (10004, 10005, 33004):
                console.print(
                    Panel(
                        "[bold red]ACTION REQUIRED: API KEY PERMISSION ISSUE[/bold red]\n\n"
                        "Your API key does not have permission to access wallet/account or trading endpoints.\n\n"
                        "1. Open Bybit API Management.\n"
                        "2. Ensure permissions include: [bold green]Unified Trading[/bold green] (or Contract), and [bold green]Account / Assets[/bold green] (Read-Only or Read-Write).\n"
                        "3. Save changes and re-run [cyan]./manage.sh verify[/cyan].",
                        border_style="red",
                    )
                )
            elif ret_code == 403 or "403" in str(ret_msg):
                console.print(
                    Panel(
                        "[bold red]ACTION REQUIRED: HTTP 403 FORBIDDEN / CLOUDFRONT BLOCK[/bold red]\n\n"
                        "Bybit or Amazon CloudFront CDN is blocking requests from this server's hosting provider or region.\n\n"
                        "• Ensure this VPS is not hosted in a restricted jurisdiction (e.g. US).\n"
                        "• If your hosting provider's IP range is blocked by Bybit, consider using a proxy or server in an allowed jurisdiction (e.g. Germany, Singapore, Tokyo, UK).",
                        border_style="red",
                    )
                )
    finally:
        await client.close()


def switch_environment(testnet: bool) -> None:
    """Toggles environment between Testnet and Live Mainnet without re-entering keys."""
    try:
        creds = load_credentials(BASE_DIR)
        updated = creds.model_copy(update={"testnet": testnet})
        save_credentials(updated, BASE_DIR)
        env_label = "TESTNET (api-testnet.bybit.com)" if testnet else "LIVE MAINNET (api.bybit.com)"
        console.print(
            Panel(
                f"[bold green]✓ Configuration updated to {env_label}[/bold green]\n\n"
                f"Next step: apply changes to the trading daemon by running:\n"
                f"  [cyan]./manage.sh restart[/cyan]",
                border_style="green",
            )
        )
    except Exception as e:
        console.print(f"[bold red]✕ Failed to switch environment: {e}[/bold red]")


def main() -> None:
    if len(sys.argv) < 2:
        render_status()
        sys.exit(0)

    cmd = sys.argv[1].lower()
    if cmd == "status":
        render_status()
    elif cmd in ("verify", "test", "check"):
        asyncio.run(verify_api_connectivity_async())
    elif cmd in ("mainnet", "live", "prod"):
        switch_environment(testnet=False)
    elif cmd in ("testnet", "demo"):
        switch_environment(testnet=True)
    elif cmd == "panic":
        asyncio.run(execute_panic_async())
    else:
        console.print(f"[red]Unknown command: {cmd}[/red]")
        sys.exit(1)


if __name__ == "__main__":
    main()
