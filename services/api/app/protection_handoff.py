import hashlib
import json
import math
import time
from decimal import Decimal, InvalidOperation
from typing import Any

from .okx_account import OkxAccountError
from .position_lots import positions_with_lots
from .position_protection import attached_parent_evidence, protection_evidence, triggered_protection_evidence
from .state_store import ACTIVE_ORDER_STATUSES, ExposureSnapshotChanged
from .trading_signal import TradeSignal


class HandoffError(ValueError):
    pass


HANDOFF_LABELS = {
    "opening_cancel_pending": "开仓余单撤销待确认", "native_pending": "原生保护生成待核对",
    "native_cancel_pending": "原生余单撤销待确认",
    "cancel_pending": "原生撤单待确认", "ready": "本地接管待执行", "closing": "分单平仓中",
    "native_executing": "原生平仓核对中", "review": "接管需人工核对", "complete": "接管完成",
}


def handoff_summaries(store, account_scope):
    return [{
        **{key: row[key] for key in (
            "handoff_id", "inst_id", "lot_id", "reason", "status", "close_sequence", "last_error",
        )},
        "opening_order_id": json.loads(row["evidence_json"])["opening_order_id"],
    } for row in store.protection_handoffs(account_scope)]


def effective_evidence(handoff: dict[str, Any]) -> dict[str, Any]:
    return json.loads(handoff.get("native_evidence_json") or handoff["evidence_json"])


def close_context(handoff: dict[str, Any]) -> dict[str, Any]:
    return {
        **effective_evidence(handoff), "kind": "handoff",
        "handoff_id": handoff["handoff_id"], "lot_id": handoff["lot_id"],
        "close_sequence": handoff["close_sequence"] + 1,
        **({"native_settlement": json.loads(handoff["native_settlement_json"])}
           if handoff.get("native_settlement_json") else {}),
    }


def native_evidence(opening: dict[str, Any], raw: dict[str, Any], expected: dict[str, Any], *, canceled=False):
    if raw.get("state") != ("canceled" if canceled else "live"):
        return None
    if raw.get("ordId") not in (None, "") or raw.get("ordIdList") not in (None, []):
        return None
    normalized = protection_evidence(
        opening, {**raw, "state": "live"}, native=True, position_size=expected["size"],
    )
    return normalized if normalized == expected else None


async def native_execution_evidence(account, opening, raw, expected, *, require_terminal=True):
    normalized = triggered_protection_evidence(opening, raw, position_size=expected["size"])
    if normalized != expected:
        raise HandoffError("handoff_native_execution_changed")
    if require_terminal and raw["state"] not in {"effective", "canceled"}:
        raise HandoffError("handoff_native_execution_not_terminal")
    children, records = [], []
    issued = filled = Decimal(0)
    for order_id in sorted(raw["ordIdList"]):
        try:
            child = await account.order_details(opening["inst_id"], ord_id=order_id)
        except Exception:
            raise HandoffError("handoff_native_children_unavailable") from None
        if opening["account_scope"] != account.account_scope:
            raise HandoffError("handoff_account_changed")
        if child.get("state") not in {"filled", "canceled", "mmp_canceled"}:
            raise HandoffError("handoff_native_child_pending")
        if (
            child.get("ordId") != order_id or child.get("ordType") != "market"
            or child.get("side") != ("sell" if opening["side"] == "buy" else "buy")
            or any(child.get(remote) != opening[local] for remote, local in (
                ("instId", "inst_id"), ("posSide", "pos_side"), ("tdMode", "td_mode"),
            )) or opening["pos_side"] == "net" and str(child.get("reduceOnly")).lower() != "true"
        ):
            raise HandoffError("handoff_native_child_identity_unverified")
        try:
            size, done = Decimal(str(child.get("sz"))), Decimal(str(child.get("accFillSz")))
            if not size.is_finite() or not done.is_finite() or not 0 <= done <= size or size <= 0:
                raise ValueError()
            if child["state"] == "filled" and done != size:
                raise ValueError()
        except (InvalidOperation, ValueError):
            raise HandoffError("handoff_native_child_quantity_unverified") from None
        issued += size
        filled += done
        children.append({
            "order_id": order_id, "state": child["state"],
            "size": str(size.normalize()), "filled_size": str(done.normalize()),
        })
        records.append(child)
    try:
        actual = Decimal(str(raw.get("actualSz")))
        if not actual.is_finite() or actual != issued or issued > Decimal(str(expected["size"])):
            raise ValueError()
    except (InvalidOperation, ValueError):
        raise HandoffError("handoff_native_issued_quantity_unverified") from None
    return {
        "algo_id": expected["algo_id"], "state": raw["state"],
        "actual_side": raw.get("actualSide") or "", "issued_size": str(issued.normalize()),
        "filled_size": str(filled.normalize()), "children": children,
    }, records


