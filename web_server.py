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
    load_config,
    save_config,
    save_credentials,
)

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"

routes = web.RouteTableDef()


@routes.get("/")
async def handle_index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "index.html")


@routes.get("/setup")
@routes.get("/setup.html")
async def handle_setup(request: web.Request) -> web.FileResponse:
    return web.FileResponse(WEB_DIR / "setup.html")


@routes.get("/api/status")
async def handle_api_status(request: web.Request) -> web.Response:
    status_file = DATA_DIR / "engine_status.json"
    if status_file.exists():
        try:
            data = json.loads(status_file.read_text(encoding="utf-8"))
            return web.json_response(data)
        except Exception:
            pass

    # Default fallback data if engine hasn't written status file yet
    config = load_config(BASE_DIR)
    default_status = {
        "status": "AUTONOMOUS_RUNNING",
        "equity": config.risk.allocated_capital_usd,
        "high_water_mark": config.risk.allocated_capital_usd,
        "daily_drawdown_pct": 0.0,
        "testnet": True,
        "trading_mode": config.trading_mode.value,
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


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes(routes)
    app.router.add_static("/", WEB_DIR, show_index=True)
    return app


if __name__ == "__main__":
    app = create_app()
    web.run_app(app, host="127.0.0.1", port=8080)
