import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.execution_engine import ExecutionEngine
from app.account_sync import AccountSynchronizer
from app.automation_worker import AutomationWorker
from app.backtest import BacktestEngine
from app.okx_trade import OkxTradeClient
from app.okx_account import OkxAccountError
from app.pushplus import PushPlusClient
from app.risk_engine import RiskEngine, RiskLimits
from app.safety_control import SafetyController
from app.state_store import StateStore
from app.strategy_engine import StrategyEngine
from app.strategy_engine import DEFAULT_STRATEGY_CONFIG
from app.trading_signal import TradeSignal
from app import main as api_main
from app.ai_analysis import TradingAgentsAdapter
from app.ai_runner import _tradingagents_ticker


def candles(count: int = 30) -> list[list[str]]:
    rows = []
    for index in range(count):
        close = 100 + index * 0.5
        rows.append(
            [
                str(1700000000000 + index * 60000),
                str(close - 0.2),
                str(close + 0.4),
                str(close - 0.4),
                str(close),
                "10",
            ]
        )
    return list(reversed(rows))


class StrategyEngineTests(unittest.TestCase):
    def test_emits_expiring_structured_signal(self) -> None:
        result = StrategyEngine().analyze("BTC-USDT-SWAP", candles())
        self.assertEqual(result["source"], "structured-technical")
        self.assertIn(result["signal"]["action"], {"open_long", "open_short", "hold"})
        self.assertGreater(result["signal"]["expires_at"], result["signal"]["created_at"])

    def test_requires_enough_candles(self) -> None:
        with self.assertRaises(ValueError):
            StrategyEngine().analyze("BTC-USDT-SWAP", candles(10))

    def test_custom_periods_and_risk_parameters_are_used(self) -> None:
        config = {
            **DEFAULT_STRATEGY_CONFIG,
            "fast_period": 5,
            "slow_period": 10,
            "rsi_period": 7,
            "leverage": 1,
            "position_pct": 2,
            "signal_ttl_minutes": 20,
        }
        result = StrategyEngine().analyze(
            "BTC-USDT-SWAP",
            candles(30),
            config=config,
        )
        self.assertEqual(result["config"]["fast_period"], 5)
        self.assertEqual(result["config"]["slow_period"], 10)
        self.assertEqual(result["signal"]["leverage"], 1)
        self.assertEqual(result["signal"]["position_pct"], 2)

    def test_invalid_strategy_ranges_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StrategyEngine.normalize_config(
                {"fast_period": 21, "slow_period": 9},
            )

    def test_state_store_seeds_disabled_default_strategy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            strategy = store.get_strategy("structured-technical")
        self.assertIsNotNone(strategy)
        self.assertFalse(strategy["enabled"])
        self.assertEqual(strategy["config"]["fast_period"], 9)


class TradingAgentsAdapterTests(unittest.TestCase):
    def test_okx_swap_maps_to_yahoo_crypto_ticker(self) -> None:
        self.assertEqual(
            _tradingagents_ticker("BTC-USDT-SWAP"),
            "BTC-USD",
        )

    def test_missing_source_path_is_not_configured(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TRADINGAGENTS_ENABLED": "true",
                "TRADINGAGENTS_PATH": "/tmp/openperpdesk-missing-tradingagents",
            },
            clear=False,
        ):
            adapter = TradingAgentsAdapter()
        self.assertFalse(adapter.configured)


class BacktestEngineTests(unittest.TestCase):
    def test_replay_returns_metrics_and_curve(self) -> None:
        result = BacktestEngine().run("BTC-USDT-SWAP", candles(60))
        self.assertIn("return_pct", result)
        self.assertIn("max_drawdown_pct", result)
        self.assertEqual(result["inst_id"], "BTC-USDT-SWAP")
        self.assertGreater(len(result["equity_curve"]), 0)

    def test_replay_uses_signal_position_and_leverage(self) -> None:
        class FixedLongStrategy:
            def analyze(self, _inst_id, rows, config=None):
                price = float(rows[0][4])
                return {
                    "signal": {
                        "action": "open_long",
                        "entry_price": price,
                        "stop_loss": price * 0.9,
                        "take_profit": price * 1.2,
                        "leverage": 3,
                        "position_pct": 10,
                    }
                }

        result = BacktestEngine(FixedLongStrategy()).run(
            "BTC-USDT-SWAP",
            candles(60),
            initial_equity=1000,
            fee_bps=0,
        )
        self.assertEqual(result["trades"], 1)
        self.assertEqual(result["trade_log"][0]["leverage"], 3)
        self.assertEqual(result["trade_log"][0]["position_pct"], 10)
        self.assertGreater(result["final_equity"], 1000)


