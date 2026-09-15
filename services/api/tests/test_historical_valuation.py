import asyncio
import json
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import main as api_main
from app.account_valuation import AccountValuationWorker
from app.historical_ledger import day_ms, prepare_history_window
from app.historical_valuation import (
    MINUTE_MS, HistoricalValuationError, parse_index_rate, rate_key, required_rates, value_history,
)
from app.okx_market import OkxMarketClient, OkxMarketError
from app.realtime import private_events
from app.state_store import BillImportBusy, StateStore, ValuationLeaseLost
from tests.fixtures.api_process import ApiProcess, eventually
from tests.fixtures.exchange_server import ExchangeServer
from tests.test_account_history import now_ms, sample
from tests.test_realtime import close_events, next_event


class HistoricalValuationTests(unittest.TestCase):
    day = date(2026, 9, 10)

    def test_rates_use_previous_completed_minute_including_midnight(self):
        self.assertEqual(rate_key("USDT", day_ms(self.day) + 59999), ("USDT", day_ms(self.day) - MINUTE_MS))
        self.assertEqual(rate_key("BTC", day_ms(self.day) + MINUTE_MS), ("BTC", day_ms(self.day)))
        for currency, stamp in [("BTC-USD", 100000), ("USDT", 1.5), ("USDT", -1), ("", 100000)]:
            with self.subTest(currency=currency), self.assertRaises(ValueError):
                rate_key(currency, stamp)

    def test_transfers_and_mixed_currency_funding_are_valued_separately(self):
        records, _ = prepare_history_window([
            sample(self.day),
            sample(self.day, "2", ccy="BTC", pnl=".01", fee="-.0001"),
            sample(self.day, "3", type="8", subType="173", fee="0", balChg="-1"),
            sample(self.day, "4", type="1", instType="", instId="", balChg="500"),
            sample(self.day, "5", type="6", balChg="-500"),
        ], self.day)
        rates = {(currency, day_ms(self.day) - MINUTE_MS): rate for currency, rate in [("USDT", ".98"), ("BTC", "50000")]}
        report = value_history(records, [self.day], {self.day.isoformat()}, rates)
        self.assertEqual(report["status"], "valued")
        self.assertEqual(Decimal(report["usd"]["net_pnl"]), Decimal("503.722"))
        self.assertEqual(Decimal(report["usd"]["cash_flow"]), Decimal("490"))
        self.assertEqual(Decimal(report["usd"]["funding"]), Decimal("-.98"))
        self.assertIsNone(report["net_return"])
        self.assertEqual(report["rows"]["1"]["index"], "USDT-USD")
        self.assertEqual(report["rows"]["1"]["candle_ms"], day_ms(self.day) - MINUTE_MS)
        self.assertEqual(required_rates(records), set(rates))

    def test_missing_rates_unknown_bills_and_uncovered_days_never_become_zero_profit(self):
        records, _ = prepare_history_window([
            sample(self.day), sample(self.day, "2", type="999"), sample(self.day, "3", instType="SPOT", instId="BTC-USDT"),
        ], self.day)
        report = value_history(records, [self.day, self.day + timedelta(days=1)], {self.day.isoformat()}, {})
        self.assertEqual(report["status"], "incomplete")
        self.assertIsNone(report["usd"])
        self.assertEqual(report["missing_rate_count"], 1)
        self.assertEqual(report["unclassified_rows"], 2)
        self.assertTrue(all(day["usd"] is None for day in report["daily"]))
        self.assertEqual(report["valued_subtotal_usd"]["net_pnl"], "0")
        self.assertFalse(report["daily"][1]["covered"])

    def test_zero_amounts_and_usd_do_not_require_remote_quotes_but_usdt_does(self):
        records, _ = prepare_history_window([
            sample(self.day, ccy="USD"), sample(self.day, "2", ccy="USDT", pnl="0", fee="0"),
        ], self.day)
        self.assertEqual(required_rates(records), set())
        report = value_history(records, [self.day], {self.day.isoformat()}, {})
        self.assertEqual(report["status"], "valued")
        self.assertEqual(report["rows"]["1"]["basis"], "same_currency")
        self.assertEqual(report["rows"]["2"]["basis"], "zero_amount")
        self.assertIsNone(report["rows"]["2"]["rate"])
        records[1]["net_pnl"] = "1"
        self.assertIn(("USDT", day_ms(self.day) - MINUTE_MS), required_rates(records))

    def test_arithmetic_does_not_inherit_context_or_silently_round(self):
        records, _ = prepare_history_window([sample(self.day, pnl="1e30", fee="-1e-30")], self.day)
        rates = {("USDT", day_ms(self.day) - MINUTE_MS): ".98"}
        with localcontext() as context:
            context.prec = 6
            result = value_history(records, [self.day], {self.day.isoformat()}, rates)
            self.assertEqual(context.prec, 6)
        self.assertEqual(result["usd"]["net_pnl"], "979999999999999999999999999999.99999999999999999999999999999902")
        rates[("USDT", day_ms(self.day) - MINUTE_MS)] = "1.123456789012345678901234567890123456789"
        with self.assertRaises(Inexact):
            value_history(records, [self.day], {self.day.isoformat()}, rates)

    def test_index_candles_reject_future_unconfirmed_duplicate_and_invalid_ohlc(self):
        timestamp = day_ms(self.day) - MINUTE_MS
        valid = [str(timestamp), "1", "1.01", ".98", ".99", "1"]
        self.assertEqual(parse_index_rate([valid], timestamp), "0.99")
        for rows in [
            [[str(timestamp + MINUTE_MS), *valid[1:]]], [[*valid[:5], "0"]], [valid, valid],
            [[*valid[:4], "NaN", "1"]], [[*valid[:4], "2", "1"]],
            [[str(timestamp + 1), *valid[1:]]], [[*valid[:5], []]], [{}], {},
        ]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                parse_index_rate(rows, timestamp)


