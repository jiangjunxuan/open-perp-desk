async function exerciseResearch() {
  const checks = {};
  const originalApi = api;
  const original = {
    token: state.token, analysis: state.analysis, symbol: state.symbol, bar: state.bar,
    status: state.status, draftRevision: state.draftRevision,
    modeTouched: researchState.modeTouched, runMode: selectedResearchMode(),
  };
  const runtime = {
    configured: true, enabled: true, runtime_state: "ready", busy: false,
    run_mode: "fast", available_run_modes: ["fast", "full"], timeout_seconds: 300,
  };
  const calls = [];
  const sample = {
    id: 41, inst_id: "BTC-USDT-SWAP", source: "TradingAgents", bias: "research",
    created_at: "2026-09-13T04:10:00+00:00", signal: {},
    report: {
      decision: "Hold",
      execution_authorized: false,
      market_context: { bar: "15m", candle_count: 100, captured_at: "2026-09-13T04:09:00+00:00", errors: [] },
      state: {
        market_report: [
          "# 市场分析",
          "",
          "浏览器验收样本，非真实模型结论。**趋势判断**与风险约束分开记录。",
          "",
          "| 观察项 | 结论 |",
          "| --- | --- |",
          "| 价格结构 | 区间震荡 |",
          "| 数据周期 | 15 分钟 |",
          "",
          "1. 等待方向确认",
          "2. 检查波动与流动性",
          "",
          "[来源](https://www.okx.com/)",
          "[坏链接](javascript:window.reportAttack=1)",
          "[本地动作](/api/v1/safety/emergency-stop)",
          "![跟踪像素](https://example.invalid/report-pixel)",
          "<script>window.reportAttack=1</script>",
          '<img src="https://example.invalid/raw-pixel" onerror="window.reportAttack=1">',
          '<svg onload="window.reportAttack=1"></svg>',
          '<iframe src="https://example.invalid/report-frame"></iframe>',
        ].join("\n"),
        sentiment_report: "市场情绪样本",
        news_report: "新闻研究样本",
        investment_plan: "投资研究样本",
        trader_investment_plan: "交易计划样本",
        final_trade_decision: "风险评议样本",
      },
    },
  };
  const metadata = ({ report, signal, ...row }) => row;
  const normalApi = async (url, options) => {
    calls.push({ url, options });
    if (url === "/api/v1/analysis/status") return { tradingagents: runtime };
    if (url === "/api/v1/analysis/ai/check") return { data: { provider_connection_verified: false } };
    if (url.startsWith("/api/v1/analysis/history?")) {
      const page = new URL(url, location.origin).searchParams.get("before_id");
      return { data: [metadata({ ...sample, id: page ? 31 : 41 })], next_before_id: page ? null : 41 };
    }
    if (/\/analysis\/\d+$/.test(url)) return { data: { ...sample, id: Number(url.split("/").pop()) } };
    throw new Error(`Unexpected fixture request: ${url}`);
  };
  const restore = () => {
    api = originalApi;
    state.token = original.token;
    state.symbol = original.symbol;
    state.bar = original.bar;
    state.status = original.status;
    $("#symbol").value = original.symbol;
    $("#bar").value = original.bar;
    setText("#heading-symbol", original.symbol);
    setText("#ticket-symbol", original.symbol);
    researchState.runBusy = false;
    researchState.checkBusy = false;
    researchState.modeTouched = original.modeTouched;
    $(`#ai-mode-${original.runMode}`).checked = true;
    resetResearchAccess();
    renderAnalysis(original.analysis);
    state.draftRevision = original.draftRevision;
    updatePrivateActionAvailability();
    delete window.reportAttack;
    delete window.__showFastResearchFixture;
    delete window.__restoreResearchFixture;
  };
  window.__restoreResearchFixture = restore;
  try {
    api = normalApi;
    state.token = "local-browser-fixture-only";
    state.symbol = sample.inst_id;
    state.bar = "15m";
    $("#symbol").value = sample.inst_id;
    $("#bar").value = "15m";
    setText("#heading-symbol", sample.inst_id);
    setText("#ticket-symbol", sample.inst_id);
    showView("strategies");
    await loadResearchStatus();
    await loadResearchHistory({ reset: true });
    checks.historyMetadataLoaded = $("#history-list button")?.textContent.includes(sample.inst_id)
      && researchState.historyRows[0].report === undefined;
    checks.runtimeReady = $("#ai-runtime-state").textContent === "运行时就绪" && !$("#check-ai-runtime").disabled;
    checks.fastModeVisible = $("#ai-runtime-note").textContent.includes("快速 OKX 研究");
    checks.modeSelectorReady = $("#ai-mode-fast").checked && !$("#ai-mode-full").disabled;
    await checkResearchRuntime();
    checks.selfCheckHonest = $("#research-run-status").textContent.includes("模型服务连通性尚未验证");
    renderAnalysis({ source: "structured-technical", signal: { action: "hold", inst_id: sample.inst_id } });
    const signal = state.analysis;
    const revision = state.draftRevision;
    await selectResearch(sample.id, { focus: false });
    checks.historyReadOnly = state.analysis === signal && state.draftRevision === revision;
    checks.completeSections = $("#report-section").options.length === 7 && researchState.record.id === sample.id;
    const fastSample = structuredClone(sample);
    fastSample.report.mode = "fast";
    fastSample.report.state.fast_research = {
      decision: "Hold", confidence: 0.64, summary: "价格仍在区间内，等待有效突破。", trend: "区间震荡", time_horizon: "日内",
      evidence: ["OKX K 线结构"], risks: ["波动放大"], invalidations: ["突破区间"],
    };
    fastSample.report.state.evidence_scope = {
      market: { status: "collected", source: "okx_public_snapshot" },
      sentiment: { status: "not_collected", reason: "fast_mode" },
      news: { status: "not_collected", reason: "fast_mode" },
      macro: { status: "not_collected", reason: "fast_mode" },
    };
    fastSample.report.state.execution_handoff = {
      status: "requires_structured_strategy", strategy_id: "structured-technical",
    };
    fastSample.report.state.news_report = "快速模式未接入新闻、宏观或基本面数据。";
    fastSample.report.state.trader_investment_plan = "快速研究结果不可执行；请使用结构化策略、风控和人工闸门完成任何后续操作。";
    window.__showFastResearchFixture = () => {
      api = normalApi;
      state.symbol = sample.inst_id;
      state.bar = "15m";
      displayResearch(fastSample);
      $("#research-history").open = true;
      renderResearchEvidence();
    };
    displayResearch(fastSample);
    const fastLabels = [...$("#report-section").options].map(option => option.textContent);
    checks.fastScopeClear = fastLabels.length === 5
      && !fastLabels.includes("市场情绪") && !fastLabels.includes("新闻分析")
      && !$("#report-mode-notice").hidden
      && $("#report-mode-title").textContent === "本次结论只使用 OKX 公共行情"
      && $("#report-mode-context").textContent.includes(`${sample.inst_id} · 15m`)
      && !$("#run-full-research").disabled;
    let upgradeOptions;
    api = async (url, options) => {
      if (url === "/api/v1/analysis/ai") {
        upgradeOptions = options;
        return { data: { ...sample.report, ...metadata(sample), mode: "full" } };
      }
      return normalApi(url, options);
    };
    await runFullResearchFromReport();
    checks.fullResearchRunsDirectly = JSON.parse(upgradeOptions.body).run_mode === "full"
      && researchState.record.report.mode === "full" && $("#report-mode-notice").hidden;
    api = normalApi;
    displayResearch(fastSample);
    $("#report-section").value = "3";
    renderResearchSection();
    checks.fastPlanStructured = $("#report-section-body .report-plan-heading strong").textContent === "观望"
      && $("#report-section-body .report-plan-summary").textContent.includes("等待有效突破")
      && $("#report-section-body .report-execution-boundary strong").textContent === "执行权限未授予"
      && Boolean($("#report-section-body [data-run-structured-analysis]"));
    displayResearch(sample);
    $("#report-section").value = "1";
    $("#report-section").dispatchEvent(new Event("change"));
    const prose = $("#report-section-body");
    checks.markdownRendered = Boolean(prose.querySelector("h1") && prose.querySelector("strong") && prose.querySelector("table") && prose.querySelector("ol"));
    checks.unsafeContentRemoved = !prose.querySelector("script, img, svg, iframe, style, [onclick], [onerror], [onload]")
      && !window.reportAttack
      && [...prose.querySelectorAll("a[href]")].every(link => link.href === "https://www.okx.com/"
        && link.target === "_blank" && link.rel === "noopener noreferrer");
    checks.noReportTracking = !performance.getEntriesByType("resource").some(entry => entry.name.includes("example.invalid"));
    state.symbol = "ETH-USDT-SWAP";
    state.bar = "1H";
    renderResearchEvidence();
    checks.contractMismatchVisible = !$("#report-warning").hidden
      && $("#report-warning").textContent.includes("ETH-USDT-SWAP") && $("#report-warning").textContent.includes("1H");
    state.symbol = sample.inst_id;
    state.bar = "15m";
    $("#history-source").value = "TradingAgents";
    $("#history-contract").value = "current";
    await loadResearchHistory({ reset: true });
    checks.filtersOnServer = calls.some(call => call.url.includes("source=TradingAgents") && call.url.includes("inst_id=BTC-USDT-SWAP"));
    researchState.cursors[1] = researchState.nextCursor;
    await loadResearchHistory({ page: 1 });
    checks.cursorPagination = researchState.page === 1 && researchState.historyRows[0].id === 31
      && !$("#history-previous").disabled && $("#history-next").disabled;
    await loadResearchHistory({ page: 0 });
    checks.previousPage = researchState.historyRows[0].id === 41;

    const pendingHistory = [];
    api = (url, options) => url.startsWith("/api/v1/analysis/history?")
      ? new Promise(resolve => pendingHistory.push(resolve)) : normalApi(url, options);
    const oldHistory = loadResearchHistory({ reset: true });
    const newHistory = loadResearchHistory({ reset: true });
    pendingHistory[1]({ data: [metadata({ ...sample, id: 92 })], next_before_id: null });
    await newHistory;
    pendingHistory[0]({ data: [metadata({ ...sample, id: 91 })], next_before_id: null });
    await oldHistory;
    checks.staleHistoryIgnored = researchState.historyRows[0].id === 92;

    const pendingDetails = [];
    api = (url, options) => /\/analysis\/\d+$/.test(url)
      ? new Promise(resolve => pendingDetails.push(resolve)) : normalApi(url, options);
    const oldDetail = selectResearch(91, { focus: false });
    const newDetail = selectResearch(92, { focus: false });
    pendingDetails[1]({ data: { ...sample, id: 92 } });
    await newDetail;
    pendingDetails[0]({ data: { ...sample, id: 91 } });
    await oldDetail;
    checks.staleDetailIgnored = researchState.record.id === 92;

    const lockedDetail = selectResearch(93, { focus: false });
    state.token = "";
    updatePrivateActionAvailability();
    pendingDetails[2]({ data: { ...sample, id: 93 } });
    await lockedDetail;
    checks.lockClearsReport = researchState.record === null && $("#report-content").hidden
      && $("#download-research").disabled && !$("#history-list").children.length;
    state.token = "local-browser-fixture-only";
    api = async (url, options) => {
      if (/\/analysis\/\d+$/.test(url)) throw new Error("fixture report missing");
      return normalApi(url, options);
    };
    await selectResearch(94, { focus: false });
    checks.reportErrorState = $("#report-empty strong").textContent === "报告读取失败"
      && $("#download-research").disabled;

    api = async (url, options) => {
      if (url === "/api/v1/analysis/ai/check") throw new Error("fixture runtime failure");
      return normalApi(url, options);
    };
    await loadResearchStatus();
    await checkResearchRuntime();
    checks.runtimeFailureState = $("#research-run-status").dataset.tone === "danger"
      && !researchState.checkBusy && !$("#check-ai-runtime").disabled;

    let resolveAI;
    api = (url, options) => url === "/api/v1/analysis/ai"
      ? new Promise(resolve => { resolveAI = resolve; }) : normalApi(url, options);
    const aiRun = runAiAnalysis();
    checks.aiClearsExecutableSignal = state.analysis === null && $("#execute-signal").disabled && researchState.runBusy;
    state.analysisRequest += 1;
    const newerSignal = { source: "structured-technical", signal: { action: "hold", inst_id: sample.inst_id } };
    renderAnalysis(newerSignal);
    resolveAI({ data: { ...sample.report, ...metadata(sample) } });
    await aiRun;
    checks.staleAIIgnoresSignal = state.analysis === newerSignal && researchState.record === null;

    let completedAiOptions;
    api = async (url, options) => {
      if (url === "/api/v1/analysis/ai") {
        completedAiOptions = options;
        return { data: { ...sample.report, ...metadata(sample) } };
      }
      return normalApi(url, options);
    };
    await runAiAnalysis();
    checks.aiCompletionReadOnly = state.analysis === null && $("#execute-signal").disabled
      && $("#preview-signal").disabled && researchState.record.id === sample.id
      && $("#analysis-summary").textContent.includes("观望");
    checks.fullModeRequested = JSON.parse(completedAiOptions.body).run_mode === "full";
    api = normalApi;
    researchState.sections = [["large", "长报告", "样".repeat(80001)]];
    $("#report-section").replaceChildren(new Option("长报告", "0"));
    renderResearchSection();
    checks.largeSectionBounded = Boolean($("#report-section-body .report-truncated"))
      && $("#report-section-body").textContent.length < 81000;
    displayResearch(sample);
    $("#report-section").value = "1";
    renderResearchSection();
    $("#research-history").open = true;
    researchRunMessage("浏览器验收样本 · 非真实模型结论");
    setMessage("");
    updatePrivateActionAvailability();
    renderResearchEvidence();
    checks.controlsContained = [".research-runtime", ".history-toolbar", ".history-pagination", ".report-section-control", ".ai-research-control", ".ai-mode-toggle"]
      .every(selector => {
        const parent = $(selector).getBoundingClientRect();
        return [...$(selector).children].filter(child => child.getClientRects().length).every(child => {
          const rect = child.getBoundingClientRect();
          return rect.left >= parent.left - 1 && rect.right <= parent.right + 1;
        });
      });
    checks.noDocumentOverflow = document.documentElement.scrollWidth <= innerWidth;
    checks.completeSafeFlow = !calls.some(call => call.url.includes("/execution") || call.url.includes("/orders"));
    return checks;
  } catch (error) {
    restore();
    throw error;
  }
}

export async function checkResearchUI(evaluate) {
  return evaluate(`(${exerciseResearch.toString()})()`);
}
