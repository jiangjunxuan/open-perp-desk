import json
from decimal import Decimal
from uuid import uuid4

from .okx_account import OkxAccountError
from .position_protection import attached_parent_evidence, protection_evidence
from .protection_handoff import effective_evidence, native_evidence, native_execution_evidence
from .state_store import ACTIVE_ORDER_STATUSES


class ProtectionReviewError(ValueError):
    pass


class ProtectionReviewer:
    """Read exchange evidence before releasing a maintenance review; never trade."""

    def __init__(self, store, sync):
        self.store, self.sync = store, sync
        self.account = sync.account_client

    def _account(self, scope):
        if not self.account.configured or self.account.account_scope != scope:
            raise ProtectionReviewError("review_account_unavailable")

    async def _query(self, scope, method, *args, **kwargs):
        result = await method(*args, **kwargs)
        self._account(scope)
        return result

    @staticmethod
    def _native_identity(opening, raw, proof):
        if (
            raw.get("algoClOrdId") != proof["algo_client_id"]
            or not raw.get("algoId")
            or proof.get("algo_id") and raw["algoId"] != proof["algo_id"]
            or raw.get("side") != ("sell" if opening["side"] == "buy" else "buy")
            or any(raw.get(remote) != opening[local] for remote, local in (
                ("instId", "inst_id"), ("posSide", "pos_side"), ("tdMode", "td_mode"),
            ))
            or raw.get("ordType") not in {"conditional", "oco"}
            or opening["pos_side"] == "net" and str(raw.get("reduceOnly")).lower() != "true"
            or raw.get("subAlgoIdList") not in (None, [])
            or raw.get("advanceOrdType") not in (None, "")
        ):
            raise ProtectionReviewError("review_native_identity_unverified")

    async def _terminal_native(self, record, opening, raw, proof):
        self._native_identity(opening, raw, proof)
        if raw.get("state") not in {"canceled", "effective", "order_failed", "expired", "mmp_canceled"}:
            raise ProtectionReviewError("review_native_not_terminal")
        if raw.get("ordIdList"):
            quantity = float(Decimal(str(raw["sz"])))
            current = protection_evidence(
                opening, {**raw, "state": "live", "actualSz": "0"}, native=True, position_size=quantity,
            )
            if not current:
                raise ProtectionReviewError("review_native_children_unverified")
            await native_execution_evidence(self.account, opening, raw, current)
            self._account(record["account_scope"])
        elif (
            raw.get("state") == "effective" or raw.get("ordId") not in (None, "")
            or raw.get("actualSz") not in (None, "", "0")
        ):
            raise ProtectionReviewError("review_native_children_unverified")

    async def _handoff_status(self, record, opening, parent, raw, proof):
        original = json.loads(record["evidence_json"])
        if original["kind"] == "attached":
            if not attached_parent_evidence(opening, parent, position_size=original["size"], expected=original):
                raise ProtectionReviewError("review_opening_protection_changed")
            if not record.get("native_evidence_json"):
                if raw is None:
                    if record["close_sequence"] and parent["state"] == "filled":
                        raise ProtectionReviewError("review_native_binding_unverified")
                    if parent["state"] == "partially_filled":
                        if record["close_sequence"]:
                            raise ProtectionReviewError("review_opening_changed")
                        return "opening_cancel_pending"
                    return "native_pending" if parent["state"] == "filled" else "ready"
                discovered = protection_evidence(
                    opening, {**raw, "state": "live", "actualSz": "0"}, native=True,
                    position_size=float(Decimal(parent["accFillSz"])),
                )
                if (
                    not discovered or record["close_sequence"]
                    or any(discovered[key] != original[key] for key in (
                        "opening_order_id", "opening_exchange_id", "algo_client_id", "stop_loss", "take_profit",
                    ))
                ):
                    raise ProtectionReviewError("review_native_binding_unverified")
                self._native_identity(opening, raw, discovered)
                if raw["state"] == "live":
                    if not native_evidence(opening, raw, discovered):
                        raise ProtectionReviewError("review_native_changed")
                elif raw["state"] == "canceled" and not raw.get("ordIdList"):
                    if not native_evidence(opening, raw, discovered, canceled=True):
                        raise ProtectionReviewError("review_native_changed")
                else:
                    await native_execution_evidence(self.account, opening, raw, discovered, require_terminal=False)
                    self._account(record["account_scope"])
                return "opening_cancel_pending" if parent["state"] == "partially_filled" else "native_pending"
        if raw is None:
            raise ProtectionReviewError("review_native_unavailable")
        self._native_identity(opening, raw, proof)
        if raw["state"] == "live":
            if record["close_sequence"] or record.get("native_settlement_json") or not native_evidence(opening, raw, proof):
                raise ProtectionReviewError("review_native_changed")
            return "cancel_pending"
        if raw["state"] == "canceled" and not raw.get("ordIdList"):
            if record.get("native_settlement_json") or not native_evidence(opening, raw, proof, canceled=True):
                raise ProtectionReviewError("review_native_changed")
            return "closing" if record["close_order_id"] else "ready"
        evidence, _ = await native_execution_evidence(
            self.account, opening, raw, proof, require_terminal=False,
        )
        self._account(record["account_scope"])
        if record.get("native_settlement_json") and json.loads(record["native_settlement_json"]) != evidence:
            raise ProtectionReviewError("review_native_settlement_changed")
        if not record.get("native_settlement_json") and record["close_sequence"]:
            raise ProtectionReviewError("review_native_settlement_missing")
        if raw["state"] == "partially_effective":
            if record["close_sequence"] or record.get("native_settlement_json"):
                raise ProtectionReviewError("review_native_reactivated")
            return "native_cancel_pending"
        return "native_executing"

    async def _adjustment_status(self, record, opening, raw, proof, lot):
        if raw is None:
            raise ProtectionReviewError("review_native_unavailable")
        self._native_identity(opening, raw, proof)
        target = Decimal(record["target_size"])
        expected = {**proof, "size": float(target)} if target else proof
        current = protection_evidence(
            opening, {**raw, "state": "live", "actualSz": "0"}, native=True,
            position_size=min(proof["size"], float(target)) if target else proof["size"],
        )
        if current not in (proof, expected):
            raise ProtectionReviewError("review_native_changed")
        if raw["state"] in {"effective", "partially_effective"} or raw["state"] == "canceled" and raw.get("ordIdList"):
            await native_execution_evidence(self.account, opening, raw, current, require_terminal=False)
            self._account(record["account_scope"])
            return "superseded", None
        remaining = Decimal(lot["remaining_size"]) if lot else Decimal(0)
        if native_evidence(opening, raw, proof, canceled=True) and not target and not remaining:
            return "complete", None
        if not native_evidence(opening, raw, current):
            raise ProtectionReviewError("review_native_changed")
        if remaining != target:
            if record["attempts"] and current != expected:
                raise ProtectionReviewError("review_adjustment_outcome_unconfirmed")
            return "superseded", {
                "adjustment_id": uuid4().hex, "opening_order_id": record["opening_order_id"],
                "lot_id": lot["lot_id"] if lot else None, "target_size": format(remaining, "f"),
                "evidence": current,
            }
        if target and current == expected:
            return "complete", None
        return ("accepted" if record["attempts"] else "prepared"), None

    async def review(self, kind, record, *, expected_version, resolution, note):
        if kind not in {"handoff", "adjustment"} or resolution not in {"resume", "position_closed"}:
            raise ProtectionReviewError("review_request_invalid")
        if not 3 <= len(note.strip()) <= 500:
            raise ProtectionReviewError("review_note_required")
        if type(expected_version) is not int or record["version"] != expected_version or record["status"] != "review":
            raise ProtectionReviewError("review_record_changed")
        scope = record["account_scope"]
        self._account(scope)
        snapshot = await self._query(scope, self.sync.sync_rest)
        if snapshot.get("errors"):
            raise ProtectionReviewError("review_account_snapshot_incomplete")
        proof = effective_evidence(record) if kind == "handoff" else json.loads(record["evidence_json"])
        opening = self.store.get_order(proof["opening_order_id"])
        position = self.store.get_position(record["position_key"])
        if (
            not opening or opening["account_scope"] != scope or opening["order_kind"] != "standard"
            or opening["exchange_order_id"] != proof["opening_exchange_id"]
            or not position or position["account_scope"] != scope
            or any(position[key] != opening[key] for key in ("inst_id", "pos_side", "td_mode"))
        ):
            raise ProtectionReviewError("review_position_unverified")
        closed = position["status"] == "closed" and position["size"] == 0
        if resolution == "position_closed" and not closed:
            raise ProtectionReviewError("review_position_not_closed")
        generation, active = self.store.execution_snapshot()
        if closed and any(row["account_scope"] == scope and row["inst_id"] == record["inst_id"]
                          and row["order_kind"] == "standard" for row in active):
            raise ProtectionReviewError("review_orders_pending")
        allocation = None if closed else self.store.position_lots(position)
        if not closed and (not allocation or allocation.get("status") != "verified"):
            raise ProtectionReviewError("review_lot_unverified")
        lots = [row for row in (allocation or {}).get("lots", []) if row["opening_order_id"] == proof["opening_order_id"]]
        if len(lots) > 1 or lots and lots[0]["lot_id"] != record["lot_id"]:
            raise ProtectionReviewError("review_lot_changed")
        lot = lots[0] if lots else None
        if kind == "handoff" and bool(record["close_order_id"]) != bool(record["close_sequence"]):
            raise ProtectionReviewError("review_close_unconfirmed")
        if kind == "handoff" and record["close_order_id"]:
            close = self.store.get_order(record["close_order_id"])
            context = json.loads(close["protection_context_json"] or "{}") if close else {}
            if (
                not close or close["account_scope"] != scope or close["inst_id"] != record["inst_id"]
                or close["order_kind"] != "standard" or not close["reduce_only"]
                or context.get("handoff_id") != record["handoff_id"]
                or context.get("close_sequence") != record["close_sequence"]
                or close["status"] not in {*ACTIVE_ORDER_STATUSES, "filled", "canceled", "mmp_canceled", "rejected"}
                or closed and close["status"] not in {"filled", "canceled", "mmp_canceled", "rejected"}
            ):
                raise ProtectionReviewError("review_close_unconfirmed")
        parent = await self._query(
            scope, self.account.order_details, record["inst_id"],
            ord_id=proof["opening_exchange_id"], client_order_id=proof["opening_order_id"],
        )
        self.sync._validate_local_order(opening, parent)
        cached = json.loads(opening["raw_json"])
        if (
            parent.get("ordId") != proof["opening_exchange_id"] or parent.get("clOrdId") != proof["opening_order_id"]
            or parent.get("state") != opening["status"]
            or Decimal(str(parent.get("accFillSz"))) != Decimal(str(cached.get("accFillSz")))
            or parent.get("tradeId") != cached.get("tradeId")
        ):
            raise ProtectionReviewError("review_opening_changed")
        if closed and parent["state"] not in {"filled", "canceled", "mmp_canceled"}:
            raise ProtectionReviewError("review_orders_pending")
        try:
            raw = await self._query(
                scope, self.account.algo_order_details, record["inst_id"],
                algo_id=proof.get("algo_id"), client_order_id=proof["algo_client_id"],
            )
        except OkxAccountError as exc:
            self._account(scope)
            if (
                exc.code != "51603" or proof["kind"] != "attached"
                or self.store.get_order(proof["algo_client_id"])
                or parent["state"] not in {"filled", "canceled", "mmp_canceled", "partially_filled"}
            ):
                raise ProtectionReviewError("review_native_unavailable") from None
            raw = None
        known_native = self.store.get_order(proof["algo_client_id"])
        if raw is not None and known_native is not None and (
            known_native["account_scope"] != scope or known_native["order_kind"] != "algo"
            or known_native["exchange_order_id"] != raw.get("algoId")
        ):
            raise ProtectionReviewError("review_native_identity_unverified")
        replacement = None
        if resolution == "position_closed":
            if raw is None:
                if parent["state"] not in {"canceled", "mmp_canceled"} or Decimal(parent["accFillSz"]) >= Decimal(parent["sz"]):
                    raise ProtectionReviewError("review_native_unavailable")
            else:
                await self._terminal_native(record, opening, raw, proof)
            status = "complete"
        elif kind == "handoff":
            status = await self._handoff_status(record, opening, parent, raw, proof)
        else:
            status, replacement = await self._adjustment_status(record, opening, raw, proof, lot)
        self._account(scope)
        return self.store.review_protection_record(
            kind, record, position, expected_generation=generation, allocation=allocation,
            status=status, resolution=resolution, note=note, replacement=replacement,
        )
