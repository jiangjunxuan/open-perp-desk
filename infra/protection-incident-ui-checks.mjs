async function exerciseProtectionIncidents() {
  const original = {
    api, token: state.token, incidents: state.incidents,
    requests: state.incidentRequests, view: document.body.dataset.view,
  };
  const sample = {
    incident_id: "incident-ui-fixture", inst_id: "BTC-USDT-SWAP",
    opening_order_id: "opd-review-fixture", exchange_order_id: "1234567890",
    failure_code: "attached_protection_51000",
    failure_detail: '<img src=x onerror="window.incidentAttack=1">',
    expected_protection: { stop_loss: 68000, take_profit: 74000 },
    status: "open", version: 0, created_at: "2026-09-13T04:03:00Z",
  };
  const calls = [];
  let response;
  const button = () => $("#protection-incident-list [data-resolve-incident]");
  const note = () => $("#protection-incident-list [data-incident-note]");
  const select = () => $("#protection-incident-list [data-incident-resolution]");
  const message = () => $("#protection-incident-list [data-incident-message]");
  const restore = () => {
    api = original.api;
    state.token = original.token;
    state.incidentRequests = original.requests;
    renderProtectionIncidents(original.incidents);
    updatePrivateActionAvailability();
    showView(original.view || "overview");
    setMessage("");
    delete window.incidentAttack;
    delete window.__restoreProtectionIncidents;
  };
  window.__restoreProtectionIncidents = restore;
  try {
    state.token = "local-incident-display-fixture";
    state.incidentRequests = new Map();
    updatePrivateActionAvailability();
    api = async (url, options) => {
      calls.push({ url, options });
      return response(url, options);
    };
    showView("positions");
    renderProtectionIncidents([sample]);
    const checks = {
      escapedEvidence: !$("#protection-incident-list").querySelector("img, script, [onerror]") && !window.incidentAttack,
      versionAvailable: button().closest("article").dataset.version === "0",
      namedFields: note().labels[0].textContent.includes("复核备注") && select().labels[0].textContent.includes("解除方式"),
      touchTargets: [note(), select(), button()].every(node => node.getBoundingClientRect().height >= 44),
    };
    await resolveProtectionIncident(button());
    checks.requiredNote = calls.length === 0 && document.activeElement === note()
      && note().getAttribute("aria-invalid") === "true" && message().textContent.includes("3 至 500");
    note().value = "在交易所复核保护单和当前仓位";
    select().value = "position_closed";
    const focused = note();
    focused.focus();
    focused.setSelectionRange(2, 5);
    renderProtectionIncidents([{ ...sample, version: 1, failure_detail: "交易所补充了失败详情" }]);
    checks.pushPreservesDraft = note() === focused && note().value === "在交易所复核保护单和当前仓位"
      && select().value === "position_closed";
    checks.pushPreservesFocus = document.activeElement === focused && focused.selectionStart === 2 && focused.selectionEnd === 5;
    checks.changedEvidenceVisible = message().textContent.includes("记录已更新")
      && $("#protection-incident-list").textContent.includes("补充了失败详情");
    let release;
    response = () => new Promise(resolve => { release = resolve; });
    const pending = resolveProtectionIncident(button());
    await resolveProtectionIncident(button());
    checks.duplicateBlocked = calls.length === 1 && button().disabled && note().disabled && select().disabled;
    const payload = JSON.parse(calls[0].options.body);
    checks.reviewedVersionSent = payload.expected_version === 1 && payload.resolution === "position_closed";
    renderProtectionIncidents([{ ...sample, version: 2, failure_detail: "请求期间的新证据" }]);
    checks.pushKeepsBusy = button().disabled && note().disabled && button().getAttribute("aria-busy") === "true";
    release({ accepted: true, data: [] });
    await pending;
    checks.lateReplyCannotClearNewEvidence = state.incidents[0]?.version === 2
      && !$("#protection-incidents").hidden && !button().disabled && !note().disabled;
    checks.lateReplyDoesNotClaimUnlock = !$("#action-message").textContent.includes("已解除");
    response = async url => {
      if (url.endsWith("/resolve")) throw Object.assign(new Error("review conflict"), { status: 409 });
      return { data: [{ ...sample, version: 3, failure_detail: "冲突后重新读取的证据" }] };
    };
    await resolveProtectionIncident(button());
    checks.conflictRefreshPreservesNote = state.incidents[0]?.version === 3
      && note().value === "在交易所复核保护单和当前仓位" && message().textContent.includes("核对最新记录");
    response = async url => url.endsWith("/resolve")
      ? { accepted: false, data: [] } : { data: [{ ...sample, version: 3 }] };
    await resolveProtectionIncident(button());
    checks.unverifiedReplyCannotClear = state.incidents.length === 1 && message().textContent.includes("结果未确认");
    response = async url => {
      if (url.endsWith("/resolve")) throw new Error("response lost");
      return { data: [] };
    };
    await resolveProtectionIncident(button());
    checks.uncertainReplyReconcilesReadOnly = state.incidents.length === 0
      && $("#action-message").textContent.includes("已不在待复核列表") && !$("#action-message").textContent.includes("已解除");
    renderProtectionIncidents([{ ...sample, version: 3 }]);
    note().value = "在交易所复核保护单和当前仓位";
    response = () => new Promise(resolve => { release = resolve; });
    const beforeLock = resolveProtectionIncident(button());
    lockPrivateAccess();
    state.token = "local-incident-display-fixture";
    updatePrivateActionAvailability();
    renderProtectionIncidents([{ ...sample, version: 4 }]);
    release({ accepted: true, data: [] });
    await beforeLock;
    checks.reauthenticationRejectsOldReply = state.incidents[0]?.version === 4 && !$("#protection-incidents").hidden;
    renderProtectionIncidents([{ ...sample, version: undefined }]);
    checks.missingVersionBlocksSubmit = button().disabled;
    renderProtectionIncidents([{ ...sample, version: 5 }]);
    note().value = "已在交易所逐项核对，保留复核依据";
    response = async () => ({ accepted: true, data: [] });
    await resolveProtectionIncident(button());
    checks.confirmedResolutionClears = $("#protection-incidents").hidden && state.incidents.length === 0;
    checks.onlyIncidentEndpoints = calls.every(call => call.url === "/api/v1/protection/incidents"
      || call.url === "/api/v1/protection/incidents/incident-ui-fixture/resolve");
    renderProtectionIncidents([{ ...sample, version: 6, failure_detail: "附带止盈止损创建失败，等待人工复核。" }]);
    note().value = "已在交易所核对当前仓位及保护订单";
    select().value = "position_closed";
    setMessage("");
    return checks;
  } catch (error) {
    restore();
    throw error;
  }
}

