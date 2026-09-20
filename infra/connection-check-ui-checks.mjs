async function exerciseConnectionCheck() {
  const original = {
    token: state.token, feed: state.privateFeedState, view: document.body.dataset.view,
    theme: document.documentElement.dataset.theme, open: $(".connection-check").open,
    snapshot: connectionCheckState.snapshot, api,
  };
  window.__restoreConnectionCheckFixture = () => {
    api = original.api;
    clearConnectionCheck();
    state.token = original.token;
    state.privateFeedState = original.feed;
    connectionCheckState.snapshot = original.snapshot;
    $(".connection-check").open = original.open;
    renderConnectionCheck();
    setTheme(original.theme, false);
    showView(original.view);
    delete window.__restoreConnectionCheckFixture;
  };
  const checks = {};
  const passed = {
    status: "passed", checked_at: "2026-09-20T12:00:00Z",
    trading_performed: false, order_lifecycle_verified: false,
    report: {
      demo: true, scope: "private_rest_and_websocket_read_only",
      trading_performed: false, order_lifecycle_verified: false,
      private_ws_connected: true, private_ws_authenticated: true,
      algo_ws_connected: true, algo_ws_authenticated: true,
      rows: Object.fromEntries(Object.keys(connectionCheckLabels).filter(name => !name.endsWith("_ws")).map(name => [name, 1])),
    },
    checks: Object.keys(connectionCheckLabels).map(name => ({ name, status: "passed", rows: name.endsWith("_ws") ? null : 1 })),
  };
  const running = { ...passed, status: "running", report: null, checked_at: null };
  showView("risk");
  $(".connection-check").open = true;
  state.token = "";
  renderConnectionCheck();
  checks.adminRequired = $("#check-account-connection").disabled
    && $("#account-check-status").textContent === "管理员未解锁";
  state.token = "local-ui-fixture-only";
  state.privateFeedState = "open";
  applyPrivateEvent("connection_check", { data: passed });
  checks.successIsReadonly = $("#account-check-status").textContent === "只读连接通过"
    && $("#account-check-results").children.length === 9
    && !$("#check-account-connection").disabled;
  applyConnectionCheck({ ...passed, report: null });
  checks.incompleteEvidenceNeverGreen = $("#account-check-status").textContent === "检测结果待确认"
    && $("#account-check-status").dataset.tone !== "good";
  checks.invalidEvidenceNeverGreen = [
    null, {}, { ...passed, checks: {} }, { ...passed, checks: [null] },
    { ...passed, checks: [...passed.checks.slice(1), passed.checks[1]] },
    { ...passed, checks: passed.checks.map(row => ({ ...row, status: "unknown" })) },
    { ...passed, report: { ...passed.report, algo_ws_authenticated: false } },
    { ...passed, report: { ...passed.report, trading_performed: true } },
    { ...passed, report: { ...passed.report, rows: {} } },
  ].every(value => {
    applyConnectionCheck(value);
    return $("#account-check-status").textContent === "检测结果待确认"
      && $("#account-check-status").dataset.tone !== "good";
  });
  applyConnectionCheck({ ...passed, status: "failed", report: null, error: "credentials_missing" });
  checks.missingCredentials = $("#account-check-status").textContent === "OKX 私有凭据未配置";
  applyConnectionCheck({ ...passed, status: "failed", report: null, error: "<script>secret</script>" });
  checks.noRawErrorLeak = !$("#account-check-status").textContent.includes("secret");

  let release;
  const requests = [];
  api = (route, options) => {
    requests.push({ route, options });
    return new Promise(resolve => { release = resolve; });
  };
  const request = startConnectionCheck();
  checks.pendingClearsOldResult = $("#check-account-connection").disabled
    && !$("#account-check-results").textContent.includes("1 条")
    && $("#account-check-status").textContent === "正在发起检测";
  await startConnectionCheck();
  checks.noDuplicateRequest = requests.length === 1
    && requests[0].route === "/api/v1/account/connection-check"
    && requests[0].options.method === "POST";
  applyPrivateEvent("connection_check", { data: passed });
  release({ data: running });
  await request;
  checks.lateHttpCannotOverwritePush = connectionCheckState.snapshot.status === "passed"
    && !$("#check-account-connection").disabled;

  const disconnectedRequest = startConnectionCheck();
  state.privateFeedState = "offline";
  clearConnectionCheck();
  release({ data: passed });
  await disconnectedRequest;
  applyPrivateEvent("connection_check", { data: passed });
  checks.disconnectionClearsAndRejectsLateResult = connectionCheckState.snapshot === null
    && $("#check-account-connection").disabled
    && !$("#account-check-results").textContent.includes("1 条");

  state.privateFeedState = "open";
  renderConnectionCheck();
  const revokedRequest = startConnectionCheck();
  state.token = "";
  clearConnectionCheck();
  release({ data: passed });
  await revokedRequest;
  checks.revocationClearsAndRejectsLateResult = connectionCheckState.snapshot === null
    && $("#account-check-status").textContent === "管理员未解锁";
  state.token = "local-ui-fixture-only";
  applyConnectionCheck(passed);
  return checks;
}

