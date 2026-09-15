function exercisePositionLots() {
  const original = {
    token: state.token, rows: state.records.positions, handoffs: state.handoffs,
    adjustments: state.adjustments, view: document.body.dataset.view,
  };
  const restore = () => {
    state.token = original.token;
    renderPositions(original.rows || []);
    state.records.positions = original.rows;
    state.adjustments = original.adjustments || [];
    renderProtectionHandoffs(original.handoffs);
    updatePrivateActionAvailability();
    showView(original.view || "overview");
    delete window.__restorePositionLots;
  };
  window.__restorePositionLots = restore;
  const row = {
    position_key: "BTC-USDT-SWAP:net:isolated", inst_id: "BTC-USDT-SWAP", pos_side: "net",
    size: 3, entry_price: 70000, notional: 2100, status: "open",
    lot_allocation: {
      status: "verified", policy: "fifo_with_protective_links", execution_ready: false,
      lots: [
        { lot_id: "first", opening_order_id: "opd12345678901234567890", remaining_size: "1", opened_size: "2",
          protection: { state: "native_size_mismatch", size: 2, stop_loss: 68000, take_profit: 74000 },
          handoff: { status: "cancel_pending" } },
        { lot_id: "second", opening_order_id: '<img src=x onerror="window.lotAttack=1">', remaining_size: "2", opened_size: "2",
          protection: { state: "native_matched", size: 2, stop_loss: 69000, take_profit: 75000 } },
      ],
    },
  };
  try {
    state.token = "local-lot-display-fixture";
    updatePrivateActionAvailability();
    showView("positions");
    renderPositions([row]);
    const summary = $("#positions-body .position-lot-toggle");
    const checks = {
      disclosureNamed: summary.textContent.trim() === "2 笔分单",
      touchTarget: summary.getBoundingClientRect().height >= 44,
      escapedOrder: !$("#positions-body").querySelector("img, script, [onerror]") && !window.lotAttack,
    };
    summary.click();
    summary.focus();
    checks.opens = summary.getAttribute("aria-expanded") === "true" && !$("#positions-body .position-lot-row").hidden;
    renderPositions([{ ...row, mark_price: 71000 }]);
    checks.pushKeepsOpen = !$("#positions-body .position-lot-row").hidden;
    checks.pushKeepsFocus = document.activeElement === $("#positions-body .position-lot-toggle");
    checks.quantityMismatchVisible = $("#positions-body").textContent.includes("原生数量待调整");
    checks.handoffVisible = $("#positions-body").textContent.includes("原生撤单待确认");
    row.lot_allocation.lots[0].handoff.status = "opening_cancel_pending";
    renderPositions([row]);
    checks.openingCancellationVisible = $("#positions-body").textContent.includes("开仓余单撤销待确认");
    row.lot_allocation.lots[0].handoff.status = "native_pending";
    renderPositions([row]);
    checks.nativeGenerationPendingVisible = $("#positions-body").textContent.includes("原生保护生成待核对");
    row.lot_allocation.lots[0].handoff.status = "native_cancel_pending";
    renderPositions([row]);
    checks.nativeCancellationPendingVisible = $("#positions-body").textContent.includes("原生余单撤销待确认");
    row.lot_allocation.lots[0].adjustment = {
      adjustment_id: "adjustment1", status: "accepted", target_size: "1", last_error: null,
    };
    renderPositions([row]);
    checks.adjustmentPendingVisible = $("#positions-body").textContent.includes("保护数量调整待确认");
    checks.protectedQuantityVisible = [...$("#positions-body").querySelectorAll("dl > div")].some(
      item => item.querySelector("dt").textContent === "原生保护" && item.querySelector("dd").textContent === "2 张",
    );
    row.lot_allocation.lots[1].protection = {
      state: "partially_effective", triggered: true, size: null, stop_loss: null, take_profit: null,
    };
    renderPositions([row]);
    checks.partialNativeTriggerVisible = $("#positions-body").textContent.includes("原生保护部分触发");
    checks.executionRemainsOff = $("#positions-body").textContent.includes("分单本地执行：未启用");
    const paused = state.marketPaused;
    state.marketPaused = true;
    checks.viewPauseDoesNotDisableWorker = lotExecutionLabel({
      execution_enabled: true, risk_engine_ready: true, trading_mode: "demo",
      safety_control: { execution_allowed: true, emergency_stopped: false },
      automation_worker: { enabled: true, dry_run: false },
    }) === "已启用";
    state.marketPaused = paused;
    row.lot_allocation.lots[0].handoff.status = "closing";
    renderPositions([row]);
    checks.handoffPushVisible = $("#positions-body").textContent.includes("分单平仓中")
      && !$("#positions-body .position-lot-row").hidden;
    renderPositions([]);
    applyPrivateEvent("protection_handoffs", { data: [{
      inst_id: "BTC-USDT-SWAP", opening_order_id: "opd12345678901234567890", status: "closing",
    }] });
    checks.pendingVisibleWithoutPosition = !$("#protection-handoffs").hidden
      && $("#protection-handoff-list").textContent.includes("分单平仓中");
    applyPrivateEvent("protection_handoffs", { data: [] });
    state.adjustments = [{
      inst_id: "BTC-USDT-SWAP", opening_order_id: "opd12345678901234567890",
      status: "accepted", target_size: "1",
    }];
    applyPrivateEvent("protection_adjustments", { data: state.adjustments });
    checks.adjustmentSummaryVisible = !$("#protection-handoffs").hidden
      && $("#protection-handoff-list").textContent.includes("保护数量调整待确认");
    applyPrivateEvent("protection_adjustments", { data: [] });
    checks.completedClearsSummary = $("#protection-handoffs").hidden;
    const unknown = { ...row, lot_allocation: { status: "unverified", reason: "lot_flat_boundary_missing", lots: [] } };
    renderPositions([unknown]);
    checks.unknownClearsLots = !$("#positions-body").querySelector("[data-lot-id]")
      && $("#positions-body").textContent.includes("开仓起点");
    row.lot_allocation.lots[1].opening_order_id = "opd23456789012345678901";
    renderPositions([row]);
    renderProtectionHandoffs([{ inst_id: "BTC-USDT-SWAP", opening_order_id: "opd12345678901234567890", status: "closing" }]);
    if ($("#positions-body .position-lot-row").hidden) $("#positions-body .position-lot-toggle").click();
    return checks;
  } catch (error) {
    restore();
    throw error;
  }
}