class StoreFixture:
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "valuation.sqlite3"))
        self.day = datetime.now(timezone.utc).date() - timedelta(days=2)
        self.account = SimpleNamespace(account_scope="scope", configured=True)
        self.market = SimpleNamespace(rate_scope="market", historical_index_rate=AsyncMock(return_value=".98"))
        self.worker = AccountValuationWorker(self.store, self.account, self.market)

    def publish(self, rows=None, *, day=None, scope="scope"):
        day = day or self.day
        rows = [sample(day)] if rows is None else rows
        job = self.store.create_bill_import(scope, [day], now_ms())
        records, summary = prepare_history_window(rows, day)
        self.store.commit_bill_history_window(job["id"], scope, day, records, summary, now_ms())
        self.store.finish_bill_import(job["id"], scope, "completed")

    def create(self, days=None):
        return self.store.create_bill_valuation("scope", "market", days or [self.day])

    def report(self, *, scope="scope", source="market", days=None, limit=100):
        return self.store.bill_history(scope, days or [self.day], rate_scope=source, limit=limit)


class ValuationStoreTests(StoreFixture, unittest.TestCase):
    def test_cache_is_source_scoped_persistent_and_report_is_not_paginated(self):
        self.publish([sample(self.day, str(index)) for index in range(102)])
        candle = day_ms(self.day) - MINUTE_MS
        self.store.save_historical_rate("market", "USDT", candle, ".98")
        self.store.save_historical_rate("other", "USDT", candle, ".5")
        report = self.report(limit=2)
        self.assertEqual(len(report["data"]), 2)
        self.assertEqual(report["valuation"]["daily"][0]["valued_rows"], 102)
        self.assertEqual(Decimal(report["valuation"]["usd"]["net_pnl"]), Decimal("989.604"))
        self.assertEqual(self.report(source="missing")["valuation"]["status"], "incomplete")
        self.assertIsNone(self.report(scope="other")["valuation"]["usd"])
        reopened = StateStore(str(self.store.path))
        self.assertEqual(reopened.historical_rate("market", "USDT", candle), ".98")
        self.assertEqual(self.store.bill_snapshot("scope")["data"], [])

    def test_reimport_uses_current_rows_and_new_missing_quote_invalidates_total(self):
        self.publish()
        self.store.save_historical_rate("market", "USDT", day_ms(self.day) - MINUTE_MS, ".98")
        self.assertEqual(self.report()["valuation"]["status"], "valued")
        self.publish([sample(self.day, "2", ccy="BTC")])
        self.assertEqual(self.report()["valuation"]["status"], "incomplete")
        self.assertIsNone(self.report()["valuation"]["usd"])
        self.assertIsNone(self.report()["data"][0]["historical_valuation"])

    def test_jobs_require_coverage_are_idempotent_and_private(self):
        with self.assertRaisesRegex(ValueError, "history_incomplete"):
            self.create()
        self.publish()
        self.publish([], day=self.day + timedelta(days=1))
        first = self.create()
        self.assertEqual(self.create()["id"], first["id"])
        with self.assertRaises(BillImportBusy):
            self.create([self.day, self.day + timedelta(days=1)])
        self.assertIsNone(self.store.bill_valuation("other"))
        self.assertNotIn("account_scope", first)
        self.assertNotIn("market_scope", first)
        self.assertNotIn("lease_owner", first)
        self.assertFalse(self.store.cancel_bill_valuation(first["id"], "other"))
        self.assertTrue(self.store.cancel_bill_valuation(first["id"], "scope"))
        self.assertIsNone(self.store.claim_bill_valuation("scope", "market", "worker", now_ms()))

    def test_lease_takeover_fences_old_owner_and_history_replacement(self):
        self.publish()
        job = self.create()
        now = now_ms()
        self.store.claim_bill_valuation("scope", "market", "first", now)
        self.assertIsNone(self.store.claim_bill_valuation("scope", "market", "second", now))
        self.assertIsNotNone(self.store.claim_bill_valuation("scope", "market", "second", now + 30_001))
        records, captured = self.store.valuation_day("scope", self.day)
        with self.assertRaises(ValuationLeaseLost):
            self.store.complete_valuation_day(job["id"], "scope", "first", self.day, captured, 0, 0, now + 30_001)
        self.publish([])
        with self.assertRaisesRegex(ValueError, "history_changed"):
            self.store.complete_valuation_day(job["id"], "scope", "second", self.day, captured, 0, 0, now + 30_002)
        self.assertEqual(self.store.bill_valuation("scope")["completed_days"], 0)

    def test_cache_write_checks_cancellation_and_market_inside_transaction(self):
        self.publish()
        job = self.create()
        now = now_ms()
        self.store.claim_bill_valuation("scope", "market", "worker", now)
        lease = (job["id"], "scope", "worker", now + 1)
        with self.assertRaises(ValuationLeaseLost):
            self.store.save_historical_rate("other", "USDT", day_ms(self.day) - MINUTE_MS, ".98", lease=lease)
        self.store.cancel_bill_valuation(job["id"], "scope")
        with self.assertRaises(ValuationLeaseLost):
            self.store.save_historical_rate("market", "USDT", day_ms(self.day) - MINUTE_MS, ".98", lease=lease)
        self.assertIsNone(self.store.historical_rate("market", "USDT", day_ms(self.day) - MINUTE_MS))


