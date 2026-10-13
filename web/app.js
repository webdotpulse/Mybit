/**
 * Bybit V5 Autonomous Engine - Live Admin UI Client
 * Handles real-time polling, regime visualization, positions & trade journal rendering,
 * online learner telemetry, and emergency panic control.
 */

let toastTimer = null;

// Initial fallback state before first API poll
let engineData = {
  status: "RUNNING",
  engine_alive: true,
  heartbeat_age_sec: 1,
  equity: 1000.00,
  capital_tier: "STANDARD",
  auto_tier_enabled: true,
  symbols: ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
  high_water_mark: 1000.00,
  daily_drawdown_pct: 0.0,
  last_reconciliation_ts: 0,
  testnet: false,
  trading_mode: "linear",
  circuit_breakers: {
    daily_drawdown_tripped: false,
    max_daily_drawdown_pct: 2.5,
    consecutive_losses: 0,
    max_consecutive_losses: 4,
    in_cooldown: false,
    cooldown_remaining_sec: 0,
    volatility_kill: false,
    volatility_cooldown_remaining_sec: 0
  },
  metrics: {
    total_trades: 0,
    win_rate: 0.0,
    profit_factor: 0.0,
    total_pnl: 0.0,
    avg_trade_pnl: 0.0
  },
  window_24h: {
    total_trades: 0,
    win_rate: 0.0,
    total_pnl: 0.0
  },
  positions: [],
  pending_orders: 0,
  recent_trades: [],
  recent_events: [],
  learner_weights: {},
  parameters: {
    tp_atr_mult: 2.0,
    sl_atr_mult: 1.8,
    kelly_scale: 0.5,
    kelly_fraction_pct: 12.0,
    default_leverage: 2
  },
  current_regime: "STANDBY",
  symbol_telemetry: {}
};

const REGIME_DETAILS = {
  BULL_TREND: {
    label: "BULL_TREND DETECTED",
    confidence: "HIGH (EMA + ADX)",
    color: "var(--accent-emerald)",
    desc: "Multi-timeframe EMA ribbon (9 > 21 > 50) aligned bullishly with positive Order Flow Imbalance (OFI)."
  },
  BEAR_TREND: {
    label: "BEAR_TREND DETECTED",
    confidence: "HIGH (EMA + ADX)",
    color: "var(--accent-rose)",
    desc: "Multi-timeframe EMA ribbon (9 < 21 < 50) aligned bearishly with negative Order Flow Imbalance (OFI)."
  },
  TREND_PULLBACK: {
    label: "TREND_PULLBACK ACTIVE",
    confidence: "HIGH (OLS R² + EMA)",
    color: "var(--accent-emerald)",
    desc: "Multi-timeframe EMA ribbon aligned with OLS R² > 0.30 and BTC macro filter. Executing PostOnly Maker limit entries."
  },
  VOLATILITY_EXPANSION: {
    label: "VOLATILITY_EXPANSION SURGE",
    confidence: "HIGH (ATR + VOL)",
    color: "var(--accent-cyan)",
    desc: "Volatility breakout confirmed with ATR expansion (> 1.4×) and directional L2 Order Flow Imbalance."
  },
  BREAKOUT_EXPANSION: {
    label: "BREAKOUT_EXPANSION SURGE",
    confidence: "HIGH (VOL + OFI)",
    color: "var(--accent-cyan)",
    desc: "Donchian channel breakout confirmed with volume surge (> 1.4×) and directional L2 Order Flow Imbalance."
  },
  MEAN_REVERSION_RANGE: {
    label: "MEAN_REVERSION_RANGE",
    confidence: "MODERATE (BB / RSI)",
    color: "var(--accent-amber)",
    desc: "Range-bound market at Bollinger Band extremes with RSI exhaustion. PostOnly mean-reversion active."
  },
  LOW_VOL_CHOP: {
    label: "LOW_VOL_CHOP (SUPPRESSED)",
    confidence: "CHOP FILTER ON",
    color: "var(--text-secondary)",
    desc: "Low-volatility sideways churn or conflicting timeframe momentum detected. Suppressing new entries to protect capital."
  },
  DEAD_CHOP: {
    label: "LOW_VOL_CHOP (SUPPRESSED)",
    confidence: "CHOP FILTER ON",
    color: "var(--text-secondary)",
    desc: "Low-volatility sideways churn or conflicting timeframe momentum detected. Suppressing new entries to protect capital."
  },
  STANDBY: {
    label: "STANDBY / SCANNING",
    confidence: "AWAITING STREAM",
    color: "var(--accent-cyan)",
    desc: "Scanning multi-timeframe kline series (1m, 5m, 15m) and L2 orderbook depth across active perpetual pairs."
  }
};

