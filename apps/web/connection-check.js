const connectionCheckState = { snapshot: null, request: 0, updates: 0, submitting: false, message: "" };
const connectionCheckLabels = {
  private_ws: "账户推送认证", algo_ws: "止盈止损推送认证",
  balance: "账户余额", positions: "持仓", config: "账户模式",
  pending_orders: "当前委托", orders_history: "历史委托",
  fills_history: "成交记录", pending_algo_orders: "当前保护委托",
};
const connectionCheckErrors = {
  credentials_missing: "OKX 私有凭据未配置",
  live_probe_not_approved: "网页检测仅用于模拟盘",
  authentication_rejected: "推送认证失败",
  websocket_disconnected: "检测期间推送连接断开",
  check_timeout: "连接检测超时",
  check_interrupted: "连接检测已中断",
  connection_check_failed: "连接检测未通过",
  check_busy: "服务器正在检测",
};

function normalizeConnectionCheck(value) {
  if (!value || typeof value !== "object" || !Array.isArray(value.checks)
    || !["idle", "running", "passed", "failed", "interrupted"].includes(value.status)
    || value.trading_performed !== false || value.order_lifecycle_verified !== false) return null;
  const names = Object.keys(connectionCheckLabels);
  if (value.checks.length !== names.length || !names.every(name =>
    value.checks.filter(row => row?.name === name).length === 1)) return null;
  const checks = value.checks.map(row => ({
    name: row.name, status: row.status,
    rows: Number.isSafeInteger(row.rows) && row.rows >= 0 ? row.rows : null,
  }));
  if (checks.some(row => !["pending", "running", "passed", "failed", "incomplete"].includes(row.status))) return null;
  const report = value.report;
  const verified = value.status === "passed" && report?.demo === true
    && report.scope === "private_rest_and_websocket_read_only"
    && report.trading_performed === false && report.order_lifecycle_verified === false
    && ["private_ws_connected", "private_ws_authenticated", "algo_ws_connected", "algo_ws_authenticated"]
      .every(name => report[name] === true)
    && checks.every(row => row.status === "passed"
      && (row.name.endsWith("_ws") || (row.rows !== null && report.rows?.[row.name] === row.rows)));
  return {
    status: value.status, checks, verified,
    checked_at: typeof value.checked_at === "string" && Number.isFinite(Date.parse(value.checked_at))
      ? value.checked_at : null,
    error: Object.hasOwn(connectionCheckErrors, value.error) ? value.error : null,
  };
}

function renderConnectionCheck() {
  const connected = Boolean(state.token) && state.privateFeedState === "open";
  const snapshot = connected ? connectionCheckState.snapshot : null;
  const running = connected && (connectionCheckState.submitting || snapshot?.status === "running");
  setBusy("#check-account-connection", running, "检测中...");
  $("#check-account-connection").disabled = !connected || running;
  const labels = { idle: "尚未检测", running: "正在检测", passed: "只读连接通过", failed: "检测未通过", interrupted: "检测已中断" };
  const verified = snapshot?.verified === true;
  const label = !state.token ? "管理员未解锁" : !connected ? "推送连接不可用"
    : connectionCheckState.submitting && !snapshot ? "正在发起检测"
      : connectionCheckState.message || connectionCheckErrors[snapshot?.error]
      || (snapshot?.status === "passed" && !verified ? "检测结果待确认" : labels[snapshot?.status] || "尚未检测");
  setState("#account-check-status", label, verified ? "good" : snapshot?.status === "failed" ? "warning" : "neutral");
  setText("#account-check-time", snapshot?.checked_at ? `${formatTime(snapshot.checked_at)} UTC` : "--");
  const statuses = { pending: "未检测", running: "检测中", passed: "通过", failed: "失败", incomplete: "未完成" };
  const content = Object.entries(connectionCheckLabels).map(([name, title]) => {
    const row = snapshot?.checks?.find(item => item.name === name);
    const count = Number.isInteger(row?.rows) && row.rows >= 0 ? ` · ${row.rows} 条` : "";
    const status = statuses[row?.status] || "--";
    return `<div><dt>${title}</dt><dd data-tone="${row?.status === "passed" ? "good" : row?.status === "failed" ? "warning" : "neutral"}">${status}${count}</dd></div>`;
  }).join("");
  if ($("#account-check-results").innerHTML !== content) $("#account-check-results").innerHTML = content;
}

function clearConnectionCheck() {
  connectionCheckState.request += 1;
  connectionCheckState.updates += 1;
  connectionCheckState.snapshot = null;
  connectionCheckState.submitting = false;
  connectionCheckState.message = "";
  renderConnectionCheck();
}

function applyConnectionCheck(snapshot) {
  if (!state.token || state.privateFeedState !== "open") return;
  connectionCheckState.updates += 1;
  connectionCheckState.snapshot = normalizeConnectionCheck(snapshot);
  connectionCheckState.message = connectionCheckState.snapshot ? "" : "检测结果待确认";
  renderConnectionCheck();
}

async function startConnectionCheck() {
  if ($("#check-account-connection").disabled) return;
  const token = state.token;
  const request = ++connectionCheckState.request;
  const updates = connectionCheckState.updates;
  const current = () => token === state.token && request === connectionCheckState.request
    && state.privateFeedState === "open";
  connectionCheckState.submitting = true;
  connectionCheckState.snapshot = null;
  connectionCheckState.message = "";
  renderConnectionCheck();
  try {
    const response = await api("/api/v1/account/connection-check", { method: "POST" });
    if (!current()) return;
    if (updates === connectionCheckState.updates) applyConnectionCheck(response.data);
  } catch (error) {
    if (!current()) return;
    if (error.status === 401) {
      lockPrivateAccess();
      return;
    }
    if (updates === connectionCheckState.updates) {
      connectionCheckState.message = connectionCheckErrors[error.message] || "检测请求未确认";
    }
  } finally {
    if (request === connectionCheckState.request) {
      connectionCheckState.submitting = false;
      renderConnectionCheck();
    }
  }
}

function initializeConnectionCheck() {
  $("#check-account-connection").addEventListener("click", startConnectionCheck);
  renderConnectionCheck();
}
