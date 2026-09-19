const researchState = {
  runtime: null,
  runtimeRequest: 0,
  historyRequest: 0,
  reportRequest: 0,
  historyRows: [],
  cursors: [null],
  page: 0,
  nextCursor: null,
  record: null,
  sections: [],
  runBusy: false,
  checkBusy: false,
  modeTouched: false,
  loaded: false,
};

const researchSources = { "structured-technical": "技术策略", TradingAgents: "TradingAgents" };
const researchBiases = { bullish: "偏多", bearish: "偏空", neutral: "中性", research: "AI 研究" };
const runtimeErrors = {
  disabled: "AI 研究未启用",
  source_missing: "TradingAgents 源码未配置",
  runtime_missing: "独立 Python 运行环境不可用",
  invalid_config: "运行参数配置无效",
  provider_configuration: "模型服务配置未完成",
  provider_unavailable: "模型服务暂时不可用，系统会在下次运行时重试",
  timeout: "研究超时",
  busy: "已有研究任务正在运行",
  runtime_failed: "研究进程未完成",
  invalid_result: "研究结果格式无效",
  output_limit: "研究结果超出大小限制",
  request_limit: "研究请求内容过大",
  storage_unavailable: "研究存储不可用",
};

function researchRunMessage(message, tone = "neutral") {
  setState("#research-run-status", message, tone);
  $("#research-run-status").hidden = !message;
}

function selectedResearchMode() {
  return document.querySelector('input[name="ai-run-mode"]:checked')?.value === "full" ? "full" : "fast";
}

function setResearchMode(mode, { user = false } = {}) {
  const input = $(`#ai-mode-${mode === "full" ? "full" : "fast"}`);
  if (!input || (user && input.disabled)) return false;
  input.checked = true;
  if (user) researchState.modeTouched = true;
  return true;
}

function updateResearchAvailability() {
  const unlocked = Boolean(state.token);
  const runtime = researchState.runtime;
  $("#check-ai-runtime").disabled = !unlocked || !runtime?.configured
    || runtime.busy || researchState.checkBusy || researchState.runBusy;
  $("#run-ai-analysis").disabled = !unlocked || !runtime?.configured
    || runtime.busy || researchState.checkBusy || researchState.runBusy
    || $("#run-ai-analysis").hasAttribute("aria-busy");
  const modes = new Set(runtime?.available_run_modes || ["fast", "full"]);
  document.querySelectorAll('input[name="ai-run-mode"]').forEach(input => {
    input.disabled = !unlocked || !runtime?.configured || runtime.busy
      || researchState.checkBusy || researchState.runBusy || !modes.has(input.value);
  });
  for (const selector of ["#history-source", "#history-contract", "#refresh-history"]) {
    $(selector).disabled = !unlocked;
  }
  $("#history-previous").disabled = !unlocked || researchState.page === 0
    || $("#research-history").getAttribute("aria-busy") === "true";
  $("#history-next").disabled = !unlocked || !researchState.nextCursor
    || $("#research-history").getAttribute("aria-busy") === "true";
  $("#download-research").disabled = !unlocked || !researchState.record;
  const fullResearch = $("#run-full-research");
  if (fullResearch) {
    fullResearch.disabled = !unlocked || !runtime?.configured || runtime.busy
      || researchState.checkBusy || researchState.runBusy || $("#ai-mode-full").disabled;
  }
  if (!unlocked && (researchState.loaded || researchState.record)) resetResearchAccess();
}

function resetResearchAccess() {
  researchState.historyRequest += 1;
  researchState.reportRequest += 1;
  researchState.loaded = false;
  researchState.historyRows = [];
  researchState.cursors = [null];
  researchState.page = 0;
  researchState.nextCursor = null;
  $("#history-list").replaceChildren();
  $("#research-history").removeAttribute("aria-busy");
  setText("#history-count", "已锁定");
  setText("#history-message", "研究记录已锁定");
  setText("#history-page", "第 1 页");
  showResearchEmpty("研究记录已锁定", "管理员访问未解锁");
  researchRunMessage("");
}

