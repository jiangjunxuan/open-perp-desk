import asyncio
import math
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from .account_sync import AccountSynchronizer
from .state_store import StateStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountReconciler:
    """Persist private WS state and periodically verify it with REST."""

    def __init__(
        self,
        synchronizer: AccountSynchronizer,
        store: StateStore,
        notifier: Callable[..., Awaitable[bool]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.synchronizer = synchronizer
        self.store = store
        self.notifier = notifier
        self.enabled = synchronizer.account_client.configured
        self.interval_seconds = max(
            15,
            int(os.getenv("ACCOUNT_SYNC_INTERVAL_SECONDS", "30")),
        )
        self.last_sync_at: str | None = None
        self.last_attempt_at: str | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stream_task: asyncio.Task[None] | None = None
        self._health_task: asyncio.Task[None] | None = None
        self._stream_changed = asyncio.Event()
        self._stream_health_changed = asyncio.Event()
        self._clock = clock or time.monotonic
        try:
            grace_seconds = float(os.getenv("PRIVATE_STREAM_ALERT_GRACE_SECONDS", "30"))
        except (TypeError, ValueError):
            grace_seconds = 30.0
        if not math.isfinite(grace_seconds):
            grace_seconds = 30.0
        self.private_stream_alert_grace_seconds = min(max(grace_seconds, 0.0), 3600.0)
        self._stream_health_poll_seconds = 1.0
        self._stream_unready_since: float | None = None
        self._stream_alerted = False

    def notify_stream(self) -> None:
        self._stream_changed.set()
        self._stream_health_changed.set()

    async def start(self) -> None:
        if self.enabled and self._task is None:
            self._stream_unready_since = None
            self._stream_alerted = False
            self._task = asyncio.create_task(
                self._run(),
                name="openperpdesk-account-reconciler",
            )
            self._stream_task = asyncio.create_task(
                self._run_stream(), name="openperpdesk-private-stream-sync",
            )
            self._health_task = asyncio.create_task(
                self._run_stream_health(), name="openperpdesk-private-stream-health",
            )

    async def stop(self) -> None:
        tasks = [
            task for task in (self._task, self._stream_task, self._health_task)
            if task is not None
        ]
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._stream_task = None
        self._health_task = None

    def snapshot(self) -> dict[str, Any]:
        private_stream = self.synchronizer.private_stream_status()
        return {
            "enabled": self.enabled,
            "running": self._task is not None,
            "interval_seconds": self.interval_seconds,
            "last_sync_at": self.last_sync_at,
            "last_attempt_at": self.last_attempt_at,
            "last_error": self.last_error,
            "private_stream": private_stream,
            "private_stream_alert_grace_seconds": self.private_stream_alert_grace_seconds,
            "private_stream_alerted": self._stream_alerted,
        }

    @staticmethod
    def _safe_stream_payload(status: dict[str, Any]) -> dict[str, Any]:
        return {
            "reason_code": status.get("reason_code"),
            "configured": bool(status.get("configured")),
            "ready": bool(status.get("ready")),
            "account_connected": bool(status.get("account_connected")),
            "account_authenticated": bool(status.get("account_authenticated")),
            "algo_connected": bool(status.get("algo_connected")),
            "algo_authenticated": bool(status.get("algo_authenticated")),
        }

    async def check_private_stream_health(self, *, now: float | None = None) -> dict[str, Any]:
        """Track private-stream incidents without exposing provider error details."""
        current = self._clock() if now is None else now
        status = self.synchronizer.private_stream_status()
        if not status["configured"]:
            self._stream_unready_since = None
            self._stream_alerted = False
            return status

        if status["ready"]:
            self._stream_unready_since = None
            if self._stream_alerted:
                self._stream_alerted = False
                payload = self._safe_stream_payload(status)
                self.store.add_audit(
                    "private_stream_recovered",
                    "Authenticated private account streams recovered",
                    payload=payload,
                )
                await self._notify_event(
                    "private_stream_recovered",
                    "OpenPerpDesk 私有账户推送已恢复",
                    "账户与原生保护订单的实时推送已连接并认证，已恢复实时状态同步。",
                    payload=payload,
                )
            return status

        if self._stream_unready_since is None:
            self._stream_unready_since = current
        if (
            not self._stream_alerted
            and current - self._stream_unready_since >= self.private_stream_alert_grace_seconds
        ):
            self._stream_alerted = True
            payload = self._safe_stream_payload(status)
            self.store.add_audit(
                "private_stream_unavailable",
                "Authenticated private account streams are not ready",
                severity="error",
                payload=payload,
            )
            await self._notify_event(
                "private_stream_unavailable",
                "OpenPerpDesk 私有账户推送中断",
                "账户或原生保护订单的实时推送持续未就绪，已禁止新的开仓请求。",
                payload=payload,
                severity="error",
            )
        return status

    async def run_once(self) -> dict[str, Any]:
        self.last_attempt_at = _now()
        stream = self.synchronizer.sync_stream()
        rest = await self.synchronizer.sync_rest()
        result = {"stream": stream, "rest": rest}
        if rest.get("errors"):
            self.last_error = "account_sync_partial: " + ", ".join(rest["errors"])
            self.store.add_audit(
                "account_reconcile_incomplete",
                "Private account reconciliation was incomplete",
                severity="warning",
                payload=result,
            )
            return result
        self.last_sync_at = _now()
        self.last_error = None
        self.store.add_audit(
            "account_reconciled",
            "Private account state reconciled",
            payload=result,
        )
        return result

    async def _run_stream(self) -> None:
        while True:
            await self._stream_changed.wait()
            self._stream_changed.clear()
            try:
                self.synchronizer.sync_stream()
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.store.add_audit(
                    "account_stream_sync_failed",
                    "Private stream update could not be persisted",
                    severity="error", payload={"error": type(exc).__name__},
                )

    async def _run_stream_health(self) -> None:
        while True:
            try:
                await self.check_private_stream_health()
                try:
                    await asyncio.wait_for(
                        self._stream_health_changed.wait(),
                        timeout=self._stream_health_poll_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                self._stream_health_changed.clear()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.store.add_audit(
                    "private_stream_health_check_failed",
                    "Private stream health could not be evaluated",
                    severity="warning",
                    payload={"error": type(exc).__name__},
                )
                await asyncio.sleep(self._stream_health_poll_seconds)

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.store.add_audit(
                    "account_reconcile_failed",
                    "Private account reconciliation failed",
                    severity="error",
                    payload={"error": type(exc).__name__},
                )
                await self._notify_event(
                    "account_reconcile_failed",
                    "OpenPerpDesk 账户对账失败",
                    f"私有账户对账周期失败：{type(exc).__name__}。",
                    payload={"error": type(exc).__name__},
                    severity="error",
                )
            await asyncio.sleep(self.interval_seconds)

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
