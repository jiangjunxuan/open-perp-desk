import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from .position_protection import attached_algo_client_id, protection_evidence
from .state_store import LOT_RECONSTRUCTION_VERSION, StateStore


class PositionLotError(ValueError):
    pass


def _decimal(value: Any, *, positive: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (ValueError, TypeError, InvalidOperation) as exc:
        raise PositionLotError("lot_quantity_invalid") from exc
    if not number.is_finite() or (positive and number <= 0):
        raise PositionLotError("lot_quantity_invalid")
    return number


def _identifier(value: Any) -> str:
    if not isinstance(value, str) or not value.isascii() or not value.isalnum() or len(value) > 128:
        raise PositionLotError("lot_fill_identity_invalid")
    return value


def _fill(row: dict[str, Any], order: dict[str, Any], position: dict[str, Any]) -> dict[str, Any]:
    try:
        raw = json.loads(order["raw_json"])
        if (
            order["account_scope"] != position["account_scope"] or order["order_kind"] != "standard"
            or order["status"] not in {"partially_filled", "filled", "canceled", "mmp_canceled"}
            or not isinstance(raw, dict) or raw.get("ordId") != order["exchange_order_id"]
            or any(row.get(remote) != order[local] or raw.get(remote) != order[local] for remote, local in (
                ("instId", "inst_id"), ("side", "side"), ("posSide", "pos_side"),
            )) or raw.get("tdMode") != order["td_mode"]
            or row.get("ordId") != order["exchange_order_id"]
            or (row.get("clOrdId") or "") != (raw.get("clOrdId") or "")
        ):
            raise PositionLotError("lot_order_identity_mismatch")
        if not order["source"].startswith("okx-") and row.get("clOrdId") != order["client_order_id"]:
            raise PositionLotError("lot_order_identity_mismatch")
        quantity = _decimal(row.get("fillSz"), positive=True)
        if not quantity <= _decimal(raw.get("accFillSz"), positive=True) <= _decimal(order["size"], positive=True):
            raise PositionLotError("lot_fill_size_unverified")
        if row.get("subType") not in {"1", "2", "3", "4", "5", "6"}:
            raise PositionLotError("lot_fill_type_unsupported")
        price = _decimal(row.get("fillPx"), positive=True)
        if not math.isfinite(float(price)):
            raise PositionLotError("lot_fill_price_invalid")
        stamp = _decimal(row.get("fillTime"), positive=True)
        if stamp != stamp.to_integral_value():
            raise PositionLotError("lot_fill_time_invalid")
        opening_side = "buy" if (
            position["pos_side"] == "long" or (position["pos_side"] == "net" and position["size"] > 0)
        ) else "sell"
        effect = quantity if row["side"] == opening_side else -quantity
        if (
            row["side"] != {"1": "buy", "2": "sell", "3": "buy", "4": "sell", "5": "sell", "6": "buy"}[row["subType"]]
            or row["subType"] in {"3", "4"} and effect < 0
            or row["subType"] in {"5", "6"} and effect > 0
        ):
            raise PositionLotError("lot_fill_direction_mismatch")
        return {
            "bill_id": _identifier(row.get("billId")), "trade_id": _identifier(row.get("tradeId")),
            "quantity": quantity, "effect": effect, "price": price, "timestamp": int(stamp), "order": order,
        }
    except (KeyError, TypeError, AttributeError, json.JSONDecodeError) as exc:
        raise PositionLotError("lot_fill_payload_invalid") from exc


class PositionLotReconciler:
    """Reconstruct virtual entry lots from a complete exchange fill chain to flat."""

    def __init__(self, store: StateStore, account, save_order: Callable[..., bool]) -> None:
        self.store, self.account, self.save_order = store, account, save_order

    async def reconcile(self, position: dict[str, Any]) -> dict[str, Any]:
        if not position.get("account_scope") or not position.get("exchange_trade_id"):
            return {
                "status": "unverified", "reason": "lot_position_identity_unverified",
                "lots": [], "execution_ready": False,
            }
        cached = self.store.position_lots(position)
        if cached and cached["status"] == "verified" and position.get("account_scope") == self.account.account_scope:
            return cached
        generation = self.store.execution_snapshot()[0]
        try:
            if (
                not position.get("account_scope") or position["account_scope"] != self.account.account_scope
                or not position.get("exchange_trade_id") or position["td_mode"] not in {"cross", "isolated"}
                or position["pos_side"] not in {"net", "long", "short"}
            ):
                raise PositionLotError("lot_position_identity_unverified")
            frames = await self._chain(position)
            generation = self.store.execution_snapshot()[0]
            result = self._allocate(position, frames)
            result["execution_generation"] = generation
        except Exception as exc:
            generation = self.store.execution_snapshot()[0]
            reason = str(exc) if isinstance(exc, PositionLotError) else "lot_history_unavailable"
            result = {"status": "unverified", "reason": reason, "lots": [], "execution_ready": False}
        if not self.store.save_position_lots(position, result, expected_generation=generation):
            return {"status": "unverified", "reason": "lot_snapshot_changed", "lots": [], "execution_ready": False}
        if result["status"] == "unverified" and (
            not cached or cached.get("reason") != result["reason"]
        ):
            self.store.add_audit(
                "position_lots_unverified", "Entry lot allocation could not be verified",
                severity="warning", payload={"inst_id": position["inst_id"], "reason": result["reason"]},
            )
        return result

    async def _chain(self, position: dict[str, Any]) -> list[dict[str, Any]]:
        remaining = abs(_decimal(position["size"], positive=False))
        if not remaining:
            raise PositionLotError("lot_position_closed")
        started = False
        frames = []
        orders = {}
        seen = set()
        previous_bill = None
        previous_time = None
        async for page in self.account.position_fill_pages(position["inst_id"]):
            if position["account_scope"] != self.account.account_scope:
                raise PositionLotError("lot_account_changed")
            for row in page:
                bill = _identifier(row.get("billId"))
                if not bill.isdigit() or int(bill) <= 0 or (
                    previous_bill is not None and int(bill) >= previous_bill
                ) or row.get("instId") != position["inst_id"] or row.get("instType") != "SWAP":
                    raise PositionLotError("lot_history_order_invalid")
                previous_bill = int(bill)
                if row.get("posSide") != position["pos_side"]:
                    continue
                if not started and row.get("tradeId") != position["exchange_trade_id"]:
                    continue
                exchange_id = _identifier(row.get("ordId"))
                if exchange_id not in orders:
                    order = self.store.exchange_order(position["inst_id"], position["account_scope"], exchange_id)
                    if order is None:
                        raw = await self.account.order_details(position["inst_id"], ord_id=exchange_id)
                        self.save_order(raw, source="okx-lot-history")
                        order = self.store.exchange_order(position["inst_id"], position["account_scope"], exchange_id)
                    if order is None:
                        raise PositionLotError("lot_order_missing")
                    orders[exchange_id] = order
                order = orders[exchange_id]
                if order["td_mode"] != position["td_mode"]:
                    continue
                frame = _fill(row, order, position)
                if frame["trade_id"] in seen:
                    raise PositionLotError("lot_trade_identity_ambiguous")
                seen.add(frame["trade_id"])
                if not started:
                    if frame["trade_id"] != position["exchange_trade_id"]:
                        continue
                    started = True
                if previous_time is not None and frame["timestamp"] > previous_time:
                    raise PositionLotError("lot_fill_time_order_invalid")
                previous_time = frame["timestamp"]
                frames.append(frame)
                remaining -= frame["effect"]
                if remaining < 0:
                    raise PositionLotError("lot_reversal_requires_review")
                if remaining == 0:
                    return list(reversed(frames))
        raise PositionLotError("lot_flat_boundary_missing" if started else "lot_latest_trade_missing")

    def _close_owner(self, position: dict[str, Any], order: dict[str, Any], lots: list[dict[str, Any]]) -> str | None:
        owners = set()
        context = json.loads(order.get("protection_context_json") or "{}")
        if order["source"].startswith("protective-") and not context:
            raise PositionLotError("lot_close_context_unverified")
        if context:
            if not order["source"].startswith("protective-") or not order["reduce_only"]:
                raise PositionLotError("lot_close_context_unverified")
            matching = [lot for lot in lots if (
                lot["opening_order_id"] == context.get("opening_order_id")
                and lot["opening_exchange_id"] == context.get("opening_exchange_id")
            )]
            if len(matching) != 1:
                raise PositionLotError("lot_close_owner_missing")
            owners.add(matching[0]["opening_order_id"])
        native = self.store.native_parent_orders(position, order["exchange_order_id"])
        if len(native) > 1:
            raise PositionLotError("lot_native_owner_ambiguous")
        for parent in native:
            if parent["status"] not in {"effective", "partially_effective"}:
                raise PositionLotError("lot_native_execution_unconfirmed")
            matching = [lot for lot in lots if lot["native_client_id"] == parent["client_order_id"]]
            if len(matching) != 1:
                raise PositionLotError("lot_native_owner_missing")
            owners.add(matching[0]["opening_order_id"])
        if len(owners) > 1:
            raise PositionLotError("lot_close_owner_ambiguous")
        return next(iter(owners), None)

    def _allocate(self, position: dict[str, Any], frames: list[dict[str, Any]]) -> dict[str, Any]:
        lots = []
        attributed_closes = fifo_closes = 0
        totals = {}
        checked_orders = set()
        for frame in frames:
            order = frame["order"]
            owner = order["client_order_id"]
            if owner not in checked_orders:
                if self.store.get_order(owner) != order:
                    raise PositionLotError("lot_order_snapshot_changed")
                checked_orders.add(owner)
            totals[owner] = totals.get(owner, Decimal(0)) + frame["quantity"]
            if totals[owner] > _decimal(json.loads(order["raw_json"])["accFillSz"]):
                raise PositionLotError("lot_order_fill_total_mismatch")
            if frame["effect"] > 0:
                if order["reduce_only"]:
                    raise PositionLotError("lot_reducing_order_opened_position")
                lot = next((item for item in lots if item["opening_order_id"] == owner), None)
                if lot is None:
                    managed = not order["source"].startswith("okx-")
                    identity = "\n".join((position["account_scope"], position["position_key"], owner, frame["bill_id"]))
                    lot = {
                        "lot_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                        "opening_order_id": owner, "opening_exchange_id": order["exchange_order_id"],
                        "first_bill_id": frame["bill_id"], "source": order["source"], "managed": managed,
                        "native_client_id": attached_algo_client_id(owner) if managed else None,
                        "opened_size": Decimal(0), "remaining_size": Decimal(0), "cost": Decimal(0),
                    }
                    lots.append(lot)
                lot["opened_size"] += frame["quantity"]
                lot["remaining_size"] += frame["quantity"]
                lot["cost"] += frame["quantity"] * frame["price"]
            else:
                target = self._close_owner(position, order, lots)
                selected = [lot for lot in lots if target is None or lot["opening_order_id"] == target]
                remaining = frame["quantity"]
                for lot in selected:
                    consumed = min(remaining, lot["remaining_size"])
                    lot["remaining_size"] -= consumed
                    remaining -= consumed
                if remaining:
                    raise PositionLotError("lot_close_quantity_exceeds_owner")
                if target:
                    attributed_closes += 1
                else:
                    fifo_closes += 1
        if sum((lot["remaining_size"] for lot in lots), Decimal(0)) != abs(_decimal(position["size"])):
            raise PositionLotError("lot_position_quantity_mismatch")
        return {
            "status": "verified", "reason": None, "policy": "fifo_with_protective_links",
            "policy_version": LOT_RECONSTRUCTION_VERSION,
            "execution_ready": False, "fill_count": len(frames), "first_bill_id": frames[0]["bill_id"],
            "last_bill_id": frames[-1]["bill_id"], "attributed_closes": attributed_closes, "fifo_closes": fifo_closes,
            "lots": [{
                **{key: value for key, value in lot.items() if key not in {"opened_size", "remaining_size", "cost"}},
                "opened_size": str(lot["opened_size"]), "remaining_size": str(lot["remaining_size"]),
                "closed_size": str(lot["opened_size"] - lot["remaining_size"]),
                "entry_price": str(lot["cost"] / lot["opened_size"]),
            } for lot in lots if lot["remaining_size"] > 0],
        }


def positions_with_lots(store: StateStore, *, account_scope: str | None = None) -> list[dict[str, Any]]:
    positions = store.list_positions()
    for position in positions:
        if account_scope is not None and position.get("account_scope") != account_scope:
            position["lot_allocation"] = {
                "status": "unverified", "reason": "lot_account_changed", "lots": [], "execution_ready": False,
            }
            continue
        allocation = store.position_lots(position) or {
            "status": "unverified", "reason": (
                "lot_snapshot_pending" if position.get("account_scope") and position.get("exchange_trade_id")
                else "lot_position_identity_unverified"
            ), "lots": [], "execution_ready": False,
        }
        handoffs = {
            row["lot_id"]: row for row in store.protection_handoffs(position.get("account_scope"), position["inst_id"])
            if row["position_key"] == position["position_key"]
        }
        for lot in allocation["lots"]:
            handoff = handoffs.get(lot["lot_id"])
            lot["handoff"] = {
                key: handoff[key] for key in ("handoff_id", "status", "last_error", "close_sequence")
            } if handoff else None
            protection = None
            state = "external_entry" if not lot["managed"] else "native_unverified"
            opening = store.get_order(lot["opening_order_id"])
            native = store.get_order(lot["native_client_id"]) if lot["native_client_id"] else None
            if opening and native and native["account_scope"] == position["account_scope"] and native["order_kind"] == "algo":
                try:
                    raw = json.loads(native["raw_json"])
                    if raw.get("state") == native["status"] and raw.get("algoId") == native["exchange_order_id"]:
                        if native["status"] == "live":
                            covered_size = min(_decimal(raw.get("sz"), positive=True), _decimal(lot["remaining_size"], positive=True))
                            protection = protection_evidence(
                                opening, raw, native=True, position_size=float(covered_size),
                            )
                        elif native["status"] in {"canceled", "effective", "order_failed", "pause"}:
                            state = native["status"]
                    if protection:
                        state = "native_matched" if _decimal(protection["size"]) == _decimal(lot["remaining_size"]) else "native_size_mismatch"
                except (ValueError, TypeError, AttributeError):
                    protection = None
            lot["protection"] = {
                "state": state, "size": protection["size"] if protection else None,
                "stop_loss": protection["stop_loss"] if protection else None,
                "take_profit": protection["take_profit"] if protection else None,
            }
        position["lot_allocation"] = allocation
    return positions
