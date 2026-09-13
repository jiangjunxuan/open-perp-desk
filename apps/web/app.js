const state = {
  token: "",
  symbol: "BTC-USDT-SWAP",
  bar: "15m",
  analysis: null,
  strategy: null,
  status: null,
  marketPaused: false,
  marketRequest: 0,
  marketFeedState: "connecting",
  marketStream: null,
  marketCandleKey: null,
  chartTool: "cursor",
  chartAnnotations: [],
  chartDraft: null,
  chartSelection: -1,
  chartUndo: [],
  chartDrag: null,
  chartEditor: null,
  marketHistoryNeedsSync: false,
  controlFeedState: "connecting",
  controlSnapshotFresh: false,
  controlUpdates: 0,
  statusRequest: 0,
  privateFeedState: "locked",
  privateUpdates: 0,
  strategyDirty: false,
  workerModeDirty: false,
  performanceRequest: 0,
  analysisRequest: 0,
  submitting: false,
  ledger: "positions",
  ticket: "signal",
  records: { positions: null, orders: null, fills: null },
  chartMode: "candles",
  chartRange: 80,
  chartCursorTime: null,
  chartFocused: false,
  draftRevision: 0,
  activityRows: null,
  activityRequest: 0,
  backtest: null,
  backtestRequest: 0,
};

const $ = (selector) => document.querySelector(selector);
const busyButtons = new WeakMap();
const terminalOrderStatuses = new Set(["filled", "canceled", "cancelled", "failed", "rejected", "effective", "triggered", "expired", "order_failed", "mmp_canceled"]);
let pendingViewFocus = 0;
const views = {
  overview: ["交易总览", "总览"],
  markets: ["行情与交易", "行情与交易"],
  positions: ["当前持仓", "持仓"],
  orders: ["委托订单", "委托订单"],
  fills: ["成交记录", "成交记录"],
  strategies: ["策略研究", "策略研究"],
  performance: ["绩效与回测", "绩效回测"],
  risk: ["风控与连接", "风控与连接"],
  activity: ["运行日志", "运行日志"],
};

function renderIcons(root = document) {
  root.querySelectorAll("[data-icon]").forEach((icon) => {
    const name = icon.dataset.icon;
    if (/^[a-z][a-z0-9-]*$/.test(name)) {
      icon.style.setProperty("--icon", `url("/assets/icons/${name}.svg")`);
    }
  });
}

function setTheme(theme, persist = true) {
  const light = theme === "light";
  document.documentElement.dataset.theme = light ? "light" : "dark";
  document.querySelectorAll("[data-theme-toggle]").forEach(button => {
    button.setAttribute("aria-pressed", String(light));
    button.title = light ? "切换到深色主题" : "切换到浅色主题";
    button.querySelector("[data-icon]").dataset.icon = light ? "moon" : "sun";
    const label = button.querySelector("[data-theme-label]");
    if (label) label.textContent = light ? "深色" : "浅色";
    renderIcons(button);
  });
  const colors = getComputedStyle(document.documentElement);
  $('meta[name="theme-color"]').content = colors.getPropertyValue("--bg").trim();
  if (persist) {
    try {
      localStorage.setItem("openperpdesk.theme", light ? "light" : "dark");
    } catch {
      // Saving an appearance preference must never interrupt the trading UI.
    }
  }
  if (state.lastCandles) renderChart(state.lastCandles);
  if (state.performance) renderEquityChart(state.performance.equity_curve);
}

function showView(requestedView, focus = false) {
  const focusRequest = ++pendingViewFocus;
  const view = Object.hasOwn(views, requestedView) ? requestedView : "overview";
  if (state.chartFocused) setChartFocus(false);
  document.body.dataset.view = view;
  updateBillHistoryView();
  document.querySelectorAll("[data-views]").forEach((element) => {
    element.hidden = !element.dataset.views.split(" ").includes(view);
  });
  showLedger(state.ledger);
  showTicket(state.ticket);
  document.querySelectorAll(".nav-item, .mobile-nav-item").forEach((item) => {
    const active = (item.dataset.section || item.dataset.route) === view;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "page");
    else item.removeAttribute("aria-current");
    if (active && item.parentElement.scrollWidth > item.parentElement.clientWidth) {
      const nav = item.parentElement;
      nav.scrollLeft += item.getBoundingClientRect().left - nav.getBoundingClientRect().left - 12;
    }
  });
  setText("#page-title", views[view][0]);
  setText("#page-breadcrumb", views[view][1]);
  updateBacktestContext();
  document.title = `${views[view][0]} | OpenPerpDesk`;
  if (view === "strategies") {
    $(".strategy-settings").open = true;
    loadResearchStatus();
    if (state.token && !researchState.loaded) loadResearchHistory();
  }
  requestAnimationFrame(() => {
    if (focusRequest === pendingViewFocus) window.scrollTo({ top: 0, behavior: "instant" });
    if (state.lastCandles) renderChart(state.lastCandles);
    if (state.performance) renderEquityChart(state.performance.equity_curve);
    if (focus && focusRequest === pendingViewFocus && !$("#auth-dialog").open) {
      $("#page-title").focus({ preventScroll: true });
    }
  });
}

function setChartFocus(focused, restoreFocus = false) {
  state.chartFocused = Boolean(focused);
  document.body.classList.toggle("chart-focused", state.chartFocused);
  const button = $("#toggle-chart-focus");
  const label = state.chartFocused ? "退出专注看盘" : "专注看盘";
  button.setAttribute("aria-pressed", String(state.chartFocused));
  button.setAttribute("aria-label", label);
  button.title = label;
  requestAnimationFrame(() => {
    if (state.lastCandles) renderChart(state.lastCandles);
    if (state.performance) renderEquityChart(state.performance.equity_curve);
    if (restoreFocus) button.focus({ preventScroll: true });
  });
}

function showTicket(ticket, focus = false) {
  state.ticket = ticket === "execution" ? "execution" : "signal";
  const inTerminal = document.body.dataset.view === "markets";
  document.querySelectorAll("[data-ticket]").forEach(tab => {
    const selected = tab.dataset.ticket === state.ticket;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const pane = $(`#${tab.dataset.ticket}-pane`);
    pane.hidden = inTerminal && !selected;
    if (inTerminal) {
      pane.setAttribute("role", "tabpanel");
      pane.setAttribute("aria-labelledby", tab.id);
      pane.tabIndex = 0;
    } else {
      pane.removeAttribute("role");
      pane.removeAttribute("tabindex");
      pane.removeAttribute("aria-labelledby");
    }
    if (selected && focus) tab.focus();
  });
}

function showLedger(ledger, focus = false) {
  state.ledger = ["positions", "orders", "fills"].includes(ledger) ? ledger : "positions";
  const inTerminal = document.body.dataset.view === "markets";
  document.querySelectorAll("[data-ledger]").forEach((tab) => {
    const selected = tab.dataset.ledger === state.ledger;
    tab.setAttribute("aria-selected", String(selected));
    tab.tabIndex = selected ? 0 : -1;
    const panel = $(`#${tab.dataset.ledger}`);
    if (inTerminal) {
      panel.hidden = !selected;
      panel.setAttribute("role", "tabpanel");
      panel.setAttribute("aria-labelledby", tab.id);
      panel.tabIndex = 0;
    } else {
      panel.removeAttribute("role");
      panel.removeAttribute("tabindex");
      panel.setAttribute("aria-labelledby", `${panel.id}-title`);
    }
    if (selected && focus) tab.focus();
  });
}

function setText(selector, value) {
  const element = $(selector);
  if (element && element.textContent !== String(value)) element.textContent = value;
}

function setState(selector, value, tone = "neutral") {
  const element = $(selector);
  if (!element) return;
  if (element.textContent !== String(value)) element.textContent = value;
  if (element.dataset.tone !== tone) element.dataset.tone = tone;
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
  element.hidden = !message;
  element.style.color = tone === "error" ? "var(--danger)" : tone === "good" ? "var(--accent)" : "";
  if ($("#auth-dialog")?.open) {
    setText("#auth-message", message);
  }
}

function setBusy(selectorOrElement, busy, busyLabel = "处理中...") {
  const element = typeof selectorOrElement === "string" ? $(selectorOrElement) : selectorOrElement;
  if (!element) return;
  if (busy) {
    if (busyButtons.has(element)) return;
    busyButtons.set(element, { html: element.innerHTML, disabled: element.disabled });
    if (!element.classList.contains("icon-button")) element.textContent = busyLabel;
    element.setAttribute("aria-busy", "true");
    element.disabled = true;
  } else {
    const previous = busyButtons.get(element);
    if (previous) element.innerHTML = previous.html;
    element.removeAttribute("aria-busy");
    if (previous) element.disabled = previous.disabled;
    busyButtons.delete(element);
  }
}

function tickClock() {
  setText("#clock", `${new Date().toISOString().replace("T", " ").slice(0, 19)} UTC`);
}