function renderResearchStatus(payload) {
  researchState.runtime = payload.tradingagents;
  const runtime = researchState.runtime || {};
  const checking = researchState.checkBusy;
  const busy = runtime.busy || researchState.runBusy;
  const label = checking ? "自检中" : busy ? "研究运行中"
    : !runtime.configured ? "未就绪"
      : ({ ready: "运行时就绪", failed: "运行失败", canceled: "已取消" })[runtime.runtime_state] || "待自检";
  const modeLabel = runtime.run_mode === "fast" ? "快速 OKX 研究" : "完整辩论研究";
  if (!researchState.modeTouched && runtime.run_mode) setResearchMode(runtime.run_mode);
  setState("#ai-runtime-state", label, !runtime.configured || runtime.runtime_state === "failed"
    ? "warning" : runtime.runtime_state === "ready" ? "good" : "neutral");
  setText("#ai-runtime-note", runtime.last_error
    ? runtimeErrors[runtime.last_error] || `运行状态：${runtime.last_error}`
    : `默认 ${modeLabel} · 研究与交易隔离 · 时限 ${runtime.timeout_seconds || "--"} 秒 · 自检不验证模型连通性`);
  updateResearchAvailability();
}

async function loadResearchStatus() {
  const request = ++researchState.runtimeRequest;
  try {
    const payload = await api("/api/v1/analysis/status");
    if (request !== researchState.runtimeRequest) return;
    renderResearchStatus(payload);
  } catch {
    if (request !== researchState.runtimeRequest) return;
    researchState.runtime = null;
    setState("#ai-runtime-state", "状态不可用", "warning");
    setText("#ai-runtime-note", "AI 状态读取失败");
  } finally {
    if (request === researchState.runtimeRequest) updateResearchAvailability();
  }
}

async function checkResearchRuntime() {
  if ($("#check-ai-runtime").disabled) return;
  const token = state.token;
  researchState.checkBusy = true;
  setBusy("#check-ai-runtime", true, "自检中...");
  updateResearchAvailability();
  setState("#ai-runtime-state", "自检中");
  researchRunMessage("正在检查独立运行环境...");
  try {
    await api("/api/v1/analysis/ai/check", { method: "POST" });
    if (token !== state.token) return;
    researchRunMessage("运行时自检通过；模型服务连通性尚未验证。", "good");
  } catch (error) {
    if (token === state.token) researchRunMessage(`运行时自检未通过：${error.message}`, "danger");
  } finally {
    researchState.checkBusy = false;
    setBusy("#check-ai-runtime", false);
    await loadResearchStatus();
  }
}

function renderResearchHistory() {
  const list = $("#history-list");
  const focusedId = list.contains(document.activeElement) ? document.activeElement.dataset.analysisId : null;
  list.replaceChildren();
  for (const row of researchState.historyRows) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.analysisId = String(row.id);
    button.className = "history-entry";
    button.setAttribute("aria-pressed", String(researchState.record?.id === row.id));
    const identity = document.createElement("strong");
    identity.textContent = row.inst_id;
    const source = document.createElement("span");
    source.textContent = researchSources[row.source] || row.source;
    const time = document.createElement("time");
    time.dateTime = row.created_at;
    time.textContent = formatTime(row.created_at);
    const bias = document.createElement("span");
    bias.textContent = researchBiases[row.bias] || row.bias;
    button.append(identity, source, time, bias);
    item.append(button);
    list.append(item);
  }
  if (focusedId) [...list.querySelectorAll("button")].find(button => button.dataset.analysisId === focusedId)?.focus({ preventScroll: true });
  setText("#history-count", `${researchState.historyRows.length} 条`);
  setText("#history-page", `第 ${researchState.page + 1} 页`);
  updateResearchAvailability();
}

