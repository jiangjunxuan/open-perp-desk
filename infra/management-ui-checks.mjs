async function exerciseManagement() {
  const checks = {};
  const originalApi = api;
  const original = {
    token: state.token, symbol: state.symbol, bar: state.bar, theme: document.documentElement.dataset.theme,
    performance: state.performance, analysis: state.analysis, draftRevision: state.draftRevision,
    equity: $("#equity-input").value, strategy: state.strategy,
  };
  const calls = [];
  const sample = [
    { created_at: "2026-09-13T04:03:00Z", severity: "error", event_type: "account_sync_failed", message: "界面回归样本：BTC 账户同步失败。" },
    { created_at: "2026-09-13T04:02:00Z", severity: "warn", event_type: "risk_rejected", message: "界面回归样本：ETH 名义敞口超过阈值。" },
    { created_at: "2026-09-13T04:01:00Z", severity: "info", event_type: "backtest_completed", message: "界面回归样本：只读回放完成，未生成委托。" },
    { created_at: '<img src=x onerror="window.auditAttack=1">', severity: "custom", event_type: '<script>window.auditAttack=1</script>', message: '<img src=x onerror="window.auditAttack=1">' },
  ];
    const result = {
    inst_id: "BTC-USDT-SWAP", final_equity: 1025, return_pct: 2.5,
    max_drawdown_pct: 0.75, trades: 6, win_rate_pct: 50,
    equity_curve: [{ equity: 1000 }, { equity: 990 }, { equity: 1025 }],
  };
  const sampleBacktest = options => {
    const amount = JSON.parse(options.body).initial_equity;
    return { data: { ...result, initial_equity: amount, final_equity: amount * 1.025,
      equity_curve: [amount, amount * .9925, amount * 1.025].map(equity => ({ equity })) } };
  };
  let response = async url => {
    if (url === "/api/v1/activity") return { data: sample };
    if (url === "/api/v1/backtest") return { data: result };
    throw new Error(`Unexpected management fixture request: ${url}`);
  };
  const restore = () => {
    api = originalApi;
    state.token = original.token;
    state.symbol = original.symbol;
    state.bar = original.bar;
    state.strategy = original.strategy;
    $("#symbol").value = original.symbol;
    $("#bar").value = original.bar;
    $("#activity-query").value = "";
    $("#activity-severity").value = "all";
    $("#backtest-equity").value = "1000";
    $("#backtest-limit").value = "300";
    $("#backtest-fee").value = "5";
    resetManagementAccess();
    renderPerformance(original.performance);
    updatePrivateActionAvailability();
    updateBacktestContext();
    setTheme(original.theme, false);
    setMessage("");
    delete window.auditAttack;
    delete window.__restoreManagementFixture;
  };
  window.__restoreManagementFixture = restore;
  try {
    api = async (url, options) => {
      calls.push({ url, options });
      return response(url, options);
    };
    state.token = "";
    updatePrivateActionAvailability();
    await runBacktest();
    await loadActivity();
    checks.lockedCannotRequest = calls.length === 0 && $("#run-backtest").disabled && $("#refresh-activity").disabled;
    checks.lockedInputs = [...document.querySelectorAll("#backtest-form input, #backtest-form select, #activity-query, #activity-severity")].every(el => el.disabled);

    state.token = "local-management-fixture-only";
    updatePrivateActionAvailability();
    showView("activity");
    renderActivity(sample);
    checks.auditSummary = $("#activity-total").textContent === "4"
      && $("#activity-errors").textContent === "1" && $("#activity-warnings").textContent === "1"
      && $("#activity-latest").textContent === "2026-09-13 04:03:00";
    checks.auditEscapesMarkup = !$("#activity-list").querySelector("script, img, [onerror]")
      && !window.auditAttack && $("#activity-list").textContent.includes("<script>");
    $("#activity-severity").value = "warning";
    $("#activity-severity").dispatchEvent(new Event("change"));
    $("#activity-query").value = "eth";
    $("#activity-query").dispatchEvent(new Event("input"));
    checks.auditFiltersTogether = $("#activity-list").querySelectorAll(".activity-row").length === 1
      && $("#activity-list").textContent.includes("ETH") && $("#activity-total").textContent === "4";
    $("#activity-query").value = "no-match";
    $("#activity-query").dispatchEvent(new Event("input"));
    checks.auditNoMatch = $("#activity-list").textContent.includes("没有匹配") && !$("#clear-activity").disabled;
    $("#clear-activity").click();
    checks.auditClearFocus = document.activeElement.id === "activity-query"
      && $("#activity-list").querySelectorAll(".activity-row").length === 4 && $("#clear-activity").disabled;
    $("#activity-query").value = "BTC";
    await loadActivity();
    checks.auditRefreshPreservesQuery = $("#activity-query").value === "BTC"
      && $("#activity-list").querySelectorAll(".activity-row").length === 1;
    response = async () => { throw new Error("fixture outage"); };
    await loadActivity();
    checks.auditFailureKeepsRows = $("#activity-list").querySelectorAll(".activity-row").length === 1
      && !$("#activity-message").hidden && $("#activity-message").textContent.includes("保留上次记录");
    let resolveActivity;
    response = () => new Promise(resolve => { resolveActivity = resolve; });
    const beforeActivity = calls.length;
    const pendingActivity = loadActivity();
    await loadActivity();
    checks.auditDuplicateBlocked = calls.length === beforeActivity + 1 && $("#refresh-activity").disabled;
    state.token = "";
    updatePrivateActionAvailability();
    resolveActivity({ data: sample });
    await pendingActivity;
    checks.auditPermissionLossClears = state.activityRows === null && $("#activity-total").textContent === "--"
      && !$("#activity-list").textContent.includes("BTC") && $("#refresh-activity").disabled;

    state.token = "local-management-fixture-only";
    updatePrivateActionAvailability();
    showView("performance");
    $("#backtest-equity").value = "0";
    const beforeInvalid = calls.length;
    await runBacktest();
    checks.backtestValidation = calls.length === beforeInvalid && !$("#backtest-equity").checkValidity();
    $("#backtest-equity").value = "2500";
    $("#backtest-limit").value = "200";
    $("#backtest-fee").value = "3";
    response = async (_, options) => sampleBacktest(options);
    await runBacktest();
    const submitted = JSON.parse(calls.at(-1).options.body);
    checks.backtestOwnParameters = submitted.initial_equity === 2500 && submitted.limit === 200
      && submitted.fee_bps === 3 && $("#equity-input").value === original.equity;
    checks.backtestInlineResult = !$("#backtest-result").hidden
      && $("#backtest-return").textContent === "2.50%" && $("#backtest-trades").textContent === "6"
      && $("#backtest-status").textContent.includes("未生成委托");
    checks.backtestSeparateFromAccount = state.performance === original.performance
      && state.analysis === original.analysis && state.draftRevision === original.draftRevision;
    $("#backtest-equity").value = "2600";
    $("#backtest-equity").dispatchEvent(new Event("input", { bubbles: true }));
    checks.changedParametersLabeled = $("#backtest-result-state").textContent === "参数已变更"
      && $("#backtest-result-context").textContent.includes("2,500");
    const completed = state.backtest;
    showView("activity");
    showView("performance");
    setTheme(original.theme === "light" ? "dark" : "light", false);
    checks.resultSurvivesNavigationAndTheme = state.backtest === completed
      && !$("#backtest-result").hidden && $("#backtest-return").textContent === "2.50%";
    setTheme(original.theme, false);
    response = async () => { throw new Error("fixture replay failure"); };
    await runBacktest();
    checks.backtestFailureKeepsResult = state.backtest === completed
      && $("#backtest-status").textContent.includes("上次结果");
    let resolveBacktest;
    response = () => new Promise(resolve => { resolveBacktest = resolve; });
    const beforeBacktest = calls.length;
    const pendingBacktest = runBacktest();
    await runBacktest();
    checks.backtestDuplicateBlocked = calls.length === beforeBacktest + 1 && $("#run-backtest").disabled;
    state.token = "";
    updatePrivateActionAvailability();
    resolveBacktest({ data: result });
    await pendingBacktest;
    checks.backtestPermissionLossClears = state.backtest === null && $("#backtest-result").hidden
      && $("#backtest-return").textContent === "--" && $("#run-backtest").disabled;

    state.token = "local-management-fixture-only";
    updatePrivateActionAvailability();
    response = async (_, options) => sampleBacktest(options);
    state.symbol = "BTC-USDT-SWAP";
    state.bar = "15m";
    await runBacktest();
    state.symbol = "ETH-USDT-SWAP";
    state.bar = "1H";
    updateBacktestContext();
    checks.resultIdentityNotRewritten = $("#backtest-market").textContent.includes("ETH")
      && $("#backtest-result-context").textContent.includes("BTC")
      && $("#backtest-result-state").textContent === "参数已变更";
    checks.onlyReadOnlyEndpoints = calls.every(call => ["/api/v1/activity", "/api/v1/backtest"].includes(call.url));
    checks.safetyGateUnchanged = !state.status.execution_enabled && !state.status.live_safety.allowed
      && $("#execute-signal").disabled;
    state.symbol = original.symbol;
    state.bar = original.bar;
    updateBacktestContext();
    renderPerformance({
      initial_equity: 1000, ending_equity: 1017.5, net_pnl: 17.5, return_pct: 1.75,
      max_drawdown_pct: 1, fills: 8, currency: "USDT",
      equity_curve: [1000, 1004, 998, 1008, 1006, 1011, 1002, 1017.5].map(equity => ({ equity })),
      by_strategy: { "界面回归样本（非账户数据）": { net_pnl: 17.5, fills: 8 } },
    });
    const table = $("#account-bills .table-wrap");
    const previousBills = $("#bills-body").innerHTML;
    $("#bills-body").innerHTML = Array.from({ length: 40 }, () =>
      `<tr>${["2026-09-13 04:00:00", "交易", "BTC-USDT-SWAP", "USDT", "1", "0", "0", "0", "1"].map(value => `<td>${value}</td>`).join("")}</tr>`).join("")
      + '<tr><td colspan="9" class="table-empty">界面回归样本 · 截断提示</td></tr>';
    table.scrollTop = 120;
    const tableTop = table.getBoundingClientRect().top;
    const headerTop = table.querySelector("th").getBoundingClientRect().top;
    checks.tableFooterKeepsHeaders = getComputedStyle(table.querySelector("thead")).display !== "none";
    checks.tableHeaderStaysVisible = Math.abs(tableTop - headerTop) <= 2;
    checks.tableKeyboardRegion = table.tabIndex === 0 && table.getAttribute("role") === "region"
      && Boolean(table.getAttribute("aria-label"));
    $("#bills-body").innerHTML = previousBills;
    table.scrollTop = 0;
    $("#activity-query").value = "";
    $("#activity-severity").value = "all";
    renderActivity(sample.slice(0, 3));
    $("#activity-message").hidden = true;
    return checks;
  } catch (error) {
    restore();
    throw error;
  }
}

