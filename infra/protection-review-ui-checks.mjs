async function exerciseProtectionReview() {
  const original = {
    api, token: state.token, handoffs: state.handoffs, adjustments: state.adjustments,
    view: document.body.dataset.view,
  };
  const proof = {
    algo_id: "123456789012345678901234567890", size: 4, stop_loss: 68000, take_profit: 74000,
  };
  const sample = {
    handoff_id: "handoff-ui-review", inst_id: "BTC-USDT-SWAP", opening_order_id: "opd-maintenance-review",
    version: 0, status: "review", last_error: "handoff_native_changed", expected_protection: proof,
  };
  const adjustment = {
    adjustment_id: "adjustment-ui-review", inst_id: "ETH-USDT-SWAP", opening_order_id: "opd-quantity-review",
    version: 0, status: "review", last_error: "adjustment_native_changed", expected_protection: proof, target_size: "3",
  };
  const calls = [];
  let response;
  const restore = () => {
    clearProtectionReviews();
    api = original.api;
    state.token = original.token;
    state.adjustments = original.adjustments;
    renderProtectionHandoffs(original.handoffs);
    updatePrivateActionAvailability();
    showView(original.view || "overview");
    setMessage("");
    delete window.__restoreProtectionReview;
  };
  window.__restoreProtectionReview = restore;
  try {
    state.token = "local-protection-review-fixture";
    updatePrivateActionAvailability();
    state.adjustments = [adjustment];
    renderProtectionHandoffs([sample]);
    showView("positions");
    const entry = $("#protection-handoff-list [data-review-protection]");
    const checks = {
      twoReviewEntries: $("#protection-handoff-list").querySelectorAll("[data-review-protection]").length === 2,
      namedEntry: entry.getAttribute("aria-label").includes("BTC-USDT-SWAP"),
      entryTouchTarget: entry.getBoundingClientRect().height >= 44,
    };
    entry.focus();
    renderProtectionHandoffs([sample]);
    checks.pushPreservesEntryFocus = document.activeElement.dataset.reviewProtection === sample.handoff_id;
    document.activeElement.click();
    const dialog = $("#protection-review-dialog");
    checks.modalOpened = dialog.open && document.activeElement.id === "protection-review-note";
    checks.proofVisible = $("#protection-review-evidence").textContent.includes("68,000")
      && $("#protection-review-evidence").textContent.includes("4 张");
    api = async (url, options) => {
      calls.push({ url, options });
      return response(url, options);
    };
    await submitProtectionReview();
    checks.emptyNoteBlocked = calls.length === 0 && $("#protection-review-message").textContent.includes("复核依据");
    $("#protection-review-note").value = "已在交易所逐项核对原始保护订单";
    $("#protection-review-resolution").value = "position_closed";
    renderProtectionHandoffs([{ ...sample, version: 1 }]);
    checks.staleVersionBlocked = $("#submit-protection-review").disabled
      && $("#protection-review-note").value.includes("逐项核对")
      && $("#protection-review-resolution").value === "position_closed";
    response = async () => ({ data: [{ ...sample, version: 1 }] });
    await reloadProtectionReview();
    checks.explicitReloadAllowsSubmit = !$("#submit-protection-review").disabled
      && protectionReviewState.current.version === 1 && $("#protection-review-note").value.includes("逐项核对");
    let release;
    response = () => new Promise(resolve => { release = resolve; });
    const pending = submitProtectionReview();
    await submitProtectionReview();
    checks.duplicateBlocked = calls.filter(call => call.options?.method === "POST").length === 1
      && $("#submit-protection-review").disabled && $("#protection-review-note").disabled;
    const payload = JSON.parse(calls.at(-1).options.body);
    checks.versionAndChoiceSubmitted = payload.expected_version === 1 && payload.resolution === "position_closed";
    renderProtectionHandoffs([{ ...sample, version: 2, last_error: "new-evidence" }]);
    checks.busySurvivesPush = $("#submit-protection-review").disabled
      && $("#protection-review-evidence").textContent.includes("new-evidence");
    release({ accepted: true, trading_performed: false, data: [] });
    await pending;
    checks.lateReplyKeepsNewReview = state.handoffs[0]?.version === 2 && $("#submit-protection-review").disabled;
    response = async () => ({ data: [{ ...sample, version: 2 }] });
    await reloadProtectionReview();
    response = async () => { throw Object.assign(new Error("review_native_changed"), { status: 409 }); };
    await submitProtectionReview();
    checks.errorsAreInline = $("#protection-review-message").textContent.includes("原生保护与保存的依据不一致")
      && $("#protection-review-note").value.includes("逐项核对");
    checks.errorsRequireReadOnlyReload = $("#submit-protection-review").disabled && !$("#refresh-protection-review").disabled;
    $("#close-protection-review").click();
    openProtectionReview("adjustments", adjustment.adjustment_id);
    checks.adjustmentContext = $("#protection-review-title").textContent === "保护数量复核"
      && $("#protection-review-evidence").textContent.includes("3 张");
    $("#protection-review-note").value = "核对了原保护数量及剩余仓位";
    response = () => new Promise(resolve => { release = resolve; });
    const beforeLock = submitProtectionReview();
    lockPrivateAccess();
    state.token = "local-protection-review-fixture";
    renderProtectionAdjustments([{ ...adjustment, version: 3 }]);
    release({ accepted: true, trading_performed: false, data: [] });
    await beforeLock;
    checks.reauthenticationDiscardsOldReply = state.adjustments[0]?.version === 3
      && !dialog.open && protectionReviewState.current === null;
    checks.onlyReviewEndpoints = calls.every(call => /^\/api\/v1\/protection\/(handoffs|adjustments)(\/[a-z-]+\/review)?$/.test(call.url));
    renderProtectionHandoffs([{ ...sample, expected_protection: { ...proof, algo_id: '<img src=x onerror="window.reviewAttack=1">' } }]);
    openProtectionReview("handoffs", sample.handoff_id);
    checks.evidenceEscaped = !$("#protection-review-evidence").querySelector("img, script, [onerror]") && !window.reviewAttack;
    $("#close-protection-review").click();
    renderProtectionHandoffs([sample]);
    openProtectionReview("handoffs", sample.handoff_id);
    $("#protection-review-note").value = "已在交易所核对持仓与保护订单，准备恢复原流程。";
    return checks;
  } catch (error) {
    restore();
    throw error;
  }
}

