import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import main as api_main, tradingview
from app.state_store import StateStore
from app.tradingview_worker import TradingViewWorker


def alert(**overrides):
    data = {
        "secret": "test-secret", "alert_id": "alert-1", "symbol": "BINANCE:BTCUSDT.P",
        "action": "open_long", "confidence": 0.9, "leverage": 2, "position_pct": 5,
        "entry_price": 50000, "stop_loss": 49000, "take_profit": 52000, "size": 0.01,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data.update(overrides)
    return data


def signal_for(**overrides):
    body = json.dumps(alert(**overrides)).encode()
    return tradingview.to_signal(tradingview.parse_body(body), body=body)


class TradingViewNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"TRADINGVIEW_SYMBOLS": "BTC-USDT-SWAP,ETH-USDT-SWAP"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_normalizes_common_symbols(self):
        self.assertEqual(tradingview.normalize_instrument("BINANCE:BTCUSDT.P"), "BTC-USDT-SWAP")
        self.assertEqual(tradingview.normalize_instrument("OKX:ETH-USDT-SWAP"), "ETH-USDT-SWAP")

    def test_signal_is_structured_and_has_server_expiry(self):
        signal, size, side, alert_id = signal_for()
        self.assertEqual(signal.source, "tradingview")
        self.assertEqual(signal.inst_id, "BTC-USDT-SWAP")
        self.assertEqual(size, 0.01)
        self.assertIsNone(side)
        self.assertEqual(alert_id, "alert-1")
        self.assertGreater(signal.expires_at, signal.created_at)

    def test_close_requires_direction(self):
        with self.assertRaisesRegex(tradingview.TradingViewWebhookError, "close_side_required"):
            signal_for(action="close", entry_price=None, stop_loss=None, take_profit=None)

    def test_open_direction_cannot_be_overridden_by_side(self):
        for action, side, stop, target in [
            ("open_long", "sell", 49000, 52000),
            ("open_short", "buy", 52000, 49000),
        ]:
            with self.subTest(action=action), self.assertRaisesRegex(
                tradingview.TradingViewWebhookError, "side_action_mismatch"
            ):
                signal_for(action=action, side=side, stop_loss=stop, take_profit=target)
        self.assertIsNone(signal_for(action="open_long", side="buy")[2])
        self.assertEqual(signal_for(action="close", side="buy")[2], "buy")

    def test_missing_old_naive_and_future_timestamp_rejected(self):
        for timestamp, reason in [
            (None, "timestamp_required"),
            ("2020-01-01T00:00:00Z", "alert_too_old"),
            (datetime.now().isoformat(), "timestamp_timezone_required"),
            ((datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat(), "alert_too_old"),
        ]:
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(tradingview.TradingViewWebhookError, reason):
                signal_for(timestamp=timestamp)

    def test_expiry_is_capped_by_server_and_original_timestamp(self):
        now = datetime.now(timezone.utc)
        with patch.dict(os.environ, {"TRADINGVIEW_SIGNAL_TTL_SECONDS": "30", "TRADINGVIEW_MAX_AGE_SECONDS": "60"}):
            signal, *_ = signal_for(timestamp=(now - timedelta(seconds=50)).isoformat(),
                                    expires_at=(now + timedelta(days=1)).isoformat())
        self.assertLessEqual(signal.expires_at, now + timedelta(seconds=10))

    def test_explicit_naive_or_expired_expiry_rejected(self):
        for expiry in [datetime.now().isoformat(), "2020-01-01T00:00:00Z"]:
            with self.subTest(expiry=expiry), self.assertRaises(tradingview.TradingViewWebhookError):
                signal_for(expires_at=expiry)

    def test_empty_or_absent_identifier_rejected(self):
        for identifier in [None, "   "]:
            with self.subTest(identifier=identifier), self.assertRaisesRegex(tradingview.TradingViewWebhookError, "alert_id_required"):
                signal_for(alert_id=identifier)

    def test_allowlist_is_closed_even_when_explicitly_empty(self):
        for symbols in ["", "ETH-USDT-SWAP"]:
            with patch.dict(os.environ, {"TRADINGVIEW_SYMBOLS": symbols}):
                with self.assertRaises(tradingview.TradingViewWebhookError):
                    signal_for()

    def test_parser_rejects_oversize_duplicate_nonfinite_and_deep_json(self):
        for body in [b"x" * (tradingview.MAX_BODY_BYTES + 1),
                     b'{"action":"hold","action":"open_long"}', b'{"size":NaN}',
                     b"[" * 2000 + b"]" * 2000]:
            with self.subTest(body=body[:40]), self.assertRaises(tradingview.TradingViewWebhookError):
                tradingview.parse_body(body)

    def test_unicode_secret_never_raises_server_error(self):
        payload = tradingview.parse_body(json.dumps(alert(secret="\u79d8\u94a5")).encode())
        with patch.dict(os.environ, {"TRADINGVIEW_WEBHOOK_SECRET": "test-secret"}):
            self.assertFalse(tradingview.verify_secret(payload, None))

    def test_nonfinite_default_size_and_context_rejected(self):
        with patch.dict(os.environ, {"TRADINGVIEW_DEFAULT_SIZE": "inf"}):
            with self.assertRaisesRegex(tradingview.TradingViewWebhookError, "invalid_default_size"):
                signal_for(size=None)
        for key, value in [("TRADINGVIEW_ACCOUNT_EQUITY", "nan"), ("TRADINGVIEW_CURRENT_NOTIONAL", "-1")]:
            with patch.dict(os.environ, {key: value}):
                with self.assertRaisesRegex(tradingview.TradingViewWebhookError, "invalid_simulation_context"):
                    tradingview.numeric_context()

    def test_dry_run_requires_explicit_false_even_without_deployment_preflight(self):
        payload = tradingview.parse_body(json.dumps(alert(dry_run=False)).encode())
        for value in ("", "tru", "0", "no", "invalid", "true"):
            with self.subTest(value=value), patch.dict(os.environ, {
                "TRADINGVIEW_EXECUTION_ENABLED": "true", "TRADINGVIEW_DRY_RUN": value,
            }):
                self.assertTrue(tradingview.execution_dry_run(payload))
                self.assertTrue(tradingview.status()["dry_run"])
        with patch.dict(os.environ, {
            "TRADINGVIEW_EXECUTION_ENABLED": "true", "TRADINGVIEW_DRY_RUN": " false ",
        }):
            self.assertFalse(tradingview.execution_dry_run(payload))
            self.assertFalse(tradingview.status()["dry_run"])
            payload.dry_run = True
            self.assertTrue(tradingview.execution_dry_run(payload))


class TradingViewEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.execution = SimpleNamespace(
            trade_client=SimpleNamespace(base_url="https://www.okx.com", demo=True,
                                         trading_mode="demo", api_key="test-key"),
            submit_signal=AsyncMock(return_value={
                "accepted": True, "order": {"status": "preview", "client_order_id": "opdtest"},
            }),
        )
        self.worker = TradingViewWorker(self.store, self.execution, lambda: True)
        patches = [
            patch.dict(os.environ, {
                "TRADINGVIEW_ENABLED": "true", "TRADINGVIEW_WEBHOOK_SECRET": "test-secret",
                "TRADINGVIEW_EXECUTION_ENABLED": "false", "TRADINGVIEW_DRY_RUN": "true",
                "TRADINGVIEW_SYMBOLS": "BTC-USDT-SWAP,ETH-USDT-SWAP", "ADMIN_API_TOKEN": "test-admin",
            }),
            patch.object(api_main, "state_store", self.store),
            patch.object(api_main, "tradingview_worker", self.worker),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.addAsyncCleanup(self.worker.close)

    async def post(self, payload=None):
        return await self.client.post("/api/v1/integrations/tradingview/webhook", json=payload or alert())

    async def test_receipt_returns_before_exchange_and_only_queues_structured_signal(self):
        response = await asyncio.wait_for(self.post(alert(unused_private="must-not-persist")), 1)
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["status"], "queued")
        self.assertTrue(response.json()["accepted"])
        self.assertIsNone(response.json()["execution_accepted"])
        self.execution.submit_signal.assert_not_awaited()
        self.assertTrue(await self.worker.run_once())
        call = self.execution.submit_signal.await_args
        self.assertEqual(call.args[0].source, "tradingview")
        self.assertEqual(call.kwargs["idempotency_key"], "tradingview:alert-1")
        self.assertTrue(call.kwargs["dry_run"])
        self.assertEqual(self.store.list_tradingview_alerts()[0]["status"], "preview")
        self.assertNotIn(b"test-secret", self.store.path.read_bytes())
        self.assertNotIn(b"must-not-persist", self.store.path.read_bytes())

    async def test_wrong_secret_and_disabled_receiver_never_enqueue(self):
        self.assertEqual((await self.post(alert(secret="wrong"))).status_code, 401)
        with patch.dict(os.environ, {"TRADINGVIEW_ENABLED": "false"}):
            self.assertEqual((await self.post()).status_code, 404)
        self.assertEqual(self.store.list_tradingview_alerts(), [])

    async def test_oversized_chunked_body_rejected(self):
        async def chunks():
            for _ in range(3):
                yield b"x" * 8192
        response = await self.client.post("/api/v1/integrations/tradingview/webhook", content=chunks())
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.store.list_tradingview_alerts(), [])

    async def test_slow_body_has_bounded_timeout(self):
        async def chunks():
            yield b"{"
            await asyncio.sleep(2)
        response = await asyncio.wait_for(self.client.post(
            "/api/v1/integrations/tradingview/webhook", content=chunks(),
        ), 1.8)
        self.assertEqual(response.status_code, 408)
        self.assertEqual(self.store.list_tradingview_alerts(), [])

    async def test_busy_database_does_not_acknowledge_unpersisted_alert(self):
        with self.store._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            response = await asyncio.wait_for(self.post(), 2)
            self.assertEqual(response.status_code, 503)
        self.assertEqual(self.store.list_tradingview_alerts(), [])

    async def test_full_queue_still_allows_duplicate_receipts(self):
        payload = alert()
        await self.post(payload)
        with self.store._connection() as connection:
            original = connection.execute("SELECT * FROM tradingview_alerts").fetchone()
            connection.executemany(
                """INSERT INTO tradingview_alerts
                    (alert_id, fingerprint, inst_id, action, dry_run, instruction_json, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [(f"other-{index}", original["fingerprint"], original["inst_id"], original["action"],
                  original["dry_run"], original["instruction_json"], original["created_at"], original["updated_at"])
                 for index in range(999)],
            )
        self.assertEqual((await self.post(alert(alert_id="overflow"))).status_code, 429)
        duplicate = await self.post(payload)
        self.assertEqual(duplicate.status_code, 202)
        self.assertTrue(duplicate.json()["idempotent"])

    async def test_duplicate_is_durable_and_content_conflicts_never_execute(self):
        payload = alert()
        self.assertEqual((await self.post(payload)).status_code, 202)
        replay = await self.post(payload)
        self.assertTrue(replay.json()["idempotent"])
        for change in [{"symbol": "ETHUSDT"}, {"stop_loss": 48000}, {"leverage": 3}, {"dry_run": False}]:
            with self.subTest(change=change):
                self.assertEqual((await self.post({**payload, **change})).status_code, 409)
        await self.worker.run_once()
        self.assertFalse(await self.worker.run_once())
        self.assertEqual((await self.post(payload)).json()["status"], "preview")
        self.execution.submit_signal.assert_awaited_once()

    async def test_open_side_alias_deduplicates_but_conflict_is_rejected(self):
        payload = alert()
        await self.post(payload)
        duplicate = await self.post({**payload, "side": "buy"})
        self.assertEqual(duplicate.status_code, 202)
        self.assertTrue(duplicate.json()["idempotent"])
        conflict = await self.post({**payload, "side": "sell"})
        self.assertEqual(conflict.status_code, 422)
        self.assertEqual(conflict.json()["detail"], "side_action_mismatch")
        self.execution.submit_signal.assert_not_awaited()

    async def test_concurrent_delivery_has_one_claim(self):
        payload = alert()
        results = await asyncio.gather(*(self.post(payload) for _ in range(8)))
        self.assertTrue(all(row.status_code == 202 for row in results))
        self.assertEqual(sum(not row.json()["idempotent"] for row in results), 1)
        other = TradingViewWorker(StateStore(str(self.store.path)), self.execution, lambda: True)
        await asyncio.gather(self.worker.run_once(), other.run_once())
        self.execution.submit_signal.assert_awaited_once()

    async def test_queued_alert_survives_database_reopen(self):
        await self.post()
        worker = TradingViewWorker(StateStore(str(self.store.path)), self.execution, lambda: True)
        self.assertTrue(await worker.run_once())
        self.assertEqual(self.store.list_tradingview_alerts()[0]["status"], "preview")

    async def test_stale_processing_is_not_replayed_after_crash(self):
        await self.post()
        self.store.claim_tradingview_alert("exited-worker", 0)
        self.assertFalse(await self.worker.run_once())
        self.assertEqual(self.store.list_tradingview_alerts()[0]["status"], "interrupted")
        self.execution.submit_signal.assert_not_awaited()
        self.assertFalse(self.store.finish_tradingview_alert("alert-1", "exited-worker", "submitted", {}))

    async def test_account_or_mode_switch_rejects_queued_alert(self):
        await self.post()
        self.execution.trade_client.api_key = "another-account"
        await self.worker.run_once()
        self.assertEqual(self.store.list_tradingview_alerts()[0]["reasons"], ["execution_scope_changed"])
        self.execution.submit_signal.assert_not_awaited()

    async def test_more_permissive_config_never_escalates_accepted_dry_run(self):
        await self.post(alert(dry_run=False))
        with patch.dict(os.environ, {"TRADINGVIEW_EXECUTION_ENABLED": "true", "TRADINGVIEW_DRY_RUN": "false"}):
            await self.worker.run_once()
        self.assertTrue(self.execution.submit_signal.await_args.kwargs["dry_run"])

    async def test_malformed_dry_run_flag_never_queues_executable_instruction(self):
        with patch.dict(os.environ, {"TRADINGVIEW_EXECUTION_ENABLED": "true", "TRADINGVIEW_DRY_RUN": "tru"}):
            response = await self.post(alert(dry_run=False))
            self.assertEqual(response.status_code, 202)
            self.assertTrue(response.json()["dry_run"])
            await self.worker.run_once()
        self.assertTrue(self.execution.submit_signal.await_args.kwargs["dry_run"])
        self.assertEqual(self.store.list_tradingview_alerts()[0]["status"], "preview")

    async def test_execution_disabled_after_receipt_rejects(self):
        with patch.dict(os.environ, {"TRADINGVIEW_EXECUTION_ENABLED": "true", "TRADINGVIEW_DRY_RUN": "false"}):
            await self.post()
        await self.worker.run_once()
        self.execution.submit_signal.assert_not_awaited()
        self.assertEqual(self.store.list_tradingview_alerts()[0]["reasons"], ["tradingview_execution_disabled"])

    async def test_expired_queued_alert_never_executes(self):
        await self.post()
        with self.store._connection() as connection:
            row = connection.execute("SELECT instruction_json FROM tradingview_alerts").fetchone()
            instruction = json.loads(row[0])
            instruction["signal"]["created_at"] = "2020-01-01T00:00:00Z"
            instruction["signal"]["expires_at"] = "2020-01-01T00:05:00Z"
            connection.execute("UPDATE tradingview_alerts SET instruction_json = ?", (json.dumps(instruction),))
        await self.worker.run_once()
        self.assertEqual(self.store.list_tradingview_alerts()[0]["status"], "expired")
        self.execution.submit_signal.assert_not_awaited()

    async def test_hold_is_observed_without_executor(self):
        await self.post(alert(action="hold", dry_run=True))
        await self.worker.run_once()
        self.assertEqual(self.store.list_tradingview_alerts()[0]["status"], "observed")
        self.execution.submit_signal.assert_not_awaited()

    async def test_execution_exception_is_unconfirmed_and_never_retried(self):
        self.execution.submit_signal.side_effect = TimeoutError("secret-must-not-be-logged")
        payload = alert()
        await self.post(payload)
        await self.worker.run_once()
        self.assertEqual((await self.post(payload)).json()["status"], "unconfirmed")
        self.assertFalse(await self.worker.run_once())
        self.execution.submit_signal.assert_awaited_once()
        self.assertNotIn(b"secret-must-not-be-logged", self.store.path.read_bytes())
        await self.worker.start()
        await asyncio.sleep(.05)
        self.assertEqual(self.worker.snapshot()["last_error"], "TimeoutError")

    async def test_existing_unconfirmed_order_is_not_reported_as_rejected(self):
        self.execution.submit_signal.return_value = {
            "accepted": False, "idempotent": True,
            "reasons": ["order_submission_unconfirmed"],
            "order": {"client_order_id": "opdpending", "status": "submission_unknown"},
        }
        payload = alert()
        with patch.dict(os.environ, {"TRADINGVIEW_EXECUTION_ENABLED": "true", "TRADINGVIEW_DRY_RUN": "false"}):
            await self.post(payload)
            await self.worker.run_once()
        receipt = (await self.post(payload)).json()
        self.assertEqual(receipt["status"], "unconfirmed")
        self.assertIsNone(receipt["execution_accepted"])
        self.assertEqual(receipt["client_order_id"], "opdpending")
        self.assertEqual(receipt["reasons"], ["order_submission_unconfirmed"])
        self.assertFalse(await self.worker.run_once())
        self.execution.submit_signal.assert_awaited_once()

    async def test_exception_receipt_uses_durable_order_evidence(self):
        for order_status, receipt_status, accepted in [
            ("rejected", "rejected", False),
            ("submitting", "unconfirmed", None),
            ("submission_unknown", "unconfirmed", None),
            ("filled", "submitted", True),
        ]:
            with self.subTest(order_status=order_status):
                payload = alert(alert_id=f"durable-{order_status}")
                with patch.dict(os.environ, {"TRADINGVIEW_EXECUTION_ENABLED": "true", "TRADINGVIEW_DRY_RUN": "false"}):
                    receipt = (await self.post(payload)).json()

                    async def fail_after_record(*_args, **_kwargs):
                        self.store.save_order({
                            "client_order_id": receipt["client_order_id"], "status": order_status,
                            "inst_id": "BTC-USDT-SWAP", "side": "buy", "size": payload["size"],
                            "pos_side": "net", "td_mode": "isolated", "ord_type": "market",
                        })
                        raise RuntimeError("private-exception-must-not-be-published")

                    self.execution.submit_signal.side_effect = fail_after_record
                    await self.worker.run_once()
                final = (await self.post(payload)).json()
                self.assertEqual(final["status"], receipt_status)
                self.assertIs(final["execution_accepted"], accepted)
                self.assertEqual(final["client_order_id"], receipt["client_order_id"])
                self.assertNotIn("private-exception", json.dumps(final))
                self.assertFalse(await self.worker.run_once())
        self.assertEqual(self.execution.submit_signal.await_count, 4)

    async def test_processing_cancellation_is_durable(self):
        entered = asyncio.Event()
        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.execution.submit_signal.side_effect = blocked
        await self.post()
        task = asyncio.create_task(self.worker.run_once())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.store.list_tradingview_alerts()[0]["status"], "interrupted")
        self.assertFalse(await self.worker.run_once())

    async def test_private_alert_list_requires_admin_and_contains_no_instruction(self):
        await self.post()
        route = "/api/v1/integrations/tradingview/alerts"
        self.assertEqual((await self.client.get(route)).status_code, 401)
        response = await self.client.get(route, headers={"X-Admin-Token": "test-admin"})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("instruction", response.text)
        self.assertNotIn("secret", response.text)
        self.assertEqual(response.json()["data"][0]["status"], "queued")
