"""
Asynchronous Web Dashboard Server for Bybit V5 Autonomous Engine.
Serves the dark glassmorphism Executive Dashboard & Zero-Touch Installer.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
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
from trade_journal import TradeJournal

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
        if request.path == "/api/config":
            return web.json_response(
                {"success": False, "error": "Configuration endpoint disabled in VIEW-ONLY mode."},
                status=403,
            )
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


@routes.get("/api/config")
async def handle_api_config(request: web.Request) -> web.Response:
    """Returns current non-sensitive configuration for the Setup Wizard."""
    if request.app.get("read_only", False):
        return web.json_response(
            {"success": False, "error": "Configuration access is disabled in View-Only mode."},
            status=403,
        )

    cfg = load_config(BASE_DIR)
    has_creds = False
    masked_key = ""
    is_testnet = False
    try:
        creds = load_credentials(BASE_DIR)
        has_creds = bool(creds.api_key and creds.api_secret)
        if creds.api_key:
            k = creds.api_key
            masked_key = f"{k[:4]}••••{k[-4:]}" if len(k) > 8 else "••••••••"
        is_testnet = bool(creds.testnet)
    except Exception:
        pass

    return web.json_response({
        "has_credentials": has_creds,
        "masked_key": masked_key,
        "testnet": is_testnet,
        "trading_mode": cfg.trading_mode.value,
        "capital": cfg.risk.allocated_capital_usd,
        "daily_risk_pct": cfg.risk.max_daily_risk_pct,
        "default_leverage": cfg.risk.default_leverage,
        "max_open_positions": cfg.risk.max_open_positions,
        "auto_tier_by_equity": cfg.risk.auto_tier_by_equity,
        "auto_scan_symbols": cfg.strategy.auto_scan_symbols,
        "symbols": cfg.strategy.symbols,
        "telegram_enabled": cfg.telegram.enabled,
        "telegram_chat_id": cfg.telegram.chat_id or "",
        "has_telegram_token": bool(cfg.telegram.bot_token),
    })


@routes.get("/api/status")
async def handle_api_status(request: web.Request) -> web.Response:
    read_only = bool(request.app.get("read_only", False))
    status_file = DATA_DIR / "engine_status.json"
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            data["read_only"] = read_only
            ts = int(data.get("timestamp", 0))
            age_sec = max(0, int(time.time()) - ts) if ts > 0 else 9999
            data["heartbeat_age_sec"] = age_sec
            if data.get("status") == "RUNNING" and age_sec > 30:
                data["status"] = "STALE_HEARTBEAT"
                data["engine_alive"] = False
            else:
                data["engine_alive"] = data.get("status") == "RUNNING"
            return web.json_response(data)
        except Exception:
            pass

    # Fallback data if engine hasn't written status file yet (or is offline)
    config = load_config(BASE_DIR)
    tier, tier_cfg = get_tier_for_equity(config.risk.allocated_capital_usd)
    is_testnet = False
    try:
        creds = load_credentials(BASE_DIR)
        is_testnet = bool(creds.testnet)
    except Exception:
        pass

    metrics = {
        "total_trades": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "total_pnl": 0.0,
        "avg_trade_pnl": 0.0,
    }
    window_24h = {
        "window_hours": 24,
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "total_pnl": 0.0,
    }
    recent_trades: list[dict[str, Any]] = []
    learner_weights: dict[str, Any] = {}

    db_path = DATA_DIR / "trading_journal.db"
    if db_path.exists():
        try:
            journal = TradeJournal(db_path)
            metrics = journal.get_summary_metrics()
            window_24h = journal.get_window_metrics(hours=24)
            recent_trades = journal.get_recent_trades(limit=10)
            learner_weights = journal.learner.get_weights_summary()
        except Exception:
            pass

    default_status = {
        "timestamp": int(time.time()),
        "status": "OFFLINE",
        "engine_alive": False,
        "heartbeat_age_sec": 0,
        "equity": config.risk.allocated_capital_usd,
        "capital_tier": tier.value,
        "tier_description": tier_cfg["description"],
        "auto_tier_enabled": config.risk.auto_tier_by_equity,
        "symbols": config.strategy.symbols or tier_cfg["symbols"],
        "max_open_positions": config.risk.max_open_positions,
        "high_water_mark": config.risk.allocated_capital_usd,
        "daily_drawdown_pct": 0.0,
        "last_reconciliation_ts": 0,
        "testnet": is_testnet,
        "trading_mode": config.trading_mode.value,
        "read_only": read_only,
        "circuit_breakers": {
            "daily_drawdown_tripped": False,
            "max_daily_drawdown_pct": config.risk.max_daily_risk_pct,
            "consecutive_losses": 0,
            "max_consecutive_losses": config.risk.max_consecutive_losses,
            "in_cooldown": False,
            "cooldown_remaining_sec": 0,
            "volatility_kill": False,
            "volatility_cooldown_remaining_sec": 0,
        },
        "metrics": metrics,
        "window_24h": window_24h,
        "positions": [],
        "pending_orders": 0,
        "recent_trades": recent_trades,
        "recent_events": [],
        "learner_weights": learner_weights,
        "parameters": {
            "tp_atr_mult": config.execution.bracket_tp_atr_mult,
            "sl_atr_mult": config.execution.bracket_sl_atr_mult,
            "kelly_scale": config.strategy.kelly_scale,
            "kelly_fraction_pct": round(config.risk.min_position_equity_pct, 2),
            "default_leverage": config.risk.default_leverage,
        },
        "current_regime": "STANDBY",
        "symbol_telemetry": {},
    }
    return web.json_response(default_status)


@routes.post("/api/panic")
async def handle_api_panic(request: web.Request) -> web.Response:
    if request.app.get("read_only", False):
        return web.json_response(
            {"success": False, "error": "Emergency panic stop is disabled in View-Only mode."},
            status=403,
        )

    venv_py = BASE_DIR / ".venv" / "bin" / "python3"
    py_exec = str(venv_py) if venv_py.exists() else sys.executable

    proc = await asyncio.create_subprocess_exec(
        py_exec,
        str(BASE_DIR / "manage_cli.py"),
        "panic",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return web.json_response({
        "success": proc.returncode == 0,
        "output": stdout.decode() or stderr.decode(),
    })


@routes.post("/api/setup")
async def handle_api_setup(request: web.Request) -> web.Response:
    if request.app.get("read_only", False):
        return web.json_response(
            {"success": False, "error": "Configuration modification is disabled in View-Only mode."},
            status=403,
        )

    try:
        body = await request.json()
        api_key = (body.get("api_key") or "").strip()
        api_secret = (body.get("api_secret") or "").strip()
        testnet = bool(body.get("testnet", False))
        mode = body.get("trading_mode", "linear")
        capital = float(body.get("capital", 1000.0))
        daily_risk = float(body.get("daily_risk_pct", 2.5))
        symbols = body.get("symbols") or ["BTCUSDT", "ETHUSDT", "SOLUSDT"]

        if api_key and api_secret:
            creds = BybitCredentials(api_key=api_key, api_secret=api_secret, testnet=testnet)
            save_credentials(creds, BASE_DIR)
        else:
            # Preserve existing encrypted credentials if already configured
            try:
                existing_creds = load_credentials(BASE_DIR)
                existing_creds.testnet = testnet
                save_credentials(existing_creds, BASE_DIR)
            except FileNotFoundError:
                return web.json_response(
                    {"success": False, "error": "Bybit API Key and Secret are required for initial setup."},
                    status=400,
                )

        cfg = load_config(BASE_DIR)
        cfg.trading_mode = TradingMode.LINEAR if mode == "linear" else TradingMode.SPOT
        cfg.risk.allocated_capital_usd = capital
        cfg.risk.max_daily_risk_pct = daily_risk
        if "auto_tier_by_equity" in body:
            cfg.risk.auto_tier_by_equity = bool(body["auto_tier_by_equity"])
        if "auto_scan_symbols" in body:
            cfg.strategy.auto_scan_symbols = bool(body["auto_scan_symbols"])
        cfg.strategy.symbols = [s.strip().upper() for s in symbols if s.strip()]

        if "telegram_enabled" in body:
            cfg.telegram.enabled = bool(body.get("telegram_enabled"))
            if body.get("telegram_bot_token"):
                cfg.telegram.bot_token = str(body.get("telegram_bot_token")).strip()
            if "telegram_chat_id" in body:
                cfg.telegram.chat_id = str(body.get("telegram_chat_id") or "").strip() or None

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

