import asyncio
from datetime import datetime, timezone
from typing import Any

from .okx_account import OkxAccountClient, OkxAccountError
from .okx_algo_stream import OkxAlgoOrderStream
from .okx_account_stream import OkxAccountStream
from .state_store import StateStore


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _text(value: Any, default: str = "") -> str:
    return str(value) if value not in (None, "") else default


def _timestamp(value: Any) -> str:
    raw = _text(value)
    if not raw:
        return "unknown"
    try:
        return datetime.fromtimestamp(
            float(raw) / 1000,
            timezone.utc,
        ).isoformat()
    except (TypeError, ValueError, OSError):
        return raw


def _position_side_for_order(pos_side: str, size: float) -> str:
    if pos_side == "long":
        return "buy"
    if pos_side == "short":
        return "sell"
    return "buy" if size >= 0 else "sell"


class AccountSynchronizer:
    """Normalize OKX private stream/REST snapshots into local state."""

    def __init__(
        self,
        store: StateStore,
        account_client: OkxAccountClient,
        account_stream: OkxAccountStream,
        algo_stream: OkxAlgoOrderStream | None = None,
    ) -> None:
        self.store = store
        self.account_client = account_client
        self.account_stream = account_stream
        self.algo_stream = algo_stream

    def _local_protection(
        self,
        inst_id: str,
        pos_side: str,
        size: float,
    ) -> tuple[float | None, float | None]:
        expected_side = _position_side_for_order(pos_side, size)
        for order in self.store.list_orders(500):
            if (
                order.get("inst_id") != inst_id
                or order.get("reduce_only")
                or order.get("side") != expected_side
                or order.get("status") not in {
                    "preview",
                    "submitting",
                    "submitted",
                    "live",
                    "partially_filled",
                    "filled",
                }
            ):
                continue
            stop_loss = order.get("stop_loss")
            take_profit = order.get("take_profit")
            if stop_loss or take_profit:
                return stop_loss, take_profit
        return None, None

    def _save_algo_order(self, item: dict[str, Any]) -> bool:
        algo_id = str(
            item.get("algoId")
            or item.get("ordId")
            or item.get("algoClOrdId")
            or item.get("clOrdId")
            or ""
        )
        if not algo_id:
            return False
        self.store.save_order(
            {
                "client_order_id": str(
                    item.get("algoClOrdId")
                    or item.get("clOrdId")
                    or f"algo-{algo_id}"
                ),
                "exchange_order_id": algo_id,
                "status": str(item.get("state") or "unknown"),
                "inst_id": str(item.get("instId") or ""),
                "side": str(
                    item.get("actualSide")
                    or item.get("side")
                    or "unknown"
                ),
                "pos_side": str(item.get("posSide") or "net"),
                "ord_type": str(item.get("ordType") or "conditional"),
                "td_mode": str(item.get("tdMode") or "cross"),
                "size": _number(item.get("actualSz") or item.get("sz")),
                "price": _number(item.get("actualPx") or item.get("px")) or None,
                "reduce_only": str(item.get("reduceOnly", "")).lower() == "true",
                "stop_loss": _number(item.get("slTriggerPx")) or None,
                "take_profit": _number(item.get("tpTriggerPx")) or None,
                "source": "okx-algo-stream",
                "raw": item,
                "created_at": _timestamp(item.get("cTime")),
                "updated_at": _timestamp(item.get("uTime")),
            }
        )
        return True

    def sync_stream(self) -> dict[str, int]:
        snapshot = self.account_stream.snapshot()
        # A disconnected private stream may still contain the last valid
        # event. Never re-apply that stale cache as current account state.
        if snapshot.get("configured") is not None and not (
            snapshot.get("connected") and snapshot.get("authenticated")
        ):
            return {
                "positions": 0,
                "orders": 0,
                "fills": 0,
                "skipped": "private_stream_not_ready",
            }
        positions = 0
        orders = 0
        for item in snapshot.get("positions", []):
            size = _number(item.get("pos"))
            pos_side = str(item.get("posSide", "net"))
            stop_loss, take_profit = self._local_protection(
                str(item.get("instId", "")),
                pos_side,
                size,
            )
            self.store.upsert_position(
                {
                    "position_key": ":".join(
                        (
                            str(item.get("instId", "")),
                            str(item.get("posSide", "net")),
                            str(item.get("mgnMode", "")),
                        )
                    ),
                    "inst_id": str(item.get("instId", "")),
                    "pos_side": pos_side,
                    "size": size,
                    "entry_price": _number(item.get("avgPx") or item.get("openAvgPx")),
                    "mark_price": _number(item.get("markPx")),
                    "notional": _number(
                        item.get("notionalUsd")
                        or item.get("notional")
                        or item.get("notionalCcy")
                    ),
                    "unrealized_pnl": _number(item.get("upl")),
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "status": "open" if abs(size) > 0 else "closed",
                }
            )
            positions += 1
        for item in snapshot.get("orders", []):
            order_id = str(item.get("ordId") or "")
            if not order_id:
                continue
            client_id = str(item.get("clOrdId") or f"okx-{order_id}")
            self.store.save_order(
                {
                    "client_order_id": client_id,
                    "exchange_order_id": order_id,
                    "status": str(item.get("state") or "unknown"),
                    "inst_id": str(item.get("instId") or ""),
                    "side": str(item.get("side") or "unknown"),
                    "pos_side": str(item.get("posSide") or "net"),
                    "ord_type": str(item.get("ordType") or "unknown"),
                    "td_mode": str(item.get("tdMode") or "cross"),
                    "size": _number(item.get("sz")),
                    "price": _number(item.get("px")) or None,
                    "reduce_only": str(item.get("reduceOnly", "")).lower() == "true",
                    "source": "okx-account-stream",
                    "raw": item,
                    "created_at": _timestamp(item.get("cTime")),
                    "updated_at": _timestamp(item.get("uTime")),
                }
            )
            orders += 1
        algo_orders = 0
        if self.algo_stream is not None:
            algo_snapshot = self.algo_stream.snapshot()
            if algo_snapshot.get("connected") and algo_snapshot.get("authenticated"):
                for item in algo_snapshot.get("orders", []):
                    if self._save_algo_order(item):
                        orders += 1
                        algo_orders += 1
        return {
            "positions": positions,
            "orders": orders,
            "algo_orders": algo_orders,
            "fills": 0,
        }

    async def sync_rest(self) -> dict[str, Any]:
        if not self.account_client.configured:
            return {"positions": 0, "orders": 0, "fills": 0}
        positions_result, pending_result, fills_result = await asyncio.gather(
            self.account_client.positions(),
            self.account_client.pending_orders(),
            self.account_client.fills_history(),
            return_exceptions=True,
        )
        errors: list[str] = []

        def _result(
            name: str,
            result: list[dict[str, Any]] | BaseException,
        ) -> list[dict[str, Any]]:
            if not isinstance(result, BaseException):
                return result
            errors.append(name)
            self.store.add_audit(
                "account_endpoint_sync_failed",
                f"OKX {name} endpoint unavailable; other account data was retained",
                severity="warning",
                payload={"endpoint": name, "error": type(result).__name__},
            )
            return []

        positions = _result("positions", positions_result)
        pending_orders = _result("pending_orders", pending_result)
        fills = _result("fills_history", fills_result)
        history_loader = getattr(self.account_client, "orders_history", None)
        history_orders: list[dict[str, Any]] = []
        if history_loader is not None:
            try:
                history_orders = await history_loader(limit=100)
            except OkxAccountError as exc:
                self.store.add_audit(
                    "order_history_sync_failed",
                    "OKX order history unavailable; pending orders were still synchronized",
                    severity="warning",
                    payload={"error": type(exc).__name__},
                )
        algo_pending_loader = getattr(
            self.account_client,
            "pending_algo_orders",
            None,
        )
        algo_history_loader = getattr(
            self.account_client,
            "algo_orders_history",
            None,
        )
        algo_pending: list[dict[str, Any]] = []
        algo_history: list[dict[str, Any]] = []
        if algo_pending_loader is not None:
            try:
                algo_pending = await algo_pending_loader(limit=100)
            except OkxAccountError as exc:
                errors.append("algo_pending")
                self.store.add_audit(
                    "algo_order_sync_failed",
                    "OKX native protection pending orders unavailable",
                    severity="warning",
                    payload={"endpoint": "orders-algo-pending", "error": type(exc).__name__},
                )
        if algo_history_loader is not None:
            try:
                algo_history = await algo_history_loader(limit=100)
            except OkxAccountError as exc:
                errors.append("algo_history")
                self.store.add_audit(
                    "algo_order_sync_failed",
                    "OKX native protection order history unavailable",
                    severity="warning",
                    payload={"endpoint": "orders-algo-history", "error": type(exc).__name__},
                )
        seen_position_keys: set[str] = set()
        for item in positions:
            size = _number(item.get("pos"))
            pos_side = str(item.get("posSide", "net"))
            stop_loss, take_profit = self._local_protection(
                str(item.get("instId", "")),
                pos_side,
                size,
            )
            position_key = ":".join(
                (
                    str(item.get("instId", "")),
                    pos_side,
                    str(item.get("mgnMode", "")),
                )
            )
            seen_position_keys.add(position_key)
            self.store.upsert_position(
                {
                    "position_key": position_key,
                    "inst_id": str(item.get("instId", "")),
                    "pos_side": pos_side,
                    "size": size,
                    "entry_price": _number(item.get("avgPx") or item.get("openAvgPx")),
                    "mark_price": _number(item.get("markPx")),
                    "notional": _number(
                        item.get("notionalUsd")
                        or item.get("notional")
                        or item.get("notionalCcy")
                    ),
                    "stop_loss": stop_loss,
                    "take_profit": take_profit,
                    "unrealized_pnl": _number(item.get("upl")),
                    "status": "open" if abs(size) > 0 else "closed",
                }
            )
        if "positions" not in errors:
            self.store.close_positions_not_seen(seen_position_keys)
        orders = [*pending_orders, *history_orders]
        seen_order_ids: set[str] = set()
        for item in orders:
            order_id = str(item.get("ordId") or "")
            if not order_id or order_id in seen_order_ids:
                continue
            seen_order_ids.add(order_id)
            self.store.save_order(
                {
                    "client_order_id": str(item.get("clOrdId") or f"okx-{order_id}"),
                    "exchange_order_id": order_id,
                    "status": str(item.get("state") or "unknown"),
                    "inst_id": str(item.get("instId") or ""),
                    "side": str(item.get("side") or "unknown"),
                    "pos_side": str(item.get("posSide") or "net"),
                    "ord_type": str(item.get("ordType") or "unknown"),
                    "td_mode": str(item.get("tdMode") or "cross"),
                    "size": _number(item.get("sz")),
                    "price": _number(item.get("px")) or None,
                    "reduce_only": str(item.get("reduceOnly", "")).lower() == "true",
                    "source": "okx-rest-pending",
                    "raw": item,
                    "created_at": _timestamp(item.get("cTime")),
                    "updated_at": _timestamp(item.get("uTime")),
                }
            )
        for item in fills:
            trade_id = _text(item.get("tradeId") or item.get("execId"))
            if not trade_id:
                continue
            fill_price = _number(item.get("fillPx"))
            fill_size = _number(item.get("fillSz"))
            if fill_price <= 0 or fill_size <= 0:
                continue
            self.store.save_fill(
                {
                    "trade_id": trade_id,
                    "exchange_order_id": _text(item.get("ordId")) or None,
                    "client_order_id": _text(item.get("clOrdId")) or None,
                    "inst_id": _text(item.get("instId")),
                    "side": _text(item.get("side"), "unknown"),
                    "pos_side": _text(item.get("posSide"), "net"),
                    "fill_price": fill_price,
                    "fill_size": fill_size,
                    "fee": _number(item.get("fee")),
                    "fee_ccy": _text(item.get("feeCcy")) or None,
                    "realized_pnl": _number(item.get("fillPnl")),
                    "filled_at": _timestamp(item.get("ts")),
                    "raw": item,
                }
            )
        algo_orders = 0
        for item in [*algo_pending, *algo_history]:
            if self._save_algo_order(item):
                algo_orders += 1
        result: dict[str, Any] = {
            "positions": len(positions),
            "orders": len(seen_order_ids),
            "algo_orders": algo_orders,
            "fills": len(fills),
        }
        if errors:
            result["errors"] = errors
        return result