async function loadResearchHistory({ reset = false, page = researchState.page } = {}) {
  if (!state.token) return;
  const token = state.token;
  const request = ++researchState.historyRequest;
  if (reset) {
    researchState.cursors = [null];
    page = 0;
    researchState.page = 0;
    researchState.nextCursor = null;
    researchState.historyRows = [];
    renderResearchHistory();
  }
  const params = new URLSearchParams({ limit: "10" });
  const cursor = researchState.cursors[page];
  if (cursor) params.set("before_id", String(cursor));
  if ($("#history-source").value) params.set("source", $("#history-source").value);
  if ($("#history-contract").value) params.set("inst_id", state.symbol);
  $("#research-history").setAttribute("aria-busy", "true");
  setText("#history-message", "正在读取研究记录...");
  updateResearchAvailability();
  try {
    const payload = await api(`/api/v1/analysis/history?${params}`);
    if (request !== researchState.historyRequest || token !== state.token) return;
    researchState.historyRows = payload.data || [];
    researchState.page = page;
    researchState.nextCursor = payload.next_before_id || null;
    researchState.loaded = true;
    setText("#history-message", researchState.historyRows.length ? "UTC 时间 · 按新到旧排列" : "没有匹配的研究记录");
    renderResearchHistory();
  } catch (error) {
    if (request === researchState.historyRequest && token === state.token) {
      setText("#history-message", `研究记录读取失败：${error.message}`);
    }
  } finally {
    if (request === researchState.historyRequest) {
      $("#research-history").removeAttribute("aria-busy");
      updateResearchAvailability();
    }
  }
}

function showResearchEmpty(title = "暂无研究报告", note = "当前合约尚无已选择的研究记录") {
  researchState.record = null;
  researchState.sections = [];
  $("#report-content").hidden = true;
  $("#report-safety").hidden = true;
  $("#report-empty").hidden = false;
  setText("#report-empty strong", title);
  setText("#report-empty span", note);
  setText("#research-title", "研究报告");
  setText("#report-meta", "暂无报告");
  $("#download-research").disabled = true;
  $("#report-mode-notice").hidden = true;
  $("#report-section-body").replaceChildren();
  $("#open-research-report").hidden = true;
  $("#report-reader").removeAttribute("aria-busy");
}

function reportValuePresent(value) {
  return value != null && value !== "" && (typeof value !== "object" || Object.keys(value).length > 0);
}

function isFastResearch(record = researchState.record) {
  const report = record?.report || {};
  return report.mode === "fast" || Boolean(report.state?.fast_research);
}

function fastEvidenceMissing(fields, key, value) {
  if (fields.evidence_scope?.[key]?.status === "not_collected") return true;
  return typeof value === "string" && value.startsWith("快速模式未接入");
}

function researchSections(record) {
  const report = record.report || {};
  if (record.source !== "TradingAgents") {
    return [
      ["summary", "策略摘要", report.summary],
      ["risk", "风险说明", report.risk_note],
      ["signal", "信号快照", record.signal],
    ].filter(([, , value]) => reportValuePresent(value));
  }
  const fields = report.state || {};
  const fast = isFastResearch(record);
  return [
    ["decision", "研究结论", report.decision],
    ["market", "市场分析", fields.market_report],
    ["sentiment", "市场情绪", fields.sentiment_report],
    ["news", "新闻分析", fields.news_report],
    ["investment", "投资研究", fields.investment_plan],
    ["trader", "交易计划", fields.trader_investment_plan],
    ["final", "风险评议", fields.final_trade_decision],
    ["investment-debate", "研究辩论", fields.investment_debate_state],
    ["risk-debate", "风险辩论", fields.risk_debate_state],
  ].filter(([key, , value]) => reportValuePresent(value)
    && !(fast && ["news", "sentiment"].includes(key) && fastEvidenceMissing(fields, key, value)));
}

function appendReportList(parent, title, values, emptyText = "未给出") {
  const section = document.createElement("section");
  section.className = "report-plan-list";
  const heading = document.createElement("h4");
  heading.textContent = title;
  const list = document.createElement("ul");
  const items = Array.isArray(values) && values.length ? values.slice(0, 8) : [emptyText];
  items.forEach(value => {
    const item = document.createElement("li");
    item.textContent = String(value);
    list.append(item);
  });
  section.append(heading, list);
  parent.append(section);
}