function formatNumber(value, digits = 4) {
  if (value === null || value === undefined || String(value).trim() === "") return "--";
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
    const error = new Error(payload.detail || payload.message || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
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
  if (!rect.width) return;
  const width = Math.max(300, Math.floor(rect.width));
  const height = Math.max(240, Math.floor(rect.height));
  const ratio = window.devicePixelRatio || 1;
  canvas.width = width * ratio;
  canvas.height = height * ratio;
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);
  const padding = { top: 14, right: 78, bottom: 24, left: 8 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom - 66;
  const candles = (rows || [])
    .slice(0, state.chartRange)
    .reverse()
    .map((row) => ({
      time: Number(row[0]),
      open: Number(row[1]),
      high: Number(row[2]),
      low: Number(row[3]),
      close: Number(row[4]),
      volume: Number(row[5]),
    }))
    .filter((candle) =>
      [candle.time, candle.open, candle.high, candle.low, candle.close].every(Number.isFinite)
    );
  const prices = candles.flatMap((candle) => [candle.high, candle.low]);
  if (candles.length < 2 || prices.length < 2) {
    state.chartGeometry = null;
    $("#chart-cursor").hidden = true;
    renderCandleReadout(null);
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
  const latest = candles.at(-1);
  const latestY = yFor(latest.close);
  const priceLabelY = Math.min(
    padding.top + chartHeight - 10,
    Math.max(padding.top + 10, latestY),
  );

  const colors = getComputedStyle(document.documentElement);
  context.strokeStyle = colors.getPropertyValue("--border-soft").trim();
  context.lineWidth = 1;
  context.font = "10px SFMono-Regular, Consolas, monospace";
  context.fillStyle = colors.getPropertyValue("--muted").trim();
  context.textAlign = "left";
  for (let row = 0; row <= 4; row += 1) {
    const y = padding.top + row * chartHeight / 4;
    context.beginPath();
    context.moveTo(padding.left, y);
    context.lineTo(padding.left + chartWidth, y);
    context.stroke();
    if (Math.abs(y - priceLabelY) > 18) {
      context.fillText(formatNumber(max - row * range / 4, 2), padding.left + chartWidth + 8, y + 3);
    }
  }
  for (let column = 1; column < 4; column += 1) {
    const x = padding.left + column * chartWidth / 4;
    context.beginPath();
    context.moveTo(x, padding.top);
    context.lineTo(x, padding.top + chartHeight);
    context.stroke();
    const candle = candles[Math.min(candles.length - 1, Math.floor(column * candles.length / 4))];
    context.fillText(new Date(candle.time).toISOString().slice(11, 16), x - 15, height - 5);
  }
  const candleWidth = Math.max(2, Math.min(12, chartWidth / candles.length * 0.58));
  const volumeTop = padding.top + chartHeight + 20;
  const volumeMax = Math.max(1, ...candles.map(candle => Number.isFinite(candle.volume) ? candle.volume : 0));
  context.fillText("成交量 · 张", padding.left, volumeTop);
  candles.forEach((candle, index) => {
    const x = xFor(index);
    const rising = candle.close >= candle.open;
    const color = colors.getPropertyValue(rising ? "--accent" : "--danger").trim();
    const openY = yFor(candle.open);
    const closeY = yFor(candle.close);
    const bodyTop = Math.min(openY, closeY);
    const bodyHeight = Math.max(1.5, Math.abs(closeY - openY));
    if (state.chartMode === "candles") {
      context.strokeStyle = color;
      context.fillStyle = color;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(x, yFor(candle.high));
      context.lineTo(x, yFor(candle.low));
      context.stroke();
      context.fillRect(x - candleWidth / 2, bodyTop, candleWidth, bodyHeight);
    }
    if (Number.isFinite(candle.volume) && candle.volume > 0) {
      const volumeHeight = candle.volume / volumeMax * 34;
      context.fillStyle = color;
      context.globalAlpha = .5;
      context.fillRect(x - candleWidth / 2, height - padding.bottom - volumeHeight, candleWidth, volumeHeight);
      context.globalAlpha = 1;
    }
  });
  if (state.chartMode === "line") {
    context.strokeStyle = colors.getPropertyValue("--accent").trim();
    context.lineWidth = 1.5;
    context.beginPath();
    candles.forEach((candle, index) => {
      if (index === 0) context.moveTo(xFor(index), yFor(candle.close));
      else context.lineTo(xFor(index), yFor(candle.close));
    });
    context.stroke();
  }
  const closeY = yFor(latest.close);
  const closeColor = colors.getPropertyValue(latest.close >= latest.open ? "--accent" : "--danger").trim();
  context.strokeStyle = closeColor;
  context.lineWidth = 1;
  context.setLineDash([3, 4]);
  context.beginPath();
  context.moveTo(padding.left, closeY);
  context.lineTo(padding.left + chartWidth, closeY);
  context.stroke();
  context.setLineDash([]);
  context.fillStyle = closeColor;
  context.fillRect(padding.left + chartWidth + 2, priceLabelY - 9, padding.right - 4, 18);
  context.fillStyle = colors.getPropertyValue("--on-chart-label").trim();
  context.fillText(formatNumber(latest.close, 2), padding.left + chartWidth + 7, priceLabelY + 3);
  drawChartAnnotations(context, {
    candles,
    padding,
    chartWidth,
    chartHeight,
    yFor,
    xFor,
  });
  state.chartGeometry = { candles, padding, chartWidth, chartHeight, min, max, yFor, xFor };
  const cursorIndex = candles.findIndex(candle => candle.time === state.chartCursorTime);
  showChartCursor(cursorIndex);
  setText(
    "#chart-a11y",
    `${state.symbol} 最新收盘 ${formatNumber(latest.close, 2)}，区间高点 ${formatNumber(max, 2)}，区间低点 ${formatNumber(min, 2)}。`,
  );
}

function chartStorageKey() {
  return `openperpdesk.chart-annotations.${state.symbol}.${state.bar}`;
}

function loadChartAnnotations() {
  state.chartDraft = null;
  state.chartSelection = -1;
  state.chartUndo = [];
  try {
    const value = JSON.parse(localStorage.getItem(chartStorageKey()) || "[]");
    const point = item => item && Number.isFinite(item.time) && item.time > 0
      && item.time <= 8640000000000000 && Number.isFinite(item.price) && item.price > 0;
    state.chartAnnotations = Array.isArray(value) ? value.slice(0, 200).filter(item =>
      item && (
        item.type === "horizontal" && Number.isFinite(item.price) && item.price > 0
        || item.type === "trend" && point(item.start) && point(item.end)
        || item.type === "text" && point(item) && typeof item.text === "string" && item.text.length <= 80
      )) : [];
  } catch {
    state.chartAnnotations = [];
  }
  updateChartAnnotationControls();
}

function saveChartAnnotations() {
  try {
    localStorage.setItem(chartStorageKey(), JSON.stringify(state.chartAnnotations));
  } catch {
    setMessage("标记保留在当前页面；浏览器存储不可用，刷新后可能丢失。", "error");
  }
  updateChartAnnotationControls();
}

function rememberChartAnnotations() {
  state.chartUndo.push(structuredClone(state.chartAnnotations));
  if (state.chartUndo.length > 20) state.chartUndo.shift();
}

function updateChartAnnotationControls() {
  const selected = state.chartAnnotations[state.chartSelection];
  for (const id of ["edit-chart-annotation", "delete-chart-annotation"]) {
    if ($(`#${id}`)) $(`#${id}`).disabled = !selected;
  }
  if ($("#undo-chart-annotation")) $("#undo-chart-annotation").disabled = !state.chartUndo.length;
  if ($("#clear-chart-annotations")) $("#clear-chart-annotations").disabled = !state.chartAnnotations.length;
  const list = $("#chart-annotation-list");
  if (list) {
    const names = { horizontal: "水平线", trend: "趋势线", text: "文字" };
    list.replaceChildren(new Option("图表标记", "-1"), ...state.chartAnnotations.map((item, index) =>
      new Option(`${index + 1}. ${names[item.type]}${item.type === "text" ? ` · ${item.text}` : ""}`, String(index))));
    list.value = String(state.chartSelection);
  }
}

function chartTimeToX(time, geometry) {
  const candles = geometry.candles;
  let right = candles.findIndex(candle => candle.time >= time);
  if (right < 0) right = candles.length - 1;
  right = Math.max(1, right);
  const left = right - 1;
  return geometry.xFor(left + (time - candles[left].time) / (candles[right].time - candles[left].time || 1));
}

function setChartTool(tool) {
  if (!["cursor", "horizontal", "trend", "text"].includes(tool)) return;
  state.chartTool = tool;
  state.chartDraft = null;
  document.querySelectorAll("[data-chart-tool]").forEach((button) => {
    button.setAttribute("aria-pressed", String(button.dataset.chartTool === tool));
  });
  const canvas = $("#price-chart");
  if (canvas) canvas.style.cursor = tool === "cursor" ? "crosshair" : "cell";
  if (state.lastCandles) renderChart(state.lastCandles);
}

function drawChartAnnotations(context, geometry) {
  const colors = getComputedStyle(document.documentElement);
  const accent = colors.getPropertyValue("--accent").trim();
  const warning = colors.getPropertyValue("--warning").trim() || "#f0b44d";
  const muted = colors.getPropertyValue("--muted").trim();
  const timeToX = time => chartTimeToX(Number(time), geometry);
  const yFor = (price) => geometry.yFor(Number(price));
  context.save();
  context.beginPath();
  context.rect(geometry.padding.left, geometry.padding.top, geometry.chartWidth, geometry.chartHeight);
  context.clip();
  context.lineWidth = 1.2;
  state.chartAnnotations.forEach((annotation, index) => {
    context.lineWidth = index === state.chartSelection ? 2.5 : 1.2;
    if (annotation.type === "horizontal") {
      const y = yFor(annotation.price);
      if (y < geometry.padding.top || y > geometry.padding.top + geometry.chartHeight) return;
      context.strokeStyle = warning;
      context.setLineDash([6, 4]);
      context.beginPath();
      context.moveTo(geometry.padding.left, y);
      context.lineTo(geometry.padding.left + geometry.chartWidth, y);
      context.stroke();
      context.setLineDash([]);
      context.fillStyle = warning;
      context.font = "10px SFMono-Regular, Consolas, monospace";
      context.textAlign = "right";
      context.fillText(formatNumber(annotation.price, 2), geometry.padding.left + geometry.chartWidth - 8, Math.max(geometry.padding.top + 12, y - 5));
      context.textAlign = "left";
    } else if (annotation.type === "trend") {
      context.strokeStyle = accent;
      context.setLineDash([]);
      context.beginPath();
      context.moveTo(timeToX(annotation.start.time), yFor(annotation.start.price));
      context.lineTo(timeToX(annotation.end.time), yFor(annotation.end.price));
      context.stroke();
    } else if (annotation.type === "text") {
      const x = timeToX(annotation.time);
      const y = yFor(annotation.price);
      context.font = "11px -apple-system, BlinkMacSystemFont, sans-serif";
      const width = Math.min(geometry.chartWidth - 12, context.measureText(annotation.text).width + 12);
      const boxX = Math.max(geometry.padding.left, Math.min(x + 6, geometry.padding.left + geometry.chartWidth - width));
      const boxY = Math.max(geometry.padding.top, y - 20);
      if (x < geometry.padding.left || x > geometry.padding.left + geometry.chartWidth
          || y < geometry.padding.top || y > geometry.padding.top + geometry.chartHeight) return;
      context.fillStyle = colors.getPropertyValue("--surface-raised").trim();
      context.strokeStyle = muted;
      context.fillRect(boxX, boxY, width, 20);
      context.strokeRect(boxX, boxY, width, 20);
      context.fillStyle = colors.getPropertyValue("--text").trim();
      let text = annotation.text;
      if (context.measureText(text).width > width - 12) {
        while (text.length && context.measureText(`${text}…`).width > width - 12) text = text.slice(0, -1);
      }
      context.fillText(text === annotation.text ? text : `${text}…`, boxX + 6, boxY + 13);
    }
  });
  if (state.chartDraft?.type === "trend") {
    context.strokeStyle = muted;
    context.setLineDash([4, 4]);
    context.beginPath();
    context.moveTo(timeToX(state.chartDraft.start.time), yFor(state.chartDraft.start.price));
    context.lineTo(timeToX(state.chartDraft.end.time), yFor(state.chartDraft.end.price));
    context.stroke();
  }
  context.restore();
}

function chartPointFromEvent(event, geometry = state.chartGeometry) {
  const canvas = $("#price-chart");
  if (!geometry || !canvas || !geometry.candles.length) return null;
  const rect = canvas.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  if (x < geometry.padding.left || x > geometry.padding.left + geometry.chartWidth
      || y < geometry.padding.top || y > geometry.padding.top + geometry.chartHeight) return null;
  const index = Math.max(0, Math.min(
    geometry.candles.length - 1,
    Math.floor((x - geometry.padding.left) / geometry.chartWidth * geometry.candles.length),
  ));
  const candle = geometry.candles[index];
  const prices = geometry.candles.flatMap(item => [item.high, item.low]);
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const range = max - min || 1;
  return {
    time: candle.time,
    price: min + (1 - (y - geometry.padding.top) / geometry.chartHeight) * range,
  };
}

function addChartAnnotation(event) {
  const point = chartPointFromEvent(event);
  if (!point) return;
  addChartAnnotationAtPoint(point);
}

function addChartAnnotationAtPoint(point) {
  if (state.chartAnnotations.length >= 200) {
    setMessage("当前合约周期最多保留 200 个标记，请先删除部分标记。", "error");
    return;
  }
  if (state.chartTool === "horizontal") {
    rememberChartAnnotations();
    state.chartAnnotations.push({ type: "horizontal", price: point.price });
    state.chartSelection = state.chartAnnotations.length - 1;
    saveChartAnnotations();
    setChartTool("cursor");
  } else if (state.chartTool === "trend") {
    if (!state.chartDraft) {
      state.chartDraft = { type: "trend", start: point, end: point };
      renderChart(state.lastCandles);
    } else {
      rememberChartAnnotations();
      state.chartAnnotations.push({
        type: "trend",
        start: state.chartDraft.start,
        end: point,
      });
      state.chartDraft = null;
      state.chartSelection = state.chartAnnotations.length - 1;
      saveChartAnnotations();
      setChartTool("cursor");
    }
  } else if (state.chartTool === "text") {
    openChartAnnotationEditor({ type: "text", ...point, text: "" }, -1);
  }
}

function openChartAnnotationEditor(annotation, index) {
  if (!annotation) return;
  state.chartEditor = { annotation: structuredClone(annotation), index, key: chartStorageKey() };
  $("#chart-note-price").value = String(annotation.price ?? annotation.start.price);
  $("#chart-note-end-price").value = String(annotation.end?.price || 1);
  $("#chart-note-end-field").hidden = annotation.type !== "trend";
  $("#chart-note-field").hidden = annotation.type !== "text";
  $("#chart-note-text").required = annotation.type === "text";
  $("#chart-note-text").value = annotation.text || "";
  $("#chart-note-dialog").showModal();
  $(annotation.type === "text" ? "#chart-note-text" : "#chart-note-price").focus();
}

function hitChartAnnotation(point, geometry) {
  const x = chartTimeToX(point.time, geometry);
  const y = geometry.yFor(point.price);
  for (let index = state.chartAnnotations.length - 1; index >= 0; index--) {
    const item = state.chartAnnotations[index];
    if (item.type === "horizontal" && Math.abs(y - geometry.yFor(item.price)) < 12) return index;
    if (item.type === "text"
        && Math.abs(x - chartTimeToX(item.time, geometry)) < 40 && Math.abs(y - geometry.yFor(item.price)) < 24) return index;
    if (item.type === "trend") {
      const ax = chartTimeToX(item.start.time, geometry), ay = geometry.yFor(item.start.price);
      const bx = chartTimeToX(item.end.time, geometry), by = geometry.yFor(item.end.price);
      const length = (bx - ax) ** 2 + (by - ay) ** 2;
      const position = length ? Math.max(0, Math.min(1, ((x - ax) * (bx - ax) + (y - ay) * (by - ay)) / length)) : 0;
      if (Math.hypot(x - ax - position * (bx - ax), y - ay - position * (by - ay)) < 12) return index;
    }
  }
  return -1;
}

function renderCandleReadout(candle) {
  setText("#candle-time", candle ? `${new Date(candle.time).toISOString().slice(5, 16).replace("T", " ")} UTC` : "--");
  for (const field of ["open", "high", "low", "close"]) {
    setText(`#candle-${field}`, formatNumber(candle?.[field], 2));
  }
}

function showChartCursor(index) {
  const geometry = state.chartGeometry;
  const cursor = $("#chart-cursor");
  if (!geometry) return;
  const valid = index >= 0 && index < geometry.candles.length;
  cursor.hidden = !valid;
  const candle = valid ? geometry.candles[index] : geometry.candles.at(-1);
  state.chartCursorTime = valid ? candle.time : null;
  if (valid) cursor.style.left = `${geometry.padding.left + (index + .5) * geometry.chartWidth / geometry.candles.length}px`;
  renderCandleReadout(candle);
}

function renderWatchlist(tickers) {
  state.lastTickers = tickers || {};
  const current = tickers?.[state.symbol]?.data || {};
  const last = Number(current.last);
  const open = Number(current.sodUtc8);
  const change = Number.isFinite(last) && Number.isFinite(open) && open
    ? ((last - open) / open) * 100
    : null;
  setText("#chart-symbol", state.symbol);
  setText("#markets-title", state.symbol.split("-").slice(0, 2).join(" / "));
  setText("#heading-symbol", state.symbol);
  setText("#ticket-symbol", state.symbol);
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
  setText("#best-bid", formatNumber(current.bidPx, 2));
  setText("#best-ask", formatNumber(current.askPx, 2));
  setText("#market-updated", formatTime(tickers?.[state.symbol]?.received_at));
  setText("#ribbon-sync", formatTime(tickers?.[state.symbol]?.received_at));
  setState("#market-price", formatNumber(current.last, 2), "neutral");

  const watchlist = $("#watchlist");
  if (watchlist) {
    const symbols = [...new Set(["BTC-USDT-SWAP", "ETH-USDT-SWAP", ...Object.keys(tickers || {})])];
    const visibleSymbols = symbols.length ? symbols : ["BTC-USDT-SWAP", "ETH-USDT-SWAP"];
    for (const item of [...watchlist.children]) {
      if (!visibleSymbols.includes(item.dataset.symbol)) item.remove();
    }
    visibleSymbols.forEach((symbol) => {
      const ticker = tickers?.[symbol]?.data || {};
      const tickerLast = Number(ticker.last);
      const tickerOpen = Number(ticker.sodUtc8);
      const tickerChange = Number.isFinite(tickerLast) && Number.isFinite(tickerOpen) && tickerOpen
        ? ((tickerLast - tickerOpen) / tickerOpen) * 100
        : null;
      const shortName = symbol.split("-")[0];
      const isActive = symbol === state.symbol;
      const freshness = tickers?.[symbol]?.fresh === false ? "延迟"
        : tickers?.[symbol]?.received_at ? "在线" : "等待";
      const changeLabel = tickerChange === null
        ? "等待行情"
        : `${tickerChange >= 0 ? "+" : ""}${tickerChange.toFixed(2)}%`;
      let button = [...watchlist.children].find(item => item.dataset.symbol === symbol);
      if (!button) {
        button = document.createElement("button");
        button.type = "button";
        button.dataset.symbol = symbol;
        button.innerHTML = '<span class="watch-item-top"><strong></strong><span class="watch-item-state"></span></span><span class="watch-item-price mono"></span><span class="watch-item-change mono"></span>';
        watchlist.append(button);
      }
      button.className = `watch-item${isActive ? " is-active" : ""}`;
      button.setAttribute("aria-pressed", String(isActive));
      for (const [selector, text] of [
        ["strong", shortName], [".watch-item-state", freshness],
        [".watch-item-price", formatNumber(ticker.last, 2)], [".watch-item-change", changeLabel],
      ]) {
        const element = button.querySelector(selector);
        if (element.textContent !== text) element.textContent = text;
      }
      button.querySelector(".watch-item-change").className = `watch-item-change mono${tickerChange !== null && tickerChange >= 0 ? " change-positive" : tickerChange !== null ? " change-negative" : ""}`;
    });
  }
}

function renderMarketOverview(overview) {
  if (overview) state.marketOverview = overview;
  const snapshot = state.marketFeedState === "open" && !state.marketPaused ? state.marketStream : null;
  const funding = snapshot?.funding_rate?.[state.symbol]?.fresh
    ? snapshot.funding_rate[state.symbol].data : state.marketOverview?.funding_rate || {};
  const openInterest = snapshot?.open_interest?.[state.symbol]?.fresh
    ? snapshot.open_interest[state.symbol].data : state.marketOverview?.open_interest || {};
  const fundingRate = funding.fundingRate == null || funding.fundingRate === "" ? NaN : Number(funding.fundingRate);
  const oi = Number(openInterest.oi || openInterest.oiCcy || NaN);
  setText(
    "#market-funding",
    Number.isFinite(fundingRate) ? `${(fundingRate * 100).toFixed(4)}%` : "--",
  );
  const compactOi = oi >= 1e8 ? `${formatNumber(oi / 1e8, 2)} 亿` : oi >= 1e4 ? `${formatNumber(oi / 1e4, 2)} 万` : formatNumber(oi, 2);
  setText("#market-oi-label", openInterest.oi ? "持仓量 · 张" : "持仓量");
  setText("#market-oi", compactOi);
  $("#market-oi").title = Number.isFinite(oi) ? `${formatNumber(oi, 2)}${openInterest.oi ? " 张" : ""}` : "暂无数据";
  $("#market-oi").setAttribute("aria-label", $("#market-oi").title);
}

function renderAnalysis(analysis) {
  state.analysis = analysis;
  clearPreflight();
  const signal = analysis?.signal || {};
  const indicators = analysis?.indicators || {};
  const config = analysis?.config || {};
  const action = signal.action || "hold";
  const actionLabel = { open_long: "开多", open_short: "开空", close: "平仓", hold: "观望" }[action] || action;
  const actionClass = action === "open_long" ? "long" : action === "open_short" ? "short" : "hold";
  const actionElement = $("#analysis-action");
  actionElement.textContent = actionLabel;
  actionElement.className = `signal ${actionClass}`;
  setText("#analysis-source", ({ "structured-technical": "技术策略", "structured": "技术策略" })[analysis?.source] || analysis?.source || "暂无信号");
  setText("#analysis-kind", "结构化策略");
  setText("#analysis-bias", researchBiases[analysis?.bias] || analysis?.bias || "等待分析");
  setText("#analysis-summary", analysis?.report?.summary || "暂无策略信号");
  setText("#analysis-context", analysis ? "研究证据：结构化策略使用当前 OKX K 线数据。" : "OKX 研究数据尚未读取");
  setText("#indicator-rsi-label", `RSI ${config.rsi_period || 14}`);
  setText("#indicator-fast-label", `SMA ${config.fast_period || 9}`);
  setText("#indicator-slow-label", `SMA ${config.slow_period || 21}`);
  setText("#indicator-rsi", formatNumber(indicators.rsi ?? indicators.rsi_14, 2));
  setText("#indicator-fast", formatNumber(indicators.sma_fast ?? indicators.sma_9, 2));
  setText("#indicator-slow", formatNumber(indicators.sma_slow ?? indicators.sma_21, 2));
  setText("#indicator-confidence", signal.confidence == null ? "--" : `${(Number(signal.confidence) * 100).toFixed(1)}%`);
  setText("#signal-entry", formatNumber(signal.entry_price, 2));
  setText("#signal-tp", formatNumber(signal.take_profit, 2));
  setText("#signal-sl", formatNumber(signal.stop_loss, 2));
  $("#preview-signal").disabled = !state.token || !analysis?.signal;
  $("#execute-signal").disabled = (
    state.submitting
    || !state.token
    || !analysis?.signal
    || !executionGateOpen(state.status)
  );
}

const executionReasons = {
  order_submission_unconfirmed: "交易所状态待确认，请先对账，不要重复发单",
  idempotency_payload_conflict: "此信号已用于不同的订单参数",
  previous_order_not_accepted: "此前订单未被接受",
  exchange_preflight_required: "交易所校验尚未配置",
  execution_disabled: "服务器执行开关已关闭",
  emergency_stop_active: "急停已触发",
  private_account_not_configured: "OKX 私有账户未配置",
  exchange_preflight_data_unavailable: "交易所校验数据读取失败",
  account_snapshot_empty: "账户快照为空",
  account_currency_valuation_unavailable: "账户币种估值暂不可用",
  bill_currency_valuation_unavailable: "账单币种缺少可靠估值，已停止放行",
  bill_type_unsupported: "存在尚未支持的账户账单类型，需先核对",
  bill_balance_change_ambiguous: "账单的账户与仓位余额变动不一致",
  bill_funding_subtype_unknown: "资金费账单类型无法识别",
  bill_funding_sign_invalid: "资金费收支方向与金额不一致",
  bill_funding_fee_ambiguous: "资金费账单包含无法确认的额外费用",
  bill_id_conflict: "重复账单内容冲突",
  bill_timestamp_invalid: "账户账单时间无效",
  account_day_changed: "校验期间已跨 UTC 日期，请重新核验",
  account_equity_invalid: "账户权益无效",
  market_data_stale: "行情已过期，请刷新",
  signal_expired: "信号已过期，请重新分析",
  signal_price_deviation: "信号价格偏离最新行情",
  hold_signal: "当前信号为观望，不下单",
  daily_loss_limit_reached: "已达到日内亏损限制",
  confidence_below_threshold: "信号置信度低于要求",
  leverage_above_limit: "杠杆超过风控上限",
  position_size_above_limit: "仓位比例超过上限",
  order_notional_above_signal_budget: "本单名义价值超过信号预算",
  total_exposure_above_limit: "总名义敞口超过上限",
  order_size_outside_contract_steps: "委托张数不符合交易所最小数量或步长",
  order_size_above_exchange_limit: "委托张数超过交易所上限",
  protective_prices_invalid_at_current_market: "止盈止损价格与最新行情不匹配",
  close_position_missing_or_ambiguous: "可平持仓不存在或无法唯一确定",
  close_size_above_position: "平仓数量超过持仓",
  close_size_above_available_position: "平仓数量超过可用持仓",
  execution_budget_snapshot_changed: "敞口快照已变化，请重新核验",
  unreconciled_order_exposure: "未确认订单尚未完成对账",
  unvalued_algo_order_exposure: "存在可能增加仓位的算法委托，敞口尚未核清",
  algo_order_identity_unavailable: "算法委托缺少交易所编号",
  active_order_account_scope_mismatch: "活动订单所属账户与当前 OKX 配置不一致",
  unrealized_pnl_invalid: "交易所持仓浮动盈亏缺失或无效",
};

function clearPreflight() {
  state.draftRevision += 1;
  setState("#preflight-state", "未核验", "neutral");
  for (const field of ["basis", "notional", "exposure"]) setText(`#preflight-${field}`, "--");
  setState("#preflight-message", "尚无核验结果", "neutral");
}

function renderPreflight(result) {
  const preflight = result.preflight || result.order?.raw?.preflight || {};
  const exchange = preflight.basis === "exchange";
  setState("#preflight-state", result.accepted ? "已通过" : "未通过", result.accepted ? "good" : "danger");
  setText("#preflight-basis", exchange ? "交易所快照" : preflight.basis === "simulation" ? "模拟参数" : "--");
  setText("#preflight-notional", exchange ? `${formatNumber(preflight.order_notional, 2)} USD` : "--");
  setText("#preflight-exposure", exchange ? `${formatNumber(preflight.current_notional, 2)} USD` : "--");
  setState("#preflight-message", result.accepted
    ? result.dry_run ? (exchange ? "交易所数据核验通过，未发单" : "仅模拟参数通过，未校验交易所账户") : "订单已受理，成交状态以交易所回报为准"
    : (result.reasons || []).map(reason => executionReasons[reason] || `校验代码：${reason}`).join("；") || "订单核验未通过",
  result.accepted ? "good" : "danger");
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
  updateBacktestContext();
}

function updatePrivateActionAvailability() {
  const unlocked = Boolean(state.token);
  const worker = state.status?.automation_worker || {};
  const integrations = state.status?.integrations || {};
  const executionAllowed = state.status?.safety_control?.execution_allowed !== false;
  $(".private-access").classList.toggle("is-unlocked", unlocked);
  document.querySelectorAll(".ticket-lock [data-open-auth]").forEach(button => { button.hidden = unlocked; });
  $("#run-analysis").disabled = !unlocked;
  $("#run-backtest").disabled = !unlocked;
  $("#refresh-activity").disabled = !unlocked;
  document.querySelectorAll("#backtest-form input, #backtest-form select").forEach(input => {
    input.disabled = !unlocked;
  });
  $("#activity-query").disabled = !unlocked || state.activityRows === null;
  $("#activity-severity").disabled = !unlocked || state.activityRows === null;
  $("#clear-activity").disabled = !unlocked || (!$("#activity-query").value && $("#activity-severity").value === "all");
  if (!unlocked) resetManagementAccess();
  $("#run-ai-analysis").disabled = !unlocked || !integrations.tradingagents_configured;
  $("#run-worker").disabled = !unlocked || worker.enabled !== true || !executionAllowed;
  $("#test-notification").disabled = !unlocked || !integrations.pushplus_configured;
  $("#refresh-private").disabled = !unlocked;
  $("#sync-ledger").disabled = !unlocked;
  $("#save-strategy").disabled = !unlocked;
  $("#toggle-worker").disabled = !unlocked;
  $("#emergency-stop").disabled = !unlocked;
  $("#resume-trading").disabled = !unlocked;
  $("#unlock-live").disabled = !unlocked
    || state.status?.live_safety?.configuration_enabled !== true
    || state.status?.live_safety?.mode_is_live !== true;
  $("#lock-live").disabled = !unlocked;
  document.querySelectorAll("button[aria-busy='true']").forEach((button) => {
    button.disabled = true;
  });
  updateResearchAvailability();
  updateBillHistoryAvailability();
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
          ? "自动策略已允许，仍受全局开关和模拟盘闸门约束。"
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
    state.strategyDirty = false;
    setText("#strategy-message", "策略参数已保存，下一次分析和自动运行将读取新配置。");
    setMessage("策略配置已更新", "good");
  } catch (error) {
    setText("#strategy-message", `策略保存失败：${error.message}`);
  } finally {
    setBusy("#save-strategy", false);
  }
}

function filteredRecords(kind, rows) {
  state.records[kind] = rows;
  const input = $(`#${kind}-query`);
  input.disabled = false;
  const query = input.value.trim().toLowerCase();
  const orderStatus = $("#orders-status");
  if (kind === "orders") orderStatus.disabled = false;
  const filtered = rows.filter(row => {
    const searchable = `${row.inst_id || ""} ${row.client_order_id || ""}`.toLowerCase();
    if (query && !searchable.includes(query)) return false;
    if (kind !== "orders" || orderStatus.value === "all") return true;
    if (orderStatus.value === "active") return !terminalOrderStatuses.has(row.status);
    if (orderStatus.value === "attention") return ["failed", "rejected", "cancel_failed", "unknown", "submission_unknown", "order_failed"].includes(row.status);
    return row.status === orderStatus.value;
  });
  setText(`#${kind}-filter-count`, `已载入 ${rows.length} 条${filtered.length !== rows.length ? ` · 匹配 ${filtered.length} 条` : ""}`);
  return filtered;
}

function emptyRecordRow(kind, hasRecords, emptyMessage) {
  return `<tr><td colspan="7" class="table-empty">${hasRecords
    ? `<strong>没有匹配的记录</strong><button class="record-clear" type="button" data-clear-records="${kind}">清除筛选</button>`
    : emptyMessage}</td></tr>`;
}

function statusTone(status) {
  if (["filled", "open"].includes(status)) return "good";
  if (["failed", "rejected", "cancel_failed", "order_failed"].includes(status)) return "danger";
  if (["unknown", "submission_unknown", "preparing", "submitting", "partially_filled"].includes(status)) return "warning";
  return "neutral";
}

function renderPositions(rows) {
  const body = $("#positions-body");
  const all = rows || [];
  setText("#ledger-count-positions", all.length);
  setText("#metric-positions", all.length);
  const filtered = filteredRecords("positions", all);
  if (!filtered.length) {
    body.innerHTML = emptyRecordRow("positions", all.length > 0, "暂无本地活动持仓");
    return;
  }
  body.innerHTML = filtered.map((row) => `
    <tr>
      <td class="mono-cell">${escapeHtml(row.inst_id)}</td>
      <td>${escapeHtml(({ long: "多仓", short: "空仓", net: "净持仓" })[row.pos_side] || row.pos_side)}</td>
      <td class="mono-cell">${formatNumber(row.size)}</td>
      <td class="mono-cell">${formatNumber(row.entry_price, 2)}</td>
      <td class="mono-cell">${formatNumber(row.notional, 2)}</td>
      <td class="mono-cell">${formatNumber(row.take_profit, 2)} / ${formatNumber(row.stop_loss, 2)}</td>
      <td><span class="record-state" data-tone="${statusTone(row.status)}">${escapeHtml(statusLabel(row.status))}</span></td>
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
  const all = rows || [];
  setText("#ledger-count-orders", all.length);
  const filtered = filteredRecords("orders", all);
  if (!filtered.length) {
    body.innerHTML = emptyRecordRow("orders", all.length > 0, "暂无本地订单");
    return;
  }
  body.innerHTML = filtered.map((row) => `
    <tr>
      <td class="mono-cell">${formatTime(row.created_at)}</td>
      <td class="mono-cell">${escapeHtml(row.inst_id)}</td>
      <td>${escapeHtml(({ buy: "买入", sell: "卖出" })[row.side] || row.side)}</td>
      <td class="mono-cell">${formatNumber(row.size)}</td>
      <td><span class="record-state" data-tone="${statusTone(row.status)}">${escapeHtml(statusLabel(row.status))}</span></td>
      <td class="mono-cell">${escapeHtml(row.client_order_id)}</td>
      <td>${terminalOrderStatuses.has(row.status) || ["cancel_failed", "canceling"].includes(row.status)
        ? '<span class="subtle">--</span>'
        : `<button class="table-action cancel-order" type="button" data-client-order-id="${escapeHtml(row.client_order_id)}">撤单</button>`}</td>
    </tr>`).join("");
}

function statusLabel(status) {
  return ({
    live: "挂单中", open: "持仓中", closed: "已平仓", pending: "待处理",
    preparing: "准备中", submitting: "提交中", submitted: "已提交", accepted: "已受理",
    filled: "已成交", partially_filled: "部分成交", canceled: "已撤销",
    rejected: "已拒绝", failed: "失败", unknown: "待确认", submission_unknown: "提交结果待确认", preview: "预览",
    cancel_failed: "撤单失败", canceling: "撤单中",
    effective: "已触发", triggered: "已触发", order_failed: "触发失败", expired: "已过期",
    mmp_canceled: "保护撤单",
  })[status] || status;
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
      result.accepted ? "撤单请求已受理，等待交易所确认" : "撤单请求未被交易所接受",
      result.accepted ? "good" : "error",
    );
    await loadPrivate();
  } catch (error) {
    setMessage(`撤单失败：${error.message}`, "error");
  }
}

function renderFills(rows) {
  const body = $("#fills-body");
  const all = rows || [];
  setText("#ledger-count-fills", all.length);
  const filtered = filteredRecords("fills", all);
  if (!filtered.length) {
    body.innerHTML = emptyRecordRow("fills", all.length > 0, "暂无已同步成交");
    return;
  }
  body.innerHTML = filtered.map((row) => `
    <tr>
      <td class="mono-cell">${escapeHtml(row.filled_at)}</td>
      <td class="mono-cell">${escapeHtml(row.inst_id)}</td>
      <td>${escapeHtml(({ buy: "买入", sell: "卖出" })[row.side] || row.side)}</td>
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
  if (!rect.width) return;
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
  const padding = { top: 16, right: 78, bottom: 22, left: 8 };
  const chartWidth = width - padding.left - padding.right;
  const chartHeight = height - padding.top - padding.bottom;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const xFor = (index) => padding.left + (index / (points.length - 1)) * chartWidth;
  const yFor = (value) => padding.top + (1 - (value - min) / range) * chartHeight;

  const colors = getComputedStyle(document.documentElement);
  context.strokeStyle = colors.getPropertyValue("--border-soft").trim();
  context.lineWidth = 1;
  context.font = "10px SFMono-Regular, Consolas, monospace";
  context.fillStyle = colors.getPropertyValue("--muted").trim();
  context.textAlign = "left";
  for (let row = 0; row <= 3; row += 1) {
    const y = padding.top + row * chartHeight / 3;
    context.beginPath();
    context.moveTo(padding.left, y);
    context.lineTo(padding.left + chartWidth, y);
    context.stroke();
    context.fillText(formatNumber(max - row * range / 3, 2), padding.left + chartWidth + 8, y + 3);
  }
  context.strokeStyle = colors.getPropertyValue("--info").trim();
  context.lineWidth = 2;
  context.beginPath();
  points.forEach((point, index) => {
    const x = xFor(index);
    const y = yFor(point);
    if (index === 0) context.moveTo(x, y);
    else context.lineTo(x, y);
  });
  context.stroke();
  context.fillStyle = colors.getPropertyValue("--info").trim();
  context.beginPath();
  context.arc(xFor(points.length - 1), yFor(points.at(-1)), 3, 0, Math.PI * 2);
  context.fill();
}

function renderPerformance(report) {
  const data = report || {};
  state.performance = data;
  const returnPct = data.return_pct == null ? NaN : Number(data.return_pct);
  const netPnl = data.net_pnl == null ? NaN : Number(data.net_pnl);
  const drawdown = data.max_drawdown_pct == null ? NaN : Number(data.max_drawdown_pct);
  const valuationMissing = ["mixed_currency", "unresolved_currency", "invalid_amount"].includes(data.valuation_status);
  setText("#performance-basis", `成交账本${data.currency ? ` · ${data.currency}` : ""} · 不含资金费`);
  setText("#performance-empty", valuationMissing ? "缺少统一币种估值，暂不生成收益曲线" : "暂无可展示的权益曲线");
  setText("#performance-return", Number.isFinite(returnPct) ? `收益 ${returnPct.toFixed(2)}%` : "收益 --");
  setText("#performance-equity", formatNumber(data.ending_equity, 2));
  setText("#performance-net-pnl", formatNumber(netPnl, 2));
  setText("#performance-drawdown", Number.isFinite(drawdown) ? `${drawdown.toFixed(2)}%` : "--");
  setText("#performance-fills", formatNumber(data.fills, 0));
  setText("#performance-origin", `起算权益 ${formatNumber(data.initial_equity, 2)}${data.currency ? ` ${data.currency}` : ""}`);
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

function renderAccountBills(payload) {
  const summary = payload?.summary;
  const status = payload?.error ? "读取失败" : !payload?.configured ? "未配置" : !summary ? "尚未同步" : payload.fresh ? "已同步" : "快照过期";
  setState("#bills-status", status, payload?.error ? "danger" : payload?.fresh ? "good" : "warning");
  if (payload?.error) {
    if (!state.billSnapshot) {
      $("#bills-body").innerHTML = '<tr><td colspan="9" class="table-empty">账单读取失败</td></tr>';
    }
    return;
  }
  state.billSnapshot = payload;
  const currencies = Object.entries(summary?.by_currency || {});
  $("#bills-summary").innerHTML = currencies.length
    ? `<span class="subtle">${escapeHtml(summary.day_utc)} UTC</span>` + currencies.map(([currency, totals]) =>
      `<span>${escapeHtml(currency)} 净变动<strong>${escapeHtml(totals.net_pnl)}</strong><span class="subtle"> · 资金费 ${escapeHtml(totals.funding)}</span></span>`).join("")
    : '<span class="subtle">UTC 当日 · 原币种金额</span>';
  const rows = payload?.data || [];
  const kinds = { trade: "交易", liquidation: "强平", adl: "自动减仓", funding: "资金费", interest: "利息", clawback: "分摊扣款", transfer: "划转（不计收益）" };
  $("#bills-body").innerHTML = rows.length ? rows.map(row => `
    <tr>
      <td class="mono-cell">${escapeHtml(new Date(row.timestamp_ms).toISOString().slice(0, 19).replace("T", " "))}</td>
      <td>${escapeHtml(kinds[row.kind] || row.kind)}</td>
      <td class="mono-cell">${escapeHtml(row.inst_id || "--")}</td>
      <td class="mono-cell">${escapeHtml(row.currency)}</td>
      ${["realized_pnl", "fees", "funding", "adjustments", "net_pnl"].map(field => `<td class="mono-cell">${escapeHtml(row[field] ?? "--")}</td>`).join("")}
    </tr>`).join("")
    : `<tr><td colspan="9" class="table-empty">${payload?.error ? "账单读取失败，现有数据未被替换" : payload?.configured ? "暂无已同步账单" : "OKX 私有凭据未配置"}</td></tr>`;
  if (payload?.has_more) {
    $("#bills-body").insertAdjacentHTML("beforeend", '<tr><td colspan="9" class="table-empty">仅展示最近记录，当日汇总包含全部已同步账单</td></tr>');
  }
}

function activityLevel(value) {
  const level = String(value || "").toLowerCase();
  if (["error", "critical", "fatal"].includes(level)) return "error";
  if (["warn", "warning"].includes(level)) return "warning";
  return level === "info" ? "info" : "other";
}

function renderActivity(rows) {
  state.activityRows = Array.isArray(rows) ? rows : [];
  const all = state.activityRows;
  const query = $("#activity-query").value.trim().toLowerCase();
  const severity = $("#activity-severity").value;
  const filtered = all.filter(row => (severity === "all" || activityLevel(row.severity) === severity)
    && (!query || `${row.event_type || ""} ${row.message || ""}`.toLowerCase().includes(query)));
  setText("#activity-total", all.length);
  setText("#activity-errors", all.filter(row => activityLevel(row.severity) === "error").length);
  setText("#activity-warnings", all.filter(row => activityLevel(row.severity) === "warning").length);
  const latest = all.reduce((value, row) => {
    const timestamp = Date.parse(row.created_at);
    return Number.isFinite(timestamp) && (value === null || timestamp > value) ? timestamp : value;
  }, null);
  setText("#activity-latest", latest === null ? "--" : formatTime(new Date(latest).toISOString()));
  setText("#activity-filter-count", `已载入 ${all.length} 条 · 匹配 ${filtered.length} 条`);
  $("#activity-query").disabled = !state.token;
  $("#activity-severity").disabled = !state.token;
  $("#clear-activity").disabled = !state.token || (!query && severity === "all");
  const list = $("#activity-list");
  list.closest("#activity").classList.toggle("audit-empty", !filtered.length);
  if (!filtered.length) {
    list.innerHTML = `<div class="table-empty"><i data-icon="scroll-text" aria-hidden="true"></i><strong>${all.length ? "没有匹配的事件" : "暂无审计事件"}</strong></div>`;
    renderIcons(list);
    return;
  }
  const labels = { error: "错误", warning: "警告", info: "信息", other: "其他" };
  const tones = { error: "danger", warning: "warning", info: "neutral", other: "neutral" };
  list.innerHTML = filtered.map((row) => `
    <article class="activity-row">
      <span class="mono subtle">${escapeHtml(formatTime(row.created_at))}</span>
      <div class="activity-identity"><span class="record-state" data-tone="${tones[activityLevel(row.severity)]}">${labels[activityLevel(row.severity)]}</span><span class="event">${escapeHtml(row.event_type)}</span></div>
      <p class="activity-message-text">${escapeHtml(row.message)}</p>
    </article>`).join("");
}

function resetManagementAccess() {
  state.activityRows = null;
  state.activityRequest += 1;
  state.backtest = null;
  state.backtestRequest += 1;
  $("#backtest-result").hidden = true;
  setState("#backtest-status", "管理员未解锁", "neutral");
  setText("#backtest-result-context", "");
  ["return", "drawdown", "final", "trades", "win-rate", "points"].forEach(name => setText(`#backtest-${name}`, "--"));
  ["total", "errors", "warnings", "latest"].forEach(name => setText(`#activity-${name}`, "--"));
  setText("#activity-filter-count", "账户未解锁");
  $("#activity-list").innerHTML = '<div class="table-empty"><i data-icon="scroll-text" aria-hidden="true"></i><strong>审计事件已锁定</strong></div>';
  $("#activity").classList.add("audit-empty");
  $("#activity-message").hidden = true;
  renderIcons($("#activity-list"));
}

async function loadActivity() {
  if (!state.token || $("#refresh-activity").hasAttribute("aria-busy")) return;
  const token = state.token;
  const request = ++state.activityRequest;
  setBusy("#refresh-activity", true);
  $("#activity-message").hidden = false;
  setState("#activity-message", "正在读取审计事件...", "neutral");
  try {
    const payload = await api("/api/v1/activity");
    if (request !== state.activityRequest || token !== state.token) return;
    if (!Array.isArray(payload.data)) throw new Error("审计数据格式无效");
    renderActivity(payload.data);
    $("#activity-message").hidden = true;
  } catch (error) {
    if (request !== state.activityRequest || token !== state.token) return;
    setState("#activity-message", `读取失败：${error.message}${state.activityRows ? "；保留上次记录" : ""}`, "danger");
  } finally {
    setBusy("#refresh-activity", false);
    updatePrivateActionAvailability();
  }
}

function executionGateOpen(status) {
  return status?.execution_enabled === true
    && !state.marketPaused
    && state.marketFeedState === "open"
    && state.marketStream?.tickers?.[state.symbol]?.fresh === true
    && state.controlFeedState === "open"
    && state.controlSnapshotFresh
    && status.risk_engine_ready === true
    && status.safety_control?.emergency_stopped !== true
    && status.safety_control?.execution_allowed === true
    && (status.trading_mode === "demo" || (status.trading_mode === "live" && status.live_safety?.allowed === true));
}

function applyStatus(status) {
  state.status = status;
  const mode = String(status.trading_mode || "demo").toUpperCase();
  const modeLabel = mode === "LIVE" ? "实盘" : "模拟盘";
  const marketLabel = status.market_data_connected ? "在线" : "未接入";
  const worker = status.automation_worker || {};
  const workerLabel = worker.running ? "运行中" : worker.enabled ? "已启用" : "待命";
  const gateOpen = executionGateOpen(status);
  const executionLabel = status.safety_control?.emergency_stopped
    ? "急停已触发"
    : gateOpen ? mode === "LIVE" ? "实盘执行已解锁" : "模拟盘执行已启用" : "执行已锁定";
  setText("#trading-mode", modeLabel);
  const environment = status.environment || "development";
  setText("#top-environment", ({ development: "开发环境", production: "生产环境", test: "测试环境" })[environment] || environment);
  setText("#top-execution", executionLabel);
  setState("#mobile-execution", executionLabel, status.safety_control?.emergency_stopped ? "danger" : gateOpen ? "good" : "warning");
  setText("#execute-signal", mode === "LIVE" ? "提交实盘订单" : "提交模拟盘订单");
  setText("#state-mode", modeLabel);
  setState("#state-market", marketLabel, status.market_data_connected ? "good" : "warning");
  const publicStream = status.market_stream || {};
  const candlesKnown = typeof publicStream.candles_connected === "boolean";
  setState("#state-candle-stream", !candlesKnown ? "--" : publicStream.candles_fresh
    ? "在线" : publicStream.candles_connected ? "等待数据" : publicStream.candles_last_error ? "重连中" : "连接中",
  !candlesKnown ? "neutral" : publicStream.candles_fresh ? "good" : "warning");
  const accountStream = status.account_stream || {};
  const accountStreamReady = accountStream.connected && accountStream.authenticated;
  setState(
    "#state-account-stream",
    accountStreamReady
      ? "在线"
      : accountStream.configured
        ? accountStream.connected
          ? "认证中"
          : "连接中"
        : "未配置",
    accountStreamReady
      ? "good"
      : accountStream.configured
        ? "warning"
        : "neutral",
  );
  setState("#state-risk", status.risk_engine_ready ? "就绪" : "已锁定", status.risk_engine_ready ? "good" : "danger");
  setText(
    "#state-exposure-limit",
    `${formatNumber(status.risk_limits?.max_total_notional_pct, 2)}% 权益`,
  );
  const limits = status.risk_limits || {};
  const limitRows = [
    ["#limit-leverage", limits.max_leverage, "倍", 1],
    ["#limit-position", limits.max_position_pct, "%", 1],
    ["#limit-exposure", limits.max_total_notional_pct, "% 权益", 1],
    ["#limit-confidence", limits.min_confidence, "%", 100],
    ["#limit-daily-loss", limits.max_daily_loss_pct, "%", 1],
    ["#limit-stop-distance", limits.max_stop_distance_pct, "%", 1],
  ];
  for (const [selector, value, unit, multiplier] of limitRows) {
    const number = formatNumber(value) === "--" ? "--" : formatNumber(Number(value) * multiplier, 2);
    setText(selector, number === "--" ? number : `${number}${unit}`);
  }
  setState("#risk-worker-state", workerLabel, worker.running ? "good" : "neutral");
  setState("#risk-worker-mode", worker.dry_run === false ? "下单模式" : worker.dry_run === true ? "仅模拟计算" : "--", worker.dry_run === false ? "warning" : "neutral");
  setText("#risk-worker-interval", formatNumber(worker.interval_seconds) === "--" ? "--" : `${formatNumber(worker.interval_seconds)} 秒`);
  setState("#state-proxy", status.integrations?.outbound_proxy_configured ? "已配置" : "未配置", status.integrations?.outbound_proxy_configured ? "good" : "neutral");
  setState("#state-pushplus", status.integrations?.pushplus_configured ? "已配置" : "未配置", status.integrations?.pushplus_configured ? "good" : "neutral");
  setState("#state-ai", status.integrations?.tradingagents_configured ? "已配置" : "未启用", status.integrations?.tradingagents_configured ? "good" : "neutral");
  const algoStream = status.algo_stream || {};
  const algoStreamReady = algoStream.connected && algoStream.authenticated;
  setState(
    "#state-algo-stream",
    algoStreamReady
      ? "在线"
      : algoStream.configured
        ? algoStream.connected ? "认证中" : "连接中"
        : "未配置",
    algoStreamReady
      ? "good"
      : algoStream.configured
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
  setText("#execution-state", executionLabel);
  setText(
    "#execution-note",
    status.safety_control?.emergency_stopped
      ? "急停已触发"
      : gateOpen ? "风险闸门已打开" : "等待执行条件满足",
  );
  setState("#ticket-lock-label", status.safety_control?.emergency_stopped ? "急停已触发" : gateOpen ? `${modeLabel}执行已启用` : "执行已锁定", gateOpen ? "good" : "warning");
  setState("#metric-risk", status.safety_control?.emergency_stopped ? "已急停" : gateOpen ? "已启用" : "已锁定", status.safety_control?.emergency_stopped ? "danger" : gateOpen ? "good" : "warning");
  setText(
    "#metric-risk-note",
    status.safety?.live_orders_allowed ? "实盘已解锁" : "实盘默认禁止",
  );
  setText(
    "#operation-summary",
    `${modeLabel} · ${executionLabel}`,
  );
  setText("#ribbon-market", marketLabel);
  setText("#ribbon-worker", workerLabel);
  setText("#toggle-worker", worker.enabled ? "停止自动执行" : "启用模拟盘自动执行");
  $("#toggle-worker").classList.toggle("danger", worker.enabled);
  $("#toggle-worker").classList.toggle("secondary", !worker.enabled);
  if (!state.workerModeDirty) $("#worker-dry-run").checked = worker.dry_run !== false;
  setText("#ribbon-sync", formatTime(status.market_stream?.last_message_at));
  setState("#sidebar-market", marketLabel, status.market_data_connected ? "good" : "warning");
  setState("#sidebar-risk", status.risk_engine_ready ? "就绪" : "锁定", status.risk_engine_ready ? "good" : "danger");
  setState("#sidebar-worker", workerLabel, worker.running ? "good" : worker.enabled ? "warning" : "neutral");
  updatePrivateActionAvailability();
  $("#execute-signal").disabled = (
    state.submitting
    || !state.analysis?.signal
    || !gateOpen
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
  const button = path.endsWith("emergency-stop") ? "#emergency-stop" : "#resume-trading";
  if ($(button).hasAttribute("aria-busy")) return;
  setBusy(button, true);
  setMessage(path.endsWith("emergency-stop") ? "正在触发急停..." : "正在恢复执行闸门...");
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
  } finally {
    setBusy(button, false);
    updatePrivateActionAvailability();
  }
}

async function loadStatus() {
  const request = ++state.statusRequest;
  const updates = state.controlUpdates;
  try {
    const payload = await api("/api/v1/system/status");
    if (request !== state.statusRequest || updates !== state.controlUpdates) return;
    applyStatus(payload);
    setConnection(true);
  } catch {
    if (request !== state.statusRequest || updates !== state.controlUpdates) return;
    setConnection(false);
  }
}

async function loadMarket() {
  const request = ++state.marketRequest;
  const { symbol, bar } = state;
  setBusy("#refresh-market", true, "读取中...");
  try {
    const candlePayload = await api(`/api/v1/market/candles?inst_id=${encodeURIComponent(symbol)}&bar=${encodeURIComponent(bar)}&limit=100`);
    if (request !== state.marketRequest) return;
    renderChart(candlePayload.data);
    state.marketCandleKey = null;
    applyLiveCandle();
    updateMarketRefreshControl();
  } catch (error) {
    if (request !== state.marketRequest) return;
    updateMarketRefreshControl();
    if (!state.lastCandles?.length) {
      setText("#chart-empty", error.message);
      $("#chart-empty").hidden = false;
      $("#chart-cursor").hidden = true;
    }
  } finally {
    if (request === state.marketRequest) setBusy("#refresh-market", false);
  }
}

function updateMarketRefreshControl() {
  const button = $("#toggle-market-refresh");
  if (!button) return;
  const label = state.marketPaused ? "继续看盘" : "暂停看盘";
  button.setAttribute("aria-label", label);
  button.title = label;
  button.querySelector("[data-icon]").dataset.icon = state.marketPaused ? "play" : "pause";
  renderIcons(button);
  button.setAttribute("aria-pressed", String(state.marketPaused));
  const live = state.marketFeedState === "open";
  const fresh = state.marketStream?.tickers?.[state.symbol]?.fresh;
  const labelText = state.marketPaused ? "行情已暂停"
    : live ? fresh ? "已连接" : "行情延迟"
      : state.marketFeedState === "connecting" ? "连接中"
        : "行情连接中断";
  const tone = !state.marketPaused && live && fresh ? "good" : "warning";
  setText("#market-refresh-state", labelText);
  setState("#market-tag", labelText, tone);
  setText("#quote-refresh-label", labelText);
  document.querySelectorAll(".watch-item-state").forEach(element => {
    const record = state.marketStream?.tickers?.[element.closest("[data-symbol]").dataset.symbol];
    element.textContent = !live || state.marketPaused ? "暂停" : record?.fresh ? "在线" : "延迟";
  });
  if (state.status) applyStatus(state.status);
}

let marketFeed;
let controlFeed;
let privateFeed;

function applyLiveCandle() {
  if (state.marketPaused || state.marketFeedState !== "open" || !state.lastCandles?.length) return;
  const snapshot = state.marketStream;
  const record = snapshot?.bar === state.bar ? snapshot.candles?.[state.symbol] : null;
  const row = record?.data;
  if (!record?.fresh || !Array.isArray(row) || row.length < 6
      || !row.slice(0, 6).every(value => value !== "" && Number.isFinite(Number(value)))
      || Number(row[0]) < Number(state.lastCandles[0][0])) return;
  const key = `${state.symbol}:${state.bar}:${JSON.stringify(row)}`;
  if (key === state.marketCandleKey) return;
  state.marketCandleKey = key;
  const rows = state.lastCandles.filter(item => String(item[0]) !== String(row[0]));
  renderChart([row, ...rows].slice(0, 100));
}

function connectMarketFeed() {
  marketFeed?.close();
  marketFeed = null;
  state.marketFeedState = "connecting";
  state.marketStream = null;
  state.marketCandleKey = null;
  updateMarketRefreshControl();
  if (state.marketPaused || document.hidden) return;
  const bar = state.bar;
  marketFeed = openLiveStream(`/api/v1/market/events?bar=${encodeURIComponent(bar)}`, {
    onState(status) {
      if (status === "offline") state.marketHistoryNeedsSync = true;
      state.marketFeedState = status;
      updateMarketRefreshControl();
    },
    onEvent(event, snapshot) {
      if (event !== "market" || snapshot.bar !== state.bar || state.marketPaused
          || !snapshot.tickers || !snapshot.candles) return;
      state.marketStream = snapshot;
      renderWatchlist(snapshot.tickers);
      renderMarketOverview();
      applyLiveCandle();
      updateMarketRefreshControl();
      if (state.marketHistoryNeedsSync && snapshot.tickers[state.symbol]?.fresh) {
        state.marketHistoryNeedsSync = false;
        loadMarket();
      }
    },
  });
}

function connectControlFeed() {
  controlFeed?.close();
  if (document.hidden) return;
  controlFeed = openLiveStream("/api/v1/system/events", {
    onState(status) {
      state.controlFeedState = status;
      setConnection(status === "open");
      if (status !== "open") {
        state.controlSnapshotFresh = false;
        state.controlUpdates += 1;
        if (state.status) applyStatus(state.status);
        $("#execute-signal").disabled = true;
        setState("#top-execution", "连接待确认 · 禁止提交", "warning");
      }
    },
    onEvent(event, payload) {
      if (event === "status") {
        state.controlSnapshotFresh = true;
        state.controlUpdates += 1;
        applyStatus(payload);
      }
      if (event === "analysis_status") {
        researchState.runtimeRequest += 1;
        renderResearchStatus(payload);
      }
    },
  });
}

function lockPrivateAccess() {
  privateFeed?.close();
  privateFeed = null;
  state.privateFeedState = "locked";
  state.token = "";
  state.privateUpdates += 1;
  renderPositions([]);
  renderOrders([]);
  renderFills([]);
  renderPnl([]);
  renderPerformance({});
  renderAccountBills({ configured: false });
  setText("#metric-equity", "--");
  setText("#metric-equity-note", "管理员访问已锁定");
  setText("#positions-tag", "需要令牌");
  updatePrivateActionAvailability();
}

async function refreshLivePerformance() {
  const token = state.token;
  const request = ++state.performanceRequest;
  if (!token) return;
  try {
    const report = await api(`/api/v1/performance/report?initial_equity=${encodeURIComponent($("#equity-input").value)}`);
    if (token !== state.token || request !== state.performanceRequest) return;
    renderPerformance(report.data);
    setText("#pnl-summary", `净 PnL ${formatNumber(report.data?.net_pnl, 4)} · 回撤 ${formatNumber(report.data?.max_drawdown_pct, 2)}% · ${report.data?.fills || 0} 笔成交`);
  } catch {
    if (token === state.token) setText("#performance-basis", "绩效更新失败 · 保留上次数据");
  }
}

function applyPrivateEvent(event, payload) {
  if (!state.token) return;
  if (event === "heartbeat") return;
  if (event === "locked") {
    lockPrivateAccess();
    return;
  }
  state.privateUpdates += 1;
  if (event === "positions") {
    renderPositions(payload.data);
    renderPnl(payload.data);
  } else if (event === "orders") renderOrders(payload.data);
  else if (event === "fills") {
    renderFills(payload.data);
    clearTimeout(state.performanceTimer);
    state.performanceTimer = setTimeout(refreshLivePerformance, 100);
  } else if (event === "activity") {
    state.activityRequest += 1;
    renderActivity(payload.data);
  } else if (event === "bills") renderAccountBills(payload);
  else if (event === "account") {
    const ready = payload.connected && payload.authenticated;
    setState("#positions-tag", ready ? "实时同步" : payload.configured ? "账户回报断开" : "私有凭据未配置", ready ? "good" : "warning");
    const balance = payload.balance?.[0];
    if (ready && balance) {
      const equity = balance.totalEq ?? balance.adjEq ?? balance.eq;
      if (equity !== undefined) {
        setText("#metric-equity", formatNumber(equity, 2));
        setText("#metric-equity-note", "OKX 账户实时回报");
      }
    }
  } else if (event === "bill_import") {
    const previous = billHistoryState.job?.status;
    billHistoryState.jobRequest += 1;
    renderBillImport(payload.job);
    updateBillHistoryAvailability();
    scheduleBillImportPoll();
    if (previous === "running" && payload.job?.status !== "running" && billHistoryState.report) {
      loadBillHistory({ reset: true });
    }
  } else if (event === "bill_archives") {
    billHistoryState.archiveUpdates += 1;
    billHistoryState.archiveRequest += 1;
    renderBillArchives(payload.data);
  } else if (event === "strategies") {
    const strategy = payload.data?.find(item => item.strategy_id === "structured-technical");
    if (strategy && !state.strategyDirty && !$("#save-strategy").hasAttribute("aria-busy")) {
      renderStrategy(strategy);
    } else if (strategy && state.strategyDirty) {
      setText("#strategy-message", "服务器配置已更新；保留当前未保存参数。");
    }
  } else if (event === "analyses" && !researchState.runBusy) {
    loadResearchHistory({ page: researchState.page });
  }
}

function connectPrivateFeed() {
  privateFeed?.close();
  privateFeed = null;
  if (!state.token || document.hidden) return;
  const token = state.token;
  privateFeed = openLiveStream("/api/v1/account/events", {
    token,
    onState(status) {
      if (token !== state.token) return;
      state.privateFeedState = status;
      if (status === "locked") lockPrivateAccess();
      else if (status !== "open") {
        setState("#positions-tag", "账户推送重连中", "warning");
        setText("#metric-equity-note", "账户推送重连中 · 保留上次数据");
      }
      scheduleBillImportPoll();
    },
    onEvent(event, payload) {
      if (token === state.token) applyPrivateEvent(event, payload);
    },
  });
}

async function loadPrivate() {
  if (!state.token) return;
  if ($("#refresh-private").hasAttribute("aria-busy")) return;
  const token = state.token;
  setBusy("#refresh-private", true, "同步中...");
  setBusy("#sync-ledger", true);
  try {
    try {
      await api("/api/v1/account/sync", { method: "POST" });
    } catch (error) {
      setMessage(`账户对账未完成：${error.message}`, "error");
    }
    if (token !== state.token) return false;
    const activityRequest = ++state.activityRequest;
    const privateUpdates = state.privateUpdates;
    const [account, positions, orders, fills, pnl, report, activity, bills] = await Promise.all([
      api("/api/v1/account/overview"),
      api("/api/v1/positions"),
      api("/api/v1/orders"),
      api("/api/v1/fills"),
      api("/api/v1/performance/pnl"),
      api(`/api/v1/performance/report?initial_equity=${encodeURIComponent($("#equity-input").value)}`),
      api("/api/v1/activity"),
      api("/api/v1/account/bills").catch(error => ({ error: error.message })),
    ]);
    if (token !== state.token) return false;
    if (privateUpdates !== state.privateUpdates && state.privateFeedState === "open") return true;
    const accountRow = account.balance?.[0] || {};
    const equity = accountRow.totalEq || accountRow.adjEq || accountRow.eq;
    setText("#metric-equity", formatNumber(equity, 2));
    setText("#metric-equity-note", account.configured ? "OKX 账户快照" : "OKX 私有凭据未配置");
    renderPositions(positions.data);
    renderPnl(positions.data);
    renderOrders(orders.data);
    renderFills(fills.data);
    renderPerformance(report.data);
    if (activityRequest === state.activityRequest) renderActivity(activity.data);
    renderAccountBills(bills);
    setText(
      "#pnl-summary",
      `净 PnL ${formatNumber(report.data?.net_pnl ?? pnl.data?.net_pnl, 4)} · 回撤 ${formatNumber(report.data?.max_drawdown_pct, 2)}% · ${report.data?.fills || pnl.data?.fills || 0} 笔成交`,
    );
    setText("#positions-tag", "已同步");
    return true;
  } catch (error) {
    setMessage(`私有数据同步失败：${error.message}`, "error");
    return false;
  } finally {
    setBusy("#refresh-private", false);
    setBusy("#sync-ledger", false);
    updatePrivateActionAvailability();
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
        ? `自动运行已启用（${payload.dry_run ? "仅模拟计算" : "模拟盘执行"}）`
        : "自动运行已停止",
      "good",
    );
    await loadStatus();
  } catch (error) {
    setMessage(`自动运行控制失败：${error.message}`, "error");
  } finally {
    setBusy("#toggle-worker", false);
  }
}

async function runAnalysis() {
  if (!state.token || $("#run-analysis").hasAttribute("aria-busy")) return;
  const token = state.token;
  const request = ++state.analysisRequest;
  const { symbol, bar } = state;
  renderAnalysis(null);
  setBusy("#run-analysis", true, "分析中...");
  setMessage("正在读取 K 线并计算...");
  try {
    const payload = await api("/api/v1/analysis", {
      method: "POST",
      body: JSON.stringify({
        inst_id: symbol,
        bar,
        limit: 100,
        strategy_id: state.strategy?.strategy_id || "structured-technical",
      }),
    });
    if (request !== state.analysisRequest || token !== state.token) return;
    renderAnalysis(payload.data);
    acceptCurrentResearch(payload.data);
    setMessage("结构化策略分析完成", "good");
    await loadPrivate();
  } catch (error) {
    if (request === state.analysisRequest) setMessage(`分析失败：${error.message}`, "error");
  } finally {
    setBusy("#run-analysis", false);
  }
}

async function runAiAnalysis() {
  if (!state.token || researchState.runBusy || researchState.checkBusy) return;
  const token = state.token;
  const request = ++state.analysisRequest;
  const { symbol, bar } = state;
  renderAnalysis(null);
  researchState.runBusy = true;
  setBusy("#run-ai-analysis", true, "研究运行中...");
  updateResearchAvailability();
  researchRunMessage(`${symbol} · ${bar} 研究运行中，尚未生成报告。`);
  setMessage("TradingAgents 正在运行...");
  try {
    const payload = await api("/api/v1/analysis/ai", {
      method: "POST",
      body: JSON.stringify({ inst_id: symbol, bar, limit: 100 }),
    });
    if (token !== state.token) return;
    if (request !== state.analysisRequest) {
      researchRunMessage(`${symbol} · ${bar} 研究已保存到历史记录。`, "good");
      await loadResearchHistory({ reset: true });
      return;
    }
    const marketContext = payload.data?.market_context || {};
    const contextErrors = marketContext.errors?.length
      ? ` · ${marketContext.errors.join("、")}读取失败`
      : "";
    setText("#analysis-source", "TradingAgents");
    setText("#analysis-kind", "AI 研究");
    setText("#analysis-bias", "AI 研究");
    const decision = payload.data.decision;
    const decisionLabel = typeof decision === "string"
      ? ({ hold: "观望", buy: "偏多", sell: "偏空" })[decision.toLowerCase()] || decision.slice(0, 260)
      : "已生成研究结论";
    setText("#analysis-summary", `模型意见：${decisionLabel}`);
    setText(
      "#analysis-context",
      `研究证据：OKX ${marketContext.bar || state.bar} · ${marketContext.candle_count || 0} 根 K 线 · 采集于 ${formatTime(marketContext.captured_at)}${contextErrors}`,
    );
    acceptCurrentResearch(payload.data);
    researchRunMessage(`${symbol} · ${bar} 研究完成，无委托权限。`, "good");
    setMessage("TradingAgents 分析完成", "good");
  } catch (error) {
    if (token === state.token) {
      researchRunMessage(`${symbol} 研究未完成：${error.message}`, "danger");
      if (request === state.analysisRequest) setMessage(`TradingAgents 未运行：${error.message}`, "error");
    }
  } finally {
    researchState.runBusy = false;
    setBusy("#run-ai-analysis", false);
    await loadResearchStatus();
  }
}

async function runBacktest() {
  if (!state.token || $("#run-backtest").hasAttribute("aria-busy")) return;
  if (!$("#backtest-form").reportValidity()) return;
  const token = state.token;
  const request = ++state.backtestRequest;
  const draft = backtestDraft();
  const strategyConfig = JSON.stringify(state.strategy?.config || {});
  setBusy("#run-backtest", true, "回放中...");
  setState("#backtest-status", `${draft.inst_id} · ${draft.bar} · 回放中...`, "neutral");
  try {
    const payload = await api("/api/v1/backtest", {
      method: "POST",
      body: JSON.stringify(draft),
    });
    if (request !== state.backtestRequest || token !== state.token) return;
    if (!payload.data || typeof payload.data !== "object") throw new Error("回测结果为空");
    state.backtest = { result: payload.data, draft, strategyConfig };
    renderBacktest();
    setState("#backtest-status", "回测完成 · 未生成委托", "good");
  } catch (error) {
    if (request !== state.backtestRequest || token !== state.token) return;
    setState("#backtest-status", `回放失败：${error.message}${state.backtest ? "；下方为上次结果" : ""}`, "danger");
  } finally {
    setBusy("#run-backtest", false);
    updatePrivateActionAvailability();
  }
}

function backtestDraft() {
  return {
    inst_id: state.symbol,
    bar: state.bar,
    limit: Number($("#backtest-limit").value),
    initial_equity: Number($("#backtest-equity").value),
    fee_bps: Number($("#backtest-fee").value),
    strategy_id: state.strategy?.strategy_id || "structured-technical",
  };
}

function updateBacktestContext() {
  setText("#backtest-market", `${state.symbol} · ${state.bar}`);
  if (!state.backtest) return;
  const changed = JSON.stringify(state.backtest.draft) !== JSON.stringify(backtestDraft())
    || state.backtest.strategyConfig !== JSON.stringify(state.strategy?.config || {});
  setState("#backtest-result-state", changed ? "参数已变更" : "已完成", changed ? "warning" : "good");
}

function renderBacktest() {
  const { result, draft } = state.backtest;
  const percent = value => value == null || !Number.isFinite(Number(value)) ? "--" : `${Number(value).toFixed(2)}%`;
  $("#backtest-result").hidden = false;
  setText("#backtest-result-context", `${draft.inst_id} · ${draft.bar} · 请求 ${draft.limit} 根 K 线 · 起算 ${formatNumber(draft.initial_equity, 2)} · 费用 ${formatNumber(draft.fee_bps, 2)} bps`);
  setState("#backtest-return", percent(result.return_pct), Number(result.return_pct) > 0 ? "good" : Number(result.return_pct) < 0 ? "danger" : "neutral");
  setText("#backtest-drawdown", percent(result.max_drawdown_pct));
  setText("#backtest-final", formatNumber(result.final_equity, 2));
  setText("#backtest-trades", formatNumber(result.trades, 0));
  setText("#backtest-win-rate", percent(result.win_rate_pct));
  setText("#backtest-points", Array.isArray(result.equity_curve) ? result.equity_curve.length : "--");
  updateBacktestContext();
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
    setMessage("PushPlus 已受理测试通知，送达以微信为准", "good");
  } catch (error) {
    setMessage(`PushPlus 发送失败：${error.message}`, "error");
  } finally {
    setBusy("#test-notification", false);
  }
}

async function submitSignal(dryRun) {
  if (state.submitting) return;
  if (!dryRun && !executionGateOpen(state.status)) {
    setMessage("行情或执行连接未就绪，禁止提交新订单。", "error");
    return;
  }
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
  if (payload.signal.inst_id !== state.symbol) {
    setMessage("分析合约与当前合约不一致，请重新分析", "error");
    return;
  }
  if (!Number.isFinite(payload.size) || payload.size <= 0
      || !Number.isFinite(payload.account_equity) || payload.account_equity <= 0) {
    setMessage("权益与合约张数必须为有效正数", "error");
    return;
  }
  const mode = state.status?.trading_mode === "live" ? "实盘" : "模拟盘";
  if (!dryRun && (!window.confirm(`确认提交 OKX ${mode}订单？\n${payload.signal.inst_id} · ${payload.signal.action} · ${payload.size} 张`)
      || !executionGateOpen(state.status))) return;
  state.submitting = true;
  const draftRevision = state.draftRevision;
  const button = dryRun ? "#preview-signal" : "#execute-signal";
  setBusy(button, true);
  $("#preview-signal").disabled = true;
  $("#execute-signal").disabled = true;
  setMessage(dryRun ? "正在执行风控预览..." : `正在提交${mode}订单...`);
  try {
    const result = await api("/api/v1/execution/signals", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    if (draftRevision === state.draftRevision) renderPreflight(result);
    const successMessage = result.dry_run
      ? "风控预览通过，未向交易所发单"
      : result.idempotent ? `已有订单：${result.order?.client_order_id || ""}，未重复发单`
        : `${mode}订单已提交`;
    setMessage(result.accepted ? successMessage : (result.reasons || []).map(reason => executionReasons[reason] || reason).join("；"), result.accepted ? "good" : "error");
    await loadPrivate();
  } catch (error) {
    setMessage(`执行失败：${error.message}`, "error");
  } finally {
    state.submitting = false;
    setBusy(button, false);
    $("#preview-signal").disabled = !state.token || !state.analysis?.signal;
    if (state.status) applyStatus(state.status);
  }
}

function invalidateAnalysis() {
  state.analysisRequest += 1;
  renderAnalysis(null);
}

function reloadSelectedMarket() {
  if ($("#chart-note-dialog").open) $("#chart-note-dialog").close();
  state.chartDrag = null;
  invalidateAnalysis();
  renderResearchEvidence();
  updateBacktestContext();
  if (state.token && $("#history-contract").value) loadResearchHistory({ reset: true });
  state.chartCursorTime = null;
  loadChartAnnotations();
  renderChart([]);
  renderWatchlist({});
  renderMarketOverview({});
  connectMarketFeed();
  document.querySelectorAll("[data-bar]").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.bar === state.bar));
  });
  loadMarket();
}

