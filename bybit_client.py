"""
High-Performance Asynchronous Bybit V5 Client (Unified Trading Account).
Implements ultra-low latency REST V5 & WebSocket streaming with connection pooling,
orderbook L2 maintenance, automatic reconnection, and native bracket execution.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set, Tuple

import aiohttp
import websockets
from websockets.exceptions import ConnectionClosed

from config import BybitCredentials, TradingMode

logger = logging.getLogger("bybit_client")


class OrderBookL2:
    """
    In-memory Level-2 OrderBook cache maintaining bids, asks,
    and computing real-time Order Flow Imbalance (OFI).
    """

    def __init__(self, symbol: str, depth_levels: int = 20):
        self.symbol = symbol
        self.depth_levels = depth_levels
        # Dict of price (float) -> size (float)
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.last_update_id: int = 0
        self.prev_best_bid_p: float = 0.0
        self.prev_best_bid_v: float = 0.0
        self.prev_best_ask_p: float = 0.0
        self.prev_best_ask_v: float = 0.0
        self.ofi: float = 0.0  # Order flow imbalance

    def apply_snapshot(self, data: Dict[str, Any]) -> None:
        """Initializes order book from snapshot."""
        self.bids = {float(p): float(s) for p, s in data.get("b", [])}
        self.asks = {float(p): float(s) for p, s in data.get("a", [])}
        self.last_update_id = data.get("u", 0)
        self._update_top_of_book()

    def apply_delta(self, data: Dict[str, Any]) -> None:
        """Applies incremental depth updates and computes OFI."""
        for p_str, s_str in data.get("b", []):
            p, s = float(p_str), float(s_str)
            if s == 0:
                self.bids.pop(p, None)
            else:
                self.bids[p] = s

        for p_str, s_str in data.get("a", []):
            p, s = float(p_str), float(s_str)
            if s == 0:
                self.asks.pop(p, None)
            else:
                self.asks[p] = s

        self.last_update_id = data.get("u", self.last_update_id + 1)
        self._update_top_of_book()

    def _update_top_of_book(self) -> None:
        """Calculates top of book and incremental Order Flow Imbalance (OFI)."""
        if not self.bids or not self.asks:
            return

        sorted_bids = sorted(self.bids.keys(), reverse=True)
        sorted_asks = sorted(self.asks.keys())

        best_bid_p = sorted_bids[0]
        best_bid_v = self.bids[best_bid_p]
        best_ask_p = sorted_asks[0]
        best_ask_v = self.asks[best_ask_p]

        # Calculate Level-1 OFI (Cont, Kukanov & Stoikov formulation)
        if self.prev_best_bid_p > 0 and self.prev_best_ask_p > 0:
            # Bid side delta
            if best_bid_p > self.prev_best_bid_p:
                delta_bid = best_bid_v
            elif best_bid_p == self.prev_best_bid_p:
                delta_bid = best_bid_v - self.prev_best_bid_v
            else:
                delta_bid = -self.prev_best_bid_v

            # Ask side delta
            if best_ask_p < self.prev_best_ask_p:
                delta_ask = best_ask_v
            elif best_ask_p == self.prev_best_ask_p:
                delta_ask = best_ask_v - self.prev_best_ask_v
            else:
                delta_ask = -self.prev_best_ask_v

            # Imbalance = delta_bid - delta_ask
            self.ofi = delta_bid - delta_ask

        self.prev_best_bid_p = best_bid_p
        self.prev_best_bid_v = best_bid_v
        self.prev_best_ask_p = best_ask_p
        self.prev_best_ask_v = best_ask_v

    @property
    def best_bid(self) -> Tuple[float, float]:
        if not self.bids:
            return (0.0, 0.0)
        p = max(self.bids.keys())
        return (p, self.bids[p])

    @property
    def best_ask(self) -> Tuple[float, float]:
        if not self.asks:
            return (0.0, 0.0)
        p = min(self.asks.keys())
        return (p, self.asks[p])

    @property
    def mid_price(self) -> float:
        bb, _ = self.best_bid
        ba, _ = self.best_ask
        if bb > 0 and ba > 0:
            return (bb + ba) / 2.0
        return bb or ba

    @property
    def spread(self) -> float:
        bb, _ = self.best_bid
        ba, _ = self.best_ask
        return max(0.0, ba - bb) if bb > 0 and ba > 0 else 0.0

    def get_cumulative_volume(self, levels: int = 5) -> Tuple[float, float]:
        """Returns (bid_volume, ask_volume) up to N levels."""
        sorted_bids = sorted(self.bids.keys(), reverse=True)[:levels]
        sorted_asks = sorted(self.asks.keys())[:levels]
        bid_vol = sum(self.bids[p] for p in sorted_bids)
        ask_vol = sum(self.asks[p] for p in sorted_asks)
        return (bid_vol, ask_vol)


class BybitV5Client:
    """
    Unified Bybit V5 Client handling REST endpoints, connection pooling,
    Private & Public WebSockets, signature generation, and state tracking.
    """

    def __init__(
        self,
        credentials: BybitCredentials,
        trading_mode: TradingMode = TradingMode.LINEAR,
        recv_window: int = 5000,
    ):
        self.creds = credentials
        self.trading_mode = trading_mode
        self.recv_window = recv_window
        self.category = trading_mode.value  # "linear" or "spot"

        # URLs
        if self.creds.testnet:
            self.rest_url = "https://api-testnet.bybit.com"
            self.ws_public_url = (
                "wss://stream-testnet.bybit.com/v5/public/linear"
                if trading_mode == TradingMode.LINEAR
                else "wss://stream-testnet.bybit.com/v5/public/spot"
            )
            self.ws_private_url = "wss://stream-testnet.bybit.com/v5/private"
        else:
            self.rest_url = "https://api.bybit.com"
            self.ws_public_url = (
                "wss://stream.bybit.com/v5/public/linear"
                if trading_mode == TradingMode.LINEAR
                else "wss://stream.bybit.com/v5/public/spot"
            )
            self.ws_private_url = "wss://stream.bybit.com/v5/private"

        self.session: Optional[aiohttp.ClientSession] = None
        self.orderbooks: Dict[str, OrderBookL2] = {}

        # WebSocket tasks and state
        self._public_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._private_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._public_topics: Set[str] = set()
        self._ws_tasks: List[asyncio.Task] = []

        # Event callbacks
        self.kline_callbacks: List[Callable[[str, str, Dict[str, Any]], Coroutine]] = []
        self.execution_callbacks: List[Callable[[Dict[str, Any]], Coroutine]] = []
        self.order_callbacks: List[Callable[[Dict[str, Any]], Coroutine]] = []
        self.position_callbacks: List[Callable[[Dict[str, Any]], Coroutine]] = []
        self.wallet_callbacks: List[Callable[[Dict[str, Any]], Coroutine]] = []

    async def initialize(self) -> None:
        """Initializes aiohttp connection pool with TCP tuning."""
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                keepalive_timeout=60.0,
                ssl=True,
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(total=10.0, connect=3.0)
            self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        self._running = True

    async def close(self) -> None:
        """Gracefully closes all WebSocket connections and REST session."""
        self._running = False
        for task in self._ws_tasks:
            task.cancel()
        if self._public_ws and not self._public_ws.closed:
            await self._public_ws.close()
        if self._private_ws and not self._private_ws.closed:
            await self._private_ws.close()
        if self.session and not self.session.closed:
            await self.session.close()
        logger.info("Bybit V5 Client closed cleanly.")

    # =========================================================================
    # REST AUTHENTICATION & REQUEST ENGINE
    # =========================================================================

    def _generate_signature(self, timestamp: str, payload_str: str) -> str:
        """
        Bybit V5 HMAC SHA256 Signature.
        Formula: HMAC_SHA256(timestamp + api_key + recv_window + payload, secret)
        """
        param_str = f"{timestamp}{self.creds.api_key}{self.recv_window}{payload_str}"
        return hmac.new(
            self.creds.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        data: Optional[Dict[str, Any]] = None,
        auth_required: bool = True,
    ) -> Dict[str, Any]:
        """
        High-performance REST dispatcher for Bybit V5 with signature calculation,
        latency logging, and rate-limit backoff.
        """
        if self.session is None or self.session.closed:
            await self.initialize()

        url = f"{self.rest_url}{path}"
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Bybit-V5-Engine/1.0.0 (Linux; x86_64)",
            "Accept": "application/json",
        }
        timestamp = str(int(time.time() * 1000))

        payload_str = ""
        body_json_str = None

        if method.upper() == "GET":
            if params:
                query_pairs = []
                for k in sorted(params.keys()):
                    v = params[k]
                    if v is not None:
                        query_pairs.append(f"{k}={v}")
                payload_str = "&".join(query_pairs)
                if payload_str:
                    url = f"{url}?{payload_str}"
        elif method.upper() == "POST":
            if data:
                body_json_str = json.dumps(data)
                payload_str = body_json_str

        if auth_required:
            signature = self._generate_signature(timestamp, payload_str)
            headers.update(
                {
                    "X-BAPI-API-KEY": self.creds.api_key,
                    "X-BAPI-TIMESTAMP": timestamp,
                    "X-BAPI-RECV-WINDOW": str(self.recv_window),
                    "X-BAPI-SIGN": signature,
                }
            )

        start_t = time.perf_counter()
        try:
            async with self.session.request(
                method, url, headers=headers, data=body_json_str
            ) as response:
                latency_ms = (time.perf_counter() - start_t) * 1000
                status_code = response.status
                raw_text = await response.text()

                res_json: Optional[Dict[str, Any]] = None
                if raw_text and raw_text.strip():
                    try:
                        parsed = json.loads(raw_text)
                        if isinstance(parsed, dict):
                            res_json = parsed
                    except Exception:
                        pass

                # If response could not be parsed as a JSON dictionary
                if res_json is None:
                    error_msg = (
                        f"Bybit API HTTP {status_code} on {path} returned non-JSON or empty response. "
                        f"Body: {raw_text[:300] if raw_text else '(empty)'}"
                    )
                    if status_code == 403:
                        logger.error(
                            f"{error_msg} | CloudFront/WAF 403 Forbidden. Possible causes: "
                            "datacenter/cloud provider IP blocked by Bybit, US/restricted region IP, "
                            "or API key lacks required permissions."
                        )
                    elif status_code == 401:
                        logger.error(
                            f"{error_msg} | 401 Unauthorized. Possible causes: "
                            "invalid API key/secret, or system clock out of sync."
                        )
                    else:
                        logger.error(error_msg)

                    return {
                        "retCode": status_code if status_code != 200 else -1,
                        "retMsg": f"HTTP {status_code}: {raw_text[:200] if raw_text else 'Empty response'}",
                        "result": {},
                    }

                # Safe retrieval of retCode and retMsg
                ret_code = res_json.get("retCode")
                if ret_code is None:
                    ret_code = status_code if status_code != 200 else 0
                    res_json["retCode"] = ret_code
                ret_msg = res_json.get("retMsg", f"HTTP {status_code}")

                if ret_code != 0:
                    # 10006 = rate limit hit
                    if ret_code == 10006:
                        logger.warning(
                            f"Rate limit reached on Bybit V5: {ret_msg}. Backing off 1.5s."
                        )
                        await asyncio.sleep(1.5)
                        return await self.request(method, path, params, data, auth_required)

                    if ret_code == 10010:
                        logger.error(
                            f"Bybit API IP Whitelist Mismatch (retCode 10010): {ret_msg}. "
                            "Your server's public IP is not in the API key's IP whitelist on Bybit."
                        )
                    elif ret_code in (10003, 10004, 10005, 33004):
                        logger.error(
                            f"Bybit API Authentication/Permission error (retCode {ret_code}): {ret_msg}. "
                            "Ensure API key has read/write permissions for Unified Trading/Contract and Account."
                        )
                    else:
                        logger.debug(
                            f"Bybit API retCode {ret_code}: {ret_msg} (latency: {latency_ms:.1f}ms)"
                        )
                return res_json
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"HTTP request to {path} failed: {e}")
            return {
                "retCode": -1,
                "retMsg": f"Network exception: {e}",
                "result": {},
            }

    # =========================================================================
    # UNIFIED ACCOUNT REST ENDPOINTS
    # =========================================================================

    async def get_wallet_balance(self, account_type: str = "UNIFIED") -> Dict[str, Any]:
        """Fetches total balance, margin, and equity for Unified Trading Account (with CONTRACT fallback)."""
        res = await self.request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": account_type},
            auth_required=True,
        )
        # If UNIFIED fails with non-zero retCode and UNIFIED was requested, attempt fallback to CONTRACT (Classic account)
        if res.get("retCode") not in (0, None) and account_type == "UNIFIED":
            logger.debug(
                f"UTA wallet-balance returned retCode {res.get('retCode')}. Retrying with accountType='CONTRACT'..."
            )
            res_contract = await self.request(
                "GET",
                "/v5/account/wallet-balance",
                params={"accountType": "CONTRACT"},
                auth_required=True,
            )
            if res_contract.get("retCode") == 0:
                logger.info("Successfully fetched wallet balance using Classic CONTRACT account type.")
                return res_contract
        return res

    async def get_positions(
        self,
        symbol: Optional[str] = None,
        settle_coin: str = "USDT",
    ) -> List[Dict[str, Any]]:
        """Queries active linear positions for reconciliation."""
        params: Dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        elif self.category == "linear":
            params["settleCoin"] = settle_coin

        res = await self.request("GET", "/v5/position/list", params=params, auth_required=True)
        if res.get("retCode") == 0:
            return (res.get("result") or {}).get("list") or []
        return []

    async def get_closed_pnl(
        self,
        symbol: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Queries historical realized closed PnL records for linear contracts."""
        params: Dict[str, Any] = {"category": self.category, "limit": min(limit, 100)}
        if symbol:
            params["symbol"] = symbol
        res = await self.request("GET", "/v5/position/closed-pnl", params=params, auth_required=True)
        if res.get("retCode") == 0:
            return (res.get("result") or {}).get("list") or []
        return []

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """Sets leverage for cross/isolated linear perpetual positions."""
        if self.category != "linear":
            return True
        data = {
            "category": "linear",
            "symbol": symbol,
            "buyLeverage": str(leverage),
            "sellLeverage": str(leverage),
        }
        res = await self.request("POST", "/v5/position/set-leverage", data=data, auth_required=True)
        # 110043 means leverage not modified (already set)
        if res.get("retCode") in (0, 110043):
            return True
        logger.warning(f"Failed to set leverage on {symbol}: {res.get('retMsg')}")
        return False

    async def create_order(
        self,
        symbol: str,
        side: str,  # "Buy" or "Sell"
        order_type: str,  # "Limit" or "Market"
        qty: float,
        price: Optional[float] = None,
        time_in_force: str = "PostOnly",  # "PostOnly", "IOC", "GTC"
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        order_link_id: Optional[str] = None,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Submits an order with native Take-Profit and Stop-Loss brackets.
        Supports PostOnly maker execution and IOC breakout execution.
        """
        if order_link_id is None:
            order_link_id = f"ms_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"

        tif = time_in_force
        if order_type == "Market":
            tif = "IOC"

        data: Dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": tif,
            "orderLinkId": order_link_id,
            "reduceOnly": reduce_only,
        }

        if price is not None and order_type == "Limit":
            data["price"] = str(price)

        if self.category == "linear":
            data["positionIdx"] = 0  # 0 for One-Way Mode in UTA
            if take_profit is not None and take_profit > 0:
                data["takeProfit"] = str(take_profit)
                data["tpTriggerBy"] = "LastPrice"
            if stop_loss is not None and stop_loss > 0:
                data["stopLoss"] = str(stop_loss)
                data["slTriggerBy"] = "LastPrice"
            if (take_profit and take_profit > 0) or (stop_loss and stop_loss > 0):
                data["tpslMode"] = "Full"

        return await self.request("POST", "/v5/order/create", data=data, auth_required=True)

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        order_link_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Cancels a specific order."""
        data: Dict[str, Any] = {"category": self.category, "symbol": symbol}
        if order_id:
            data["orderId"] = order_id
        if order_link_id:
            data["orderLinkId"] = order_link_id
        return await self.request("POST", "/v5/order/cancel", data=data, auth_required=True)

    async def cancel_all_orders(self, symbol: Optional[str] = None) -> Dict[str, Any]:
        """Cancels all active orders across a symbol or entire category."""
        data: Dict[str, Any] = {"category": self.category}
        if symbol:
            data["symbol"] = symbol
        elif self.category == "linear":
            data["settleCoin"] = "USDT"
        return await self.request("POST", "/v5/order/cancel-all", data=data, auth_required=True)

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Queries current active/unfilled orders."""
        params: Dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        elif self.category == "linear":
            params["settleCoin"] = "USDT"

        res = await self.request("GET", "/v5/order/realtime", params=params, auth_required=True)
        if res.get("retCode") == 0:
            return (res.get("result") or {}).get("list") or []
        return []

    async def set_trading_stop(
        self,
        symbol: str,
        take_profit: Optional[float] = None,
        stop_loss: Optional[float] = None,
        trailing_stop: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Dynamically adjusts Take Profit, Stop Loss, or Trailing Stop on an active position."""
        data: Dict[str, Any] = {
            "category": self.category,
            "symbol": symbol,
            "positionIdx": 0,
            "tpslMode": "Full",
        }
        if take_profit is not None:
            data["takeProfit"] = str(take_profit) if take_profit > 0 else ""
        if stop_loss is not None:
            data["stopLoss"] = str(stop_loss) if stop_loss > 0 else ""
        if trailing_stop is not None:
            data["trailingStop"] = str(trailing_stop)

        return await self.request("POST", "/v5/position/trading-stop", data=data, auth_required=True)

    async def get_tickers(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        """Queries market tickers including 24h stats, mark price, and funding rate."""
        params: Dict[str, Any] = {"category": self.category}
        if symbol:
            params["symbol"] = symbol
        res = await self.request("GET", "/v5/market/tickers", params=params, auth_required=False)
        if res.get("retCode") == 0:
            return (res.get("result") or {}).get("list") or []
        return []

    async def get_funding_rate(self, symbol: str) -> float:
        """Fetches current funding rate for a linear perpetual symbol."""
        if self.category != "linear":
            return 0.0
        tickers = await self.get_tickers(symbol=symbol)
        if tickers:
            try:
                return float(tickers[0].get("fundingRate") or 0.0)
            except (ValueError, TypeError):
                return 0.0
        return 0.0

    async def get_klines(
        self,
        symbol: str,
        interval: str = "1",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Fetches historical klines for indicators initialization."""
        # Bybit format: interval '1', '5', '15', '60', 'D'
        clean_interval = interval.replace("m", "")
        params = {
            "category": self.category,
            "symbol": symbol,
            "interval": clean_interval,
            "limit": limit,
        }
        res = await self.request("GET", "/v5/market/kline", params=params, auth_required=False)
        if res.get("retCode") == 0:
            raw_list = (res.get("result") or {}).get("list") or []
            # Bybit returns klines newest to oldest: [startTime, open, high, low, close, volume, turnover]
            # Reverse to chronological order (oldest to newest)
            parsed = []
            for k in reversed(raw_list):
                try:
                    parsed.append(
                        {
                            "start": int(k[0]),
                            "open": float(k[1]),
                            "high": float(k[2]),
                            "low": float(k[3]),
                            "close": float(k[4]),
                            "volume": float(k[5]),
                            "turnover": float(k[6]),
                        }
                    )
                except (IndexError, ValueError, TypeError):
                    continue
            return parsed
        return []

    # =========================================================================
    # WEBSOCKET STREAMING (PUBLIC & PRIVATE)
    # =========================================================================

    def subscribe_orderbook(self, symbol: str) -> None:
        """Registers symbol for 50-level real-time OrderBook L2."""
        if symbol not in self.orderbooks:
            self.orderbooks[symbol] = OrderBookL2(symbol)
        topic = f"orderbook.50.{symbol}"
        self._public_topics.add(topic)

    def subscribe_kline(self, symbol: str, interval: str) -> None:
        """Registers symbol and timeframe for kline streaming."""
        clean_interval = interval.replace("m", "")
        topic = f"kline.{clean_interval}.{symbol}"
        self._public_topics.add(topic)

    async def start_streams(self) -> None:
        """Spawns resilient background WebSocket tasks."""
        self._ws_tasks.append(asyncio.create_task(self._run_public_ws()))
        self._ws_tasks.append(asyncio.create_task(self._run_private_ws()))

    async def _run_public_ws(self) -> None:
        """Maintains persistent Public WebSocket with auto-reconnect."""
        backoff = 1.0
        while self._running:
            try:
                logger.info(f"Connecting to Public WS: {self.ws_public_url}")
                async with websockets.connect(
                    self.ws_public_url,
                    ping_interval=None,  # We handle native Bybit ping/pong
                    close_timeout=5,
                ) as ws:
                    self._public_ws = ws
                    backoff = 1.0

                    # Subscribe to configured topics
                    if self._public_topics:
                        sub_msg = {"op": "subscribe", "args": list(self._public_topics)}
                        await ws.send(json.dumps(sub_msg))
                        logger.info(f"Subscribed to {len(self._public_topics)} public topics.")

                    ping_task = asyncio.create_task(self._ws_ping_loop(ws))
                    try:
                        async for msg in ws:
                            await self._handle_public_message(msg)
                    finally:
                        ping_task.cancel()
            except (ConnectionClosed, asyncio.CancelledError, Exception) as e:
                if not self._running:
                    break
                logger.warning(f"Public WS disconnected ({e}). Reconnecting in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _run_private_ws(self) -> None:
        """Maintains persistent Private WebSocket with signature auth and auto-reconnect."""
        backoff = 1.0
        while self._running:
            try:
                logger.info(f"Connecting to Private WS: {self.ws_private_url}")
                async with websockets.connect(
                    self.ws_private_url,
                    ping_interval=None,
                    close_timeout=5,
                ) as ws:
                    self._private_ws = ws
                    backoff = 1.0

                    # Authenticate
                    expires = int(time.time() * 1000) + 10000
                    sign_str = f"GET/realtime{expires}"
                    signature = hmac.new(
                        self.creds.api_secret.encode("utf-8"),
                        sign_str.encode("utf-8"),
                        hashlib.sha256,
                    ).hexdigest()

                    auth_msg = {
                        "op": "auth",
                        "args": [self.creds.api_key, expires, signature],
                    }
                    await ws.send(json.dumps(auth_msg))

                    # Subscribe to private execution topics
                    private_topics = ["execution", "order", "position", "wallet"]
                    sub_msg = {"op": "subscribe", "args": private_topics}
                    await ws.send(json.dumps(sub_msg))
                    logger.info("Private WS authenticated & subscribed to execution topics.")

                    ping_task = asyncio.create_task(self._ws_ping_loop(ws))
                    try:
                        async for msg in ws:
                            await self._handle_private_message(msg)
                    finally:
                        ping_task.cancel()
            except (ConnectionClosed, asyncio.CancelledError, Exception) as e:
                if not self._running:
                    break
                logger.warning(f"Private WS disconnected ({e}). Reconnecting in {backoff:.1f}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _ws_ping_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Sends native Bybit ping heartbeat every 20 seconds."""
        while self._running:
            try:
                await asyncio.sleep(20)
                if not ws.closed:
                    await ws.send(json.dumps({"op": "ping"}))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug(f"Ping error: {e}")
                break

    async def _handle_public_message(self, raw_msg: str) -> None:
        """Routes public orderbook and kline messages to handlers."""
        data = json.loads(raw_msg)
        topic = data.get("topic", "")

        # Orderbook updates
        if topic.startswith("orderbook."):
            symbol = topic.split(".")[-1]
            ob = self.orderbooks.get(symbol)
            if ob:
                msg_type = data.get("type", "")
                depth_data = data.get("data") or {}
                if msg_type == "snapshot":
                    ob.apply_snapshot(depth_data)
                elif msg_type == "delta":
                    ob.apply_delta(depth_data)

        # Kline updates
        elif topic.startswith("kline."):
            parts = topic.split(".")
            interval = parts[1]
            symbol = parts[2]
            kline_list = data.get("data") or []
            for k in kline_list:
                for cb in self.kline_callbacks:
                    asyncio.create_task(cb(symbol, interval, k))

    async def _handle_private_message(self, raw_msg: str) -> None:
        """Routes private execution, order, position, and wallet updates."""
        data = json.loads(raw_msg)
        topic = data.get("topic", "")
        payload = data.get("data") or []

        if topic == "execution":
            for item in payload:
                for cb in self.execution_callbacks:
                    asyncio.create_task(cb(item))
        elif topic == "order":
            for item in payload:
                for cb in self.order_callbacks:
                    asyncio.create_task(cb(item))
        elif topic == "position":
            for item in payload:
                for cb in self.position_callbacks:
                    asyncio.create_task(cb(item))
        elif topic == "wallet":
            for item in payload:
                for cb in self.wallet_callbacks:
                    asyncio.create_task(cb(item))
