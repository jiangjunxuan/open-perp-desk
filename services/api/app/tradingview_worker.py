import asyncio
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from . import tradingview
from .trading_signal import TradeSignal


class TradingViewWorker:
    """Execute durable alerts outside the webhook response lifetime."""

    def __init__(self, store, execution, market_data_fresh) -> None:
        self.store, self.execution = store, execution
        self.market_data_fresh = market_data_fresh
        self.owner = uuid4().hex
        self._task = None
        self._wake = asyncio.Event()
        self._cycle_lock = asyncio.Lock()
        self.last_error = None

    def execution_scope(self) -> str:
        client = self.execution.trade_client
        identity = [client.base_url, client.demo, client.trading_mode, client.api_key]
        return hashlib.sha256(json.dumps(identity).encode()).hexdigest()

    def snapshot(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "last_error": self.last_error,
        }

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="openperpdesk-tradingview")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def notify(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                processed = await self.run_once()
                if processed:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
            try:
                await asyncio.wait_for(self._wake.wait(), 1)
            except TimeoutError:
                pass

    async def run_once(self) -> bool:
        if self._cycle_lock.locked():
            return False
        async with self._cycle_lock:
            job = await asyncio.to_thread(
                self.store.claim_tradingview_alert, self.owner,
                int(datetime.now(timezone.utc).timestamp() * 1000),
            )
            if job is None:
                return False
            self.last_error = None
            try:
                await asyncio.wait_for(self._process(job), 60)
            except asyncio.CancelledError:
                await self._finish(job, "interrupted", {"reasons": ["processing_interrupted"]})
                raise
            except Exception as exc:
                # The executor persists its order reservation before sending.
                # Unknown outcomes are reconciled there, never retried here.
                self.last_error = type(exc).__name__
                await self._finish(job, "unconfirmed", {"reasons": ["execution_result_unconfirmed"]})
            return True

    async def _finish(self, job, status, result) -> None:
        persisted = await asyncio.to_thread(
            self.store.finish_tradingview_alert, job["alert_id"], self.owner, status, result,
        )
        if persisted:
            await asyncio.to_thread(
                self.store.add_audit, "tradingview_signal_processed", "TradingView alert processing finished",
                severity="warning" if status in {"rejected", "expired", "interrupted", "unconfirmed"} else "info",
                payload={"alert_id": job["alert_id"], "inst_id": job["signal"]["inst_id"],
                         "status": status, "dry_run": job["dry_run"], **result},
            )

    async def _process(self, job) -> None:
        signal = TradeSignal.model_validate(job["signal"])
        if signal.expires_at <= datetime.now(timezone.utc):
            await self._finish(job, "expired", {"accepted": False, "reasons": ["signal_expired"]})
            return
        reason = None
        config = tradingview.status()
        if not config["configured"]:
            reason = "tradingview_integration_disabled"
        elif signal.inst_id not in config["symbols"]:
            reason = "symbol_not_allowed"
        elif job["execution_scope"] != self.execution_scope():
            reason = "execution_scope_changed"
        elif not job["dry_run"] and config["dry_run"]:
            reason = "tradingview_execution_disabled"
        if reason:
            await self._finish(job, "rejected", {"accepted": False, "reasons": [reason]})
            return
        if signal.action == "hold":
            await self._finish(job, "observed", {"accepted": False, "reasons": ["hold_signal"]})
            return
        result = await self.execution.submit_signal(
            signal, **job["context"], size=job["size"], dry_run=job["dry_run"],
            side_override=job["side"], idempotency_key=f"tradingview:{job['alert_id']}",
            market_data_fresh=self.market_data_fresh(),
        )
        accepted = bool(result.get("accepted"))
        status = ("preview" if job["dry_run"] else "submitted") if accepted else "rejected"
        await self._finish(job, status, {
            "accepted": accepted, "reasons": result.get("reasons", []),
            "client_order_id": (result.get("order") or {}).get("client_order_id"),
        })
