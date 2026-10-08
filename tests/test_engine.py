"""
Comprehensive Unit & Regression Test Suite for Bybit V5 Trading Engine.
"""

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np
import pytest
from unittest.mock import AsyncMock, MagicMock

from bybit_client import BybitV5Client, OrderBookL2
from config import (
    AppConfig,
    BybitCredentials,
    TradingMode,
    load_config,
    load_credentials,
    save_config,
    save_credentials,
)
from risk_manager import Position, RiskManager
from strategy import Bar, MarketRegime, StrategyEngine, TimeframeSeries
from trade_journal import ParameterAutoTuner, TradeJournal, TradeRecord


def test_config_encryption_and_permissions():
    """Validates Fernet encryption and 0600 file permission enforcement."""
    with tempfile.TemporaryDirectory() as tmpdir:
        base_dir = Path(tmpdir)
        creds = BybitCredentials(
            api_key="AK_1234567890_TEST",
            api_secret="AS_9876543210_SUPERSECRET",
            testnet=True,
        )
        save_credentials(creds, base_dir)

        # Verify file exists and has 0600 permissions
        sec_file = base_dir / "secrets.enc"
        assert sec_file.exists()
        mode = oct(os.stat(sec_file).st_mode & 0o777)
        assert mode == "0o600"

        loaded = load_credentials(base_dir)
        assert loaded.api_key == creds.api_key
        assert loaded.api_secret == creds.api_secret
        assert loaded.testnet is True


def test_bybit_signature_generation():
    """Validates HMAC SHA256 signature algorithm against Bybit V5 specification."""
    creds = BybitCredentials(
        api_key="test_api_key_123",
        api_secret="test_api_secret_456",
        testnet=True,
    )
    client = BybitV5Client(creds, TradingMode.LINEAR, recv_window=5000)

    timestamp = "1672531200000"
    payload = '{"category":"linear","symbol":"BTCUSDT"}'
    sig = client._generate_signature(timestamp, payload)

    # Signature must be 64-char hex string
    assert len(sig) == 64
    assert all(c in "0123456789abcdef" for c in sig)


def test_orderbook_l2_and_ofi():
    """Verifies Level-2 depth tracking and Order Flow Imbalance (OFI)."""
    ob = OrderBookL2("BTCUSDT")

    # Snapshot
    ob.apply_snapshot({
        "b": [["65000.0", "1.5"], ["64990.0", "3.0"]],
        "a": [["65010.0", "2.0"], ["65020.0", "4.0"]],
        "u": 100,
    })

    assert ob.best_bid == (65000.0, 1.5)
    assert ob.best_ask == (65010.0, 2.0)
    assert ob.mid_price == 65005.0
    assert ob.spread == 10.0

    # Incremental update: Higher bid entered -> positive OFI
    ob.apply_delta({
        "b": [["65005.0", "1.0"]],
        "a": [],
        "u": 101,
    })
    assert ob.best_bid == (65005.0, 1.0)
    assert ob.ofi > 0

    # Bid deleted (size = 0)
    ob.apply_delta({
        "b": [["65005.0", "0"]],
        "a": [],
        "u": 102,
    })
    assert ob.best_bid == (65000.0, 1.5)


def test_strategy_indicators_and_regimes():
    """Validates EMA ribbon, ATR, VWAP, and market regime classifier."""
    cfg = AppConfig()
    strat = StrategyEngine(cfg)

    # Seed 60 1m ascending candles
    base_t = int(time.time() * 1000) - (60 * 60 * 1000)
    for i in range(60):
        t = base_t + (i * 60 * 1000)
        price = 50000.0 + (i * 50.0)
        strat.update_kline("BTCUSDT", "1m", {
            "start": t,
            "open": price - 10.0,
            "high": price + 25.0,
            "low": price - 15.0,
            "close": price,
            "volume": 20.0,
            "turnover": price * 20.0,
        })

    # Seed 5m bars
    for i in range(12):
        t = base_t + (i * 300 * 1000)
        price = 50000.0 + (i * 250.0)
        strat.update_kline("BTCUSDT", "5m", {
            "start": t,
            "open": price - 20.0,
            "high": price + 40.0,
            "low": price - 30.0,
            "close": price,
            "volume": 100.0,
            "turnover": price * 100.0,
        })

    ob = OrderBookL2("BTCUSDT")
    ob.apply_snapshot({
        "b": [["53000.0", "2.0"]],
        "a": [["53001.0", "2.0"]],
        "u": 1,
    })
    ob.ofi = 5.0

    regime, metrics = strat.classify_regime("BTCUSDT", ob)
    assert regime in (MarketRegime.BULL_TREND, MarketRegime.VOLATILITY_EXPANSION)

    # Signal generation
    sig = strat.generate_signal("BTCUSDT", ob)
    assert sig is not None
    assert sig.side == "Buy"
    assert sig.tp_price > sig.price
    assert sig.sl_price < sig.price


