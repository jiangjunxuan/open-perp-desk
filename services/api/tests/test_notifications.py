import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.account_sync import AccountSynchronizer
from app.automation_worker import AutomationWorker
from app.execution_engine import ExecutionEngine
from app import main as api_main
from app.risk_engine import RiskEngine, RiskLimits
from app.safety_control import SafetyController
from app.state_store import StateStore
from app.strategy_engine import DEFAULT_STRATEGY_CONFIG
from app.trading_signal import TradeSignal


class FakePushPlus:
    configured = True

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send(self, title: str, content: str) -> dict[str, str]:
        self.messages.append((title, content))
        return {"code": "200"}


class FakeTradeClient:
    configured = True
    demo = True
    trading_mode = "demo"
    enabled = True

    async def place_order(self, _order):
        return {"code": "0", "data": [{"ordId": "exchange-1"}]}


class ExecutionNotificationTests(unittest.TestCase):
    def test_risk_rejection_is_forwarded_to_pushplus(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            pushplus = FakePushPlus()
            engine = ExecutionEngine(
                store,
                RiskEngine(RiskLimits()),
                FakeTradeClient(),
                pushplus,
                SafetyController(store),
            )
            signal = TradeSignal(
                inst_id="BTC-USDT-SWAP",
                action="open_long",
                confidence=0.1,
                leverage=2,
                position_pct=5,
                entry_price=100,
                stop_loss=95,
                take_profit=110,
                source="test",
            )

            result = asyncio.run(
                engine.submit_signal(
                    signal,
                    account_equity=1000,
                    daily_pnl_pct=0,
                    dry_run=True,
                )
            )

        self.assertFalse(result["accepted"])
        self.assertEqual(len(pushplus.messages), 1)
        self.assertIn("风控拒绝", pushplus.messages[0][0])
        self.assertIn("BTC-USDT-SWAP", pushplus.messages[0][1])


class FakeAccountClient:
    configured = True

    async def positions(self):
        return []

    async def pending_orders(self):
        return []

    async def fills_history(self):
        return [
            {
                "tradeId": "trade-1",
                "ordId": "order-1",
                "clOrdId": "client-1",
                "instId": "BTC-USDT-SWAP",
                "side": "buy",
                "posSide": "net",
                "fillPx": "50000",
                "fillSz": "1",
                "fee": "-0.1",
                "feeCcy": "USDT",
                "fillPnl": "2.5",
                "ts": "1760000000000",
            }
        ]

    async def orders_history(self, **_):
        return []

    async def pending_algo_orders(self, **_):
        return []

    async def algo_orders_history(self, **_):
        return []


class FillNotificationTests(unittest.TestCase):
    def test_new_fill_is_notified_once_across_repeated_sync(self) -> None:
        notifications: list[tuple[str, str, str]] = []

        async def notifier(
            event_type: str,
            title: str,
            content: str,
            **_kwargs,
        ) -> bool:
            notifications.append((event_type, title, content))
            return True

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            synchronizer = AccountSynchronizer(
                store,
                FakeAccountClient(),
                object(),
                notifier=notifier,
            )

            asyncio.run(synchronizer.sync_rest())
            asyncio.run(synchronizer.sync_rest())

        self.assertEqual(
            [event_type for event_type, _, _ in notifications],
            ["fill_received"],
        )
        self.assertIn("50000", notifications[0][2])

    def test_private_stream_fill_is_persisted_idempotently(self) -> None:
        class PrivateStream:
            def snapshot(self):
                return {
                    "configured": True,
                    "connected": True,
                    "authenticated": True,
                    "positions": [],
                    "orders": [],
                    "fills": [
                        {
                            "tradeId": "stream-trade-1",
                            "ordId": "order-1",
                            "clOrdId": "client-1",
                            "instId": "BTC-USDT-SWAP",
                            "side": "buy",
                            "posSide": "net",
                            "fillPx": "50000",
                            "fillSz": "1",
                            "fillFee": "-0.1",
                            "fillPnl": "2.5",
                            "fillTime": "1760000000000",
                        }
                    ],
                }

        class AccountClient:
            configured = False

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            synchronizer = AccountSynchronizer(
                store,
                AccountClient(),
                PrivateStream(),
            )
            first = synchronizer.sync_stream()
            second = synchronizer.sync_stream()
            fills = store.list_fills()

        self.assertEqual(first["fills"], 1)
        self.assertEqual(second["fills"], 1)
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["trade_id"], "stream-trade-1")

    def test_rest_correction_is_not_overwritten_by_cached_stream_fill(self) -> None:
        class PrivateStream:
            def snapshot(self):
                return {
                    "configured": True,
                    "connected": True,
                    "authenticated": True,
                    "positions": [],
                    "orders": [],
                    "fills": [{
                        "tradeId": "trade-1",
                        "ordId": "order-1",
                        "instId": "BTC-USDT-SWAP",
                        "side": "buy",
                        "fillPx": "50000",
                        "fillSz": "1",
                        "fillFee": "-0.2",
                        "fillPnl": "0",
                        "fillTime": "1760000000000",
                    }],
                }

        notifications = []

        async def notifier(event_type, *_args, **_kwargs):
            notifications.append(event_type)
            return True

        async def sync_both(synchronizer):
            synchronizer.sync_stream()
            await asyncio.sleep(0)
            await synchronizer.sync_rest()
            synchronizer.sync_stream()
            await asyncio.sleep(0)

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            synchronizer = AccountSynchronizer(
                store,
                FakeAccountClient(),
                PrivateStream(),
                notifier=notifier,
            )
            asyncio.run(sync_both(synchronizer))
            fill = store.get_fill("trade-1")

        self.assertEqual(fill["fee"], -0.1)
        self.assertEqual(fill["realized_pnl"], 2.5)
        self.assertEqual(notifications, ["fill_received"])


