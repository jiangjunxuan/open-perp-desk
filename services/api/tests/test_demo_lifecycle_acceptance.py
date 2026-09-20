import asyncio
import copy
import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.fixtures.api_process import ADMIN_TOKEN, ApiProcess
from tests.fixtures.exchange_server import ExchangeServer, SYMBOL


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/okx-demo-lifecycle-smoke.py"
CONFIRMATION = "OPENPERPDESK_OKX_DEMO_LIFECYCLE"
SPEC = importlib.util.spec_from_file_location("openperpdesk_demo_lifecycle", SCRIPT)
LIFECYCLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LIFECYCLE)


class StreamEvidenceApi:
    def __init__(self):
        self.account = {
            "connected": True, "authenticated": True,
            "last_message_at": "2026-09-19T00:00:01+00:00",
            "orders": [{
                "clOrdId": "open-1", "instId": SYMBOL, "state": "filled",
                "attachAlgoOrds": [{"attachAlgoClOrdId": "protection-1"}],
            }],
            "fills": [{
                "clOrdId": "open-1", "tradeId": "trade-1",
                "fillSz": "1", "fillPx": "100",
            }],
        }
        self.algo = {
            "connected": True, "authenticated": True,
            "last_message_at": "2026-09-19T00:00:02+00:00",
        }
        self.protection = {
            "client_order_id": "protection-1", "inst_id": SYMBOL,
            "order_kind": "algo", "source": "okx-algo-stream", "status": "live",
        }

    def get(self, path):
        if path == "/account/stream":
            return self.account
        if path == "/system/status":
            return {"algo_stream": self.algo}
        if path == "/orders?limit=500":
            return {"data": [self.protection]}
        raise AssertionError("Unexpected fixture route")


