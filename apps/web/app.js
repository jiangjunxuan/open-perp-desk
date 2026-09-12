const state = {
  token: "",
  symbol: "BTC-USDT-SWAP",
  bar: "15m",
  analysis: null,
  strategy: null,
  status: null,
};

const $ = (selector) => document.querySelector(selector);

function setText(selector, value) {
  const element = $(selector);
  if (element) element.textContent = value;
}

function setState(selector, value, tone = "neutral") {
  const element = $(selector);
  if (!element) return;
  element.textContent = value;
  element.dataset.tone = tone;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function setMessage(message, tone = "normal") {
  const element = $("#action-message");
  if (!element) return;
  element.textContent = message;
  element.style.color = tone === "error" ? "var(--danger)" : tone === "good" ? "var(--accent)" : "";
}

function setBusy(selectorOrElement, busy, busyLabel = "处理中...") {
  const element = typeof selectorOrElement === "string" ? $(selectorOrElement) : selectorOrElement;
  if (!element) return;
  if (busy) {
    if (!element.dataset.defaultLabel) element.dataset.defaultLabel = element.textContent;
    if (!element.dataset.defaultDisabled) element.dataset.defaultDisabled = String(element.disabled);
    element.textContent = busyLabel;
    element.setAttribute("aria-busy", "true");
    element.disabled = true;
  } else {
    element.textContent = element.dataset.defaultLabel || element.textContent;
    element.removeAttribute("aria-busy");
    element.disabled = element.dataset.defaultDisabled === "true";
  }
}

function tickClock() {
  setText("#clock", `${new Date().toISOString().replace("T", " ").slice(0, 19)} UTC`);
}

function formatNumber(value, digits = 4) {
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString("en-US", { maximumFractionDigits: digits })
    : "--";
}

function formatTime(value) {
  return value ? String(value).replace("T", " ").slice(0, 19) : "--";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("X-Admin-Token", state.token);
  if (options.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, cache: "no-store" });
  let payload = {};
  try { payload = await response.json(); } catch {}
  if (!response.ok) {
    throw new Error(payload.detail || payload.message || `HTTP ${response.status}`);
  }
  return payload;
}

function setConnection(online) {
  const element = $("#connection-status");
  if (!element) return;
  element.textContent = online ? "API 在线" : "API 离线";
  element.classList.toggle("status-good", online);
  element.classList.toggle("status-neutral", !online);
  element.dataset.tone = online ? "good" : "danger";
}