class ProtectiveExitNotificationTests(unittest.TestCase):
    class Market:
        async def mark_price(self, _symbol):
            return 94.0

    class Account:
        configured = False

    class AccountSync:
        def sync_stream(self):
            return {"positions": 0, "orders": 0, "fills": 0}

    class Strategy:
        def analyze(self, *_args, **_kwargs):
            raise AssertionError("strategy should not run before protective exit")

    def test_protective_exit_is_not_repeated_for_same_position(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AUTO_TRADING_ENABLED": "true",
                "AUTO_TRADING_DRY_RUN": "true",
                "AUTO_TRADING_SYMBOLS": "BTC-USDT-SWAP",
            },
            clear=False,
        ):
            with tempfile.TemporaryDirectory() as directory:
                store = StateStore(str(Path(directory) / "state.sqlite3"))
                store.save_strategy(
                    "structured-technical",
                    "测试策略",
                    enabled=True,
                    config=DEFAULT_STRATEGY_CONFIG,
                )
                store.upsert_position(
                    {
                        "position_key": "BTC-USDT-SWAP:long:cross",
                        "inst_id": "BTC-USDT-SWAP",
                        "pos_side": "long",
                        "size": 1,
                        "entry_price": 100,
                        "stop_loss": 95,
                        "take_profit": 110,
                    }
                )
                pushplus = FakePushPlus()
                execution = ExecutionEngine(
                    store,
                    RiskEngine(RiskLimits()),
                    FakeTradeClient(),
                    pushplus,
                    SafetyController(store),
                )
                worker = AutomationWorker(
                    self.Market(),
                    self.Account(),
                    self.AccountSync(),
                    self.Strategy(),
                    execution,
                    RiskEngine(RiskLimits()),
                    store,
                )

                first = asyncio.run(worker.run_once())
                second = asyncio.run(worker.run_once())

        self.assertEqual(first["results"][0]["action"], "stop_loss")
        self.assertTrue(second["results"][0]["accepted"])
        self.assertEqual(
            sum("保护性平仓预览" in title for title, _ in pushplus.messages),
            1,
        )
        self.assertTrue(any("不会向交易所发单" in content for _, content in pushplus.messages))


class SafetyRouteNotificationTests(unittest.TestCase):
    class Store:
        def __init__(self) -> None:
            self.audits: list[tuple[str, str]] = []

        def add_audit(self, event_type, message, **_kwargs):
            self.audits.append((event_type, message))

    class Safety:
        def __init__(self) -> None:
            self.stopped = False

        def stop(self, _reason):
            self.stopped = True

        def resume(self, _reason):
            self.stopped = False

        def snapshot(self):
            return {"emergency_stopped": self.stopped}

    class Execution:
        def __init__(self) -> None:
            self.events: list[tuple[str, str]] = []

        async def notify_event(self, event_type, title, _content, **_kwargs):
            self.events.append((event_type, title))
            return True

    def test_emergency_stop_and_resume_await_notifications(self) -> None:
        store = self.Store()
        safety = self.Safety()
        execution = self.Execution()
        with patch.object(api_main, "state_store", store), patch.object(
            api_main,
            "safety_controller",
            safety,
        ), patch.object(api_main, "execution_engine", execution):
            stopped = asyncio.run(
                api_main.emergency_stop(
                    api_main.SafetyReasonRequest(reason="test stop"),
                )
            )
            resumed = asyncio.run(
                api_main.resume_trading(
                    api_main.SafetyReasonRequest(reason="test resume"),
                )
            )

        self.assertTrue(stopped["emergency_stopped"])
        self.assertFalse(resumed["emergency_stopped"])
        self.assertEqual(
            [event_type for event_type, _ in execution.events],
            ["emergency_stop", "emergency_resume"],
        )


if __name__ == "__main__":
    unittest.main()
