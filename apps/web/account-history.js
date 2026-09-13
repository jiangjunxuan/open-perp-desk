const billHistoryState = {
  request: 0, jobRequest: 0, report: null, range: null,
  cursors: [null], page: 0, nextCursor: null, job: null, timer: null,
  loading: false, starting: false,
};

function billHistoryRange() {
  return { start_day: $("#bill-history-start").value, end_day: $("#bill-history-end").value };
}

function billHistoryRangeChanged() {
  return JSON.stringify(billHistoryRange()) !== JSON.stringify(billHistoryState.range);
}

function updateBillHistoryAvailability() {
  const unlocked = Boolean(state.token);
  for (const selector of ["#bill-history-start", "#bill-history-end", "#query-bill-history"]) {
    $(selector).disabled = !unlocked;
  }
  $("#query-bill-history").disabled ||= billHistoryState.loading;
  const configured = billHistoryState.report?.configured ?? state.status?.integrations?.okx_credentials_configured;
  $("#import-bill-history").disabled = !unlocked || !configured || billHistoryState.starting
    || billHistoryState.job?.status === "running";
  $("#bill-history-previous").disabled = !unlocked || billHistoryState.loading || billHistoryState.page === 0 || billHistoryRangeChanged();
  $("#bill-history-next").disabled = !unlocked || billHistoryState.loading || !billHistoryState.nextCursor || billHistoryRangeChanged();
  if (!unlocked && (billHistoryState.report || billHistoryState.job || billHistoryState.loading || billHistoryState.starting)) {
    resetBillHistoryAccess();
  }
}

function resetBillHistoryAccess() {
  billHistoryState.request += 1;
  billHistoryState.jobRequest += 1;
  clearTimeout(billHistoryState.timer);
  billHistoryState.timer = null;
  billHistoryState.report = null;
  billHistoryState.range = null;
  billHistoryState.job = null;
  billHistoryState.cursors = [null];
  billHistoryState.page = 0;
  billHistoryState.nextCursor = null;
  billHistoryState.loading = false;
  billHistoryState.starting = false;
  setBusy("#query-bill-history", false);
  setBusy("#import-bill-history", false);
  document.querySelectorAll("#bill-history input, #bill-history button").forEach(control => { control.disabled = true; });
  $("#bill-import-progress").hidden = true;
  setText("#bill-history-status", "已锁定");
  setText("#bill-history-message", "管理员未解锁");
  setText("#bill-history-range", "尚无查询结果");
  setText("#bill-history-coverage", "覆盖情况未知");
  setText("#bill-history-page", "第 1 页");
  $("#bill-history-summary").replaceChildren();
  $("#bill-history-body").innerHTML = '<tr><td colspan="8" class="table-empty">历史账单已锁定</td></tr>';
}

function handleBillHistoryUnauthorized(error) {
  if (error.status !== 401) return false;
  lockPrivateAccess();
  resetBillHistoryAccess();
  updatePrivateActionAvailability();
  return true;
}

