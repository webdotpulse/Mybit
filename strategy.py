"""
Quantitative Strategy & Market Regime Classifier Module.
Implements Multi-Timeframe Feature Engine (1m, 5m, 15m), Order Flow Imbalance (OFI),
Regime Classification (Bull/Bear Trend, Volatility Expansion, Chop),
Dynamic Half-Kelly Position Sizing, and Maker-First Order Formulation.
"""

from __future__ import annotations

import collections
import logging
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

from bybit_client import OrderBookL2
from config import AppConfig, StrategyConfig

logger = logging.getLogger("strategy")


class MarketRegime(str, Enum):
    BULL_TREND = "BULL_TREND"
    BEAR_TREND = "BEAR_TREND"
    TREND_PULLBACK = "TREND_PULLBACK"
    MEAN_REVERSION_RANGE = "MEAN_REVERSION_RANGE"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    LOW_VOL_CHOP = "LOW_VOL_CHOP"


@dataclass
class Bar:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float = 0.0


@dataclass
class Signal:
    symbol: str
    side: str  # "Buy" or "Sell"
    regime: MarketRegime
    conviction: float  # 0.0 to 1.0
    price: float  # Suggested entry price
    tp_price: float
    sl_price: float
    atr: float
    order_type: str = "Limit"
    time_in_force: str = "PostOnly"  # "PostOnly" or "IOC"
    leverage: int = 5
    metadata: Dict[str, float] = field(default_factory=dict)
    tp1_price: Optional[float] = None
    tp2_price: Optional[float] = None


