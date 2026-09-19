const protectionReviewState = { current: null, requests: new Map(), updates: 0 };

function protectionReviewRow(review) {
  const rows = review.kind === "handoffs" ? state.handoffs : state.adjustments;
  const key = review.kind === "handoffs" ? "handoff_id" : "adjustment_id";
  return rows.find(row => row[key] === review.id);
}

function protectionReviewError(error) {
  const labels = {
    review_record_changed: "维护记录已更新，请重新核对。",
    review_snapshot_changed: "核验期间账户记录发生变化，请重新核对。",
    review_account_changed: "当前账户已切换，复核未通过。",
    review_account_unavailable: "当前账户尚未连接，复核未通过。",
    review_account_snapshot_incomplete: "账户对账不完整，复核未通过。",
    review_position_not_closed: "持仓尚未确认归零，不能结案。",
    review_position_unverified: "无法确认维护记录所属的账户和持仓。",
    review_orders_pending: "仍有活动或结果未确认的普通委托。",
    review_close_unconfirmed: "原分单平仓结果尚未确认。",
    review_native_not_terminal: "原生保护尚未结束，不能结案。",
    review_native_changed: "原生保护与保存的依据不一致。",
    review_native_unavailable: "无法取得可确认的原生保护记录。",
    review_native_identity_unverified: "原生保护身份不匹配。",
    review_native_children_unverified: "原生保护派生订单尚未核实。",
    review_native_settlement_changed: "原生成交与已保存的终态依据不一致。",
    review_native_settlement_missing: "缺少原生成交终态依据。",
    review_native_reactivated: "原生保护重新生效，不能恢复原流程。",
    review_native_binding_unverified: "尚不能绑定当前原生保护。",
    review_opening_changed: "开仓记录在核验期间发生变化。",
    review_opening_protection_changed: "开仓单附带保护参数已改变。",
    review_lot_unverified: "分单账本尚未核验。",
    review_lot_changed: "分单余量或归属已改变。",
    review_adjustment_outcome_unconfirmed: "旧改量请求结果未确认，不能丢弃原维护记录。",
    handoff_native_child_pending: "原生保护派生的平仓单尚未结束。",
  };
  return labels[error.message] || "复核结果未确认，请重新读取当前记录。";
}

