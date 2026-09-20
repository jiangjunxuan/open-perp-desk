"""Read-only private connection diagnostics shared by the console and CLI."""

import asyncio
from copy import deepcopy
from datetime import datetime, timezone

from .okx_account import OkxAccountClient
from .okx_account_stream import OkxAccountStream
from .okx_algo_stream import OkxAlgoOrderStream


REST_CHECKS = (
    "balance", "positions", "config", "pending_orders",
    "orders_history", "fills_history", "pending_algo_orders",
)
CHECKS = ("private_ws", "algo_ws", *REST_CHECKS)


class PrivateProbeError(RuntimeError):
    """Fixed diagnostics only; never include provider responses or credentials."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


async def wait_for_authentication(account_stream, algo_stream, timeout: float) -> None:
    async with asyncio.timeout(timeout):
        while not (account_stream.authenticated and algo_stream.authenticated):
            errors = [error for error in (
                account_stream.last_error, algo_stream.last_error,
            ) if error]
            if errors and all(error in {"OkxAuthenticationError", "login_failed"} for error in errors):
                raise PrivateProbeError(
                    "Private WebSocket authentication was rejected.", "authentication_rejected",
                )
            await asyncio.sleep(0.1)


async def probe_connections(
    account, account_stream, algo_stream, *, timeout: float, allow_live: bool,
    progress=lambda name, status, rows=None: None,
) -> dict:
    if not account.configured:
        raise PrivateProbeError("OKX private credentials are not configured.", "credentials_missing")
    if not account.demo and not allow_live:
        raise PrivateProbeError(
            "Refusing a live private probe without --allow-live.", "live_probe_not_approved",
        )

    async def read(name):
        progress(name, "running")
        try:
            method = getattr(account, name)
            rows = await method(**({"limit": 100} if name.endswith("_history") or name == "pending_algo_orders" else {}))
        except asyncio.CancelledError:
            raise
        except Exception:
            progress(name, "failed")
            raise
        progress(name, "passed", len(rows))
        return rows

    try:
        async with asyncio.timeout(timeout):
            progress("private_ws", "running")
            progress("algo_ws", "running")
            await asyncio.gather(account_stream.start(), algo_stream.start())
            await wait_for_authentication(account_stream, algo_stream, timeout)
            progress("private_ws", "passed")
            progress("algo_ws", "passed")
            async with asyncio.TaskGroup() as group:
                reads = {name: group.create_task(read(name)) for name in REST_CHECKS}
            disconnected = False
            for name, stream in (("private_ws", account_stream), ("algo_ws", algo_stream)):
                if not (stream.connected and stream.authenticated):
                    progress(name, "failed")
                    disconnected = True
            if disconnected:
                raise PrivateProbeError(
                    "Private WebSocket disconnected during the probe.", "websocket_disconnected",
                )
            result = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "scope": "private_rest_and_websocket_read_only",
                "demo": account.demo,
                "proxy_configured": bool(account_stream.proxy_url),
                "private_ws_connected": account_stream.connected,
                "private_ws_authenticated": account_stream.authenticated,
                "algo_ws_connected": algo_stream.connected,
                "algo_ws_authenticated": algo_stream.authenticated,
                "rows": {name: len(task.result()) for name, task in reads.items()},
                "order_lifecycle_verified": False,
                "trading_performed": False,
            }
    finally:
        # Wait for both cleanups even if one fails. Do not publish success first.
        stopped = await asyncio.gather(account_stream.stop(), algo_stream.stop(), return_exceptions=True)
        for error in stopped:
            if isinstance(error, BaseException):
                raise error
    return result


class PrivateConnectionCheck:
    """One bounded, process-local diagnostic; never reuse trading streams."""

    def __init__(self, *, timeout: float = 45) -> None:
        self.timeout = timeout
        self._task = None
        self._snapshot = self._empty()

    @staticmethod
    def _empty() -> dict:
        return {
            "status": "idle", "started_at": None, "checked_at": None, "error": None,
            "checks": [{"name": name, "status": "pending", "rows": None} for name in CHECKS],
            "report": None, "trading_performed": False, "order_lifecycle_verified": False,
        }

    def snapshot(self) -> dict:
        return deepcopy(self._snapshot)

    def start(self) -> dict:
        if self._task is not None and not self._task.done():
            raise PrivateProbeError("A private connection check is already running.", "check_busy")
        self._snapshot = self._empty()
        self._snapshot.update(status="running", started_at=datetime.now(timezone.utc).isoformat())
        self._task = asyncio.create_task(self._run(), name="openperpdesk-private-connection-check")
        return self.snapshot()

    def _progress(self, name, status, rows=None) -> None:
        for check in self._snapshot["checks"]:
            if check["name"] == name:
                check.update(status=status, rows=rows)
                return

    async def _run(self) -> None:
        try:
            report = await probe_connections(
                OkxAccountClient(), OkxAccountStream(), OkxAlgoOrderStream(),
                timeout=self.timeout, allow_live=False, progress=self._progress,
            )
            self._snapshot.update(status="passed", report=report)
        except asyncio.CancelledError:
            self._snapshot.update(status="interrupted", error="check_interrupted")
            raise
        except PrivateProbeError as error:
            self._snapshot.update(status="failed", error=error.code)
        except TimeoutError:
            self._snapshot.update(status="failed", error="check_timeout")
        except Exception:
            self._snapshot.update(status="failed", error="connection_check_failed")
        finally:
            for check in self._snapshot["checks"]:
                if check["status"] == "running":
                    check["status"] = "incomplete"
            self._snapshot["checked_at"] = datetime.now(timezone.utc).isoformat()

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