export async function checkManagement({ evaluate, command, screenshot }) {
  const results = [];
  for (let attempt = 0; attempt < 120; attempt++) {
    if (await evaluate('typeof state !== "undefined" && state.status !== null')) break;
    await new Promise(resolve => setTimeout(resolve, 100));
    if (attempt === 119) throw new Error("Management checks: system status did not load");
  }
  for (const theme of ["dark", "light"]) {
    for (const viewport of [
      { name: "wide", width: 1920, height: 1080 },
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
      const checks = await evaluate(`(${exerciseManagement.toString()})()`);
      for (const route of ["performance", "activity"]) {
        await evaluate(`showView("${route}")`);
        await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
        const layout = await evaluate(`(() => {
          const visible = [...document.querySelectorAll("#${route} input, #${route} select, #${route} button, #backtest input, #backtest select, #backtest button")]
            .filter(el => el.getClientRects().length);
          return {
            pageContained: document.documentElement.scrollWidth <= innerWidth,
            controlsContained: visible.every(el => {
              const rect = el.getBoundingClientRect(), parent = el.parentElement.getBoundingClientRect();
              return rect.width >= 44 && rect.height >= 44 && rect.left >= parent.left - 1 && rect.right <= parent.right + 1;
            }),
            columnsSeparate: (() => {
              if ("${route}" !== "performance") return true;
              const left = $("#performance").getBoundingClientRect(), right = $("#backtest").getBoundingClientRect();
              return innerWidth >= 1024 ? left.right <= right.left + 1 : left.bottom <= right.top + 1;
            })(),
            chartPainted: (() => {
              if ("${route}" !== "performance") return true;
              const canvas = $("#equity-chart");
              return canvas.getContext("2d").getImageData(0, 0, canvas.width, canvas.height).data
                .some((value, index) => index % 4 === 3 && value > 0);
            })()
          };
        })()`);
        Object.entries(layout).forEach(([key, value]) => { checks[`${route}_${key}`] = value; });
        if (["desktop", "mobile"].includes(viewport.name)) {
          await screenshot(`openperpdesk-${theme}-${route}-${viewport.name}-fixture.png`);
        }
      }
      const failed = Object.entries(checks).filter(([, value]) => value !== true);
      results.push({ theme, viewport: viewport.name, checks });
      await evaluate("window.__restoreManagementFixture()");
      if (failed.length) throw new Error(`${theme} ${viewport.name} management checks failed: ${JSON.stringify(failed)}`);
    }
  }
  return results;
}