function renderProtectionReview() {
  const review = protectionReviewState.current;
  if (!review) return;
  const current = protectionReviewRow(review);
  const row = current || review.row;
  const proof = row.expected_protection || {};
  const pending = protectionReviewState.requests.has(`${review.kind}:${review.id}`);
  const changed = !current || current.status !== "review" || current.version !== review.version;
  const reasons = {
    handoff_native_changed: "原生保护参数已改变",
    handoff_opening_changed: "开仓附带保护已改变",
    handoff_opening_not_terminal: "开仓余单尚未结束",
    handoff_native_binding_unverified: "原生保护绑定未确认",
    handoff_native_settlement_changed: "原生成交终态依据已改变",
    handoff_cancellation_unverified: "原生撤销结果未确认",
    handoff_native_reactivated: "原生保护重新生效",
    adjustment_native_changed: "原生保护数量或参数已改变",
    adjustment_native_children_missing: "原生派生订单缺失",
    adjustment_native_terminal: "原生保护异常终止",
    adjustment_native_state_unverified: "原生保护状态未确认",
  };
  setText("#protection-review-title", review.kind === "handoffs" ? "保护接管复核" : "保护数量复核");
  const details = [
    ["合约", row.inst_id], ["开仓单", row.opening_order_id],
    ["原生保护", proof.algo_id || proof.algo_client_id || "--"],
    ["原保护张数", proof.size == null ? "--" : `${formatNumber(proof.size)} 张`],
    ["止盈 / 止损", `${formatNumber(proof.take_profit)} / ${formatNumber(proof.stop_loss)}`],
    ["调整目标", row.target_size == null ? "按分单余量退出" : `${row.target_size} 张`],
    ["待核对原因", reasons[row.last_error] || row.last_error || "--"],
  ];
  $("#protection-review-evidence").innerHTML = details.map(([label, value]) =>
    `<div><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");
  setText("#protection-review-message", pending ? "正在核验交易所记录。"
    : review.loading ? "正在读取维护记录。" : review.message
      || (!current || current.status !== "review" ? "该记录已不在待复核状态。"
        : changed ? "维护记录已更新，请重新核对。" : ""));
  const submit = $("#submit-protection-review");
  setBusy(submit, pending, "核验中...");
  submit.disabled = pending || review.loading || review.needsReload || changed || !state.token || !Number.isInteger(review.version);
  if (!pending) submit.textContent = $("#protection-review-resolution").value === "resume" ? "核验并恢复" : "核验并结案";
  $("#refresh-protection-review").disabled = pending || review.loading || !state.token;
  $("#protection-review-note").disabled = pending || review.loading || !state.token;
  $("#protection-review-resolution").disabled = pending || review.loading || !state.token;
}

function updateProtectionReview() {
  protectionReviewState.updates += 1;
  renderProtectionReview();
}

function openProtectionReview(kind, id) {
  if (!state.token || !["handoffs", "adjustments"].includes(kind)) return;
  const row = protectionReviewRow({ kind, id });
  if (!row || row.status !== "review" || !Number.isInteger(row.version)) return;
  protectionReviewState.current = { kind, id, row, version: row.version, message: "", loading: false, needsReload: false };
  $("#protection-review-note").value = "";
  $("#protection-review-resolution").value = "resume";
  renderProtectionReview();
  $("#protection-review-dialog").showModal();
  $("#protection-review-note").focus();
}

async function reloadProtectionReview() {
  const review = protectionReviewState.current;
  if (!review || review.loading || !state.token || protectionReviewState.requests.has(`${review.kind}:${review.id}`)) return;
  const token = state.token;
  const updates = protectionReviewState.updates;
  review.loading = true;
  review.message = "";
  renderProtectionReview();
  try {
    const response = await api(`/api/v1/protection/${review.kind}`);
    if (state.token !== token || protectionReviewState.current !== review) return;
    if (!Array.isArray(response.data)) throw new Error("Unverified maintenance response");
    if (updates === protectionReviewState.updates) {
      (review.kind === "handoffs" ? renderProtectionHandoffs : renderProtectionAdjustments)(response.data);
    }
    const row = protectionReviewRow(review);
    if (row) Object.assign(review, { row, version: row.version, needsReload: false });
  } catch (error) {
    if (state.token === token && protectionReviewState.current === review) {
      if (error.status === 401) {
        lockPrivateAccess();
        setMessage("管理员授权已失效，请重新解锁。", "error");
      } else {
        review.message = protectionReviewError(error);
        review.needsReload = true;
      }
    }
  } finally {
    review.loading = false;
    if (protectionReviewState.current === review) renderProtectionReview();
  }
}

async function submitProtectionReview() {
  const review = protectionReviewState.current;
  if (!review || !state.token || $("#submit-protection-review").disabled) return;
  const row = protectionReviewRow(review);
  if (!row || row.status !== "review" || row.version !== review.version) return;
  const note = $("#protection-review-note").value.trim();
  if (note.length < 3 || note.length > 500) {
    review.message = "请填写 3 至 500 字的复核依据。";
    renderProtectionReview();
    $("#protection-review-note").focus();
    return;
  }
  const key = `${review.kind}:${review.id}`;
  if (protectionReviewState.requests.has(key)) return;
  const request = { token: state.token, updates: protectionReviewState.updates };
  const resolution = $("#protection-review-resolution").value;
  protectionReviewState.requests.set(key, request);
  review.message = "";
  renderProtectionReview();
  const current = () => state.token === request.token && protectionReviewState.requests.get(key) === request;
  try {
    const response = await api(`/api/v1/protection/${review.kind}/${encodeURIComponent(review.id)}/review`, {
      method: "POST", body: JSON.stringify({ expected_version: review.version, resolution, note }),
    });
    if (!current()) return;
    if (response.accepted !== true || response.trading_performed !== false || !Array.isArray(response.data)) {
      throw new Error("Unverified maintenance review response");
    }
    if (request.updates === protectionReviewState.updates) {
      (review.kind === "handoffs" ? renderProtectionHandoffs : renderProtectionAdjustments)(response.data);
    }
    review.message = "复核请求已完成，当前状态以维护列表为准。";
  } catch (error) {
    if (!current()) return;
    if (error.status === 401) {
      lockPrivateAccess();
      setMessage("管理员授权已失效，请重新解锁。", "error");
      return;
    }
    review.message = protectionReviewError(error);
    review.needsReload = true;
  } finally {
    if (protectionReviewState.requests.get(key) === request) protectionReviewState.requests.delete(key);
    renderProtectionReview();
  }
}

function clearProtectionReviews() {
  protectionReviewState.requests.clear();
  protectionReviewState.current = null;
  $("#protection-review-dialog").close();
  $("#protection-review-note").value = "";
  setText("#protection-review-evidence", "");
  setText("#protection-review-message", "");
}

function initializeProtectionReviews() {
  $("#protection-handoff-list").addEventListener("click", event => {
    const button = event.target.closest("[data-review-protection]");
    if (button) openProtectionReview(button.dataset.reviewKind, button.dataset.reviewProtection);
  });
  $("#close-protection-review").addEventListener("click", () => $("#protection-review-dialog").close());
  $("#protection-review-dialog").addEventListener("close", () => {
    const review = protectionReviewState.current;
    if (!review || !state.token || $("#protection-review-dialog").open) return;
    [...$("#protection-handoff-list").querySelectorAll("[data-review-protection]")].find(button =>
      button.dataset.reviewKind === review.kind && button.dataset.reviewProtection === review.id)?.focus({ preventScroll: true });
  });
  $("#refresh-protection-review").addEventListener("click", reloadProtectionReview);
  $("#protection-review-resolution").addEventListener("change", renderProtectionReview);
  $("#protection-review-form").addEventListener("submit", event => {
    event.preventDefault();
    submitProtectionReview();
  });
}
