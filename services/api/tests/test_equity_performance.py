import asyncio
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.account_performance import AccountPerformanceWorker
from app.equity_performance import flow_rates, observation_bounds, prepare_interval, summarize_performance, value_interval
from app.historical_ledger import DAY_MS, day_ms
from app.historical_valuation import HistoricalValuationError
from app.okx_account import OkxAccountClient, OkxAccountError
from app.okx_market import OkxMarketClient
from app.realtime import private_events
from app.state_store import PerformanceLeaseLost, StateStore
from tests.fixtures.api_process import ApiProcess, eventually
from tests.fixtures.exchange_server import ExchangeServer
from tests.test_realtime import close_events, next_event


DAY = date(2026, 9, 10)


def baseline(day=DAY, equity="1000", start=100, end=500):
    target = day_ms(day)
    return {"day_utc": day.isoformat(), "target_ms": target, "request_started_ms": target + start,
            "received_at_ms": target + end, "equity_usd": equity, "status": "captured"}


def transfer(timestamp, identity="1", currency="USD", cash="100", **fields):
    return {"billId": identity, "ts": str(timestamp), "ccy": currency, "type": "1",
            "subType": "11" if Decimal(cash) >= 0 else "12", "balChg": cash, "posBalChg": "0",
            "instType": "", "instId": "", **fields}