function renderBillHistory(payload, range, page) {
  if (!Array.isArray(payload.data) || !payload.coverage || !payload.summary) throw new Error("账本数据格式无效");
  const labels = {
    trade: "交易", funding: "资金费", liquidation: "强平", adl: "自动减仓",
    interest: "利息", clawback: "分摊", account_transfer: "账户划转",
    internal_transfer: "保证金划转", non_swap: "非永续业务", unclassified: "待分类",
  };
  const html = payload.data.map(row => `<tr>
    <td class="mono-cell">${escapeHtml(formatTime(new Date(row.timestamp_ms).toISOString()))}</td>
    <td>${escapeHtml(labels[row.kind] || "待分类")}</td>
    <td class="mono-cell">${escapeHtml(row.inst_id || "--")}</td>
    <td class="mono-cell">${escapeHtml(row.currency)}</td>
    ${["realized_pnl", "fees", "funding", "cash_flow"].map(field => `<td class="mono-cell">${escapeHtml(row[field] ?? "--")}</td>`).join("")}
  </tr>`).join("");
  $("#bill-history-body").innerHTML = html || '<tr><td colspan="8" class="table-empty">该日期范围暂无已补录账单</td></tr>';
  setText("#bill-history-range", `${range.start_day} 至 ${range.end_day} UTC · 共 ${payload.total} 条 · 原币种${payload.coverage.last_imported_at ? ` · 采集于 ${formatTime(payload.coverage.last_imported_at)}` : ""}`);
  const coverage = payload.coverage;
  setState("#bill-history-coverage", `已覆盖 ${coverage.completed_days} / ${coverage.total_days} 天`, coverage.complete ? "good" : "warning");
  setState("#bill-history-status", coverage.complete ? "分页采集完整" : "存在历史缺口", coverage.complete ? "good" : "warning");
  const summary = payload.summary;
  $("#bill-history-summary").innerHTML = Object.entries(summary.swap_pnl_by_currency || {})
    .map(([ccy, values]) => `<span>${escapeHtml(ccy)} 已识别永续净损益<strong>${escapeHtml(values.net_pnl)}</strong></span>`).join("")
    + Object.entries(summary.trading_account_transfers || {})
      .map(([ccy, value]) => `<span>${escapeHtml(ccy)} 账户划转<strong>${escapeHtml(value)}</strong></span>`).join("");
  const unclassified = summary.counts?.unclassified || 0;
  setState("#bill-history-message",
    `未作历史汇率估值，不计算账户总收益${unclassified ? ` · ${unclassified} 条账单待分类` : ""}${!payload.configured ? " · OKX 私有凭据未配置" : ""}`,
    unclassified ? "warning" : "neutral");
  billHistoryState.report = payload;
  billHistoryState.range = range;
  billHistoryState.page = page;
  billHistoryState.nextCursor = payload.next_cursor;
  setText("#bill-history-page", `第 ${page + 1} 页`);
}

async function loadBillHistory({ reset = false, page = billHistoryState.page } = {}) {
  if (!state.token) return;
  if (!$("#bill-history-form").reportValidity()) return;
  const request = ++billHistoryState.request;
  const token = state.token;
  const range = billHistoryRange();
  if (reset) page = 0;
  const params = new URLSearchParams({ ...range, limit: "100" });
  const cursor = reset ? null : billHistoryState.cursors[page];
  if (cursor) {
    params.set("before_ts", cursor.timestamp_ms);
    params.set("before_id", cursor.bill_id);
  }
  billHistoryState.loading = true;
  setBusy("#query-bill-history", true, "读取中...");
  setState("#bill-history-message", "正在读取历史账本...", "neutral");
  updateBillHistoryAvailability();
  try {
    const payload = await api(`/api/v1/account/bills/history?${params}`);
    if (request !== billHistoryState.request || token !== state.token) return;
    renderBillHistory(payload, range, page);
    if (reset) billHistoryState.cursors = [null];
  } catch (error) {
    if (request === billHistoryState.request && token === state.token) {
      if (handleBillHistoryUnauthorized(error)) return;
      setState("#bill-history-message", `查询失败：${error.message}${billHistoryState.report ? "；保留上次范围的数据" : ""}`, "danger");
    }
  } finally {
    if (request === billHistoryState.request) {
      billHistoryState.loading = false;
      setBusy("#query-bill-history", false);
      updateBillHistoryAvailability();
    }
  }
}

function renderBillImport(job) {
  if (job && (!/^[a-f0-9]{32}$/.test(job.id) || !["running", "completed", "failed", "interrupted"].includes(job.status))) {
    throw new Error("补录状态格式无效");
  }
  billHistoryState.job = job;
  $("#bill-import-progress").hidden = !job;
  if (!job) return;
  const labels = { running: "补录中", completed: "补录完成", failed: "补录失败", interrupted: "补录中断" };
  setState("#bill-import-status", labels[job.status] || "状态未知", job.status === "completed" ? "good" : "warning");
  setText("#bill-import-range", `${job.start_day} 至 ${job.end_day} UTC`);
  $("#bill-import-meter").max = Math.max(1, job.total_days);
  $("#bill-import-meter").value = job.completed_days;
  setText("#bill-import-count", `${job.completed_days} / ${job.total_days} 天 · ${job.rows_imported} 条`);
}

