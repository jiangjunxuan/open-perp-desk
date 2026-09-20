import asyncio
import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from .okx_account import OkxAccountClient, OkxAccountError
from .okx_algo_stream import OkxAlgoOrderStream
from .okx_account_stream import OkxAccountStream
from .account_ledger import AccountLedgerError, parse_daily_bills
from .position_protection import attached_algo_client_id, linked_protection
from .protection_incident import expected_protection
from .position_lots import PositionLotReconciler
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


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _position_key(item: dict[str, Any]) -> str:
    return ":".join((
        str(item.get("instId", "")),
        str(item.get("posSide", "net")),
        str(item.get("mgnMode", "")),
    ))


def _trade_id(value: Any) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value) else None


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
        self._stream_position_tokens: dict[str, tuple[Any, str]] = {}
        self._position_versions: dict[str, int] = {}
        self._position_times: dict[str, int] = {}
        self._rest_lock = asyncio.Lock()

    def private_stream_status(self) -> dict[str, Any]:
        """Return a secret-free readiness view for both authenticated streams."""
        account_configured = getattr(self.account_stream, "configured", False) is True
        account_connected = getattr(self.account_stream, "connected", False) is True
        account_authenticated = getattr(self.account_stream, "authenticated", False) is True
        algo_configured = getattr(self.algo_stream, "configured", False) is True
        algo_connected = getattr(self.algo_stream, "connected", False) is True
        algo_authenticated = getattr(self.algo_stream, "authenticated", False) is True
        configured = account_configured and algo_configured
        ready = bool(
            configured
            and account_connected
            and account_authenticated
            and algo_connected
            and algo_authenticated
        )
        if ready:
            reason_code = None
        elif not configured:
            reason_code = "private_stream_not_configured"
        elif not account_connected or not account_authenticated:
            reason_code = "account_stream_not_ready"
        elif not algo_connected or not algo_authenticated:
            reason_code = "algo_stream_not_ready"
        else:
            reason_code = "private_stream_not_ready"
        return {
            "configured": configured,
            "ready": ready,
            "account_connected": account_connected,
            "account_authenticated": account_authenticated,
            "algo_connected": algo_connected,
            "algo_authenticated": algo_authenticated,
            "reason_code": reason_code,
        }

    def private_stream_ready(self) -> bool:
        return bool(self.private_stream_status()["ready"])

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
        current, _ = self.store.save_exchange_order({
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
        self._record_attached_protection_failure(current, json.loads(current["raw_json"]))
        return True

    def _record_attached_protection_failure(
        self, previous: dict[str, Any] | None, item: dict[str, Any],
    ) -> None:
        """Persist explicit OKX attached-protection failures before they disappear."""
        if (
            not previous
            or previous["source"].startswith("okx-")
            or not (previous.get("stop_loss") or previous.get("take_profit"))
            or item.get("state") not in {"partially_filled", "filled", "canceled", "mmp_canceled"}
            or "attachAlgoOrds" not in item
        ):
            return
        attached = item.get("attachAlgoOrds")
        code, detail = "", ""
        if not isinstance(attached, list) or len(attached) != 1 or not isinstance(attached[0], dict):
            code, detail = "attached_protection_missing", "OKX order snapshot did not contain one attached protection record."
        else:
            terms = attached[0]
            raw_code = terms.get("failCode")
            if raw_code not in (None, "", "0", 0):
                code = f"attached_protection_{raw_code}"
                detail = _text(terms.get("failReason") or terms.get("failMsg"), "OKX rejected attached protection.")
            elif terms.get("attachAlgoClOrdId") != attached_algo_client_id(previous["client_order_id"]):
                code, detail = "attached_protection_identity_mismatch", "Attached protection client ID does not match the opening order."
        if not code:
            return
        account_scope = previous.get("account_scope") or getattr(self.account_client, "account_scope", None)
        if not account_scope:
            return
        record = self.store.record_protection_incident({
            "account_scope": account_scope,
            "inst_id": previous["inst_id"],
            "position_key": f"{previous['inst_id']}:{previous['pos_side']}:{previous['td_mode']}",
            "opening_order_id": previous["client_order_id"],
            "exchange_order_id": previous.get("exchange_order_id") or _text(item.get("ordId")) or None,
            "expected_protection": expected_protection(previous),
            "failure_code": code,
            "failure_detail": detail,
        })
        if not record["changed"]:
            return
        self.store.add_audit(
            "attached_protection_failed",
            "OKX attached protection creation failed",
            severity="error",
            payload={
                "incident_id": record["incident_id"], "inst_id": previous["inst_id"],
                "opening_order_id": previous["client_order_id"], "failure_code": code,
            },
        )
        self._schedule_notification(
            "attached_protection_failed",
            "OpenPerpDesk 附带保护创建失败",
            f"{previous['inst_id']} 开仓 {previous['client_order_id']} 的止盈止损未能确认，已暂停新的开仓并要求人工复核。",
            severity="error",
            payload={"incident_id": record["incident_id"], "failure_code": code},
        )

    def _local_protection(
        self,
        inst_id: str,
        pos_side: str,
        size: float,
        *,
        td_mode: str,
        account_scope: str | None,
        trade_id: str | None,
    ) -> tuple[float | None, float | None, str | None]:
        empty = (None, None, None)
        if account_scope != getattr(self.account_client, "account_scope", None):
            return empty
        evidence = linked_protection(
            self.store, inst_id, pos_side, size, td_mode=td_mode,
            account_scope=account_scope, trade_id=trade_id,
        )
        return (
            evidence["stop_loss"], evidence["take_profit"], evidence["opening_order_id"],
        ) if evidence else empty

    def _protection_unlinked(self, previous: dict[str, Any] | None, owner: str | None, size: float) -> None:
        if not previous or owner or not size or not (previous["stop_loss"] or previous["take_profit"]):
            return
        payload = {"inst_id": previous["inst_id"], "reason": "position_protection_unverified"}
        self.store.add_audit(
            "position_protection_unverified",
            "Current position trade or native protection terms could not be verified",
            severity="warning", payload=payload,
        )
        self._schedule_notification(
            "position_protection_unverified", "OpenPerpDesk 本地保护关联待核对",
            f"{previous['inst_id']} 的成交关联或当前原生保护参数无法核实，已暂停该持仓本地兜底；请核对交易所原生保护单。",
            severity="warning", payload=payload,
        )

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
        if not math.isfinite(size) or size <= 0:
            size = _number(item.get("actualSz"))
        if not math.isfinite(size) or size < 0:
            size = 0.0
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
        account_scope = getattr(self.account_client, "account_scope", None)
        owner = next((
            row for row in self.store.managed_opening_orders(account_scope, str(item.get("instId") or ""))
            if attached_algo_client_id(row["client_order_id"]) == client_order_id
        ), None) if account_scope else None
        if owner:
            self.store.supersede_protection_incident(
                account_scope, owner["client_order_id"],
                reason="A matching native protection order was observed.",
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

    def _save_position(self, item: dict[str, Any]) -> bool:
        key = _position_key(item)
        timestamp = _exchange_ms(item.get("uTime"))
        if timestamp is not None and timestamp < self._position_times.get(key, 0):
            return False
        size = _number(item.get("pos"))
        pos_side = str(item.get("posSide", "net"))
        td_mode = str(item.get("mgnMode", ""))
        scope = getattr(self.account_client, "account_scope", None)
        trade_id = _trade_id(item.get("tradeId"))
        stop_loss, take_profit, owner = self._local_protection(
            str(item.get("instId", "")), pos_side, size,
            td_mode=td_mode, account_scope=scope, trade_id=trade_id,
        )
        if owner and scope:
            self.store.supersede_protection_incident(
                scope, owner,
                reason="A matching native protection order was observed.",
            )
        previous = self.store.get_position(key)
        self.store.upsert_position({
            "position_key": key,
            "inst_id": str(item.get("instId", "")),
            "pos_side": pos_side,
            "td_mode": td_mode,
            "account_scope": scope,
            "exchange_trade_id": trade_id,
            "protection_order_id": owner,
            "size": size,
            "entry_price": _number(item.get("avgPx") or item.get("openAvgPx")),
            "mark_price": _number(item.get("markPx")),
            "notional": _number(item.get("notionalUsd") or item.get("notional") or item.get("notionalCcy")),
            "unrealized_pnl": _number(item.get("upl")),
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "status": "open" if abs(size) > 0 else "closed",
        })
        self._protection_unlinked(previous, owner, size)
        if timestamp is not None:
            self._position_times[key] = timestamp
        return True

    def _refresh_position_protection(self, key: str) -> None:
        current = self.store.get_position(key)
        if current is None or current["status"] != "open":
            return
        stop_loss, take_profit, owner = self._local_protection(
            current["inst_id"], current["pos_side"], current["size"],
            td_mode=current["td_mode"], account_scope=current["account_scope"],
            trade_id=current["exchange_trade_id"],
        )
        if (stop_loss, take_profit, owner) != (
            current["stop_loss"], current["take_profit"], current["protection_order_id"],
        ):
            self.store.upsert_position({
                **current, "stop_loss": stop_loss, "take_profit": take_profit,
                "protection_order_id": owner,
                "updated_at": None,
            })
            self._protection_unlinked(current, owner, current["size"])

    def sync_stream(self) -> dict[str, Any]:
        snapshot = self.account_stream.snapshot()
        algo_orders = 0
        if self.algo_stream is not None:
            algo_snapshot = self.algo_stream.snapshot()
            if algo_snapshot.get("connected") and algo_snapshot.get("authenticated"):
                for item in algo_snapshot.get("orders", []):
                    if self._save_algo_order(item):
                        algo_orders += 1
        # A disconnected private stream may still contain the last valid
        # event. Never re-apply that stale cache as current account state.
        if snapshot.get("configured") is not None and not (
            snapshot.get("connected") and snapshot.get("authenticated")
        ):
            for position in self.store.list_positions():
                self._refresh_position_protection(position["position_key"])
            return {
                "positions": 0,
                "orders": algo_orders,
                "algo_orders": algo_orders,
                "fills": 0,
                "balances": 0,
                "skipped": "private_stream_not_ready",
            }
        positions = 0
        orders = algo_orders
        balances = 0
        if snapshot.get("balance"):
            try:
                exchange_times = [
                    _exchange_ms(item.get("uTime"))
                    for item in snapshot["balance"] if isinstance(item, dict)
                ]
                balances = int(self.store.save_equity_snapshot(
                    self.account_client.account_scope,
                    max((stamp for stamp in exchange_times if stamp is not None), default=_now_ms()),
                    "private_stream",
                    snapshot["balance"],
                ))
            except (TypeError, ValueError):
                self.store.add_audit(
                    "equity_snapshot_invalid",
                    "Private equity snapshot was not persisted",
                    severity="warning",
                    payload={"source": "private_stream"},
                )
        # Position protection depends on confirmed fills from this same snapshot.
        for item in snapshot.get("orders", []):
            if self._save_regular_order(item, source="okx-account-stream"):
                orders += 1
        for item in snapshot.get("positions", []):
            key = _position_key(item)
            token = (
                snapshot.get("position_versions", {}).get(key),
                json.dumps(item, sort_keys=True),
            )
            if self._stream_position_tokens.get(key) == token:
                self._refresh_position_protection(key)
                continue
            if self._save_position(item):
                self._position_versions[key] = self._position_versions.get(key, 0) + 1
                positions += 1
            self._stream_position_tokens[key] = token
        for position in self.store.list_positions():
            self._refresh_position_protection(position["position_key"])
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
        async with self._rest_lock:
            return await self._sync_rest()

    async def _sync_rest(self) -> dict[str, Any]:
        if not self.account_client.configured:
            return {"positions": 0, "orders": 0, "fills": 0}
        captured_at = datetime.now(timezone.utc)
        position_versions = dict(self._position_versions)

        async def load_bills() -> list[dict[str, Any]] | None:
            loader = getattr(self.account_client, "bills_today", None)
            return await loader(as_of=captured_at) if loader is not None else None

        balance_loader = getattr(self.account_client, "balance", None)
        account_scope = getattr(self.account_client, "account_scope", None)
        balance_result, positions_result, pending_result, fills_result, bills_result = await asyncio.gather(
            balance_loader() if callable(balance_loader) else asyncio.sleep(0, result=[]),
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

        balance_saved = 0
        if callable(balance_loader):
            balance = _result("balance", balance_result)
        else:
            balance = []
        if callable(balance_loader) and account_scope and "balance" not in errors:
            try:
                balance_saved = int(self.store.save_equity_snapshot(
                    account_scope, int(captured_at.timestamp() * 1000),
                    "rest_reconcile", balance,
                ))
            except ValueError:
                errors.append("balance")
                self.store.add_audit(
                    "equity_snapshot_invalid",
                    "REST equity snapshot was not persisted",
                    severity="warning",
                    payload={"source": "rest_reconcile"},
                )
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

        algo_orders = 0
        seen_algo_ids: set[str] = set()
        for item in [*algo_pending, *algo_history]:
            try:
                if self._save_algo_order(item, source="okx-algo-rest"):
                    algo_orders += 1
                    seen_algo_ids.add(str(item.get("algoId") or ""))
            except OrderSnapshotConflict:
                if "algo_identity" not in errors:
                    errors.append("algo_identity")
        algo_recovery = await self._recover_algo_orders(seen_algo_ids)
        if algo_recovery["unresolved"]:
            errors.append("algo_recovery")

        seen_position_keys: set[str] = set()
        for item in positions:
            position_key = _position_key(item)
            seen_position_keys.add(position_key)
            pushed_during_request = self._position_versions.get(position_key) != position_versions.get(position_key)
            rest_timestamp = _exchange_ms(item.get("uTime"))
            proven_newer = rest_timestamp is not None and rest_timestamp > self._position_times.get(position_key, rest_timestamp)
            if not pushed_during_request or proven_newer:
                self._save_position(item)
        if "positions" not in errors:
            # A REST response cannot roll back or close a position updated by a
            # push while this request was in flight.
            seen_position_keys.update(
                key for key, version in self._position_versions.items()
                if version != position_versions.get(key)
            )
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
        for position in self.store.list_positions():
            self._refresh_position_protection(position["position_key"])
        lot_results = []
        if getattr(self.account_client, "position_fill_pages", None) is not None:
            lot_reconciler = PositionLotReconciler(self.store, self.account_client, self._save_regular_order)
            for position in self.store.list_positions():
                lot_results.append(await lot_reconciler.reconcile(position))
        result: dict[str, Any] = {
            "positions": len(positions),
            "orders": len(seen_order_ids),
            "algo_orders": algo_orders,
            "fills": len(fills),
            "bills": bill_count,
            "balances": balance_saved,
            "recovery": recovery,
            "algo_recovery": algo_recovery,
            "position_lots": {
                "verified": sum(row["status"] == "verified" for row in lot_results),
                "unverified": sum(row["status"] != "verified" for row in lot_results),
            },
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

    async def _recover_algo_orders(self, seen_ids: set[str]) -> dict[str, int]:
        result = {"checked": 0, "recovered": 0, "unresolved": 0}
        loader = getattr(self.account_client, "algo_order_details", None)
        _, active = self.store.execution_snapshot()
        for order in active:
            if order["order_kind"] != "algo" or order.get("exchange_order_id") in seen_ids:
                continue
            result["checked"] += 1
            try:
                scope = getattr(self.account_client, "account_scope", None)
                if not loader or not scope or order.get("account_scope") != scope:
                    raise OrderSnapshotConflict("algo_account_scope_unverified")
                raw = json.loads(order["raw_json"])
                client_id = raw.get("algoClOrdId") or None
                item = await loader(
                    order["inst_id"], algo_id=order.get("exchange_order_id"),
                    client_order_id=client_id,
                )
                if (
                    not isinstance(item, dict) or item.get("instId") != order["inst_id"]
                    or item.get("algoId") != order["exchange_order_id"]
                    or (client_id and item.get("algoClOrdId") != client_id)
                    or not _exchange_ms(item.get("uTime"))
                    or item.get("state") not in {
                        "live", "pause", "partially_effective", "effective", "canceled",
                        "order_failed", "partially_failed",
                    }
                ):
                    raise OrderSnapshotConflict("algo_lookup_identity_mismatch")
                self._save_algo_order(item, source="okx-algo-recovery")
                result["recovered"] += 1
            except Exception as exc:
                result["unresolved"] += 1
                self.store.add_audit(
                    "algo_order_recovery_unresolved",
                    "Missing native protection was not assumed canceled",
                    severity="warning",
                    payload={"client_order_id": order["client_order_id"], "error": type(exc).__name__},
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
