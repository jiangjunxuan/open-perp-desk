import asyncio
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from tests.fixtures.exchange_server import ExchangeServer


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/okx-private-smoke.py"
spec = importlib.util.spec_from_file_location("private_acceptance_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class PrivateProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.output = Path(directory.name) / "report.json"
        self.output.write_text('{"old":true}')
        self.account = SimpleNamespace(configured=True, demo=True)
        self.methods = ("balance", "positions", "config", "pending_orders",
                        "orders_history", "fills_history", "pending_algo_orders")
        for name in self.methods:
            setattr(self.account, name, AsyncMock(return_value=[{"private": "never-publish"}]))
        self.streams = [SimpleNamespace(connected=True, authenticated=True, last_error=None,
                                      start=AsyncMock(), stop=AsyncMock()) for _ in range(2)]
        for name, instance in zip(
            ("OkxAccountClient", "OkxAccountStream", "OkxAlgoOrderStream"),
            (self.account, *self.streams),
        ):
            replacement = patch.object(probe, name, return_value=instance)
            replacement.start()
            self.addCleanup(replacement.stop)

    async def run_probe(self, **kwargs):
        return await probe.run_probe(timeout=kwargs.get("timeout", 1),
                                     allow_live=kwargs.get("allow_live", False), output=self.output)

    async def test_success_contains_counts_not_values_and_report_is_private(self):
        result = await self.run_probe()
        self.assertEqual(result["rows"], {name: 1 for name in self.methods})
        self.assertFalse(result["trading_performed"] or result["order_lifecycle_verified"])
        self.assertNotIn("never-publish", self.output.read_text())
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o600)
        for stream in self.streams:
            stream.stop.assert_awaited_once()

    async def test_missing_credentials_or_unapproved_live_never_starts_connections(self):
        for configured, demo in ((False, True), (True, False)):
            with self.subTest(configured=configured, demo=demo):
                self.account.configured, self.account.demo = configured, demo
                self.output.write_text('{"old":true}')
                with self.assertRaises(probe.PrivateProbeError):
                    await self.run_probe()
                self.assertFalse(self.output.exists())
        for stream in self.streams:
            stream.start.assert_not_awaited()
        self.account.balance.assert_not_awaited()

    async def test_approved_live_is_still_readonly(self):
        self.account.demo = False
        result = await self.run_probe(allow_live=True)
        self.assertFalse(result["demo"] or result["trading_performed"])

    async def test_start_failure_cleans_up_both_connections(self):
        self.streams[1].start.side_effect = RuntimeError("start failure")
        with self.assertRaises(RuntimeError):
            await self.run_probe()
        for stream in self.streams:
            stream.stop.assert_awaited_once()
        self.assertFalse(self.output.exists())

    async def test_rest_stage_shares_deadline_and_cancels_siblings(self):
        canceled = []

        async def blocked(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.append(True)

        for name in self.methods:
            getattr(self.account, name).side_effect = blocked
        with self.assertRaises(TimeoutError):
            await self.run_probe(timeout=0.03)
        self.assertEqual(len(canceled), len(self.methods))
        self.assertFalse(self.output.exists())
        for stream in self.streams:
            stream.stop.assert_awaited_once()

    async def test_disconnect_during_rest_does_not_publish_success(self):
        async def disconnect():
            self.streams[1].authenticated = False
            self.streams[1].connected = False
            return []

        self.account.balance.side_effect = disconnect
        with self.assertRaisesRegex(probe.PrivateProbeError, "disconnected"):
            await self.run_probe()
        self.assertFalse(self.output.exists())

    async def test_rejected_login_never_queries_rest(self):
        self.streams[0].authenticated = False
        self.streams[0].last_error = "OkxAuthenticationError"
        with self.assertRaisesRegex(probe.PrivateProbeError, "authentication was rejected"):
            await self.run_probe()
        self.account.balance.assert_not_awaited()
        self.assertFalse(self.output.exists())
        for stream in self.streams:
            stream.stop.assert_awaited_once()

    async def test_one_rest_failure_cancels_remaining_reads(self):
        canceled = []

        async def blocked(*_args, **_kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                canceled.append(True)

        async def fail():
            await asyncio.sleep(0)
            raise RuntimeError("private provider message")

        for name in self.methods:
            getattr(self.account, name).side_effect = blocked
        self.account.balance.side_effect = fail
        with self.assertRaises(ExceptionGroup):
            await self.run_probe()
        self.assertEqual(len(canceled), len(self.methods) - 1)
        self.assertFalse(self.output.exists())
        for stream in self.streams:
            stream.stop.assert_awaited_once()

    async def test_cleanup_failure_does_not_publish_success(self):
        self.streams[0].stop.side_effect = OSError("fixture")
        with self.assertRaises(OSError):
            await self.run_probe()
        self.assertFalse(self.output.exists())


class PrivateProbeCliTests(unittest.TestCase):
    def test_provider_error_is_not_printed(self):
        for error in (RuntimeError("private-key-and-proxy-password"), OSError("secret/path"),
                      ExceptionGroup("private", [RuntimeError("private-key")])):
            stdout, stderr = io.StringIO(), io.StringIO()
            with self.subTest(kind=type(error).__name__), \
                 patch.object(sys, "argv", ["probe", "--output", "-"]), \
                 patch.object(probe, "run_probe", AsyncMock(side_effect=error)), \
                 redirect_stdout(stdout), redirect_stderr(stderr), \
                 self.assertRaises(SystemExit) as raised:
                probe.main()
            self.assertEqual(raised.exception.code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertNotIn(str(error), stderr.getvalue())
            self.assertNotIn("private-key", stderr.getvalue())

    def test_stdout_only_mode_passes_no_output_path(self):
        result = {"scope": "fixture", "trading_performed": False}
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["probe", "--output", "-"]), \
             patch.object(probe, "run_probe", AsyncMock(return_value=result)) as runner, \
             redirect_stdout(stdout):
            probe.main()
        self.assertIsNone(runner.await_args.kwargs["output"])
        self.assertEqual(json.loads(stdout.getvalue()), result)


class PrivateProbeSocketTests(unittest.IsolatedAsyncioTestCase):
    async def test_stdin_probe_real_loopback_rest_and_ws_never_imports_execution(self):
        exchange = await ExchangeServer().start()
        self.addAsyncCleanup(exchange.close)
        environment = {"PATH": os.defpath, "OKX_DEMO": "true", "NO_PROXY": "*",
                       "OKX_PROXY_URL": "", **exchange.environment()}
        script = SCRIPT.read_bytes() + b"\nassert 'app.main' not in sys.modules\nassert 'app.okx_trade' not in sys.modules\n"
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-", "--timeout", "5", "--output", "-",
            cwd=ROOT / "services/api", env=environment,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(script), 15)
            self.assertEqual(process.returncode, 0, stderr.decode())
            result = json.loads(stdout)
            self.assertTrue(result["private_ws_authenticated"] and result["algo_ws_authenticated"])
            self.assertEqual(result["rows"]["balance"], 1)
            self.assertFalse(result["trading_performed"] or result["order_lifecycle_verified"])
            self.assertEqual(set(exchange.subscriptions), {"/private", "/algo"})
            self.assertTrue(exchange.gets)
            self.assertEqual(exchange.posts, [])
            self.assertEqual(exchange.errors, [])
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
