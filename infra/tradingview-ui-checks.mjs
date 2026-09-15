async function exerciseTradingView() {
  const original = {
    token: state.token, alerts: state.tradingviewAlerts, feed: state.privateFeedState,
    view: document.body.dataset.view, theme: document.documentElement.dataset.theme,
    open: $(".tradingview-setup").open,
  };
  const clipboard = Object.getOwnPropertyDescriptor(navigator, "clipboard");
  const copied = [];
  const checks = {};
  const sample = [
    { alert_id: "ui-fixture-preview", inst_id: "BTC-USDT-SWAP", action: "open_long",
      dry_run: true, status: "preview", created_at: "2026-09-16T00:00:00Z", reasons: [] },
    { alert_id: "ui-fixture-interrupted", inst_id: "ETH-USDT-SWAP", action: "close",
      dry_run: false, status: "interrupted", created_at: "2026-09-16T00:00:01Z", reasons: ["processing_interrupted"],
      client_order_id: "opdTestInterruptedOrder" },
    { alert_id: '<img src=x onerror="window.tvAttack=1">', inst_id: "BTC-USDT-SWAP", action: "hold",
      dry_run: true, status: "observed", created_at: "2026-09-16T00:00:02Z", reasons: ["hold_signal"] },
  ];
  window.__restoreTradingViewFixture = () => {
    state.token = original.token;
    state.privateFeedState = original.feed;
    renderTradingViewAlerts(original.alerts);
    if (clipboard) Object.defineProperty(navigator, "clipboard", clipboard);
    else delete navigator.clipboard;
    $(".tradingview-setup").open = original.open;
    setTheme(original.theme, false);
    showView(original.view);
    setText("#tradingview-setup-message", "");
    delete window.tvAttack;
    delete window.__restoreTradingViewFixture;
  };
  Object.defineProperty(navigator, "clipboard", {
    configurable: true, value: { writeText: async value => copied.push(value) },
  });
  showView("risk");
  $(".tradingview-setup").open = true;
  state.token = "";
  renderTradingViewAlerts(sample);
  checks.lockedClearsData = !$("#tradingview-alert-body").textContent.includes("ui-fixture");
  state.token = "local-ui-fixture-only";
  state.privateFeedState = "open";
  applyPrivateEvent("tradingview_alerts", { data: sample });
  checks.liveEvent = $("#tradingview-alert-body").querySelectorAll("tr").length === 3;
  checks.meaningfulStatuses = $("#tradingview-alert-body").textContent.includes("预览通过")
    && $("#tradingview-alert-body").textContent.includes("需查单")
    && $("#tradingview-alert-body").textContent.includes("opdTestInterruptedOrder")
    && !$("#tradingview-alert-body").textContent.includes("已成交");
  checks.escapedPayload = !$("#tradingview-alert-body").querySelector("img, script")
    && !window.tvAttack && $("#tradingview-alert-body").textContent.includes("<img");
  const template = JSON.parse($("#tradingview-payload-template").textContent);
  checks.safeTemplate = template.action === "hold" && template.dry_run === true
    && template.timestamp === "{{timenow}}" && template.alert_id.includes("{{timenow}}");
  updateTradingViewSetup(state.status);
  await copyTradingViewValue("#tradingview-webhook-url", "address copied");
  await copyTradingViewValue("#tradingview-payload-template", "template copied");
  updateTradingViewSetup(state.status);
  checks.copyUsesFieldValue = copied[0] === `${location.origin}/api/v1/integrations/tradingview/webhook`
    && JSON.parse(copied[1]).action === "hold";
  checks.pushPreservesFeedback = $("#tradingview-setup-message").textContent === "template copied";
  navigator.clipboard.writeText = async () => { throw new Error("permission denied"); };
  await copyTradingViewValue("#tradingview-webhook-url", "");
  checks.copyFailureSelects = document.activeElement.id === "tradingview-webhook-url"
    && $("#tradingview-webhook-url").selectionEnd === $("#tradingview-webhook-url").value.length
    && $("#tradingview-setup-message").textContent.includes("已选中");
  renderTradingViewAlerts(null, "推送连接断开");
  state.privateFeedState = "offline";
  applyPrivateEvent("tradingview_alerts", { data: sample });
  checks.offlineRejectsLateData = !$("#tradingview-alert-body").textContent.includes("ui-fixture");
  state.token = "";
  applyPrivateEvent("tradingview_alerts", { data: sample });
  checks.revocationRejectsLateData = !$("#tradingview-alert-body").textContent.includes("ui-fixture");
  state.token = "local-ui-fixture-only";
  state.privateFeedState = "open";
  renderTradingViewAlerts(sample);
  setText("#tradingview-setup-message", "");
  $("#tradingview-webhook-url").blur();
  window.getSelection().removeAllRanges();
  return checks;
}

export async function checkTradingView({ evaluate, command, screenshot }) {
  const checks = await evaluate(`(${exerciseTradingView.toString()})()`);
  try {
    if (Object.values(checks).some(value => value !== true)) {
      throw new Error(`TradingView UI checks failed: ${JSON.stringify(checks)}`);
    }
    const layouts = [];
    for (const theme of ["dark", "light"]) {
      for (const width of [1440, 1024, 768, 390, 320]) {
        await command("Emulation.setDeviceMetricsOverride", {
          width, height: 1100, deviceScaleFactor: 1, mobile: width < 768,
        });
        await evaluate(`setTheme("${theme}", false)`);
        const layout = await evaluate(`(() => {
          const panel = $(".tradingview-setup").getBoundingClientRect();
          const field = $(".copy-field").getBoundingClientRect();
          const button = $("#copy-tradingview-url").getBoundingClientRect();
          const input = $("#tradingview-webhook-url").getBoundingClientRect();
          return {
            contained: document.documentElement.scrollWidth <= innerWidth && panel.right <= innerWidth,
            copyControls: input.right + 7 <= button.left && button.right <= field.right + 1 && button.width >= 44,
            tableScrollable: $(".tradingview-alert-table").tabIndex === 0,
            templateScrollable: $("#tradingview-payload-template").tabIndex === 0,
          };
        })()`);
        if (Object.values(layout).some(value => value !== true)) {
          throw new Error(`TradingView ${theme}/${width} layout failed: ${JSON.stringify(layout)}`);
        }
        layouts.push({ theme, width, ...layout });
        if (width === 1440 || width === 390) {
          await evaluate('$(".tradingview-setup").scrollIntoView({block: "start"})');
          await screenshot(`openperpdesk-tradingview-${theme}-${width}.png`);
        }
      }
    }
    return { checks, layouts };
  } finally {
    await evaluate("window.__restoreTradingViewFixture?.()");
  }
}