class DemoLifecycleUnitTests(unittest.TestCase):
    def test_order_attempt_marks_lifecycle_before_response_can_be_lost(self):
        class LostResponseApi:
            def get(self, path):
                return status

            def post(self, path, payload=None):
                if path == "/execution/signals" and payload.get("dry_run") is True:
                    return {
                        "accepted": True,
                        "dry_run": True,
                        "preflight": {"basis": "exchange"},
                        "order": {"status": "preview"},
                    }
                raise LIFECYCLE.DemoLifecycleError("fixture response lost")

        status = {
            "risk_limits": {"max_position_pct": 10, "max_stop_distance_pct": 5},
            "account_stream": {"last_message_at": "2026-09-19T00:00:00+00:00"},
            "algo_stream": {"last_message_at": "2026-09-19T00:00:00+00:00"},
        }
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "fixture-admin-token"}), \
             patch.object(LIFECYCLE, "validate_environment"), \
             patch.object(LIFECYCLE, "ApiClient", return_value=LostResponseApi()), \
             patch.object(
                 LIFECYCLE, "verify_initial_state",
                 return_value=(status, Decimal("1"), Decimal("100")),
             ), \
             patch.object(LIFECYCLE, "cleanup_after_failure", return_value=True) as cleanup, \
             patch.object(LIFECYCLE, "emergency_lock", return_value=True):
            with self.assertRaisesRegex(LIFECYCLE.DemoLifecycleError, "fixture response lost"):
                LIFECYCLE.run_probe(
                    origin="http://127.0.0.1:8000",
                    inst_id=SYMBOL,
                    side="long",
                    timeout=30,
                    confirmation=CONFIRMATION,
                    output=None,
                )
        cleanup.assert_called_once()
        self.assertGreater(cleanup.call_args.args[3] - time.monotonic(), 58)

    def test_cleanup_waits_for_existing_reduce_only_close(self):
        position = {"inst_id": SYMBOL, "size": 1}
        reducing = {
            "inst_id": SYMBOL,
            "client_order_id": "existing-close",
            "status": "live",
            "reduce_only": True,
            "order_kind": "standard",
        }
        active = {"positions": [position], "orders": [reducing], "fills": []}
        flat = {"positions": [], "orders": [], "fills": []}
        with patch.object(
            LIFECYCLE, "synchronized_snapshot",
            side_effect=[active, active, flat, flat, flat],
        ), patch.object(LIFECYCLE, "close_position") as close, \
             patch.object(LIFECYCLE.time, "sleep"):
            cleaned = LIFECYCLE.cleanup_after_failure(
                SimpleNamespace(), SYMBOL, "fixture-source", time.monotonic() + 5,
            )
        self.assertTrue(cleaned)
        close.assert_not_called()

    def test_private_fill_requires_matching_raw_websocket_evidence(self):
        evidence = LIFECYCLE.wait_for_private_fill(
            StreamEvidenceApi(), time.monotonic() + 1, "open-1",
            "2026-09-19T00:00:00+00:00", "fixture open fill",
            require_algo=True,
            algo_before="2026-09-19T00:00:00+00:00",
        )
        self.assertEqual(evidence["private_last_message_at"], "2026-09-19T00:00:01+00:00")
        self.assertEqual(evidence["algo_last_message_at"], "2026-09-19T00:00:02+00:00")
        self.assertEqual(evidence["protection_client_id"], "protection-1")

    def test_missing_stale_unmatched_and_rest_only_events_cannot_pass(self):
        for case in (
            "no_algo_message", "algo_disconnected", "wrong_protection",
            "rest_protection", "missing_fill", "wrong_fill", "invalid_fill", "stale_private",
        ):
            with self.subTest(case=case):
                api = StreamEvidenceApi()
                if case == "no_algo_message":
                    api.algo["last_message_at"] = None
                elif case == "algo_disconnected":
                    api.algo["connected"] = False
                elif case == "wrong_protection":
                    api.protection["client_order_id"] = "unrelated-protection"
                elif case == "rest_protection":
                    api.protection["source"] = "okx-algo-rest"
                elif case == "missing_fill":
                    api.account["fills"] = []
                elif case == "wrong_fill":
                    api.account["fills"][0]["clOrdId"] = "unrelated-order"
                elif case == "invalid_fill":
                    api.account["fills"][0]["fillSz"] = "NaN"
                else:
                    api.account["last_message_at"] = "2026-09-19T00:00:00+00:00"
                with patch.object(LIFECYCLE.time, "monotonic", side_effect=[0, 2]), \
                     patch.object(LIFECYCLE.time, "sleep"), \
                     self.assertRaisesRegex(LIFECYCLE.DemoLifecycleError, "private WebSocket"):
                    LIFECYCLE.wait_for_private_fill(
                        api, 1, "open-1", "2026-09-19T00:00:00+00:00",
                        "fixture open fill", require_algo=True, algo_before=None,
                    )

    def test_cleanup_waits_for_opening_cancellation_before_closing(self):
        opening = {
            "inst_id": SYMBOL, "client_order_id": "opening-1",
            "status": "live", "reduce_only": False,
        }
        pending = {"positions": [{"inst_id": SYMBOL, "size": 1}], "orders": [opening], "fills": []}
        canceled = copy.deepcopy(pending)
        canceled["orders"][0]["status"] = "canceled"
        flat = {"positions": [], "orders": [{
            "inst_id": SYMBOL, "client_order_id": "close-1", "status": "filled",
        }], "fills": []}
        snapshots = [pending, pending, canceled, flat, flat, flat]
        api = SimpleNamespace(post=Mock(return_value={"accepted": True}))
        with patch.object(LIFECYCLE, "synchronized_snapshot", side_effect=snapshots) as sync, \
             patch.object(LIFECYCLE.time, "sleep"), \
             patch.object(LIFECYCLE, "close_position") as close:
            def verified_close(*_args):
                self.assertGreaterEqual(sync.call_count, 3)
                return "close-1"
            close.side_effect = verified_close
            self.assertTrue(LIFECYCLE.cleanup_after_failure(
                api, SYMBOL, "fixture-source", time.monotonic() + 5,
            ))
        close.assert_called_once()
        api.post.assert_called_once_with("/execution/orders/opening-1/cancel")

    def test_emergency_stop_is_attempted_even_when_worker_control_fails(self):
        class LockApi:
            def __init__(self):
                self.posts = []

            def post(self, path, payload):
                self.posts.append(path)
                if path == "/worker/control":
                    raise LIFECYCLE.DemoLifecycleError("fixture worker failure")

            def get(self, path):
                if path == "/safety/status":
                    return {"emergency_stopped": True, "order_submission_allowed": False}
                return {"enabled": False, "running": False}

        api = LockApi()
        self.assertTrue(LIFECYCLE.emergency_lock(api))
        self.assertEqual(api.posts, ["/safety/emergency-stop", "/worker/control"])

    def test_expired_phase_never_sends_another_http_request(self):
        api = LIFECYCLE.ApiClient("http://127.0.0.1:8000", "fixture-admin")
        api.deadline = time.monotonic() - 1
        with patch.object(api.opener, "open") as send, \
             self.assertRaisesRegex(LIFECYCLE.DemoLifecycleError, "timed out"):
            api.post("/execution/signals", {"dry_run": False})
        send.assert_not_called()

    def test_redirects_and_nonfinite_positions_are_rejected(self):
        with self.assertRaisesRegex(LIFECYCLE.DemoLifecycleError, "redirects"):
            LIFECYCLE.RejectRedirects().redirect_request(
                None, None, 302, "", {}, "https://outside.invalid",
            )
        for value in ("NaN", "Infinity", None, "invalid"):
            with self.subTest(value=value), \
                 self.assertRaises(LIFECYCLE.DemoLifecycleError):
                LIFECYCLE.positions_for([{"inst_id": SYMBOL, "size": value}], SYMBOL)

    def test_near_full_order_history_is_rejected_before_any_trade(self):
        status = {
            "trading_mode": "demo", "execution_enabled": True,
            "live_safety": {"allowed": False},
            "integrations": {"tradingview": {"enabled": False}},
            "market_stream": {"connected": True, "fresh": True},
            "account_stream": {
                "configured": True, "connected": True, "authenticated": True,
                "account_ready": True,
            },
            "algo_stream": {"configured": True, "connected": True, "authenticated": True},
        }
        api = SimpleNamespace(get=Mock(side_effect=[
            status,
            {"enabled": False, "running": False, "dry_run": True},
            {"emergency_stopped": False, "order_submission_allowed": True},
            {"configured": True, "demo": True, "positions": []},
        ]), post=Mock())
        snapshot = {"positions": [], "orders": [{} for _ in range(481)], "fills": []}
        with patch.object(LIFECYCLE, "synchronized_snapshot", return_value=snapshot), \
             self.assertRaisesRegex(LIFECYCLE.DemoLifecycleError, "room for new"):
            LIFECYCLE.verify_initial_state(api, SYMBOL)
        api.post.assert_not_called()