def verified_lot(store, position, lot_id):
    allocation = store.position_lots(position)
    if not allocation or allocation["status"] != "verified":
        raise HandoffError("handoff_lot_unverified")
    return next((lot for lot in allocation["lots"] if lot["lot_id"] == lot_id), None)


class ProtectionHandoff:
    """Reconcile an entry and its native exit before closing the virtual lot."""

    def __init__(self, store, account_sync, execution, *, account=None) -> None:
        self.store, self.sync, self.execution = store, account_sync, execution
        self.account = account if account is not None else account_sync.account_client

    def has_work(self, inst_id: str) -> bool:
        if self.store.protection_handoffs(self.account.account_scope, inst_id):
            return True
        return any(
            row["inst_id"] == inst_id and row["lot_allocation"]["status"] == "verified"
            and any(lot["managed"] and (
                lot["protection"]["state"] in {"native_matched", "native_size_mismatch", "attached_pending"}
                or lot["protection"].get("triggered")
            )
                    for lot in row["lot_allocation"]["lots"])
            for row in positions_with_lots(self.store, account_scope=self.account.account_scope)
        )

    def _enabled(self, account_scope=None) -> None:
        if not self.execution.safety.execution_allowed or not self.execution.trade_client.enabled:
            raise HandoffError("handoff_execution_locked")
        if not self.account.configured:
            raise HandoffError("handoff_account_unconfigured")
        trade = self.execution.trade_client
        if any(getattr(trade, field, None) != getattr(self.account, field, None)
               for field in ("api_key", "base_url", "demo")):
            raise HandoffError("handoff_account_mismatch")
        if account_scope is not None and self.account.account_scope != account_scope:
            raise HandoffError("handoff_account_changed")

    async def _transition(self, handoff, **changes):
        updated = self.store.update_protection_handoff(handoff, **changes)
        if updated is None:
            raise HandoffError("handoff_changed")
        if updated["status"] != handoff["status"]:
            payload = {"inst_id": updated["inst_id"], "handoff_id": updated["handoff_id"], "status": updated["status"]}
            self.store.add_audit("protection_handoff_state", "Protection handoff state changed", payload=payload)
            await self.execution.notify_event(
                "protection_handoff_state", "OpenPerpDesk 保护接管状态更新",
                f"{updated['inst_id']}：{HANDOFF_LABELS.get(updated['status'], '状态待核对')}。", payload=payload,
                severity="warning" if updated["status"] == "review" else "info",
            )
        return updated

    def _candidate(self, inst_id, mark_price):
        for position in positions_with_lots(self.store, account_scope=self.account.account_scope):
            if position["inst_id"] != inst_id or position["lot_allocation"]["status"] != "verified":
                continue
            long = position["pos_side"] == "long" or position["pos_side"] == "net" and position["size"] > 0
            for lot in position["lot_allocation"]["lots"]:
                if not lot["managed"]:
                    continue
                opening = self.store.get_order(lot["opening_order_id"])
                native = self.store.get_order(lot["native_client_id"])
                triggered = False
                if not opening or opening["account_scope"] != position["account_scope"]:
                    continue
                if not native or opening["status"] == "partially_filled":
                    proof = attached_parent_evidence(
                        opening, json.loads(opening["raw_json"]), position_size=float(lot["remaining_size"]),
                    )
                elif (
                    opening["status"] in {"filled", "canceled", "mmp_canceled"}
                    and native["account_scope"] == position["account_scope"] and native["order_kind"] == "algo"
                ):
                    raw = json.loads(native["raw_json"])
                    try:
                        covered_size = min(float(lot["remaining_size"]), float(raw.get("sz") or 0))
                    except (ValueError, TypeError):
                        continue
                    proof = protection_evidence(opening, raw, native=True, position_size=covered_size)
                    if proof and not native_evidence(opening, raw, proof):
                        proof = None
                    if not proof:
                        proof = triggered_protection_evidence(opening, raw, position_size=covered_size)
                        triggered = proof is not None
                else:
                    proof = None
                if not proof:
                    continue
                if triggered:
                    reason = {"sl": "stop_loss", "tp": "take_profit"}.get(raw.get("actualSide"), "native_trigger")
                    return position, lot, proof, reason, True
                stop, target = proof["stop_loss"], proof["take_profit"]
                hit_stop = stop and (mark_price <= stop if long else mark_price >= stop)
                hit_target = target and (mark_price >= target if long else mark_price <= target)
                if hit_stop or hit_target:
                    return position, lot, proof, "stop_loss" if hit_stop else "take_profit", False
        return None

    @staticmethod
    def _signal(handoff):
        return TradeSignal(
            inst_id=handoff["inst_id"], action="close", confidence=1, leverage=1,
            position_pct=0, source=f"protective-{handoff['reason']}",
        )

    @staticmethod
    def _side(position):
        return "sell" if position["pos_side"] == "long" or position["pos_side"] == "net" and position["size"] > 0 else "buy"

    async def run(self, inst_id: str, mark_price: float, *, dry_run: bool, market_data_fresh: bool) -> dict | None:
        if type(mark_price) not in {int, float} or not math.isfinite(mark_price) or mark_price <= 0:
            raise HandoffError("handoff_mark_price_invalid")
        pending = self.store.protection_handoffs(self.account.account_scope, inst_id)
        try:
            if pending:
                if dry_run:
                    return {"inst_id": inst_id, "action": "protection_handoff_paused", "accepted": False}
                self._enabled()
                return await self._advance(pending[0], market_data_fresh=market_data_fresh)
            candidate = self._candidate(inst_id, mark_price)
            if candidate is None:
                return None
            position, lot, proof, reason, triggered = candidate
            identity = "\n".join((position["account_scope"], lot["lot_id"], proof["algo_id"] or proof["algo_client_id"]))
            handoff_id = hashlib.sha256(identity.encode()).hexdigest()[:32]
            signal = self._signal({"inst_id": inst_id, "reason": reason})
            quantity = float(lot["remaining_size"])
            context = {**proof, "lot_id": lot["lot_id"]}
            if triggered:
                context["native_triggered"] = True
            if dry_run:
                result = await self.execution.submit_signal(
                    signal, account_equity=0, daily_pnl_pct=0, size=quantity,
                    side_override=self._side(position), dry_run=True, idempotency_key=f"handoff-preview:{handoff_id}",
                    market_data_fresh=market_data_fresh, expected_protection=context,
                    expected_position_trade_id=position["exchange_trade_id"],
                )
                return {"inst_id": inst_id, "action": "protection_handoff_preview", **result}
            self._enabled()
            try:
                prepared = await self.execution.preflight.prepare(
                    signal, quantity, self._side(position), expected_protection=context,
                    expected_position_trade_id=position["exchange_trade_id"],
                )
            except ValueError as exc:
                raise HandoffError(str(exc)) from exc
            fresh = market_data_fresh and -5 <= time.time() - prepared.market_timestamp <= 30
            decision = self.execution.risk_engine.evaluate(
                signal, account_equity=prepared.account_equity, daily_pnl_pct=prepared.daily_pnl_pct,
                current_notional=prepared.current_notional, order_notional=prepared.order_notional,
                market_data_fresh=fresh, verified_close=prepared.verified_close,
            )
            if not decision.approved:
                raise HandoffError("handoff_risk_rejected")
            handoff = self.store.create_protection_handoff(
                {"handoff_id": handoff_id, "lot_id": lot["lot_id"], "evidence": proof, "reason": reason,
                 "trigger_price": mark_price},
                position, expected_generation=prepared.generation,
            )
            return await self._advance(handoff, market_data_fresh=market_data_fresh)
        except (HandoffError, ExposureSnapshotChanged) as exc:
            return {"inst_id": inst_id, "action": "protection_handoff_wait", "accepted": False, "reasons": [str(exc)]}

    async def _refresh(self):
        result = await self.sync.sync_rest()
        if result.get("errors"):
            raise HandoffError("handoff_account_snapshot_incomplete")

    def _remaining(self, handoff):
        position = self.store.get_position(handoff["position_key"])
        if not position:
            raise HandoffError("handoff_position_missing")
        if position["account_scope"] != handoff["account_scope"]:
            raise HandoffError("handoff_account_changed")
        if position["status"] == "closed" and not position["size"]:
            return position, None
        return position, verified_lot(self.store, position, handoff["lot_id"])

    async def _advance(self, handoff, *, market_data_fresh, allow_cancel=True):
        self._enabled(handoff["account_scope"])
        if handoff["status"] == "review":
            raise HandoffError(handoff["last_error"] or "handoff_review_required")
        if json.loads(handoff["evidence_json"])["kind"] == "attached":
            return await self._advance_opening(handoff, market_data_fresh=market_data_fresh, allow_cancel=allow_cancel)
        return await self._advance_native(handoff, market_data_fresh=market_data_fresh, allow_cancel=allow_cancel)

    async def _advance_opening(self, handoff, *, market_data_fresh, allow_cancel):
        proof = json.loads(handoff["evidence_json"])
        opening = self.store.get_order(proof["opening_order_id"])
        if not opening or opening["account_scope"] != handoff["account_scope"]:
            raise HandoffError("handoff_opening_unverified")
        try:
            raw = await self.account.order_details(
                handoff["inst_id"], ord_id=proof["opening_exchange_id"], client_order_id=proof["opening_order_id"],
            )
        except Exception:
            raise HandoffError("handoff_opening_query_unavailable") from None
        self._enabled(handoff["account_scope"])
        if not attached_parent_evidence(opening, raw, position_size=proof["size"], expected=proof):
            await self._transition(handoff, status="review", last_error="handoff_opening_changed")
            raise HandoffError("handoff_opening_changed")
        self.sync._save_regular_order(raw, source="okx-handoff-parent")
        opening = self.store.get_order(proof["opening_order_id"])
        if raw["state"] == "partially_filled":
            if handoff["status"] != "opening_cancel_pending":
                await self._transition(handoff, status="review", last_error="handoff_opening_not_terminal")
                raise HandoffError("handoff_opening_not_terminal")
            now = int(time.time() * 1000)
            if allow_cancel and now >= handoff["cancel_after_ms"]:
                if not market_data_fresh:
                    raise HandoffError("handoff_market_data_stale")
                handoff = await self._transition(
                    handoff, cancel_attempts=handoff["cancel_attempts"] + 1, cancel_after_ms=now + 30000,
                )
                self._enabled(handoff["account_scope"])
                error = None
                try:
                    response = await self.execution.trade_client.cancel_order(handoff["inst_id"], proof["opening_exchange_id"])
                    rows = response.get("data", [])
                    if len(rows) != 1 or rows[0].get("ordId") != proof["opening_exchange_id"] or str(rows[0].get("sCode")) != "0":
                        error = "handoff_opening_cancel_ack_unverified"
                except Exception:
                    error = "handoff_opening_cancel_outcome_unknown"
                handoff = await self._transition(handoff, last_error=error)
                return await self._advance(handoff, market_data_fresh=market_data_fresh, allow_cancel=False)
            return {"inst_id": handoff["inst_id"], "action": "protection_opening_cancel_pending", "accepted": False}
        if handoff.get("native_evidence_json"):
            return await self._advance_native(handoff, market_data_fresh=market_data_fresh, allow_cancel=allow_cancel)
        try:
            native = await self.account.algo_order_details(handoff["inst_id"], client_order_id=proof["algo_client_id"])
        except OkxAccountError as exc:
            if exc.code != "51603":
                raise HandoffError("handoff_native_query_unavailable") from None
            self._enabled(handoff["account_scope"])
            if self.store.get_order(proof["algo_client_id"]):
                raise HandoffError("handoff_native_disappeared")
            if raw["state"] in {"canceled", "mmp_canceled"} and Decimal(raw["accFillSz"]) < Decimal(raw["sz"]):
                return await self._close(handoff, market_data_fresh=market_data_fresh)
            if handoff["status"] != "native_pending":
                await self._transition(handoff, status="native_pending", last_error="handoff_native_creation_pending")
            raise HandoffError("handoff_native_creation_pending")
        except Exception:
            raise HandoffError("handoff_native_query_unavailable") from None
        self._enabled(handoff["account_scope"])
        discovered = protection_evidence(
            opening, {**native, "state": "live", "actualSz": "0"}, native=True,
            position_size=float(raw["accFillSz"]),
        )
        if (
            native.get("state") not in {"live", "canceled", "effective", "partially_effective"}
            or not discovered or any(discovered[key] != proof[key] for key in (
                "opening_order_id", "opening_exchange_id", "algo_client_id", "stop_loss", "take_profit",
            )) or handoff["close_sequence"]
        ):
            await self._transition(handoff, status="review", last_error="handoff_native_binding_unverified")
            raise HandoffError("handoff_native_binding_unverified")
        self.sync._save_algo_order(native, source="okx-algo-handoff")
        bound = self.store.bind_handoff_native(handoff, discovered)
        if not bound:
            raise HandoffError("handoff_changed")
        self.store.add_audit(
            "protection_handoff_native_bound", "Attached protection resolved to a native order",
            payload={"inst_id": handoff["inst_id"], "handoff_id": handoff["handoff_id"], "algo_id": discovered["algo_id"]},
        )
        return await self._advance_native(bound, market_data_fresh=market_data_fresh, allow_cancel=True)

    async def _advance_native(self, handoff, *, market_data_fresh, allow_cancel=True):
        self._enabled()
        if handoff["account_scope"] != self.account.account_scope:
            raise HandoffError("handoff_account_changed")
        if handoff["status"] == "review":
            raise HandoffError(handoff["last_error"] or "handoff_review_required")
        proof = effective_evidence(handoff)
        opening = self.store.get_order(proof["opening_order_id"])
        if not opening or opening["account_scope"] != handoff["account_scope"]:
            raise HandoffError("handoff_opening_unverified")
        try:
            raw = await self.account.algo_order_details(
                handoff["inst_id"], algo_id=proof["algo_id"], client_order_id=proof["algo_client_id"],
            )
        except Exception:
            raise HandoffError("handoff_native_query_unavailable") from None
        self._enabled()
        if handoff["account_scope"] != self.account.account_scope:
            raise HandoffError("handoff_account_changed")
        self.sync._save_algo_order(raw, source="okx-algo-handoff")
        if raw.get("state") == "live":
            if handoff["status"] != "cancel_pending" or not native_evidence(opening, raw, proof):
                await self._transition(handoff, status="review", last_error="handoff_native_changed")
                raise HandoffError("handoff_native_changed")
            now = int(time.time() * 1000)
            if allow_cancel and now >= handoff["cancel_after_ms"]:
                if not market_data_fresh:
                    raise HandoffError("handoff_market_data_stale")
                handoff = await self._transition(
                    handoff, cancel_attempts=handoff["cancel_attempts"] + 1, cancel_after_ms=now + 30000,
                )
                self._enabled()
                error = None
                try:
                    response = await self.execution.trade_client.cancel_algo_order(handoff["inst_id"], proof["algo_id"])
                    rows = response.get("data", [])
                    if len(rows) != 1 or rows[0].get("algoId") != proof["algo_id"] or str(rows[0].get("sCode")) != "0":
                        error = "handoff_cancel_ack_unverified"
                except Exception:
                    error = "handoff_cancel_outcome_unknown"
                handoff = await self._transition(handoff, last_error=error)
                return await self._advance(handoff, market_data_fresh=market_data_fresh, allow_cancel=False)
            return {"inst_id": handoff["inst_id"], "action": "protection_cancel_pending", "accepted": False}
        if raw.get("state") in {"effective", "partially_effective"} or (
            raw.get("state") == "canceled" and raw.get("ordIdList")
        ):
            return await self._settle_native(handoff, opening, raw, proof, market_data_fresh, allow_cancel)
        if handoff.get("native_settlement_json"):
            await self._transition(handoff, status="review", last_error="handoff_native_settlement_changed")
            raise HandoffError("handoff_native_settlement_changed")
        if not native_evidence(opening, raw, proof, canceled=True):
            await self._transition(handoff, status="review", last_error="handoff_cancellation_unverified")
            raise HandoffError("handoff_cancellation_unverified")
        return await self._close(handoff, market_data_fresh=market_data_fresh)

    async def _settle_native(self, handoff, opening, raw, proof, market_data_fresh, allow_cancel):
        try:
            evidence, records = await native_execution_evidence(
                self.account, opening, raw, proof, require_terminal=False,
            )
        except HandoffError as exc:
            if handoff.get("native_settlement_json") and str(exc) not in {
                "handoff_native_children_unavailable", "handoff_account_changed",
            }:
                await self._transition(handoff, status="review", last_error=str(exc))
            raise
        self._enabled(handoff["account_scope"])
        for child in records:
            self.sync._save_regular_order(child, source="okx-handoff-history")
        if raw["state"] == "partially_effective":
            if handoff["close_sequence"] or handoff.get("native_settlement_json"):
                await self._transition(handoff, status="review", last_error="handoff_native_reactivated")
                raise HandoffError("handoff_native_reactivated")
            if handoff["status"] != "native_cancel_pending":
                handoff = await self._transition(handoff, status="native_cancel_pending")
            now = int(time.time() * 1000)
            if allow_cancel and now >= handoff["cancel_after_ms"]:
                if not market_data_fresh:
                    raise HandoffError("handoff_market_data_stale")
                handoff = await self._transition(
                    handoff, cancel_attempts=handoff["cancel_attempts"] + 1, cancel_after_ms=now + 30000,
                )
                self._enabled(handoff["account_scope"])
                error = None
                try:
                    response = await self.execution.trade_client.cancel_algo_order(handoff["inst_id"], proof["algo_id"])
                    rows = response.get("data", [])
                    if len(rows) != 1 or rows[0].get("algoId") != proof["algo_id"] or str(rows[0].get("sCode")) != "0":
                        error = "handoff_cancel_ack_unverified"
                except Exception:
                    error = "handoff_cancel_outcome_unknown"
                handoff = await self._transition(handoff, last_error=error)
                return await self._advance(handoff, market_data_fresh=market_data_fresh, allow_cancel=False)
            return {"inst_id": handoff["inst_id"], "action": "protection_native_cancel_pending", "accepted": False}
        if handoff.get("native_settlement_json"):
            if json.loads(handoff["native_settlement_json"]) != evidence:
                await self._transition(handoff, status="review", last_error="handoff_native_settlement_changed")
                raise HandoffError("handoff_native_settlement_changed")
        else:
            if handoff["status"] != "native_executing":
                handoff = await self._transition(handoff, status="native_executing")
            saved = self.store.settle_handoff_native(handoff, evidence)
            if not saved:
                raise HandoffError("handoff_changed")
            handoff = saved
        result = await self._close(handoff, market_data_fresh=market_data_fresh)
        if result["action"] == "protection_handoff_complete" and not handoff["close_order_id"]:
            result["action"] = "protection_native_completed"
        return result

    async def _close(self, handoff, *, market_data_fresh):
        await self._refresh()
        position, lot = self._remaining(handoff)
        if handoff["close_order_id"]:
            previous = self.store.get_order(handoff["close_order_id"])
            if not previous or previous["status"] in ACTIVE_ORDER_STATUSES:
                raise HandoffError("handoff_close_unconfirmed")
            if previous["status"] not in {"filled", "canceled", "mmp_canceled", "rejected"}:
                raise HandoffError("handoff_close_terminal_unverified")
        if lot is None:
            await self._transition(handoff, status="complete", last_error=None)
            return {"inst_id": handoff["inst_id"], "action": "protection_handoff_complete", "accepted": True}
        if handoff["status"] != "ready":
            handoff = await self._transition(handoff, status="ready", last_error=None)
        self._enabled()
        context = close_context(handoff)
        try:
            result = await self.execution.submit_signal(
                self._signal(handoff), account_equity=0, daily_pnl_pct=0, size=float(lot["remaining_size"]),
                side_override=self._side(position), idempotency_key=f"handoff:{handoff['handoff_id']}:{context['close_sequence']}",
                expected_position_trade_id=position["exchange_trade_id"], expected_protection=context,
                market_data_fresh=market_data_fresh,
            )
        except Exception:
            raise HandoffError("handoff_close_outcome_requires_reconciliation") from None
        if result.get("accepted") and not result.get("idempotent"):
            reason_label = {"stop_loss": "止损", "take_profit": "止盈"}.get(handoff["reason"], "原生保护")
            await self.execution.notify_event(
                "protective_exit_triggered", "OpenPerpDesk 保护性平仓触发",
                f"{handoff['inst_id']} {reason_label}已触发，标记价格 {handoff['trigger_price']}。"
                "已核对开仓委托与保护状态，已提交分单平仓请求。",
                payload={"inst_id": handoff["inst_id"], "handoff_id": handoff["handoff_id"],
                         "mark_price": handoff["trigger_price"], "size": float(lot["remaining_size"])},
                severity="warning",
            )
            try:
                await self._refresh()
                latest = self.store.protection_handoff(handoff["handoff_id"])
                submitted = self.store.get_order(latest["close_order_id"])
                _, remainder = self._remaining(latest)
                if submitted["status"] in {"filled", "canceled", "mmp_canceled"} and remainder is None:
                    await self._transition(latest, status="complete", last_error=None)
            except HandoffError:
                pass
        return {"inst_id": handoff["inst_id"], "action": "protection_handoff_close", **result}
