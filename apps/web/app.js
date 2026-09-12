const connectionStatus = document.querySelector("#connection-status");
const clock = document.querySelector("#clock");

function setText(selector, value) {
  const element = document.querySelector(selector);
  if (element) element.textContent = value;
}

function tickClock() {
  clock.textContent = new Date().toISOString().replace("T", " ").slice(0, 19) + " UTC";
}

function setConnection(online) {
  connectionStatus.textContent = online ? "API 在线" : "API 离线";
  connectionStatus.classList.toggle("status-good", online);
  connectionStatus.classList.toggle("status-neutral", !online);
}

function formatPrice(value) {
  if (value === undefined || value === null || value === "") return "--";
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString("en-US", { maximumFractionDigits: 4 })
    : "--";
}

function formatChange(last, open) {
  const lastNumber = Number(last);
  const openNumber = Number(open);
  if (!Number.isFinite(lastNumber) || !Number.isFinite(openNumber) || openNumber === 0) {
    return { label: "--", className: "" };
  }
  const change = ((lastNumber - openNumber) / openNumber) * 100;
  return {
    label: `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`,
    className: change >= 0 ? "change-positive" : "change-negative",
  };
}

function renderWatchlist(tickers) {
  const body = document.querySelector("#watchlist-body");
  if (!body) return;
  const rows = Object.values(tickers || {});
  body.replaceChildren();
  if (!rows.length) {
    const empty = document.createElement("tr");
    empty.innerHTML = '<td colspan="5" class="table-empty">等待公共行情...</td>';
    body.append(empty);
    return;
  }
  for (const record of rows) {
    const data = record.data || {};
    const change = formatChange(data.last, data.sodUtc8);
    const row = document.createElement("tr");
    row.innerHTML = `
      <td class="mono-cell">${data.instId || "--"}</td>
      <td class="mono-cell">${formatPrice(data.last)}</td>
      <td class="mono-cell">${formatPrice(data.bidPx)} / ${formatPrice(data.askPx)}</td>
      <td class="mono-cell ${change.className}">${change.label}</td>
      <td class="mono-cell">${record.received_at ? record.received_at.slice(11, 19) + " UTC" : "--"}</td>
    `;
    body.append(row);
  }
}

function applyStatus(status) {
  const mode = String(status.trading_mode || "demo").toUpperCase();
  const modeLabel = mode === "LIVE" ? "实盘" : "模拟盘";
  setText("#trading-mode", modeLabel);
  setText("#state-mode", modeLabel);
  setText("#state-market", status.market_data_connected ? "在线" : "未接入");
  setText("#state-risk", status.risk_engine_ready ? "就绪" : "已锁定");
  setText("#state-proxy", status.integrations?.outbound_proxy_configured ? "已配置" : "未配置");
  setText("#state-pushplus", status.integrations?.pushplus_configured ? "已配置" : "未配置");
  setText("#execution-state", status.execution_enabled ? "已启用" : "未连接");
}

async function loadStatus() {
  try {
    const response = await fetch("/api/v1/system/status", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    applyStatus(await response.json());
    setConnection(true);
  } catch {
    setConnection(false);
  }
}

async function loadMarket() {
  try {
    const response = await fetch("/api/v1/market/stream", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const ticker = payload.tickers?.["BTC-USDT-SWAP"]?.data || {};
    const last = ticker.last ? Number(ticker.last).toLocaleString("en-US", { maximumFractionDigits: 2 }) : "--";
    const change = ticker.sodUtc8
      ? `${(Number(ticker.last) - Number(ticker.sodUtc8)).toFixed(2)}`
      : "--";
    setText("#market-price", last);
    setText("#market-summary", `最新价 ${last} · 日内变化 ${change} · 数据来自 OKX 公共行情`);
    setText("#market-tag", payload.fresh ? "实时只读" : "数据过期");
    setText("#state-market", payload.fresh ? "在线" : "数据过期");
    renderWatchlist(payload.tickers);
  } catch {
    setText("#market-price", "--");
    setText("#market-summary", "行情暂时不可用，交易执行仍保持锁定");
    setText("#market-tag", "连接失败");
    setText("#state-market", "离线");
    renderWatchlist({});
  }
}

tickClock();
setInterval(tickClock, 1000);
loadStatus();
setInterval(loadStatus, 15000);
loadMarket();
setInterval(loadMarket, 15000);