class TimeframeSeries:
    """Manages rolling OHLCV history and technical indicators for one timeframe."""

    def __init__(self, symbol: str, interval: str, max_bars: int = 250):
        self.symbol = symbol
        self.interval = interval
        self.max_bars = max_bars
        self.bars: Deque[Bar] = collections.deque(maxlen=max_bars)

    def add_or_update_bar(self, bar: Bar) -> None:
        """Appends new bar or updates current candle."""
        if not self.bars:
            self.bars.append(bar)
            return

        last = self.bars[-1]
        if bar.timestamp == last.timestamp:
            # Update bar in-place
            self.bars[-1] = bar
        elif bar.timestamp > last.timestamp:
            self.bars.append(bar)

    def get_closes(self) -> np.ndarray:
        return np.array([b.close for b in self.bars], dtype=float)

    def get_highs(self) -> np.ndarray:
        return np.array([b.high for b in self.bars], dtype=float)

    def get_lows(self) -> np.ndarray:
        return np.array([b.low for b in self.bars], dtype=float)

    def get_volumes(self) -> np.ndarray:
        return np.array([b.volume for b in self.bars], dtype=float)

    def calculate_ema(self, period: int) -> float:
        """Calculates current Exponential Moving Average."""
        closes = self.get_closes()
        if len(closes) == 0:
            return 0.0
        if len(closes) <= 2:
            return float(closes[-1])

        eff_period = min(period, len(closes))
        multiplier = 2.0 / (period + 1.0)
        ema = float(np.mean(closes[:eff_period]))
        for c in closes[eff_period:]:
            ema = (c - ema) * multiplier + ema
        return ema

    def calculate_atr(self, period: int = 14) -> float:
        """Calculates Average True Range."""
        if len(self.bars) < period + 1:
            return 0.0

        highs = self.get_highs()
        lows = self.get_lows()
        closes = self.get_closes()

        tr_list = []
        for i in range(1, len(closes)):
            h = highs[i]
            l = lows[i]
            c_prev = closes[i - 1]
            tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
            tr_list.append(tr)

        if len(tr_list) < period:
            return 0.0
        # Exponential smoothing of TR
        atr = float(np.mean(tr_list[:period]))
        multiplier = 1.0 / period
        for tr in tr_list[period:]:
            atr = (tr - atr) * multiplier + atr
        return atr

    def calculate_vwap(self, window_bars: int = 60) -> float:
        """Calculates rolling Volume Weighted Average Price."""
        if not self.bars:
            return 0.0

        recent_bars = list(self.bars)[-window_bars:]
        pv_sum = sum(((b.high + b.low + b.close) / 3.0) * b.volume for b in recent_bars)
        vol_sum = sum(b.volume for b in recent_bars)
        if vol_sum == 0:
            return recent_bars[-1].close
        return pv_sum / vol_sum

    def calculate_volume_sma(self, period: int = 20) -> float:
        """Calculates Simple Moving Average of candle volume."""
        volumes = self.get_volumes()
        if len(volumes) < period:
            return float(np.mean(volumes)) if len(volumes) > 0 else 0.0
        return float(np.mean(volumes[-period:]))

    def calculate_adx(self, period: int = 14) -> Tuple[float, float, float]:
        """
        Calculates Welles Wilder's Average Directional Index (ADX), +DI, and -DI.
        Returns: (adx, plus_di, minus_di).
        """
        if len(self.bars) < period * 2:
            return 0.0, 0.0, 0.0

        highs = self.get_highs()
        lows = self.get_lows()
        closes = self.get_closes()

        tr_list: List[float] = []
        plus_dm_list: List[float] = []
        minus_dm_list: List[float] = []

        for i in range(1, len(closes)):
            h, l, c_prev = highs[i], lows[i], closes[i - 1]
            tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
            tr_list.append(tr)

            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]

            if up_move > down_move and up_move > 0:
                plus_dm_list.append(up_move)
            else:
                plus_dm_list.append(0.0)

            if down_move > up_move and down_move > 0:
                minus_dm_list.append(down_move)
            else:
                minus_dm_list.append(0.0)

        if len(tr_list) < period:
            return 0.0, 0.0, 0.0

        # Wilder smoothing initialization
        tr_s = [sum(tr_list[:period])]
        p_dm_s = [sum(plus_dm_list[:period])]
        m_dm_s = [sum(minus_dm_list[:period])]

        for i in range(period, len(tr_list)):
            tr_s.append(tr_s[-1] - (tr_s[-1] / period) + tr_list[i])
            p_dm_s.append(p_dm_s[-1] - (p_dm_s[-1] / period) + plus_dm_list[i])
            m_dm_s.append(m_dm_s[-1] - (m_dm_s[-1] / period) + minus_dm_list[i])

        dx_list: List[float] = []
        plus_di_list: List[float] = []
        minus_di_list: List[float] = []

        for tr, pdm, mdm in zip(tr_s, p_dm_s, m_dm_s):
            if tr <= 0:
                continue
            p_di = 100.0 * (pdm / tr)
            m_di = 100.0 * (mdm / tr)
            di_sum = p_di + m_di
            dx = 100.0 * abs(p_di - m_di) / di_sum if di_sum > 0 else 0.0
            dx_list.append(dx)
            plus_di_list.append(p_di)
            minus_di_list.append(m_di)

        if not dx_list:
            return 0.0, 0.0, 0.0

        if len(dx_list) < period:
            return dx_list[-1], plus_di_list[-1], minus_di_list[-1]

        adx = sum(dx_list[:period]) / period
        for dx in dx_list[period:]:
            adx = (adx * (period - 1) + dx) / period

        return adx, plus_di_list[-1], minus_di_list[-1]

    def calculate_rsi(self, period: int = 14) -> float:
        """Calculates Welles Wilder's Relative Strength Index (RSI)."""
        closes = self.get_closes()
        if len(closes) < period + 1:
            return 50.0

        deltas = np.diff(closes)
        gains = np.maximum(deltas, 0.0)
        losses = np.maximum(-deltas, 0.0)

        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))

        for g, l in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain * (period - 1) + float(g)) / period
            avg_loss = (avg_loss * (period - 1) + float(l)) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return float(100.0 - (100.0 / (1.0 + rs)))

    def calculate_linreg(self, period: int = 8) -> Tuple[float, float, float]:
        """
        Computes rolling linear regression on close prices.
        Returns: (slope, r2_score, next_bar_forecast).
        """
        closes = self.get_closes()
        if len(closes) < period:
            c = float(closes[-1]) if len(closes) > 0 else 0.0
            return 0.0, 0.0, c

        y = closes[-period:]
        x = np.arange(period)
        cov = np.cov(x, y)[0, 1]
        var_x = np.var(x)
        slope = float(cov / var_x) if var_x > 0 else 0.0
        intercept = float(np.mean(y) - slope * np.mean(x))
        y_pred = slope * x + intercept

        ss_tot = float(np.sum((y - np.mean(y)) ** 2))
        ss_res = float(np.sum((y - y_pred) ** 2))
        r2 = float(1.0 - (ss_res / ss_tot)) if ss_tot > 0 else 0.0
        next_forecast = float(slope * period + intercept)

        return slope, r2, next_forecast

    def calculate_bollinger(self, period: int = 20, num_std: float = 2.0) -> Tuple[float, float, float, float]:
        """
        Computes Bollinger Bands and %B position.
        Returns: (upper_band, middle_sma, lower_band, pct_b).
        """
        closes = self.get_closes()
        if len(closes) < period:
            c = float(closes[-1]) if len(closes) > 0 else 0.0
            return c, c, c, 0.5

        recent = closes[-period:]
        sma = float(np.mean(recent))
        std = float(np.std(recent))
        upper = sma + num_std * std
        lower = sma - num_std * std
        current = float(closes[-1])
        bandwidth = upper - lower
        pct_b = float((current - lower) / bandwidth) if bandwidth > 0 else 0.5

        return upper, sma, lower, pct_b