function renderFastCapability(key, value, body) {
  const report = researchState.record?.report || {};
  const fields = report.state || {};
  if (!isFastResearch()) return false;

  const handoff = fields.execution_handoff || {};
  const structuredHandoff = handoff.status === "requires_structured_strategy";
  const legacyHandoff = typeof value === "string" && value.startsWith("快速研究结果不可执行");
  if (key === "trader" && (structuredHandoff || legacyHandoff)) {
    const fastResult = fields.fast_research || {};
    const plan = fields.investment_plan || {};
    const decision = String(fastResult.decision || plan.decision || report.decision || "Hold");
    const decisionLabel = ({ buy: "偏多", sell: "偏空", hold: "观望" })[decision.toLowerCase()] || decision;
    const confidence = Number(fastResult.confidence ?? plan.confidence);
    const section = document.createElement("section");
    section.className = "report-plan";
    const header = document.createElement("div");
    header.className = "report-plan-heading";
    const heading = document.createElement("div");
    const eyebrow = document.createElement("span");
    eyebrow.textContent = "研究交接";
    const title = document.createElement("strong");
    title.textContent = decisionLabel;
    heading.append(eyebrow, title);
    const tag = document.createElement("span");
    tag.className = "report-status-tag";
    tag.textContent = Number.isFinite(confidence) ? `置信度 ${Math.round(confidence * 100)}%` : "置信度未给出";
    header.append(heading, tag);
    section.append(header);

    const summary = fastResult.summary || fields.market_report;
    if (summary) {
      const summaryText = document.createElement("p");
      summaryText.className = "report-plan-summary";
      summaryText.textContent = String(summary);
      section.append(summaryText);
    }

    const facts = [
      ["趋势", fastResult.trend || plan.trend || "未给出"],
      ["周期", fastResult.time_horizon || plan.time_horizon || "未给出"],
    ];
    const details = document.createElement("dl");
    details.className = "report-plan-grid";
    facts.forEach(([termText, detailText]) => {
      const row = document.createElement("div");
      const term = document.createElement("dt");
      term.textContent = termText;
      const detail = document.createElement("dd");
      detail.textContent = String(detailText);
      row.append(term, detail);
      details.append(row);
    });
    section.append(details);
    appendReportList(section, "依据", fastResult.evidence || plan.evidence, "本次研究未给出明确依据");
    appendReportList(section, "风险", fastResult.risks || plan.risks, "本次研究未给出独立风险项");
    appendReportList(section, "失效条件", fastResult.invalidations || plan.invalidations, "本次研究未给出失效条件");

    const boundary = document.createElement("div");
    boundary.className = "report-execution-boundary";
    const boundaryText = document.createElement("div");
    const boundaryTitle = document.createElement("strong");
    boundaryTitle.textContent = "执行权限未授予";
    const boundaryDetail = document.createElement("span");
    boundaryDetail.textContent = "研究结论不会直接创建订单，必须重新生成结构化策略并通过独立风控。";
    boundaryText.append(boundaryTitle, boundaryDetail);
    const action = document.createElement("button");
    action.type = "button";
    action.className = "button secondary";
    action.dataset.runStructuredAnalysis = "true";
    action.disabled = !state.token || researchState.runBusy;
    action.textContent = "运行策略分析";
    boundary.append(boundaryText, action);
    section.append(boundary);
    body.append(section);
    return true;
  }
  return false;
}

function renderResearchModeNotice() {
  const notice = $("#report-mode-notice");
  if (!notice) return;
  const record = researchState.record;
  const fast = record?.source === "TradingAgents" && isFastResearch(record);
  notice.hidden = !fast;
  if (!fast) return;
  const reportBar = record.report?.market_context?.bar || "--";
  const sameContext = record.inst_id === state.symbol && reportBar === state.bar;
  setText(
    "#report-mode-context",
    sameContext
      ? `完整研究将重新采集 ${state.symbol} · ${state.bar}。`
      : `当前选择为 ${state.symbol} · ${state.bar}；完整研究将使用当前选择，不沿用旧报告的 ${record.inst_id} · ${reportBar}。`,
  );
  updateResearchAvailability();
}