$("#symbol").addEventListener("change", (event) => {
  state.symbol = event.target.value;
  reloadSelectedMarket();
});
$("#bar").addEventListener("change", (event) => {
  state.bar = event.target.value;
  reloadSelectedMarket();
});
document.querySelectorAll("[data-bar]").forEach(button => {
  button.addEventListener("click", () => {
    if (state.bar === button.dataset.bar) return;
    state.bar = button.dataset.bar;
    $("#bar").value = state.bar;
    reloadSelectedMarket();
  });
});
document.querySelectorAll("[data-chart-mode]").forEach(button => {
  button.addEventListener("click", () => {
    state.chartMode = button.dataset.chartMode;
    document.querySelectorAll("[data-chart-mode]").forEach(item => {
      item.setAttribute("aria-pressed", String(item === button));
    });
    renderChart(state.lastCandles);
  });
});
$("#chart-range").addEventListener("change", event => {
  state.chartRange = Number(event.target.value);
  renderChart(state.lastCandles);
});
document.querySelectorAll("[data-chart-tool]").forEach(button => {
  button.addEventListener("click", () => setChartTool(button.dataset.chartTool));
});
$("#chart-annotation-list").addEventListener("change", event => {
  state.chartSelection = Number(event.target.value);
  setChartTool("cursor");
  updateChartAnnotationControls();
});
$("#edit-chart-annotation").addEventListener("click", () => {
  openChartAnnotationEditor(state.chartAnnotations[state.chartSelection], state.chartSelection);
});
$("#delete-chart-annotation").addEventListener("click", () => {
  if (!state.chartAnnotations[state.chartSelection]) return;
  rememberChartAnnotations();
  state.chartAnnotations.splice(state.chartSelection, 1);
  state.chartSelection = -1;
  saveChartAnnotations();
  renderChart(state.lastCandles);
});
$("#undo-chart-annotation").addEventListener("click", () => {
  if (!state.chartUndo.length) return;
  state.chartAnnotations = state.chartUndo.pop();
  state.chartSelection = -1;
  state.chartDraft = null;
  saveChartAnnotations();
  renderChart(state.lastCandles);
});
$("#clear-chart-annotations").addEventListener("click", () => {
  if (!state.chartAnnotations.length) return;
  rememberChartAnnotations();
  state.chartAnnotations = [];
  state.chartSelection = -1;
  state.chartDraft = null;
  saveChartAnnotations();
  renderChart(state.lastCandles);
});
$("#close-chart-note").addEventListener("click", () => $("#chart-note-dialog").close());
$("#chart-note-dialog").addEventListener("close", () => {
  state.chartEditor = null;
  setChartTool("cursor");
  $("#price-chart").focus({ preventScroll: true });
});
$("#chart-note-form").addEventListener("submit", event => {
  event.preventDefault();
  const editor = state.chartEditor;
  if (!editor || editor.key !== chartStorageKey()) return;
  const annotation = structuredClone(editor.annotation);
  const price = Number($("#chart-note-price").value);
  const endPrice = Number($("#chart-note-end-price").value);
  const text = $("#chart-note-text").value.trim();
  if (!Number.isFinite(price) || price <= 0 || !Number.isFinite(endPrice) || endPrice <= 0
      || annotation.type === "text" && (!text || text.length > 80)) return;
  if (annotation.type === "trend") {
    annotation.start.price = price;
    annotation.end.price = endPrice;
  } else annotation.price = price;
  if (annotation.type === "text") annotation.text = text;
  rememberChartAnnotations();
  if (editor.index < 0) {
    state.chartAnnotations.push(annotation);
    state.chartSelection = state.chartAnnotations.length - 1;
  } else {
    state.chartAnnotations[editor.index] = annotation;
    state.chartSelection = editor.index;
  }
  saveChartAnnotations();
  $("#chart-note-dialog").close();
});
$("#price-chart").addEventListener("pointermove", event => {
  const geometry = state.chartGeometry;
  if (!geometry || !$("#chart-empty").hidden) return;
  const point = chartPointFromEvent(event, state.chartDrag?.geometry || geometry);
  if (state.chartDrag && point) {
    const drag = state.chartDrag;
    const item = structuredClone(drag.original);
    const priceDelta = point.price - drag.point.price;
    const timeDelta = point.time - drag.point.time;
    if (item.type === "trend") {
      for (const endpoint of [item.start, item.end]) {
        endpoint.price += priceDelta;
        endpoint.time += timeDelta;
      }
      if (item.start.price <= 0 || item.end.price <= 0) return;
    } else {
      item.price += priceDelta;
      if (item.price <= 0) return;
      if (item.type === "text") item.time += timeDelta;
    }
    drag.moved = true;
    state.chartAnnotations[drag.index] = item;
    renderChart(state.lastCandles);
    return;
  }
  if (state.chartDraft && point) {
    state.chartDraft.end = point;
    renderChart(state.lastCandles);
  }
  const x = event.clientX - event.currentTarget.getBoundingClientRect().left - geometry.padding.left;
  showChartCursor(x < 0 || x > geometry.chartWidth ? -1 : Math.min(geometry.candles.length - 1, Math.floor(x / geometry.chartWidth * geometry.candles.length)));
});
$("#price-chart").addEventListener("pointerleave", () => showChartCursor(-1));
$("#price-chart").addEventListener("pointerdown", event => {
  if (event.button !== 0) return;
  if (state.chartTool !== "cursor") {
    event.preventDefault();
    addChartAnnotation(event);
  } else {
    const point = chartPointFromEvent(event);
    if (!point) return;
    state.chartSelection = hitChartAnnotation(point, state.chartGeometry);
    const selected = state.chartAnnotations[state.chartSelection];
    if (selected) {
      state.chartDrag = { point, index: state.chartSelection, original: structuredClone(selected),
        before: structuredClone(state.chartAnnotations), geometry: state.chartGeometry, moved: false };
      event.currentTarget.setPointerCapture(event.pointerId);
    }
    updateChartAnnotationControls();
    renderChart(state.lastCandles);
  }
});
for (const type of ["pointerup", "pointercancel"]) {
  $("#price-chart").addEventListener(type, event => {
    const drag = state.chartDrag;
    if (!drag) return;
    if (type === "pointercancel") state.chartAnnotations = drag.before;
    else if (drag.moved) {
      state.chartUndo.push(drag.before);
      if (state.chartUndo.length > 20) state.chartUndo.shift();
      saveChartAnnotations();
    }
    state.chartDrag = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    renderChart(state.lastCandles);
  });
}
$("#price-chart").addEventListener("keydown", event => {
  const geometry = state.chartGeometry;
  if (event.key === "Delete" || event.key === "Backspace") {
    event.preventDefault();
    $("#delete-chart-annotation").click();
    return;
  }
  if (event.key === "Enter" && geometry) {
    event.preventDefault();
    const candle = geometry.candles.find(item => item.time === state.chartCursorTime) || geometry.candles.at(-1);
    if (state.chartTool === "cursor") $("#edit-chart-annotation").click();
    else addChartAnnotationAtPoint({ time: candle.time, price: candle.close });
    return;
  }
  if (!geometry || !$("#chart-empty").hidden || !["ArrowLeft", "ArrowRight", "Home", "End", "Escape"].includes(event.key)) return;
  event.preventDefault();
  if (event.key === "Escape") {
    setChartTool("cursor");
    return showChartCursor(-1);
  }
  let index = geometry.candles.findIndex(candle => candle.time === state.chartCursorTime);
  if (index < 0) index = geometry.candles.length - 1;
  if (event.key === "Home") index = 0;
  else if (event.key === "End") index = geometry.candles.length - 1;
  else index += event.key === "ArrowLeft" ? -1 : 1;
  showChartCursor(Math.max(0, Math.min(geometry.candles.length - 1, index)));
});
document.querySelectorAll("[data-ledger]").forEach(tab => {
  tab.addEventListener("click", () => showLedger(tab.dataset.ledger));
  tab.addEventListener("keydown", event => {
    const tabs = [...document.querySelectorAll("[data-ledger]")];
    let index = tabs.indexOf(tab);
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    if (event.key === "Home") index = 0;
    else if (event.key === "End") index = tabs.length - 1;
    else index = (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    showLedger(tabs[index].dataset.ledger, true);
  });
});
document.querySelectorAll("[data-ticket]").forEach(tab => {
  tab.addEventListener("click", () => showTicket(tab.dataset.ticket));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const target = event.key === "Home" ? "signal" : event.key === "End" ? "execution"
      : tab.dataset.ticket === "signal" ? "execution" : "signal";
    showTicket(target, true);
  });
});
$("#review-order").addEventListener("click", () => showTicket("execution", true));
$("#toggle-chart-focus").addEventListener("click", () => {
  setChartFocus(!state.chartFocused, true);
  window.scrollTo({ top: 0, behavior: "instant" });
});
document.addEventListener("keydown", event => {
  if (event.key !== "Escape" || !state.chartFocused || document.querySelector("dialog[open]")) return;
  event.preventDefault();
  setChartFocus(false, true);
});
const recordRenderers = { positions: renderPositions, orders: renderOrders, fills: renderFills };
document.querySelectorAll("[data-record-query]").forEach(input => {
  input.addEventListener("input", () => {
    const kind = input.dataset.recordQuery;
    if (state.records[kind] !== null) recordRenderers[kind](state.records[kind]);
  });
});
$("#orders-status").addEventListener("change", () => {
  if (state.records.orders !== null) renderOrders(state.records.orders);
});
document.addEventListener("click", event => {
  const clear = event.target.closest("[data-clear-records]");
  if (!clear) return;
  const kind = clear.dataset.clearRecords;
  if (!Object.hasOwn(recordRenderers, kind) || state.records[kind] === null) return;
  const input = $(`#${kind}-query`);
  input.value = "";
  if (kind === "orders") $("#orders-status").value = "all";
  recordRenderers[kind](state.records[kind]);
  input.focus();
});
$("#size-input").addEventListener("input", clearPreflight);
$(".strategy-settings").addEventListener("input", () => { state.strategyDirty = true; });
$("#worker-dry-run").addEventListener("change", () => { state.workerModeDirty = true; });
$("#equity-input").addEventListener("input", clearPreflight);
$("#refresh-market").addEventListener("click", () => {
  if (!state.marketPaused && state.marketFeedState !== "open") connectMarketFeed();
  loadMarket();
});
$("#toggle-market-details").addEventListener("click", () => {
  const collapsed = $("#market-stats").classList.toggle("is-collapsed");
  const button = $("#toggle-market-details");
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", collapsed ? "展开行情详情" : "收起行情详情");
  button.title = button.getAttribute("aria-label");
});
$("#toggle-market-refresh").addEventListener("click", () => {
  state.marketPaused = !state.marketPaused;
  if (state.marketPaused) {
    state.marketRequest += 1;
    setBusy("#refresh-market", false);
  }
  connectMarketFeed();
  if (!state.marketPaused) loadMarket();
  setMessage(state.marketPaused ? "行情自动刷新已暂停" : "行情自动刷新已恢复", "good");
});
$("#run-analysis").addEventListener("click", runAnalysis);
$("#backtest-form").addEventListener("submit", event => {
  event.preventDefault();
  runBacktest();
});
$("#backtest-form").addEventListener("input", updateBacktestContext);
$("#refresh-activity").addEventListener("click", loadActivity);
$("#activity-query").addEventListener("input", () => {
  if (state.activityRows !== null) renderActivity(state.activityRows);
});
$("#activity-severity").addEventListener("change", () => {
  if (state.activityRows !== null) renderActivity(state.activityRows);
});
$("#clear-activity").addEventListener("click", () => {
  $("#activity-query").value = "";
  $("#activity-severity").value = "all";
  if (state.activityRows !== null) renderActivity(state.activityRows);
  $("#activity-query").focus();
});
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
$("#sync-ledger").addEventListener("click", loadPrivate);
$("#watchlist").addEventListener("click", (event) => {
  const button = event.target.closest(".watch-item");
  if (!button) return;
  const symbol = button.dataset.symbol;
  if (!symbol || symbol === state.symbol) return;
  state.symbol = symbol;
  $("#symbol").value = symbol;
  reloadSelectedMarket();
});
$("#orders-body").addEventListener("click", (event) => {
  const button = event.target.closest(".cancel-order");
  if (button) cancelOrder(button.dataset.clientOrderId);
});
$("#emergency-stop").addEventListener("click", () => setEmergencyStop("/api/v1/safety/emergency-stop", "web operator emergency stop"));
$("#resume-trading").addEventListener("click", () => setEmergencyStop("/api/v1/safety/resume", "web operator resume"));
$("#auth-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setBusy("#save-token", true, "同步中...");
  privateFeed?.close();
  privateFeed = null;
  state.privateFeedState = "locked";
  state.token = $("#admin-token").value.trim();
  if (!state.token) lockPrivateAccess();
  try {
    const [privateLoaded, strategiesLoaded] = await Promise.all([
      loadPrivate(),
      loadStrategies(),
    ]);
    updatePrivateActionAvailability();
    if (state.token && privateLoaded && strategiesLoaded) {
      setMessage("已解锁私有数据（令牌只保存在当前页面内存）", "good");
      $("#preview-signal").disabled = !state.analysis?.signal;
      $("#admin-token").value = "";
      $("#auth-dialog").close();
      $("#open-auth").title = "管理员已解锁";
      $("#open-auth").setAttribute("aria-label", "管理员已解锁");
      loadResearchHistory({ reset: true });
      connectPrivateFeed();
    } else if (state.token) {
      lockPrivateAccess();
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

document.querySelectorAll(".sidebar .nav-item").forEach(item => {
  const link = item.cloneNode(true);
  link.className = "mobile-nav-item";
  link.dataset.route = item.dataset.section;
  delete link.dataset.section;
  $("#mobile-navigation").append(link);
});
let navigationRoutePending = false;
document.addEventListener("click", event => {
  const item = event.target.closest(".nav-item, [data-route]");
  if (!item || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  if ($("#navigation-dialog").open) {
    navigationRoutePending = true;
    $("#navigation-dialog").close();
  }
  if (location.hash !== item.hash) history.pushState(null, "", item.hash);
  showView(item.dataset.section || item.dataset.route, true);
  window.scrollTo({ top: 0, behavior: "instant" });
  if (item.id === "open-research-report" && researchState.record) {
    requestAnimationFrame(() => $("#research-title").focus());
  }
});
$("#open-navigation").addEventListener("click", () => {
  $("#navigation-dialog").showModal();
  $("#mobile-navigation [aria-current='page']")?.focus();
});
$("#close-navigation").addEventListener("click", () => $("#navigation-dialog").close());
$("#navigation-dialog").addEventListener("close", () => {
  if (!navigationRoutePending && $("#open-navigation").getClientRects().length) {
    $("#open-navigation").focus({ preventScroll: true });
  }
  navigationRoutePending = false;
});
const mobileViewport = matchMedia("(max-width: 767px)");
mobileViewport.addEventListener("change", event => {
  if (!event.matches && $("#navigation-dialog").open) {
    $("#navigation-dialog").close();
    $("#page-title").focus({ preventScroll: true });
  }
});
window.addEventListener("popstate", () => showView(location.hash.slice(1)));
window.addEventListener("hashchange", () => {
  if (location.hash !== "#main-content") showView(location.hash.slice(1));
});
let authOpener;
document.addEventListener("click", event => {
  const opener = event.target.closest("#open-auth, [data-open-auth]");
  if (!opener) return;
  pendingViewFocus += 1;
  authOpener = opener;
  setText("#auth-message", "");
  $("#auth-dialog").showModal();
  $("#admin-token").focus();
});
$("#close-auth").addEventListener("click", () => $("#auth-dialog").close());
$("#auth-dialog").addEventListener("close", () => {
  $("#admin-token").value = "";
  const target = authOpener?.getClientRects().length ? authOpener : $("#open-auth");
  target.focus({ preventScroll: true });
});
$("#collapse-sidebar").addEventListener("click", () => {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  const button = $("#collapse-sidebar");
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", collapsed ? "展开导航" : "收起导航");
  button.title = collapsed ? "展开导航" : "收起导航";
  button.querySelector("[data-icon]").dataset.icon = collapsed ? "panel-left-open" : "panel-left-close";
  renderIcons(button);
  if (state.lastCandles) renderChart(state.lastCandles);
});
document.querySelectorAll("[data-theme-toggle]").forEach(button => {
  button.addEventListener("click", () => {
    setTheme(document.documentElement.dataset.theme === "light" ? "dark" : "light");
  });
});
window.addEventListener("storage", event => {
  if (event.key === "openperpdesk.theme" || event.key === null) {
    setTheme(event.newValue, false);
  }
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    marketFeed?.close();
    controlFeed?.close();
    privateFeed?.close();
    state.marketFeedState = "offline";
    state.controlFeedState = "offline";
    state.controlSnapshotFresh = false;
    state.controlUpdates += 1;
    state.privateFeedState = "offline";
  } else {
    connectMarketFeed();
    connectControlFeed();
    connectPrivateFeed();
    loadMarket();
  }
});
window.addEventListener("pagehide", () => {
  marketFeed?.close();
  controlFeed?.close();
  privateFeed?.close();
});
window.addEventListener("pageshow", event => {
  if (event.persisted) {
    connectMarketFeed();
    connectControlFeed();
    connectPrivateFeed();
    loadMarket();
  }
});

setTheme(document.documentElement.dataset.theme, false);
renderIcons();
loadChartAnnotations();
initializeResearch();
initializeBillHistory();
showView(location.hash.slice(1));
tickClock();
setInterval(tickClock, 1000);
loadStatus();
setInterval(() => {
  if (!document.hidden && state.controlFeedState !== "open") loadStatus();
}, 15000);
connectControlFeed();
loadMarket();
connectMarketFeed();
updateMarketRefreshControl();