class ValuationWorkerTests(StoreFixture, unittest.IsolatedAsyncioTestCase):
    async def test_worker_fetches_unique_rates_and_reuses_cache_without_private_operations(self):
        self.publish([sample(self.day, str(index)) for index in range(20)])
        self.create()
        await self.worker.run_once()
        self.assertEqual(self.store.bill_valuation("scope")["state"], "completed")
        self.assertEqual(self.market.historical_index_rate.await_count, 1)
        self.assertEqual(self.report()["valuation"]["status"], "valued")
        self.create()
        await self.worker.run_once()
        self.assertEqual(self.market.historical_index_rate.await_count, 1)
        self.assertEqual(self.store.bill_valuation("scope")["rates_loaded"], 0)

    async def test_missing_rate_is_partial_and_retry_can_complete(self):
        self.publish()
        self.market.historical_index_rate.side_effect = HistoricalValuationError("historical_rate_unavailable")
        self.create()
        await self.worker.run_once()
        self.assertEqual(self.store.bill_valuation("scope")["state"], "partial")
        self.assertEqual(self.store.bill_valuation("scope")["rates_missing"], 1)
        self.assertIsNone(self.report()["valuation"]["usd"])
        self.market.historical_index_rate.side_effect = None
        self.create()
        await self.worker.run_once()
        self.assertEqual(self.store.bill_valuation("scope")["state"], "completed")

    async def test_network_failure_does_not_persist_remote_secrets_or_partial_completion(self):
        self.publish()
        self.market.historical_index_rate.side_effect = OkxMarketError("proxy https://user:private-password@invalid")
        self.create()
        await self.worker.run_once()
        job = self.store.bill_valuation("scope")
        self.assertEqual(job["state"], "failed")
        self.assertEqual(job["completed_days"], 0)
        self.assertEqual(job["error"], "market_request_failed")
        self.assertNotIn("private-password", json.dumps(job))

    async def test_cancellation_requeues_and_new_worker_resumes_cached_quotes(self):
        self.publish([sample(self.day), sample(self.day, "2", ccy="BTC")])
        entered = asyncio.Event()

        async def rates(currency, _):
            if currency == "BTC":
                return "50000"
            entered.set()
            await asyncio.Event().wait()

        self.market.historical_index_rate.side_effect = rates
        self.create()
        running = asyncio.create_task(self.worker.run_once())
        await asyncio.wait_for(entered.wait(), 2)
        running.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await running
        self.assertEqual(self.store.bill_valuation("scope")["state"], "queued")
        self.assertEqual(self.store.historical_rate("market", "BTC", day_ms(self.day) - MINUTE_MS), "50000")
        self.market.historical_index_rate = AsyncMock(return_value=".98")
        await AccountValuationWorker(StateStore(str(self.store.path)), self.account, self.market).run_once()
        self.market.historical_index_rate.assert_awaited_once_with("USDT", day_ms(self.day) - MINUTE_MS)
        self.assertEqual(self.store.bill_valuation("scope")["state"], "completed")

    async def test_explicit_cancel_prevents_late_response_from_publishing(self):
        self.publish()
        entered, release = asyncio.Event(), asyncio.Event()
        async def quote(*_):
            entered.set()
            await release.wait()
            return ".98"
        self.market.historical_index_rate.side_effect = quote
        job = self.create()
        task = asyncio.create_task(self.worker.run_once())
        await asyncio.wait_for(entered.wait(), 2)
        self.store.cancel_bill_valuation(job["id"], "scope")
        release.set()
        await task
        self.assertEqual(self.store.bill_valuation("scope")["state"], "canceled")
        self.assertIsNone(self.store.historical_rate("market", "USDT", day_ms(self.day) - MINUTE_MS))

    async def test_client_uses_bounded_index_query_and_validates_candle(self):
        client = OkxMarketClient()
        timestamp = day_ms(self.day) - MINUTE_MS
        client.get = AsyncMock(return_value=[[str(timestamp), "1", "1", ".98", ".99", "1"]])
        self.assertEqual(await client.historical_index_rate("USDT", timestamp), "0.99")
        client.get.assert_awaited_once_with("/api/v5/market/history-index-candles", {
            "instId": "USDT-USD", "bar": "1m", "after": str(timestamp + MINUTE_MS),
            "before": str(timestamp - 1), "limit": "2",
        })

    async def test_api_auth_scope_validation_and_job_are_read_only(self):
        self.publish()
        with patch.object(api_main, "state_store", self.store), patch.object(api_main, "account_client", self.account), \
             patch.object(api_main, "market_client", self.market), patch.object(api_main, "account_valuation", self.worker), \
             patch.dict(os.environ, {"ADMIN_API_TOKEN": "local-valuation-test"}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="http://local") as client:
                dates = {"start_day": str(self.day), "end_day": str(self.day)}
                self.assertEqual((await client.post("/api/v1/account/bills/valuation", json=dates)).status_code, 401)
                client.headers["X-Admin-Token"] = "local-valuation-test"
                response = await client.post("/api/v1/account/bills/valuation", json=dates)
                self.assertEqual(response.status_code, 202)
                job = response.json()["job"]
                await self.worker.run_once()
                report = (await client.get("/api/v1/account/bills/history", params=dates)).json()
                self.assertEqual(report["valuation"]["status"], "valued")
                self.account.account_scope = "other"
                self.assertIsNone((await client.get("/api/v1/account/bills/valuation")).json()["job"])
                self.assertEqual((await client.post(f"/api/v1/account/bills/valuation/{job['id']}/cancel")).status_code, 404)
                self.assertEqual((await client.post("/api/v1/account/bills/valuation", json=dates)).status_code, 422)

    async def test_private_sse_publishes_valuation_progress_without_source_identifiers(self):
        self.publish()
        account = SimpleNamespace(configured=True, connected=True, authenticated=True, balance=[], last_message_at=None)
        events = private_events(self.store, account, self.account, lambda: True)
        self.addAsyncCleanup(close_events, events)
        for _ in range(20):
            if "event: heartbeat" in await anext(events):
                break
        self.create()
        async with asyncio.timeout(2):
            while True:
                frame = await anext(events)
                if "event: bill_valuation" in frame:
                    self.assertIn('"queued"', frame)
                    self.assertNotIn("market_scope", frame)
                    self.assertNotIn("account_scope", frame)
                    break