function renderResearchSection() {
  const index = Number($("#report-section").value);
  const section = researchState.sections[index];
  const body = $("#report-section-body");
  body.replaceChildren();
  if (!section) return;
  const [key, title, value] = section;
  body.setAttribute("aria-label", title);
  setText("#report-section-count", `${index + 1} / ${researchState.sections.length}`);
  if (renderFastCapability(key, value, body)) {
    body.scrollTop = 0;
    return;
  }
  const text = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  const maximum = 80000;
  const bounded = text.slice(0, maximum);
  if (typeof value !== "string" || !window.marked || !window.DOMPurify?.isSupported) {
    const pre = document.createElement("pre");
    pre.textContent = bounded;
    body.append(pre);
  } else {
    try {
      const parser = new marked.Marked({ renderer: { html: () => "", image: () => "" } });
      const fragment = DOMPurify.sanitize(parser.parse(bounded, { async: false }), {
        ALLOWED_TAGS: ["p", "br", "hr", "strong", "em", "del", "blockquote", "ul", "ol", "li",
          "h1", "h2", "h3", "h4", "h5", "h6", "pre", "code", "table", "thead", "tbody",
          "tr", "th", "td", "a"],
        ALLOWED_ATTR: ["href", "title"],
        ALLOW_DATA_ATTR: false,
        ALLOW_ARIA_ATTR: false,
        RETURN_DOM_FRAGMENT: true,
      });
      // Only explicit public HTTP(S) links survive. No relative application actions or media.
      fragment.querySelectorAll("a").forEach(link => {
        const href = link.getAttribute("href") || "";
        try {
          const url = new URL(href);
          if (!/^https?:$/.test(url.protocol) || url.username || url.password) throw new Error();
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.referrerPolicy = "no-referrer";
        } catch {
          link.removeAttribute("href");
        }
      });
      fragment.querySelectorAll("table").forEach(table => {
        const wrapper = document.createElement("div");
        wrapper.className = "report-table-wrap";
        wrapper.tabIndex = 0;
        wrapper.setAttribute("role", "region");
        wrapper.setAttribute("aria-label", "报告表格");
        table.replaceWith(wrapper);
        wrapper.append(table);
      });
      body.append(fragment);
    } catch {
      const pre = document.createElement("pre");
      pre.textContent = bounded;
      body.append(pre);
    }
  }
  if (text.length > maximum) {
    const note = document.createElement("p");
    note.className = "report-truncated";
    note.textContent = "本章节显示前 80,000 个字符，下载文件包含完整内容。";
    body.append(note);
  }
  body.scrollTop = 0;
}

function renderResearchEvidence() {
  const record = researchState.record;
  if (!record) return;
  const context = record.source === "TradingAgents" ? record.report?.market_context || {} : {};
  const evidence = [
    ["研究合约", record.inst_id],
    ["研究模式", record.report?.mode === "full" ? "完整研究" : record.report?.mode === "fast" ? "快速研究" : "--"],
    ["数据周期", context.bar || "--"],
    ["K 线数量", context.candle_count == null ? "--" : `${context.candle_count} 根`],
    ["采集时间 · UTC", formatTime(context.captured_at)],
  ];
  const list = $("#report-evidence");
  list.replaceChildren();
  for (const [label, value] of evidence) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = value;
    row.append(term, detail);
    list.append(row);
  }
  const warnings = [];
  if (record.inst_id !== state.symbol) warnings.push(`报告属于 ${record.inst_id}，当前选中 ${state.symbol}`);
  if (context.bar && context.bar !== state.bar) warnings.push(`报告周期 ${context.bar}，当前周期 ${state.bar}`);
  if (context.errors?.length) {
    const labels = { ticker: "报价", candles: "K 线", funding_rate: "资金费率", open_interest: "持仓量" };
    warnings.push(`数据缺失：${context.errors.map(key => labels[key] || key).join("、")}`);
  }
  $("#report-warning").hidden = !warnings.length;
  setText("#report-warning", warnings.join("；"));
  renderResearchModeNotice();
}