export async function checkProtectionIncidents({ evaluate, command, screenshot }) {
  const results = [];
  for (const theme of ["dark", "light"]) {
    for (const viewport of [
      { name: "desktop", width: 1440, height: 1000 },
      { name: "laptop", width: 1024, height: 900 },
      { name: "mobile", width: 390, height: 844 },
    ]) {
      await command("Emulation.setDeviceMetricsOverride", { ...viewport, deviceScaleFactor: 1, mobile: viewport.width < 768 });
      await evaluate(`setTheme("${theme}", false)`);
      const checks = await evaluate(`(${exerciseProtectionIncidents.toString()})()`);
      await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
      Object.assign(checks, await evaluate(`(() => {
        const section = $("#protection-incidents");
        section.scrollIntoView({ block: "start", inline: "nearest" });
        const fields = [...section.querySelectorAll("input, select, button")];
        return {
          pageContained: document.documentElement.scrollWidth <= innerWidth,
          rowsContained: [...section.querySelectorAll("article, .protection-incident-actions")]
            .every(node => node.scrollWidth <= node.clientWidth),
          controlsDoNotOverlap: fields.every((a, i) => fields.slice(i + 1).every(b => {
            const x = a.getBoundingClientRect(), y = b.getBoundingClientRect();
            return Math.min(x.right, y.right) - Math.max(x.left, y.left) < 1
              || Math.min(x.bottom, y.bottom) - Math.max(x.top, y.top) < 1;
          })),
        };
      })()`));
      await evaluate(`(() => {
        document.activeElement?.blur();
        window.scrollTo({ top: 0, left: 0, behavior: "instant" });
        return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      })()`);
      await screenshot(`openperpdesk-${theme}-protection-incidents-${viewport.name}-fixture.png`);
      await evaluate("window.__restoreProtectionIncidents()");
      results.push({ theme, viewport: viewport.name, checks });
      const failures = Object.entries(checks).filter(([, value]) => value !== true);
      if (failures.length) throw new Error(`Protection incident UI failed: ${JSON.stringify(failures)}`);
    }
  }
  return results;
}