class EquityPerformanceMathTests(unittest.TestCase):
    def setUp(self):
        self.start = baseline()
        self.end = baseline(DAY + timedelta(days=1), "1150")
        self.mid = day_ms(DAY) + DAY_MS // 2 + 300

    def report(self, rows=None, rates=None):
        return value_interval(self.start, self.end, prepare_interval(rows or [], self.start, self.end), rates or {})

    def test_deposit_not_profit_and_midpoint_modified_dietz(self):
        report = self.report([transfer(self.mid)])
        self.assertEqual(Decimal(report["pnl_usd"]), 50)
        self.assertEqual(Decimal(report["cash_flow_usd"]), 100)
        self.assertEqual(Decimal(report["weighted_capital_usd"]), 1050)
        self.assertAlmostEqual(float(report["return_pct"]), 100 * 50 / 1050)
        self.assertEqual(report["status"], "estimated")

    def test_withdrawal_and_no_trades_include_total_equity_unrealized_change(self):
        self.end["equity_usd"] = "950"
        report = self.report([transfer(self.mid, cash="-100")])
        self.assertEqual(Decimal(report["pnl_usd"]), 50)
        self.assertEqual(Decimal(report["weighted_capital_usd"]), 950)
        self.assertEqual(self.report()["pnl_usd"], "-50")

    def test_previous_day_cutoff_does_not_drop_flow_after_last_midnight(self):
        timestamp = self.end["target_ms"] + 50
        report = self.report([transfer(timestamp)])
        self.assertEqual(report["cash_flow_usd"], "100")
        self.assertEqual(report["end_ms"], self.end["received_at_ms"] + 1)
        self.assertGreater(report["end_ms"], self.end["target_ms"])

    def test_inclusive_boundary_windows_invalidate_return_even_if_flows_net_zero(self):
        for timestamp in (self.start["request_started_ms"], self.start["received_at_ms"],
                          self.end["request_started_ms"], self.end["received_at_ms"]):
            with self.subTest(timestamp=timestamp):
                result = self.report([transfer(timestamp), transfer(timestamp, "2", cash="-100")])
                self.assertEqual(result["status"], "boundary_uncertain")
                self.assertEqual(result["boundary_flow_rows"], 2)
                self.assertIsNone(result["pnl_usd"])
                self.assertIsNone(result["return_pct"])

    def test_zero_transfers_do_not_create_boundary_uncertainty(self):
        report = self.report([transfer(self.start["received_at_ms"], currency="USDT", cash="0")])
        self.assertEqual(report["status"], "estimated")
        self.assertEqual(report["cash_flow_usd"], "0")

    def test_stablecoins_require_historical_prices_only_external_flows_need_rates(self):
        raw = transfer(self.mid, currency="USDT")
        records = prepare_interval([raw], self.start, self.end)
        key = next(iter(flow_rates(records)))
        self.assertEqual(key, ("USDT", self.mid // 60_000 * 60_000 - 60_000))
        missing = self.report([raw])
        self.assertEqual(missing["status"], "missing_rates")
        self.assertIsNone(missing["cash_flow_usd"])
        valued = self.report([raw], {key: ".98"})
        self.assertEqual(valued["cash_flow_usd"], "98.00")
        self.assertEqual(valued["pnl_usd"], "52.00")
        internal = transfer(self.mid, currency="BTC", type="6", cash="-10")
        self.assertEqual(self.report([internal])["pnl_usd"], "150")
        self.assertEqual(flow_rates(prepare_interval([internal], self.start, self.end)), set())

    def test_known_spot_trades_not_external_flows_but_unknown_non_swap_bills_block(self):
        raw = transfer(self.mid, currency="BTC", type="2", instType="SPOT", instId="BTC-USDT")
        self.assertEqual(self.report([raw])["status"], "estimated")
        raw["type"] = "999"
        self.assertEqual(self.report([raw])["status"], "unclassified_bills")

    def test_unrecognized_transfer_direction_or_subtype_never_silently_becomes_pnl(self):
        for changes in ({"subType": "12"}, {"subType": "999"}, {"from": "18", "to": "6"},
                        {"posBalChg": "1"}, {"balChg": "NaN"}):
            with self.subTest(changes=changes):
                result = self.report([transfer(self.mid, **changes)])
                self.assertEqual(result["status"], "unclassified_bills")
                self.assertIsNone(result["pnl_usd"])
        self.assertEqual(self.report([transfer(self.mid, **{"from": "6", "to": "18"})])["status"], "estimated")

    def test_invalid_identity_outside_interval_and_partial_last_day_are_rejected(self):
        for rows in ([transfer(self.start["request_started_ms"] - 1)],
                     [transfer(self.end["received_at_ms"] + 1)],
                     [transfer(self.mid), transfer(self.mid)],
                     [transfer(self.mid, identity="")]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                self.report(rows)
        self.end["target_ms"] += DAY_MS
        with self.assertRaises(ValueError):
            observation_bounds(self.start, self.end)

    def test_strict_decimal_money_does_not_inherit_callers_rounding(self):
        self.start["equity_usd"] = "1000000000000000000000000000000"
        self.end["equity_usd"] = "1000000000000000000000000000000.000000000000000000000000000001"
        with localcontext() as context:
            context.prec = 5
            report = self.report()
            self.assertEqual(context.prec, 5)
        self.assertEqual(Decimal(report["pnl_usd"]), Decimal("1e-30"))
        self.end["equity_usd"] = "1e120"
        with self.assertRaises(Inexact):
            self.report()

    def test_nonpositive_equity_and_effective_capital_do_not_report_returns(self):
        for begin, end, cash in (("0", "100", "100"), ("1000", "-1", "0"), ("100", "1", "-300")):
            self.start["equity_usd"], self.end["equity_usd"] = begin, end
            with self.subTest(begin=begin, end=end, cash=cash):
                result = self.report([transfer(self.mid, cash=cash)])
                self.assertEqual(result["status"], "estimated")
                self.assertEqual(result["return_status"], "nonpositive_capital")
                self.assertIsNone(result["return_pct"])
                self.assertIsNotNone(result["pnl_usd"])

    def test_linked_returns_and_observed_drawdown_do_not_bridge_missing_periods(self):
        rows = [{"status": "estimated", "return_pct": "10", "pnl_usd": "100", "cash_flow_usd": "0"},
                {"status": "estimated", "return_pct": "-20", "pnl_usd": "-220", "cash_flow_usd": "0"}]
        report = summarize_performance(rows)
        self.assertEqual(Decimal(report["linked_return_pct"]), -12)
        self.assertEqual(Decimal(report["observed_max_drawdown_pct"]), -20)
        self.assertEqual(Decimal(report["pnl_usd"]), -120)
        rows.insert(1, {"status": "missing_baselines"})
        report = summarize_performance(rows)
        self.assertIsNone(report["linked_return_pct"])
        self.assertIsNone(report["pnl_usd"])
        self.assertTrue(all(row["nav_index"] is None for row in report["daily"]))


class PerformanceFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.day = datetime.now(timezone.utc).date() - timedelta(days=2)
        self.start = baseline(self.day)
        self.end = baseline(self.day + timedelta(days=1), "1150")
        self.now = day_ms(self.day) + 2 * DAY_MS + 3_600_000
        self.account = SimpleNamespace(configured=True, account_scope="scope", bills_archive=AsyncMock(return_value=[]))
        self.market = SimpleNamespace(rate_scope="market", historical_index_rate=AsyncMock(return_value=".98"))
        self.worker = AccountPerformanceWorker(self.store, self.account, self.market, clock=lambda: self.now)
        for item in (self.start, self.end):
            self.store.save_equity_baseline("scope", item["target_ms"], item["request_started_ms"],
                                           item["received_at_ms"], [{"totalEq": item["equity_usd"]}])

    def report(self, scope="scope", market="market", days=None):
        return self.store.bill_history(scope, days or [self.day], rate_scope=market)["performance"]

    def schedule(self, retry=False):
        return self.store.schedule_performance("scope", "market", [self.day], self.now, retry=retry)


class PerformanceStoreTests(PerformanceFixture, unittest.TestCase):
    def test_scheduling_is_idempotent_scope_isolated_and_missing_baselines_explicit(self):
        self.assertEqual(self.schedule()["scheduled"], 1)
        self.assertEqual(self.schedule()["scheduled"], 0)
        self.assertEqual(self.report()["daily"][0]["status"], "queued")
        self.assertEqual(self.report(market="other")["daily"][0]["status"], "missing_interval")
        self.assertEqual(self.report(scope="other")["daily"][0]["status"], "missing_baselines")
        result = self.store.schedule_performance("scope", "market", [self.day + timedelta(days=1)], self.now)
        self.assertEqual(result["missing_baselines"], 1)
        self.assertEqual(result["scheduled"], 0)

    def test_lease_takeover_cancel_and_stale_completion_fencing(self):
        self.schedule()
        job = self.store.claim_performance("scope", "market", "a", self.now)
        self.assertIsNone(self.store.claim_performance("scope", "other", "b", self.now + 60_001))
        self.assertIsNone(self.store.claim_performance("scope", "market", "b", self.now))
        self.assertIsNotNone(self.store.claim_performance("scope", "market", "b", self.now + 60_001))
        with self.assertRaises(PerformanceLeaseLost):
            self.store.touch_performance(job["id"], "scope", "a", self.now + 60_001)
        self.assertEqual(self.store.cancel_performance("other", "market", [self.day]), 0)
        self.assertEqual(self.store.cancel_performance("scope", "market", [self.day]), 1)
        with self.assertRaises(PerformanceLeaseLost):
            self.store.complete_performance(job["id"], "scope", "b", self.now + 60_002,
                                            value_interval(self.start, self.end, [], {}), {"market_scope": "market"})
        self.assertEqual(self.schedule()["scheduled"], 0)
        self.assertEqual(self.schedule(retry=True)["scheduled"], 1)

    def test_newly_closed_interval_waits_five_minutes_for_ledger_availability(self):
        self.now = self.end["received_at_ms"] + 1
        self.schedule()
        self.assertIsNone(self.store.claim_performance("scope", "market", "a", self.now))
        self.assertIsNotNone(self.store.claim_performance("scope", "market", "a", self.now + 300_000))


class PerformanceWorkerTests(PerformanceFixture, unittest.IsolatedAsyncioTestCase):
    async def test_auto_collection_covers_full_interval_and_survives_restart(self):
        self.account.bills_archive.return_value = [transfer(self.end["target_ms"] + 50, currency="USDT")]
        await self.worker.run_once()
        expected_bounds = observation_bounds(self.start, self.end)
        self.assertEqual(self.account.bills_archive.call_args.args, expected_bounds)
        result = self.report()
        self.assertEqual(result["status"], "estimated")
        self.assertEqual(result["pnl_usd"], "52.00")
        self.assertEqual(self.market.historical_index_rate.await_count, 1)
        self.assertEqual(self.store.bill_history("scope", [self.day])["coverage"]["completed_days"], 0)
        self.assertFalse(self.store.bill_history("scope", [self.day])["coverage"]["complete"])
        reopened = StateStore(str(self.store.path))
        self.assertEqual(reopened.bill_history("scope", [self.day], rate_scope="market")["performance"], result)
        await AccountPerformanceWorker(reopened, self.account, self.market, clock=lambda: self.now).run_once()
        self.assertEqual(self.account.bills_archive.await_count, 1)
        for private in ("account_scope", "market_scope", "evidence_json", "record_json", "raw", "lease_owner"):
            self.assertNotIn(f"'{private}'", str(result))

    async def test_persistent_upstream_failures_retry_with_capped_backoff_without_partial_publication(self):
        self.account.bills_archive.side_effect = OkxAccountError("503 private-secret-url")
        for index in range(8):
            await self.worker.run_once()
            row = self.report()["daily"][0]
            self.assertEqual(row["status"], "retry_wait")
            self.assertIsNone(row["pnl_usd"])
            self.assertEqual(row["attempts"], index + 1)
            self.assertLessEqual(row["next_attempt_ms"] - self.now, 15_000)
            self.assertNotIn("private-secret", str(row))
            self.now = row["next_attempt_ms"]
        self.account.bills_archive.side_effect = None
        await self.worker.run_once()
        self.assertEqual(self.report()["status"], "estimated")
        self.assertEqual(self.account.bills_archive.await_count, 9)

    async def test_missing_rates_unsupported_bill_and_malformed_data_are_not_zero_returns(self):
        self.account.bills_archive.return_value = [transfer(self.start["received_at_ms"] + 1, currency="USDT")]
        self.market.historical_index_rate.side_effect = HistoricalValuationError("historical_rate_unavailable")
        await self.worker.run_once()
        self.assertEqual(self.report()["daily"][0]["status"], "missing_rates")
        self.assertIsNone(self.report()["linked_return_pct"])
        self.schedule(retry=True)
        self.market.historical_index_rate.side_effect = None
        await self.worker.run_once()
        self.assertEqual(self.report()["status"], "estimated")
        self.account.bills_archive.return_value = [transfer(self.start["received_at_ms"] + 1, type="999")]
        self.schedule(retry=True)
        await self.worker.run_once()
        self.assertEqual(self.report()["daily"][0]["status"], "unclassified_bills")
        self.account.bills_archive.return_value = [transfer(self.start["request_started_ms"] - 1)]
        self.schedule(retry=True)
        await self.worker.run_once()
        self.assertEqual(self.report()["daily"][0]["status"], "failed")

    async def test_cancel_during_pagination_discards_entire_interval(self):
        async def collect(*args, on_page):
            self.store.cancel_performance("scope", "market", [self.day])
            await on_page()
            return []
        self.account.bills_archive.side_effect = collect
        await self.worker.run_once()
        self.assertEqual(self.report()["daily"][0]["status"], "canceled")
        self.assertEqual(self.report()["complete_intervals"], 0)
        self.market.historical_index_rate.assert_not_awaited()

    async def test_expired_claim_in_same_worker_cannot_overwrite_replacement_attempt(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed(*args, **kwargs):
            entered.set()
            await release.wait()
            return [transfer(self.start["received_at_ms"] + 1, cash="100")]
        self.account.bills_archive.side_effect = delayed
        old = asyncio.create_task(self.worker.run_once())
        await asyncio.wait_for(entered.wait(), 2)
        self.now += 60_001
        self.account.bills_archive.side_effect = None
        await self.worker.run_once()
        release.set()
        await old
        self.assertEqual(self.report()["pnl_usd"], "150")
        self.assertEqual(self.report()["daily"][0]["attempts"], 2)

    async def test_retention_boundary_crossed_during_collection_discards_interval(self):
        with patch("app.account_performance.archive_first_day",
                   side_effect=[self.day, self.day, self.day + timedelta(days=1)]):
            await self.worker.run_once()
        self.assertEqual(self.report()["complete_intervals"], 0)
        self.assertIsNone(self.report()["pnl_usd"])
        self.assertEqual(self.report()["daily"][0]["error"], "performance_outside_retention")

    async def test_account_or_market_switch_prevents_publication(self):
        for field in ("account", "market"):
            with self.subTest(field=field):
                async def collect(*args, **kwargs):
                    if field == "account":
                        self.account.account_scope = "other"
                    else:
                        self.market.rate_scope = "other"
                    return []
                self.account.bills_archive.side_effect = collect
                self.schedule(retry=True)
                await self.worker.run_once()
                self.assertEqual(self.report()["complete_intervals"], 0)
                self.account.account_scope, self.market.rate_scope = "scope", "market"
                self.now += 60_001

    async def test_lifecycle_shutdown_requeues_and_unconfigured_worker_makes_no_requests(self):
        self.account.configured = False
        await self.worker.run_once()
        self.account.bills_archive.assert_not_awaited()
        self.account.configured = True
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.account.bills_archive.side_effect = pending
        await self.worker.start()
        await asyncio.wait_for(entered.wait(), 2)
        await self.worker.close()
        self.assertEqual(self.report()["daily"][0]["status"], "retry_wait")
        self.assertIsNone(self.report()["pnl_usd"])

    async def test_private_sse_pushes_committed_progress_without_reading_exchange(self):
        stream = SimpleNamespace(configured=True, connected=True, authenticated=True, balance=[], last_message_at=None)
        events = private_events(self.store, stream, self.account, lambda: True, lambda: "market")
        self.addAsyncCleanup(close_events, events)
        async with asyncio.timeout(3):
            while "event: account_performance" not in await anext(events):
                pass
            self.account.bills_archive.assert_not_awaited()
            await self.worker.run_once()
            while True:
                frame = await anext(events)
                if "event: account_performance" in frame:
                    self.assertIn('"pending":0', frame)
                    self.assertIn('"intervals":1', frame)
                    self.assertNotIn("scope", frame)
                    break

    async def test_private_progress_switches_market_scope_even_without_database_writes(self):
        await self.worker.run_once()
        stream = SimpleNamespace(configured=True, connected=True, authenticated=True, balance=[], last_message_at=None)
        events = private_events(self.store, stream, self.account, lambda: True, lambda: self.market.rate_scope)
        self.addAsyncCleanup(close_events, events)
        async with asyncio.timeout(3):
            while True:
                frame = await anext(events)
                if "event: account_performance" in frame:
                    self.assertIn('"intervals":1', frame)
                    break
            revision = self.store.revision
            self.market.rate_scope = "other"
            while True:
                frame = await anext(events)
                if "event: account_performance" in frame:
                    self.assertIn('"intervals":0', frame)
                    break
            self.assertEqual(revision, self.store.revision)


@unittest.skipIf(os.name == "nt", "The inherited Uvicorn socket requires POSIX")
class PerformanceProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_http_pagination_historical_rate_sse_restart_and_read_only_gate(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        exchange = ExchangeServer()
        self.addAsyncCleanup(exchange.close)
        await exchange.start()
        api = ApiProcess(directory.name, exchange)
        api.environment["EXECUTION_ENABLED"] = "false"
        self.addAsyncCleanup(api.stop)
        day = datetime.now(timezone.utc).date() - timedelta(days=2)
        begin, end = baseline(day), baseline(day + timedelta(days=1), "1150")
        with patch.dict(os.environ, api.environment, clear=True):
            account = OkxAccountClient()
            market = OkxMarketClient()
        store = StateStore(str(api.database))
        for item in (begin, end):
            store.save_equity_baseline(account.account_scope, item["target_ms"], item["request_started_ms"],
                                       item["received_at_ms"], [{"totalEq": item["equity_usd"]}])
        exchange.archive_bills = [transfer(begin["received_at_ms"] + 1 + index, str(1000 + index),
                                           currency="USDT", cash="1") for index in range(101)]
        await api.start()
        path = f"/account/bills/history?start_day={day}&end_day={day}"
        async def completed():
            report = await api.request("GET", path)
            return report["performance"] if report["performance"]["status"] == "estimated" else None
        await eventually(completed)
        report = (await api.request("GET", path))["performance"]
        self.assertEqual(report["pnl_usd"], "51.02")
        self.assertEqual(report["daily"][0]["rows"], 101)
        self.assertEqual(report["daily"][0]["flow_count"], 101)
        self.assertEqual(len(report["daily"][0]["flows"]), 100)
        self.assertTrue(any(item["query"].get("after") for item in exchange.gets
                            if item["path"] == "/api/v5/account/bills-archive"))
        for endpoint in ("/account/performance/collect", "/account/performance/cancel"):
            denied = await api.client.post("/api/v1" + endpoint, json={"start_day": str(day), "end_day": str(day)},
                                           headers={"X-Admin-Token": "invalid"})
            self.assertEqual(denied.status_code, 401)
        invalid = await api.client.post("/api/v1/account/performance/collect",
                                        json={"start_day": str(day), "end_day": str(day - timedelta(days=1))})
        self.assertEqual(invalid.status_code, 422)
        async with api.client.stream("GET", "/api/v1/account/events") as response:
            progress = await next_event(response.aiter_lines(), "account_performance", lambda row: row["intervals"] == 1)
            self.assertEqual(progress["pending"], 0)
        exchange.fail_paths.add("/api/v5/account/bills-archive")
        accepted = await api.client.post("/api/v1/account/performance/collect",
                                         json={"start_day": str(day), "end_day": str(day)})
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json()["scheduled"], 1)
        async def retrying():
            return (await api.request("GET", path))["performance"]["daily"][0]["status"] == "retry_wait"
        await eventually(retrying)
        canceled = await api.request("POST", "/account/performance/cancel", {"start_day": str(day), "end_day": str(day)})
        self.assertEqual(canceled["canceled"], 1)
        self.assertEqual((await api.request("GET", path))["performance"]["daily"][0]["status"], "canceled")
        exchange.fail_paths.clear()
        await api.request("POST", "/account/performance/collect", {"start_day": str(day), "end_day": str(day)}, status=202)
        await eventually(completed)
        report = (await api.request("GET", path))["performance"]
        await api.stop()
        await api.start()
        self.assertEqual((await api.request("GET", path))["performance"], report)
        self.assertFalse((await api.request("GET", "/execution/status"))["execution_enabled"])
        self.assertIsNone(store.historical_rate("wrong-market", "USDT", begin["target_ms"] - 60_000))
        self.assertIsNotNone(store.historical_rate(market.rate_scope, "USDT", begin["target_ms"] - 60_000))
        self.assertEqual(exchange.posts, [])
        self.assertEqual(exchange.errors, [])


if __name__ == "__main__":
    unittest.main()
