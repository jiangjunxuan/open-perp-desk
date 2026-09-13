async function exerciseBillHistory() {
  const checks = {};
  const originalApi = api;
  const original = { token: state.token, status: state.status, analysis: state.analysis, revision: state.draftRevision };
  const calls = [];
  const range = billHistoryRange();
  const job = {
    id: "a".repeat(32), ...range, status: "running", completed_days: 1, total_days: 7, rows_imported: 100,
  };
  const record = {
    bill_id: "101", timestamp_ms: Date.parse(range.start_day) + 1000,
    kind: "trade", currency: "USDT", inst_id: "BTC-USDT-SWAP",
    realized_pnl: "0.00000000000000001", fees: "-0.1", funding: null, cash_flow: null,
  };
  const baseline = {
    day_utc: range.start_day, target_ms: Date.parse(range.start_day), status: "captured",
    request_started_ms: Date.parse(range.start_day) + 100,
    received_at_ms: Date.parse(range.start_day) + 500,
    equity_usd: "1000.00000000000000001",
  };
  const report = {
    configured: true, total: 101, data: [record],
    coverage: { complete: false, completed_days: 1, total_days: 7, last_imported_at: "2026-09-13T05:00:00Z" },
    summary: {
      swap_pnl_by_currency: { USDT: { net_pnl: "-0.09999999999999999" } },
      trading_account_transfers: { USDT: "500" }, counts: { unclassified: 1 }, valuation_status: "not_valued",
    },
    next_cursor: { timestamp_ms: record.timestamp_ms, bill_id: "101" },
    equity_baselines: { data: [baseline, {
      day_utc: range.end_day, target_ms: Date.parse(range.end_day), status: "missing", equity_usd: null,
    }] },
  };
  let handler = async url => url.includes("/archives") ? { data: [] } : url.includes("/history?")
    ? { ...report, data: url.includes("before_id=") ? [{ ...record, bill_id: "100", currency: "BTC", realized_pnl: null }] : report.data,
      next_cursor: url.includes("before_id=") ? null : report.next_cursor }
    : { job: null };
  const restore = () => {
    clearTimeout(billHistoryState.timer);
    $("#bill-history").open = false;
    api = originalApi;
    state.token = original.token;
    state.status = original.status;
    resetBillHistoryAccess();
    $("#bill-history-start").value = range.start_day;
    $("#bill-history-end").value = range.end_day;
    updatePrivateActionAvailability();
    delete window.billHistoryAttack;
    delete window.__restoreBillHistoryFixture;
  };
  window.__restoreBillHistoryFixture = restore;
  try {
    api = async (url, options) => { calls.push({ url, options }); return handler(url, options); };
    state.token = "";
    updatePrivateActionAvailability();
    await loadBillHistory();
    await startBillImport();
    await changeBillArchive();
    checks.lockedNoRequests = calls.length === 0 && $("#import-bill-history").disabled;
    state.token = "local-bill-history-fixture";
    state.status = { ...original.status, integrations: { ...original.status.integrations, okx_credentials_configured: true } };
    updatePrivateActionAvailability();
    showView("performance");
    $("#bill-history").open = true;
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    if (billHistoryState.loading) await new Promise(resolve => setTimeout(resolve, 20));
    checks.openLoadsReadOnly = calls.some(call => call.url.includes("/history?")) && calls.every(call => !call.options?.method);
    checks.coverageNotProfit = $("#bill-history-status").textContent.includes("缺口")
      && $("#bill-history-message").textContent.includes("不计算账户总收益")
      && $("#bill-history-message").textContent.includes("1 条账单待分类");
    checks.amountsExact = $("#bill-history-body").textContent.includes("0.00000000000000001")
      && $("#bill-history-summary").textContent.includes("-0.09999999999999999");
    checks.transfersSeparate = $("#bill-history-summary").textContent.includes("账户划转")
      && $("#bill-history-summary").textContent.includes("500");
    checks.baselineEvidence = $("#equity-baseline-status").textContent.includes("1 / 2")
      && $("#equity-baseline-status").textContent.includes("非零点精确估值")
      && $("#equity-baseline-body").textContent.includes("1000.00000000000000001")
      && $("#equity-baseline-body").textContent.includes("00:00:00.100")
      && $("#equity-baseline-body").textContent.includes("缺失")
      && $("#equity-baseline-body").textContent.includes("--");
    checks.captureDateVisible = $("#bill-history-range").textContent.includes("采集于 2026-09-13 05:00:00");
    billHistoryState.cursors[1] = billHistoryState.nextCursor;
    await loadBillHistory({ page: 1 });
    checks.cursorPagination = calls.at(-1).url.includes("before_id=101")
      && billHistoryState.page === 1 && $("#bill-history-next").disabled && !$("#bill-history-previous").disabled;
    checks.absentAmountsUnknown = $("#bill-history-body").textContent.includes("--");
    await loadBillHistory({ page: 0 });
    handler = async () => ({ ...report, data: [{ ...record, inst_id: '<img src=x onerror="window.billHistoryAttack=1">' }] });
    await loadBillHistory({ reset: true });
    checks.escapedRemoteContent = !$("#bill-history-body").querySelector("img, script")
      && !window.billHistoryAttack && $("#bill-history-body").textContent.includes("<img");
    const saved = billHistoryState.report;
    handler = async () => { throw new Error("fixture query failure"); };
    await loadBillHistory({ reset: true });
    checks.failurePreservesSnapshot = billHistoryState.report === saved && $("#bill-history-message").textContent.includes("保留上次范围");

    const pending = [];
    handler = () => new Promise(resolve => pending.push(resolve));
    const first = loadBillHistory({ reset: true });
    const second = loadBillHistory({ reset: true });
    pending[1]({ ...report, total: 202 });
    await second;
    pending[0](report);
    await first;
    checks.lateQueryIgnored = billHistoryState.report.total === 202 && !billHistoryState.loading;
    let resolveBaselineQuery;
    handler = () => new Promise(resolve => { resolveBaselineQuery = resolve; });
    const olderBaselineQuery = loadBillHistory();
    const newest = {...report, equity_baselines: {data: [{...baseline, equity_usd: "0"}]}};
    applyPrivateEvent("equity_baseline", {latest: {...baseline, equity_usd: "0"}});
    handler = async () => newest;
    resolveBaselineQuery(report);
    await olderBaselineQuery;
    checks.baselinePushRejectsOldQuery = billHistoryState.report === newest
      && $("#equity-baseline-body tr").children[1].textContent === "0";
    const beforeBaselinePush = calls.length;
    applyPrivateEvent("equity_baseline", {latest: {...baseline, received_at_ms: baseline.received_at_ms + 1}});
    await new Promise(resolve => setTimeout(resolve, 0));
    checks.baselinePushRefreshesVisibleRange = calls.length === beforeBaselinePush + 1;
    handler = async () => report;
    await loadBillHistory();
    $("#bill-history-start").value = range.end_day;
    updateBillHistoryAvailability();
    checks.rangeChangeBlocksOldCursor = $("#bill-history-next").disabled
      && $("#bill-history-range").textContent.includes(range.start_day);
    $("#bill-history-start").value = range.start_day;
    updateBillHistoryAvailability();

    let resolveImport;
    handler = () => new Promise(resolve => { resolveImport = resolve; });
    const postsBefore = calls.filter(call => call.options?.method === "POST").length;
    const starting = startBillImport();
    await startBillImport();
    checks.duplicateImportBlocked = calls.filter(call => call.options?.method === "POST").length === postsBefore + 1;
    applyPrivateEvent("bill_import", { job: { ...job, completed_days: 2 } });
    resolveImport({ job });
    await starting;
    checks.acceptanceNotCompletion = billHistoryState.job.status === "running"
      && $("#bill-history-message").textContent.includes("尚未完成")
      && $("#bill-import-meter").value === 2 && $("#import-bill-history").disabled;
    checks.importPushDoesNotLeaveBusy = !billHistoryState.starting && !$("#import-bill-history").hasAttribute("aria-busy");
    clearTimeout(billHistoryState.timer);
    handler = async url => url.includes("/history?") ? { ...report, coverage: { ...report.coverage, complete: true, completed_days: 7 } }
      : { job: { ...job, status: "completed", completed_days: 7 } };
    await refreshBillImport(job.id);
    checks.completionRefreshesCoverage = $("#bill-import-status").textContent === "补录完成"
      && $("#bill-history-status").textContent === "分页采集完整";
    renderBillImport(job);
    scheduleBillImportPoll();
    showView("markets");
    checks.pollStopsOutsideView = billHistoryState.timer === null;
    showView("performance");
    checks.pollResumesInView = billHistoryState.timer !== null;
    clearTimeout(billHistoryState.timer);
    handler = async () => { const error = new Error("Invalid admin token."); error.status = 401; throw error; };
    await loadBillHistory();
    checks.unauthorizedClears = state.token === "" && billHistoryState.report === null
      && $("#bill-history-body").textContent.includes("已锁定") && $("#bill-import-progress").hidden
      && $("#equity-baseline-body").textContent.includes("已锁定");
    checks.busyStateCleared = !$("#query-bill-history").hasAttribute("aria-busy")
      && !$("#import-bill-history").hasAttribute("aria-busy");

    state.token = "local-bill-history-fixture";
    updatePrivateActionAvailability();
    let resolveLate;
    handler = () => new Promise(resolve => { resolveLate = resolve; });
    const late = loadBillHistory();
    state.token = "";
    updatePrivateActionAvailability();
    resolveLate(report);
    await late;
    checks.permissionLossRejectsLateData = billHistoryState.report === null
      && $("#bill-history-body").textContent.includes("已锁定");
    state.token = "local-bill-history-fixture";
    updatePrivateActionAvailability();
    handler = async () => report;
    await loadBillHistory({ reset: true });
    checks.reauthenticationWorks = billHistoryState.report !== null && !$("#query-bill-history").disabled;
    checks.tradingDraftUntouched = state.analysis === original.analysis && state.draftRevision === original.revision;
    const archive = { id: "b".repeat(32), year: 2024, quarter: "Q1", state: "waiting", next_attempt_ms: Date.now() + 60000 };
    $("#bill-archive-year").value = "2024";
    $("#bill-archive-quarter").value = "Q1";
    handler = async () => {
      applyPrivateEvent("bill_archives", { data: [archive] });
      return { job: { ...archive, state: "queued" } };
    };
    await changeBillArchive();
    checks.archivePushWinsLateResponse = billHistoryState.archives[0].state === "waiting"
      && !billHistoryState.archiveBusy && !$("#request-bill-archive").hasAttribute("aria-busy")
      && $("#bill-archive-list").textContent.includes("等待文件");
    $("#bill-archive-year").value = "2023";
    applyPrivateEvent("bill_archives", { data: [{ ...archive, state: "downloading" }] });
    checks.archiveDraftPreserved = $("#bill-archive-year").value === "2023";
    handler = async () => ({ data: [{ ...archive, state: "canceled" }] });
    await changeBillArchive("cancel", archive);
    checks.archiveCancelIsExplicit = calls.at(-1).url.endsWith(`/${archive.id}/cancel`)
      && $("#bill-archive-list").textContent.includes("已停止");
    handler = async () => ({ job: { ...archive, state: "queued" } });
    await changeBillArchive("retry", archive);
    checks.archiveRetryKeepsQuarter = JSON.parse(calls.at(-1).options.body).retry === true
      && JSON.parse(calls.at(-1).options.body).year === 2024;
    const valued = {
      ...report, coverage: { ...report.coverage, complete: true, completed_days: 7 },
      valuation: {
        status: "valued", missing_rate_count: 0, unclassified_rows: 0,
        usd: { realized_pnl: "9.8", fees: "-0.098", funding: "-0.98", adjustments: "0", net_pnl: "8.722", cash_flow: "490" },
        net_return: null,
      },
    };
    handler = async url => url.includes("/history?") ? valued : { job: null };
    await loadBillHistory({ reset: true });
    checks.valuationSeparatesTransfers = $("#bill-valuation-summary").textContent.includes("8.722")
      && $("#bill-valuation-summary").textContent.includes("账户净划入490")
      && $("#bill-history-message").textContent.includes("不计算账户总收益");
    const valuationJob = {
      id: "d".repeat(32), ...range, state: "queued", total_days: 7, completed_days: 0, rates_loaded: 0, rates_missing: 0,
    };
    let finishValuation;
    handler = url => url.includes("/history?") ? Promise.resolve(valued)
      : new Promise(resolve => { finishValuation = resolve; });
    const beforeValuation = calls.filter(call => call.options?.method === "POST").length;
    const startingValuation = changeBillValuation();
    await changeBillValuation();
    checks.duplicateValuationBlocked = calls.filter(call => call.options?.method === "POST").length === beforeValuation + 1;
    applyPrivateEvent("bill_valuation", { job: { ...valuationJob, state: "running", completed_days: 2, rates_loaded: 4 } });
    finishValuation({ job: valuationJob });
    await startingValuation;
    checks.valuationPushWins = billHistoryState.valuationJob.state === "running"
      && $("#bill-valuation-meter").value === 2 && !billHistoryState.valuationBusy
      && !$("#value-bill-history").hasAttribute("aria-busy");
    const beforeQuery = calls.filter(call => call.url.includes("/history?")).length;
    $("#bill-history-start").value = range.end_day;
    applyPrivateEvent("bill_valuation", { job: { ...valuationJob, state: "running", completed_days: 3 } });
    checks.valuationPreservesDraftRange = $("#bill-history-start").value === range.end_day
      && calls.filter(call => call.url.includes("/history?")).length === beforeQuery;
    $("#bill-history-start").value = range.start_day;
    handler = async url => url.includes("/history?") ? valued : { job: { ...valuationJob, state: "canceled" } };
    await changeBillValuation(true);
    checks.valuationCancelExplicit = calls.some(call => call.options?.method === "POST"
      && call.url.endsWith(`/valuation/${valuationJob.id}/cancel`))
      && billHistoryState.valuationJob.state === "canceled";
    renderBillValuation({ status: "incomplete", missing_rate_count: 3, unclassified_rows: 1 });
    checks.valuationIncompleteHidesTotals = !$("#bill-valuation-summary").textContent.includes("8.722")
      && $("#bill-valuation-message").textContent.includes("缺少 3 组报价");
    let lateValuation;
    handler = () => new Promise(resolve => { lateValuation = resolve; });
    const loadingValuation = loadBillValuationJob();
    state.token = "";
    updatePrivateActionAvailability();
    lateValuation({ job: valuationJob });
    await loadingValuation;
    checks.valuationRevocationClears = billHistoryState.valuationJob === null
      && $("#bill-valuation-summary").children.length === 0 && $("#bill-valuation-progress").hidden;
    state.token = "local-bill-history-fixture";
    updatePrivateActionAvailability();
    let finishArchives;
    handler = () => new Promise(resolve => { finishArchives = resolve; });
    const pendingArchives = loadBillArchives();
    state.token = "";
    updatePrivateActionAvailability();
    finishArchives({ data: [archive] });
    await pendingArchives;
    checks.archivePermissionLossClearsData = billHistoryState.archives === null
      && $("#bill-archive-list").children.length === 0 && $("#request-bill-archive").disabled;
    state.token = "local-bill-history-fixture";
    updatePrivateActionAvailability();
    handler = async () => report;
    await loadBillHistory({ reset: true });
    renderBillArchives([{ ...archive, state: "waiting" }, { ...archive, id: "c".repeat(32), year: 2023, state: "failed", error: "archive_bill_outside_quarter" }]);
    renderBillValuation(valued.valuation);
    renderBillValuationJob({ ...valuationJob, state: "completed", completed_days: 7, rates_loaded: 38 });
    checks.onlyImportPosts = calls.filter(call => call.options?.method === "POST")
      .every(call => /^\/api\/v1\/account\/bills\/(imports|archives|valuation)/.test(call.url));
    setState("#bill-history-message", "界面回归样本（非真实账单） · 原币种账本与历史 USD 估值", "neutral");
    renderBillImport({ ...job, status: "failed", completed_days: 1 });
    return checks;
  } catch (error) {
    restore();
    throw error;
  }
}