class AutomationWorkerSizingTests(unittest.TestCase):
    class FakeMarket:
        async def instruments(self, symbol):
            self.symbol = symbol
            return [{
                "instId": symbol,
                "ctVal": "0.01",
                "lotSz": "0.01",
                "minSz": "0.01",
            }]

    def test_order_size_uses_equity_leverage_and_contract_metadata(self) -> None:
        market = self.FakeMarket()
        worker = AutomationWorker(
            market,
            object(),
            object(),
            object(),
            object(),
            object(),
            object(),
        )
        size = asyncio.run(
            worker._order_size(
                "BTC-USDT-SWAP",
                mark_price=50000,
                account_equity=1000,
                leverage=2,
                position_pct=5,
            )
        )
        self.assertEqual(size, 0.2)
        self.assertEqual(market.symbol, "BTC-USDT-SWAP")

    def test_order_size_skips_budget_below_exchange_minimum(self) -> None:
        market = self.FakeMarket()
        worker = AutomationWorker(
            market,
            object(),
            object(),
            object(),
            object(),
            object(),
            object(),
        )
        size = asyncio.run(
            worker._order_size(
                "BTC-USDT-SWAP",
                mark_price=50000,
                account_equity=1,
                leverage=1,
                position_pct=1,
            )
        )
        self.assertEqual(size, 0.0)


