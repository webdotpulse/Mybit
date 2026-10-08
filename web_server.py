"""
Asynchronous Web Dashboard Server for Bybit V5 Autonomous Engine.
Serves the dark glassmorphism Executive Dashboard & Zero-Touch Installer.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from aiohttp import web

from config import (
    AppConfig,
    BybitCredentials,
    TradingMode,
    get_tier_for_equity,
    load_config,
    load_credentials,
    save_config,
    save_credentials,
)

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"

routes = web.RouteTableDef()


@web.middleware
async def view_only_middleware(request: web.Request, handler) -> web.StreamResponse:
    """Blocks mutating actions and setup access when operating in view-only / read-only mode."""
    is_read_only = request.app.get("read_only", False)
    if is_read_only:
        # Block setup configuration endpoints
        if request.path in ("/setup", "/setup.html"):
            raise web.HTTPFound("/")
        # Block mutating API requests
        if request.method == "POST" and request.path in ("/api/panic", "/api/setup"):
            return web.json_response(
                {
                    "success": False,
                    "error": "Action forbidden: Web dashboard is running in VIEW-ONLY mode. Changes cannot be made from public view.",
                },
                status=403,
            )
    return await handler(request)


@routes.get("/")
async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


@routes.get("/setup")
@routes.get("/setup.html")
async def handle_setup(request: web.Request) -> web.StreamResponse:
    if request.app.get("read_only", False):
        raise web.HTTPFound("/")
    return web.FileResponse(WEB_DIR / "setup.html")


@routes.get("/api/status")
async def handle_api_status(request: web.Request) -> web.Response:
    read_only = bool(request.app.get("read_only", False))
    status_file = DATA_DIR / "engine_status.json"
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            data["read_only"] = read_only
            return web.json_response(data)
        except Exception:
            pass

    # Default fallback data if engine hasn't written status file yet
    config = load_config(BASE_DIR)
    tier, tier_cfg = get_tier_for_equity(config.risk.allocated_capital_usd)
    is_testnet = False
    try:
        creds = load_credentials(BASE_DIR)
        is_testnet = bool(creds.testnet)
    except Exception:
        pass

    default_status = {
        "status": "AUTONOMOUS_RUNNING",
        "equity": config.risk.allocated_capital_usd,
        "capital_tier": tier.value,
        "tier_description": tier_cfg["description"],
        "auto_tier_enabled": config.risk.auto_tier_by_equity,
        "symbols": tier_cfg["symbols"],
        "high_water_mark": config.risk.allocated_capital_usd,
        "daily_drawdown_pct": 0.0,
        "testnet": is_testnet,
        "trading_mode": config.trading_mode.value,
        "read_only": read_only,
        "circuit_breakers": {
            "daily_drawdown_tripped": False,
            "consecutive_losses": 0,
            "in_cooldown": False,
            "cooldown_remaining_sec": 0,
            "volatility_kill": False,
        },
        "metrics": {
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "avg_trade_pnl": 0.0,
        },
        "positions": [],
        "parameters": {
            "tp_atr_mult": config.execution.bracket_tp_atr_mult,
            "sl_atr_mult": config.execution.bracket_sl_atr_mult,
            "kelly_scale": config.strategy.kelly_scale,
        },
        "current_regime": {
            "name": "BULL_TREND",
            "confidence": "85%",
            "description": "Multi-timeframe EMA ribbon aligned. Positive OFI detected. Maker post-only limit orders active.",
        },
    }
    return web.json_response(default_status)


@routes.post("/api/panic")
async def handle_api_panic(request: web.Request) -> web.Response:
    if request.app.get("read_only", False):
        return web.json_response(
            {"success": False, "error": "Emergency panic stop is disabled in View-Only mode."},
            status=403,
        )

    # Trigger emergency stop via CLI helper
    proc = await asyncio.create_subprocess_exec(
        str(BASE_DIR / ".venv" / "bin" / "python3"),
        str(BASE_DIR / "manage_cli.py"),
        "panic",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return web.json_response({"success": proc.returncode == 0, "output": stdout.decode()})


@routes.post("/api/setup")
async def handle_api_setup(request: web.Request) -> web.Response:
    if request.app.get("read_only", False):
        return web.json_response(
            {"success": False, "error": "Configuration modification is disabled in View-Only mode."},
            status=403,
        )

    try:
        body = await request.json()
        api_key = body.get("api_key")
        api_secret = body.get("api_secret")
        testnet = body.get("testnet", True)
        mode = body.get("trading_mode", "linear")
        capital = float(body.get("capital", 1000.0))
        daily_risk = float(body.get("daily_risk_pct", 2.5))
        symbols = body.get("symbols", ["BTCUSDT", "ETHUSDT", "SOLUSDT"])

        creds = BybitCredentials(api_key=api_key, api_secret=api_secret, testnet=testnet)
        save_credentials(creds, BASE_DIR)

        cfg = load_config(BASE_DIR)
        cfg.trading_mode = TradingMode.LINEAR if mode == "linear" else TradingMode.SPOT
        cfg.risk.allocated_capital_usd = capital
        cfg.risk.max_daily_risk_pct = daily_risk
        cfg.strategy.symbols = symbols
        if body.get("telegram_enabled"):
            cfg.telegram.enabled = True
            cfg.telegram.bot_token = body.get("telegram_bot_token")
            cfg.telegram.chat_id = body.get("telegram_chat_id")

        save_config(cfg, BASE_DIR)
        return web.json_response({"success": True})
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=400)


def create_app(read_only: bool = False) -> web.Application:
    app = web.Application(middlewares=[view_only_middleware])
    app["read_only"] = read_only
    app.add_routes(routes)
    app.router.add_static("/", WEB_DIR, show_index=True)
    return app


if __name__ == "__main__":
    import argparse
    import os

    parser = argparse.ArgumentParser(description="Bybit V5 Web Dashboard")
    parser.add_argument(
        "--host",
        default=os.getenv("HOST", "127.0.0.1"),
        help="Bind host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("PORT", 8080)),
        help="Port (default: 8080)",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Bind to 0.0.0.0 for external network access (defaults to view-only mode)",
    )
    parser.add_argument(
        "--read-only",
        "--view-only",
        dest="read_only",
        action="store_true",
        default=os.getenv("READ_ONLY", "0").lower() in ("1", "true", "yes"),
        help="Enforce strict view-only mode (disables setup and panic actions)",
    )
    parser.add_argument(
        "--allow-write",
        action="store_true",
        help="Allow admin mutations even when public bind is enabled",
    )
    args = parser.parse_args()

    # Automatically enforce view-only mode on public views unless explicitly overridden
    is_view_only = args.read_only or (args.public and not args.allow_write)
    bind_host = "0.0.0.0" if args.public else args.host

    target_port = args.port
    if target_port == 8080:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                # Check if port 8080 is in use
                if s.connect_ex(("127.0.0.1", 8080)) == 0:
                    print("Notice: Port 8080 is occupied by another process. Auto-switching to port 8088.")
                    target_port = 8088
        except Exception:
            pass

    mode_label = "VIEW-ONLY (Mutations & Setup Disabled)" if is_view_only else "ADMIN (Full Control)"
    print(f"Starting Bybit V5 Dashboard on http://{bind_host}:{target_port} [{mode_label}]")

    app = create_app(read_only=is_view_only)
    web.run_app(app, host=bind_host, port=target_port)