export async function checkConnectionUI({ evaluate, command, screenshot }) {
  try {
    const checks = await evaluate(`(${exerciseConnectionCheck.toString()})()`);
    if (Object.values(checks).some(value => value !== true)) {
      throw new Error(`Connection UI checks failed: ${JSON.stringify(checks)}`);
    }
    const layouts = [];
    for (const theme of ["dark", "light"]) {
      for (const width of [1440, 1024, 768, 390, 320]) {
        await command("Emulation.setDeviceMetricsOverride", {
          width, height: 1000, deviceScaleFactor: 1, mobile: width < 768,
        });
        await evaluate(`setTheme("${theme}", false)`);
        const layout = await evaluate(`(() => {
          const button = $("#check-account-connection").getBoundingClientRect();
          const panel = $(".connection-check").getBoundingClientRect();
          return {
            contained: document.documentElement.scrollWidth <= innerWidth && panel.right <= innerWidth,
            buttonSize: button.width >= 44 && button.height >= 44,
            rowsFit: [...$("#account-check-results").querySelectorAll("dt, dd")].every(element =>
              element.scrollWidth <= element.clientWidth + 1),
          };
        })()`);
        if (Object.values(layout).some(value => value !== true)) {
          throw new Error(`Connection UI ${theme}/${width} failed: ${JSON.stringify(layout)}`);
        }
        layouts.push({ theme, width, ...layout });
        if (width === 1440 || width === 390) {
          await evaluate('$(".connection-check").scrollIntoView({block: "start"})');
          await screenshot(`openperpdesk-connection-${theme}-${width}.png`);
        }
      }
    }
    return { checks, layouts };
  } finally {
    await evaluate("window.__restoreConnectionCheckFixture?.()");
  }
}

export async function checkConnectionRoundTrip({ evaluate, command, origin, token }) {
  if (!token) return null;
  await command("Page.navigate", { url: `${origin}/#risk` });
  const waitFor = async expression => {
    for (let attempt = 0; attempt < 200; attempt++) {
      if (await evaluate(expression)) return;
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    throw new Error("Connection check browser/API round trip timed out");
  };
  await waitFor('typeof initializeConnectionCheck === "function" && document.querySelector("#open-auth") !== null');
  await evaluate(`(() => {
    $("#open-auth").click();
    $("#admin-token").value = ${JSON.stringify(token)};
    $("#auth-form").requestSubmit();
  })()`);
  try {
    await waitFor('state.privateFeedState === "open" && !$("#auth-dialog").open');
    await evaluate('$(".connection-check").open = true; $("#check-account-connection").click()');
    await waitFor('connectionCheckState.snapshot?.status === "passed" || connectionCheckState.snapshot?.status === "failed"');
    const checks = await evaluate(`(() => ({
      passed: $("#account-check-status").textContent === "只读连接通过",
      allNinePassed: connectionCheckState.snapshot?.checks.filter(row => row.status === "passed").length === 9,
      readOnlyVerified: connectionCheckState.snapshot?.verified === true,
      controlsUnlocked: !$("#check-account-connection").disabled,
      executionStillLocked: state.status?.execution_enabled === false
        && state.status?.automation_worker?.enabled === false,
    }))()`);
    if (Object.values(checks).some(value => value !== true)) {
      throw new Error(`Connection browser/API round trip failed: ${JSON.stringify(checks)}`);
    }
    return checks;
  } finally {
    await evaluate("lockPrivateAccess()");
  }
}
