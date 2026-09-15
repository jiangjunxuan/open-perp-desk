import json
from typing import Any


INCIDENT_LABELS = {
    "open": "附带保护创建失败",
    "review": "保护事故需人工复核",
    "resolved": "保护事故已解除",
    "superseded": "原生保护已恢复",
}


def incident_summaries(store, account_scope: str) -> list[dict[str, Any]]:
    rows = store.protection_incidents(account_scope)
    incidents = []
    for row in rows:
        try:
            expected = json.loads(row["expected_protection_json"] or "{}")
        except (TypeError, ValueError):
            expected = {}
        incidents.append({
            "incident_id": row["incident_id"],
            "inst_id": row["inst_id"],
            "position_key": row["position_key"],
            "opening_order_id": row["opening_order_id"],
            "exchange_order_id": row["exchange_order_id"],
            "expected_protection": expected,
            "failure_code": row["failure_code"],
            "failure_detail": row["failure_detail"],
            "status": row["status"],
            "resolution": row["resolution"],
            "resolution_note": row["resolution_note"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "resolved_at": row["resolved_at"],
        })
    return incidents


def expected_protection(order: dict[str, Any]) -> dict[str, Any]:
    try:
        context = json.loads(order.get("protection_context_json") or "{}")
    except (TypeError, ValueError):
        context = {}
    return {
        "opening_order_id": order["client_order_id"],
        "opening_exchange_id": order.get("exchange_order_id"),
        "stop_loss": order.get("stop_loss"),
        "take_profit": order.get("take_profit"),
        **({"protection_context": context} if context else {}),
    }
