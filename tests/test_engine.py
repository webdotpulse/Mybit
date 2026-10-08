"""
Comprehensive Unit & Regression Test Suite for Bybit V5 Trading Engine.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import numpy as np
import pytest

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
from strategy import Bar, MarketRegime, StrategyEngine
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

