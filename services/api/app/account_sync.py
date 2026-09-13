import asyncio
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from .okx_account import OkxAccountClient, OkxAccountError
from .okx_algo_stream import OkxAlgoOrderStream
from .okx_account_stream import OkxAccountStream
from .account_ledger import AccountLedgerError, parse_daily_bills
from .state_store import OrderSnapshotConflict, StateStore


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
    except (TypeError, ValueError, OSError, OverflowError):
        return raw


def _exchange_ms(value: Any) -> int | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return timestamp if timestamp > 0 else None


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
        notifier: Callable[..., Awaitable[bool]] | None = None,
    ) -> None:
        self.store = store
        self.account_client = account_client
        self.account_stream = account_stream
        self.algo_stream = algo_stream
        self.notifier = notifier

    @staticmethod
    def _validate_local_order(order: dict[str, Any], item: dict[str, Any]) -> None:
        if order["source"].startswith("okx-"):
            return
        fields = {
            "inst_id": "instId", "side": "side", "pos_side": "posSide",
            "ord_type": "ordType", "td_mode": "tdMode",
        }
        if any(str(order[local]) != str(item.get(remote)) for local, remote in fields.items()):
            raise OrderSnapshotConflict("order_payload_mismatch")
        try:
            size = Decimal(str(item.get("sz")))
            if not size.is_finite() or size != Decimal(str(order["size"])):
                raise OrderSnapshotConflict("order_size_mismatch")
        except InvalidOperation as exc:
            raise OrderSnapshotConflict("order_size_invalid") from exc
        if item.get("state") not in {"live", "partially_filled", "filled", "canceled", "mmp_canceled"}:
            raise OrderSnapshotConflict("order_state_unconfirmed")
        if not order.get("exchange_order_id"):
            try:
                created = datetime.fromisoformat(order["created_at"])
                if created.tzinfo is None:
                    raise ValueError("missing timezone")
                exchange_created = _exchange_ms(item.get("cTime"))
                delta = exchange_created - int(created.timestamp() * 1000) if exchange_created else None
            except (ValueError, TypeError, OverflowError) as exc:
                raise OrderSnapshotConflict("order_creation_time_unverified") from exc
            if delta is None or not -10000 <= delta <= 120000:
                raise OrderSnapshotConflict("order_creation_time_mismatch")

    def _save_regular_order(self, item: dict[str, Any], *, source: str) -> bool:
        order_id = _text(item.get("ordId"))
        if not order_id:
            return False
        client_id = _text(item.get("clOrdId"), f"okx-{order_id}")
        previous = self.store.get_order(client_id)
        if previous:
            scope = getattr(self.account_client, "account_scope", None)
            if scope and not previous["source"].startswith("okx-") and previous.get("account_scope") != scope:
                raise OrderSnapshotConflict("order_account_scope_unverified")
            self._validate_local_order(previous, item)
        side, pos_side = _text(item.get("side"), "unknown"), _text(item.get("posSide"), "net")
        self.store.save_exchange_order({
            "client_order_id": client_id,
            "exchange_order_id": order_id,
            "status": _text(item.get("state"), "unknown"),
            "inst_id": _text(item.get("instId")),
            "side": side,
            "pos_side": pos_side,
            "ord_type": _text(item.get("ordType"), "unknown"),
            "td_mode": _text(item.get("tdMode"), "cross"),
            "size": _number(item.get("sz")),
            "price": _number(item.get("px")) or None,
            "reduce_only": str(item.get("reduceOnly", "")).lower() == "true"
                or (pos_side, side) in {("long", "sell"), ("short", "buy")},
            "source": source,
            "order_kind": "standard",
            "account_scope": getattr(self.account_client, "account_scope", None),
            "raw": item,
            "created_at": _timestamp(item.get("cTime")),
            "updated_at": _timestamp(item.get("uTime")),
            "exchange_updated_ms": _exchange_ms(item.get("uTime")),
        })
        return True

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

    def _save_algo_order(self, item: dict[str, Any], *, source: str = "okx-algo-stream") -> bool:
        algo_id = str(
            item.get("algoId")
            or item.get("algoClOrdId")
            or ""
        )
        if not algo_id:
            return False
        client_order_id = str(
            item.get("algoClOrdId")
            or f"algo-{algo_id}"
        )
        status = str(item.get("state") or "unknown")
        previous = self.store.get_order(client_order_id)
        side, pos_side = _text(item.get("side"), "unknown"), _text(item.get("posSide"), "net")
        size = _number(item.get("sz"))
        if size <= 0:
            size = _number(item.get("actualSz"))
        saved, applied = self.store.save_exchange_order(
            {
                "client_order_id": client_order_id,
                "exchange_order_id": algo_id,
                "status": status,
                "inst_id": str(item.get("instId") or ""),
                "side": side,
                "pos_side": pos_side,
                "ord_type": str(item.get("ordType") or "conditional"),
                "td_mode": str(item.get("tdMode") or "cross"),
                "size": size,
                "price": _number(item.get("actualPx") or item.get("px")) or None,
                "reduce_only": str(item.get("reduceOnly", "")).lower() == "true"
                    or (pos_side, side) in {("long", "sell"), ("short", "buy")},
                "stop_loss": _number(item.get("slTriggerPx")) or None,
                "take_profit": _number(item.get("tpTriggerPx")) or None,
                "source": source,
                "order_kind": "algo",
                "account_scope": getattr(self.account_client, "account_scope", None),
                "raw": item,
                "created_at": _timestamp(item.get("cTime")),
                "updated_at": _timestamp(item.get("uTime")),
                "exchange_updated_ms": _exchange_ms(item.get("uTime")),
            }
        )
        terminal_states = {
            "effective",
            "triggered",
            "filled",
            "canceled",
            "cancelled",
            "failed",
            "order_failed",
            "expired",
        }
        if applied and saved["status"].lower() in terminal_states and (
            previous is None or str(previous.get("status")) != status
        ):
            self._schedule_notification(
                "algo_order_state",
                "OpenPerpDesk 止盈止损状态更新",
                (
                    f"{item.get('instId') or '未知合约'} 原生保护单状态："
                    f"{status}，算法单号 {algo_id}。"
                ),
                payload={
                    "algo_id": algo_id,
                    "inst_id": item.get("instId"),
                    "status": status,
                },
                severity="warning" if status.lower() in {"failed", "order_failed"} else "info",
            )
        return True

    def _save_fill(
        self,
        item: dict[str, Any],
        *,
        update_existing: bool = True,
    ) -> tuple[dict[str, Any] | None, bool]:
        trade_id = _text(item.get("tradeId") or item.get("execId"))
        if not trade_id:
            return None, False
        fill_price = _number(item.get("fillPx"))
        fill_size = _number(item.get("fillSz"))
        if (
            not math.isfinite(fill_price)
            or not math.isfinite(fill_size)
            or fill_price <= 0
            or fill_size <= 0
        ):
            return None, False
        record = {
            "trade_id": trade_id,
            "exchange_order_id": _text(item.get("ordId")) or None,
            "client_order_id": _text(item.get("clOrdId")) or None,
            "inst_id": _text(item.get("instId")),
            "side": _text(item.get("side"), "unknown"),
            "pos_side": _text(item.get("posSide"), "net"),
            "fill_price": fill_price,
            "fill_size": fill_size,
            "fee": _number(item.get("fillFee") or item.get("fee")),
            "fee_ccy": _text(item.get("fillFeeCcy") or item.get("feeCcy")) or None,
            "realized_pnl": _number(item.get("fillPnl")),
            "filled_at": _timestamp(
                item.get("fillTime") or item.get("ts") or item.get("uTime")
            ),
            "raw": item,
        }
        is_new_fill = self.store.get_fill(trade_id) is None
        if is_new_fill or update_existing:
            self.store.save_fill(record)
        return record, is_new_fill

    def _schedule_notification(
        self,
        event_type: str,
        title: str,
        content: str,
        *,
        payload: dict[str, Any] | None = None,
        severity: str = "info",
    ) -> None:
        if self.notifier is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(
            self._notify_event(
                event_type,
                title,
                content,
                payload=payload,
                severity=severity,
            )
        )

    async def _notify_event(
        self,
        event_type: str,
        title: str,
        content: str,
        *,
        payload: dict[str, Any] | None = None,
        severity: str = "info",
    ) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(
                event_type,
                title,
                content,
                payload=payload,
                severity=severity,
            )
        except Exception as exc:
            self.store.add_audit(
                "notification_dispatch_failed",
                "Notification dispatcher raised unexpectedly",
                severity="warning",
                payload={"event_type": event_type, "error": type(exc).__name__},
            )

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
        # Position protection depends on confirmed fills from this same snapshot.
        for item in snapshot.get("orders", []):
            if self._save_regular_order(item, source="okx-account-stream"):
                orders += 1
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
        algo_orders = 0
        if self.algo_stream is not None:
            algo_snapshot = self.algo_stream.snapshot()
            if algo_snapshot.get("connected") and algo_snapshot.get("authenticated"):
                for item in algo_snapshot.get("orders", []):
                    if self._save_algo_order(item):
                        orders += 1
                        algo_orders += 1
        stream_fills = 0
        for item in snapshot.get("fills", []):
            record, is_new_fill = self._save_fill(item, update_existing=False)
            if record is None:
                continue
            stream_fills += 1
            if is_new_fill:
                self._schedule_notification(
                    "fill_received",
                    "OpenPerpDesk 收到成交回报",
                    (
                        f"{record['inst_id'] or '未知合约'} {record['side']} "
                        f"{record['fill_size']} @ {record['fill_price']}，"
                        f"已实现 PnL {record['realized_pnl']}。"
                    ),
                    payload={
                        "trade_id": record["trade_id"],
                        "inst_id": record["inst_id"],
                        "side": record["side"],
                        "fill_size": record["fill_size"],
                        "fill_price": record["fill_price"],
                        "realized_pnl": record["realized_pnl"],
                    },
                )
        return {
            "positions": positions,
            "orders": orders,
            "algo_orders": algo_orders,
            "fills": stream_fills,
        }

    async def sync_rest(self) -> dict[str, Any]:
        if not self.account_client.configured:
            return {"positions": 0, "orders": 0, "fills": 0}
        captured_at = datetime.now(timezone.utc)

        async def load_bills() -> list[dict[str, Any]] | None:
            loader = getattr(self.account_client, "bills_today", None)
            return await loader(as_of=captured_at) if loader is not None else None

        positions_result, pending_result, fills_result, bills_result = await asyncio.gather(
            self.account_client.positions(),
            self.account_client.pending_orders(),
            self.account_client.fills_history(),
            load_bills(),
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
        bill_count = 0
        if bills_result is not None:
            bills = _result("bills", bills_result)
            if "bills" not in errors:
                try:
                    parsed, summary = parse_daily_bills(bills, captured_at)
                    self.store.save_bill_snapshot(self.account_client.account_scope, parsed, summary)
                    bill_count = len(parsed)
                except AccountLedgerError as exc:
                    errors.append("bills")
                    self.store.add_audit(
                        "account_ledger_invalid",
                        "Account bills could not be valued consistently",
                        severity="warning",
                        payload={"reason": str(exc)},
                    )
        history_loader = getattr(self.account_client, "orders_history", None)
        history_orders: list[dict[str, Any]] = []
        if history_loader is not None:
            try:
                history_orders = await history_loader(limit=100)
            except OkxAccountError as exc:
                errors.append("orders_history")
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
        orders = [*pending_orders, *history_orders]
        seen_order_ids: set[str] = set()
        for item in orders:
            order_id = str(item.get("ordId") or "")
            if not order_id:
                continue
            seen_order_ids.add(order_id)
            try:
                self._save_regular_order(item, source="okx-rest-orders")
            except OrderSnapshotConflict:
                if "order_identity" not in errors:
                    errors.append("order_identity")
        recovery = await self._recover_orders(seen_order_ids)
        if recovery["unresolved"]:
            errors.append("order_recovery")

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
        for item in fills:
            record, is_new_fill = self._save_fill(item)
            if record is None:
                continue
            if is_new_fill:
                await self._notify_event(
                    "fill_received",
                    "OpenPerpDesk 收到成交回报",
                    (
                        f"{record['inst_id'] or '未知合约'} {record['side']} "
                        f"{record['fill_size']} @ {record['fill_price']}，"
                        f"已实现 PnL {record['realized_pnl']}。"
                    ),
                    payload={
                        "trade_id": record["trade_id"],
                        "inst_id": record["inst_id"],
                        "side": record["side"],
                        "fill_size": record["fill_size"],
                        "fill_price": record["fill_price"],
                        "realized_pnl": record["realized_pnl"],
                    },
                )
        algo_orders = 0
        for item in [*algo_pending, *algo_history]:
            if self._save_algo_order(item, source="okx-algo-rest"):
                algo_orders += 1
        result: dict[str, Any] = {
            "positions": len(positions),
            "orders": len(seen_order_ids),
            "algo_orders": algo_orders,
            "fills": len(fills),
            "bills": bill_count,
            "recovery": recovery,
        }
        if errors:
            result["errors"] = errors
            await self._notify_event(
                "account_sync_failed",
                "OpenPerpDesk 账户同步异常",
                f"OKX 账户同步部分失败：{', '.join(errors)}。",
                payload={"errors": errors},
                severity="warning",
            )
        return result

    async def _recover_orders(self, seen_order_ids: set[str]) -> dict[str, int]:
        loader = getattr(self.account_client, "order_details", None)
        result = {"checked": 0, "recovered": 0, "unresolved": 0}
        if loader is None:
            return result
        scope = getattr(self.account_client, "account_scope", None)
        _, active_orders = self.store.execution_snapshot()
        for order in active_orders:
            if order.get("order_kind") == "algo" or order.get("exchange_order_id") in seen_order_ids:
                continue
            result["checked"] += 1
            try:
                if not scope or order.get("account_scope") != scope:
                    raise OrderSnapshotConflict("order_account_scope_unverified")
                raw = json.loads(order["raw_json"])
                remote_client_id = raw.get("clOrdId") if order["source"].startswith("okx-") else order["client_order_id"]
                item = await loader(
                    order["inst_id"],
                    ord_id=order.get("exchange_order_id"),
                    client_order_id=remote_client_id or None,
                )
                if item.get("instId") != order["inst_id"] or (
                    order.get("exchange_order_id") and item.get("ordId") != order["exchange_order_id"]
                ) or (remote_client_id and item.get("clOrdId") != remote_client_id):
                    raise OrderSnapshotConflict("order_lookup_identity_mismatch")
                if item.get("state") not in {"live", "partially_filled", "filled", "canceled", "mmp_canceled"}:
                    raise OrderSnapshotConflict("order_state_unconfirmed")
                if not _exchange_ms(item.get("uTime")):
                    raise OrderSnapshotConflict("order_update_time_unverified")
                self._validate_local_order(order, item)
                self._save_regular_order(item, source="okx-rest-recovery")
                recovered = self.store.get_order(order["client_order_id"])
                if recovered is None or recovered["status"] not in {"live", "partially_filled", "filled", "canceled", "mmp_canceled"}:
                    raise OrderSnapshotConflict("order_snapshot_not_applied")
                result["recovered"] += 1
            except (OkxAccountError, OrderSnapshotConflict, ValueError, TypeError, TimeoutError) as exc:
                result["unresolved"] += 1
                self.store.add_audit(
                    "order_recovery_unconfirmed",
                    "Exact order lookup could not confirm the durable order; no retry was sent",
                    severity="warning",
                    payload={
                        "client_order_id": order["client_order_id"],
                        "error": type(exc).__name__,
                        "code": getattr(exc, "code", None),
                    },
                )
        return result
