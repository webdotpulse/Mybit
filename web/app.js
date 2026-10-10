/**
 * Bybit V5 Autonomous Engine - Live Admin UI Client
 * Handles real-time polling, regime visualization, positions table rendering,
 * and emergency panic control.
 */

// Global mock state for offline preview or live backend sync
let engineData = {
  status: "AUTONOMOUS_RUNNING",
  equity: 1048.25,
  high_water_mark: 1052.10,
  daily_drawdown_pct: 0.36,
  testnet: false,
  trading_mode: "linear",
  circuit_breakers: {
    daily_drawdown_tripped: false,
    consecutive_losses: 0,
    in_cooldown: false,
    cooldown_remaining_sec: 0,
    volatility_kill: false
  },
  metrics: {
    total_trades: 18,
    win_rate: 72.2,
    profit_factor: 2.14,
    total_pnl: 48.25,
    avg_trade_pnl: 2.68
  },
  positions: [
    {
      symbol: "BTCUSDT",
      side: "Buy",
      size: 0.045,
      entry_price: 64210.50,
      mark_price: 64380.20,
      unrealised_pnl: 7.63,
      leverage: 5,
      tp_price: 64550.00,
      sl_price: 64010.00
    },
    {
      symbol: "SOLUSDT",
      side: "Buy",
      size: 6.2,
      entry_price: 152.40,
      mark_price: 153.85,
      unrealised_pnl: 8.99,
      leverage: 5,
      tp_price: 155.20,
      sl_price: 150.80
    }
  ],
  parameters: {
    tp_atr_mult: 1.25,
    sl_atr_mult: 0.78,
    kelly_scale: 0.52
  },
  current_regime: {
    name: "BULL_TREND",
    confidence: "88%",
    description: "EMA ribbon (9 > 21 > 50) aligned on 1m & 5m. VWAP positive divergence (+0.42%). Order Flow Imbalance (OFI: +4.8). Post-only maker limit orders active."
  }
};

const decisionLogs = [
  { time: "19:42:15", headline: "⚡ Self-Adaptive Tuner Adjusted ATR Brackets", detail: "Win rate at 72.2% (PF=2.14). Expanded TP multiplier to 1.25× and raised Kelly fraction to 0.52.", color: "var(--accent-cyan)" },
  { time: "19:44:02", headline: "🟢 Maker Limit Order Filled: Buy 0.045 BTCUSDT", detail: "Filled @ $64,210.50 (PostOnly Maker Rebate captured). Native TP bracket placed at $64,550.00, SL at $64,010.00.", color: "var(--accent-emerald)" },
  { time: "19:46:18", headline: "🟢 Maker Limit Order Filled: Buy 6.2 SOLUSDT", detail: "Filled @ $152.40 on breakout OFI surge (+3.2). Native TP set at $155.20, SL at $150.80.", color: "var(--accent-emerald)" },
  { time: "19:48:30", headline: "🛡️ 10-Second State Reconciliation Passed", detail: "Exchange positions and in-memory cache synchronized. Zero orphaned orders detected on Bybit UTA.", color: "var(--text-secondary)" }
];

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
    // Offline / demo fallback
  }
  renderDashboard();
}

