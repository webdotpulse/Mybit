#!/usr/bin/env python3
"""
Interactive Zero-Touch CLI Installer for Bybit V5 Autonomous Engine.
Eliminates manual file editing, validates parameters, encrypts credentials (0600),
and provisions the systemd daemon.
"""

from __future__ import annotations

import argparse
import getpass
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt
from rich.table import Table

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from config import (  # noqa: E402
    AppConfig,
    BybitCredentials,
    TradingMode,
    save_config,
    save_credentials,
)

console = Console()


def print_banner() -> None:
    banner_text = (
        "[bold cyan]BYBIT V5 AUTONOMOUS QUANTITATIVE TRADING ENGINE[/bold cyan]\n"
        "[dim]Unified Trading Account (UTA) • Micro-Scalping • Adaptive Regime Engine[/dim]\n\n"
        "[green]Zero-Touch Deployment Wizard[/green] — All secrets are encrypted in-place."
    )
    console.print(Panel(banner_text, border_style="cyan", padding=(1, 2)))


def check_environment() -> None:
    """Verifies Python version and virtual environment."""
    if sys.version_info < (3, 11):
        console.print(
            f"[bold red]Error: Python 3.11+ is required. Found Python {sys.version_info.major}.{sys.version_info.minor}[/bold red]"
        )
        sys.exit(1)


def prompt_credentials() -> BybitCredentials:
    """Securely gathers and validates Bybit V5 credentials."""
    console.print("\n[bold yellow]Step 1: Bybit API Credentials[/bold yellow]")
    console.print(
        "[dim]Ensure your Bybit API key has 'Unified Trading' or 'Contract' read-write permissions.[/dim]\n"
    )

    while True:
        api_key = Prompt.ask("[cyan]Enter Bybit V5 API Key[/cyan]", password=True).strip()
        if len(api_key) >= 10:
            break
        console.print("[red]API Key too short. Please re-enter.[/red]")

    while True:
        api_secret = Prompt.ask("[cyan]Enter Bybit V5 API Secret[/cyan]", password=True).strip()
        if len(api_secret) >= 15:
            break
        console.print("[red]API Secret too short. Please re-enter.[/red]")

    testnet = Confirm.ask(
        "[cyan]Use Bybit Testnet? (Recommended for initial verification)[/cyan]",
        default=True,
    )

    return BybitCredentials(api_key=api_key, api_secret=api_secret, testnet=testnet)


def prompt_strategy_parameters() -> AppConfig:
    """Gathers risk parameters and target symbols."""
    console.print("\n[bold yellow]Step 2: Trading Mode & Capital Allocation[/bold yellow]")

    mode_choice = Prompt.ask(
        "[cyan]Select Trading Mode[/cyan]",
        choices=["linear", "spot"],
        default="linear",
    )
    trading_mode = TradingMode.LINEAR if mode_choice == "linear" else TradingMode.SPOT

    capital = FloatPrompt.ask(
        "[cyan]Allocated Capital Pool (USD)[/cyan]",
        default=1000.0,
    )

    daily_risk = FloatPrompt.ask(
        "[cyan]Max Daily Risk Drawdown % (Circuit Breaker)[/cyan]",
        default=2.5,
    )

    default_pairs = "BTCUSDT, ETHUSDT, SOLUSDT"
    symbols_raw = Prompt.ask(
        "[cyan]Target Trading Pairs (comma-separated)[/cyan]",
        default=default_pairs,
    )
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]

    # Telegram alerts
    console.print("\n[bold yellow]Step 3: Mobile Alerts & Remote Emergency Shutoff[/bold yellow]")
    enable_tg = Confirm.ask("[cyan]Enable Telegram Bot alerts & emergency commands?[/cyan]", default=False)
    tg_token = None
    tg_chat_id = None
    if enable_tg:
        tg_token = Prompt.ask("[cyan]Telegram Bot Token[/cyan]", password=True).strip()
        tg_chat_id = Prompt.ask("[cyan]Telegram Authorized Chat ID[/cyan]").strip()

    cfg = AppConfig(
        trading_mode=trading_mode,
    )
    cfg.risk.allocated_capital_usd = capital
    cfg.risk.max_daily_risk_pct = daily_risk
    cfg.strategy.symbols = symbols
    cfg.telegram.enabled = enable_tg
    cfg.telegram.bot_token = tg_token
    cfg.telegram.chat_id = tg_chat_id

    return cfg


