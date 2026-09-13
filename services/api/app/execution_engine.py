import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from .okx_trade import OkxOrderRejected, OkxTradeClient, OrderRequest
from .order_preflight import OrderPreflight, PreflightError, PreparedExecution
from .pushplus import PushPlusClient, PushPlusError
from .risk_engine import RiskEngine
from .safety_control import SafetyController
from .state_store import ExposureSnapshotChanged, StateStore
from .trading_signal import TradeSignal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ExecutionEngine:
    """Risk-gated, idempotent signal executor for OKX Demo."""

    def __init__(
        self,
        store: StateStore,
        risk_engine: RiskEngine,
        trade_client: OkxTradeClient,
        pushplus: PushPlusClient,
        safety: SafetyController | None = None,
        preflight: OrderPreflight | None = None,
    ) -> None:
        self.store = store
        self.risk_engine = risk_engine
        self.trade_client = trade_client
        self.pushplus = pushplus
        self.safety = safety or SafetyController(store)
        self.preflight = preflight

    @staticmethod
    def client_order_id(
        signal: TradeSignal,
        idempotency_key: str | None = None,
    ) -> str:
        if idempotency_key:
            digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]
            return f"opd{digest}"
        digest = hashlib.sha256(
            json.dumps(signal.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()[:20]
        return f"opd{digest}"

    @staticmethod
    def _existing_result(
        existing: dict[str, Any],
        *,
        dry_run: bool,
        size: float,
        side: str,
    ) -> dict[str, Any]:
        status = existing["status"]
        conflict = existing["size"] != size or existing["side"] != side
        confirmed = status in (
            {"preview"} if dry_run else {"submitted", "live", "partially_filled", "filled", "canceled"}
        )
        reason = (
            "idempotency_payload_conflict" if conflict
            else "order_submission_unconfirmed" if status in {"preparing", "submitting", "submission_unknown"}
            else "previous_order_not_accepted"
        )
        return {
            "accepted": confirmed and not conflict,
            "idempotent": True,
            "dry_run": dry_run,
            "order": existing,
            "reasons": [] if confirmed and not conflict else [reason],
        }

    async def submit_signal(
        self,
        signal: TradeSignal,
        *,
        account_equity: float,
        daily_pnl_pct: float,
        current_notional: float = 0.0,
        size: float = 1.0,
        dry_run: bool = False,
        side_override: Literal["buy", "sell"] | None = None,
        idempotency_key: str | None = None,
        market_data_fresh: bool = True,
    ) -> dict[str, Any]:
        if not self.safety.execution_allowed:
            self.store.add_audit(
                "emergency_stop_rejected",
                "Order rejected because emergency stop is active",
                severity="warning",
            )
            await self.notify_event(
                "emergency_stop_rejected",
                "OpenPerpDesk 拒绝订单",
                f"{signal.inst_id} 信号被急停闸门拦截。",
                payload={"inst_id": signal.inst_id, "action": signal.action},
                severity="warning",
            )
            return {
                "accepted": False,
                "idempotent": False,
                "reasons": ["emergency_stop_active"],
            }
        side = side_override or ("buy" if signal.action == "open_long" else "sell")
        client_order_id = self.client_order_id(signal, idempotency_key)
        legacy_order_id = f"opd-{client_order_id[3:]}"
        if dry_run:
            preview_key = json.dumps(
                [client_order_id, size, side, signal.stop_loss, signal.take_profit],
                allow_nan=False,
            )
            client_order_id = "opdp" + hashlib.sha256(preview_key.encode()).hexdigest()[:20]
        existing = self.store.get_order(client_order_id)
        if not dry_run and existing is None:
            legacy = self.store.get_order(legacy_order_id)
            if legacy and legacy["status"] != "preview":
                existing = legacy
        if existing:
            replay_side = existing["side"] if signal.action == "close" and side_override is None else side
            return self._existing_result(existing, dry_run=dry_run, size=size, side=replay_side)

        prepared: PreparedExecution | None = None
        if not dry_run or (self.preflight and self.preflight.configured):
            if self.preflight is None:
                return await self._preflight_rejection(signal, "exchange_preflight_required")
            if not dry_run and not self.trade_client.enabled:
                return await self._preflight_rejection(signal, "execution_disabled")
            try:
                prepared = await self.preflight.prepare(signal, size, side_override)
            except PreflightError as exc:
                return await self._preflight_rejection(signal, str(exc))
            signal = prepared.signal
            account_equity = prepared.account_equity
            daily_pnl_pct = prepared.daily_pnl_pct
            current_notional = prepared.current_notional
            side = prepared.side
            age = datetime.now(timezone.utc).timestamp() - prepared.market_timestamp
            market_data_fresh = market_data_fresh and -5 <= age <= 30
        if not self.safety.execution_allowed:
            return await self._preflight_rejection(signal, "emergency_stop_active")
        decision = self.risk_engine.evaluate(
            signal,
            account_equity=account_equity,
            daily_pnl_pct=daily_pnl_pct,
            current_notional=current_notional,
            market_data_fresh=market_data_fresh,
            order_notional=prepared.order_notional if prepared else None,
            verified_close=bool(prepared and prepared.verified_close),
        )
        if not decision.approved:
            self.store.add_audit(
                "risk_rejected",
                "Signal rejected by risk engine",
                severity="warning",
                payload={"signal": decision.signal, "reasons": decision.reasons},
            )
            await self.notify_event(
                "risk_rejected",
                "OpenPerpDesk 风控拒绝",
                f"{signal.inst_id} {signal.action} 未通过风控：{', '.join(decision.reasons)}",
                payload={"inst_id": signal.inst_id, "reasons": decision.reasons},
                severity="warning",
            )
            return {
                "accepted": False,
                "idempotent": False,
                "reasons": list(decision.reasons),
                "risk": decision.signal,
            }

        reduce_only = signal.action == "close"
        order = OrderRequest(
            inst_id=signal.inst_id,
            side=side,
            pos_side=prepared.pos_side if prepared else "net",
            td_mode=prepared.td_mode if prepared else "isolated",
            ord_type="market",
            sz=size,
            reduce_only=reduce_only,
            stop_loss=signal.stop_loss if not reduce_only else None,
            take_profit=signal.take_profit if not reduce_only else None,
            cl_ord_id=client_order_id,
        )
        record = {
            "client_order_id": client_order_id,
            "status": "preview" if dry_run else "preparing",
            "inst_id": signal.inst_id,
            "side": side,
            "pos_side": order.pos_side,
            "ord_type": order.ord_type,
            "td_mode": order.td_mode,
            "size": size,
            "reduce_only": reduce_only,
            "stop_loss": signal.stop_loss,
            "take_profit": signal.take_profit,
            "source": signal.source,
            "raw": {
                "signal": signal.model_dump(mode="json"),
                "preflight": prepared.summary() if prepared else {"basis": "simulation"},
            },
            "risk_notional": prepared.order_notional if prepared and not reduce_only else 0.0,
            "account_scope": prepared.account_scope if prepared else None,
            "created_at": _now(),
        }
        try:
            claimed, saved = self.store.claim_order(
                record,
                expected_generation=prepared.generation if prepared and not dry_run else None,
            )
        except ExposureSnapshotChanged:
            return await self._preflight_rejection(signal, "execution_budget_snapshot_changed")
        if not claimed:
            return self._existing_result(saved, dry_run=dry_run, size=size, side=side)
        if dry_run:
            self.store.add_audit(
                "order_preview",
                "Order passed risk review in preview mode",
                payload=record,
            )
            return {
                "accepted": True,
                "idempotent": False,
                "dry_run": True,
                "order": saved,
                "risk": decision.signal,
                "preflight": record["raw"]["preflight"],
            }

        order_attempted = False
        try:
            if not reduce_only:
                await self.trade_client.set_leverage(
                    signal.inst_id, signal.leverage, order.td_mode, order.pos_side,
                )
            if not self.safety.execution_allowed:
                raise OkxOrderRejected("Emergency stop became active during preparation")
            if prepared and prepared.daily_accounting and (
                prepared.daily_accounting["day_utc"] != datetime.now(timezone.utc).date().isoformat()
            ):
                raise OkxOrderRejected("UTC accounting day changed during preparation")
            if signal.expires_at and signal.expires_at <= datetime.now(timezone.utc):
                raise OkxOrderRejected("Signal expired during preparation")
            if prepared and datetime.now(timezone.utc).timestamp() - prepared.market_timestamp > 30:
                raise OkxOrderRejected("Market snapshot expired during preparation")
            self.store.finalize_submission(
                client_order_id, "submitting", raw=record["raw"],
            )
            order_attempted = True
            response = await self.trade_client.place_order(order)
        except asyncio.CancelledError:
            self.store.finalize_submission(
                client_order_id,
                "submission_unknown" if order_attempted else "rejected",
                raw={**record["raw"], "error": "CancelledError"},
            )
            self.store.add_audit(
                "order_submission_unknown" if order_attempted else "order_preparation_canceled",
                "Submission interrupted" if order_attempted else "Preparation canceled before order submission",
                severity="error",
                payload={"client_order_id": client_order_id},
            )
            raise
        except Exception as exc:
            rejected = not order_attempted or isinstance(exc, OkxOrderRejected)
            status = "rejected" if rejected else "submission_unknown"
            event_type = "order_failed" if rejected else "order_submission_unknown"
            saved = self.store.finalize_submission(
                client_order_id,
                status,
                raw={**record["raw"], "error": type(exc).__name__},
            )
            self.store.add_audit(
                event_type,
                "Order rejected" if rejected else "Order outcome is unknown; automatic retry is blocked",
                severity="error",
                payload={"order": saved, "error": type(exc).__name__},
            )
            await self.notify_event(
                event_type,
                "OpenPerpDesk 订单被拒绝" if rejected else "OpenPerpDesk 订单状态待确认",
                f"{signal.inst_id} {side} {size}：{type(exc).__name__}。"
                + ("" if rejected else "需先与交易所对账，不会自动重发。"),
                payload={"order": saved, "error": type(exc).__name__},
                severity="error",
            )
            raise

        data = response.get("data", [])
        exchange_order_id = str(data[0].get("ordId", "")) if data else None
        saved = self.store.finalize_submission(
            client_order_id,
            "submitted",
            exchange_order_id=exchange_order_id,
            raw={**record["raw"], "exchange": response},
        )
        self.store.add_audit(
            "order_submitted",
            "Demo order submitted after risk approval",
            payload=saved,
        )
        await self.notify_event(
            "order_submitted",
            "OpenPerpDesk Demo 订单已提交",
            f"{signal.inst_id} {side} {size}，客户端订单号 {client_order_id}。",
            payload={"order": saved},
        )
        return {
            "accepted": True,
            "idempotent": False,
            "dry_run": False,
            "order": saved,
            "risk": decision.signal,
            "preflight": record["raw"]["preflight"],
            "exchange": response,
        }

    async def _preflight_rejection(self, signal: TradeSignal, reason: str) -> dict[str, Any]:
        self.store.add_audit(
            "execution_preflight_rejected", "Execution preflight did not approve the order",
            severity="warning", payload={"inst_id": signal.inst_id, "reason": reason},
        )
        await self.notify_event(
            "execution_preflight_rejected", "OpenPerpDesk 发单校验未通过",
            f"{signal.inst_id}：{reason}，未发单。",
            payload={"inst_id": signal.inst_id, "reason": reason}, severity="warning",
        )
        return {"accepted": False, "idempotent": False, "reasons": [reason]}

    def protective_exit(
        self,
        *,
        inst_id: str,
        mark_price: float,
    ) -> dict[str, Any] | None:
        for position in self.store.list_positions():
            if position["inst_id"] != inst_id:
                continue
            stop_loss = position["stop_loss"]
            take_profit = position["take_profit"]
            if not stop_loss and not take_profit:
                continue
            is_long = position["pos_side"] in {"long", "net"} and position["size"] > 0
            hit_stop = bool(stop_loss and ((is_long and mark_price <= stop_loss) or (not is_long and mark_price >= stop_loss)))
            hit_target = bool(take_profit and ((is_long and mark_price >= take_profit) or (not is_long and mark_price <= take_profit)))
            if hit_stop or hit_target:
                reason = "stop_loss" if hit_stop else "take_profit"
                return {
                    "inst_id": inst_id,
                    "reason": reason,
                    "mark_price": mark_price,
                    "position": position,
                }
        return None

    async def notify_event(
        self,
        event_type: str,
        title: str,
        content: str,
        *,
        payload: dict[str, Any] | None = None,
        severity: str = "info",
    ) -> bool:
        if not self.pushplus.configured:
            return False
        try:
            await self.pushplus.send(title, content)
            return True
        except Exception as exc:
            self.store.add_audit(
                "notification_failed",
                "PushPlus notification failed",
                severity="warning",
                payload={
                    "event_type": event_type,
                    "title": title,
                    "severity": severity,
                    "error": type(exc).__name__,
                    **(payload or {}),
                },
            )
            return False

    async def notify(self, title: str, content: str) -> dict[str, Any]:
        if not self.pushplus.configured:
            raise PushPlusError("pushplus_unconfigured")
        return await self.pushplus.send(title, content)
