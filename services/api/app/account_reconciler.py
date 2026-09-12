import asyncio
import os
from datetime import datetime, timezone
from typing import Any

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
    ) -> None:
        self.synchronizer = synchronizer
        self.store = store
        self.enabled = synchronizer.account_client.configured
        self.interval_seconds = max(
            15,
            int(os.getenv("ACCOUNT_SYNC_INTERVAL_SECONDS", "30")),
        )
        self.last_sync_at: str | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="openperpdesk-account-reconciler",
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "running": self._task is not None,
            "interval_seconds": self.interval_seconds,
            "last_sync_at": self.last_sync_at,
            "last_error": self.last_error,
        }

    async def run_once(self) -> dict[str, Any]:
        stream = self.synchronizer.sync_stream()
        rest = await self.synchronizer.sync_rest()
        result = {"stream": stream, "rest": rest}
        self.last_sync_at = _now()
        self.last_error = None
        self.store.add_audit(
            "account_reconciled",
            "Private account state reconciled",
            payload=result,
        )
        return result

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
            await asyncio.sleep(self.interval_seconds)