def display_summary(creds: BybitCredentials, cfg: AppConfig) -> None:
    """Presents configuration overview before saving."""
    table = Table(title="Engine Configuration Summary", show_header=True, header_style="bold magenta")
    table.add_column("Parameter", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Bybit Network", "TESTNET" if creds.testnet else "LIVE MAINNET")
    table.add_row("Trading Mode", cfg.trading_mode.value.upper())
    table.add_row("Allocated Capital", f"${cfg.risk.allocated_capital_usd:,.2f}")
    table.add_row("Max Daily Drawdown Cutoff", f"{cfg.risk.max_daily_risk_pct}%")
    table.add_row("Consecutive Loss Limit", f"{cfg.risk.max_consecutive_losses} trades (30m cooldown)")
    table.add_row("Kelly Sizing Range", f"{cfg.risk.min_position_equity_pct}% - {cfg.risk.max_position_equity_pct}%")
    table.add_row("Trading Pairs", ", ".join(cfg.strategy.symbols))
    table.add_row("Telegram Integration", "ENABLED" if cfg.telegram.enabled else "DISABLED")

    console.print(table)


def setup_systemd(base_dir: Path) -> None:
    """Generates systemd service file for 24/7 daemonization."""
    console.print("\n[bold yellow]Step 4: Systemd Service Daemonization[/bold yellow]")

    venv_python = base_dir / ".venv" / "bin" / "python3"
    if not venv_python.exists():
        venv_python = Path(sys.executable)

    user = os.environ.get("USER", "root")

    service_content = f"""[Unit]
Description=Bybit V5 Autonomous Quantitative Trading Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
WorkingDirectory={base_dir}
ExecStart={venv_python} {base_dir}/main.py
Restart=on-failure
RestartSec=5s
KillMode=process
LimitNOFILE=65535
StandardOutput=journal
StandardError=journal
Environment="PYTHONUNBUFFERED=1"

[Install]
WantedBy=multi-user.target
"""
    # Write to local file first
    local_service = base_dir / "bybit-engine.service"
    local_service.write_text(service_content)
    console.print(f"[green]✓ Generated local service unit: {local_service}[/green]")

    is_root = os.geteuid() == 0
    can_sudo = False
    try:
        res = subprocess.run(["sudo", "-n", "true"], capture_output=True)
        can_sudo = res.returncode == 0
    except Exception:
        can_sudo = False

    if is_root:
        target_path = Path("/etc/systemd/system/bybit-engine.service")
        target_path.write_text(service_content)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        console.print("[green]✓ Installed service to /etc/systemd/system/bybit-engine.service[/green]")
    elif can_sudo:
        subprocess.run(["sudo", "cp", str(local_service), "/etc/systemd/system/bybit-engine.service"], check=False)
        subprocess.run(["sudo", "systemctl", "daemon-reload"], check=False)
        console.print("[green]✓ Installed service to /etc/systemd/system/bybit-engine.service (via sudo)[/green]")
    else:
        # Install as user systemd service
        user_systemd_dir = Path.home() / ".config" / "systemd" / "user"
        user_systemd_dir.mkdir(parents=True, exist_ok=True)
        user_service = user_systemd_dir / "bybit-engine.service"
        # User unit does not need User= parameter
        user_service_content = service_content.replace(f"User={user}\n", "")
        user_service.write_text(user_service_content)
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        console.print(f"[green]✓ Installed user service: {user_service}[/green]")


def main() -> None:
    parser = argparse.ArgumentParser(description="Bybit V5 Autonomous Engine Setup Wizard")
    parser.add_argument("--non-interactive", action="store_true", help="Non-interactive setup using arguments")
    parser.add_argument("--api-key", type=str, help="Bybit API Key")
    parser.add_argument("--api-secret", type=str, help="Bybit API Secret")
    parser.add_argument("--testnet", action="store_true", default=True, help="Use testnet")
    parser.add_argument("--mode", type=str, default="linear", choices=["linear", "spot"])
    parser.add_argument("--capital", type=float, default=1000.0)
    args = parser.parse_args()

    check_environment()
    print_banner()

    if args.non_interactive:
        if not args.api_key or not args.api_secret:
            console.print("[bold red]--api-key and --api-secret are required in non-interactive mode.[/bold red]")
            sys.exit(1)
        creds = BybitCredentials(api_key=args.api_key, api_secret=args.api_secret, testnet=args.testnet)
        cfg = AppConfig(trading_mode=TradingMode.LINEAR if args.mode == "linear" else TradingMode.SPOT)
        cfg.risk.allocated_capital_usd = args.capital
    else:
        creds = prompt_credentials()
        cfg = prompt_strategy_parameters()
        display_summary(creds, cfg)

        if not Confirm.ask("\n[bold green]Deploy this configuration?[/bold green]", default=True):
            console.print("[yellow]Setup aborted by user.[/yellow]")
            sys.exit(0)

    # 1. Save and encrypt credentials
    save_credentials(creds, BASE_DIR)
    console.print("\n[bold green]✓ Bybit API credentials securely encrypted with 0600 permissions.[/bold green]")

    # 2. Save AppConfig
    save_config(cfg, BASE_DIR)
    console.print("[bold green]✓ Application configuration written to config.json.[/bold green]")

    # 3. Setup systemd
    setup_systemd(BASE_DIR)

    console.print(
        Panel(
            "[bold green]Setup successfully completed![/bold green]\n\n"
            "• Use [cyan]./manage.sh status[/cyan] to inspect live metrics\n"
            "• Use [cyan]./manage.sh start[/cyan] or [cyan]systemctl start bybit-engine[/cyan] to launch\n"
            "• Use [cyan]./manage.sh logs[/cyan] to tail live engine logs\n"
            "• Use [cyan]./manage.sh panic[/cyan] for emergency stop",
            border_style="green",
        )
    )


if __name__ == "__main__":
    main()
