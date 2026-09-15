import asyncio
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
from app.account_history import AccountHistoryImporter
from app.historical_ledger import (
    DAY_MS, archive_first_day, combine_history_windows, day_ms, history_days, prepare_history_window,
)
from app.okx_account import OkxAccountClient, OkxAccountError
from app.state_store import BillImportBusy, BillImportLeaseLost, StateStore
from tests.fixtures.api_process import ApiProcess, eventually
from tests.fixtures.exchange_server import ExchangeServer


def now_ms():
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def sample(day, bill_id="1", **changes):
    return {
        "billId": bill_id, "ts": str(day_ms(day) + 1000), "ccy": "USDT",
        "instId": "BTC-USDT-SWAP", "instType": "SWAP", "type": "2", "subType": "5",
        "pnl": "10", "fee": "-.1", "balChg": "9.9", "posBalChg": "0", **changes,
    }


class HistoricalLedgerTests(unittest.TestCase):
    day = date(2026, 9, 12)

    def test_account_transfers_are_not_trading_pnl_and_currencies_stay_separate(self):
        records, summary = prepare_history_window([
            sample(self.day),
            sample(self.day, "2", type="1", instId="", instType="", balChg="500"),
            sample(self.day, "3", type="1", instId="", instType="", balChg="-25"),
            sample(self.day, "4", type="6", balChg="-100"),
            sample(self.day, "5", ccy="BTC", pnl="0.00000000000000001", fee="0"),
            sample(self.day, "6", type="8", subType="173", fee="0", balChg="0", posBalChg="-1.25"),
        ], self.day)
        self.assertEqual(summary["trading_account_transfers"], {"USDT": "475"})
        self.assertEqual(Decimal(summary["swap_pnl_by_currency"]["USDT"]["net_pnl"]), Decimal("8.65"))
        self.assertEqual(Decimal(summary["swap_pnl_by_currency"]["BTC"]["net_pnl"]), Decimal("1e-17"))
        self.assertIsNone(records[1]["net_pnl"])
        self.assertEqual(summary["counts"]["internal_transfer"], 1)
        self.assertEqual(summary["valuation_status"], "not_valued")

    def test_unknown_types_invalid_amounts_and_other_markets_are_preserved_as_unclassified(self):
        records, summary = prepare_history_window([
            sample(self.day, type="999"),
            sample(self.day, "2", fee="NaN"),
            sample(self.day, "3", instType="SPOT", instId="BTC-USDT"),
            sample(self.day, "4", type="1", balChg="100", posBalChg="100"),
        ], self.day)
        self.assertEqual(summary["counts"]["unclassified"], 3)
        self.assertEqual(summary["counts"]["non_swap"], 1)
        self.assertEqual(summary["swap_pnl_by_currency"], {})
        self.assertEqual(records[0]["raw"]["type"], "999")
        self.assertIsNone(records[1]["net_pnl"])

    def test_history_arithmetic_preserves_large_and_tiny_values_independent_of_context(self):
        rows = [
            sample(self.day, pnl="1000000000000000000000000000000", fee="-0.000000000000000000000000000001"),
            sample(self.day, "2", type="1", balChg="1000000000000000000000000000000"),
            sample(self.day, "3", type="1", balChg="0.000000000000000000000000000001"),
        ]
        with localcontext() as caller:
            caller.prec = 6
            records, summary = prepare_history_window(rows, self.day)
            combined = combine_history_windows([summary, summary])
            self.assertEqual(caller.prec, 6)
        self.assertEqual(records[0]["net_pnl"], "999999999999999999999999999999.999999999999999999999999999999")
        self.assertEqual(combined["swap_pnl_by_currency"]["USDT"]["net_pnl"], "1999999999999999999999999999999.999999999999999999999999999998")
        self.assertEqual(combined["trading_account_transfers"]["USDT"], "2000000000000000000000000000000.000000000000000000000000000002")

    def test_history_arithmetic_refuses_inexact_values_instead_of_rounding(self):
        with self.assertRaises(Inexact):
            prepare_history_window([sample(self.day, pnl="1e80", fee="1")], self.day)
        _, large = prepare_history_window([sample(self.day, pnl="1e80", fee="0")], self.day)
        _, small = prepare_history_window([sample(self.day, pnl="1", fee="0")], self.day)
        with self.assertRaises(Inexact):
            combine_history_windows([large, small])

    def test_malformed_identity_and_out_of_window_fail_whole_day(self):
        for changes in [
            {"billId": ""}, {"ccy": ""}, {"ts": "1.5"},
            {"ts": str(day_ms(self.day) - 1)}, {"ts": str(day_ms(self.day) + DAY_MS)},
            {"unexpected": float("nan")},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                prepare_history_window([sample(self.day), sample(self.day, "2", **changes)], self.day)
        with self.assertRaises(ValueError):
            prepare_history_window([sample(self.day), sample(self.day)], self.day)

    def test_closed_days_calendar_retention_and_read_range(self):
        now = datetime(2024, 5, 31, 12, tzinfo=timezone.utc)
        self.assertEqual(archive_first_day(now), date(2024, 3, 1))
        self.assertEqual(len(history_days(date(2024, 3, 1), date(2024, 5, 30), now=now, importing=True)), 91)
        for start, end in [(date(2024, 2, 29), date(2024, 3, 1)), (date(2024, 5, 31), date(2024, 5, 31))]:
            with self.assertRaises(ValueError):
                history_days(start, end, now=now, importing=True)
        self.assertEqual(len(history_days(date(2023, 1, 1), date(2023, 1, 3), now=now)), 3)
        with self.assertRaises(ValueError):
            history_days(date(2023, 1, 1), date(2024, 5, 30), now=now)


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "ledger.sqlite3"))
        self.day = datetime.now(timezone.utc).date() - timedelta(days=2)

    def publish(self, rows, scope="scope", day=None):
        day = day or self.day
        job = self.store.create_bill_import(scope, [day], now_ms())
        records, summary = prepare_history_window(rows, day)
        self.store.commit_bill_history_window(job["id"], scope, day, records, summary, now_ms())
        self.store.finish_bill_import(job["id"], scope, "completed")
        return job

    def test_atomic_refresh_and_idempotent_account_scoped_records(self):
        rows = [sample(self.day), sample(self.day, "2")]
        self.publish(rows)
        self.publish(rows)
        self.publish([sample(self.day, pnl="-100")], scope="other")
        report = self.store.bill_history("scope", [self.day])
        self.assertEqual(report["total"], 2)
        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual(self.store.bill_history("other", [self.day])["total"], 1)
        self.assertEqual(self.store.bill_snapshot("scope")["data"], [])
        self.assertIsNone(self.store.bill_snapshot("scope")["summary"])

    def test_failed_commit_rolls_back_window_and_job_progress(self):
        self.publish([sample(self.day)])
        job = self.store.create_bill_import("scope", [self.day], now_ms())
        records, summary = prepare_history_window([sample(self.day, pnl="999")], self.day)
        with self.assertRaises(Exception):
            self.store.commit_bill_history_window(job["id"], "scope", self.day, records + records, summary, now_ms())
        self.assertEqual(self.store.bill_history("scope", [self.day])["data"][0]["realized_pnl"], "10")
        self.assertEqual(self.store.bill_import("scope", job["id"])["completed_days"], 0)

    def test_partial_coverage_and_full_summary_are_independent_of_list_limit(self):
        self.publish([sample(self.day, str(index)) for index in range(103)])
        report = self.store.bill_history("scope", [self.day, self.day + timedelta(days=1)], limit=2)
        self.assertFalse(report["coverage"]["complete"])
        self.assertEqual(report["coverage"]["missing_days"], [(self.day + timedelta(days=1)).isoformat()])
        self.assertEqual(report["summary"]["rows"], 103)
        self.assertEqual(len(report["data"]), 2)
        self.assertIsNone(report["summary"]["net_return"])

    def test_cursor_handles_same_timestamp_and_never_crosses_account_scope(self):
        self.publish([sample(self.day, str(index)) for index in range(7)])
        found, cursor = [], {}
        while True:
            result = self.store.bill_history("scope", [self.day], limit=2, **cursor)
            found.extend(row["bill_id"] for row in result["data"])
            if not result["next_cursor"]:
                break
            cursor = {"before_ts": result["next_cursor"]["timestamp_ms"], "before_id": result["next_cursor"]["bill_id"]}
        self.assertEqual(len(set(found)), 7)
        self.assertEqual(len(found), 7)
        self.assertEqual(self.store.bill_history("other", [self.day], **cursor)["data"], [])
        with self.assertRaises(ValueError):
            self.store.bill_history("scope", [self.day], before_ts=1)

    def test_shared_lease_prevents_concurrent_jobs_and_fences_expired_owner(self):
        first = self.store.create_bill_import("scope", [self.day], now_ms() - 120_001)
        second = StateStore(str(self.store.path))
        job = second.create_bill_import("scope", [self.day], now_ms())
        self.assertEqual(self.store.bill_import("scope", first["id"])["status"], "interrupted")
        with self.assertRaises(BillImportBusy):
            self.store.create_bill_import("scope", [self.day], now_ms())
        records, summary = prepare_history_window([sample(self.day)], self.day)
        with self.assertRaises(BillImportLeaseLost):
            self.store.commit_bill_history_window(first["id"], "scope", self.day, records, summary, now_ms())
        self.store.commit_bill_history_window(job["id"], "scope", self.day, records, summary, now_ms())
        self.assertEqual(self.store.bill_import("scope", job["id"])["completed_days"], 1)

    def test_empty_day_has_real_coverage_and_survives_reopen(self):
        self.publish([])
        restored = StateStore(str(self.store.path))
        result = restored.bill_history("scope", [self.day])
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["total"], 0)
        self.assertFalse(restored.bill_history("scope", [self.day + timedelta(days=1)])["coverage"]["complete"])


class HistoryImporterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "ledger.sqlite3"))
        self.day = datetime.now(timezone.utc).date() - timedelta(days=2)
        self.client = SimpleNamespace(configured=True, account_scope="scope", bills_archive=AsyncMock(return_value=[]))
        self.importer = AccountHistoryImporter(self.store, self.client)
        self.addAsyncCleanup(self.importer.close)

    async def done(self, job):
        async def terminal():
            return self.store.bill_import("scope", job["id"])["status"] != "running"
        await eventually(terminal)
        return self.store.bill_import("scope", job["id"])

    async def test_partial_failure_preserves_completed_day_and_can_retry(self):
        self.client.bills_archive.side_effect = [
            [sample(self.day)], OkxAccountError("socks5://secret-proxy-password"),
        ]
        job = await self.importer.start(self.day, self.day + timedelta(days=1))
        result = await self.done(job)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["completed_days"], 1)
        self.assertNotIn("password", str(result))
        report = self.store.bill_history("scope", [self.day, self.day + timedelta(days=1)])
        self.assertFalse(report["coverage"]["complete"])
        self.client.bills_archive.side_effect = [[sample(self.day)], []]
        retry = await self.importer.start(self.day, self.day + timedelta(days=1))
        self.assertEqual((await self.done(retry))["status"], "completed")
        self.assertTrue(self.store.bill_history("scope", [self.day, self.day + timedelta(days=1)])["coverage"]["complete"])
        self.assertEqual(self.store.bill_history("scope", [self.day])["total"], 1)

    async def test_cancel_clears_running_job_without_publishing_incomplete_day(self):
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.client.bills_archive.side_effect = pending
        job = await self.importer.start(self.day, self.day)
        await entered.wait()
        await self.importer.close()
        self.assertEqual(self.store.bill_import("scope", job["id"])["status"], "interrupted")
        self.assertFalse(self.store.bill_history("scope", [self.day])["coverage"]["complete"])

    async def test_account_changed_during_request_cannot_publish_to_old_scope(self):
        async def changed(*args, **kwargs):
            self.client.account_scope = "other"
            return [sample(self.day)]
        self.client.bills_archive.side_effect = changed
        job = await self.importer.start(self.day, self.day)
        self.assertEqual((await self.done(job))["status"], "failed")
        self.assertEqual(self.store.bill_history("scope", [self.day])["total"], 0)

    async def test_archive_transport_fixed_window_signing_pagination_and_spacing(self):
        requests, starts = [], []
        def handle(request):
            starts.append(asyncio.get_running_loop().time())
            requests.append(request)
            page = [sample(self.day, str(index)) for index in range(100)] if "after" not in request.url.params else [sample(self.day, "101")]
            return httpx.Response(200, json={"code": "0", "data": page})
        with patch.dict(os.environ, {"OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass", "OKX_DEMO": "true"}):
            client = OkxAccountClient(httpx.MockTransport(handle))
        heartbeat = AsyncMock()
        rows = await client.bills_archive(day_ms(self.day), day_ms(self.day) + DAY_MS, on_page=heartbeat)
        self.assertEqual(len(rows), 101)
        self.assertEqual(heartbeat.await_count, 2)
        self.assertGreaterEqual(starts[1] - starts[0], .39)
        self.assertNotIn("instType", requests[0].url.params)
        self.assertEqual(requests[1].url.params["after"], "99")
        self.assertEqual(requests[0].url.params["begin"], str(day_ms(self.day) - 1))
        self.assertEqual(requests[0].url.params["end"], str(day_ms(self.day) + DAY_MS))
        self.assertEqual(requests[0].headers["ok-access-sign"], client.signature(
            requests[0].headers["ok-access-timestamp"], "GET", requests[0].url.raw_path.decode(), secret_key="secret",
        ))

    async def test_archive_padding_keeps_exact_boundaries_without_adjacent_day_rows(self):
        client = OkxAccountClient()
        start = day_ms(self.day)
        client._get = AsyncMock(return_value=[
            sample(self.day, str(index), ts=str(timestamp))
            for index, timestamp in enumerate([start - 1, start, start + DAY_MS - 1, start + DAY_MS])
        ])
        result = await client.bills_archive(start, start + DAY_MS)
        self.assertEqual([row["ts"] for row in result], [str(start), str(start + DAY_MS - 1)])

    async def test_failed_second_page_and_repeated_cursor_never_publish_first_page(self):
        client = OkxAccountClient()
        client.api_key, client.secret_key, client.passphrase = "key", "secret", "pass"
        client._get = AsyncMock(side_effect=[
            [sample(self.day, str(index)) for index in range(100)], OkxAccountError("page failed"),
        ])
        importer = AccountHistoryImporter(self.store, client)
        self.addAsyncCleanup(importer.close)
        job = await importer.start(self.day, self.day)
        async def terminal():
            return self.store.bill_import(client.account_scope, job["id"])["status"] != "running"
        await eventually(terminal)
        self.assertEqual(self.store.bill_history(client.account_scope, [self.day])["total"], 0)
        client._get = AsyncMock(return_value=[sample(self.day, "same") for _ in range(100)])
        with self.assertRaises(OkxAccountError):
            await client.bills_archive(day_ms(self.day), day_ms(self.day) + DAY_MS)

    async def test_api_auth_bounds_scoping_and_background_job(self):
        with patch.object(api_main, "state_store", self.store), patch.object(api_main, "account_client", self.client), patch.object(api_main, "account_history", self.importer), patch.dict(os.environ, {"ADMIN_API_TOKEN": "local-history-test"}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="http://local") as client:
                dates = {"start_day": self.day.isoformat(), "end_day": self.day.isoformat()}
                self.assertEqual((await client.post("/api/v1/account/bills/imports", json=dates)).status_code, 401)
                client.headers["X-Admin-Token"] = "local-history-test"
                response = await client.post("/api/v1/account/bills/imports", json=dates)
                self.assertEqual(response.status_code, 202)
                job = response.json()["job"]
                await self.done(job)
                response = await client.get("/api/v1/account/bills/history", params=dates)
                self.assertTrue(response.json()["coverage"]["complete"])
                self.assertEqual((await client.get("/api/v1/account/bills/history", params={**dates, "before_ts": 1})).status_code, 422)
                self.client.account_scope = "other"
                self.assertEqual((await client.get(f"/api/v1/account/bills/imports/{job['id']}")).status_code, 404)
                self.assertFalse((await client.get("/api/v1/account/bills/history", params=dates)).json()["coverage"]["complete"])