@unittest.skipIf(os.name == "nt", "The API acceptance fixture requires POSIX sockets")
class DemoLifecycleAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.exchange = ExchangeServer()
        self.exchange.autofill = True
        self.exchange.private_paused = False
        self.addAsyncCleanup(self.exchange.close)
        await self.exchange.start()
        self.api = ApiProcess(self.directory.name, self.exchange)
        self.addAsyncCleanup(self.api.stop)
        await self.api.start()

    def environment(self):
        return {
            **os.environ,
            **self.api.environment,
            "ADMIN_API_TOKEN": ADMIN_TOKEN,
            "TRADING_MODE": "demo",
            "OKX_DEMO": "true",
            "EXECUTION_ENABLED": "true",
            "LIVE_TRADING_ENABLED": "false",
            "AUTO_TRADING_ENABLED": "false",
            "AUTO_TRADING_DRY_RUN": "true",
            "TRADINGVIEW_ENABLED": "false",
        }

    async def run_smoke(self, confirmation=CONFIRMATION, side="long"):
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(SCRIPT),
            "--origin", self.api.origin,
            "--inst-id", SYMBOL,
            "--side", side,
            "--timeout", "45",
            "--confirm", confirmation,
            "--output", "-",
            cwd=ROOT,
            env=self.environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 140)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        return process.returncode, stdout.decode(), stderr.decode()

    async def test_minimum_demo_order_protection_close_and_final_lock(self):
        code, stdout, stderr = await self.run_smoke()
        self.assertEqual(code, 0, stderr)
        report = json.loads(stdout)
        self.assertEqual(report["scope"], "okx_demo_order_lifecycle")
        self.assertTrue(report["demo"])
        self.assertTrue(report["private_stream_verified"])
        self.assertTrue(report["algo_stream_verified"])
        self.assertTrue(report["dry_run_verified"])
        self.assertTrue(report["exchange_preflight_verified"])
        self.assertTrue(report["open_fill_verified"])
        self.assertTrue(report["native_protection_verified"])
        self.assertTrue(report["close_fill_verified"])
        self.assertTrue(report["position_flat_verified"])
        self.assertTrue(report["protection_terminal_verified"])
        self.assertTrue(report["cleanup_completed"])
        self.assertTrue(report["emergency_stopped"])
        self.assertTrue(report["order_lifecycle_verified"])
        self.assertTrue(report["trading_performed"])
        self.assertFalse(report["real_funds_used"])
        self.assertFalse(report["live_execution_allowed"])

        submissions = self.exchange.order_submissions
        self.assertEqual(len(submissions), 2)
        self.assertIn("attachAlgoOrds", submissions[0])
        self.assertTrue(submissions[1]["reduceOnly"])
        self.assertEqual({row["state"] for row in self.exchange.algos.values()}, {"canceled"})
        self.assertEqual((await self.api.request("GET", "/positions"))["data"], [])
        self.assertEqual(len((await self.api.request("GET", "/fills"))["data"]), 2)
        safety = await self.api.request("GET", "/safety/status")
        self.assertTrue(safety["emergency_stopped"])
        self.assertFalse(safety["order_submission_allowed"])
        worker = await self.api.request("GET", "/worker/status")
        self.assertFalse(worker["enabled"] or worker["running"])
        self.assertEqual(self.exchange.errors, [])

    async def test_short_side_also_finishes_flat_and_locked(self):
        code, stdout, stderr = await self.run_smoke(side="short")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["side"], "short")
        self.assertEqual([row["side"] for row in self.exchange.order_submissions], ["sell", "buy"])
        self.assertTrue(self.exchange.order_submissions[1]["reduceOnly"])
        self.assertEqual((await self.api.request("GET", "/positions"))["data"], [])
        self.assertTrue((await self.api.request("GET", "/safety/status"))["emergency_stopped"])

    async def assert_lost_response_cleanup(self, action):
        original = LIFECYCLE.ApiClient.post
        lost = False

        def lose_response(client, path, payload=None):
            nonlocal lost
            result = original(client, path, payload)
            if (
                not lost and path == "/execution/signals"
                and payload.get("dry_run") is False
                and payload["signal"]["action"] == action
            ):
                lost = True
                raise LIFECYCLE.DemoLifecycleError("fixture response lost after acceptance")
            return result

        with patch.dict(os.environ, self.environment()), \
             patch.object(LIFECYCLE.ApiClient, "post", lose_response):
            with self.assertRaisesRegex(LIFECYCLE.DemoLifecycleError, "fixture response lost"):
                await asyncio.to_thread(
                    LIFECYCLE.run_probe, origin=self.api.origin, inst_id=SYMBOL, side="long",
                    timeout=45, confirmation=CONFIRMATION, output=None,
                )
        self.assertTrue(lost)
        self.assertEqual(len(self.exchange.order_submissions), 2)
        self.assertTrue(self.exchange.order_submissions[1]["reduceOnly"])
        self.assertEqual((await self.api.request("GET", "/positions"))["data"], [])
        self.assertEqual({row["state"] for row in self.exchange.algos.values()}, {"canceled"})
        self.assertTrue((await self.api.request("GET", "/safety/status"))["emergency_stopped"])
        self.assertEqual(self.exchange.errors, [])

    async def test_lost_open_response_still_closes_and_locks(self):
        await self.assert_lost_response_cleanup("open_long")

    async def test_lost_close_response_never_creates_a_second_close(self):
        await self.assert_lost_response_cleanup("close")

    async def test_confirmation_is_exact_and_never_submits_on_mismatch(self):
        code, _stdout, stderr = await self.run_smoke("wrong-confirmation")
        self.assertEqual(code, 1)
        self.assertIn("Exact Demo lifecycle confirmation is required", stderr)
        self.assertEqual(self.exchange.order_submissions, [])
        self.assertFalse((await self.api.request("GET", "/safety/status"))["emergency_stopped"])


if __name__ == "__main__":
    unittest.main()