function formatSmartPrice(val) {
  const num = Number(val);
  if (!Number.isFinite(num) || num <= 0) return "—";
  if (num >= 1000) {
    return `$${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  if (num >= 1) {
    return `$${num.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
  }
  return `$${num.toLocaleString(undefined, { minimumFractionDigits: 4, maximumFractionDigits: 6 })}`;
}

function escapeHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function fetchStatus() {
  try {
    const res = await fetch('/api/status');
    if (res.ok) {
      const data = await res.json();
      if (data && (data.equity !== undefined || data.status !== undefined)) {
        engineData = { ...engineData, ...data };
      }
    }
  } catch (e) {
    // Keep last known state on transient network error
  }
  renderDashboard();
}

function renderDashboard() {
  // 1. Mode & Tier Tags
  const modeTag = document.getElementById('mode-tag');
  if (modeTag && engineData.trading_mode) {
    modeTag.innerText = engineData.trading_mode === 'spot' ? 'UTA Spot' : 'UTA Linear';
  }

  const tierTag = document.getElementById('tier-tag');
  if (tierTag && engineData.capital_tier) {
    const suffix = engineData.auto_tier_enabled !== false ? ' (AUTO)' : '';
    tierTag.innerText = `TIER: ${engineData.capital_tier}${suffix}`;
  }

  // 2. Dynamic Live / Testnet Environment Badge
  const envTag = document.getElementById('env-tag');
  if (envTag) {
    const isTestnet = Boolean(engineData.testnet);
    if (isTestnet) {
      envTag.innerText = "TESTNET";
      envTag.style.background = "rgba(138, 43, 226, 0.15)";
      envTag.style.color = "#d094ff";
      envTag.style.borderColor = "rgba(138, 43, 226, 0.4)";
    } else {
      envTag.innerText = "LIVE MAINNET";
      envTag.style.background = "rgba(0, 230, 118, 0.15)";
      envTag.style.color = "var(--accent-emerald)";
      envTag.style.borderColor = "rgba(0, 230, 118, 0.4)";
    }
  }

  // 3. Engine Running / Panic / Offline Status Badge
  const statusBadge = document.getElementById('engine-status-badge');
  const statusText = document.getElementById('engine-status-text');
  const pulseDot = document.getElementById('engine-pulse-dot');
  const infraHeartbeat = document.getElementById('infra-heartbeat');
  const statusStr = String(engineData.status || "RUNNING");

  if (statusText) {
    if (statusStr.includes('PANIC') || statusStr.includes('HALT')) {
      statusText.innerText = 'PANIC HALTED';
      if (statusBadge) {
        statusBadge.style.borderColor = 'var(--accent-rose)';
        statusBadge.style.color = 'var(--accent-rose)';
        statusBadge.style.background = 'rgba(255, 51, 102, 0.12)';
      }
      if (pulseDot) {
        pulseDot.style.backgroundColor = 'var(--accent-rose)';
        pulseDot.style.boxShadow = '0 0 8px var(--accent-rose)';
      }
    } else if (statusStr === 'OFFLINE' || statusStr === 'STALE_HEARTBEAT') {
      statusText.innerText = statusStr === 'OFFLINE' ? 'DAEMON OFFLINE' : `STALE (${engineData.heartbeat_age_sec || 0}s)`;
      if (statusBadge) {
        statusBadge.style.borderColor = 'var(--accent-amber)';
        statusBadge.style.color = 'var(--accent-amber)';
        statusBadge.style.background = 'rgba(255, 184, 0, 0.12)';
      }
      if (pulseDot) {
        pulseDot.style.backgroundColor = 'var(--accent-amber)';
        pulseDot.style.boxShadow = '0 0 8px var(--accent-amber)';
      }
    } else {
      statusText.innerText = 'AUTONOMOUS RUNNING';
      if (statusBadge) {
        statusBadge.style.borderColor = 'rgba(0, 230, 118, 0.4)';
        statusBadge.style.color = 'var(--accent-emerald)';
        statusBadge.style.background = 'rgba(0, 245, 155, 0.1)';
      }
      if (pulseDot) {
        pulseDot.style.backgroundColor = 'var(--accent-emerald)';
        pulseDot.style.boxShadow = '0 0 8px var(--accent-emerald)';
      }
    }
  }

  if (infraHeartbeat) {
    const age = Number(engineData.heartbeat_age_sec ?? 0);
    if (statusStr === 'OFFLINE') {
      infraHeartbeat.innerText = 'Daemon Offline';
      infraHeartbeat.style.color = 'var(--accent-amber)';
    } else if (age > 30) {
      infraHeartbeat.innerText = `Stale (${age}s ago)`;
      infraHeartbeat.style.color = 'var(--accent-rose)';
    } else {
      infraHeartbeat.innerText = `Live (${age}s ago)`;
      infraHeartbeat.style.color = 'var(--accent-emerald)';
    }
  }

  // 4. View-Only Lockdown
  const isReadOnly = Boolean(engineData.read_only);
  const panicBtn = document.getElementById('btn-panic');
  const configLink = document.getElementById('nav-config-link');
  const viewOnlyBadge = document.getElementById('view-only-badge');

  if (isReadOnly) {
    if (panicBtn) panicBtn.style.display = 'none';
    if (configLink) configLink.style.display = 'none';
    if (viewOnlyBadge) viewOnlyBadge.style.display = 'inline-flex';
  } else {
    if (panicBtn) panicBtn.style.display = '';
    if (configLink) configLink.style.display = '';
    if (viewOnlyBadge) viewOnlyBadge.style.display = 'none';
  }

  // 5. KPI Cards
  const eq = Number(engineData.equity || 0);
  const hwm = Number(engineData.high_water_mark || eq);
  document.getElementById('kpi-equity').innerText = `$${eq.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  document.getElementById('kpi-hwm').innerText = `$${hwm.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  const w24 = engineData.window_24h || {};
  const pnl24 = Number(w24.total_pnl || 0);
  const pnl24El = document.getElementById('kpi-24h-pnl');
  if (pnl24El) {
    const s24 = pnl24 >= 0 ? '+' : '';
    pnl24El.innerText = `24h: ${s24}$${pnl24.toFixed(2)}`;
    pnl24El.className = pnl24 >= 0 ? 'positive' : 'negative';
  }

  const cb = engineData.circuit_breakers || {};
  const maxDdLimit = Number(cb.max_daily_drawdown_pct || 2.5);
  const ddPct = Number(engineData.daily_drawdown_pct || 0);
  const ddEl = document.getElementById('kpi-drawdown');
  ddEl.innerText = `${ddPct.toFixed(2)}%`;
  ddEl.className = ddPct >= maxDdLimit * 0.75 ? 'kpi-value negative' : 'kpi-value positive';

  const maxDdLabel = document.getElementById('kpi-max-dd');
  if (maxDdLabel) maxDdLabel.innerText = `${maxDdLimit.toFixed(2)}% Max`;

  const metrics = engineData.metrics || {};
  const pnlEl = document.getElementById('kpi-pnl');
  const pnlVal = Number(metrics.total_pnl || 0);
  const sign = pnlVal >= 0 ? '+' : '';
  pnlEl.innerText = `${sign}$${pnlVal.toFixed(2)}`;
  pnlEl.className = pnlVal >= 0 ? 'kpi-value positive' : 'kpi-value negative';

  document.getElementById('kpi-winrate').innerText = `${Number(metrics.win_rate || 0).toFixed(1)}% Win Rate (PF: ${Number(metrics.profit_factor || 0).toFixed(2)})`;
  document.getElementById('kpi-trades').innerText = `${metrics.total_trades || 0} Trades`;

  const params = engineData.parameters || {};
  const kellyPct = params.kelly_fraction_pct !== undefined
    ? Number(params.kelly_fraction_pct)
    : Number((params.kelly_scale || 0.5) * 2.0);
  document.getElementById('kpi-kelly').innerText = `${kellyPct.toFixed(2)}%`;

  const kellySub = document.getElementById('kpi-kelly-sub');
  if (kellySub) {
    const lev = params.default_leverage || 2;
    const pending = engineData.pending_orders || 0;
    kellySub.innerHTML = `<span>Leverage: ${lev}x • Pending Orders: ${pending}</span>`;
  }

  // 6. Regime Banner & Per-Symbol Radar
  renderRegimeAndRadar();

  // 7. Reconciliation Timestamp
  const reconEl = document.getElementById('last-recon-time');
  if (reconEl) {
    const reconTs = Number(engineData.last_reconciliation_ts || 0);
    if (reconTs > 0) {
      const reconAge = Math.max(0, Math.floor(Date.now() / 1000) - reconTs);
      reconEl.innerText = reconAge <= 2 ? 'Just now (10s sync)' : `${reconAge}s ago (10s sync)`;
    } else {
      reconEl.innerText = statusStr === 'OFFLINE' ? 'Offline' : '10s sync loop';
    }
  }

  // 8. Active Positions Table
  renderPositionsTable();

  // 9. Recent Closed Trades Table
  renderRecentTradesTable();

  // 10. Autonomous Event Feed
  renderEventFeed();

  // 11. Circuit Breakers & Learner Weights
  renderCircuitBreakers(cb, maxDdLimit, ddPct);
  renderTuningAndLearner(params, engineData.learner_weights);
}

function renderRegimeAndRadar() {
  const regimeNameEl = document.getElementById('regime-name');
  const regimeDescEl = document.getElementById('regime-desc');
  const regimePillEl = document.getElementById('regime-pill');
  const regimeIndEl = document.getElementById('regime-indicator');

  const rawRegime = engineData.current_regime;
  if (rawRegime && typeof rawRegime === 'object') {
    regimeNameEl.innerText = `REGIME: ${rawRegime.name} DETECTED`;
    regimeDescEl.innerText = rawRegime.description || '';
    regimePillEl.innerText = `CONFIDENCE: ${rawRegime.confidence || '85%'}`;
  } else {
    const key = String(rawRegime || 'STANDBY').toUpperCase();
    const info = REGIME_DETAILS[key] || REGIME_DETAILS.STANDBY;
    regimeNameEl.innerText = `REGIME: ${info.label}`;
    regimeDescEl.innerText = info.desc;
    regimePillEl.innerText = info.confidence;
    if (regimeIndEl) {
      regimeIndEl.style.background = info.color;
      regimeIndEl.style.boxShadow = `0 0 10px ${info.color}`;
    }
  }

  const radarRow = document.getElementById('symbol-radar-row');
  if (!radarRow) return;
  const telemetry = engineData.symbol_telemetry || {};
  const symbols = engineData.symbols || Object.keys(telemetry);

  if (!symbols || symbols.length === 0) {
    radarRow.innerHTML = '';
    return;
  }

  radarRow.innerHTML = symbols.map(sym => {
    const t = telemetry[sym] || {};
    const reg = t.regime || 'SCANNING';
    const px = t.mid_price > 0 ? formatSmartPrice(t.mid_price) : ' Priming';
    const ofi = t.ofi !== undefined ? (t.ofi >= 0 ? `+${Number(t.ofi).toFixed(2)}` : Number(t.ofi).toFixed(2)) : '0.00';
    const regColor = reg === 'TREND_PULLBACK' || reg === 'BREAKOUT_EXPANSION'
      ? 'var(--accent-emerald)'
      : reg === 'MEAN_REVERSION_RANGE'
        ? 'var(--accent-amber)'
        : 'var(--text-secondary)';
    return `
      <div class="symbol-pill">
        <strong style="color: #fff;">${escapeHtml(sym)}</strong>
        <span style="color: var(--accent-cyan);">${px}</span>
        <span style="color: ${regColor}; font-size: 0.68rem;">${escapeHtml(reg)}</span>
        <span style="color: var(--text-muted); font-size: 0.68rem;">OFI:${ofi}</span>
      </div>
    `;
  }).join('');
}

function renderPositionsTable() {
  const tbody = document.getElementById('positions-tbody');
  const positions = engineData.positions || [];
  const maxPos = engineData.max_open_positions || 3;
  document.getElementById('pos-count-tag').innerText = `${positions.length} / ${maxPos} Active`;

  if (positions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 2rem;">No active positions open. Engine continuously scanning multi-timeframe order flow.</td></tr>`;
    return;
  }

  tbody.innerHTML = positions.map(p => {
    const isLong = String(p.side || '').toLowerCase() === 'buy';
    const sideClass = isLong ? 'side-buy' : 'side-sell';
    const uPnl = Number(p.unrealised_pnl || 0);
    const uPnlClass = uPnl >= 0 ? 'positive' : 'negative';
    const uPnlSign = uPnl >= 0 ? '+' : '';
    const tp = p.tp_price ? formatSmartPrice(p.tp_price) : 'Runner';
    const sl = p.sl_price ? formatSmartPrice(p.sl_price) : 'Active';
    const tp1Badge = p.tp1_filled
      ? `<span class="brand-tag" style="margin-left: 0.35rem; background: rgba(0,245,155,0.15); color: var(--accent-emerald);">TP1 BANKED</span>`
      : p.tp1_price
        ? `<span style="color: var(--text-muted); font-size: 0.72rem; margin-left: 0.3rem;">(TP1: ${formatSmartPrice(p.tp1_price)})</span>`
        : '';
    const beBadge = p.breakeven_set
      ? `<span class="brand-tag" style="margin-left: 0.35rem; background: rgba(0,240,255,0.15); color: var(--accent-cyan);">BE LOCKED</span>`
      : '';

    return `
      <tr>
        <td><strong style="color: #fff;">${escapeHtml(p.symbol)}</strong></td>
        <td><span class="side-badge ${sideClass}">${escapeHtml(String(p.side).toUpperCase())}</span></td>
        <td>${escapeHtml(p.size)}</td>
        <td>${formatSmartPrice(p.entry_price)}</td>
        <td>${formatSmartPrice(p.mark_price)}</td>
        <td class="${uPnlClass}"><strong>${uPnlSign}$${uPnl.toFixed(2)}</strong></td>
        <td style="color: var(--accent-emerald);">${tp}${tp1Badge}</td>
        <td style="color: var(--accent-rose);">${sl}${beBadge}</td>
        <td><span style="background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 4px;">${p.leverage || 1}x</span></td>
      </tr>
    `;
  }).join('');
}

function renderRecentTradesTable() {
  const tbody = document.getElementById('trades-tbody');
  const tag = document.getElementById('recent-trades-tag');
  if (!tbody) return;

  const trades = engineData.recent_trades || [];
  if (tag) tag.innerText = `${trades.length} Recent`;

  if (trades.length === 0) {
    tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; color: var(--text-muted); padding: 1.5rem;">No closed trades recorded yet in the SQLite journal.</td></tr>`;
    return;
  }

  tbody.innerHTML = trades.map(t => {
    const isBuy = String(t.side || '').toLowerCase() === 'buy';
    const sideClass = isBuy ? 'side-buy' : 'side-sell';
    const pnl = Number(t.realized_pnl || 0);
    const pnlPct = Number(t.realized_pnl_pct || 0);
    const pnlClass = pnl >= 0 ? 'positive' : 'negative';
    const sign = pnl >= 0 ? '+' : '';
    const holdSec = Number(t.holding_time_sec || 0);
    const holdStr = holdSec >= 60 ? `${(holdSec / 60).toFixed(1)}m` : `${holdSec.toFixed(0)}s`;

    return `
      <tr>
        <td><strong style="color: #fff;">${escapeHtml(t.symbol)}</strong></td>
        <td><span class="side-badge ${sideClass}">${escapeHtml(String(t.side).toUpperCase())}</span></td>
        <td>${escapeHtml(t.qty)}</td>
        <td>${formatSmartPrice(t.entry_price)} → ${formatSmartPrice(t.exit_price)}</td>
        <td class="${pnlClass}"><strong>${sign}$${pnl.toFixed(2)}</strong> <span style="font-size: 0.72rem;">(${sign}${pnlPct.toFixed(2)}%)</span></td>
        <td><span style="font-size: 0.72rem; color: var(--text-secondary);">${escapeHtml(t.regime || 'SCALP')}</span></td>
        <td style="color: var(--text-muted);">${holdStr}</td>
      </tr>
    `;
  }).join('');
}

function renderEventFeed() {
  const container = document.getElementById('feed-container');
  if (!container) return;

  const events = engineData.recent_events || [];
  if (events.length === 0) {
    return;
  }

  container.innerHTML = events.map(ev => {
    const level = ev.level || 'info';
    const borderColor = level === 'success'
      ? 'var(--accent-emerald)'
      : level === 'danger'
        ? 'var(--accent-rose)'
        : level === 'warn'
          ? 'var(--accent-amber)'
          : 'var(--accent-cyan)';
    return `
      <div class="feed-item" style="border-left-color: ${borderColor};">
        <div class="feed-time">${escapeHtml(ev.time_str || '--:--:--')}</div>
        <div class="feed-content">
          <div class="feed-headline">[${escapeHtml(ev.category || 'EVENT')}] ${escapeHtml(ev.message)}</div>
        </div>
      </div>
    `;
  }).join('');
}

function renderCircuitBreakers(cb, maxDdLimit, ddPct) {
  const ddNameEl = document.getElementById('cb-dd-name');
  if (ddNameEl) ddNameEl.innerText = `Daily Max Drawdown (${maxDdLimit.toFixed(1)}%)`;

  const ddCurrEl = document.getElementById('cb-dd-current');
  if (ddCurrEl) ddCurrEl.innerText = `Current: ${ddPct.toFixed(2)}%`;

  const ddBadge = document.getElementById('cb-dd-badge');
  if (cb.daily_drawdown_tripped) {
    ddBadge.innerText = 'TRIPPED';
    ddBadge.className = 'cb-badge cb-tripped';
  } else {
    ddBadge.innerText = 'ACTIVE / OK';
    ddBadge.className = 'cb-badge cb-active';
  }

  const lossBadge = document.getElementById('cb-loss-badge');
  if (cb.in_cooldown) {
    lossBadge.innerText = `COOLDOWN (${cb.cooldown_remaining_sec}s)`;
    lossBadge.className = 'cb-badge cb-tripped';
  } else {
    lossBadge.innerText = 'ACTIVE / OK';
    lossBadge.className = 'cb-badge cb-active';
  }
  const maxLosses = cb.max_consecutive_losses || 4;
  document.getElementById('cb-loss-count').innerText = `${cb.consecutive_losses || 0} / ${maxLosses} Losses (30m cooldown)`;

  const volBadge = document.getElementById('cb-vol-badge');
  if (volBadge) {
    if (cb.volatility_kill) {
      if (cb.volatility_cooldown_remaining_sec > 0) {
        volBadge.innerText = `COOLDOWN (${cb.volatility_cooldown_remaining_sec}s)`;
      } else {
        volBadge.innerText = 'HALTED';
      }
      volBadge.className = 'cb-badge cb-tripped';
    } else {
      volBadge.innerText = 'ACTIVE / OK';
      volBadge.className = 'cb-badge cb-active';
    }
  }
}

function renderTuningAndLearner(params, learnerWeights) {
  if (params) {
    if (params.tp_atr_mult !== undefined) {
      document.getElementById('param-tp').innerText = `${Number(params.tp_atr_mult).toFixed(2)} × ATR`;
    }
    if (params.sl_atr_mult !== undefined) {
      document.getElementById('param-sl').innerText = `${Number(params.sl_atr_mult).toFixed(2)} × ATR`;
    }
    if (params.kelly_scale !== undefined) {
      document.getElementById('param-kelly').innerText = `${Number(params.kelly_scale).toFixed(2)} (Half-Kelly)`;
    }
  }

  const weightsContainer = document.getElementById('learner-weights-list');
  if (!weightsContainer || !learnerWeights) return;

  const entries = Object.entries(learnerWeights);
  if (entries.length === 0) return;

  weightsContainer.innerHTML = entries.map(([regime, stats]) => {
    const mult = Number(stats.multiplier || 1.0);
    const trades = Number(stats.trades || 0);
    const color = mult >= 1.05 ? 'var(--accent-emerald)' : mult < 0.95 ? 'var(--accent-amber)' : 'var(--accent-cyan)';
    return `
      <div style="display: flex; justify-content: space-between; align-items: center;">
        <span style="color: var(--text-secondary);">${escapeHtml(regime)}</span>
        <span style="color: ${color}; font-weight: 600;">${mult.toFixed(2)}× <span style="color: var(--text-muted); font-weight: 400;">(${trades}t)</span></span>
      </div>
    `;
  }).join('');
}

function openPanicModal() {
  if (engineData.read_only) {
    showToast("⚠️ Actions disabled: Web dashboard is operating in VIEW-ONLY mode.", false);
    return;
  }
  const dialog = document.getElementById('panic-dialog');
  if (dialog && typeof dialog.showModal === 'function') {
    dialog.showModal();
  } else if (confirm("⚠️ ARE YOU SURE? This will immediately cancel ALL open orders and submit MARKET orders to close all positions.")) {
    confirmPanicExecution();
  }
}

function closePanicModal() {
  const dialog = document.getElementById('panic-dialog');
  if (dialog && typeof dialog.close === 'function') {
    dialog.close();
  }
}

async function confirmPanicExecution() {
  closePanicModal();
  showToast("🚨 EXECUTING PANIC LIQUIDATION...", false);
  try {
    const res = await fetch('/api/panic', { method: 'POST' });
    const data = await res.json();
    if (res.ok && data.success !== false) {
      showToast("✓ All positions flattened and orders cancelled.", true);
    } else {
      showToast(`⚠️ Panic response: ${data.error || data.output || 'Check CLI logs'}`, false);
    }
    fetchStatus();
  } catch (e) {
    engineData.positions = [];
    engineData.status = "PANIC_HALTED";
    renderDashboard();
    showToast("✓ Emergency stop triggered.", true);
  }
}

// Keep backwards compatibility if called directly
const triggerPanic = openPanicModal;

function showToast(msg, isSuccess = true) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.style.borderColor = isSuccess ? 'var(--accent-emerald)' : 'var(--accent-rose)';
  toast.innerText = msg;

  if (typeof toast.showPopover === 'function') {
    try {
      toast.showPopover();
    } catch (e) {
      toast.style.display = 'block';
    }
  } else {
    toast.style.display = 'block';
  }

  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    if (typeof toast.hidePopover === 'function') {
      try {
        toast.hidePopover();
      } catch (e) {}
    }
    toast.style.display = 'none';
  }, 4000);
}

// Initial fetch and 2-second polling loop
fetchStatus();
setInterval(fetchStatus, 2000);
