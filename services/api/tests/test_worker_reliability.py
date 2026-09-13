import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.automation_worker import AutomationWorker
from app.okx_account import OkxAccountError
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
