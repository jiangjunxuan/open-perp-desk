import hashlib
import json
import math
from decimal import Decimal, InvalidOperation
from typing import Any

from .state_store import StateStore


def attached_algo_client_id(client_order_id: str) -> str:
    return "opdp" + hashlib.sha256(client_order_id.encode()).hexdigest()[:24]


def _positive(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("protection_number_invalid")
    return result


def _levels(item: dict[str, Any]) -> tuple[float | None, float | None]:
    levels = []
    for leg in ("sl", "tp"):
        value = item.get(f"{leg}TriggerPx")
        if value in (None, ""):
            levels.append(None)
            continue
        if (
            item.get(f"{leg}TriggerPxType") != "mark" or item.get(f"{leg}OrdPx") != "-1"
            or item.get(f"{leg}TriggerRatio") not in (None, "")
        ):
            raise ValueError("protection_trigger_unsupported")
        price = float(_positive(value))
        if not math.isfinite(price) or price <= 0:
            raise ValueError("protection_price_invalid")
        levels.append(price)
    if not any(levels) or item.get("amendPxOnTriggerType") not in (None, "", "0"):
        raise ValueError("protection_levels_unverified")
    return levels[0], levels[1]


def protection_evidence(
    opening: dict[str, Any],
    current: dict[str, Any],
    *,
    native: bool,
    position_size: float,
) -> dict[str, Any] | None:
    """Normalize only fixed-size, mark-triggered market exits we can reproduce."""
    try:
        owner = opening["client_order_id"]
        client_id = attached_algo_client_id(owner)
        opening_raw = json.loads(opening["raw_json"])
        filled = _positive(opening_raw.get("accFillSz"))
        quantity = _positive(abs(position_size))
        timestamp = _positive(current.get("uTime"))
        if timestamp != timestamp.to_integral_value():
            return None
        if not quantity <= filled <= _positive(opening["size"]):
            return None
        if any(current.get(remote) != opening[local] for remote, local in (
            ("instId", "inst_id"), ("posSide", "pos_side"), ("tdMode", "td_mode"),
        )):
            return None
        if native:
            closing_side = "sell" if opening["side"] == "buy" else "buy"
            if (
                current.get("algoClOrdId") != client_id or not current.get("algoId")
                or current.get("state") != "live" or current.get("side") != closing_side
                or current.get("ordType") not in {"conditional", "oco"}
                or (opening["pos_side"] == "net" and str(current.get("reduceOnly")).lower() != "true")
                or current.get("closeFraction") not in (None, "")
                or current.get("actualSz") not in (None, "", "0")
            ):
                return None
            native_size = _positive(current.get("sz"))
            if not quantity <= native_size <= filled:
                return None
            stop, target = _levels(current)
            # Conditional TP/SL orders do not execute both legs as OCO.
            if current["ordType"] == "conditional" and stop and target:
                return None
            identity = {"kind": "native", "algo_id": str(current["algoId"]), "size": float(native_size)}
        else:
            if (
                current.get("state") != "partially_filled"
                or current.get("ordId") != opening["exchange_order_id"]
                or current.get("clOrdId") != owner or current.get("side") != opening["side"]
                or current.get("tradeId") != opening_raw.get("tradeId")
                or _positive(current.get("accFillSz")) != filled
                or _positive(current.get("sz")) != _positive(opening["size"])
            ):
                return None
            attached = current.get("attachAlgoOrds")
            if not isinstance(attached, list) or len(attached) != 1 or not isinstance(attached[0], dict):
                return None
            terms = attached[0]
            if (
                terms.get("attachAlgoClOrdId") != client_id
                or terms.get("tpOrdKind") not in (None, "", "condition")
                or terms.get("sz") not in (None, "")
                or terms.get("failCode") not in (None, "", "0")
            ):
                return None
            stop, target = _levels(terms)
            identity = {"kind": "attached", "algo_id": None, "size": float(filled)}
        return {
            **identity, "opening_order_id": owner, "opening_exchange_id": opening["exchange_order_id"],
            "algo_client_id": client_id, "stop_loss": stop, "take_profit": target,
        }
    except (ValueError, TypeError, InvalidOperation, KeyError, AttributeError):
        return None


def linked_protection(
    store: StateStore,
    inst_id: str,
    pos_side: str,
    size: float,
    *,
    td_mode: str,
    account_scope: str | None,
    trade_id: str | None,
) -> dict[str, Any] | None:
    if (
        not account_scope or not trade_id or not math.isfinite(size) or not size
        or pos_side not in {"net", "long", "short"} or td_mode not in {"cross", "isolated"}
    ):
        return None
    side = "buy" if pos_side == "long" or (pos_side == "net" and size > 0) else "sell"
    orders = store.protection_orders(inst_id, account_scope, pos_side, td_mode, trade_id)
    if len(orders) != 1:
        return None
    order = orders[0]
    try:
        raw = json.loads(order["raw_json"])
        if not isinstance(raw, dict) or any(raw.get(field) != expected for field, expected in (
            ("instId", inst_id), ("posSide", pos_side), ("tdMode", td_mode), ("side", side),
            ("ordId", order["exchange_order_id"]), ("clOrdId", order["client_order_id"]),
        )) or not order["exchange_order_id"] or order["side"] != side or order["source"].startswith("okx-"):
            return None
        native = store.get_order(attached_algo_client_id(order["client_order_id"]))
        if native is not None:
            if native["account_scope"] != account_scope or native["order_kind"] != "algo":
                return None
            current = json.loads(native["raw_json"])
            if (
                not isinstance(current, dict) or current.get("algoId") != native["exchange_order_id"]
                or current.get("state") != native["status"]
            ):
                return None
            return protection_evidence(order, current, native=True, position_size=size)
        # Filled-order attached terms are historical intent, not current native state.
        return protection_evidence(order, raw, native=False, position_size=size)
    except (ValueError, TypeError):
        return None


def attached_parent_evidence(
    opening: dict[str, Any], current: dict[str, Any], *, position_size: float,
    expected: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Verify attached terms independently of a parent's active/terminal state."""
    try:
        state = current.get("state")
        filled, size = _positive(current.get("accFillSz")), _positive(current.get("sz"))
        if (
            state not in {"partially_filled", "filled", "canceled", "mmp_canceled"}
            or filled > size or state == "filled" and filled != size
            or state == "partially_filled" and filled == size
        ):
            return None
        proof = protection_evidence(
            {**opening, "raw_json": json.dumps(current)},
            {**current, "state": "partially_filled"}, native=False, position_size=position_size,
        )
        if not proof or expected is not None and (
            proof["size"] < expected["size"]
            or any(proof.get(key) != value for key, value in expected.items() if key != "size")
        ):
            return None
        return proof
    except (ValueError, TypeError, InvalidOperation):
        return None


def triggered_protection_evidence(
    opening: dict[str, Any], current: dict[str, Any], *, position_size: float,
) -> dict[str, Any] | None:
    children = current.get("ordIdList")
    if (
        current.get("state") not in {"effective", "partially_effective", "canceled"}
        or not isinstance(children, list) or not children
        or any(not isinstance(item, str) or not item.isascii() or not item.isalnum() for item in children)
        or len(set(children)) != len(children)
        or current.get("ordId") not in (None, "", *children)
        or current.get("subAlgoIdList") not in (None, [])
        or current.get("advanceOrdType") not in (None, "")
    ):
        return None
    return protection_evidence(
        opening, {**current, "state": "live", "actualSz": "0"},
        native=True, position_size=position_size,
    )
