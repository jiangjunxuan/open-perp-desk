import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.equity_baseline import (
    BASELINE_POLICY,
    CAPTURE_WINDOW_MS,
    EquityBaselineSampler,
    baseline_window,
    total_equity,
)
from app.historical_ledger import DAY_MS, day_ms
from app.okx_account import OkxAccountClient
from app.realtime import private_events
from app.state_store import StateStore
from tests.fixtures.api_process import ApiProcess
from tests.fixtures.exchange_server import ExchangeServer
from tests.test_realtime import close_events, next_event


TARGET_MS = day_ms(date(2026, 9, 10))


class EquityBaselineTests(unittest.TestCase):
    def test_total_equity_requires_total_eq_and_allows_zero(self):
        self.assertEqual(total_equity([{"totalEq": "0", "adjEq": "99"}]), "0")
        self.assertEqual(total_equity([{"totalEq": "-1.25"}]), "-1.25")
        for balance in (
            [],
            [{"adjEq": "100"}],
            [{"totalEq": "100"}, {"totalEq": "100"}],
            [{"totalEq": "NaN"}],
        ):
            with self.subTest(balance=balance), self.assertRaises(ValueError):
                total_equity(balance)

    def test_window_requires_observation_after_boundary_and_within_sixty_seconds(self):
        target = TARGET_MS
        baseline_window(target, target + 1, target + 1_000)
        for start, received in (
            (target - 1, target),
            (target + CAPTURE_WINDOW_MS + 1, target + CAPTURE_WINDOW_MS + 1),
            (target + 1, target + 10_002),
            (target + 1_000, target + 999),
        ):
            with self.subTest(start=start, received=received), self.assertRaises(ValueError):
                baseline_window(target, start, received)

    def test_store_is_account_scoped_and_duplicate_boundary_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            target = TARGET_MS
            self.assertTrue(store.save_equity_baseline(
                "account-a", target, target + 100, target + 500, [{"totalEq": "0"}],
            ))
            self.assertFalse(store.save_equity_baseline(
                "account-a", target, target + 200, target + 600, [{"totalEq": "10"}],
            ))
            self.assertIsNotNone(store.equity_baseline("account-a", target))
            self.assertIsNone(store.equity_baseline("account-b", target))
            self.assertEqual(store.equity_baseline("account-a", target)["equity_usd"], "0")

    def test_sampler_captures_once_and_exposes_explicit_policy(self):
        target = TARGET_MS
        now = target + 2_000
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            account = SimpleNamespace(
                configured=True, account_scope="scope-a", balance=AsyncMock(return_value=[{"totalEq": "123.45"}]),
            )
            sampler = EquityBaselineSampler(store, account, clock=lambda: now)
            captured = asyncio.run(sampler.run_once())
            self.assertTrue(captured)
            self.assertEqual(account.balance.await_count, 1)
            self.assertEqual(store.equity_baseline("scope-a", target)["equity_usd"], "123.45")
            self.assertEqual(sampler.snapshot()["policy"], BASELINE_POLICY)
            self.assertFalse(asyncio.run(sampler.run_once()))
            self.assertEqual(account.balance.await_count, 1)

    def test_sampler_does_not_backfill_a_missed_window(self):
        target = TARGET_MS
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            account = SimpleNamespace(
                configured=True, account_scope="scope-a", balance=AsyncMock(return_value=[{"totalEq": "123.45"}]),
            )
            sampler = EquityBaselineSampler(store, account, clock=lambda: target + CAPTURE_WINDOW_MS + 1)
            self.assertFalse(asyncio.run(sampler.run_once()))
            account.balance.assert_not_awaited()
            self.assertIsNone(store.equity_baseline("scope-a", target))

    def test_history_exposes_missing_boundaries_without_promoting_legacy_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            day = date(2026, 9, 10)
            store.save_equity_snapshot("scope", TARGET_MS, "rest_reconcile", [{"totalEq": "100"}])
            store.save_equity_baseline("scope", TARGET_MS + DAY_MS, TARGET_MS + DAY_MS + 100,
                                      TARGET_MS + DAY_MS + 500, [{"totalEq": "123.45000000000000001"}])
            result = store.bill_history("scope", [day, day + timedelta(days=1)], limit=1)
            report = result["equity_baselines"]
            self.assertEqual(report["required"], 3)
            self.assertEqual(report["captured"], 1)
            self.assertEqual(report["data"][0]["status"], "missing")
            self.assertIsNone(report["data"][0]["equity_usd"])
            self.assertEqual(report["data"][1]["equity_usd"], "123.45000000000000001")
            self.assertEqual(report["data"][2]["day_utc"], "2026-09-12")
            self.assertIsNone(result["summary"]["net_return"])
            self.assertEqual(store.bill_history("other", [day])["equity_baselines"]["captured"], 0)
            encoded = str(report)
            for private_field in ("account_scope", "balance_json", "details"):
                self.assertNotIn(private_field, encoded)
            reopened = StateStore(str(store.path))
            self.assertEqual(reopened.equity_baseline("scope"), report["data"][1])


class EquitySamplerAsyncTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.now = TARGET_MS + 1000
        self.account = SimpleNamespace(
            configured=True, account_scope="scope",
            balance=AsyncMock(return_value=[{"totalEq": "123.45"}]),
        )
        self.sampler = EquityBaselineSampler(self.store, self.account, clock=lambda: self.now)

    async def test_transient_failure_retries_without_saving_or_exposing_error_details(self):
        self.account.balance.side_effect = [RuntimeError("private-token-in-error"), [{"totalEq": "10"}]]
        self.assertFalse(await self.sampler.run_once())
        self.assertIsNone(self.store.equity_baseline("scope"))
        self.assertEqual(self.sampler.snapshot()["last_error"], "RuntimeError")
        self.now += 5000
        self.assertTrue(await self.sampler.run_once())
        self.assertEqual(self.account.balance.await_count, 2)
        self.assertIsNone(self.sampler.snapshot()["last_error"])
        self.assertNotIn("private-token-in-error", str(self.store.list_audit()))

    async def test_account_change_and_slow_response_do_not_publish(self):
        async def changed():
            self.account.account_scope = "other"
            return [{"totalEq": "10"}]
        self.account.balance.side_effect = changed
        self.assertFalse(await self.sampler.run_once())
        self.assertIsNone(self.store.equity_baseline("scope"))
        self.assertIsNone(self.store.equity_baseline("other"))
        async def slow():
            self.now += 10_001
            return [{"totalEq": "10"}]
        self.account.balance.side_effect = slow
        self.assertFalse(await self.sampler.run_once())
        self.assertIsNone(self.store.equity_baseline("other"))

    async def test_concurrent_calls_and_restart_use_persisted_boundary(self):
        self.assertEqual(await asyncio.gather(self.sampler.run_once(), self.sampler.run_once()), [True, False])
        self.assertEqual(self.account.balance.await_count, 1)
        other = EquityBaselineSampler(StateStore(str(self.store.path)), self.account, clock=lambda: self.now)
        self.assertFalse(await other.run_once())
        self.assertEqual(self.account.balance.await_count, 1)

    async def test_lifecycle_cancels_pending_read_without_publishing(self):
        entered = asyncio.Event()
        async def pending():
            entered.set()
            await asyncio.Event().wait()
        self.account.balance.side_effect = pending
        await self.sampler.start()
        await self.sampler.start()
        await asyncio.wait_for(entered.wait(), 2)
        self.assertTrue(self.sampler.snapshot()["running"])
        await self.sampler.stop()
        self.assertFalse(self.sampler.snapshot()["running"])
        self.assertIsNone(self.store.equity_baseline("scope"))
        self.assertEqual(self.account.balance.await_count, 1)

    async def test_unconfigured_sampler_does_not_start(self):
        self.account.configured = False
        await self.sampler.start()
        self.assertFalse(await self.sampler.run_once())
        self.assertFalse(self.sampler.snapshot()["running"])
        self.account.balance.assert_not_awaited()

    async def test_committed_baseline_is_pushed_without_raw_balance_or_account_scope(self):
        stream = SimpleNamespace(
            configured=True, connected=True, authenticated=True, balance=[], last_message_at=None,
        )
        events = private_events(self.store, stream, self.account, lambda: True)
        self.addAsyncCleanup(close_events, events)
        async with asyncio.timeout(2):
            while "event: equity_baseline" not in await anext(events):
                pass
            self.assertTrue(await self.sampler.run_once())
            while True:
                frame = await anext(events)
                if "event: equity_baseline" in frame:
                    self.assertIn('"equity_usd":"123.45"', frame)
                    self.assertNotIn("account_scope", frame)
                    self.assertNotIn("balance_json", frame)
                    break


@unittest.skipIf(os.name == "nt", "The inherited Uvicorn socket requires POSIX")
class EquityBaselineProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_capture_api_sse_and_restart_are_read_only(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        exchange = ExchangeServer()
        self.addAsyncCleanup(exchange.close)
        await exchange.start()
        api = ApiProcess(directory.name, exchange)
        api.environment["EXECUTION_ENABLED"] = "false"
        self.addAsyncCleanup(api.stop)
        day = datetime.now(timezone.utc).date() - timedelta(days=2)
        target = day_ms(day)
        with patch.dict(os.environ, api.environment, clear=True):
            account = OkxAccountClient()
        store = StateStore(str(api.database))
        sampler = EquityBaselineSampler(store, account, clock=lambda: target + 500)
        self.assertTrue(await sampler.run_once())
        await api.start()
        query = f"/account/bills/history?start_day={day}&end_day={day}"
        first = await api.request("GET", query)
        evidence = first["equity_baselines"]
        self.assertEqual(evidence["captured"], 1)
        self.assertEqual(evidence["data"][0]["equity_usd"], "1000")
        self.assertEqual(evidence["data"][1]["status"], "missing")
        self.assertIsNone(first["valuation"]["net_return"])
        async with api.client.stream("GET", "/api/v1/account/events") as response:
            pushed = await next_event(response.aiter_lines(), "equity_baseline", lambda item: bool(item["latest"]))
            self.assertEqual(pushed["latest"], evidence["data"][0])
        denied = await api.client.get("/api/v1" + query, headers={"X-Admin-Token": "invalid"})
        self.assertEqual(denied.status_code, 401)
        await api.stop()
        await api.start()
        self.assertEqual((await api.request("GET", query))["equity_baselines"], evidence)
        self.assertFalse((await api.request("GET", "/execution/status"))["execution_enabled"])
        self.assertEqual(exchange.posts, [])
        self.assertEqual(exchange.errors, [])


if __name__ == "__main__":
    unittest.main()