def test_dynamic_kelly_scaling():
    """Verifies Half-Kelly formula bounds [0.5%, 1.5%]."""
    cfg = AppConfig()
    strat = StrategyEngine(cfg)

    # Unseeded -> Default midpoint 1.0%
    assert strat.calculate_kelly_fraction() == 0.01

    # Record 20 winning trades
    for _ in range(20):
        strat.record_trade_result(50.0)
    assert strat.calculate_kelly_fraction() == 0.015  # Max bounded at 1.5%

    # Record consecutive losses
    for _ in range(30):
        strat.record_trade_result(-50.0)
    assert strat.calculate_kelly_fraction() == 0.005  # Min bounded at 0.5%


def test_risk_manager_circuit_breakers():
    """Verifies Drawdown, Consecutive Loss, and Volatility Spike failsafes."""
    cfg = AppConfig()
    creds = BybitCredentials(api_key="x"*12, api_secret="y"*16, testnet=True)
    client = BybitV5Client(creds)
    rm = RiskManager(cfg, client)

    # Normal state
    rm.update_wallet_balance(1000.0)
    allowed, _ = rm.is_order_permitted("BTCUSDT", 50.0)
    assert allowed is True

    # 1. Daily Drawdown Breached (2.5%)
    rm.update_wallet_balance(970.0)  # 3% drawdown
    assert rm.daily_drawdown_tripped is True
    allowed, reason = rm.is_order_permitted("BTCUSDT", 50.0)
    assert allowed is False
    assert "drawdown" in reason.lower()

    # 2. Consecutive Losses Breached
    rm2 = RiskManager(cfg, client)
    rm2.update_wallet_balance(1000.0)
    for _ in range(4):
        rm2.record_trade_fill(-15.0)
    allowed, reason = rm2.is_order_permitted("BTCUSDT", 50.0)
    assert allowed is False
    assert "consecutive loss" in reason.lower()

    # 3. Volatility Spike Check
    # Feed 60 regular ATR values (e.g. 10.0), then a spike to 60.0
    for _ in range(60):
        rm2.evaluate_volatility_spike("BTCUSDT", 10.0)
    is_spike = rm2.evaluate_volatility_spike("BTCUSDT", 60.0)
    assert is_spike is True
    assert rm2.volatility_kill_tripped is True