function displayResearch(record, { focus = false } = {}) {
  researchState.record = record;
  researchState.sections = researchSections(record);
  $("#report-reader").removeAttribute("aria-busy");
  $("#report-empty").hidden = true;
  $("#report-content").hidden = false;
  $("#report-safety").hidden = false;
  $("#open-research-report").hidden = false;
  setText("#research-title", `${record.inst_id} 研究`);
  const mode = record.source === "TradingAgents"
    ? ({ fast: "快速研究", full: "完整研究" })[record.report?.mode]
    : null;
  setText("#report-meta", `${researchSources[record.source] || record.source}${mode ? ` · ${mode}` : ""} · ${formatTime(record.created_at)} UTC${record.id ? ` · #${record.id}` : ""}`);
  const select = $("#report-section");
  select.replaceChildren();
  researchState.sections.forEach(([, title], index) => select.add(new Option(title, String(index))));
  select.disabled = !researchState.sections.length;
  renderResearchSection();
  if (!researchState.sections.length) setText("#report-section-body", "此记录没有可显示的报告章节");
  renderResearchEvidence();
  renderResearchHistory();
  if (focus && document.body.dataset.view === "strategies") {
    if (matchMedia("(max-width: 767px)").matches) $("#research-history").open = false;
    $("#research-title").focus();
  }
}

async function selectResearch(id, { focus = true } = {}) {
  if (!state.token || !Number.isSafeInteger(id) || id < 1) return;
  const token = state.token;
  const request = ++researchState.reportRequest;
  showResearchEmpty("正在读取报告...", `研究记录 #${id}`);
  $("#report-reader").setAttribute("aria-busy", "true");
  try {
    const payload = await api(`/api/v1/analysis/${id}`);
    if (request !== researchState.reportRequest || token !== state.token) return;
    displayResearch(payload.data, { focus });
  } catch (error) {
    if (request === researchState.reportRequest && token === state.token) {
      showResearchEmpty("报告读取失败", error.message);
    }
  } finally {
    if (request === researchState.reportRequest) $("#report-reader").removeAttribute("aria-busy");
  }
}

function acceptCurrentResearch(analysis) {
  researchState.reportRequest += 1;
  const record = analysis.source === "TradingAgents"
    ? { id: analysis.id, inst_id: analysis.inst_id, source: analysis.source, bias: analysis.bias,
      created_at: analysis.created_at || analysis.generated_at, signal: {}, report: analysis }
    : analysis;
  displayResearch(record);
  loadResearchHistory({ reset: true });
}

async function runFullResearchFromReport() {
  const button = $("#run-full-research");
  if (!button || button.disabled || !setResearchMode("full", { user: true })) return;
  researchRunMessage(`${state.symbol} · ${state.bar} · 正在启动完整研究...`);
  await runAiAnalysis();
}

function initializeResearch() {
  $("#check-ai-runtime").addEventListener("click", checkResearchRuntime);
  $("#refresh-history").addEventListener("click", () => loadResearchHistory({ reset: true }));
  for (const selector of ["#history-source", "#history-contract"]) {
    $(selector).addEventListener("change", () => loadResearchHistory({ reset: true }));
  }
  $("#history-previous").addEventListener("click", () => {
    if (researchState.page > 0) loadResearchHistory({ page: researchState.page - 1 });
  });
  $("#history-next").addEventListener("click", () => {
    if (!researchState.nextCursor) return;
    researchState.cursors[researchState.page + 1] = researchState.nextCursor;
    loadResearchHistory({ page: researchState.page + 1 });
  });
  $("#history-list").addEventListener("click", event => {
    const button = event.target.closest("[data-analysis-id]");
    if (button) selectResearch(Number(button.dataset.analysisId));
  });
  $("#report-section").addEventListener("change", renderResearchSection);
  document.querySelectorAll('input[name="ai-run-mode"]').forEach(input => {
    input.addEventListener("change", () => {
      researchState.modeTouched = true;
    });
  });
  $("#run-full-research").addEventListener("click", runFullResearchFromReport);
  $("#report-section-body").addEventListener("click", event => {
    if (event.target.closest("[data-run-structured-analysis]")) $("#run-analysis").click();
  });
  $("#download-research").addEventListener("click", () => {
    if (!state.token || !researchState.record) return;
    const record = researchState.record;
    const blob = new Blob([JSON.stringify(record, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `research-${String(record.inst_id).replace(/[^A-Z0-9-]/g, "")}-${record.id || "current"}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  loadResearchStatus();
  setInterval(() => {
    if (state.controlFeedState !== "open" && (document.body.dataset.view === "strategies" || researchState.runBusy)) loadResearchStatus();
  }, 15000);
}