export async function checkPositionLots({ evaluate, command, screenshot }) {
  const results = [];
  for (const theme of ["dark", "light"]) {
    for (const viewport of [
      { name: "desktop", width: 1440, height: 1000 },
      { name: "mobile", width: 390, height: 844 },
    ]) {
      await command("Emulation.setDeviceMetricsOverride", { ...viewport, deviceScaleFactor: 1, mobile: viewport.width < 768 });
      await evaluate(`setTheme("${theme}", false)`);
      const checks = await evaluate(`(${exercisePositionLots.toString()})()`);
      await evaluate("new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))");
      const layout = await evaluate(`(() => {
        const details = $("#positions-body .position-lot-details");
        details.scrollIntoView({ block: "nearest", inline: "nearest" });
        return {
          pageContained: document.documentElement.scrollWidth <= innerWidth,
          disclosureContained: details.scrollWidth <= details.clientWidth,
          rowsContained: [...details.querySelectorAll("li")].every(row => row.scrollWidth <= row.clientWidth),
          mainRowCompact: $("#positions-body > tr").getBoundingClientRect().height < 120,
          scrollableTable: getComputedStyle($("#positions .table-wrap")).overflowX === "auto",
        };
      })()`);
      Object.assign(checks, layout);
      await evaluate(`(() => {
        document.activeElement?.blur();
        window.scrollTo({ top: 0, left: 0, behavior: "instant" });
        return new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      })()`);
      await screenshot(`openperpdesk-${theme}-position-lots-${viewport.name}-fixture.png`);
      await evaluate("window.__restorePositionLots()");
      const failures = Object.entries(checks).filter(([, value]) => value !== true);
      results.push({ theme, viewport: viewport.name, checks });
      if (failures.length) throw new Error(`Position lot UI failed: ${JSON.stringify(failures)}`);
    }
  }
  return results;
}