class AutomationWorkerRunTests(unittest.TestCase):
    class FakeMarket:
        async def ticker(self, symbol):
            return {"instId": symbol, "last": "50000"}

        async def candles(self, symbol, bar, limit):
            return candles(limit)

        async def instruments(self, symbol):
            return [{
                "instId": symbol,
                "ctVal": "0.01",
                "lotSz": "0.01",
                "minSz": "0.01",
            }]

    class FakeAccount:
        configured = False

    class FakeAccountSync:
        def sync_stream(self):
            return {"positions": 0, "orders": 0, "fills": 0}

    class FakeStrategy:
        def analyze(self, symbol, _rows, config=None):
            signal = TradeSignal(
                inst_id=symbol,
                action="open_long",
                confidence=0.9,
                leverage=2,
                position_pct=5,
                entry_price=50000,
                stop_loss=49000,
                take_profit=52000,
                source="test-worker",
            )
            return {
                "inst_id": symbol,
                "source": "test-worker",
                "bias": "bullish",
                "config": config or {},
                "indicators": {},
                "signal": signal.model_dump(mode="json"),
                "report": {"summary": "worker test"},
            }

    def test_run_once_analyzes_sizes_and_previews_signal(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AUTO_TRADING_ENABLED": "true",
                "AUTO_TRADING_DRY_RUN": "true",
                "AUTO_TRADING_ACCOUNT_EQUITY": "1000",
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
                safety = SafetyController(store)
                execution = ExecutionEngine(
                    store,
                    RiskEngine(RiskLimits()),
                    FakeTradeClient(),
                    FakePushPlus(),
                    safety,
                )
                worker = AutomationWorker(
                    self.FakeMarket(),
                    self.FakeAccount(),
                    self.FakeAccountSync(),
                    self.FakeStrategy(),
                    execution,
                    RiskEngine(RiskLimits()),
                    store,
                    safety,
                )
                result = asyncio.run(worker.run_once())
                orders = store.list_orders()

        self.assertTrue(result["ran"])
        self.assertEqual(result["results"][0]["size"], 0.2)
        self.assertTrue(result["results"][0]["accepted"])
        self.assertEqual(orders[0]["status"], "preview")

    def test_run_once_rejects_signal_when_market_stream_is_stale(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AUTO_TRADING_ENABLED": "true",
                "AUTO_TRADING_DRY_RUN": "true",
                "AUTO_TRADING_ACCOUNT_EQUITY": "1000",
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
                safety = SafetyController(store)
                execution = ExecutionEngine(
                    store,
                    RiskEngine(RiskLimits()),
                    FakeTradeClient(),
                    FakePushPlus(),
                    safety,
                )
                worker = AutomationWorker(
                    self.FakeMarket(),
                    self.FakeAccount(),
                    self.FakeAccountSync(),
                    self.FakeStrategy(),
                    execution,
                    RiskEngine(RiskLimits()),
                    store,
                    safety,
                    market_data_fresh=lambda: False,
                )
                result = asyncio.run(worker.run_once())
                orders = store.list_orders()

        self.assertTrue(result["ran"])
        self.assertFalse(result["results"][0]["accepted"])
        self.assertEqual(orders, [])

    def test_same_candle_is_idempotent_across_worker_cycles(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AUTO_TRADING_ENABLED": "true",
                "AUTO_TRADING_DRY_RUN": "true",
                "AUTO_TRADING_ACCOUNT_EQUITY": "1000",
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
                safety = SafetyController(store)
                execution = ExecutionEngine(
                    store,
                    RiskEngine(RiskLimits()),
                    FakeTradeClient(),
                    FakePushPlus(),
                    safety,
                )
                worker = AutomationWorker(
                    self.FakeMarket(),
                    self.FakeAccount(),
                    self.FakeAccountSync(),
                    self.FakeStrategy(),
                    execution,
                    RiskEngine(RiskLimits()),
                    store,
                    safety,
                )
                first = asyncio.run(worker.run_once())
                second = asyncio.run(worker.run_once())
                orders = store.list_orders()

        self.assertTrue(first["results"][0]["accepted"])
        self.assertTrue(second["results"][0]["idempotent"])
        self.assertEqual(len(orders), 1)



    def test_multiple_symbols_share_the_same_cycle_exposure_budget(self) -> None:
        with patch.dict(
            os.environ,
            {
                "AUTO_TRADING_ENABLED": "true",
                "AUTO_TRADING_DRY_RUN": "true",
                "AUTO_TRADING_ACCOUNT_EQUITY": "1000",
                "AUTO_TRADING_SYMBOLS": "BTC-USDT-SWAP,ETH-USDT-SWAP",
            },
            clear=False,
        ):
            with tempfile.TemporaryDirectory() as directory:
                store = StateStore(str(Path(directory) / "state.sqlite3"))
                store.save_strategy(
                    "structured-technical",
                    "测试策略",
                    enabled=True,
                    config={
                        **DEFAULT_STRATEGY_CONFIG,
                        "position_pct": 10,
                        "leverage": 1,
                    },
                )
                safety = SafetyController(store)
                risk = RiskEngine(RiskLimits(max_total_notional_pct=15))
                execution = ExecutionEngine(
                    store,
                    risk,
                    FakeTradeClient(),
                    FakePushPlus(),
                    safety,
                )
                worker = AutomationWorker(
                    self.FakeMarket(),
                    self.FakeAccount(),
                    self.FakeAccountSync(),
                    self.FakeStrategy(),
                    execution,
                    risk,
                    store,
                    safety,
                )
                result = asyncio.run(worker.run_once())

        self.assertTrue(result["results"][0]["accepted"])
        self.assertFalse(result["results"][1]["accepted"])
        self.assertIn(
            "total_exposure_above_limit",
            result["results"][1].get("reasons", []),
        )


class FakeTradeClient:
    configured = True
    demo = True
    trading_mode = "demo"
    enabled = True

    async def place_order(self, order):
        return {"code": "0", "data": [{"ordId": "exchange-1"}]}


class FakePushPlus:
    configured = False


class ExecutionEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(str(Path(self.temp_dir.name) / "state.sqlite3"))
        self.safety = SafetyController(self.store)
        self.engine = ExecutionEngine(
            self.store,
            RiskEngine(RiskLimits()),
            FakeTradeClient(),
            FakePushPlus(),
            self.safety,
        )
        self.signal = TradeSignal(
            inst_id="BTC-USDT-SWAP",
            action="open_long",
            confidence=0.9,
            leverage=2,
            position_pct=5,
            entry_price=100,
            stop_loss=95,
            take_profit=110,
            source="test",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_preview_persists_order_without_network(self) -> None:
        result = asyncio.run(
            self.engine.submit_signal(
                self.signal,
                account_equity=1000,
                daily_pnl_pct=0,
                dry_run=True,
            )
        )
        self.assertTrue(result["accepted"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(self.store.list_orders()), 1)

    def test_same_signal_is_idempotent(self) -> None:
        first = asyncio.run(
            self.engine.submit_signal(
                self.signal,
                account_equity=1000,
                daily_pnl_pct=0,
                dry_run=True,
            )
        )
        second = asyncio.run(
            self.engine.submit_signal(
                self.signal,
                account_equity=1000,
                daily_pnl_pct=0,
                dry_run=True,
            )
        )
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        self.assertEqual(len(self.store.list_orders()), 1)

    def test_rejected_signal_is_not_saved_as_order(self) -> None:
        rejected = self.signal.model_copy(update={"confidence": 0.1})
        result = asyncio.run(
            self.engine.submit_signal(
                rejected,
                account_equity=1000,
                daily_pnl_pct=0,
                dry_run=True,
            )
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(self.store.list_orders(), [])
        self.assertEqual(self.store.list_audit()[0]["event_type"], "risk_rejected")

    def test_protective_exit_detects_stop(self) -> None:
        self.store.upsert_position(
            {
                "inst_id": "BTC-USDT-SWAP",
                "pos_side": "long",
                "size": 1,
                "entry_price": 100,
                "stop_loss": 95,
                "take_profit": 110,
            }
        )
        exit_event = self.engine.protective_exit(
            inst_id="BTC-USDT-SWAP",
            mark_price=94,
        )
        self.assertEqual(exit_event["reason"], "stop_loss")

    def test_emergency_stop_blocks_new_signal(self) -> None:
        self.safety.stop("test")
        result = asyncio.run(
            self.engine.submit_signal(
                self.signal,
                account_equity=1000,
                daily_pnl_pct=0,
                dry_run=True,
            )
        )
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reasons"], ["emergency_stop_active"])

    def test_stale_market_data_blocks_signal_before_preview(self) -> None:
        result = asyncio.run(
            self.engine.submit_signal(
                self.signal,
                account_equity=1000,
                daily_pnl_pct=0,
                dry_run=True,
                market_data_fresh=False,
            )
        )
        self.assertFalse(result["accepted"])
        self.assertIn("market_data_stale", result["reasons"])
        self.assertEqual(self.store.list_orders(), [])


class StateStoreFillTests(unittest.TestCase):
    def test_fill_is_idempotent_and_pnl_summary_is_net_of_fees(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            fill = {
                "trade_id": "trade-1",
                "exchange_order_id": "order-1",
                "client_order_id": "client-1",
                "inst_id": "BTC-USDT-SWAP",
                "side": "sell",
                "pos_side": "net",
                "fill_price": 101,
                "fill_size": 1,
                "fee": -0.2,
                "fee_ccy": "USDT",
                "realized_pnl": 5,
                "filled_at": "2026-01-01T00:00:00Z",
            }
            store.save_fill(fill)
            store.save_fill({**fill, "realized_pnl": 6})
            summary = store.pnl_summary()
        self.assertEqual(summary["fills"], 1)
        self.assertEqual(summary["realized_pnl"], 6)
        self.assertEqual(summary["fees"], -0.2)
        self.assertEqual(summary["net_pnl"], 5.8)

    def test_performance_report_builds_drawdown_daily_and_strategy_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            store.save_order(
                {
                    "client_order_id": "client-1",
                    "exchange_order_id": "order-1",
                    "status": "filled",
                    "inst_id": "BTC-USDT-SWAP",
                    "side": "sell",
                    "pos_side": "net",
                    "ord_type": "market",
                    "td_mode": "isolated",
                    "size": 1,
                    "source": "structured-technical",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            )
            store.save_fill(
                {
                    "trade_id": "trade-1",
                    "client_order_id": "client-1",
                    "inst_id": "BTC-USDT-SWAP",
                    "side": "sell",
                    "pos_side": "net",
                    "fill_price": 101,
                    "fill_size": 1,
                    "fee": 0,
                    "realized_pnl": 10,
                    "filled_at": "2026-01-01T01:00:00Z",
                }
            )
            store.save_fill(
                {
                    "trade_id": "trade-2",
                    "client_order_id": "client-1",
                    "inst_id": "BTC-USDT-SWAP",
                    "side": "sell",
                    "pos_side": "net",
                    "fill_price": 99,
                    "fill_size": 1,
                    "fee": -1,
                    "fee_ccy": "USDT",
                    "realized_pnl": -20,
                    "filled_at": "2026-01-01T02:00:00Z",
                }
            )
            report = store.performance_report(initial_equity=1000)
        self.assertEqual(report["net_pnl"], -11)
        self.assertEqual(report["ending_equity"], 989)
        self.assertEqual(report["max_drawdown"], 21)
        self.assertEqual(report["daily"]["2026-01-01"]["fills"], 2)
        self.assertEqual(report["by_strategy"]["structured-technical"]["net_pnl"], -11)
        self.assertEqual(len(report["equity_curve"]), 2)

    def test_risk_context_uses_position_notional_and_current_day_pnl(self) -> None:
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            store.upsert_position(
                {
                    "position_key": "BTC-USDT-SWAP:net:cross",
                    "inst_id": "BTC-USDT-SWAP",
                    "pos_side": "net",
                    "size": 1,
                    "entry_price": 100,
                    "mark_price": 101,
                    "notional": 250,
                    "unrealized_pnl": -2,
                }
            )
            now = datetime.now(timezone.utc).isoformat()
            store.save_fill(
                {
                    "trade_id": "today",
                    "client_order_id": "client-1",
                    "inst_id": "BTC-USDT-SWAP",
                    "side": "sell",
                    "pos_side": "net",
                    "fill_price": 101,
                    "fill_size": 1,
                    "fee": -1,
                    "realized_pnl": -10,
                    "filled_at": now,
                }
            )
            context = store.risk_context(1000)

        self.assertEqual(context["current_notional"], 250)
        self.assertEqual(context["daily_pnl"], -13)
        self.assertEqual(context["daily_pnl_pct"], -1.3)

    def test_active_order_check_ignores_preview_and_terminal_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            base = {
                "client_order_id": "client-1",
                "exchange_order_id": "exchange-1",
                "status": "preview",
                "inst_id": "BTC-USDT-SWAP",
                "side": "buy",
                "pos_side": "net",
                "ord_type": "market",
                "td_mode": "isolated",
                "size": 1,
                "source": "test",
            }
            store.save_order(base)
            self.assertFalse(store.has_active_order("BTC-USDT-SWAP"))
            store.save_order({**base, "status": "live"})
            self.assertTrue(store.has_active_order("BTC-USDT-SWAP"))
            store.save_order({**base, "status": "filled"})
            self.assertFalse(store.has_active_order("BTC-USDT-SWAP"))


class CancelOrderRouteTests(unittest.TestCase):
    class FakeStore:
        def __init__(self) -> None:
            self.order = {
                "client_order_id": "client-1",
                "exchange_order_id": "exchange-1",
                "status": "live",
                "inst_id": "BTC-USDT-SWAP",
            }
            self.audits: list[tuple[str, str]] = []

        def get_order(self, client_order_id):
            return self.order if client_order_id == self.order["client_order_id"] else None

        def save_order(self, order):
            self.order = order
            return order

        def add_audit(self, event_type, message, **_):
            self.audits.append((event_type, message))

    class FakeTrade:
        async def cancel_order(self, inst_id, ord_id):
            self.args = (inst_id, ord_id)
            return {"code": "0", "data": [{"sCode": "0", "ordId": ord_id}]}

    def test_cancel_route_persists_exchange_success(self) -> None:
        store = self.FakeStore()
        trade = self.FakeTrade()
        with patch.object(api_main, "state_store", store), patch.object(
            api_main,
            "trade_client",
            trade,
        ):
            result = asyncio.run(api_main.cancel_stored_order("client-1"))

        self.assertTrue(result["accepted"])
        self.assertEqual(store.order["status"], "canceling")
        self.assertEqual(trade.args, ("BTC-USDT-SWAP", "exchange-1"))
        self.assertEqual(store.audits[0][0], "order_cancel_requested")

    def test_cancel_route_rejects_empty_exchange_result(self) -> None:
        class EmptyResultTrade(self.FakeTrade):
            async def cancel_order(self, inst_id, ord_id):
                self.args = (inst_id, ord_id)
                return {"code": "0", "data": []}

        store = self.FakeStore()
        trade = EmptyResultTrade()
        with patch.object(api_main, "state_store", store), patch.object(
            api_main,
            "trade_client",
            trade,
        ):
            result = asyncio.run(api_main.cancel_stored_order("client-1"))

        self.assertFalse(result["accepted"])
        self.assertEqual(store.order["status"], "cancel_failed")
        self.assertEqual(store.audits[0][0], "order_cancel_failed")


class AccountOverviewRouteTests(unittest.TestCase):
    class PartialAccountClient:
        configured = True
        demo = True

        async def balance(self):
            return [{"totalEq": "1000"}]

        async def positions(self):
            raise OkxAccountError("positions temporarily unavailable")

        async def config(self):
            return [{"acctLv": "2"}]

    def test_account_overview_keeps_available_sections(self) -> None:
        with patch.object(
            api_main,
            "account_client",
            self.PartialAccountClient(),
        ):
            result = asyncio.run(api_main.account_overview())

        self.assertEqual(result["balance"][0]["totalEq"], "1000")
        self.assertEqual(result["config"][0]["acctLv"], "2")
        self.assertEqual(result["positions"], [])
        self.assertEqual(result["errors"], {"positions": "OkxAccountError"})


class WorkerControlRouteTests(unittest.TestCase):
    class FakeWorker:
        enabled = False
        dry_run = True
        started = False
        stopped = False

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

        def snapshot(self):
            return {
                "enabled": self.enabled,
                "dry_run": self.dry_run,
                "started": self.started,
                "stopped": self.stopped,
            }

    class FakeSafety:
        emergency_stopped = False

    class FakeTrade:
        trading_mode = "demo"
        demo = True
        enabled = False

    class FakeStore:
        def add_audit(self, *_args, **_kwargs):
            return None

    def test_web_control_can_enable_dry_run_worker(self) -> None:
        worker = self.FakeWorker()
        with patch.object(api_main, "automation_worker", worker), patch.object(
            api_main,
            "safety_controller",
            self.FakeSafety(),
        ), patch.object(api_main, "trade_client", self.FakeTrade()), patch.object(
            api_main,
            "state_store",
            self.FakeStore(),
        ):
            result = asyncio.run(
                api_main.control_worker(
                    api_main.WorkerControlRequest(enabled=True, dry_run=True),
                )
            )

        self.assertTrue(result["enabled"])
        self.assertTrue(result["dry_run"])
        self.assertTrue(worker.started)

    def test_web_control_rejects_non_dry_run_without_demo_execution(self) -> None:
        worker = self.FakeWorker()
        with patch.object(api_main, "automation_worker", worker), patch.object(
            api_main,
            "safety_controller",
            self.FakeSafety(),
        ), patch.object(api_main, "trade_client", self.FakeTrade()), patch.object(
            api_main,
            "state_store",
            self.FakeStore(),
        ):
            with self.assertRaises(HTTPException) as context:
                asyncio.run(
                    api_main.control_worker(
                        api_main.WorkerControlRequest(enabled=True, dry_run=False),
                    )
                )

        self.assertEqual(context.exception.status_code, 423)
        self.assertFalse(worker.started)


class FakeAccountClient:
    configured = True

    async def positions(self):
        return [{
            "instId": "BTC-USDT-SWAP",
            "posSide": "net",
            "mgnMode": "cross",
            "pos": "0",
            "avgPx": "100",
            "markPx": "101",
            "upl": "0",
        }]

    async def pending_orders(self):
        return []

    async def fills_history(self):
        return [{
            "tradeId": "trade-1",
            "ordId": "order-1",
            "clOrdId": "client-1",
            "instId": "BTC-USDT-SWAP",
            "side": "sell",
            "posSide": "net",
            "fillPx": "101",
            "fillSz": "1",
            "fee": "-0.2",
            "feeCcy": "USDT",
            "fillPnl": "5",
            "ts": "1767225600000",
        }]


class FailingOrderHistoryAccountClient(FakeAccountClient):
    async def orders_history(self, **_):
        raise OkxAccountError("history unavailable")


class FailingPositionsAccountClient(FakeAccountClient):
    async def positions(self):
        raise OkxAccountError("positions unavailable")


class AlgoAccountClient(FakeAccountClient):
    async def pending_algo_orders(self, **_):
        return [{
            "algoId": "algo-pending-1",
            "algoClOrdId": "protect-pending-1",
            "instId": "BTC-USDT-SWAP",
            "side": "sell",
            "posSide": "net",
            "ordType": "conditional",
            "tdMode": "isolated",
            "sz": "0.2",
            "slTriggerPx": "49000",
            "tpTriggerPx": "52000",
            "state": "live",
            "cTime": "1767225600000",
            "uTime": "1767225660000",
        }]

    async def algo_orders_history(self, **_):
        return [{
            "algoId": "algo-history-1",
            "algoClOrdId": "protect-history-1",
            "instId": "ETH-USDT-SWAP",
            "side": "buy",
            "posSide": "net",
            "ordType": "conditional",
            "tdMode": "cross",
            "sz": "1",
            "tpTriggerPx": "2100",
            "state": "effective",
            "cTime": "1767225600000",
            "uTime": "1767225660000",
        }]


class OrderTimestampAccountClient(FakeAccountClient):
    async def pending_orders(self):
        return [{
            "ordId": "order-1",
            "clOrdId": "client-1",
            "state": "live",
            "instId": "BTC-USDT-SWAP",
            "side": "buy",
            "posSide": "net",
            "ordType": "market",
            "tdMode": "isolated",
            "sz": "1",
            "cTime": "1767225600000",
            "uTime": "1767225660000",
        }]


class ProtectedPositionAccountClient(FakeAccountClient):
    account_scope = "protected-fixture"

    async def positions(self):
        return [{
            "instId": "BTC-USDT-SWAP",
            "posSide": "long",
            "mgnMode": "isolated",
            "pos": "1",
            "avgPx": "50000",
            "markPx": "50010",
            "upl": "0",
            "tradeId": "entry-trade",
        }]


class FakeAccountStream:
    def snapshot(self):
        return {"positions": [], "orders": []}


class AccountSyncTests(unittest.TestCase):
    def test_rest_sync_persists_exchange_fills(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            synchronizer = AccountSynchronizer(
                store,
                FakeAccountClient(),
                FakeAccountStream(),
            )
            result = asyncio.run(synchronizer.sync_rest())
            fills = store.list_fills()
        self.assertEqual(result["fills"], 1)
        self.assertEqual(fills[0]["trade_id"], "trade-1")
        self.assertEqual(fills[0]["realized_pnl"], 5)
        self.assertTrue(fills[0]["filled_at"].startswith("2026-01-01T00:00:00"))

    def test_rest_snapshot_closes_missing_local_positions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            store.upsert_position(
                {
                    "position_key": "ETH-USDT-SWAP:net:cross",
                    "inst_id": "ETH-USDT-SWAP",
                    "pos_side": "net",
                    "size": 1,
                    "entry_price": 2000,
                }
            )
            synchronizer = AccountSynchronizer(
                store,
                FakeAccountClient(),
                FakeAccountStream(),
            )
            asyncio.run(synchronizer.sync_rest())
            positions = store.list_positions()
        self.assertEqual(positions, [])

    def test_history_failure_keeps_pending_sync_and_records_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            synchronizer = AccountSynchronizer(
                store,
                FailingOrderHistoryAccountClient(),
                FakeAccountStream(),
            )
            result = asyncio.run(synchronizer.sync_rest())
            events = store.list_audit()
        self.assertEqual(result["orders"], 0)
        self.assertEqual(events[0]["event_type"], "order_history_sync_failed")

    def test_positions_failure_does_not_close_existing_local_position(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            store.upsert_position(
                {
                    "position_key": "ETH-USDT-SWAP:net:cross",
                    "inst_id": "ETH-USDT-SWAP",
                    "pos_side": "net",
                    "size": 1,
                    "entry_price": 2000,
                }
            )
            synchronizer = AccountSynchronizer(
                store,
                FailingPositionsAccountClient(),
                FakeAccountStream(),
            )
            result = asyncio.run(synchronizer.sync_rest())
            positions = store.list_positions()
            events = store.list_audit()
        self.assertEqual(result["errors"], ["positions"])
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["status"], "open")
        self.assertEqual(events[0]["event_type"], "account_endpoint_sync_failed")

    def test_exchange_order_timestamps_are_normalized(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            synchronizer = AccountSynchronizer(
                store,
                OrderTimestampAccountClient(),
                FakeAccountStream(),
            )
            asyncio.run(synchronizer.sync_rest())
            order = store.get_order("client-1")
        self.assertEqual(order["created_at"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(order["updated_at"], "2026-01-01T00:01:00+00:00")

    def test_position_sync_restores_local_protective_levels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            store.save_order(
                {
                    "client_order_id": "entry-1",
                    "exchange_order_id": "exchange-1",
                    "status": "filled",
                    "inst_id": "BTC-USDT-SWAP",
                    "side": "buy",
                    "pos_side": "long",
                    "ord_type": "market",
                    "td_mode": "isolated",
                    "size": 1,
                    "stop_loss": 49000,
                    "take_profit": 52000,
                    "source": "structured-technical",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "account_scope": "protected-fixture",
                    "raw": {
                        "ordId": "exchange-1", "clOrdId": "entry-1",
                        "instId": "BTC-USDT-SWAP", "posSide": "long", "tdMode": "isolated",
                        "side": "buy", "tradeId": "entry-trade", "accFillSz": "1",
                    },
                }
            )
            synchronizer = AccountSynchronizer(
                store,
                ProtectedPositionAccountClient(),
                FakeAccountStream(),
            )
            asyncio.run(synchronizer.sync_rest())
            position = store.list_positions()[0]
        self.assertEqual(position["stop_loss"], 49000)
        self.assertEqual(position["take_profit"], 52000)

    def test_rest_sync_persists_native_algo_orders(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            synchronizer = AccountSynchronizer(
                store,
                AlgoAccountClient(),
                FakeAccountStream(),
            )
            result = asyncio.run(synchronizer.sync_rest())
            orders = {
                item["client_order_id"]: item
                for item in store.list_orders()
            }

        self.assertEqual(result["algo_orders"], 2)
        self.assertIn("protect-pending-1", orders)
        self.assertIn("protect-history-1", orders)
        self.assertEqual(orders["protect-pending-1"]["stop_loss"], 49000)
        self.assertEqual(orders["protect-pending-1"]["take_profit"], 52000)
        self.assertEqual(orders["protect-history-1"]["source"], "okx-algo-rest")

    def test_disconnected_private_stream_does_not_reapply_cached_state(self) -> None:
        class CachedButDisconnectedStream:
            def snapshot(self):
                return {
                    "configured": True,
                    "connected": False,
                    "authenticated": False,
                    "positions": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "mgnMode": "isolated",
                            "pos": "1",
                            "avgPx": "50000",
                            "markPx": "50010",
                        }
                    ],
                    "orders": [],
                }

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            synchronizer = AccountSynchronizer(
                store,
                FakeAccountClient(),
                CachedButDisconnectedStream(),
            )
            result = synchronizer.sync_stream()

        self.assertEqual(result["skipped"], "private_stream_not_ready")
        self.assertEqual(result["positions"], 0)


if __name__ == "__main__":
    unittest.main()