function renderChart(rows) {
  state.lastCandles = rows || [];
  const canvas = $("#price-chart");
  const empty = $("#chart-empty");
  if (!canvas) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(300, Math.floor(rect.width));
  const height = Math.max(240, Math.floor(rect.height));
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const padding = { top: 14, right: 58, bottom: 24, left: 8 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const candles = (rows || [])
    .slice(0, 80)
    .reverse()
    .map((row) => ({
      open: Number(row[1]),
      high: Number(row[2]),
      low: Number(row[3]),
      close: Number(row[4]),
    }))
    .filter((candle) =>
      [candle.open, candle.high, candle.low, candle.close].every(Number.isFinite)
    );
  const prices = candles.flatMap((candle) => [candle.high, candle.low]);
  if (candles.length < 2 || prices.length < 2) {
    empty.textContent = "等待 OKX K 线数据...";
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const range = max - min || 1;
  const yFor = (value) =>
    padding.top + (1 - (value - min) / range) * chartHeight;
  const xFor = (index) =>
    padding.left + (index + 0.5) * chartWidth / candles.length;

  context.strokeStyle = "#1b2b34";
  context.lineWidth = 1;
  context.font = "10px SFMono-Regular, Consolas, monospace";
  context.fillStyle = "#71838d";
  context.textAlign = "left";
  for (let row = 0; row <= 4; row += 1) {
    const y = padding.top + row * chartHeight / 4;
    context.beginPath();
    context.moveTo(padding.left, y);
    context.lineTo(padding.left + chartWidth, y);
    context.stroke();
    context.fillText(formatNumber(max - row * range / 4, 2), padding.left + chartWidth + 8, y + 3);
  }
  for (let column = 1; column < 4; column += 1) {
    const x = padding.left + column * chartWidth / 4;
    context.beginPath();
    context.moveTo(x, padding.top);
    context.lineTo(x, padding.top + chartHeight);
    context.stroke();
  }
  const candleWidth = Math.max(2, Math.min(12, chartWidth / candles.length * 0.58));
  candles.forEach((candle, index) => {
    const x = xFor(index);
    const rising = candle.close >= candle.open;
    const color = rising ? "#22c55e" : "#ef5350";
    const openY = yFor(candle.open);
    const closeY = yFor(candle.close);
    const bodyTop = Math.min(openY, closeY);
    const bodyHeight = Math.max(1.5, Math.abs(closeY - openY));
    context.strokeStyle = color;
    context.fillStyle = rising ? "#123c2b" : "#3b2028";
    context.lineWidth = 1;
    context.beginPath();
    context.moveTo(x, yFor(candle.high));
    context.lineTo(x, yFor(candle.low));
    context.stroke();
    context.fillRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
    context.strokeRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
  });
  const latest = candles.at(-1);
  setText(
    "#chart-a11y",
    `${state.symbol} 最新收盘 ${formatNumber(latest.close, 2)}，区间高点 ${formatNumber(max, 2)}，区间低点 ${formatNumber(min, 2)}。`,
  );
}

function renderWatchlist(tickers) {
  const current = tickers?.[state.symbol]?.data || {};
  const last = Number(current.last);
  const open = Number(current.sodUtc8);
  const change = Number.isFinite(last) && Number.isFinite(open) && open
    ? ((last - open) / open) * 100
    : null;
  setText("#chart-symbol", state.symbol);
  setText("#heading-symbol", state.symbol);
  setText("#market-price", formatNumber(current.last, 2));
  const summary = $("#market-summary");
  if (summary) {
    summary.textContent = change === null ? "--" : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`;
    summary.classList.toggle("change-positive", change !== null && change >= 0);
    summary.classList.toggle("change-negative", change !== null && change < 0);
  }
  setText("#market-high", formatNumber(current.high24h, 2));
  setText("#market-low", formatNumber(current.low24h, 2));
  const bid = Number(current.bidPx);
  const ask = Number(current.askPx);
  setText("#market-spread", Number.isFinite(bid) && Number.isFinite(ask) ? formatNumber(Math.max(0, ask - bid), 2) : "--");
  setText("#market-updated", formatTime(tickers?.[state.symbol]?.received_at));
  setState("#market-price", formatNumber(current.last, 2), "neutral");
}

function renderMarketOverview(overview) {
  const funding = overview?.funding_rate || {};
  const openInterest = overview?.open_interest || {};
  const fundingRate = Number(funding.fundingRate);
  const oi = Number(openInterest.oi || openInterest.oiCcy);
  setText(
    "#market-funding",
    Number.isFinite(fundingRate) ? `${(fundingRate * 100).toFixed(4)}%` : "--",
  );
  setText("#market-oi", Number.isFinite(oi) ? formatNumber(oi, 2) : "--");
}

function renderAnalysis(analysis) {
  state.analysis = analysis;
  const signal = analysis?.signal || {};
  const indicators = analysis?.indicators || {};
  const config = analysis?.config || {};
  const action = signal.action || "hold";
  const actionLabel = { open_long: "OPEN LONG", open_short: "OPEN SHORT", close: "CLOSE", hold: "HOLD" }[action] || action.toUpperCase();
  const actionClass = action === "open_long" ? "long" : action === "open_short" ? "short" : "hold";
  const actionElement = $("#analysis-action");
  actionElement.textContent = actionLabel;
  actionElement.className = `signal ${actionClass}`;
  setText("#analysis-source", analysis?.source || "暂无");
  setText("#analysis-bias", analysis?.bias || "等待分析");
  setText("#analysis-summary", analysis?.report?.summary || "AI 研究结果会显示在这里。");
  setText("#indicator-rsi-label", `RSI ${config.rsi_period || 14}`);
  setText("#indicator-fast-label", `SMA ${config.fast_period || 9}`);
  setText("#indicator-slow-label", `SMA ${config.slow_period || 21}`);
  setText("#indicator-rsi", formatNumber(indicators.rsi ?? indicators.rsi_14, 2));
  setText("#indicator-fast", formatNumber(indicators.sma_fast ?? indicators.sma_9, 2));
  setText("#indicator-slow", formatNumber(indicators.sma_slow ?? indicators.sma_21, 2));
  setText("#indicator-confidence", signal.confidence == null ? "--" : `${(Number(signal.confidence) * 100).toFixed(1)}%`);
  $("#preview-signal").disabled = !state.token || !analysis?.signal;
  $("#execute-signal").disabled = (
    !state.token
    || !analysis?.signal
    || state.status?.execution_enabled !== true
    || state.status?.safety_control?.execution_allowed === false
  );
}

function renderStrategy(strategy) {
  state.strategy = strategy;
  const config = strategy?.config || {};
  $("#strategy-enabled").checked = strategy?.enabled === true;
  $("#fast-period").value = config.fast_period ?? 9;
  $("#slow-period").value = config.slow_period ?? 21;
  $("#rsi-period").value = config.rsi_period ?? 14;
  $("#strategy-leverage").value = config.leverage ?? 2;
  $("#strategy-position-pct").value = config.position_pct ?? 5;
}

function updatePrivateActionAvailability() {
  const unlocked = Boolean(state.token);
  const worker = state.status?.automation_worker || {};
  const integrations = state.status?.integrations || {};
  const executionAllowed = state.status?.safety_control?.execution_allowed !== false;
  $("#run-analysis").disabled = !unlocked;
  $("#run-backtest").disabled = !unlocked;
  $("#run-ai-analysis").disabled = !unlocked || !integrations.tradingagents_configured;
  $("#run-worker").disabled = !unlocked || worker.enabled !== true || !executionAllowed;
  $("#test-notification").disabled = !unlocked || !integrations.pushplus_configured;
  $("#refresh-private").disabled = !unlocked;
  $("#save-strategy").disabled = !unlocked;
  $("#toggle-worker").disabled = !unlocked;
  $("#emergency-stop").disabled = !unlocked;
  $("#resume-trading").disabled = !unlocked;
  $("#unlock-live").disabled = !unlocked
    || state.status?.live_safety?.configuration_enabled !== true
    || state.status?.live_safety?.mode_is_live !== true;
  $("#lock-live").disabled = !unlocked;
}

async function loadStrategies() {
  if (!state.token) return;
  try {
    const payload = await api("/api/v1/strategies");
    const strategy = (payload.data || []).find(
      (item) => item.strategy_id === "structured-technical",
    );
    if (strategy) {
      renderStrategy(strategy);
      setText(
        "#strategy-message",
        strategy.enabled
          ? "自动策略已允许，Worker 仍受全局开关和 Demo 闸门约束。"
          : "自动策略当前关闭。",
      );
    }
    return true;
  } catch (error) {
    setText("#strategy-message", `策略配置读取失败：${error.message}`);
    return false;
  }
}

async function saveStrategy() {
  if (!state.token) {
    setText("#strategy-message", "请先输入管理员令牌");
    return;
  }
  const config = {
    fast_period: Number($("#fast-period").value),
    slow_period: Number($("#slow-period").value),
    rsi_period: Number($("#rsi-period").value),
    leverage: Number($("#strategy-leverage").value),
    position_pct: Number($("#strategy-position-pct").value),
  };
  if (
    !Object.values(config).every(Number.isFinite)
    || config.fast_period >= config.slow_period
  ) {
    setText("#strategy-message", "参数无效：快线必须小于慢线，且所有数值需要有效。");
    return;
  }
  setBusy("#save-strategy", true, "保存中...");
  try {
    const payload = await api("/api/v1/strategies/structured-technical", {
      method: "PUT",
      body: JSON.stringify({
        name: "结构化技术策略",
        enabled: $("#strategy-enabled").checked,
        config,
      }),
    });
    renderStrategy(payload.data);
    setText("#strategy-message", "策略参数已保存，下一次分析和 Worker 周期会读取新配置。");
    setMessage("策略配置已更新", "good");
  } catch (error) {
    setText("#strategy-message", `策略保存失败：${error.message}`);
  } finally {
    setBusy("#save-strategy", false);
  }
}

function renderPositions(rows) {
  const body = $("#positions-body");
  if (!rows?.length) {
    body.innerHTML = '<tr><td colspan="7" class="table-empty">暂无本地活动持仓</td></tr>';
    setText("#metric-positions", "0");
    return;
  }
  setText("#metric-positions", rows.length);
  body.innerHTML = rows.map((row) => `
    <tr>
      <td class="mono-cell">${escapeHtml(row.inst_id)}</td>
      <td>${escapeHtml(row.pos_side)}</td>
      <td class="mono-cell">${formatNumber(row.size)}</td>
      <td class="mono-cell">${formatNumber(row.entry_price, 2)}</td>
      <td class="mono-cell">${formatNumber(row.notional, 2)}</td>
      <td class="mono-cell">${formatNumber(row.take_profit, 2)} / ${formatNumber(row.stop_loss, 2)}</td>
      <td>${escapeHtml(row.status)}</td>
    </tr>`).join("");
}

function renderPnl(rows) {
  const total = (rows || []).reduce(
    (sum, row) => sum + (Number(row.unrealized_pnl) || 0),
    0,
  );
  const element = $("#metric-pnl");
  if (!element) return;
  element.textContent = formatNumber(total, 2);
  element.classList.toggle("change-positive", total > 0);
  element.classList.toggle("change-negative", total < 0);
}

function renderOrders(rows) {
  const body = $("#orders-body");
  if (!rows?.length) {
    body.innerHTML = '<tr><td colspan="7" class="table-empty">暂无本地订单</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => `
    <tr>
      <td class="mono-cell">${formatTime(row.created_at)}</td>
      <td class="mono-cell">${escapeHtml(row.inst_id)}</td>
      <td>${escapeHtml(row.side)}</td>
      <td class="mono-cell">${formatNumber(row.size)}</td>
      <td>${escapeHtml(row.status)}</td>
      <td class="mono-cell">${escapeHtml(row.client_order_id)}</td>
      <td>${["canceled", "filled", "failed", "rejected", "cancel_failed"].includes(row.status)
        ? '<span class="subtle">--</span>'
        : `<button class="table-action cancel-order" type="button" data-client-order-id="${escapeHtml(row.client_order_id)}">撤单</button>`}</td>
    </tr>`).join("");
}

async function cancelOrder(clientOrderId) {
  if (!state.token || !clientOrderId) return;
  if (!window.confirm(`确认撤销订单 ${clientOrderId}？`)) return;
  setMessage("正在请求撤单...");
  try {
    const result = await api(
      `/api/v1/execution/orders/${encodeURIComponent(clientOrderId)}/cancel`,
      { method: "POST" },
    );
    setMessage(
      result.accepted ? "订单已撤销" : "订单撤销未被交易所接受",
      result.accepted ? "good" : "error",
    );
    await loadPrivate();
  } catch (error) {
    setMessage(`撤单失败：${error.message}`, "error");
  }
}

function renderFills(rows) {
  const body = $("#fills-body");
  if (!rows?.length) {
    body.innerHTML = '<tr><td colspan="7" class="table-empty">暂无已同步成交</td></tr>';
    return;
  }
  body.innerHTML = rows.map((row) => `
    <tr>
      <td class="mono-cell">${escapeHtml(row.filled_at)}</td>
      <td class="mono-cell">${escapeHtml(row.inst_id)}</td>
      <td>${escapeHtml(row.side)}</td>
      <td class="mono-cell">${formatNumber(row.fill_price, 2)}</td>
      <td class="mono-cell">${formatNumber(row.fill_size)}</td>
      <td class="mono-cell">${formatNumber(row.realized_pnl, 4)}</td>
      <td class="mono-cell">${formatNumber(row.fee, 4)}</td>
    </tr>`).join("");
}

function renderEquityChart(curve) {
  const canvas = $("#equity-chart");
  const empty = $("#performance-empty");
  if (!canvas || !empty) return;
  const rect = canvas.getBoundingClientRect();
  const width = Math.max(300, Math.floor(rect.width));
  const height = Math.max(180, Math.floor(rect.height));
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const points = (curve || [])
    .map((item) => Number(item.equity))
    .filter(Number.isFinite);
  if (points.length < 2) {
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  const padding = { top: 16, right: 58, bottom: 22, left: 8 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const xFor = (index) => padding.left + (index / (points.length - 1)) * chartWidth;
  const yFor = (value) => padding.top + (1 - (value - min) / range) * chartHeight;

  context.strokeStyle = "#1b2b34";
  context.lineWidth = 1;
  context.font = "10px SFMono-Regular, Consolas, monospace";
  context.fillStyle = "#71838d";
  context.textAlign = "left";
  for (let row = 0; row <= 3; row += 1) {
    const y = padding.top + row * chartHeight / 3;
    context.beginPath();
    context.moveTo(padding.left, y);
    context.lineTo(padding.left + chartWidth, y);
    context.stroke();
    context.fillText(formatNumber(max - row * range / 3, 2), padding.left + chartWidth + 8, y + 3);
  }
  context.strokeStyle = "#7bb6ff";
  context.lineWidth = 2;
  context.beginPath();
  points.forEach((point, index) => {
    const x = xFor(index);
    const y = yFor(point);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.stroke();
  context.fillStyle = "#7bb6ff";
  context.beginPath();
  context.arc(xFor(points.length - 1), yFor(points.at(-1)), 3, 0, Math.PI * 2);
  context.fill();
}

function renderPerformance(report) {
  const data = report || {};
  state.performance = data;
  const returnPct = Number(data.return_pct);
  const netPnl = Number(data.net_pnl);
  const drawdown = Number(data.max_drawdown_pct);
  setText("#performance-return", Number.isFinite(returnPct) ? `收益 ${returnPct.toFixed(2)}%` : "收益 --");
  setText("#performance-equity", formatNumber(data.ending_equity, 2));
  setText("#performance-net-pnl", formatNumber(netPnl, 2));
  setText("#performance-drawdown", Number.isFinite(drawdown) ? `${drawdown.toFixed(2)}%` : "--");
  setText("#performance-fills", formatNumber(data.fills, 0));
  const pnlElement = $("#performance-net-pnl");
  pnlElement.classList.toggle("change-positive", Number.isFinite(netPnl) && netPnl > 0);
  pnlElement.classList.toggle("change-negative", Number.isFinite(netPnl) && netPnl < 0);
  const buckets = Object.entries(data.by_strategy || {});
  const strategyElement = $("#strategy-performance");
  strategyElement.innerHTML = buckets.length
    ? buckets.map(([name, bucket]) => `
        <span><b>${escapeHtml(name)}</b> ${formatNumber(bucket.net_pnl, 2)} · ${formatNumber(bucket.fills, 0)} 笔</span>
      `).join("")
    : '<span class="subtle">暂无已实现成交，权益曲线将在成交后出现</span>';
  renderEquityChart(data.equity_curve);
}

function renderActivity(rows) {
  const list = $("#activity-list");
  if (!rows?.length) {
    list.innerHTML = '<div class="table-empty">暂无审计事件</div>';
    return;
  }
  list.innerHTML = rows.map((row) => `
    <div class="activity-row">
      <span class="mono subtle">${formatTime(row.created_at)}</span>
      <span class="event">${escapeHtml(row.event_type)}</span>
      <span class="severity">${escapeHtml(row.severity)} · ${escapeHtml(row.message)}</span>
    </div>`).join("");
}

function applyStatus(status) {
  state.status = status;
  const mode = String(status.trading_mode || "demo").toUpperCase();
  const modeLabel = mode === "LIVE" ? "实盘" : "模拟盘";
  const marketLabel = status.market_data_connected ? "在线" : "未接入";
  const worker = status.automation_worker || {};
  const workerLabel = worker.running ? "运行中" : worker.enabled ? "已启用" : "待命";
  const executionLabel = status.execution_enabled
    ? mode === "LIVE" ? "实盘执行已解锁" : "Demo 执行已启用"
    : "执行已锁定";
  const executionAllowed = status.safety_control?.execution_allowed !== false;
  setText("#trading-mode", modeLabel);
  setText("#top-environment", String(status.environment || "development").toUpperCase());
  setText("#top-execution", executionLabel);
  setText("#execute-signal", mode === "LIVE" ? "提交实盘订单" : "执行 Demo");
  setText("#state-mode", modeLabel);
  setState("#state-market", marketLabel, status.market_data_connected ? "good" : "warning");
  setState("#state-risk", status.risk_engine_ready ? "就绪" : "danger", status.risk_engine_ready ? "good" : "danger");
  setText(
    "#state-exposure-limit",
    `${formatNumber(status.risk_limits?.max_total_notional_pct, 2)}% 权益`,
  );
  setState("#state-proxy", status.integrations?.outbound_proxy_configured ? "已配置" : "未配置", status.integrations?.outbound_proxy_configured ? "good" : "neutral");
  setState("#state-pushplus", status.integrations?.pushplus_configured ? "已配置" : "未配置", status.integrations?.pushplus_configured ? "good" : "neutral");
  setState("#state-ai", status.integrations?.tradingagents_configured ? "已配置" : "未启用", status.integrations?.tradingagents_configured ? "good" : "neutral");
  setState(
    "#state-algo-stream",
    status.algo_stream?.connected
      ? "在线"
      : status.algo_stream?.configured
        ? "连接中"
        : "未配置",
    status.algo_stream?.connected
      ? "good"
      : status.algo_stream?.configured
        ? "warning"
        : "neutral",
  );
  setState("#state-stop", status.safety_control?.emergency_stopped ? "已急停" : "未触发", status.safety_control?.emergency_stopped ? "danger" : "good");
  const liveSafety = status.live_safety || {};
  setText(
    "#live-safety-message",
    liveSafety.allowed
      ? "实盘闸门已解锁，仅在当前进程内有效。"
      : liveSafety.mode_is_live
        ? "实盘配置存在，但仍需要人工解锁。"
        : "当前不是实盘模式，解锁按钮保持关闭。",
  );
  setText("#unlock-live", liveSafety.allowed ? "实盘已解锁" : "解锁实盘");
  setText("#execution-state", status.execution_enabled ? executionLabel : "已锁定");
  setText(
    "#execution-note",
    status.safety_control?.emergency_stopped
      ? "急停已触发"
      : status.execution_enabled ? "风险闸门已打开" : "需要 Demo 凭据与开关",
  );
  setText("#metric-risk", status.risk_engine_ready ? "就绪" : "锁定");
  setText(
    "#metric-risk-note",
    status.safety?.live_orders_allowed ? "实盘已解锁" : "实盘默认禁止",
  );
  setText(
    "#operation-summary",
    `${modeLabel} · ${status.safety_control?.emergency_stopped ? "急停中" : status.execution_enabled ? "执行已启用" : "执行已锁定"}`,
  );
  setText("#ribbon-market", marketLabel);
  setText("#ribbon-worker", workerLabel);
  setText("#toggle-worker", worker.enabled ? "停止 Worker" : "启用 Demo Worker");
  $("#toggle-worker").classList.toggle("danger", worker.enabled);
  $("#toggle-worker").classList.toggle("secondary", !worker.enabled);
  $("#worker-dry-run").checked = worker.dry_run !== false;
  setText("#ribbon-sync", formatTime(status.market_stream?.last_message_at));
  setState("#sidebar-market", marketLabel, status.market_data_connected ? "good" : "warning");
  setState("#sidebar-risk", status.risk_engine_ready ? "就绪" : "锁定", status.risk_engine_ready ? "good" : "danger");
  setState("#sidebar-worker", workerLabel, worker.running ? "good" : worker.enabled ? "warning" : "neutral");
  updatePrivateActionAvailability();
  $("#execute-signal").disabled = (
    !state.analysis?.signal
    || status.execution_enabled !== true
    || !executionAllowed
    || !state.token
  );
}

async function unlockLive() {
  if (!state.token) {
    setMessage("请先输入管理员令牌", "error");
    return;
  }
  const phrase = $("#live-unlock-phrase").value;
  if (!phrase) {
    setText("#live-safety-message", "请输入人工解锁短语。");
    return;
  }
  setBusy("#unlock-live", true, "解锁中...");
  try {
    await api("/api/v1/safety/live/unlock", {
      method: "POST",
      body: JSON.stringify({ phrase }),
    });
    $("#live-unlock-phrase").value = "";
    setMessage("实盘安全闸门已在当前进程内解锁", "good");
    await loadStatus();
  } catch (error) {
    setText("#live-safety-message", `实盘解锁失败：${error.message}`);
    setMessage("实盘解锁失败", "error");
  } finally {
    setBusy("#unlock-live", false);
  }
}

async function lockLive() {
  if (!state.token) {
    setMessage("请先输入管理员令牌", "error");
    return;
  }
  setBusy("#lock-live", true, "锁定中...");
  try {
    await api("/api/v1/safety/live/lock", { method: "POST" });
    setMessage("实盘安全闸门已锁定", "good");
    await loadStatus();
  } catch (error) {
    setText("#live-safety-message", `实盘锁定失败：${error.message}`);
    setMessage("实盘锁定失败", "error");
  } finally {
    setBusy("#lock-live", false);
  }
}

async function setEmergencyStop(path, message) {
  if (!state.token) {
    setMessage("请先输入管理员令牌", "error");
    return;
  }
  try {
    const payload = await api(path, {
      method: "POST",
      body: JSON.stringify({ reason: message }),
    });
    setText("#state-stop", payload.emergency_stopped ? "已急停" : "未触发");
    setMessage(payload.emergency_stopped ? "新订单已停止放行" : "执行闸门已恢复", "good");
    await loadStatus();
  } catch (error) {
    setMessage(`安全操作失败：${error.message}`, "error");
  }
}

async function loadStatus() {
  try {
    const payload = await api("/api/v1/system/status");
    applyStatus(payload);
    setConnection(true);
  } catch {
    setConnection(false);
  }
}

async function loadMarket() {
  setBusy("#refresh-market", true, "读取中...");
  try {
    const [stream, candlePayload] = await Promise.all([
      api(`/api/v1/market/stream?symbol=${encodeURIComponent(state.symbol)}`),
      api(`/api/v1/market/candles?inst_id=${encodeURIComponent(state.symbol)}&bar=${encodeURIComponent(state.bar)}&limit=100`),
    ]);
    renderWatchlist(stream.tickers);
    renderChart(candlePayload.data);
    try {
      const overview = await api(`/api/v1/market/overview?inst_id=${encodeURIComponent(state.symbol)}`);
      renderMarketOverview(overview);
    } catch {
      renderMarketOverview({});
    }
    setText("#market-tag", stream.fresh ? "实时只读" : "数据过期");
    setState("#state-market", stream.fresh ? "在线" : "数据过期", stream.fresh ? "good" : "warning");
  } catch (error) {
    setText("#market-tag", "连接失败");
    setText("#chart-empty", error.message);
    $("#chart-empty").hidden = false;
  } finally {
    setBusy("#refresh-market", false);
  }
}

async function loadPrivate() {
  if (!state.token) return;
  setBusy("#refresh-private", true, "同步中...");
  try {
    try {
      await api("/api/v1/account/sync", { method: "POST" });
    } catch (error) {
      setMessage(`账户对账未完成：${error.message}`, "error");
    }
    const [account, positions, orders, fills, pnl, report, activity] = await Promise.all([
      api("/api/v1/account/overview"),
      api("/api/v1/positions"),
      api("/api/v1/orders"),
      api("/api/v1/fills"),
      api("/api/v1/performance/pnl"),
      api(`/api/v1/performance/report?initial_equity=${encodeURIComponent($("#equity-input").value)}`),
      api("/api/v1/activity"),
    ]);
    const accountRow = account.balance?.[0] || {};
    const equity = accountRow.totalEq || accountRow.adjEq || accountRow.eq;
    setText("#metric-equity", formatNumber(equity, 2));
    setText("#metric-equity-note", account.configured ? "OKX 账户快照" : "OKX 私有凭据未配置");
    renderPositions(positions.data);
    renderPnl(positions.data);
    renderOrders(orders.data);
    renderFills(fills.data);
    renderPerformance(report.data);
    renderActivity(activity.data);
    setText(
      "#pnl-summary",
      `净 PnL ${formatNumber(report.data?.net_pnl ?? pnl.data?.net_pnl, 4)} · 回撤 ${formatNumber(report.data?.max_drawdown_pct, 2)}% · ${report.data?.fills || pnl.data?.fills || 0} 笔成交`,
    );
    setText("#positions-tag", "已同步");
    setText("#metric-equity-note", "本地执行状态已解锁");
    return true;
  } catch (error) {
    setMessage(`私有数据同步失败：${error.message}`, "error");
    return false;
  } finally {
    setBusy("#refresh-private", false);
  }
}

async function toggleWorker() {
  if (!state.token) {
    setMessage("请先输入管理员令牌", "error");
    return;
  }
  const worker = state.status?.automation_worker || {};
  const enabled = worker.enabled !== true;
  setBusy("#toggle-worker", true, enabled ? "启用中..." : "停止中...");
  try {
    const payload = await api("/api/v1/worker/control", {
      method: "POST",
      body: JSON.stringify({
        enabled,
        dry_run: enabled ? $("#worker-dry-run").checked : null,
      }),
    });
    setMessage(
      payload.enabled
        ? `Worker 已启用（${payload.dry_run ? "Dry Run" : "Demo 执行"}）`
        : "Worker 已停止",
      "good",
    );
    await loadStatus();
  } catch (error) {
    setMessage(`Worker 控制失败：${error.message}`, "error");
  } finally {
    setBusy("#toggle-worker", false);
  }
}

async function runAnalysis() {
  setBusy("#run-analysis", true, "分析中...");
  setMessage("正在读取 K 线并计算...");
  try {
    const payload = await api("/api/v1/analysis", {
      method: "POST",
      body: JSON.stringify({
        inst_id: state.symbol,
        bar: state.bar,
        limit: 100,
        strategy_id: state.strategy?.strategy_id || "structured-technical",
      }),
    });
    renderAnalysis(payload.data);
    setMessage("结构化策略分析完成", "good");
    await loadPrivate();
  } catch (error) {
    setMessage(`分析失败：${error.message}`, "error");
  } finally {
    setBusy("#run-analysis", false);
  }
}

async function runAiAnalysis() {
  setBusy("#run-ai-analysis", true, "TradingAgents...");
  setMessage("TradingAgents 正在运行...");
  try {
    const payload = await api("/api/v1/analysis/ai", {
      method: "POST",
      body: JSON.stringify({ inst_id: state.symbol, bar: state.bar, limit: 100 }),
    });
    setText("#analysis-source", "TradingAgents");
    setText("#analysis-bias", "AI 研究");
    setText("#analysis-summary", JSON.stringify(payload.data.decision || payload.data).slice(0, 300));
    setMessage("TradingAgents 分析完成", "good");
  } catch (error) {
    setMessage(`TradingAgents 未运行：${error.message}`, "error");
  } finally {
    setBusy("#run-ai-analysis", false);
  }
}

async function runBacktest() {
  setBusy("#run-backtest", true, "回放中...");
  setMessage("正在回放最近 300 根 K 线...");
  try {
    const payload = await api("/api/v1/backtest", {
      method: "POST",
      body: JSON.stringify({
        inst_id: state.symbol,
        bar: state.bar,
        limit: 300,
        initial_equity: Number($("#equity-input").value),
        fee_bps: 5,
        strategy_id: state.strategy?.strategy_id || "structured-technical",
      }),
    });
    const result = payload.data;
    setMessage(
      `回放完成：收益 ${Number(result.return_pct).toFixed(2)}% · 回撤 ${Number(result.max_drawdown_pct).toFixed(2)}% · 交易 ${result.trades} 笔`,
      "good",
    );
  } catch (error) {
    setMessage(`回放失败：${error.message}`, "error");
  } finally {
    setBusy("#run-backtest", false);
  }
}

async function runWorkerOnce() {
  setBusy("#run-worker", true, "运行中...");
  setMessage("正在执行一次受保护的 Worker 周期...");
  try {
    const payload = await api("/api/v1/worker/run", {
      method: "POST",
    });
    const result = payload.results || [];
    setMessage(
      payload.ran
        ? `Worker 完成：${result.length} 个合约已处理`
        : `Worker 未运行：${payload.reason || "当前配置不允许"}`,
      payload.ran ? "good" : "error",
    );
    await Promise.all([loadStatus(), loadPrivate()]);
  } catch (error) {
    setMessage(`Worker 运行失败：${error.message}`, "error");
  } finally {
    setBusy("#run-worker", false);
  }
}

async function testNotification() {
  setBusy("#test-notification", true, "发送中...");
  setMessage("正在发送 PushPlus 测试通知...");
  try {
    await api("/api/v1/notifications/test", {
      method: "POST",
      body: JSON.stringify({
        title: "OpenPerpDesk 测试通知",
        content: "来自 OpenPerpDesk 控制台的 PushPlus 链路测试。",
      }),
    });
    setMessage("PushPlus 测试通知已发送", "good");
  } catch (error) {
    setMessage(`PushPlus 发送失败：${error.message}`, "error");
  } finally {
    setBusy("#test-notification", false);
  }
}

async function submitSignal(dryRun) {
  if (!state.analysis?.signal) {
    setMessage("请先生成策略分析", "error");
    return;
  }
  const payload = {
    signal: state.analysis.signal,
    account_equity: Number($("#equity-input").value),
    daily_pnl_pct: 0,
    current_notional: 0,
    size: Number($("#size-input").value),
    dry_run: dryRun,
  };
  setMessage(dryRun ? "正在执行风控预览..." : "正在提交 Demo 订单...");
  try {
    const result = await api("/api/v1/execution/signals", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    setMessage(result.accepted ? (dryRun ? "风控通过，订单已预览" : "Demo 订单已提交") : `风控拒绝：${(result.reasons || []).join(", ")}`, result.accepted ? "good" : "error");
    await loadPrivate();
  } catch (error) {
    setMessage(`执行失败：${error.message}`, "error");
  }
}

$("#symbol").addEventListener("change", (event) => {
  state.symbol = event.target.value;
  loadMarket();
});
$("#bar").addEventListener("change", (event) => {
  state.bar = event.target.value;
  loadMarket();
});
$("#refresh-market").addEventListener("click", loadMarket);
$("#run-analysis").addEventListener("click", runAnalysis);
$("#run-backtest").addEventListener("click", runBacktest);
$("#run-ai-analysis").addEventListener("click", runAiAnalysis);
$("#run-worker").addEventListener("click", runWorkerOnce);
$("#test-notification").addEventListener("click", testNotification);
$("#save-strategy").addEventListener("click", saveStrategy);
$("#toggle-worker").addEventListener("click", toggleWorker);
$("#unlock-live").addEventListener("click", unlockLive);
$("#lock-live").addEventListener("click", lockLive);
$("#preview-signal").addEventListener("click", () => submitSignal(true));
$("#execute-signal").addEventListener("click", () => submitSignal(false));
$("#refresh-private").addEventListener("click", loadPrivate);
$("#orders-body").addEventListener("click", (event) => {
  const button = event.target.closest(".cancel-order");
  if (button) cancelOrder(button.dataset.clientOrderId);
});
$("#emergency-stop").addEventListener("click", () => setEmergencyStop("/api/v1/safety/emergency-stop", "web operator emergency stop"));
$("#resume-trading").addEventListener("click", () => setEmergencyStop("/api/v1/safety/resume", "web operator resume"));
$("#save-token").addEventListener("click", async () => {
  setBusy("#save-token", true, "同步中...");
  state.token = $("#admin-token").value.trim();
  try {
    const [privateLoaded, strategiesLoaded] = await Promise.all([
      loadPrivate(),
      loadStrategies(),
    ]);
    updatePrivateActionAvailability();
    if (state.token && privateLoaded && strategiesLoaded) {
      setMessage("已解锁私有数据（令牌只保存在当前页面内存）", "good");
      $("#preview-signal").disabled = !state.analysis?.signal;
    } else if (state.token) {
      state.token = "";
      updatePrivateActionAvailability();
      setMessage("管理员令牌无效或私有接口不可用", "error");
    }
  } finally {
    setBusy("#save-token", false);
  }
});

let resizeTimer;
window.addEventListener("resize", () => {
  window.clearTimeout(resizeTimer);
  resizeTimer = window.setTimeout(() => {
    if (state.lastCandles) renderChart(state.lastCandles);
    if (state.performance) renderEquityChart(state.performance.equity_curve);
  }, 150);
});

const navItems = [...document.querySelectorAll(".nav-item")];
const navTargets = navItems
  .map((item) => document.querySelector(item.getAttribute("href")))
  .filter(Boolean);
if ("IntersectionObserver" in window) {
  const navObserver = new IntersectionObserver((entries) => {
    const visible = entries
      .filter((entry) => entry.isIntersecting)
      .sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    navItems.forEach((item) => {
      const active = item.getAttribute("href") === `#${visible.target.id}`;
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "page");
      else item.removeAttribute("aria-current");
    });
  }, { rootMargin: "-20% 0px -65% 0px", threshold: [0, .25, .5] });
  navTargets.forEach((target) => navObserver.observe(target));
}

tickClock();
setInterval(tickClock, 1000);
loadStatus();
setInterval(loadStatus, 15000);
loadMarket();
setInterval(loadMarket, 15000);
