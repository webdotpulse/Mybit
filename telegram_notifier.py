"""
Asynchronous Telegram Alert Dispatcher and Remote Emergency Command Handler.
Allows mobile monitoring of fills, circuit breakers, and remote execution of /panic or /status.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, Optional

import aiohttp

from config import TelegramConfig

logger = logging.getLogger("telegram_notifier")


class TelegramNotifier:
    """Dispatches trade notifications and listens for emergency remote commands."""

    def __init__(
        self,
        config: TelegramConfig,
        panic_callback: Optional[Callable[[], Coroutine[Any, Any, Dict[str, Any]]]] = None,
        status_callback: Optional[Callable[[], Coroutine[Any, Any, str]]] = None,
        report_callback: Optional[Callable[[], Coroutine[Any, Any, str]]] = None,
    ):
        self.config = config
        self.token = config.bot_token
        self.chat_id = config.chat_id
        self.enabled = bool(config.enabled and self.token and self.chat_id)
        self.panic_callback = panic_callback
        self.status_callback = status_callback
        self.report_callback = report_callback
        self.session: Optional[aiohttp.ClientSession] = None
        self._running = False
        self._last_update_id = 0
        self._poll_task: Optional[asyncio.Task] = None

    async def initialize(self) -> None:
        """Starts aiohttp session and spawns command listener if enabled."""
        if not self.enabled:
            return
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=10.0)
            )
        self._running = True
        if self.config.poll_commands:
            self._poll_task = asyncio.create_task(self._poll_commands_loop())
        logger.info("Telegram notifier initialized.")

    async def close(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    async def send_message(self, text: str, parse_mode: str = "Markdown") -> bool:
        """Sends a message to the configured Telegram chat."""
        if not self.enabled or not self.token or not self.chat_id:
            return False

        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }

        try:
            if self.session is None or self.session.closed:
                await self.initialize()
            assert self.session is not None
            async with self.session.post(url, json=payload) as resp:
                if resp.status == 200:
                    return True
                else:
                    body = await resp.text()
                    logger.warning(f"Telegram API responded with {resp.status}: {body}")
                    return False
        except Exception as e:
            logger.error(f"Failed to dispatch Telegram message: {e}")
            return False

    async def send_trade_entry(
        self,
        symbol: str,
        side: str,
        price: float,
        qty: float,
        tp: Optional[float],
        sl: Optional[float],
        regime: str,
    ) -> None:
        emoji = "🟢" if side == "Buy" else "🔴"
        tp_str = f"${tp:.4f}" if tp else "None"
        sl_str = f"${sl:.4f}" if sl else "None"
        msg = (
            f"{emoji} *TRADE OPENED: {symbol}*\n"
            f"• *Side*: {side.upper()}\n"
            f"• *Entry Price*: ${price:,.4f}\n"
            f"• *Quantity*: {qty}\n"
            f"• *Take Profit*: {tp_str}\n"
            f"• *Stop Loss*: {sl_str}\n"
            f"• *Regime*: `{regime}`"
        )
        await self.send_message(msg)

    async def send_trade_exit(
        self,
        symbol: str,
        side: str,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        holding_sec: float,
    ) -> None:
        emoji = "💰" if pnl >= 0 else "🔻"
        sign = "+" if pnl >= 0 else ""
        msg = (
            f"{emoji} *TRADE CLOSED: {symbol}*\n"
            f"• *Side*: {side.upper()}\n"
            f"• *Exit Price*: ${exit_price:,.4f}\n"
            f"• *Realized PnL*: `{sign}${pnl:.2f} ({sign}{pnl_pct:.2f}%)`\n"
            f"• *Holding Time*: {holding_sec:.1f}s"
        )
        await self.send_message(msg)

    async def send_circuit_breaker_alert(self, title: str, details: str) -> None:
        msg = (
            f"🚨 *CRITICAL RISK ALERT: {title}* 🚨\n\n"
            f"{details}\n\n"
            f"_Engine has enforced protective countermeasures._"
        )
        await self.send_message(msg)

    async def _poll_commands_loop(self) -> None:
        """Polls for incoming user commands like /status or /panic."""
        while self._running:
            try:
                url = f"https://api.telegram.org/bot{self.token}/getUpdates"
                params = {"offset": self._last_update_id + 1, "timeout": 20}
                if self.session is None or self.session.closed:
                    await self.initialize()
                assert self.session is not None
                async with self.session.get(url, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        updates = data.get("result", [])
                        for u in updates:
                            self._last_update_id = u.get("update_id", self._last_update_id)
                            msg = u.get("message", {})
                            text = msg.get("text", "").strip()
                            chat = msg.get("chat", {})
                            sender_id = str(chat.get("id", ""))

                            # Only respond to authorized chat_id
                            if sender_id != str(self.chat_id):
                                continue

                            await self._handle_command(text)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Telegram polling error: {e}")
                await asyncio.sleep(5)

    async def _handle_command(self, cmd: str) -> None:
        cmd_lower = cmd.lower()
        if cmd_lower in ("/panic", "/kill"):
            await self.send_message("⚠️ *EXECUTING PANIC LIQUIDATION...*")
            if self.panic_callback:
                res = await self.panic_callback()
                await self.send_message(f"✅ *Panic liquidation executed:* `{res}`")
        elif cmd_lower in ("/status", "/stats"):
            if self.status_callback:
                status_text = await self.status_callback()
                await self.send_message(status_text)
            else:
                await self.send_message("ℹ️ Engine running. Status callback not linked.")
        elif cmd_lower in ("/report", "/daily", "/summary"):
            if self.report_callback:
                report_text = await self.report_callback()
                await self.send_message(report_text)
            elif self.status_callback:
                status_text = await self.status_callback()
                await self.send_message(status_text)
            else:
                await self.send_message("ℹ️ Engine running. Report callback not linked.")
        elif cmd_lower in ("/help", "/start"):
            help_msg = (
                "🤖 *Bybit Autonomous Engine Remote Control*\n\n"
                "• `/status` - Live account balance, equity, and open positions\n"
                "• `/report` - 24-hour daily performance digest (PnL, win rate, trades)\n"
                "• `/panic` - Instantly cancel all orders and market close positions\n"
                "• `/help` - Show this menu"
            )
            await self.send_message(help_msg)