@unittest.skipIf(os.name == "nt", "The inherited Uvicorn socket requires POSIX")
class HistoryProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_api_archive_import_restart_and_failed_page_retry(self):
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
        exchange.archive_bills = [sample(day, str(index)) for index in range(102)]
        dates = {"start_day": day.isoformat(), "end_day": day.isoformat()}
        exchange.archive_fail_after = True
        job = (await api.request("POST", "/account/bills/imports", dates, status=202))["job"]
        async def finished():
            return (await api.request("GET", f"/account/bills/imports/{job['id']}"))["job"]["status"] != "running"
        await eventually(finished)
        query = f"/account/bills/history?start_day={day}&end_day={day}&limit=2"
        self.assertFalse((await api.request("GET", query))["coverage"]["complete"])
        exchange.archive_fail_after = False
        job = (await api.request("POST", "/account/bills/imports", dates, status=202))["job"]
        await eventually(finished)
        report = await api.request("GET", query)
        self.assertEqual(report["total"], 102)
        self.assertTrue(report["coverage"]["complete"])
        self.assertEqual(len(report["data"]), 2)
        self.assertEqual(report["summary"]["rows"], 102)
        await api.stop()
        await api.start()
        self.assertEqual((await api.request("GET", query))["total"], 102)
        self.assertEqual((await api.request("GET", "/account/bills"))["data"], [])
        self.assertFalse((await api.request("GET", "/execution/status"))["execution_enabled"])
        self.assertEqual(exchange.posts, [])
        self.assertEqual(exchange.errors, [])