function scheduleBillImportPoll() {
  clearTimeout(billHistoryState.timer);
  billHistoryState.timer = null;
  if (state.privateFeedState !== "open" && state.token && $("#bill-history").open && document.body.dataset.view === "performance"
      && billHistoryState.job?.status === "running") {
    billHistoryState.timer = setTimeout(() => refreshBillImport(billHistoryState.job?.id), 1500);
  }
}

async function refreshBillImport(jobId = null) {
  if (!state.token) return;
  const token = state.token;
  const request = ++billHistoryState.jobRequest;
  const previous = billHistoryState.job?.status;
  try {
    const payload = await api(`/api/v1/account/bills/imports${jobId ? `/${encodeURIComponent(jobId)}` : ""}`);
    if (request !== billHistoryState.jobRequest || token !== state.token) return;
    renderBillImport(payload.job);
    if (previous === "running" && payload.job?.status !== "running") {
      await loadBillHistory({ reset: true });
    }
  } catch (error) {
    if (request === billHistoryState.jobRequest && token === state.token) {
      if (handleBillHistoryUnauthorized(error)) return;
      setState("#bill-history-message", `补录状态读取失败：${error.message}`, "warning");
    }
  } finally {
    if (request === billHistoryState.jobRequest && token === state.token) {
      updateBillHistoryAvailability();
      scheduleBillImportPoll();
    }
  }
}

async function startBillImport() {
  if ($("#import-bill-history").disabled || !state.token || !$("#bill-history-form").reportValidity()) return;
  const token = state.token;
  const request = ++billHistoryState.jobRequest;
  billHistoryState.starting = true;
  setBusy("#import-bill-history", true, "提交中...");
  try {
    const payload = await api("/api/v1/account/bills/imports", {
      method: "POST", body: JSON.stringify(billHistoryRange()),
    });
    if (request !== billHistoryState.jobRequest || token !== state.token) return;
    if (!payload.job) throw new Error("补录请求缺少任务记录");
    renderBillImport(payload.job);
    setState("#bill-history-message", "补录请求已受理，尚未完成", "neutral");
  } catch (error) {
    if (request === billHistoryState.jobRequest && token === state.token) {
      if (handleBillHistoryUnauthorized(error)) return;
      setState("#bill-history-message", `未能开始补录：${error.message}`, "danger");
    }
  } finally {
    if (request === billHistoryState.jobRequest) {
      billHistoryState.starting = false;
      setBusy("#import-bill-history", false);
      updateBillHistoryAvailability();
      scheduleBillImportPoll();
    }
  }
}

function updateBillHistoryView() {
  scheduleBillImportPoll();
}

function initializeBillHistory() {
  const today = new Date().toISOString().slice(0, 10);
  const yesterday = new Date(Date.parse(today) - 86400000).toISOString().slice(0, 10);
  $("#bill-history-start").value = new Date(Date.parse(today) - 7 * 86400000).toISOString().slice(0, 10);
  $("#bill-history-end").value = yesterday;
  $("#bill-history-start").max = yesterday;
  $("#bill-history-end").max = yesterday;
  $("#bill-history-form").addEventListener("submit", event => {
    event.preventDefault();
    loadBillHistory({ reset: true });
  });
  $("#bill-history-form").addEventListener("input", updateBillHistoryAvailability);
  $("#import-bill-history").addEventListener("click", startBillImport);
  $("#bill-history-previous").addEventListener("click", () => loadBillHistory({ page: billHistoryState.page - 1 }));
  $("#bill-history-next").addEventListener("click", () => {
    billHistoryState.cursors[billHistoryState.page + 1] = billHistoryState.nextCursor;
    loadBillHistory({ page: billHistoryState.page + 1 });
  });
  $("#bill-history").addEventListener("toggle", () => {
    if ($("#bill-history").open && state.token) {
      if (!billHistoryState.report) loadBillHistory({ reset: true });
      refreshBillImport(billHistoryState.job?.id);
    } else scheduleBillImportPoll();
  });
  updateBillHistoryAvailability();
}
