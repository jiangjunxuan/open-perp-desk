import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Any

from .historical_ledger import DAY_MS, day_ms, history_days, prepare_history_window
from .okx_account import OkxAccountClient, OkxAccountError
from .state_store import BillImportLeaseLost, StateStore


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class AccountHistoryImporter:
    """Read-only, leased imports; publish complete UTC days, never partial pages."""

    def __init__(self, store: StateStore, client: OkxAccountClient) -> None:
        self.store = store
        self.client = client
        self._tasks: set[asyncio.Task[None]] = set()
        self._closing = False

    async def start(self, start_day: date, end_day: date) -> dict[str, Any]:
        if self._closing:
            raise RuntimeError("history_importer_stopping")
        if not self.client.configured:
            raise OkxAccountError("OKX read-only credentials are not configured")
        days = history_days(start_day, end_day, now=datetime.now(timezone.utc), importing=True)
        scope = self.client.account_scope
        job = await asyncio.to_thread(self.store.create_bill_import, scope, days, _now_ms())
        task = asyncio.create_task(self._run(job["id"], scope, days), name=f"bill-import-{job['id']}")
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return job

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logging.getLogger(__name__).error("History importer stopped: %s", type(error).__name__)

    async def close(self) -> None:
        self._closing = True
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, job_id: str, scope: str, days: list[date]) -> None:
        async def heartbeat() -> None:
            if scope != self.client.account_scope:
                raise BillImportLeaseLost("history_account_scope_changed")
            await asyncio.to_thread(self.store.touch_bill_import, job_id, scope, _now_ms())

        try:
            for day in days:
                await heartbeat()
                raw = await self.client.bills_archive(day_ms(day), day_ms(day) + DAY_MS, on_page=heartbeat)
                records, summary = await asyncio.to_thread(prepare_history_window, raw, day)
                await heartbeat()
                await asyncio.to_thread(
                    self.store.commit_bill_history_window, job_id, scope, day, records, summary, _now_ms(),
                )
            await asyncio.to_thread(self.store.finish_bill_import, job_id, scope, "completed")
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.finish_bill_import, job_id, scope, "interrupted", "import_cancelled")
            raise
        except Exception as exc:
            # Do not persist remote error strings, which may contain proxy URLs.
            await asyncio.to_thread(self.store.finish_bill_import, job_id, scope, "failed", type(exc).__name__)
