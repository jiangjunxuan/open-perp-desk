import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import main as api_main, private_connection
from app.private_connection import PrivateConnectionCheck, PrivateProbeError, REST_CHECKS
from app.realtime import private_events
from app.state_store import StateStore
from tests.fixtures.exchange_server import ExchangeServer
from tests.fixtures.api_process import ApiProcess, eventually


class ConnectionCheckTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account = SimpleNamespace(configured=True, demo=True)
        for name in REST_CHECKS:
            setattr(self.account, name, AsyncMock(return_value=[{"private": "do-not-publish"}]))
        self.streams = [SimpleNamespace(
            connected=True, authenticated=True, proxy_url="socks5://secret-user:secret@fixture",
            last_error=None, start=AsyncMock(), stop=AsyncMock(),
        ) for _ in range(2)]
        for name, value in zip(
            ("OkxAccountClient", "OkxAccountStream", "OkxAlgoOrderStream"),
            (self.account, *self.streams),
        ):
            replacement = patch.object(private_connection, name, return_value=value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.check = PrivateConnectionCheck(timeout=1)
        self.addAsyncCleanup(self.check.close)

    async def complete(self):
        accepted = self.check.start()
        self.assertEqual(accepted["status"], "running")
        await self.check._task
        return self.check.snapshot()

    async def test_success_has_per_check_counts_but_no_account_values_or_proxy_secrets(self):
        result = await self.complete()
        self.assertEqual(result["status"], "passed")
        self.assertTrue(all(row["status"] == "passed" for row in result["checks"]))
        self.assertTrue(all(row["rows"] == 1 for row in result["checks"] if row["name"] in REST_CHECKS))
        self.assertFalse(result["trading_performed"] or result["order_lifecycle_verified"])
        encoded = json.dumps(result)
        self.assertNotIn("secret", encoded)
        self.assertNotIn("do-not-publish", encoded)
        self.assertTrue(result["report"]["proxy_configured"])
        for stream in self.streams:
            stream.stop.assert_awaited_once()
        result["checks"][0]["status"] = "tampered"
        self.assertEqual(self.check.snapshot()["checks"][0]["status"], "passed")

    async def test_missing_credentials_and_live_mode_do_not_open_connections(self):
        for configured, demo, error in (
            (False, True, "credentials_missing"), (True, False, "live_probe_not_approved"),
        ):
            self.account.configured, self.account.demo = configured, demo
            result = await self.complete()
            self.assertEqual(result["error"], error)
            self.assertEqual(result["status"], "failed")
            self.assertIsNone(result["report"])
            self.account.balance.assert_not_awaited()
        for stream in self.streams:
            stream.start.assert_not_awaited()

    async def test_running_check_cannot_be_started_twice(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            started.set()
            await release.wait()
            return []

        self.account.balance.side_effect = blocked
        self.check.start()
        await started.wait()
        with self.assertRaises(PrivateProbeError) as raised:
            self.check.start()
        self.assertEqual(raised.exception.code, "check_busy")
        self.assertEqual(self.check.snapshot()["status"], "running")
        release.set()
        await self.check._task
        self.account.balance.assert_awaited_once()

    async def test_failure_clears_previous_success_and_redacts_provider_message(self):
        self.assertEqual((await self.complete())["status"], "passed")
        self.account.balance.side_effect = RuntimeError("key-and-proxy-password")
        started = self.check.start()
        self.assertIsNone(started["report"])
        self.assertIsNone(started["checked_at"])
        await self.check._task
        result = self.check.snapshot()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "connection_check_failed")
        self.assertIsNone(result["report"])
        self.assertNotIn("key-and-proxy-password", json.dumps(result))
        self.assertEqual(next(row for row in result["checks"] if row["name"] == "balance")["status"], "failed")

    async def test_timeout_cancels_reads_and_stops_both_streams(self):
        canceled = []

        async def blocked(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.append(True)

        for name in REST_CHECKS:
            getattr(self.account, name).side_effect = blocked
        self.check.timeout = 0.03
        result = await self.complete()
        self.assertEqual(result["error"], "check_timeout")
        self.assertEqual(len(canceled), len(REST_CHECKS))
        self.assertFalse(any(row["status"] == "running" for row in result["checks"]))
        for stream in self.streams:
            stream.stop.assert_awaited_once()

    async def test_shutdown_cancels_probe_and_never_publishes_success(self):
        started = asyncio.Event()

        async def blocked():
            started.set()
            await asyncio.Event().wait()

        self.account.balance.side_effect = blocked
        self.check.start()
        await started.wait()
        await self.check.close()
        result = self.check.snapshot()
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["error"], "check_interrupted")
        self.assertIsNone(result["report"])
        for stream in self.streams:
            stream.stop.assert_awaited_once()

    async def test_cleanup_failure_waits_for_other_stream_and_does_not_report_pass(self):
        completed = []

        async def stop():
            await asyncio.sleep(0.01)
            completed.append(True)

        self.streams[0].stop.side_effect = RuntimeError("private cleanup error")
        self.streams[1].stop.side_effect = stop
        result = await self.complete()
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["report"])
        self.assertEqual(completed, [True])


class ConnectionCheckApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.check = PrivateConnectionCheck()
        self.addAsyncCleanup(self.check.close)
        for replacement in (
            patch.dict(os.environ, {"ADMIN_API_TOKEN": "connection-check-fixture", "OKX_API_KEY": ""}),
            patch.object(api_main, "private_connection_check", self.check),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.route = "/api/v1/account/connection-check"
        self.headers = {"X-Admin-Token": "connection-check-fixture"}

    async def test_both_endpoints_require_admin_and_unauthorized_request_starts_nothing(self):
        for method in ("GET", "POST"):
            response = await self.client.request(method, self.route)
            self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.check._task)
        response = await self.client.get(self.route, headers=self.headers)
        self.assertEqual(response.json()["data"]["status"], "idle")

    async def test_missing_credentials_is_a_readonly_diagnostic_not_a_server_error(self):
        response = await self.client.post(self.route, headers=self.headers)
        self.assertEqual(response.status_code, 202)
        await self.check._task
        result = (await self.client.get(self.route, headers=self.headers)).json()["data"]
        self.assertEqual(result["error"], "credentials_missing")
        self.assertFalse(result["trading_performed"])

    async def test_busy_response_is_fixed_and_does_not_restart_task(self):
        self.check._task = asyncio.create_task(asyncio.Event().wait())
        original = self.check._task
        response = await self.client.post(self.route, headers=self.headers)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {"detail": "check_busy"})
        self.assertIs(self.check._task, original)

    async def test_sse_delivers_in_memory_progress_without_database_write_and_respects_revocation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            stream = SimpleNamespace(configured=False, connected=False, authenticated=False, balance=[], last_message_at=None)
            client = SimpleNamespace(account_scope="fixture", configured=False)
            allowed = True
            events = private_events(
                store, stream, client, lambda: allowed, connection_check=self.check.snapshot,
            )
            async def close_events():
                await events.aclose()

            self.addAsyncCleanup(close_events)

            async def next_check():
                async with asyncio.timeout(2):
                    async for frame in events:
                        if frame.startswith("event: connection_check\n"):
                            return json.loads(frame.split("data: ", 1)[1])["data"]

            self.assertEqual((await next_check())["status"], "idle")
            revision = store.revision
            self.check.start()
            await self.check._task
            self.assertEqual((await next_check())["error"], "credentials_missing")
            self.assertEqual(store.revision, revision)
            allowed = False
            self.assertEqual(await anext(events), "event: locked\ndata: {}\n\n")


class ConnectionCheckSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_loopback_check_has_only_read_requests_and_restores_no_execution(self):
        exchange = await ExchangeServer().start()
        self.addAsyncCleanup(exchange.close)
        with patch.dict(os.environ, {
            **exchange.environment(), "OKX_DEMO": "true", "OKX_PROXY_URL": "",
        }):
            check = PrivateConnectionCheck(timeout=5)
            self.addAsyncCleanup(check.close)
            check.start()
            await asyncio.wait_for(check._task, 10)
        result = check.snapshot()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual(set(exchange.subscriptions), {"/private", "/algo"})
        self.assertEqual(exchange.posts, [])
        self.assertEqual(exchange.errors, [])
        self.assertFalse(result["trading_performed"] or result["order_lifecycle_verified"])

    async def test_api_subprocess_pushes_completed_check_and_keeps_execution_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            exchange = await ExchangeServer().start()
            api = ApiProcess(directory, exchange)
            api.environment["EXECUTION_ENABLED"] = "false"
            try:
                await api.start()
                await api.request("POST", "/account/connection-check", status=202)

                async def finished():
                    state = await api.request("GET", "/account/connection-check")
                    return state["data"]["status"] != "running"

                await eventually(finished)
                state = await api.request("GET", "/account/connection-check")
                self.assertEqual(state["data"]["status"], "passed", state)
                async with asyncio.timeout(5):
                    async with api.client.stream("GET", "/api/v1/account/events") as response:
                        event = ""
                        async for line in response.aiter_lines():
                            if line.startswith("event:"):
                                event = line.split(":", 1)[1].strip()
                            elif line.startswith("data:") and event == "connection_check":
                                pushed = json.loads(line.split(":", 1)[1])["data"]
                                break
                self.assertEqual(pushed, state["data"])
                status = await api.request("GET", "/system/status")
                self.assertFalse(status["execution_enabled"])
                self.assertFalse(status["automation_worker"]["enabled"])
                self.assertFalse(status["live_safety"]["allowed"])
                self.assertEqual(exchange.posts, [])
                self.assertEqual(exchange.errors, [])
            finally:
                await api.stop()
                await exchange.close()
