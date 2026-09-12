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
  connectionStatus.textContent = online ? "API ONLINE" : "API OFFLINE";
  connectionStatus.classList.toggle("status-good", online);
  connectionStatus.classList.toggle("status-neutral", !online);
}

function applyStatus(status) {
  const mode = String(status.trading_mode || "demo").toUpperCase();
  setText("#trading-mode", `${mode} MODE`);
  setText("#state-mode", mode);
  setText("#state-market", status.market_data_connected ? "ONLINE" : "OFFLINE");
  setText("#state-risk", status.risk_engine_ready ? "READY" : "LOCKED");
  setText("#state-proxy", status.integrations?.outbound_proxy_configured ? "CONFIGURED" : "NOT SET");
  setText("#state-pushplus", status.integrations?.pushplus_configured ? "CONFIGURED" : "NOT SET");
  setText("#execution-state", status.execution_enabled ? "Enabled" : "Disconnected");
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

tickClock();
setInterval(tickClock, 1000);
loadStatus();
setInterval(loadStatus, 15000);

