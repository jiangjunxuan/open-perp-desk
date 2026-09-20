import asyncio
import math
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from app.account_sync import AccountSynchronizer
from app.execution_engine import ExecutionEngine
from app.okx_trade import OkxOrderRejected, OrderRequest
from app.order_preflight import PreparedExecution
from app.risk_engine import RiskEngine, RiskLimits
from app.state_store import StateStore
from app.trading_signal import TradeSignal


class DisabledPushPlus:
    configured = False


class RecordingTrade:
    enabled = True

    def __init__(self):
        self.orders = []

    async def place_order(self, order):
        self.orders.append(order)
        return {"code": "0", "data": [{"ordId": "exchange1"}]}

    async def set_leverage(self, *_args):
        return {}


class LifecyclePreflight:
    """Isolate submission lifecycle tests from the independently tested account reader."""
    configured = True

    def __init__(self, store):
        self.store = store

    async def prepare(self, signal, size, side_override):
        now = datetime.now(timezone.utc)
        return PreparedExecution(
            signal, 1000, 0, 0, size * 100,
            side_override or "buy", "net", "isolated",
            self.store.execution_snapshot()[0], now.isoformat(), now.timestamp(),
        )


class ExecutionReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.trade = RecordingTrade()
        self.engine = self.make_engine(self.trade)
        self.signal = TradeSignal(
            inst_id="BTC-USDT-SWAP", action="open_long", confidence=0.9,
            leverage=2, position_pct=10, entry_price=100,
            stop_loss=95, take_profit=110,
        )

    def make_engine(self, trade):
        return ExecutionEngine(
            self.store, RiskEngine(RiskLimits()), trade, DisabledPushPlus(),
            preflight=LifecyclePreflight(self.store),
            private_stream_ready=lambda: True,
        )

    async def submit(self, engine=None, **kwargs):
        return await (engine or self.engine).submit_signal(
            self.signal, account_equity=1000, daily_pnl_pct=0, **kwargs,
        )

    async def test_preview_then_execution_sends_exactly_one_order(self):
        preview = await self.submit(dry_run=True)
        actual = await self.submit()
        replay = await self.submit()
        self.assertNotEqual(preview["order"]["client_order_id"], actual["order"]["client_order_id"])
        self.assertTrue(actual["accepted"])
        self.assertFalse(actual["dry_run"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(len(self.trade.orders), 1)
        self.assertEqual(len(self.store.list_orders()), 2)

    async def test_preview_can_change_size_without_poisoning_execution(self):
        first = await self.submit(dry_run=True, size=1)
        second = await self.submit(dry_run=True, size=2)
        self.assertNotEqual(first["order"]["client_order_id"], second["order"]["client_order_id"])
        await self.submit(size=2)
        conflict = await self.submit(size=1)
        self.assertEqual(conflict["reasons"], ["idempotency_payload_conflict"])
        self.assertFalse(conflict["accepted"])
        self.assertEqual(len(self.trade.orders), 1)

    async def test_two_executors_claim_before_network_io(self):
        started, release = asyncio.Event(), asyncio.Event()
        store = self.store

        class DelayedTrade(RecordingTrade):
            async def place_order(self, order):
                self.orders.append(order)
                assert store.get_order(order.cl_ord_id)["status"] == "submitting"
                started.set()
                await release.wait()
                return {"code": "0", "data": [{"ordId": "exchange1"}]}

        trade = DelayedTrade()
        first = asyncio.create_task(self.submit(self.make_engine(trade)))
        await asyncio.wait_for(started.wait(), 1)
        second = await self.submit(self.make_engine(trade))
        self.assertFalse(second["accepted"])
        self.assertEqual(second["reasons"], ["order_submission_unconfirmed"])
        release.set()
        self.assertTrue((await first)["accepted"])
        self.assertEqual(len(trade.orders), 1)

    async def test_unknown_outcome_survives_restart_without_retry(self):
        class TimeoutTrade(RecordingTrade):
            async def place_order(self, order):
                self.orders.append(order)
                raise TimeoutError("response lost")

        trade = TimeoutTrade()
        with self.assertRaises(TimeoutError):
            await self.submit(self.make_engine(trade))
        self.assertEqual(self.store.list_orders()[0]["status"], "submission_unknown")
        self.assertTrue(self.store.has_active_order(self.signal.inst_id))
        retry = await self.submit(self.make_engine(trade))
        self.assertFalse(retry["accepted"])
        self.assertTrue(retry["idempotent"])
        self.assertEqual(len(trade.orders), 1)

    async def test_explicit_rejection_is_not_an_unknown_outcome(self):
        class RejectedTrade(RecordingTrade):
            async def place_order(self, _order):
                raise OkxOrderRejected("insufficient balance")

        with self.assertRaises(OkxOrderRejected):
            await self.submit(self.make_engine(RejectedTrade()))
        self.assertEqual(self.store.list_orders()[0]["status"], "rejected")
        self.assertFalse(self.store.has_active_order(self.signal.inst_id))

    async def test_cancellation_keeps_durable_unconfirmed_intent(self):
        started = asyncio.Event()

        class InterruptedTrade(RecordingTrade):
            async def place_order(self, _order):
                started.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(self.submit(self.make_engine(InterruptedTrade())))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        retry = await self.submit()
        self.assertFalse(retry["accepted"])
        self.assertEqual(self.store.list_orders()[0]["status"], "submission_unknown")
        self.assertEqual(self.trade.orders, [])

    async def test_stream_fill_is_not_downgraded_by_later_http_success(self):
        store = self.store

        class FilledBeforeResponse(RecordingTrade):
            async def place_order(self, order):
                stored = store.get_order(order.cl_ord_id)
                store.save_order({
                    **stored, "status": "filled", "exchange_order_id": "exchange1",
                    "raw": {"state": "filled"},
                })
                return {"code": "0", "data": [{"ordId": "exchange1"}]}

        result = await self.submit(self.make_engine(FilledBeforeResponse()))
        self.assertEqual(result["order"]["status"], "filled")

    async def test_legacy_real_order_is_not_resubmitted(self):
        record = (await self.submit(dry_run=True))["order"]
        real_id = self.engine.client_order_id(self.signal)
        self.store.save_order({
            **record, "client_order_id": f"opd-{real_id[3:]}",
            "status": "submitted", "exchange_order_id": "legacy1",
        })
        result = await self.submit()
        self.assertTrue(result["idempotent"])
        self.assertEqual(self.trade.orders, [])

    async def test_preview_does_not_install_stop_loss_on_existing_position(self):
        record = (await self.submit(dry_run=True))["order"]
        self.store.save_order({
            **record, "account_scope": "preview-fixture", "exchange_order_id": "preview-exchange",
            "raw": {
                "ordId": "preview-exchange", "clOrdId": record["client_order_id"],
                "instId": self.signal.inst_id, "posSide": "net", "tdMode": "isolated",
                "side": "buy", "accFillSz": "1", "tradeId": "preview-trade",
            },
        })
        sync = AccountSynchronizer(self.store, SimpleNamespace(account_scope="preview-fixture"), object())
        self.assertEqual(sync._local_protection(
            self.signal.inst_id, "net", 1, td_mode="isolated",
            account_scope="preview-fixture", trade_id="preview-trade",
        ), (None, None, None))


class OrderBoundaryTests(unittest.TestCase):
    def test_small_numbers_are_not_rounded_to_zero(self):
        order = OrderRequest(inst_id="BTC-USDT-SWAP", side="buy", sz=0.00000001)
        self.assertEqual(order.okx_payload()["sz"], "0.00000001")
        self.assertEqual(OrderRequest._number(1000000), "1000000")

    def test_ids_are_alphanumeric_and_attached_id_fits(self):
        for invalid in ("with-dash", "with_underscore"):
            with self.assertRaises(ValueError):
                OrderRequest(inst_id="BTC-USDT-SWAP", side="buy", sz=1, cl_ord_id=invalid)
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP", side="buy", sz=1,
            cl_ord_id="a" * 32, stop_loss=90, take_profit=110,
        )
        attached = order.okx_payload()["attachAlgoOrds"][0]["attachAlgoClOrdId"]
        self.assertTrue(attached.isalnum())
        self.assertLessEqual(len(attached), 32)

    def test_nonfinite_numbers_fail_validation_and_risk(self):
        for value in (math.nan, math.inf, -math.inf):
            with self.assertRaises(ValueError):
                OrderRequest(inst_id="BTC-USDT-SWAP", side="buy", sz=value)
            signal = TradeSignal(
                inst_id="BTC-USDT-SWAP", action="close", confidence=1,
                leverage=1, position_pct=0,
            )
            decision = RiskEngine().evaluate(
                signal, account_equity=value, daily_pnl_pct=0,
            )
            self.assertFalse(decision.approved)
            self.assertIn("risk_context_not_finite", decision.reasons)

    def test_naive_signal_timestamps_fail_validation(self):
        with self.assertRaises(ValueError):
            TradeSignal(
                inst_id="BTC-USDT-SWAP", action="hold", confidence=1,
                leverage=1, position_pct=0, created_at="2026-09-13T00:00:00",
            )
