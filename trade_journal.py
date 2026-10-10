"""
Trade Journal & Self-Adaptive Learning Engine.
Maintains an SQLite database of all trade executions, feature vectors at entry,
holding times, slippage, and fee impact.
Evaluates realized performance every N trades and dynamically auto-tunes parameters.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from config import AppConfig

logger = logging.getLogger("trade_journal")


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    side: str
    entry_time: int  # epoch ms
    exit_time: int   # epoch ms
    holding_time_sec: float
    entry_price: float
    exit_price: float
    qty: float
    realized_pnl: float
    realized_pnl_pct: float
    fee_paid: float
    slippage_bps: float
    order_type: str
    regime: str
    conviction: float
    atr_at_entry: float
    ema_spread_at_entry: float
    vwap_delta_at_entry: float
    ofi_at_entry: float


class TradeJournal:
    """SQLite-backed trade memory and self-adaptive parameter optimizer."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._learner: Optional[OnlineStrategyLearner] = None

    @property
    def learner(self) -> OnlineStrategyLearner:
        """Returns the online strategy learner instance for continuous reinforcement learning."""
        if self._learner is None:
            self._learner = OnlineStrategyLearner(self)
        return self._learner

    def _get_connection(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10.0)

    def _init_db(self) -> None:
        """Creates tables if they do not exist."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    entry_time INTEGER NOT NULL,
                    exit_time INTEGER NOT NULL,
                    holding_time_sec REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    exit_price REAL NOT NULL,
                    qty REAL NOT NULL,
                    realized_pnl REAL NOT NULL,
                    realized_pnl_pct REAL NOT NULL,
                    fee_paid REAL NOT NULL,
                    slippage_bps REAL NOT NULL,
                    order_type TEXT NOT NULL,
                    regime TEXT NOT NULL,
                    conviction REAL NOT NULL,
                    atr_at_entry REAL NOT NULL,
                    ema_spread_at_entry REAL NOT NULL,
                    vwap_delta_at_entry REAL NOT NULL,
                    ofi_at_entry REAL NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS parameter_tuning_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp INTEGER NOT NULL,
                    trades_analyzed INTEGER NOT NULL,
                    sharpe_ratio REAL NOT NULL,
                    win_rate REAL NOT NULL,
                    profit_factor REAL NOT NULL,
                    prev_tp_atr_mult REAL NOT NULL,
                    new_tp_atr_mult REAL NOT NULL,
                    prev_sl_atr_mult REAL NOT NULL,
                    new_sl_atr_mult REAL NOT NULL,
                    prev_kelly_scale REAL NOT NULL,
                    new_kelly_scale REAL NOT NULL,
                    rationale TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS strategy_learning_memory (
                    feature_key TEXT PRIMARY KEY,
                    weight REAL NOT NULL,
                    wins INTEGER NOT NULL DEFAULT 0,
                    losses INTEGER NOT NULL DEFAULT 0,
                    total_pnl REAL NOT NULL DEFAULT 0.0,
                    last_updated INTEGER NOT NULL
                )
                """
            )
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_exit_time ON trades(exit_time)")
            conn.commit()

    def record_trade(self, trade: TradeRecord) -> None:
        """Inserts a completed trade record into the database."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO trades VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    trade.trade_id,
                    trade.symbol,
                    trade.side,
                    trade.entry_time,
                    trade.exit_time,
                    trade.holding_time_sec,
                    trade.entry_price,
                    trade.exit_price,
                    trade.qty,
                    trade.realized_pnl,
                    trade.realized_pnl_pct,
                    trade.fee_paid,
                    trade.slippage_bps,
                    trade.order_type,
                    trade.regime,
                    trade.conviction,
                    trade.atr_at_entry,
                    trade.ema_spread_at_entry,
                    trade.vwap_delta_at_entry,
                    trade.ofi_at_entry,
                ),
            )
            conn.commit()
            logger.info(
                f"Logged trade {trade.trade_id} on {trade.symbol}: PnL=${trade.realized_pnl:.2f} "
                f"({trade.realized_pnl_pct:.2f}%) holding {trade.holding_time_sec:.1f}s"
            )

    def get_recent_trades(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Returns the most recent trades in chronological order."""
        with self._get_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM trades ORDER BY exit_time DESC LIMIT ?", (limit,)
            )
            rows = cursor.fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_total_trade_count(self) -> int:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM trades")
            return int(cursor.fetchone()[0])

    def get_summary_metrics(self) -> Dict[str, Any]:
        """Calculates all-time summary statistics for dashboard and reporting."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT realized_pnl, realized_pnl_pct FROM trades")
            rows = cursor.fetchall()

            if not rows:
                return {
                    "total_trades": 0,
                    "win_rate": 0.0,
                    "profit_factor": 0.0,
                    "total_pnl": 0.0,
                    "avg_trade_pnl": 0.0,
                }

            pnls = [r[0] for r in rows]
            wins = [p for p in pnls if p > 0]
            losses = [abs(p) for p in pnls if p < 0]

            total_trades = len(pnls)
            win_rate = (len(wins) / total_trades) * 100.0 if total_trades > 0 else 0.0
            sum_gains = sum(wins)
            sum_losses = sum(losses)
            profit_factor = (sum_gains / sum_losses) if sum_losses > 0 else (99.0 if sum_gains > 0 else 0.0)

            return {
                "total_trades": total_trades,
                "win_rate": round(win_rate, 2),
                "profit_factor": round(profit_factor, 2),
                "total_pnl": round(sum(pnls), 2),
                "avg_trade_pnl": round(float(np.mean(pnls)), 2),
            }

    def get_window_metrics(self, hours: int = 24) -> Dict[str, Any]:
        """Calculates performance statistics for a recent time window (e.g. past 24 hours)."""
        cutoff = int(time.time()) - (hours * 3600)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT realized_pnl, realized_pnl_pct, fee_paid FROM trades WHERE exit_time >= ?",
                (cutoff,)
            )
            rows = cursor.fetchall()
            if not rows:
                return {
                    "total_trades": 0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0.0,
                    "total_pnl": 0.0,
                    "fees_paid": 0.0,
                    "profit_factor": 0.0,
                }
            pnls = [r[0] for r in rows]
            fees = sum(r[2] for r in rows)
            wins = [p for p in pnls if p > 0]
            losses = [abs(p) for p in pnls if p < 0]
            total_trades = len(pnls)
            win_rate = (len(wins) / total_trades) * 100.0 if total_trades > 0 else 0.0
            sum_gains = sum(wins)
            sum_losses = sum(losses)
            profit_factor = (sum_gains / sum_losses) if sum_losses > 0 else (99.0 if sum_gains > 0 else 0.0)
            return {
                "total_trades": total_trades,
                "wins": len(wins),
                "losses": len(losses),
                "win_rate": round(win_rate, 2),
                "total_pnl": round(sum(pnls), 2),
                "fees_paid": round(fees, 2),
                "profit_factor": round(profit_factor, 2),
            }


class ParameterAutoTuner:
    """
    Self-Adaptive Tuning Engine.
    Periodically inspects rolling trade performance and tunes ATR multiplier brackets
    and Kelly sizing factor dynamically.
    """

    def __init__(self, journal: TradeJournal, config: AppConfig):
        self.journal = journal
        self.config = config

    def evaluate_and_tune(self) -> Optional[Dict[str, Any]]:
        """
        Runs adaptive parameter tuning based on the last N closed trades.
        Safe bounded adjustments:
        - TP ATR mult: [0.8, 2.5]
        - SL ATR mult: [0.5, 1.5]
        - Kelly scale: [0.2, 0.8]
        """
        interval = self.config.strategy.auto_tuning_trade_interval
        recent = self.journal.get_recent_trades(limit=interval)
        if len(recent) < interval:
            return None

        pnls = [t["realized_pnl"] for t in recent]
        wins = [p for p in pnls if p > 0]
        losses = [abs(p) for p in pnls if p < 0]

        win_rate = len(wins) / len(pnls)
        sum_gains = sum(wins)
        sum_losses = sum(losses)
        profit_factor = (sum_gains / sum_losses) if sum_losses > 0 else 2.0

        # Approximate trade Sharpe ratio
        pnl_std = float(np.std(pnls))
        pnl_mean = float(np.mean(pnls))
        sharpe = (pnl_mean / pnl_std * np.sqrt(365)) if pnl_std > 0 else 0.0

        prev_tp = self.config.execution.bracket_tp_atr_mult
        prev_sl = self.config.execution.bracket_sl_atr_mult
        prev_kelly = self.config.strategy.kelly_scale

        new_tp = prev_tp
        new_sl = prev_sl
        new_kelly = prev_kelly
        rationale_parts = []

        # High-performing condition: expand TP target, allow slightly higher Kelly
        if win_rate >= 0.60 and profit_factor >= 1.6:
            new_tp = min(2.5, round(prev_tp * 1.10, 2))
            new_kelly = min(0.8, round(prev_kelly * 1.05, 2))
            rationale_parts.append(
                f"Strong performance (WinRate={win_rate*100:.1f}%, PF={profit_factor:.2f}). "
                f"Expanded TP ATR multiplier to {new_tp} and Kelly scale to {new_kelly}."
            )

        # Underperforming condition: tighten SL to minimize drawdowns, reduce Kelly
        elif win_rate < 0.45 or profit_factor < 1.0:
            new_sl = max(0.5, round(prev_sl * 0.90, 2))
            new_kelly = max(0.2, round(prev_kelly * 0.85, 2))
            rationale_parts.append(
                f"Underperformance detected (WinRate={win_rate*100:.1f}%, PF={profit_factor:.2f}). "
                f"Tightened SL ATR multiplier to {new_sl} and lowered Kelly scale to {new_kelly}."
            )
        else:
            rationale_parts.append("Metrics within normal equilibrium. No threshold modification needed.")

        rationale = " ".join(rationale_parts)

        # Apply to in-memory config
        self.config.execution.bracket_tp_atr_mult = new_tp
        self.config.execution.bracket_sl_atr_mult = new_sl
        self.config.strategy.kelly_scale = new_kelly

        # Log into database
        with self.journal._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO parameter_tuning_log (
                    timestamp, trades_analyzed, sharpe_ratio, win_rate,
                    profit_factor, prev_tp_atr_mult, new_tp_atr_mult,
                    prev_sl_atr_mult, new_sl_atr_mult, prev_kelly_scale,
                    new_kelly_scale, rationale
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(time.time() * 1000),
                    len(recent),
                    round(sharpe, 3),
                    round(win_rate * 100, 2),
                    round(profit_factor, 2),
                    prev_tp,
                    new_tp,
                    prev_sl,
                    new_sl,
                    prev_kelly,
                    new_kelly,
                    rationale,
                ),
            )
            conn.commit()

        logger.info(f"⚡ [SELF-ADAPTIVE TUNER] {rationale}")
        return {
            "trades_analyzed": len(recent),
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "sharpe": sharpe,
            "new_tp": new_tp,
            "new_sl": new_sl,
            "new_kelly": new_kelly,
            "rationale": rationale,
        }


class OnlineStrategyLearner:
    """
    Online Self-Adaptive Machine Learning Engine.
    Learns dynamically from every realized trade outcome using online reinforcement
    weight adjustment. Persists learned weights to SQLite so intelligence is preserved
    and evolves over time.
    """

    DEFAULT_WEIGHTS = {
        "trend_pullback": 1.15,
        "trend_momentum": 1.00,
        "mean_reversion": 1.00,
        "volatility_expansion": 0.85,
        "ofi_lead": 1.05,
        "rsi_divergence": 1.00,
        "linreg_trend": 1.05,
    }

    def __init__(self, journal: TradeJournal):
        self.journal = journal
        self.weights: Dict[str, float] = dict(self.DEFAULT_WEIGHTS)
        self.stats: Dict[str, Dict[str, Any]] = {}
        self._load_memory()

    def _load_memory(self) -> None:
        try:
            with self.journal._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT feature_key, weight, wins, losses, total_pnl FROM strategy_learning_memory"
                )
                rows = cursor.fetchall()
                for k, w, wins, losses, tot_pnl in rows:
                    self.weights[k] = float(w)
                    self.stats[k] = {
                        "wins": int(wins),
                        "losses": int(losses),
                        "total_pnl": float(tot_pnl),
                    }
            if rows:
                logger.info(f"🧠 Loaded {len(rows)} learned weights from database memory.")
        except Exception as e:
            logger.debug(f"Failed to load learning memory, using defaults: {e}")

    def record_trade_result(
        self, regime: str, pnl: float, metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Updates regime and feature weights based on realized profit or loss."""
        keys_to_update = []
        if regime:
            reg_key = regime.lower()
            if reg_key in self.weights:
                keys_to_update.append(reg_key)
            elif "pullback" in reg_key:
                keys_to_update.append("trend_pullback")
            elif "momentum" in reg_key or "trend" in reg_key:
                keys_to_update.append("trend_momentum")
            elif "reversion" in reg_key or "chop" in reg_key:
                keys_to_update.append("mean_reversion")
            elif "expansion" in reg_key:
                keys_to_update.append("volatility_expansion")

        if metadata:
            if abs(float(metadata.get("ofi", 0.0))) > 5.0:
                keys_to_update.append("ofi_lead")
            if float(metadata.get("r2", 0.0)) > 0.30:
                keys_to_update.append("linreg_trend")

        now_ms = int(time.time() * 1000)
        learning_rate = 0.05

        with self.journal._get_connection() as conn:
            cursor = conn.cursor()
            for k in set(keys_to_update):
                current_w = self.weights.get(k, 1.0)
                st = self.stats.setdefault(k, {"wins": 0, "losses": 0, "total_pnl": 0.0})

                if pnl > 0:
                    st["wins"] += 1
                    # Reward: increase weight (clamped between 0.35 and 2.20)
                    new_w = min(2.20, round(current_w + learning_rate * min(1.0, max(0.2, pnl / 0.5)), 3))
                else:
                    st["losses"] += 1
                    # Penalize underperforming feature
                    new_w = max(0.35, round(current_w - learning_rate * min(1.0, max(0.2, abs(pnl) / 0.5)), 3))

                st["total_pnl"] += pnl
                self.weights[k] = new_w

                cursor.execute(
                    """
                    INSERT INTO strategy_learning_memory (feature_key, weight, wins, losses, total_pnl, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feature_key) DO UPDATE SET
                        weight=excluded.weight,
                        wins=excluded.wins,
                        losses=excluded.losses,
                        total_pnl=excluded.total_pnl,
                        last_updated=excluded.last_updated
                    """,
                    (k, new_w, st["wins"], st["losses"], round(st["total_pnl"], 4), now_ms),
                )
            conn.commit()

        logger.info(
            f"🧠 [ONLINE LEARNER] Trade result PnL=${pnl:.2f} logged for {regime}. "
            f"Active weights: {self.weights}"
        )

    def get_weight(self, key: str) -> float:
        return self.weights.get(key, 1.0)