export async function checkProtectionReview({ evaluate, command, screenshot }) {
  const results = [];
  for (const theme of ["dark", "light"]) {
    for (const viewport of [
      { name: "desktop", width: 1440, height: 1000 },
      { name: "mobile", width: 390, height: 844 },
    ]) {
      await command("Emulation.setDeviceMetricsOverride", { ...viewport, deviceScaleFactor: 1, mobile: viewport.width < 768 });
      await evaluate(`setTheme("${theme}", false)`);
      const checks = await evaluate(`(${exerciseProtectionReview.toString()})()`);
      await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
      Object.assign(checks, await evaluate(`(() => {
        const dialog = $("#protection-review-dialog");
        const bounds = dialog.getBoundingClientRect();
        return {
          dialogContained: bounds.left >= 0 && bounds.right <= innerWidth && bounds.top >= 0 && bounds.bottom <= innerHeight,
          contentContained: dialog.scrollWidth <= dialog.clientWidth,
          proofWraps: [...dialog.querySelectorAll("dd")].every(node => node.scrollWidth <= node.clientWidth),
          controlsContained: [...dialog.querySelectorAll("input, select, textarea, button")].every(node => {
            const rect = node.getBoundingClientRect();
            return rect.left >= bounds.left && rect.right <= bounds.right;
          }),
        };
      })()`));
      await screenshot(`openperpdesk-${theme}-protection-review-${viewport.name}-fixture.png`);
      await evaluate("window.__restoreProtectionReview()");
      results.push({ theme, viewport: viewport.name, checks });
      const failures = Object.entries(checks).filter(([, value]) => value !== true);
      if (failures.length) throw new Error(`Protection review UI failed: ${JSON.stringify(failures)}`);
    }
  }
  return results;
}
