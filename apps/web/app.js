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
    const response = await fetch("/api/v1/market/ticker?inst_id=BTC-USDT-SWAP", { cache: "no-store" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    const ticker = payload.data || {};
    const last = ticker.last ? Number(ticker.last).toLocaleString("en-US", { maximumFractionDigits: 2 }) : "--";
    const change = ticker.sodUtc8
      ? `${(Number(ticker.last) - Number(ticker.sodUtc8)).toFixed(2)}`
      : "--";
    setText("#market-price", last);
    setText("#market-summary", `最新价 ${last} · 日内变化 ${change} · 数据来自 OKX 公共行情`);
    setText("#market-tag", "实时只读");
    setText("#state-market", "在线");
  } catch {
    setText("#market-price", "--");
    setText("#market-summary", "行情暂时不可用，交易执行仍保持锁定");
    setText("#market-tag", "连接失败");
    setText("#state-market", "离线");
  }
}

tickClock();
setInterval(tickClock, 1000);
loadStatus();
setInterval(loadStatus, 15000);
loadMarket();
setInterval(loadMarket, 15000);