@unittest.skipIf(os.name == "nt", "The inherited Uvicorn socket requires POSIX")
class ValuationProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_api_import_value_push_and_restart_without_exchange_mutations(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        exchange = ExchangeServer()
        self.addAsyncCleanup(exchange.close)
        await exchange.start()
        api = ApiProcess(directory.name, exchange)
        api.environment["EXECUTION_ENABLED"] = "false"
        self.addAsyncCleanup(api.stop)
        await api.start()
        day = datetime.now(timezone.utc).date() - timedelta(days=2)
        exchange.archive_bills = [sample(day), sample(day, "2", type="1", instType="", instId="", balChg="500")]
        dates = {"start_day": str(day), "end_day": str(day)}
        job = (await api.request("POST", "/account/bills/imports", dates, status=202))["job"]
        async def imported():
            return (await api.request("GET", f"/account/bills/imports/{job['id']}"))["job"]["status"] == "completed"
        await eventually(imported)
        async with api.client.stream("GET", "/api/v1/account/events") as stream:
            await api.request("POST", "/account/bills/valuation", dates, status=202)
            payload = await next_event(stream.aiter_lines(), "bill_valuation", lambda row: row["job"] and row["job"]["state"] == "completed")
            self.assertEqual(payload["job"]["completed_days"], 1)
        query = f"/account/bills/history?start_day={day}&end_day={day}"
        report = await api.request("GET", query)
        self.assertEqual(Decimal(report["valuation"]["usd"]["net_pnl"]), Decimal("9.702"))
        self.assertEqual(Decimal(report["valuation"]["usd"]["cash_flow"]), Decimal("490"))
        await api.stop()
        await api.start()
        self.assertEqual((await api.request("GET", query))["valuation"], report["valuation"])
        self.assertFalse((await api.request("GET", "/execution/status"))["execution_enabled"])
        self.assertEqual(exchange.posts, [])
        self.assertEqual(exchange.errors, [])