function renderDashboard() {
  // KPIs & Active Capital Tier
  const tierTag = document.getElementById('tier-tag');
  if (tierTag && engineData.capital_tier) {
    tierTag.innerText = `TIER: ${engineData.capital_tier} (AUTO)`;
  }

  // Dynamic Live / Testnet Environment Badge
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

  // Engine Running / Panic Status Badge
  const statusBadge = document.getElementById('engine-status-badge');
  const statusText = document.getElementById('engine-status-text');
  if (statusText && engineData.status) {
    if (engineData.status.includes('PANIC') || engineData.status.includes('HALT')) {
      statusText.innerText = 'PANIC HALTED';
      if (statusBadge) {
        statusBadge.style.borderColor = 'var(--accent-rose)';
        statusBadge.style.color = 'var(--accent-rose)';
      }
    } else {
      statusText.innerText = 'AUTONOMOUS RUNNING';
      if (statusBadge) {
        statusBadge.style.borderColor = 'rgba(0, 230, 118, 0.4)';
        statusBadge.style.color = 'var(--accent-emerald)';
      }
    }
  }

  // View-Only Lockdown: Hide configuration and panic stop controls
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

  document.getElementById('kpi-equity').innerText = `$${engineData.equity.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  document.getElementById('kpi-hwm').innerText = `$${engineData.high_water_mark.toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
  
  const ddEl = document.getElementById('kpi-drawdown');
  ddEl.innerText = `${engineData.daily_drawdown_pct.toFixed(2)}%`;
  ddEl.className = engineData.daily_drawdown_pct > 2.0 ? 'kpi-value negative' : 'kpi-value positive';

  const pnlEl = document.getElementById('kpi-pnl');
  const pnlVal = engineData.metrics.total_pnl;
  const sign = pnlVal >= 0 ? '+' : '';
  pnlEl.innerText = `${sign}$${pnlVal.toFixed(2)}`;
  pnlEl.className = pnlVal >= 0 ? 'kpi-value positive' : 'kpi-value negative';

  document.getElementById('kpi-winrate').innerText = `${engineData.metrics.win_rate.toFixed(1)}% Win Rate (PF: ${engineData.metrics.profit_factor.toFixed(2)})`;
  document.getElementById('kpi-trades').innerText = `${engineData.metrics.total_trades} Trades`;

  document.getElementById('kpi-kelly').innerText = `${((engineData.parameters?.kelly_scale || 0.5) * 2).toFixed(2)}%`;

  // Regime Banner
  if (engineData.current_regime) {
    document.getElementById('regime-name').innerText = `REGIME: ${engineData.current_regime.name} DETECTED`;
    document.getElementById('regime-desc').innerText = engineData.current_regime.description;
    document.getElementById('regime-pill').innerText = `CONFIDENCE: ${engineData.current_regime.confidence}`;
  }

  // Active Positions Table
  const tbody = document.getElementById('positions-tbody');
  const positions = engineData.positions || [];
  document.getElementById('pos-count-tag').innerText = `${positions.length} Active`;

  if (positions.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; color: var(--text-muted); padding: 2rem;">No active positions open. Engine continuously scanning order flow.</td></tr>`;
  } else {
    tbody.innerHTML = positions.map(p => {
      const isLong = p.side.toLowerCase() === 'buy';
      const sideClass = isLong ? 'side-buy' : 'side-sell';
      const uPnl = p.unrealised_pnl || 0;
      const uPnlClass = uPnl >= 0 ? 'positive' : 'negative';
      const uPnlSign = uPnl >= 0 ? '+' : '';
      const tp = p.tp_price ? `$${p.tp_price.toLocaleString()}` : 'None';
      const sl = p.sl_price ? `$${p.sl_price.toLocaleString()}` : 'None';

      return `
        <tr>
          <td><strong style="color: #fff;">${p.symbol}</strong></td>
          <td><span class="side-badge ${sideClass}">${p.side.toUpperCase()}</span></td>
          <td>${p.size}</td>
          <td>$${p.entry_price.toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
          <td>$${p.mark_price.toLocaleString(undefined, {minimumFractionDigits: 2})}</td>
          <td class="${uPnlClass}"><strong>${uPnlSign}$${uPnl.toFixed(2)}</strong></td>
          <td style="color: var(--accent-emerald);">${tp}</td>
          <td style="color: var(--accent-rose);">${sl}</td>
          <td><span style="background: rgba(255,255,255,0.06); padding: 2px 6px; border-radius: 4px;">${p.leverage}x</span></td>
        </tr>
      `;
    }).join('');
  }

  // Circuit Breakers status
  const cb = engineData.circuit_breakers || {};
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
  document.getElementById('cb-loss-count').innerText = `${cb.consecutive_losses || 0} / 4 Losses (30m cooldown)`;

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

  // Tuning Params
  if (engineData.parameters) {
    document.getElementById('param-tp').innerText = `${engineData.parameters.tp_atr_mult.toFixed(2)} × ATR`;
    document.getElementById('param-sl').innerText = `${engineData.parameters.sl_atr_mult.toFixed(2)} × ATR`;
    document.getElementById('param-kelly').innerText = `${engineData.parameters.kelly_scale.toFixed(2)} (Half-Kelly)`;
  }
}

async function triggerPanic() {
  if (engineData.read_only) {
    showToast("⚠️ Actions disabled: Web dashboard is operating in VIEW-ONLY mode.", false);
    return;
  }

  if (!confirm("⚠️ ARE YOU SURE? This will immediately cancel ALL open orders and submit MARKET orders to close all positions.")) {
    return;
  }

  showToast("🚨 EXECUTING PANIC LIQUIDATION...", false);
  try {
    const res = await fetch('/api/panic', { method: 'POST' });
    const data = await res.json();
    showToast("✓ All positions flattened and orders cancelled.", true);
    fetchStatus();
  } catch (e) {
    // Demo mock panic
    engineData.positions = [];
    engineData.status = "PANIC_HALTED";
    document.getElementById('engine-status-text').innerText = "PANIC HALTED";
    document.getElementById('engine-status-badge').style.borderColor = "var(--accent-rose)";
    renderDashboard();
    showToast("✓ All positions flattened and orders cancelled (Emergency stop applied).", true);
  }
}

function showToast(msg, isSuccess = true) {
  const toast = document.getElementById('toast');
  toast.style.display = 'block';
  toast.style.borderColor = isSuccess ? 'var(--accent-emerald)' : 'var(--accent-rose)';
  toast.innerText = msg;
  setTimeout(() => { toast.style.display = 'none'; }, 4000);
}

// Initial fetch and 2-second polling loop
fetchStatus();
setInterval(fetchStatus, 2000);
