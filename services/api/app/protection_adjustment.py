import json
import math
import time
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from .okx_trade import OkxOrderRejected
from .order_preflight import ContractSpec, PreflightError
from .position_protection import attached_algo_client_id, protection_evidence
from .protection_handoff import HandoffError, native_evidence
from .state_store import ExposureSnapshotChanged


class AdjustmentError(ValueError):
    pass


ADJUSTMENT_LABELS = {
    "prepared": "保护数量调整待执行", "submitted": "保护数量调整待确认",
    "accepted": "保护数量调整待确认", "review": "保护数量需人工核对",
    "complete": "保护数量已同步", "superseded": "原数量维护已结束", "rejected": "保护数量调整被拒绝",
}


def adjustment_summaries(store, account_scope):
    return [{
        **{key: row[key] for key in (
            "adjustment_id", "inst_id", "opening_order_id", "lot_id", "target_size", "status", "last_error", "version",
        )},
        "expected_protection": json.loads(row["evidence_json"]),
    } for row in store.protection_adjustments(account_scope)]


class ProtectionAdjustment:
    """Reconcile fixed native quantities without replaying uncertain amendments."""

    def __init__(self, handoff, market):
        self.handoff, self.market = handoff, market
        self.store, self.account, self.sync = handoff.store, handoff.account, handoff.sync
        self.execution = handoff.execution

    @staticmethod
    def _hit(opening, proof, mark):
        long = opening["side"] == "buy"
        stop, target = proof["stop_loss"], proof["take_profit"]
        return bool(stop and (mark <= stop if long else mark >= stop)
                    or target and (mark >= target if long else mark <= target))

    def _position_lot(self, opening):
        position = self.store.get_position(f"{opening['inst_id']}:{opening['pos_side']}:{opening['td_mode']}")
        if not position or position["account_scope"] != opening["account_scope"]:
            raise AdjustmentError("adjustment_position_unverified")
        if position["status"] == "closed" and not position["size"]:
            return position, None
        allocation = self.store.position_lots(position)
        if not allocation or allocation["status"] != "verified":
            raise AdjustmentError("adjustment_lot_unverified")
        lots = [lot for lot in allocation["lots"] if lot["opening_order_id"] == opening["client_order_id"]]
        if len(lots) > 1:
            raise AdjustmentError("adjustment_lot_ambiguous")
        return position, lots[0] if lots else None

    def _candidate(self, inst_id, mark=None):
        for opening in self.store.managed_opening_orders(self.account.account_scope, inst_id):
            native = self.store.get_order(attached_algo_client_id(opening["client_order_id"]))
            if not native or native["account_scope"] != opening["account_scope"] or native["order_kind"] != "algo":
                continue
            try:
                position, lot = self._position_lot(opening)
                target = Decimal(lot["remaining_size"] if lot else "0")
                raw = json.loads(native["raw_json"])
                proof = protection_evidence(opening, raw, native=True, position_size=float(Decimal(raw["sz"])))
                if (
                    native["status"] != "live" or not proof or not native_evidence(opening, raw, proof)
                    or raw.get("subAlgoIdList") not in (None, []) or raw.get("advanceOrdType") not in (None, "")
                    or Decimal(str(proof["size"])) == target
                    or target and mark is not None and self._hit(opening, proof, mark)
                ):
                    continue
                return position, opening, lot, proof, target
            except (AdjustmentError, ValueError, KeyError, InvalidOperation):
                continue
        return None

    def has_work(self, inst_id):
        return bool(self.store.protection_adjustments(self.account.account_scope, inst_id) or self._candidate(inst_id))

    async def _transition(self, record, **changes):
        updated = self.store.update_protection_adjustment(record, **changes)
        if not updated:
            raise AdjustmentError("adjustment_changed")
        if updated["status"] != record["status"]:
            payload = {
                "inst_id": updated["inst_id"], "adjustment_id": updated["adjustment_id"],
                "status": updated["status"], "target_size": updated["target_size"],
            }
            self.store.add_audit("protection_adjustment_state", "Native protection quantity state changed", payload=payload)
            await self.execution.notify_event(
                "protection_adjustment_state", "OpenPerpDesk 保护数量状态更新",
                f"{updated['inst_id']}：{ADJUSTMENT_LABELS[updated['status']]}，目标数量 {updated['target_size']} 张。",
                payload=payload, severity="warning" if updated["status"] in {"review", "rejected"} else "info",
            )
        return updated

    async def run(self, inst_id, mark_price, *, dry_run, market_data_fresh):
        try:
            if type(mark_price) not in {int, float} or not math.isfinite(mark_price) or mark_price <= 0:
                raise AdjustmentError("adjustment_mark_price_invalid")
            pending = self.store.protection_adjustments(self.account.account_scope, inst_id)
            if dry_run:
                return {"inst_id": inst_id, "action": "protection_adjustment_preview", "accepted": False} if (
                    pending or self._candidate(inst_id, mark_price)
                ) else None
            self.handoff._enabled()
            if pending:
                return await self._advance(pending[0], market_data_fresh=market_data_fresh)
            await self.handoff._refresh()
            candidate = self._candidate(inst_id, mark_price)
            if not candidate:
                return None
            position, opening, lot, proof, target = candidate
            record = self.store.claim_protection_adjustment({
                "adjustment_id": uuid4().hex, "opening_order_id": opening["client_order_id"],
                "lot_id": lot["lot_id"] if lot else None, "evidence": proof,
                "target_size": format(target, "f"), "now_ms": int(time.time() * 1000),
            }, position, expected_generation=self.store.execution_snapshot()[0])
            return await self._advance(record, market_data_fresh=market_data_fresh)
        except (AdjustmentError, HandoffError, ExposureSnapshotChanged, PreflightError) as exc:
            return {"inst_id": inst_id, "action": "protection_adjustment_wait", "accepted": False, "reasons": [str(exc)]}

    async def _advance(self, record, *, market_data_fresh, allow_send=True):
        self.handoff._enabled(record["account_scope"])
        if record["status"] == "review":
            raise AdjustmentError(record["last_error"] or "adjustment_review_required")
        proof = json.loads(record["evidence_json"])
        opening = self.store.get_order(record["opening_order_id"])
        if not opening or opening["account_scope"] != record["account_scope"]:
            raise AdjustmentError("adjustment_opening_unverified")
        try:
            raw = await self.account.algo_order_details(
                record["inst_id"], algo_id=proof["algo_id"], client_order_id=proof["algo_client_id"],
            )
        except Exception:
            raise AdjustmentError("adjustment_native_query_unavailable") from None
        self.handoff._enabled(record["account_scope"])
        target = Decimal(record["target_size"])
        expected = {**proof, "size": float(target)} if target else proof
        current = protection_evidence(
            opening, {**raw, "state": "live", "actualSz": "0"}, native=True,
            position_size=min(proof["size"], float(target)) if target else proof["size"],
        )
        if (
            current not in (proof, expected) or raw.get("subAlgoIdList") not in (None, [])
            or raw.get("advanceOrdType") not in (None, "")
        ):
            await self._transition(record, status="review", last_error="adjustment_native_changed")
            raise AdjustmentError("adjustment_native_changed")
        self.sync._save_algo_order(raw, source="okx-algo-adjustment")
        if raw["state"] in {"effective", "partially_effective"} and not raw.get("ordIdList"):
            await self._transition(record, status="review", last_error="adjustment_native_children_missing")
            raise AdjustmentError("adjustment_native_children_missing")
        if raw["state"] in {"effective", "partially_effective"} or raw["state"] == "canceled" and raw.get("ordIdList"):
            await self._transition(record, status="superseded", last_error=None)
            return {"inst_id": record["inst_id"], "action": "protection_adjustment_native_triggered", "accepted": False}
        if not target and native_evidence(opening, raw, proof, canceled=True):
            position, lot = self._position_lot(opening)
            if position["status"] != "closed" or position["size"]:
                await self._transition(record, status="superseded", last_error="adjustment_lot_changed")
                raise AdjustmentError("adjustment_lot_changed")
            await self._transition(record, status="complete", last_error=None)
            return {"inst_id": record["inst_id"], "action": "protection_adjustment_complete", "accepted": True}
        if raw["state"] in {"canceled", "order_failed", "expired", "mmp_canceled"}:
            await self._transition(record, status="review", last_error="adjustment_native_terminal")
            raise AdjustmentError("adjustment_native_terminal")
        if not native_evidence(opening, raw, current):
            await self._transition(record, status="review", last_error="adjustment_native_state_unverified")
            raise AdjustmentError("adjustment_native_state_unverified")
        if target and current == expected:
            await self.handoff._refresh()
            self.handoff._enabled(record["account_scope"])
            position, lot = self._position_lot(opening)
            if not lot or Decimal(lot["remaining_size"]) != target:
                await self._transition(record, status="superseded", last_error="adjustment_lot_changed")
                raise AdjustmentError("adjustment_lot_changed")
            await self._transition(record, status="complete", last_error=None)
            return {"inst_id": record["inst_id"], "action": "protection_adjustment_complete", "accepted": True}
        now = int(time.time() * 1000)
        if not allow_send or record["status"] in {"submitted", "accepted"} and now < record["retry_after_ms"]:
            return {"inst_id": record["inst_id"], "action": "protection_adjustment_pending", "accepted": False}
        if not market_data_fresh:
            raise AdjustmentError("adjustment_market_data_stale")
        await self.handoff._refresh()
        position, lot = self._position_lot(opening)
        remaining = Decimal(lot["remaining_size"] if lot else "0")
        if remaining != target:
            if record["status"] == "prepared":
                await self._transition(record, status="superseded", last_error="adjustment_lot_changed")
            raise AdjustmentError("adjustment_lot_changed")
        generation = self.store.execution_snapshot()[0]
        try:
            mark = await self.market.mark_price(record["inst_id"])
            rows = await self.market.instruments(record["inst_id"])
        except Exception:
            raise AdjustmentError("adjustment_market_unavailable") from None
        self.handoff._enabled(record["account_scope"])
        if target and self._hit(opening, proof, mark):
            await self._transition(record, status="superseded", last_error="adjustment_trigger_reached")
            raise AdjustmentError("adjustment_trigger_reached")
        if len(rows) != 1 or rows[0].get("instId") != record["inst_id"]:
            raise AdjustmentError("adjustment_contract_unverified")
        spec = ContractSpec.parse(rows[0])
        if target:
            spec.validate_size(target)
        try:
            fresh = await self.account.algo_order_details(
                record["inst_id"], algo_id=proof["algo_id"], client_order_id=proof["algo_client_id"],
            )
        except Exception:
            raise AdjustmentError("adjustment_native_query_unavailable") from None
        self.handoff._enabled(record["account_scope"])
        if not native_evidence(opening, fresh, proof):
            return await self._advance(record, market_data_fresh=market_data_fresh, allow_send=False)
        claimed = self.store.start_protection_adjustment(
            record, position, expected_generation=generation, now_ms=now,
        )
        if not claimed:
            raise AdjustmentError("adjustment_changed")
        self.handoff._enabled(record["account_scope"])
        try:
            response = await (
                self.execution.trade_client.amend_algo_size(record["inst_id"], proof["algo_id"], record["target_size"], record["adjustment_id"])
                if target else self.execution.trade_client.cancel_algo_order(record["inst_id"], proof["algo_id"])
            )
        except OkxOrderRejected:
            claimed = await self._transition(
                claimed, status="rejected", last_error="adjustment_rejected", retry_after_ms=now + 30000,
            )
            return {"inst_id": record["inst_id"], "action": "protection_adjustment_rejected", "accepted": False}
        except Exception:
            claimed = await self._transition(claimed, last_error="adjustment_outcome_unknown")
        else:
            data = response.get("data", [])
            if (
                len(data) == 1 and data[0].get("algoId") == proof["algo_id"]
                and (not target or data[0].get("reqId") == record["adjustment_id"])
                and str(data[0].get("sCode", "")).isdigit()
            ):
                rejected = str(data[0]["sCode"]) != "0"
                claimed = await self._transition(
                    claimed, status="rejected" if rejected else "accepted",
                    last_error="adjustment_rejected" if rejected else None,
                )
                if rejected:
                    return {"inst_id": record["inst_id"], "action": "protection_adjustment_rejected", "accepted": False}
            else:
                claimed = await self._transition(claimed, last_error="adjustment_ack_unverified")
        return await self._advance(claimed, market_data_fresh=market_data_fresh, allow_send=False)
