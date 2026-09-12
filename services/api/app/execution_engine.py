import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from .okx_trade import OkxTradeClient, OrderRequest
from .pushplus import PushPlusClient, PushPlusError
from .risk_engine import RiskEngine
from .safety_control import SafetyController
from .state_store import StateStore
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
    ) -> None:
        self.store = store
        self.risk_engine = risk_engine
        self.trade_client = trade_client
        self.pushplus = pushplus
        self.safety = safety or SafetyController(store)

    @staticmethod
    def client_order_id(
        signal: TradeSignal,
        idempotency_key: str | None = None,
    ) -> str:
        if idempotency_key:
            digest = hashlib.sha256(idempotency_key.encode()).hexdigest()[:20]
            return f"opd-{digest}"
        digest = hashlib.sha256(
            json.dumps(signal.model_dump(mode="json"), sort_keys=True).encode()
        ).hexdigest()[:20]
        return f"opd-{digest}"

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
            return {
                "accepted": False,
                "idempotent": False,
                "reasons": ["emergency_stop_active"],
            }
        client_order_id = self.client_order_id(signal, idempotency_key)
        existing = self.store.get_order(client_order_id)
        if existing:
            return {
                "accepted": existing["status"] not in {"rejected", "failed"},
                "idempotent": True,
                "order": existing,
                "reasons": [],
            }

        decision = self.risk_engine.evaluate(
            signal,
            account_equity=account_equity,
            daily_pnl_pct=daily_pnl_pct,
            current_notional=current_notional,
            market_data_fresh=market_data_fresh,
        )
        if not decision.approved:
            self.store.add_audit(
                "risk_rejected",
                "Signal rejected by risk engine",
                severity="warning",
                payload={"signal": decision.signal, "reasons": decision.reasons},
            )
            return {
                "accepted": False,
                "idempotent": False,
                "reasons": list(decision.reasons),
                "risk": decision.signal,
            }

        side = side_override or ("buy" if signal.action == "open_long" else "sell")
        reduce_only = signal.action == "close"
        order = OrderRequest(
            inst_id=signal.inst_id,
            side=side,
            pos_side="net",
            ord_type="market",
            sz=size,
            reduce_only=reduce_only,
            stop_loss=signal.stop_loss if not reduce_only else None,
            take_profit=signal.take_profit if not reduce_only else None,
            cl_ord_id=client_order_id,
        )
        record = {
            "client_order_id": client_order_id,
            "status": "preview" if dry_run else "submitting",
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
            "raw": {"signal": signal.model_dump(mode="json")},
            "created_at": _now(),
        }
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
                "order": self.store.save_order(record),
                "risk": decision.signal,
            }

        try:
            response = await self.trade_client.place_order(order)
        except Exception as exc:
            record["status"] = "failed"
            record["raw"] = {"error": type(exc).__name__}
            saved = self.store.save_order(record)
            self.store.add_audit(
                "order_failed",
                "Demo order submission failed",
                severity="error",
                payload={"order": saved, "error": type(exc).__name__},
            )
            raise

        data = response.get("data", [])
        exchange_order_id = str(data[0].get("ordId", "")) if data else None
        record["status"] = "submitted"
        record["exchange_order_id"] = exchange_order_id
        record["raw"] = response
        saved = self.store.save_order(record)
        self.store.add_audit(
            "order_submitted",
            "Demo order submitted after risk approval",
            payload=saved,
        )
        await self._notify("OpenPerpDesk Demo 成交提交", f"{signal.inst_id} {side} {size}")
        return {
            "accepted": True,
            "idempotent": False,
            "dry_run": False,
            "order": saved,
            "risk": decision.signal,
            "exchange": response,
        }

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

    async def _notify(self, title: str, content: str) -> None:
        if not self.pushplus.configured:
            return
        try:
            await self.pushplus.send(title, content)
        except PushPlusError:
            self.store.add_audit(
                "notification_failed",
                "PushPlus notification failed",
                severity="warning",
            )

    async def notify(self, title: str, content: str) -> dict[str, Any]:
        if not self.pushplus.configured:
            raise PushPlusError("PushPlus token is not configured")
        return await self.pushplus.send(title, content)