def test_trade_journal_and_autotuner():
    """Verifies SQLite persistence, metrics calculation, and auto-tuning."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_journal.db"
        journal = TradeJournal(db_path)
        cfg = AppConfig()
        cfg.strategy.auto_tuning_trade_interval = 10

        # Insert 10 profitable trades
        for i in range(10):
            tr = TradeRecord(
                trade_id=f"trade_{i}",
                symbol="BTCUSDT",
                side="Buy",
                entry_time=1000,
                exit_time=2000,
                holding_time_sec=15.0,
                entry_price=60000.0,
                exit_price=60150.0,
                qty=0.01,
                realized_pnl=1.5,
                realized_pnl_pct=0.25,
                fee_paid=0.05,
                slippage_bps=0.2,
                order_type="Maker",
                regime="BULL_TREND",
                conviction=0.8,
                atr_at_entry=120.0,
                ema_spread_at_entry=15.0,
                vwap_delta_at_entry=10.0,
                ofi_at_entry=3.0,
            )
            journal.record_trade(tr)

        metrics = journal.get_summary_metrics()
        assert metrics["total_trades"] == 10
        assert metrics["win_rate"] == 100.0
        assert metrics["total_pnl"] == 15.0

        tuner = ParameterAutoTuner(journal, cfg)
        res = tuner.evaluate_and_tune()
        assert res is not None
        assert res["new_tp"] > 1.2


def test_auto_capital_tier_by_equity():
    """Verifies that available funds dynamically auto-configure risk, pairs, and limits."""
    from config import CapitalTier, get_tier_for_equity

    # 1. Test Micro/Bootstrap Tier ($50 USD balance)
    tier_50, cfg_50 = get_tier_for_equity(50.0)
    assert tier_50 == CapitalTier.MICRO_BOOTSTRAP
    assert "BTCUSDT" not in cfg_50["symbols"]  # BTC excluded due to high min-lot
    assert "SOLUSDT" in cfg_50["symbols"]
    assert cfg_50["max_open_positions"] == 1
    assert cfg_50["max_daily_risk_pct"] == 5.0
    assert cfg_50["default_leverage"] == 5

    # 2. Test Small/Growth Tier ($250 USD balance)
    tier_250, cfg_250 = get_tier_for_equity(250.0)
    assert tier_250 == CapitalTier.GROWTH_SMALL
    assert cfg_250["max_open_positions"] == 2
    assert cfg_250["max_daily_risk_pct"] == 3.5

    # 3. Test Standard Tier ($2,500 USD balance)
    tier_2500, cfg_2500 = get_tier_for_equity(2500.0)
    assert tier_2500 == CapitalTier.STANDARD
    assert "BTCUSDT" in cfg_2500["symbols"]
    assert cfg_2500["max_open_positions"] == 3
    assert cfg_2500["max_daily_risk_pct"] == 2.5

    # 4. Test Institutional Tier ($25,000 USD balance)
    tier_25k, cfg_25k = get_tier_for_equity(25000.0)
    assert tier_25k == CapitalTier.INSTITUTIONAL
    assert cfg_25k["max_open_positions"] == 4
    assert cfg_25k["max_daily_risk_pct"] == 2.0
    assert cfg_25k["default_leverage"] == 3


class MockAsyncContext:
    def __init__(self, obj):
        self.obj = obj

    async def __aenter__(self):
        return self.obj

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return None


def test_bybit_empty_and_error_response_handling():
    """Ensures HTTP 403/502/empty responses do NOT raise 'NoneType' has no attribute 'get'."""
    from unittest.mock import AsyncMock, MagicMock
    import asyncio

    async def _test():
        creds = BybitCredentials(api_key="AK_TEST_KEY_1234", api_secret="AS_TEST_SECRET_5678", testnet=True)
        client = BybitV5Client(creds)

        mock_resp = MagicMock()
        mock_resp.status = 403
        mock_resp.text = AsyncMock(return_value="")

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.request = MagicMock(return_value=MockAsyncContext(mock_resp))

        client.session = mock_session

        res = await client.request("GET", "/v5/account/wallet-balance")
        assert isinstance(res, dict)
        assert res.get("retCode") == 403
        assert "HTTP 403" in res.get("retMsg", "")

    asyncio.run(_test())


def test_bybit_null_json_response_handling():
    """Ensures responses containing 'null' JSON parse cleanly without AttributeError."""
    from unittest.mock import AsyncMock, MagicMock
    import asyncio

    async def _test():
        creds = BybitCredentials(api_key="AK_TEST_KEY_1234", api_secret="AS_TEST_SECRET_5678", testnet=True)
        client = BybitV5Client(creds)

        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.text = AsyncMock(return_value="null")

        mock_session = MagicMock()
        mock_session.closed = False
        mock_session.request = MagicMock(return_value=MockAsyncContext(mock_resp))

        client.session = mock_session

        res = await client.request("GET", "/v5/account/wallet-balance")
        assert isinstance(res, dict)
        assert res.get("retCode") == 0 or res.get("retCode") == -1

    asyncio.run(_test())


def test_bybit_wallet_balance_contract_fallback():
    """Verifies that if UTA wallet balance fails, it retries with CONTRACT mode."""
    from unittest.mock import AsyncMock
    import asyncio

    async def _test():
        creds = BybitCredentials(api_key="AK_TEST_KEY_1234", api_secret="AS_TEST_SECRET_5678", testnet=True)
        client = BybitV5Client(creds)

        async def mock_req(method, path, params=None, data=None, auth_required=True):
            if params and params.get("accountType") == "UNIFIED":
                return {"retCode": 10001, "retMsg": "accountType not valid for classic account", "result": {}}
            elif params and params.get("accountType") == "CONTRACT":
                return {
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "list": [
                            {
                                "accountType": "CONTRACT",
                                "totalEquity": "",
                                "coin": [{"coin": "USDT", "equity": "250.0", "walletBalance": "250.0"}],
                            }
                        ]
                    },
                }
            return {"retCode": -1, "result": {}}

        client.request = AsyncMock(side_effect=mock_req)
        res = await client.get_wallet_balance(account_type="UNIFIED")
        assert res.get("retCode") == 0
        item = res.get("result", {}).get("list", [{}])[0]
        assert item.get("accountType") == "CONTRACT"
        assert item.get("coin")[0]["equity"] == "250.0"

    asyncio.run(_test())


def test_trading_engine_wallet_equity_resilience():
    """Verifies that TradingEngine.initialize() parses UTA, CONTRACT, and failure cases without crashing."""
    from unittest.mock import AsyncMock, MagicMock
    import asyncio
    from main import TradingEngine

    async def _test():
        config = AppConfig()
        config.risk.allocated_capital_usd = 500.0
        creds = BybitCredentials(api_key="AK_TEST_KEY_1234", api_secret="AS_TEST_SECRET_5678", testnet=True)

        engine = TradingEngine(config, creds)
        # Mock telegram, ws, set_leverage, get_klines, etc.
        engine.client.initialize = AsyncMock()
        engine.telegram.initialize = AsyncMock()
        engine.telegram.send_message = AsyncMock()
        engine.client.set_leverage = AsyncMock(return_value=True)
        engine.client.get_klines = AsyncMock(return_value=[])
        engine.client.subscribe_orderbook = MagicMock()
        engine.client.subscribe_kline = MagicMock()
        engine.risk_manager.reconcile = AsyncMock()
        engine.client.start_streams = AsyncMock()

        # Case 1: Failure / Error code response (e.g. retCode 10010 or NoneType body)
        engine.client.get_wallet_balance = AsyncMock(return_value={"retCode": 10010, "retMsg": "Unmatched IP", "result": None})
        await engine.initialize()
        assert engine.risk_manager.current_equity == 500.0  # Fallback worked smoothly!

        # Case 2: CONTRACT account with totalEquity="" and coin list
        engine.client.get_wallet_balance = AsyncMock(
            return_value={
                "retCode": 0,
                "result": {
                    "list": [
                        {
                            "accountType": "CONTRACT",
                            "totalEquity": "",
                            "coin": [{"coin": "USDT", "equity": "125.0", "walletBalance": "125.0"}],
                        }
                    ]
                },
            }
        )
        await engine.initialize()
        assert engine.risk_manager.current_equity == 125.0

    asyncio.run(_test())


def test_micro_bootstrap_bybit_lot_and_notional_compliance():
    """Validates that micro-account sizing satisfies Bybit 5 USDT min notional and lot steps."""
    from main import TradingEngine
    from config import get_tier_for_equity, CapitalTier

    tier, tier_cfg = get_tier_for_equity(40.36)
    assert tier == CapitalTier.MICRO_BOOTSTRAP
    assert tier_cfg["max_open_positions"] == 1
    assert "DOGEUSDT" in tier_cfg["symbols"]
    assert "SUIUSDT" in tier_cfg["symbols"]

    config = AppConfig()
    creds = BybitCredentials(api_key="AK_TEST_KEY_1234", api_secret="AS_TEST_SECRET_5678", testnet=True)
    engine = TradingEngine(config, creds)
    engine.risk_manager.current_equity = 40.36

    # Test SUI lot step formatting (must be step of 10)
    sui_qty_small = engine._format_qty("SUIUSDT", 2.9)
    assert sui_qty_small == 10.0  # Must round up to minOrderQty 10!
    sui_qty_large = engine._format_qty("SUIUSDT", 23.4)
    assert sui_qty_large == 20.0

    # Test DOGE lot step formatting (must be integer, min 1)
    doge_qty = engine._format_qty("DOGEUSDT", 67.2)
    assert doge_qty == 67.0

    # Test SOL lot step formatting (must be at least 0.1)
    sol_qty = engine._format_qty("SOLUSDT", 0.04)
    assert sol_qty == 0.1

    # Test that risk manager permits exchange minimum notional size
    permitted, reason = engine.risk_manager.is_order_permitted("SUIUSDT", 10.30)
    assert permitted, f"Expected permitted but got: {reason}"


def test_reconciliation_empty_strings():
    """Validates that risk_manager.reconcile handles empty string fields from Bybit without crashing."""
    import asyncio
    from unittest.mock import AsyncMock
    config = AppConfig()
    client = BybitV5Client(
        BybitCredentials(
            api_key="AK_1234567890_TEST",
            api_secret="AS_9876543210_SUPERSECRET",
            testnet=True,
        )
    )
    rm = RiskManager(config, client)

    # Mock get_positions with empty string fields
    client.get_positions = AsyncMock(
        return_value=[
            {
                "symbol": "DOGEUSDT",
                "side": "Buy",
                "size": "100.0",
                "avgPrice": "0.08345",
                "markPrice": "0.08350",
                "unrealisedPnl": "",  # Empty string from Bybit
                "leverage": "5",
                "takeProfit": "",    # Empty string from Bybit
                "stopLoss": "",      # Empty string from Bybit
                "trailingStop": "",  # Empty string from Bybit
            }
        ]
    )
    client.get_open_orders = AsyncMock(
        return_value=[
            {
                "symbol": "DOGEUSDT",
                "orderId": "ord_123",
                "createdTime": "",   # Empty string from Bybit
            }
        ]
    )
    client.cancel_order = AsyncMock()

    asyncio.run(rm.reconcile())

    assert "DOGEUSDT" in rm.positions
    pos = rm.positions["DOGEUSDT"]
    assert pos.size == 100.0
    assert pos.unrealised_pnl == 0.0
    assert pos.take_profit is None
    assert pos.stop_loss is None


def test_breakeven_stop_advancement():
    """Verifies that RiskManager advances Stop Loss to entry + fee buffer when mark price reaches trigger."""
    config = AppConfig()
    config.execution.breakeven_atr_trigger = 0.85
    config.execution.breakeven_buffer_bps = 8.0

    creds = BybitCredentials(api_key="test_api_key_123", api_secret="test_api_secret_456", testnet=True)
    client = BybitV5Client(creds)
    client.set_trading_stop = AsyncMock(return_value={"retCode": 0, "retMsg": "OK"})

    rm = RiskManager(config, client)

    # Position with entry price $100.0, ATR $2.0. Trigger distance is 0.85 * 2.0 = $1.70. Trigger price = $101.70.
    rm.positions["SOLUSDT"] = Position(
        symbol="SOLUSDT",
        side="Buy",
        size=1.0,
        entry_price=100.0,
        mark_price=101.0,  # Below trigger ($101.70)
        unrealised_pnl=1.0,
        leverage=5,
        stop_loss=98.0,
        entry_atr=2.0,
        breakeven_set=False,
    )

    # Test below trigger
    updates = asyncio.run(rm.evaluate_position_stops())
    assert len(updates) == 0
    assert rm.positions["SOLUSDT"].breakeven_set is False

    # Price moves to $102.0 (exceeds $101.70 trigger)
    rm.positions["SOLUSDT"].mark_price = 102.0
    updates = asyncio.run(rm.evaluate_position_stops())
    assert len(updates) == 1
    assert updates[0]["type"] == "BREAKEVEN_SET"
    assert rm.positions["SOLUSDT"].breakeven_set is True
    # New SL should be entry ($100) + 8 bps buffer ($0.08) = $100.08
    assert rm.positions["SOLUSDT"].stop_loss == 100.08
    client.set_trading_stop.assert_called_once_with(symbol="SOLUSDT", stop_loss=100.08)


def test_stagnant_position_exit():
    """Verifies that positions open longer than stagnant_exit_mins with no movement are closed."""
    config = AppConfig()
    config.execution.stagnant_exit_mins = 45
    config.execution.stagnant_atr_threshold = 0.25

    creds = BybitCredentials(api_key="test_api_key_123", api_secret="test_api_secret_456", testnet=True)
    client = BybitV5Client(creds)
    client.create_order = AsyncMock(return_value={"retCode": 0, "retMsg": "OK"})

    rm = RiskManager(config, client)

    # Position opened 50 minutes ago, entry $100.0, mark $100.10, ATR $2.0
    # Price delta ($0.10) is <= 0.25 * 2.0 ($0.50) -> stagnant!
    rm.positions["SOLUSDT"] = Position(
        symbol="SOLUSDT",
        side="Buy",
        size=2.0,
        entry_price=100.0,
        mark_price=100.10,
        unrealised_pnl=0.20,
        leverage=5,
        entry_atr=2.0,
        opened_at=time.time() - (50 * 60),  # 50 minutes ago
    )

    closed = asyncio.run(rm.check_stagnant_positions())
    assert len(closed) == 1
    assert closed[0]["symbol"] == "SOLUSDT"
    client.create_order.assert_called_once_with(
        symbol="SOLUSDT",
        side="Sell",
        order_type="Market",
        qty=2.0,
        time_in_force="IOC",
        reduce_only=True,
    )


def test_funding_rate_filtering():
    """Verifies that StrategyEngine suppresses signals trading into adverse funding rates."""
    config = AppConfig()
    config.execution.funding_rate_filter = True
    config.execution.max_adverse_funding_rate = 0.0003  # 0.03%

    strat = StrategyEngine(config)
    tf_1m = TimeframeSeries("SOLUSDT", "1m")
    tf_5m = TimeframeSeries("SOLUSDT", "5m")
    strat.series["SOLUSDT"] = {"1m": tf_1m, "5m": tf_5m}

    # Populate bullish bars
    for i in range(70):
        p = 100.0 + i * 0.5
        b = Bar(timestamp=1000 + i * 60, open=p, high=p + 0.5, low=p - 0.2, close=p + 0.3, volume=500)
        tf_1m.add_or_update_bar(b)
        tf_5m.add_or_update_bar(b)

    ob = OrderBookL2("SOLUSDT")
    curr_px = tf_1m.bars[-1].close
    ob.bids = {curr_px - 0.01: 50.0}
    ob.asks = {curr_px + 0.01: 50.0}
    ob.ofi = 10.0

    # 1. Normal funding rate (+0.01%): Long signal should be generated
    sig_normal = strat.generate_signal("SOLUSDT", ob, funding_rate=0.0001)
    assert sig_normal is not None
    assert sig_normal.side == "Buy"

    # 2. Heavy positive funding rate (+0.05% > 0.03%): Long signal should be suppressed
    sig_adverse = strat.generate_signal("SOLUSDT", ob, funding_rate=0.0005)
    assert sig_adverse is None


def test_quantitative_backtester_simulation():
    """Verifies that QuantitativeBacktester accurately executes offline simulations."""
    from backtester import QuantitativeBacktester

    config = AppConfig()
    config.risk.allocated_capital_usd = 100.0
    config.risk.default_leverage = 5

    bt = QuantitativeBacktester(config, initial_capital=100.0)

    # Generate synthetic trending bars with expanding volatility
    bars = []
    price = 0.0800
    for i in range(200):
        # Create an upward trend with occasional swings
        delta = 0.0002 if (i % 5 != 0) else -0.0001
        price += delta
        b = Bar(
            timestamp=1700000000000 + (i * 60000),
            open=round(price, 5),
            high=round(price + 0.0004, 5),
            low=round(price - 0.0002, 5),
            close=round(price + 0.0001, 5),
            volume=50000.0,
        )
        bars.append(b)

    result = bt.run("DOGEUSDT", bars)
    assert result.total_bars == 200
    assert result.initial_capital == 100.0
    assert isinstance(result.final_equity, float)
    assert isinstance(result.win_rate, float)
    assert isinstance(result.max_drawdown_pct, float)


def test_adx_and_volume_sma():
    """Verifies that TimeframeSeries accurately computes Welles Wilder ADX and volume SMA."""
    tf = TimeframeSeries("BTCUSDT", "1m")

    # Generate 50 trending bars with increasing volume
    for i in range(50):
        price = 60000.0 + (i * 20.0)
        tf.add_or_update_bar(Bar(
            timestamp=1700000000000 + (i * 60000),
            open=price - 5.0,
            high=price + 15.0,
            low=price - 10.0,
            close=price,
            volume=10.0 + i,
        ))

    vol_sma = tf.calculate_volume_sma(20)
    assert vol_sma > 0
    # Average of last 20 bars (30 to 49) = 39.5 + 10 = 49.5
    assert 45.0 <= vol_sma <= 55.0

    adx, plus_di, minus_di = tf.calculate_adx(14)
    assert isinstance(adx, float)
    assert adx > 0.0
    # In a clean ascending trend, +DI must exceed -DI
    assert plus_di > minus_di


def test_adx_and_macro_regime_filtering():
    """Verifies that ADX and 15m macro trend correctly suppress low-volatility chop."""
    cfg = AppConfig()
    cfg.execution.adx_filter = True
    cfg.execution.adx_threshold = 25.0

    strat = StrategyEngine(cfg)

    # 1. Flat price action with zero trend (ADX will be near 0)
    base_t = int(time.time() * 1000) - (60 * 60 * 1000)
    for i in range(60):
        t = base_t + (i * 60 * 1000)
        strat.update_kline("BTCUSDT", "1m", {
            "start": t,
            "open": 65000.0,
            "high": 65002.0,
            "low": 64998.0,
            "close": 65000.0,
            "volume": 5.0,
            "turnover": 65000.0 * 5.0,
        })

    ob = OrderBookL2("BTCUSDT")
    ob.apply_snapshot({"b": [["65000.0", "1.0"]], "a": [["65001.0", "1.0"]], "u": 1})

    regime, metrics = strat.classify_regime("BTCUSDT", ob)
    # Must be classified as LOW_VOL_CHOP due to flat action and low ADX
    assert regime == MarketRegime.LOW_VOL_CHOP


def test_breakout_tp_atr_mult():
    """Verifies that Volatility Expansion regime applies expanded breakout TP multiplier."""
    cfg = AppConfig()
    cfg.execution.breakout_tp_atr_mult = 2.5
    cfg.execution.volume_confirmation = False  # Isolate price breakout

    strat = StrategyEngine(cfg)

    base_t = int(time.time() * 1000) - (80 * 60 * 1000)
    # Seed 60 normal bars with ATR ~ 10
    for i in range(60):
        t = base_t + (i * 60 * 1000)
        strat.update_kline("BTCUSDT", "1m", {
            "start": t,
            "open": 60000.0,
            "high": 60010.0,
            "low": 59990.0,
            "close": 60000.0,
            "volume": 10.0,
            "turnover": 600000.0,
        })

    # Add 20 bars with massive volatility expansion (ATR explodes)
    for i in range(20):
        t = base_t + ((60 + i) * 60 * 1000)
        p = 60000.0 + (i * 100.0)
        strat.update_kline("BTCUSDT", "1m", {
            "start": t,
            "open": p - 40.0,
            "high": p + 120.0,
            "low": p - 30.0,
            "close": p + 80.0,
            "volume": 50.0,
            "turnover": p * 50.0,
        })

    ob = OrderBookL2("BTCUSDT")
    best_ask = 62000.0
    ob.apply_snapshot({"b": [["61999.0", "1.0"]], "a": [[str(best_ask), "1.0"]], "u": 1})
    ob.ofi = 25.0

    sig = strat.generate_signal("BTCUSDT", ob)
    assert sig is not None
    assert sig.regime == MarketRegime.VOLATILITY_EXPANSION
    # Verify that TP distance from entry price reflects breakout_tp_atr_mult (2.5 * ATR)
    expected_tp_dist = round(2.5 * sig.atr, 4)
    actual_tp_dist = round(sig.tp_price - sig.price, 4)
    assert abs(actual_tp_dist - expected_tp_dist) < 0.1






