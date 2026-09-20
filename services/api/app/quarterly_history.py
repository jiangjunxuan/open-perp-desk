import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from .bill_archive import BillArchiveDownloader, BillArchiveError, parse_bill_archive, quarter_days
from .historical_ledger import prepare_history_window
from .okx_account import OkxAccountError
from .state_store import BillImportBusy, BillImportLeaseLost


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


class QuarterlyHistoryImporter:
    def __init__(self, store, client, *, downloader=None) -> None:
        self.store, self.client = store, client
        self.downloader = downloader or BillArchiveDownloader(proxy=client.proxy_url)
        self.owner = uuid4().hex
        self.poll_ms = 60_000
        self._task = None
        self._wake = asyncio.Event()
        self._closing = False
        self.last_error = None

    def snapshot(self):
        return {
            "configured": self.client.configured,
            "running": self._task is not None and not self._task.done(),
            "last_error": self.last_error,
        }

    async def start(self) -> None:
        if self.client.configured and self._task is None:
            self._task = asyncio.create_task(self._run(), name="openperpdesk-quarterly-history")

    async def request(self, year: int, quarter: str, *, retry: bool = False):
        if self._closing:
            raise BillArchiveError("archive_importer_stopping")
        if not self.client.configured:
            raise OkxAccountError("OKX read-only credentials are not configured")
        quarter_days(year, quarter)
        job = await asyncio.to_thread(
            self.store.create_bill_archive, self.client.account_scope, year, quarter, _now_ms(), retry=retry,
        )
        self._wake.set()
        return job

    async def close(self) -> None:
        self._closing = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                processed = await self.run_once()
                self.last_error = None
                if processed:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
            try:
                await asyncio.wait_for(self._wake.wait(), 5)
            except TimeoutError:
                pass

    async def run_once(self) -> bool:
        if not self.client.configured:
            return False
        scope = self.client.account_scope
        job = await asyncio.to_thread(self.store.claim_bill_archive, scope, self.owner, _now_ms())
        if job is None:
            return False

        async def update(state, **values):
            await asyncio.to_thread(
                self.store.update_bill_archive, job["id"], scope, self.owner, _now_ms(), state=state, **values,
            )

        async def heartbeat():
            while True:
                if scope != self.client.account_scope:
                    raise BillImportLeaseLost("archive_account_scope_changed")
                await asyncio.to_thread(self.store.touch_bill_archive, job["id"], scope, self.owner, _now_ms())
                await asyncio.sleep(20)

        async def process():
            if scope != self.client.account_scope:
                raise BillImportLeaseLost("archive_account_scope_changed")
            if job["import_job_id"]:
                imported = await asyncio.to_thread(self.store.bill_import, scope, job["import_job_id"])
                if imported and imported["status"] == "completed":
                    await update("completed", release=True)
                    return
            if not job["requested_at_ms"]:
                # Persist before the POST: a crash/timeout resumes by querying
                # the report, not blindly repeating a potentially accepted POST.
                job["requested_at_ms"] = _now_ms()
                await update("requesting", requested_at_ms=job["requested_at_ms"])
                result = await self.client.apply_bill_archive(job["year"], job["quarter"])
                if result["result"] == "false":
                    await update("waiting", next_attempt_ms=_now_ms() + self.poll_ms, release=True)
                    return
            status = await self.client.bill_archive_status(job["year"], job["quarter"])
            if status["state"] == "failed":
                raise BillArchiveError("archive_generation_failed")
            if status["state"] == "ongoing":
                if _now_ms() - job["requested_at_ms"] > 4 * 3600_000:
                    raise BillArchiveError("archive_generation_timeout")
                await update("waiting", next_attempt_ms=_now_ms() + self.poll_ms, release=True)
                return
            await update("downloading")
            content = await self.downloader.download(status.get("fileHref", ""))
            subtypes = await self.client.bill_subtypes()
            windows = await asyncio.to_thread(parse_bill_archive, content, job["year"], job["quarter"], subtypes)
            del content
            if scope != self.client.account_scope:
                raise BillImportLeaseLost("archive_account_scope_changed")
            imported = await asyncio.to_thread(self.store.create_bill_import, scope, list(windows), _now_ms())
            job["import_job_id"] = imported["id"]
            await update("importing", import_job_id=imported["id"])
            for day, rows in windows.items():
                if scope != self.client.account_scope:
                    raise BillImportLeaseLost("archive_account_scope_changed")
                records, summary = await asyncio.to_thread(prepare_history_window, rows, day)
                await asyncio.to_thread(
                    self.store.commit_bill_history_window, imported["id"], scope, day, records, summary, _now_ms(),
                    archive_lease=(job["id"], self.owner),
                )
            await asyncio.to_thread(self.store.finish_bill_import, imported["id"], scope, "completed")
            await update("completed", release=True)

        tasks = [asyncio.create_task(process()), asyncio.create_task(heartbeat())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self._interrupt_import(scope, job)
            try:
                await update("waiting", next_attempt_ms=_now_ms(), release=True)
            except BillImportLeaseLost:
                pass
            raise
        except BillImportBusy:
            await update("waiting", next_attempt_ms=_now_ms() + self.poll_ms, error="archive_waiting_for_import", release=True)
        except BillImportLeaseLost:
            await self._interrupt_import(scope, job)
            try:
                await update("failed", error="archive_lease_or_account_changed", release=True)
            except BillImportLeaseLost:
                pass
        except Exception as exc:
            await self._interrupt_import(scope, job)
            failures = job["failures"] + 1
            retry = isinstance(exc, OkxAccountError) and failures < 5
            error = str(exc) if isinstance(exc, BillArchiveError) else type(exc).__name__
            await update(
                "waiting" if retry else "failed", error=error, failures=failures,
                next_attempt_ms=_now_ms() + self.poll_ms, release=True,
            )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return True

    async def _interrupt_import(self, scope, job):
        if job["import_job_id"]:
            await asyncio.to_thread(
                self.store.finish_bill_import, job["import_job_id"], scope, "interrupted", "archive_import_interrupted",
            )
