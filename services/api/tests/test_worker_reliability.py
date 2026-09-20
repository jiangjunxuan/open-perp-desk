import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.automation_worker import AutomationWorker
from app.live_safety import LiveSafetyGate
from app.okx_account import OkxAccountError
from app.okx_trade import OkxTradeClient
from app.state_store import StateStore


class WorkerReliabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.store.save_strategy("structured-technical", "test", enabled=True, config={})

    def worker(self, *, market=None, account=None, sync=None):
        worker = AutomationWorker(
            market or object(), account or object(), sync or object(),
            object(), object(), object(), self.store,
        )
        worker.enabled = True
        worker.dry_run = True
        return worker

    def test_only_explicit_false_disables_startup_dry_run(self):
        for value in (None, "", " ", "tru", "0", "off", "FALSEE", "true", "TRUE", " true ", "false", "FALSE", " false "):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["AUTO_TRADING_DRY_RUN"] = value
                worker = AutomationWorker(
                    object(), object(), object(), object(), object(), object(), self.store,
                )
                expected = value is None or value.strip().lower() != "false"
                self.assertEqual(worker.dry_run, expected)
                self.assertEqual(worker.snapshot()["dry_run"], expected)

    async def test_worker_rejects_live_execution_even_with_unlocked_live_client(self):
        with patch.dict(os.environ, {
            "EXECUTION_ENABLED": "true", "TRADING_MODE": "live", "OKX_DEMO": "false",
            "LIVE_TRADING_ENABLED": "true", "LIVE_UNLOCK_PHRASE": "fixture-unlock",
            "OKX_API_KEY": "fixture-key", "OKX_SECRET_KEY": "fixture-secret",
            "OKX_PASSPHRASE": "fixture-passphrase",
        }, clear=True):
            gate = LiveSafetyGate()
            self.assertTrue(gate.unlock("fixture-unlock"))
            client = OkxTradeClient(live_gate=gate)
            self.assertTrue(client.enabled)
            sync = SimpleNamespace(sync_stream=Mock())
            worker = self.worker(sync=sync)
            worker.execution = SimpleNamespace(trade_client=client)
            worker.dry_run = False
            result = await worker.run_once()
            self.assertFalse(result["ran"])
            self.assertEqual(result["reason"], "Demo execution is disabled")
            sync.sync_stream.assert_not_called()

    async def test_enabled_demo_client_reaches_strategy_check(self):
        worker = self.worker()
        worker.execution = SimpleNamespace(trade_client=SimpleNamespace(
            enabled=True, demo=True, trading_mode="demo",
        ))
        worker.dry_run = False
        with patch.object(self.store, "get_strategy", return_value=None) as lookup:
            result = await worker.run_once()
        self.assertEqual(result["reason"], "strategy_not_found:structured-technical")
        lookup.assert_called_once_with("structured-technical")

    async def test_partial_account_snapshot_stops_cycle_before_any_market_or_order(self):
        class Account:
            configured = True

        class Sync:
            def sync_stream(self):
                return {}

            async def sync_rest(self):
                return {"errors": ["positions"]}

        result = await self.worker(account=Account(), sync=Sync()).run_once()
        self.assertFalse(result["ran"])
        self.assertEqual(result["reason"], "account_snapshot_unavailable")

    async def test_configured_account_never_uses_simulated_equity_on_failure(self):
        class Account:
            configured = True

            async def balance(self):
                raise OkxAccountError("offline")

        with self.assertRaises(OkxAccountError):
            await self.worker(account=Account())._account_equity()

    async def test_zero_and_nonfinite_exchange_equity_are_rejected(self):
        class Account:
            configured = True
            value = "0"

            async def balance(self):
                return [{"totalEq": self.value}]

        account = Account()
        for value in ("0", "-1", "NaN", "Infinity", ""):
            account.value = value
            with self.subTest(value=value), self.assertRaises(OkxAccountError):
                await self.worker(account=account)._account_equity()

    async def test_non_dry_run_requires_real_equity(self):
        class Account:
            configured = False

        worker = self.worker(account=Account())
        worker.dry_run = False
        with self.assertRaises(OkxAccountError):
            await worker._account_equity()

    async def test_parallel_worker_cycles_do_not_overlap(self):
        worker = self.worker()
        started, release = asyncio.Event(), asyncio.Event()

        async def cycle():
            started.set()
            await release.wait()
            return {"ran": True}

        with patch.object(worker, "_run_once", cycle):
            first = asyncio.create_task(worker.run_once())
            await started.wait()
            second = await worker.run_once()
            self.assertEqual(second["reason"], "worker_cycle_in_progress")
            release.set()
            self.assertTrue((await first)["ran"])

    async def test_size_rounds_to_lot_multiple_not_only_decimal_places(self):
        class Market:
            async def instruments(self, symbol):
                return [{"instId": symbol, "ctVal": "1", "lotSz": "0.25", "minSz": "0.25"}]

        size = await self.worker(market=Market())._order_size(
            "BTC-USDT-SWAP", mark_price=100, account_equity=1000,
            leverage=1, position_pct=13,
        )
        self.assertEqual(size, 1.25)

    async def test_inverse_contract_uses_fixed_quote_notional(self):
        class Market:
            async def instruments(self, symbol):
                return [{
                    "instId": symbol, "ctType": "inverse", "ctVal": "100",
                    "ctMult": "1", "lotSz": "1", "minSz": "1",
                }]

        size = await self.worker(market=Market())._order_size(
            "BTC-USD-SWAP", mark_price=50000, account_equity=1000,
            leverage=2, position_pct=10,
        )
        self.assertEqual(size, 2)

    async def test_missing_and_nonfinite_metadata_do_not_default_to_one_contract(self):
        class Market:
            rows = []

            async def instruments(self, _symbol):
                return self.rows

        market = Market()
        worker = self.worker(market=market)
        for rows in (
            [], [{"instId": "BTC-USDT-SWAP"}],
            [{"instId": "BTC-USDT-SWAP", "ctVal": "NaN", "lotSz": "1", "minSz": "1"}],
            [{"instId": "BTC-USDT-SWAP", "ctVal": "1", "lotSz": "0", "minSz": "1"}],
        ):
            market.rows = rows
            self.assertEqual(await worker._order_size(
                "BTC-USDT-SWAP", 50000, 1000, 2, 5,
            ), 0)
        self.assertEqual(await self.worker()._order_size(
            "BTC-USDT-SWAP", 50000, 1000, 2, 5,
        ), 0)
