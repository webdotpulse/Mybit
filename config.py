"""
Production Configuration Module for Bybit V5 Unified Trading Account Engine.
Validates parameters with Pydantic v2 and enforces strict security via Fernet encryption.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from pydantic import BaseModel, Field, field_validator


class TradingMode(str, Enum):
    LINEAR = "linear"  # USDT Perpetual (Unified Trading Account)
    SPOT = "spot"      # Spot Trading


class BybitCredentials(BaseModel):
    """Raw Bybit API credentials."""
    api_key: str = Field(..., min_length=10, description="Bybit V5 API Key")
    api_secret: str = Field(..., min_length=15, description="Bybit V5 API Secret")
    testnet: bool = Field(default=False, description="Use Bybit Testnet environment")

    @field_validator("api_key", "api_secret")
    @classmethod
    def strip_whitespace(cls, v: str) -> str:
        return v.strip()


class CapitalTier(str, Enum):
    MICRO_BOOTSTRAP = "MICRO_BOOTSTRAP"  # $10 - $99 USD (e.g. $50)
    GROWTH_SMALL = "GROWTH_SMALL"        # $100 - $999 USD
    STANDARD = "STANDARD"                # $1,000 - $9,999 USD
    INSTITUTIONAL = "INSTITUTIONAL"      # $10,000+ USD


def get_tier_for_equity(equity: float) -> Tuple[CapitalTier, Dict[str, Any]]:
    """
    Computes mathematically optimal risk bounds, pairs, and exposure limits
    strictly based on the account's available funds.
    """
    if equity < 100.0:
        return (
            CapitalTier.MICRO_BOOTSTRAP,
            {
                "symbols": ["DOGEUSDT", "SUIUSDT", "SOLUSDT"],
                "max_open_positions": 1,
                "max_daily_risk_pct": 5.0,  # 5% gives breathing room on micro accounts
                "min_position_equity_pct": 2.0,
                "max_position_equity_pct": 5.0,
                "default_leverage": 5,
                "auto_tuning_interval": 15,
                "description": "Micro/Bootstrap Tier ($10 - $99). Low-notional altcoins (DOGE/SUI/SOL), max 1 position, 5% daily risk."
            }
        )
    elif equity < 1000.0:
        # Accounts under $250 stick to DOGE, SUI, and SOL (excluding ETH to prevent overexposure)
        active_symbols = ["DOGEUSDT", "SUIUSDT", "SOLUSDT"] if equity < 250.0 else ["DOGEUSDT", "SUIUSDT", "SOLUSDT", "ETHUSDT"]
        return (
            CapitalTier.GROWTH_SMALL,
            {
                "symbols": active_symbols,
                "max_open_positions": 2,
                "max_daily_risk_pct": 3.5,
                "min_position_equity_pct": 1.0,
                "max_position_equity_pct": 3.0,
                "default_leverage": 5,
                "auto_tuning_interval": 20,
                "description": f"Growth Tier (${int(equity)} USD). Highly liquid pairs, max 2 concurrent positions, 3.5% daily risk."
            }
        )
    elif equity < 10000.0:
        return (
            CapitalTier.STANDARD,
            {
                "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                "max_open_positions": 3,
                "max_daily_risk_pct": 2.5,
                "min_position_equity_pct": 0.5,
                "max_position_equity_pct": 1.5,
                "default_leverage": 5,
                "auto_tuning_interval": 20,
                "description": "Standard Quantitative Tier ($1,000 - $9,999). Major bluechips, max 3 positions, 2.5% daily risk."
            }
        )
    else:
        return (
            CapitalTier.INSTITUTIONAL,
            {
                "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT", "AVAXUSDT"],
                "max_open_positions": 4,
                "max_daily_risk_pct": 2.0,
                "min_position_equity_pct": 0.5,
                "max_position_equity_pct": 1.0,
                "default_leverage": 3,
                "auto_tuning_interval": 25,
                "description": "Institutional Tier ($10,000+). Major pairs, 4 positions, conservative 2.0% daily risk, 3x leverage."
            }
        )


class RiskConfig(BaseModel):
    """Mission-Critical Risk Management and Capital Protection Limits."""
    auto_tier_by_equity: bool = Field(
        default=True,
        description="Automatically adapt symbols, risk bounds, and lot sizes based on live available funds"
    )
    current_tier: CapitalTier = Field(
        default=CapitalTier.STANDARD,
        description="Currently active dynamic capital tier"
    )
    allocated_capital_usd: float = Field(
        default=1000.0,
        gt=0,
        description="Allocated trading equity pool in USD"
    )
    max_daily_risk_pct: float = Field(
        default=2.5,
        ge=0.5,
        le=10.0,
        description="Daily max drawdown circuit breaker threshold (%)"
    )
    min_position_equity_pct: float = Field(
        default=0.5,
        ge=0.1,
        le=5.0,
        description="Lower bound Kelly position sizing fraction (%)"
    )
    max_position_equity_pct: float = Field(
        default=1.5,
        ge=0.5,
        le=10.0,
        description="Upper bound Kelly position sizing fraction (%)"
    )
    max_consecutive_losses: int = Field(
        default=4,
        ge=2,
        le=10,
        description="Consecutive loss cutoff triggering cooldown"
    )
    consecutive_loss_cooldown_mins: int = Field(
        default=30,
        ge=5,
        le=240,
        description="Pause trading duration after consecutive loss cutoff (minutes)"
    )
    atr_spike_threshold_std: float = Field(
        default=3.0,
        ge=2.0,
        le=6.0,
        description="ATR volatility spike kill switch threshold in standard deviations"
    )
    default_leverage: int = Field(
        default=5,
        ge=1,
        le=50,
        description="Default position leverage for linear perpetuals"
    )
    max_open_positions: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum concurrent open positions across all pairs"
    )


class ExecutionConfig(BaseModel):
    """Maker-first execution and order placement rules."""
    maker_first: bool = Field(
        default=True,
        description="Prioritize Post-Only limit orders to capture maker rebates"
    )
    post_only_retry_limit: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum retries for adjusting maker post-only limit price"
    )
    post_only_timeout_sec: float = Field(
        default=2.5,
        ge=0.5,
        le=15.0,
        description="Timeout waiting for maker fill before recalculating or aggressive IOC"
    )
    bracket_tp_atr_mult: float = Field(
        default=1.35,
        ge=0.4,
        le=5.0,
        description="ATR multiplier for native Take Profit bracket"
    )
    breakout_tp_atr_mult: float = Field(
        default=2.2,
        ge=1.0,
        le=5.0,
        description="ATR multiplier for high-conviction breakout Take Profit bracket"
    )
    bracket_sl_atr_mult: float = Field(
        default=0.90,
        ge=0.2,
        le=3.0,
        description="ATR multiplier for native Stop Loss bracket"
    )
    trailing_stop: bool = Field(
        default=True,
        description="Enable dynamic trailing stop adjustment as position moves in profit"
    )
    breakeven_atr_trigger: float = Field(
        default=0.75,
        ge=0.2,
        le=3.0,
        description="ATR gain trigger to advance Stop Loss to Breakeven (+ fee buffer)"
    )
    breakeven_buffer_bps: float = Field(
        default=12.0,
        ge=0.0,
        le=50.0,
        description="Buffer in basis points above entry price when setting Breakeven Stop Loss (covers round-trip fees + slippage)"
    )
    stagnant_exit_mins: int = Field(
        default=15,
        ge=2,
        le=240,
        description="Holding minutes after which a stagnant scalp position is evaluated for exit"
    )
    stagnant_atr_threshold: float = Field(
        default=0.25,
        ge=0.05,
        le=1.0,
        description="Price must be within this ATR multiple of entry to trigger stagnant exit"
    )
    funding_rate_filter: bool = Field(
        default=True,
        description="Filter trades against adverse perpetual funding rates"
    )
    max_adverse_funding_rate: float = Field(
        default=0.0003,
        ge=0.0001,
        le=0.002,
        description="Maximum adverse funding rate (0.03%) before suppressing trend entries"
    )
    trade_cooldown_mins: float = Field(
        default=0.5,
        ge=0.0,
        le=120.0,
        description="Minimum cooldown in minutes after trade exit before re-entering same pair (e.g. 0.5 = 30s)"
    )
    adx_filter: bool = Field(
        default=True,
        description="Enforce ADX trend strength threshold to suppress low-volatility chop"
    )
    adx_threshold: float = Field(
        default=20.0,
        ge=10.0,
        le=50.0,
        description="Minimum ADX required for trend scalping signals"
    )
    volume_confirmation: bool = Field(
        default=True,
        description="Require candle volume >= multiplier * 20-period volume SMA"
    )
    volume_multiplier: float = Field(
        default=1.1,
        ge=0.8,
        le=3.0,
        description="Volume multiplier relative to SMA(20) required for breakout entries"
    )
    slippage_tolerance_bps: float = Field(
        default=5.0,
        ge=1.0,
        le=30.0,
        description="Maximum slippage tolerance in basis points for IOC orders"
    )


class StrategyConfig(BaseModel):
    """Algorithmic trend and regime parameters."""
    symbols: List[str] = Field(
        default_factory=lambda: ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        min_length=1,
        description="Trading pair tickers"
    )
    timeframes: List[str] = Field(
        default_factory=lambda: ["1m", "5m", "15m"],
        description="Multi-timeframe analysis intervals"
    )
    ema_fast: int = Field(default=9, ge=3, le=20)
    ema_mid: int = Field(default=21, ge=10, le=50)
    ema_slow: int = Field(default=50, ge=30, le=200)
    vwap_window_mins: int = Field(default=60, ge=15, le=240)
    atr_period: int = Field(default=14, ge=5, le=50)
    ofi_depth_levels: int = Field(default=20, ge=5, le=50)
    kelly_scale: float = Field(
        default=0.5,
        ge=0.1,
        le=1.0,
        description="Half-Kelly scaling factor"
    )
    auto_tuning_trade_interval: int = Field(
        default=20,
        ge=10,
        le=100,
        description="Number of closed trades between parameter auto-tuning passes"
    )


class TelegramConfig(BaseModel):
    """Telegram notifications and emergency shutoff listener."""
    enabled: bool = Field(default=False)
    bot_token: Optional[str] = Field(default=None)
    chat_id: Optional[str] = Field(default=None)
    poll_commands: bool = Field(default=True)


class AppConfig(BaseModel):
    """Root Engine Configuration."""
    trading_mode: TradingMode = Field(default=TradingMode.LINEAR)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    execution: ExecutionConfig = Field(default_factory=ExecutionConfig)
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    data_dir: Path = Field(default_factory=lambda: Path("data"))
    log_dir: Path = Field(default_factory=lambda: Path("logs"))

    @field_validator("data_dir", "log_dir", mode="before")
    @classmethod
    def resolve_paths(cls, v: str | Path) -> Path:
        p = Path(v)
        p.mkdir(parents=True, exist_ok=True)
        return p


# =====================================================================
# SECURE STORAGE & CREDENTIAL ENCRYPTION (Fernet with chmod 600 key)
# =====================================================================

KEY_FILE_NAME = ".engine_key"
SECRETS_FILE_NAME = "secrets.enc"
CONFIG_FILE_NAME = "config.json"


def get_base_dir() -> Path:
    """Returns application root directory."""
    return Path(__file__).resolve().parent


def get_or_create_master_key(base_dir: Optional[Path] = None) -> bytes:
    """
    Loads or generates a cryptographically secure 256-bit Fernet key.
    Enforces POSIX file permissions 0600 (owner read/write only).
    """
    if base_dir is None:
        base_dir = get_base_dir()
    key_path = base_dir / KEY_FILE_NAME

    if key_path.exists():
        # Check permissions
        mode = stat.S_IMODE(os.stat(key_path).st_mode)
        if mode != 0o600 and os.name == "posix":
            os.chmod(key_path, 0o600)
        return key_path.read_bytes().strip()

    # Generate new key
    key = Fernet.generate_key()
    with open(key_path, "wb") as f:
        f.write(key)
    if os.name == "posix":
        os.chmod(key_path, 0o600)
    return key


def save_credentials(creds: BybitCredentials, base_dir: Optional[Path] = None) -> None:
    """Encrypts credentials and stores them securely with 0600 permissions."""
    if base_dir is None:
        base_dir = get_base_dir()
    key = get_or_create_master_key(base_dir)
    fernet = Fernet(key)

    payload = creds.model_dump_json().encode("utf-8")
    encrypted_data = fernet.encrypt(payload)

    secrets_path = base_dir / SECRETS_FILE_NAME
    with open(secrets_path, "wb") as f:
        f.write(encrypted_data)
    if os.name == "posix":
        os.chmod(secrets_path, 0o600)


def load_credentials(base_dir: Optional[Path] = None) -> BybitCredentials:
    """Decrypts credentials from encrypted store."""
    if base_dir is None:
        base_dir = get_base_dir()
    secrets_path = base_dir / SECRETS_FILE_NAME
    if not secrets_path.exists():
        raise FileNotFoundError(
            f"Encrypted secrets file not found at {secrets_path}. "
            "Run installer.py or install.sh to initialize configuration."
        )

    key = get_or_create_master_key(base_dir)
    fernet = Fernet(key)
    encrypted_data = secrets_path.read_bytes()
    decrypted_bytes = fernet.decrypt(encrypted_data)
    data = json.loads(decrypted_bytes.decode("utf-8"))
    return BybitCredentials(**data)


def save_config(config: AppConfig, base_dir: Optional[Path] = None) -> None:
    """Saves non-sensitive application settings to config.json."""
    if base_dir is None:
        base_dir = get_base_dir()
    config_path = base_dir / CONFIG_FILE_NAME
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(config.model_dump_json(indent=2))
    if os.name == "posix":
        os.chmod(config_path, 0o640)


def load_config(base_dir: Optional[Path] = None) -> AppConfig:
    """Loads configuration, creating defaults if not yet present."""
    if base_dir is None:
        base_dir = get_base_dir()
    config_path = base_dir / CONFIG_FILE_NAME
    if not config_path.exists():
        cfg = AppConfig()
        save_config(cfg, base_dir)
        return cfg
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return AppConfig(**data)
