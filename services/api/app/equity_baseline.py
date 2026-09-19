import asyncio
from datetime import datetime, timezone
from typing import Any, Callable

from .account_ledger import amount
from .historical_ledger import DAY_MS


CAPTURE_WINDOW_MS = 60_000
MAX_REQUEST_MS = 10_000
BASELINE_POLICY = "observed_rest_window_after_utc_midnight"


def total_equity(balance: list[dict[str, Any]]) -> str:
    if not isinstance(balance, list) or len(balance) != 1 or not isinstance(balance[0], dict):
        raise ValueError("equity_snapshot_balance_invalid")
    # adjEq is collateral-adjusted margin equity, not total account NAV.
    value = balance[0].get("totalEq")
    if value in (None, ""):
        raise ValueError("equity_snapshot_equity_missing")
    return str(amount(value, "equity_usd"))


def baseline_window(target_ms: int, request_started_ms: int, received_at_ms: int) -> None:
    if any(type(value) is not int or value <= 0 for value in (target_ms, request_started_ms, received_at_ms)):
        raise ValueError("equity_baseline_timestamp_invalid")
    if target_ms % DAY_MS:
        raise ValueError("equity_baseline_target_invalid")
    if not target_ms <= request_started_ms <= received_at_ms <= target_ms + CAPTURE_WINDOW_MS:
        raise ValueError("equity_baseline_window_missed")
    if received_at_ms - request_started_ms > MAX_REQUEST_MS:
        raise ValueError("equity_baseline_request_too_slow")


class EquityBaselineSampler:
    """Read-only boundary observations with explicit timing, never backfilled NAV."""

    def __init__(self, store, account, *, clock: Callable[[], int] | None = None) -> None:
        self.store = store
        self.account = account
        self.clock = clock or (lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
        self.last_attempt_ms: int | None = None
        self.last_capture_ms: int | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self.account.configured and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._run(), name="openperpdesk-equity-baseline")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.account.configured,
            "running": self._task is not None and not self._task.done(),
            "policy": BASELINE_POLICY, "capture_window_ms": CAPTURE_WINDOW_MS,
            "last_attempt_ms": self.last_attempt_ms, "last_capture_ms": self.last_capture_ms,
            "last_error": self.last_error,
        }

    async def run_once(self) -> bool:
        async with self._lock:
            if not self.account.configured:
                return False
            started = self.clock()
            target = started // DAY_MS * DAY_MS
            if started - target >= CAPTURE_WINDOW_MS:
                return False
            scope = self.account.account_scope
            existing = await asyncio.to_thread(self.store.equity_baseline, scope, target)
            if existing is not None:
                self.last_capture_ms = existing["received_at_ms"]
                self.last_error = None
                return False
            self.last_attempt_ms = self.clock()
            try:
                async with asyncio.timeout(MAX_REQUEST_MS / 1000):
                    balance = await self.account.balance()
                received = self.clock()
                if scope != self.account.account_scope or not self.account.configured:
                    raise ValueError("equity_baseline_account_changed")
                saved = await asyncio.to_thread(
                    self.store.save_equity_baseline, scope, target, self.last_attempt_ms, received, balance,
                )
                persisted = await asyncio.to_thread(self.store.equity_baseline, scope, target)
                self.last_capture_ms = persisted["received_at_ms"]
                self.last_error = None
                if saved:
                    await asyncio.to_thread(
                        self.store.add_audit, "equity_baseline_captured",
                        "UTC boundary equity observation persisted",
                        payload={"target_ms": target, "request_started_ms": self.last_attempt_ms, "received_at_ms": received},
                    )
                return saved
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = type(exc).__name__
                if error != self.last_error:
                    await asyncio.to_thread(
                        self.store.add_audit, "equity_baseline_failed",
                        "UTC boundary equity observation unavailable",
                        severity="warning", payload={"error": error, "target_ms": target},
                    )
                self.last_error = error
                return False

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
            now = self.clock()
            until_midnight = DAY_MS - now % DAY_MS
            await asyncio.sleep(min(5 if now % DAY_MS < CAPTURE_WINDOW_MS else 30, until_midnight / 1000))