class StrategyEngine:
    """
    Main quantitative intelligence engine.
    Computes multi-timeframe features, determines regime, and produces signals.
    """

    def __init__(self, config: AppConfig, learner: Optional[Any] = None):
        self.config = config
        self.strat_cfg = config.strategy
        self.exec_cfg = config.execution
        self.risk_cfg = config.risk
        self.learner = learner  # OnlineStrategyLearner

        # Multi-timeframe structures: symbol -> timeframe -> TimeframeSeries
        self.series: Dict[str, Dict[str, TimeframeSeries]] = {}
        for s in self.strat_cfg.symbols:
            self.series[s] = {
                tf: TimeframeSeries(s, tf) for tf in self.strat_cfg.timeframes
            }

        # Rolling win/loss memory for Kelly Criterion calculation
        self.rolling_trades: Deque[float] = collections.deque(maxlen=50)

    def record_trade_result(
        self,
        pnl: float,
        regime: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Records realized trade PnL for dynamic Kelly fraction tuning and online learning."""
        self.rolling_trades.append(pnl)
        if self.learner and regime:
            try:
                self.learner.record_trade_result(regime, pnl, metadata)
            except Exception as e:
                logger.debug(f"Online learner record error: {e}")

    def calculate_kelly_fraction(self) -> float:
        """
        Computes dynamic Half-Kelly capital fraction:
        Kelly = W - ((1 - W) / R)
        Clamped strictly between min_position_equity_pct and max_position_equity_pct.
        """
        min_pct = self.risk_cfg.min_position_equity_pct / 100.0
        max_pct = self.risk_cfg.max_position_equity_pct / 100.0

        if len(self.rolling_trades) < 10:
            # Default to mid-point during initial cold start
            return (min_pct + max_pct) / 2.0

        wins = [p for p in self.rolling_trades if p > 0]
        losses = [abs(p) for p in self.rolling_trades if p < 0]

        if not wins:
            return min_pct
        if not losses:
            return max_pct

        win_rate = len(wins) / len(self.rolling_trades)
        avg_win = float(np.mean(wins))
        avg_loss = float(np.mean(losses))

        if avg_loss == 0:
            return max_pct

        win_loss_ratio = avg_win / avg_loss
        kelly = win_rate - ((1.0 - win_rate) / win_loss_ratio)

        # Apply Half-Kelly scale for risk aversion
        half_kelly = kelly * self.strat_cfg.kelly_scale
        return max(min_pct, min(max_pct, half_kelly))

    def update_kline(self, symbol: str, interval: str, kline_raw: Dict[str, Any]) -> None:
        """Ingests live kline update into the series."""
        if symbol not in self.series or interval not in self.series[symbol]:
            return

        bar = Bar(
            timestamp=int(kline_raw.get("start", 0)),
            open=float(kline_raw.get("open", 0)),
            high=float(kline_raw.get("high", 0)),
            low=float(kline_raw.get("low", 0)),
            close=float(kline_raw.get("close", 0)),
            volume=float(kline_raw.get("volume", 0)),
            turnover=float(kline_raw.get("turnover", 0)),
        )
        self.series[symbol][interval].add_or_update_bar(bar)

    def classify_regime(
        self,
        symbol: str,
        orderbook: Optional[OrderBookL2] = None,
    ) -> Tuple[MarketRegime, Dict[str, float]]:
        """
        Classifies market regime into Bull Trend, Bear Trend, Trend Pullback,
        Mean Reversion Range, Volatility Expansion, or Low Vol Chop.
        Enforces multi-horizon linear regression forecasting, ADX trend strength,
        Wilder RSI, and Bollinger Band structure.
        """
        tf_1m = self.series[symbol].get("1m")
        tf_5m = self.series[symbol].get("5m")
        tf_15m = self.series[symbol].get("15m")

        if not tf_1m or len(tf_1m.bars) < self.strat_cfg.ema_slow:
            return MarketRegime.LOW_VOL_CHOP, {}

        # 1m technical indicators
        ema9 = tf_1m.calculate_ema(self.strat_cfg.ema_fast)
        ema21 = tf_1m.calculate_ema(self.strat_cfg.ema_mid)
        ema50 = tf_1m.calculate_ema(self.strat_cfg.ema_slow)
        atr = tf_1m.calculate_atr(self.strat_cfg.atr_period)
        vwap = tf_1m.calculate_vwap(self.strat_cfg.vwap_window_mins)
        current_close = tf_1m.bars[-1].close
        vol_sma = tf_1m.calculate_volume_sma(20)
        current_vol = tf_1m.bars[-1].volume if tf_1m.bars else 0.0

        # ADX trend strength calculation (Wilder ADX)
        adx, plus_di, minus_di = tf_1m.calculate_adx(self.strat_cfg.atr_period)

        # Advanced forecasting: RSI, Linear Regression slope, and Bollinger Bands
        rsi_1m = tf_1m.calculate_rsi(14)
        slope_1m, r2_1m, next_px_1m = tf_1m.calculate_linreg(8)
        upper_bb, mid_bb, lower_bb, pct_b = tf_1m.calculate_bollinger(20)

        # 5m and 15m trend confirmation
        ema9_5m = tf_5m.calculate_ema(self.strat_cfg.ema_fast) if (tf_5m and len(tf_5m.bars) > 0) else ema9
        ema21_5m = tf_5m.calculate_ema(self.strat_cfg.ema_mid) if (tf_5m and len(tf_5m.bars) > 0) else ema21
        atr_5m = tf_5m.calculate_atr(14) if (tf_5m and len(tf_5m.bars) >= 14) else 0.0
        vwap_5m = tf_5m.calculate_vwap(30) if (tf_5m and len(tf_5m.bars) > 0) else vwap
        slope_5m, r2_5m, _ = (
            tf_5m.calculate_linreg(8) if (tf_5m and len(tf_5m.bars) >= 8) else (slope_1m, r2_1m, 0.0)
        )

        ema50_15m = tf_15m.calculate_ema(self.strat_cfg.ema_slow) if (tf_15m and len(tf_15m.bars) > 0) else (
            tf_5m.calculate_ema(self.strat_cfg.ema_slow) if (tf_5m and len(tf_5m.bars) > 0) else ema50
        )

        # OFI from OrderBook L2
        ofi = orderbook.ofi if orderbook else 0.0

        # Calculate historical ATR mean for expansion detection
        recent_bars = list(tf_1m.bars)[-60:]
        tr_list = [
            max(b.high - b.low, abs(b.high - b.open)) for b in recent_bars
        ]
        mean_tr = float(np.mean(tr_list)) if tr_list else atr

        metrics = {
            "close": current_close,
            "ema9": ema9,
            "ema21": ema21,
            "ema50": ema50,
            "ema9_5m": ema9_5m,
            "ema21_5m": ema21_5m,
            "ema50_15m": ema50_15m,
            "atr": atr,
            "atr_5m": atr_5m,
            "vwap": vwap,
            "vwap_5m": vwap_5m,
            "ofi": ofi,
            "mean_tr": mean_tr,
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "vol_sma": vol_sma,
            "current_vol": current_vol,
            "rsi": rsi_1m,
            "slope_1m": slope_1m,
            "r2_1m": r2_1m,
            "slope_5m": slope_5m,
            "r2_5m": r2_5m,
            "pct_b": pct_b,
        }

        # 1. Volatility Expansion Check (High Conviction Breakout)
        is_atr_expansion = atr > (1.6 * mean_tr) and mean_tr > 0
        volume_ok = True
        if self.exec_cfg.volume_confirmation and vol_sma > 0:
            volume_ok = (current_vol >= vol_sma * self.exec_cfg.volume_multiplier)

        if is_atr_expansion and volume_ok and abs(ofi) > 0:
            return MarketRegime.VOLATILITY_EXPANSION, metrics

        # 2. High-Probability Trend Pullback (Buy Dips in Macro Uptrend, Sell Rallies in Macro Downtrend)
        is_macro_bull = ema9_5m > ema21_5m and current_close >= ema50_15m and current_close > vwap_5m and slope_5m > 0
        is_macro_bear = ema9_5m < ema21_5m and current_close <= ema50_15m and current_close < vwap_5m and slope_5m < 0

        last_bar = tf_1m.bars[-1]
        if is_macro_bull and last_bar.low <= ema21 and 38 <= rsi_1m <= 60 and ofi >= 0:
            metrics["pullback_side"] = 1.0  # Buy
            return MarketRegime.TREND_PULLBACK, metrics

        if is_macro_bear and last_bar.high >= ema21 and 40 <= rsi_1m <= 62 and ofi <= 0:
            metrics["pullback_side"] = -1.0  # Sell
            return MarketRegime.TREND_PULLBACK, metrics

        # 3. Macro & Micro Momentum Alignment (BULL_TREND / BEAR_TREND)
        is_bull_ribbon_1m = ema9 > ema21 > ema50
        is_bull_ribbon_5m = ema9_5m >= ema21_5m
        is_above_vwap = current_close > vwap
        is_bull_di = (plus_di >= minus_di) if adx > 0 else True

        if is_bull_ribbon_1m and is_bull_ribbon_5m and is_macro_bull and is_above_vwap and is_bull_di and ofi >= 0:
            return MarketRegime.BULL_TREND, metrics

        is_bear_ribbon_1m = ema9 < ema21 < ema50
        is_bear_ribbon_5m = ema9_5m <= ema21_5m
        is_below_vwap = current_close < vwap
        is_bear_di = (minus_di >= plus_di) if adx > 0 else True

        if is_bear_ribbon_1m and is_bear_ribbon_5m and is_macro_bear and is_below_vwap and is_bear_di and ofi <= 0:
            return MarketRegime.BEAR_TREND, metrics

        # 4. Mean-Reversion Scalp in Range / Low ADX Chop (ADX < 24)
        if adx < 24 or abs(slope_5m) < 0.00005:
            if pct_b <= 0.15 and rsi_1m <= 38 and ofi >= 0:
                metrics["range_side"] = 1.0  # Buy
                return MarketRegime.MEAN_REVERSION_RANGE, metrics
            elif pct_b >= 0.85 and rsi_1m >= 62 and ofi <= 0:
                metrics["range_side"] = -1.0  # Sell
                return MarketRegime.MEAN_REVERSION_RANGE, metrics

        # 5. Otherwise: Low-Volatility Chop (Suppress trend strategies)
        return MarketRegime.LOW_VOL_CHOP, metrics

    def generate_signal(
        self,
        symbol: str,
        orderbook: OrderBookL2,
        funding_rate: float = 0.0,
    ) -> Optional[Signal]:
        """
        Synthesizes technical features and orderbook micro-structure into actionable signals.
        Enforces Maker-First post-only execution with native fee-aware bracket pricing.
        """
        best_bid, _ = orderbook.best_bid
        best_ask, _ = orderbook.best_ask
        mid = orderbook.mid_price

        if best_bid <= 0 or best_ask <= 0 or mid <= 0:
            return None

        # Check spread sanity: avoid trading if spread is too wide
        spread_bps = (orderbook.spread / mid) * 10000.0
        if spread_bps > 15.0:
            logger.debug(f"[{symbol}] Spread too wide ({spread_bps:.1f} bps). Skipping.")
            return None

        regime, metrics = self.classify_regime(symbol, orderbook)

        # Suppress trend execution in Chop regime
        if regime == MarketRegime.LOW_VOL_CHOP:
            return None

        atr = metrics.get("atr", 0.0)
        atr_5m = metrics.get("atr_5m", 0.0)
        if atr <= 0:
            return None

        tp_atr_mult = self.exec_cfg.bracket_tp_atr_mult
        breakout_tp = getattr(self.exec_cfg, "breakout_tp_atr_mult", 1.40)
        sl_atr_mult = self.exec_cfg.bracket_sl_atr_mult

        # Adverse Funding Rate Filter helper
        def is_funding_adverse(trade_side: str) -> bool:
            if not self.exec_cfg.funding_rate_filter:
                return False
            max_fr = self.exec_cfg.max_adverse_funding_rate
            if trade_side == "Buy" and funding_rate > max_fr:
                logger.info(
                    f"[{symbol}] Suppressing LONG entry: funding rate {funding_rate*100:.4f}% > "
                    f"adverse threshold {max_fr*100:.4f}%"
                )
                return True
            elif trade_side == "Sell" and funding_rate < -max_fr:
                logger.info(
                    f"[{symbol}] Suppressing SHORT entry: funding rate {funding_rate*100:.4f}% < "
                    f"-{max_fr*100:.4f}%"
                )
                return True
            return False

        # Helper to compute fee-aware bracket distances
        def get_bracket_distances(price: float, is_5m_wave: bool = True) -> Tuple[float, float, float]:
            base_atr = atr_5m if (is_5m_wave and atr_5m > 0) else atr
            # Enforce minimum profit target of 35 bps to solidly clear all exchange fees
            min_tp_dist = price * 0.0035
            tp_dist = max(tp_atr_mult * base_atr, min_tp_dist)
            sl_dist = max(sl_atr_mult * base_atr, price * 0.0025)
            tp1_bps = getattr(self.exec_cfg, "tp1_bps", 28.0)
            tp1_dist = price * (tp1_bps / 10000.0)
            return tp_dist, sl_dist, tp1_dist

        # Retrieve dynamic online learner weights if active
        def get_conviction(base: float, regime_key: str) -> float:
            if not self.learner:
                return base
            w = self.learner.get_weight(regime_key)
            return min(0.98, max(0.50, round(base * w, 2)))

        # 1. High-Conviction Trend Pullback (Dip Buying / Rally Selling)
        if regime == MarketRegime.TREND_PULLBACK:
            pullback_side = metrics.get("pullback_side", 1.0)
            if pullback_side > 0:
                side = "Buy"
                if is_funding_adverse(side):
                    return None
                price = best_bid
                tp_dist, sl_dist, tp1_dist = get_bracket_distances(price, is_5m_wave=True)
                tp_price = round(price + tp_dist, 4)
                sl_price = round(price - sl_dist, 4)
                tp1_price = round(price + tp1_dist, 4)
                conviction = get_conviction(0.85, "trend_pullback")

                return Signal(
                    symbol=symbol,
                    side=side,
                    regime=regime,
                    conviction=conviction,
                    price=price,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    atr=atr,
                    order_type="Limit",
                    time_in_force="PostOnly" if self.exec_cfg.maker_first else "GTC",
                    leverage=self.risk_cfg.default_leverage,
                    metadata=metrics,
                    tp1_price=tp1_price,
                    tp2_price=tp_price,
                )
            else:
                side = "Sell"
                if is_funding_adverse(side):
                    return None
                price = best_ask
                tp_dist, sl_dist, tp1_dist = get_bracket_distances(price, is_5m_wave=True)
                tp_price = round(price - tp_dist, 4)
                sl_price = round(price + sl_dist, 4)
                tp1_price = round(price - tp1_dist, 4)
                conviction = get_conviction(0.85, "trend_pullback")

                return Signal(
                    symbol=symbol,
                    side=side,
                    regime=regime,
                    conviction=conviction,
                    price=price,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    atr=atr,
                    order_type="Limit",
                    time_in_force="PostOnly" if self.exec_cfg.maker_first else "GTC",
                    leverage=self.risk_cfg.default_leverage,
                    metadata=metrics,
                    tp1_price=tp1_price,
                    tp2_price=tp_price,
                )

        # 2. Mean-Reversion Scalping in Range/Chop
        if regime == MarketRegime.MEAN_REVERSION_RANGE:
            range_side = metrics.get("range_side", 1.0)
            if range_side > 0:
                side = "Buy"
                if is_funding_adverse(side):
                    return None
                price = best_bid
                tp_dist = max(1.10 * atr, price * 0.0030)
                sl_dist = max(0.85 * atr, price * 0.0022)
                tp_price = round(price + tp_dist, 4)
                sl_price = round(price - sl_dist, 4)
                conviction = get_conviction(0.75, "mean_reversion")

                return Signal(
                    symbol=symbol,
                    side=side,
                    regime=regime,
                    conviction=conviction,
                    price=price,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    atr=atr,
                    order_type="Limit",
                    time_in_force="PostOnly" if self.exec_cfg.maker_first else "GTC",
                    leverage=self.risk_cfg.default_leverage,
                    metadata=metrics,
                )
            else:
                side = "Sell"
                if is_funding_adverse(side):
                    return None
                price = best_ask
                tp_dist = max(1.10 * atr, price * 0.0030)
                sl_dist = max(0.85 * atr, price * 0.0022)
                tp_price = round(price - tp_dist, 4)
                sl_price = round(price + sl_dist, 4)
                conviction = get_conviction(0.75, "mean_reversion")

                return Signal(
                    symbol=symbol,
                    side=side,
                    regime=regime,
                    conviction=conviction,
                    price=price,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    atr=atr,
                    order_type="Limit",
                    time_in_force="PostOnly" if self.exec_cfg.maker_first else "GTC",
                    leverage=self.risk_cfg.default_leverage,
                    metadata=metrics,
                )

        # 3. High-Conviction Breakout Momentum (Volatility Expansion)
        if regime == MarketRegime.VOLATILITY_EXPANSION:
            # Direction determined by VWAP and OFI
            if metrics["close"] > metrics["vwap"] and orderbook.ofi > 0:
                side = "Buy"
                if is_funding_adverse(side):
                    return None
                price = best_ask
                base_atr = atr_5m if atr_5m > 0 else atr
                tp_dist = max(breakout_tp * base_atr, price * 0.0040)
                sl_dist = max(sl_atr_mult * base_atr, price * 0.0028)
                tp_price = round(price + tp_dist, 4)
                sl_price = round(price - sl_dist, 4)
                conviction = get_conviction(0.80, "volatility_expansion")

                return Signal(
                    symbol=symbol,
                    side=side,
                    regime=regime,
                    conviction=conviction,
                    price=price,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    atr=atr,
                    order_type="Limit",
                    time_in_force="IOC",
                    leverage=self.risk_cfg.default_leverage,
                    metadata=metrics,
                )
            elif metrics["close"] < metrics["vwap"] and orderbook.ofi < 0:
                side = "Sell"
                if is_funding_adverse(side):
                    return None
                price = best_bid
                base_atr = atr_5m if atr_5m > 0 else atr
                tp_dist = max(breakout_tp * base_atr, price * 0.0040)
                sl_dist = max(sl_atr_mult * base_atr, price * 0.0028)
                tp_price = round(price - tp_dist, 4)
                sl_price = round(price + sl_dist, 4)
                conviction = get_conviction(0.80, "volatility_expansion")

                return Signal(
                    symbol=symbol,
                    side=side,
                    regime=regime,
                    conviction=conviction,
                    price=price,
                    tp_price=tp_price,
                    sl_price=sl_price,
                    atr=atr,
                    order_type="Limit",
                    time_in_force="IOC",
                    leverage=self.risk_cfg.default_leverage,
                    metadata=metrics,
                )
            return None

        # 4. Maker-First Trend Scalping (BULL_TREND / BEAR_TREND)
        if regime == MarketRegime.BULL_TREND:
            side = "Buy"
            if is_funding_adverse(side):
                return None
            price = best_bid
            tp_dist, sl_dist, tp1_dist = get_bracket_distances(price, is_5m_wave=True)
            tp_price = round(price + tp_dist, 4)
            sl_price = round(price - sl_dist, 4)
            tp1_price = round(price + tp1_dist, 4)
            conviction = get_conviction(0.78, "trend_momentum")

            return Signal(
                symbol=symbol,
                side=side,
                regime=regime,
                conviction=conviction,
                price=price,
                tp_price=tp_price,
                sl_price=sl_price,
                atr=atr,
                order_type="Limit",
                time_in_force="PostOnly" if self.exec_cfg.maker_first else "GTC",
                leverage=self.risk_cfg.default_leverage,
                metadata=metrics,
                tp1_price=tp1_price,
                tp2_price=tp_price,
            )

        elif regime == MarketRegime.BEAR_TREND:
            side = "Sell"
            if is_funding_adverse(side):
                return None
            price = best_ask
            tp_dist, sl_dist, tp1_dist = get_bracket_distances(price, is_5m_wave=True)
            tp_price = round(price - tp_dist, 4)
            sl_price = round(price + sl_dist, 4)
            tp1_price = round(price - tp1_dist, 4)
            conviction = get_conviction(0.78, "trend_momentum")

            return Signal(
                symbol=symbol,
                side=side,
                regime=regime,
                conviction=conviction,
                price=price,
                tp_price=tp_price,
                sl_price=sl_price,
                atr=atr,
                order_type="Limit",
                time_in_force="PostOnly" if self.exec_cfg.maker_first else "GTC",
                leverage=self.risk_cfg.default_leverage,
                metadata=metrics,
                tp1_price=tp1_price,
                tp2_price=tp_price,
            )

        return None

