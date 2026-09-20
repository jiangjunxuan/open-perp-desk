import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.account_reconciler import AccountReconciler
from app.account_sync import AccountSynchronizer
from app.execution_engine import ExecutionEngine
from app.order_preflight import PreparedExecution
from app.risk_engine import RiskEngine
from app.safety_control import SafetyController
from app.state_store import StateStore
from app.trading_signal import TradeSignal
from tests.fixtures.api_process import ApiProcess, eventually
from tests.fixtures.exchange_server import ExchangeServer, SYMBOL
from tests.test_realtime import next_event


class StreamStatus:
    def __init__(self, *, ready: bool = False, configured: bool = True) -> None:
        self.ready = ready
        self.configured = configured

    @property
    def ready(self):
        return self.connected and self.authenticated

    @ready.setter
    def ready(self, value):
        self.connected = self.authenticated = value

    def snapshot(self):
        raise AssertionError("Readiness must not copy the account ledger")


class PrivateStreamHealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.account_stream = StreamStatus()
        self.algo_stream = StreamStatus()
        self.account = SimpleNamespace(configured=True)
        self.sync = AccountSynchronizer(
            self.store,
            self.account,
            self.account_stream,
            self.algo_stream,
        )
        self.now = 0.0
        self.notifications: list[tuple[str, str, str, dict]] = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    def reconciler(self, notifier=None) -> AccountReconciler:
        return AccountReconciler(
            self.sync,
            self.store,
            notifier=notifier,
            clock=lambda: self.now,
        )

    async def notify(self, event_type, title, content, **kwargs):
        self.notifications.append((event_type, title, content, kwargs))
        return True

    def test_readiness_requires_both_authenticated_private_streams(self):
        self.account_stream.ready = True
        status = self.sync.private_stream_status()
        self.assertFalse(status["ready"])
        self.assertEqual(status["reason_code"], "algo_stream_not_ready")
        self.algo_stream.ready = True
        self.assertTrue(self.sync.private_stream_ready())

    def test_missing_stream_or_unknown_state_is_not_ready(self):
        self.account_stream.ready = self.algo_stream.ready = True
        for stream in (None, object(), SimpleNamespace(configured="true", connected=True, authenticated=True)):
            with self.subTest(stream=stream), patch.object(self.sync, "algo_stream", stream):
                self.assertFalse(self.sync.private_stream_ready())
        for stream in (self.account_stream, self.algo_stream):
            for field in ("connected", "authenticated"):
                with self.subTest(field=field), patch.object(stream, field, False):
                    self.assertFalse(self.sync.private_stream_ready())

    def test_application_executor_uses_the_shared_stream_gate(self):
        from app import main

        self.assertEqual(main.execution_engine.private_stream_ready, main.account_sync.private_stream_ready)
        self.assertEqual(main.execution_engine._private_stream_is_ready(), main.account_sync.private_stream_ready())

    async def test_short_outage_is_silent_and_sustained_outage_is_deduplicated(self):
        with patch.dict(os.environ, {"PRIVATE_STREAM_ALERT_GRACE_SECONDS": "30"}):
            reconciler = self.reconciler(self.notify)
        await reconciler.check_private_stream_health(now=0)
        self.now = 29
        await reconciler.check_private_stream_health()
        self.assertEqual(self.notifications, [])

        self.now = 30
        await reconciler.check_private_stream_health()
        self.assertEqual([row[0] for row in self.notifications], ["private_stream_unavailable"])
        self.now = 90
        await reconciler.check_private_stream_health()
        self.assertEqual(len(self.notifications), 1)

        self.account_stream.ready = self.algo_stream.ready = True
        self.now = 91
        await reconciler.check_private_stream_health()
        self.assertEqual(
            [row[0] for row in self.notifications],
            ["private_stream_unavailable", "private_stream_recovered"],
        )
        self.now = 92
        await reconciler.check_private_stream_health()
        self.assertEqual(len(self.notifications), 2)

    async def test_recovery_before_grace_does_not_emit_an_incident(self):
        with patch.dict(os.environ, {"PRIVATE_STREAM_ALERT_GRACE_SECONDS": "30"}):
            reconciler = self.reconciler(self.notify)
        await reconciler.check_private_stream_health(now=0)
        self.account_stream.ready = self.algo_stream.ready = True
        self.now = 10
        await reconciler.check_private_stream_health()
        self.assertEqual(self.notifications, [])
        self.assertFalse(reconciler.snapshot()["private_stream_alerted"])

    async def test_later_outage_has_its_own_grace_and_partial_recovery_does_not_reset_it(self):
        with patch.dict(os.environ, {"PRIVATE_STREAM_ALERT_GRACE_SECONDS": "30"}):
            reconciler = self.reconciler(self.notify)
        self.account_stream.ready = self.algo_stream.ready = True
        await reconciler.check_private_stream_health(now=0)
        self.account_stream.ready = self.algo_stream.ready = False
        await reconciler.check_private_stream_health(now=100)
        self.account_stream.ready = True
        await reconciler.check_private_stream_health(now=129)
        self.assertEqual(self.notifications, [])
        await reconciler.check_private_stream_health(now=130)
        self.assertEqual(len(self.notifications), 1)
        self.algo_stream.ready = True
        await reconciler.check_private_stream_health(now=131)
        self.algo_stream.ready = False
        await reconciler.check_private_stream_health(now=200)
        await reconciler.check_private_stream_health(now=229)
        self.assertEqual(len(self.notifications), 2)
        await reconciler.check_private_stream_health(now=230)
        self.assertEqual(
            [row[0] for row in self.notifications],
            ["private_stream_unavailable", "private_stream_recovered", "private_stream_unavailable"],
        )

    async def test_unconfigured_streams_do_not_raise_alert(self):
        self.account_stream.configured = False
        with patch.dict(os.environ, {"PRIVATE_STREAM_ALERT_GRACE_SECONDS": "0"}):
            reconciler = self.reconciler(self.notify)
        await reconciler.check_private_stream_health(now=0)
        self.assertEqual(self.notifications, [])

    async def test_notification_failure_is_audited_without_raw_provider_details(self):
        async def failing_notifier(*_args, **_kwargs):
            raise RuntimeError("private-proxy-secret")

        with patch.dict(os.environ, {"PRIVATE_STREAM_ALERT_GRACE_SECONDS": "0"}):
            reconciler = self.reconciler(failing_notifier)
        await reconciler.check_private_stream_health(now=0)
        serialized = json.dumps(self.store.list_audit(), ensure_ascii=True)
        self.assertIn("private_stream_unavailable", serialized)
        self.assertIn("notification_dispatch_failed", serialized)
        self.assertNotIn("private-proxy-secret", serialized)
        self.assertNotIn("ConnectionClosed", serialized)
        self.assertTrue(reconciler.snapshot()["private_stream_alerted"])

    async def test_disabled_pushplus_is_silent_and_not_reported_as_delivery_failure(self):
        engine = ExecutionEngine(self.store, RiskEngine(), object(), SimpleNamespace(configured=False))
        with patch.dict(os.environ, {"PRIVATE_STREAM_ALERT_GRACE_SECONDS": "0"}):
            reconciler = self.reconciler(engine.notify_event)
        await reconciler.check_private_stream_health(now=0)
        await reconciler.check_private_stream_health(now=100)
        self.assertEqual(
            [row["event_type"] for row in self.store.list_audit()],
            ["private_stream_unavailable"],
        )

    async def test_failed_provider_attempt_is_audited_once_not_retried_every_tick(self):
        sender = SimpleNamespace(configured=True, send=AsyncMock(side_effect=RuntimeError("private-secret")))
        engine = ExecutionEngine(self.store, RiskEngine(), object(), sender)
        with patch.dict(os.environ, {"PRIVATE_STREAM_ALERT_GRACE_SECONDS": "0"}):
            reconciler = self.reconciler(engine.notify_event)
        for now in (0, 1, 2, 30):
            await reconciler.check_private_stream_health(now=now)
        sender.send.assert_awaited_once()
        events = [row["event_type"] for row in self.store.list_audit()]
        self.assertEqual(events.count("notification_failed"), 1)
        self.assertNotIn("notification_dispatch_failed", events)
        self.assertNotIn("private-secret", json.dumps(self.store.list_audit()))

    async def test_monitor_is_independent_of_rest_and_wakes_on_events_and_restarts(self):
        with patch.dict(os.environ, {"PRIVATE_STREAM_ALERT_GRACE_SECONDS": "0"}):
            reconciler = self.reconciler(self.notify)
        reconciler._stream_health_poll_seconds = 3600
        rest_started = asyncio.Event()

        async def blocked_rest():
            rest_started.set()
            await asyncio.Event().wait()

        async def wait_notifications(count):
            async with asyncio.timeout(2):
                while len(self.notifications) < count:
                    await asyncio.sleep(.01)

        with patch.object(reconciler, "run_once", side_effect=blocked_rest), \
             patch.object(self.sync, "sync_stream", Mock()):
            try:
                await reconciler.start()
                await asyncio.wait_for(rest_started.wait(), 2)
                await wait_notifications(1)
                tasks = (reconciler._task, reconciler._stream_task, reconciler._health_task)
                await reconciler.start()
                self.assertEqual(tasks, (reconciler._task, reconciler._stream_task, reconciler._health_task))
                self.account_stream.ready = self.algo_stream.ready = True
                reconciler.notify_stream()
                await wait_notifications(2)
                await reconciler.stop()
                self.assertTrue(all(task.done() for task in tasks))
                self.account_stream.ready = self.algo_stream.ready = False
                await reconciler.start()
                await wait_notifications(3)
                self.assertEqual(self.notifications[-1][0], "private_stream_unavailable")
            finally:
                await reconciler.stop()
            self.assertIsNone(reconciler._health_task)
            self.assertFalse(reconciler.snapshot()["running"])

    async def test_stop_cancels_an_inflight_notification(self):
        started, canceled = asyncio.Event(), asyncio.Event()

        async def blocked_notifier(*_args, **_kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                canceled.set()

        with patch.dict(os.environ, {"PRIVATE_STREAM_ALERT_GRACE_SECONDS": "0"}):
            reconciler = self.reconciler(blocked_notifier)
        with patch.object(reconciler, "run_once", AsyncMock()), \
             patch.object(self.sync, "sync_stream", Mock()):
            try:
                await reconciler.start()
                await asyncio.wait_for(started.wait(), 2)
            finally:
                await asyncio.wait_for(reconciler.stop(), 2)
        self.assertTrue(canceled.is_set())
        self.assertNotIn("notification_dispatch_failed", json.dumps(self.store.list_audit()))


class TradeClient:
    enabled = True
    configured = True
    demo = True
    trading_mode = "demo"

    def __init__(self) -> None:
        self.orders = []

    async def set_leverage(self, *_args):
        return {}

    async def place_order(self, order):
        self.orders.append(order)
        return {"code": "0", "data": [{"ordId": "exchange-1"}]}


class StreamGateExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.trade = TradeClient()
        self.ready = False
        self.signal = TradeSignal(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            confidence=0.9,
            leverage=2,
            position_pct=5,
            entry_price=50000,
            stop_loss=49000,
            take_profit=52000,
            source="test",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def engine(self, *, preflight=None) -> ExecutionEngine:
        return ExecutionEngine(
            self.store,
            RiskEngine(),
            self.trade,
            SimpleNamespace(configured=False),
            SafetyController(self.store),
            preflight=preflight,
            private_stream_ready=lambda: self.ready,
        )

    async def test_real_open_is_blocked_but_dry_run_is_still_available(self):
        engine = self.engine()
        blocked = await engine.submit_signal(
            self.signal,
            account_equity=1000,
            daily_pnl_pct=0,
            size=0.2,
            dry_run=False,
        )
        self.assertFalse(blocked["accepted"])
        self.assertEqual(blocked["reasons"], ["private_stream_not_ready"])
        self.assertEqual(self.trade.orders, [])

        preview = await engine.submit_signal(
            self.signal,
            account_equity=1000,
            daily_pnl_pct=0,
            size=0.2,
            dry_run=True,
        )
        self.assertTrue(preview["accepted"])
        self.assertTrue(preview["dry_run"])

    async def test_missing_invalid_or_raising_callback_fails_closed(self):
        def failing():
            raise RuntimeError("private-secret")

        engine = self.engine()
        for callback in (None, lambda: None, lambda: "true", failing):
            with self.subTest(callback=callback):
                engine.private_stream_ready = callback
                result = await engine.submit_signal(
                    self.signal, account_equity=1000, daily_pnl_pct=0, size=.2,
                )
                self.assertEqual(result["reasons"], ["private_stream_not_ready"])
        self.assertEqual(self.trade.orders, [])
        self.assertNotIn("private-secret", json.dumps(self.store.list_audit()))

    async def test_repeated_opening_attempts_do_not_bypass_notification_grace(self):
        engine = self.engine()
        engine.notify_event = AsyncMock()
        for attempt in range(3):
            result = await engine.submit_signal(
                self.signal, account_equity=1000, daily_pnl_pct=0, size=.2,
                idempotency_key=f"opening-{attempt}",
            )
            self.assertEqual(result["reasons"], ["private_stream_not_ready"])
        engine.notify_event.assert_not_awaited()
        self.assertEqual(len(self.store.list_audit()), 3)
        self.assertEqual(self.store.list_orders(), [])

    async def test_disconnect_during_leverage_preparation_prevents_final_send(self):
        async def prepare(signal, *_args, **_kwargs):
            return PreparedExecution(
                signal=signal, account_equity=1000, daily_pnl_pct=0,
                current_notional=0, order_notional=100, side="buy",
                pos_side="net", td_mode="isolated",
                generation=self.store.execution_snapshot()[0],
                captured_at=datetime.now(timezone.utc).isoformat(),
                market_timestamp=time.time(), account_scope="test-account",
            )

        async def disconnect(*_args):
            self.ready = False
            return {}

        self.ready = True
        self.trade.set_leverage = AsyncMock(side_effect=disconnect)
        engine = self.engine(preflight=SimpleNamespace(configured=True, prepare=prepare))
        engine.notify_event = AsyncMock()
        result = await engine.submit_signal(
            self.signal, account_equity=1000, daily_pnl_pct=0, size=.2,
        )
        self.trade.set_leverage.assert_awaited_once()
        self.assertEqual(result["reasons"], ["private_stream_not_ready"])
        self.assertEqual(result["order"]["status"], "rejected")
        self.assertFalse(self.store.has_active_order(self.signal.inst_id))
        self.assertEqual(self.trade.orders, [])
        engine.notify_event.assert_not_awaited()
        replay = await engine.submit_signal(
            self.signal, account_equity=1000, daily_pnl_pct=0, size=.2,
        )
        self.assertTrue(replay["idempotent"])
        self.trade.set_leverage.assert_awaited_once()

    async def test_real_close_is_allowed_when_private_stream_is_down(self):
        class ClosePreflight:
            configured = True

            async def prepare(self, signal, size, side_override, **_kwargs):
                return PreparedExecution(
                    signal=signal,
                    account_equity=1000,
                    daily_pnl_pct=0,
                    current_notional=0,
                    order_notional=0,
                    side=side_override or "sell",
                    pos_side="net",
                    td_mode="isolated",
                    generation=self.store_generation,
                    captured_at=datetime.now(timezone.utc).isoformat(),
                    market_timestamp=time.time(),
                    verified_close=True,
                    account_scope="test-account",
                )

        preflight = ClosePreflight()
        preflight.store_generation = self.store.execution_snapshot()[0]
        engine = self.engine(preflight=preflight)
        close_signal = self.signal.model_copy(update={
            "action": "close", "confidence": 1, "leverage": 1,
            "position_pct": 0, "stop_loss": None, "take_profit": None,
        })
        result = await engine.submit_signal(
            close_signal,
            account_equity=1000,
            daily_pnl_pct=0,
            size=0.2,
            dry_run=False,
            side_override="sell",
        )
        self.assertTrue(result["accepted"])
        self.assertEqual(len(self.trade.orders), 1)

    async def test_disconnect_after_preflight_is_blocked_before_exchange_request(self):
        class OpeningPreflight:
            configured = True

            async def prepare(self, signal, size, side_override, **_kwargs):
                self.outer.ready = False
                return PreparedExecution(
                    signal=signal,
                    account_equity=1000,
                    daily_pnl_pct=0,
                    current_notional=0,
                    order_notional=100,
                    side="buy",
                    pos_side="net",
                    td_mode="isolated",
                    generation=self.outer.store.execution_snapshot()[0],
                    captured_at=datetime.now(timezone.utc).isoformat(),
                    market_timestamp=time.time(),
                    account_scope="test-account",
                )

        preflight = OpeningPreflight()
        preflight.outer = self
        self.ready = True
        result = await self.engine(preflight=preflight).submit_signal(
            self.signal,
            account_equity=1000,
            daily_pnl_pct=0,
            size=0.2,
            dry_run=False,
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reasons"], ["private_stream_not_ready"])
        self.assertEqual(self.trade.orders, [])
        self.assertEqual(self.store.list_orders(), [])


class PrivateStreamProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_algo_outage_pushes_status_blocks_openings_and_deduplicates_notifications(self):
        algo_available = asyncio.Event()
        algo_available.set()

        class DisconnectableExchange(ExchangeServer):
            async def socket_handler(self, socket):
                if socket.request.path == "/algo":
                    await algo_available.wait()
                await super().socket_handler(socket)

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        exchange = await DisconnectableExchange().start()
        self.addAsyncCleanup(exchange.close)
        api = ApiProcess(directory.name, exchange)
        self.addAsyncCleanup(api.stop)
        api.environment["PRIVATE_STREAM_ALERT_GRACE_SECONDS"] = "0.05"
        await api.start()
        signal = TradeSignal(
            inst_id=SYMBOL, action="open_long", confidence=.9, leverage=2,
            position_pct=5, entry_price=float(exchange.price),
            stop_loss=float(exchange.price) * .98, take_profit=float(exchange.price) * 1.04,
        )

        async def notification_count(count):
            async def reached():
                return len(exchange.notifications) >= count
            await eventually(reached, timeout=5)

        try:
            async with api.client.stream("GET", "/api/v1/system/events") as response:
                lines = response.aiter_lines()
                initial = await next_event(lines, "status", lambda row: row["private_stream"]["ready"])
                self.assertTrue(initial["execution_enabled"])
                # The deliberately tiny grace can also report startup connection time.
                async def startup_settled():
                    status = await api.request("GET", "/system/status")
                    return (
                        not status["account_reconciler"]["private_stream_alerted"]
                        and len(exchange.notifications) % 2 == 0
                    )
                await eventually(startup_settled, timeout=5)
                baseline = len(exchange.notifications)
                algo_available.clear()
                for socket in tuple(exchange.ws.connections):
                    if socket.request.path == "/algo":
                        await socket.close()
                outage = await next_event(lines, "status", lambda row: not row["private_stream"]["ready"])
                self.assertEqual(outage["private_stream"]["reason_code"], "algo_stream_not_ready")
                for _ in range(3):
                    result = await api.request("POST", "/execution/signals", {
                        "signal": signal.model_dump(mode="json"),
                        "account_equity": 1000, "daily_pnl_pct": 0, "size": 1, "dry_run": False,
                    })
                    self.assertFalse(result["accepted"])
                    self.assertEqual(result["reasons"], ["private_stream_not_ready"])
                await notification_count(baseline + 1)
                self.assertEqual(len(exchange.notifications), baseline + 1)
                self.assertIn("推送中断", exchange.notifications[baseline]["title"])
                algo_available.set()
                await next_event(lines, "status", lambda row: row["private_stream"]["ready"])
                await notification_count(baseline + 2)
                await asyncio.sleep(.2)
                self.assertEqual(len(exchange.notifications), baseline + 2)
                self.assertIn("推送已恢复", exchange.notifications[baseline + 1]["title"])
                self.assertEqual(exchange.posts, [])
                self.assertEqual(exchange.errors, [])
        finally:
            algo_available.set()


if __name__ == "__main__":
    unittest.main()