export async function checkBillHistory({ evaluate, command, screenshot }) {
  const results = [];
  for (const theme of ["dark", "light"]) {
    for (const viewport of [
      { name: "desktop", width: 1440, height: 1000 },
      { name: "laptop", width: 1024, height: 900 },
      { name: "tablet", width: 768, height: 1024 },
      { name: "landscape", width: 844, height: 390 },
      { name: "mobile", width: 390, height: 844 },
      { name: "small", width: 375, height: 812 },
      { name: "narrow", width: 320, height: 740 },
    ]) {
      await command("Emulation.setDeviceMetricsOverride", { ...viewport, deviceScaleFactor: 1, mobile: viewport.width < 768 });
      await evaluate(`setTheme("${theme}", false)`);
      const checks = await evaluate(`(${exerciseBillHistory.toString()})()`);
      const layout = await evaluate(`(() => ({
        contained: document.documentElement.scrollWidth <= innerWidth,
        controlsFit: [...document.querySelectorAll("#bill-history-form input, #bill-history-form button")].every(el => {
          const rect = el.getBoundingClientRect(), parent = el.parentElement.getBoundingClientRect();
          return rect.height >= 44 && rect.width >= 44 && rect.left >= parent.left - 1 && rect.right <= parent.right + 1;
        }),
        paginationSingleRow: (() => {
          const previous = $("#bill-history-previous").getBoundingClientRect();
          const next = $("#bill-history-next").getBoundingClientRect();
          const page = $("#bill-history-page").getBoundingClientRect();
          return Math.abs(previous.top - next.top) < 1
            && page.top >= previous.top && page.bottom <= previous.bottom
            && page.left >= previous.right && page.right <= next.left;
        })(),
        tableKeyboardScrollable: (() => {
          const region = $("#bill-history-body").closest(".table-wrap");
          region.focus({ preventScroll: true });
          return document.activeElement === region && region.getAttribute("role") === "region"
            && Boolean(region.getAttribute("aria-label"));
        })()
      }))()`);
      Object.assign(checks, layout);
      if (["desktop", "mobile"].includes(viewport.name)) {
        await screenshot(`openperpdesk-${theme}-bill-history-${viewport.name}-fixture.png`);
      }
      await evaluate("window.__restoreBillHistoryFixture()");
      const failed = Object.entries(checks).filter(([, passed]) => passed !== true);
      if (failed.length) throw new Error(`${theme} ${viewport.name} bill history failed: ${JSON.stringify(failed)}`);
      results.push({ theme, viewport: viewport.name, checks });
    }
  }
  return results;
}
