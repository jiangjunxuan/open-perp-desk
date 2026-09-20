import asyncio
import csv
import gzip
import io
import json
import os
import socket
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app.bill_archive import BillArchiveDownloader, BillArchiveError, parse_bill_archive, quarter_days
from app import main as api_main
from app.historical_ledger import day_ms
from app.okx_account import OkxAccountClient, OkxAccountError
from app.quarterly_history import QuarterlyHistoryImporter, _now_ms
from app.state_store import BillImportLeaseLost, StateStore


FIELDS = ["billId", "ts", "ccy", "instId", "instType", "subType", "balChg", "posBalChg", "pnl", "fee"]


def csv_bytes(rows=None, fields=FIELDS):
    rows = rows if rows is not None else [{
        "billId": "1", "ts": str(day_ms(date(2024, 1, 1))), "ccy": "USDT",
        "instId": "BTC-USDT-SWAP", "instType": "SWAP", "subType": "5",
        "balChg": "9.9", "posBalChg": "0", "pnl": "10", "fee": "-.1",
    }]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def zipped(content, name="bills.csv"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, content)
    return output.getvalue()


class ArchiveParserTests(unittest.TestCase):
    def test_calendar_quarters_and_first_retained_day(self):
        self.assertEqual(len(quarter_days(2024, "Q1")), 91)
        self.assertEqual(quarter_days(2021, "Q1")[0], date(2021, 2, 1))
        self.assertEqual(quarter_days(2024, "Q4")[-1], date(2024, 12, 31))
        now = datetime(2026, 9, 13, tzinfo=timezone.utc)
        for year, quarter in [(2020, "Q4"), (2026, "Q3"), (2027, "Q1"), (2024, "1"), (True, "Q1")]:
            with self.subTest(year=year, quarter=quarter), self.assertRaises(BillArchiveError):
                quarter_days(year, quarter, now=now)

    def test_plain_zip_and_gzip_keep_decimal_strings_and_infer_documented_type(self):
        for content in [csv_bytes(), zipped(csv_bytes()), gzip.compress(csv_bytes())]:
            with self.subTest(kind=content[:2]):
                windows = parse_bill_archive(content, 2024, "Q1", {"5": "2"})
                self.assertEqual(len(windows), 91)
                row = windows[date(2024, 1, 1)][0]
                self.assertEqual((row["fee"], row["type"], row["archive_type_source"]), ("-.1", "2", "okx_subtypes"))
                self.assertEqual(windows[date(2024, 3, 31)], [])

    def test_unknown_subtype_is_preserved_not_guessed(self):
        row = parse_bill_archive(csv_bytes(), 2024, "Q1", {})[date(2024, 1, 1)][0]
        self.assertNotIn("type", row)
        self.assertEqual(row["subType"], "5")

    def test_duplicate_rows_wrong_quarter_missing_columns_and_truncated_rows_fail(self):
        row = next(csv.DictReader(io.StringIO(csv_bytes().decode("utf-8-sig"))))
        payloads = [
            csv_bytes([row, row]), csv_bytes([{**row, "ts": str(day_ms(date(2024, 4, 1)))}]),
            b"billId,ts\n1,2\n", csv_bytes() + b"2,3\n",
            csv_bytes([{**row, "type": "8"}], [*FIELDS, "type"]),
        ]
        for content in payloads:
            with self.subTest(content=content[:40]), self.assertRaises(BillArchiveError):
                parse_bill_archive(content, 2024, "Q1", {"5": "2"})

    def test_archive_paths_and_expansion_limits(self):
        for content in [zipped(csv_bytes(), "../bills.csv"), b"PKbroken", gzip.compress(b"a" * 1024)]:
            with self.subTest(prefix=content[:2]), patch("app.bill_archive.MAX_CSV_BYTES", 100), self.assertRaises(BillArchiveError):
                parse_bill_archive(content, 2024, "Q1", {})
        with patch("app.bill_archive.MAX_ROWS", 0), self.assertRaisesRegex(BillArchiveError, "row_limit"):
            parse_bill_archive(csv_bytes(), 2024, "Q1", {})


class BytesStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"fixture"


class ArchiveTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_report_post_signs_exact_body_without_any_trade_request(self):
        calls = []
        def handle(request):
            calls.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"code": "0", "data": [{"result": "false"}]})
            if request.url.path.endswith("subtypes"):
                return httpx.Response(200, json={"code": "0", "data": [{"type": "2", "subTypeDetails": [{"subType": "5"}]}]})
            return httpx.Response(200, json={"code": "0", "data": [{"state": "ongoing"}]})
        client = OkxAccountClient(httpx.MockTransport(handle))
        client.api_key, client.secret_key, client.passphrase, client.proxy_url = "fixture", "secret", "passphrase", None
        await client.apply_bill_archive(2024, "Q1")
        self.assertEqual((await client.bill_archive_status(2024, "Q1"))["state"], "ongoing")
        self.assertEqual(await client.bill_subtypes(), {"5": "2"})
        request = calls[0]
        self.assertEqual(request.content, b'{"year":"2024","quarter":"Q1"}')
        self.assertEqual(request.headers["ok-access-sign"], client.signature(
            request.headers["ok-access-timestamp"], "POST", request.url.path, request.content.decode(), "secret",
        ))
        self.assertTrue(all("/trade/" not in str(item.url) for item in calls))

    async def test_download_pins_public_ip_and_does_not_forward_credentials(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, stream=BytesStream())
        downloader = BillArchiveDownloader(transport=httpx.MockTransport(handle))
        with patch("app.bill_archive.socket.getaddrinfo", return_value=[(socket.AF_INET, 1, 6, "", ("1.1.1.1", 443))]):
            self.assertEqual(await downloader.download("https://static.okx.com/report?signature=private"), b"fixture")
        request = calls[0]
        self.assertEqual(request.url.host, "1.1.1.1")
        self.assertEqual(request.headers["host"], "static.okx.com")
        self.assertEqual(request.extensions["sni_hostname"], "static.okx.com")
        self.assertFalse(any(name.lower().startswith("ok-access-") for name in request.headers))

    async def test_private_hosts_unapproved_hosts_redirects_and_large_responses_are_rejected(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "http://127.0.0.1"}, stream=BytesStream())
        downloader = BillArchiveDownloader(transport=httpx.MockTransport(handle))
        for url in ["http://static.okx.com/a", "https://other.example/a", "https://user:secret@static.okx.com/a"]:
            with self.assertRaises(BillArchiveError):
                await downloader.download(url)
        self.assertEqual(calls, [])
        with patch("app.bill_archive.socket.getaddrinfo", return_value=[(socket.AF_INET, 1, 6, "", ("127.0.0.1", 443))]):
            with self.assertRaisesRegex(BillArchiveError, "private_address"):
                await downloader.download("https://static.okx.com/a")
        with patch("app.bill_archive.socket.getaddrinfo", return_value=[(socket.AF_INET, 1, 6, "", ("1.1.1.1", 443))]):
            with self.assertRaisesRegex(BillArchiveError, "download_rejected"):
                await downloader.download("https://static.okx.com/a")
            downloader.transport = httpx.MockTransport(lambda _: httpx.Response(200, headers={"Content-Length": "999999999"}, stream=BytesStream()))
            with self.assertRaisesRegex(BillArchiveError, "too_large"):
                await downloader.download("https://static.okx.com/a")
        self.assertEqual(len(calls), 1)


class Client:
    configured = True
    account_scope = "fixture-scope"
    proxy_url = None

    def __init__(self):
        self.applications = 0
        self.state = "finished"

    async def apply_bill_archive(self, year, quarter):
        self.applications += 1
        return {"result": "false"}

    async def bill_archive_status(self, year, quarter):
        return {"state": self.state, "fileHref": "https://static.okx.com/private?signature=secret"}

    async def bill_subtypes(self):
        return {"5": "2"}


class ArchiveWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "ledger.sqlite3")
        self.store = StateStore(self.path)
        self.client = Client()
        self.downloader = AsyncMock()
        self.downloader.download.return_value = zipped(csv_bytes())
        self.service = self.make_service()

    def make_service(self):
        service = QuarterlyHistoryImporter(self.store, self.client, downloader=self.downloader)
        service.poll_ms = 0
        return service

    async def test_restart_waiting_job_does_not_reapply_and_imports_existing_ledger(self):
        job = await self.service.request(2024, "Q1")
        duplicate = await self.service.request(2024, "Q1")
        self.assertEqual(job["id"], duplicate["id"])
        await self.service.run_once()
        self.assertEqual(self.store.bill_archives(self.client.account_scope)[0]["state"], "waiting")
        self.store = StateStore(self.path)
        await self.make_service().run_once()
        current = self.store.bill_archives(self.client.account_scope)[0]
        self.assertEqual((current["state"], current["completed_days"]), ("completed", 91))
        self.assertEqual(self.client.applications, 1)
        history = self.store.bill_history(self.client.account_scope, quarter_days(2024, "Q1"))
        self.assertTrue(history["coverage"]["complete"])
        self.assertEqual(history["data"][0]["net_pnl"], "9.9")
        self.assertNotIn("signature", json.dumps(current))
        self.assertNotIn("lease_owner", current)

    async def test_failed_generation_and_bad_file_do_not_claim_coverage(self):
        await self.service.request(2024, "Q1")
        await self.service.run_once()
        self.client.state = "failed"
        await self.service.run_once()
        current = self.store.bill_archives(self.client.account_scope)[0]
        self.assertEqual((current["state"], current["error"]), ("failed", "archive_generation_failed"))
        self.client.state = "finished"
        self.downloader.download.return_value = b"bad"
        await self.service.request(2024, "Q1", retry=True)
        await self.service.run_once()
        await self.service.run_once()
        self.assertEqual(self.store.bill_archives(self.client.account_scope)[0]["state"], "failed")
        self.assertFalse(self.store.bill_history(self.client.account_scope, quarter_days(2024, "Q1"))["coverage"]["complete"])

    async def test_cancel_and_account_scope_change_prevent_publication(self):
        job = await self.service.request(2024, "Q1")
        await self.service.run_once()
        async def cancel(_):
            self.store.cancel_bill_archive(job["id"], self.client.account_scope)
            return zipped(csv_bytes())
        self.downloader.download.side_effect = cancel
        await self.service.run_once()
        self.assertEqual(self.store.bill_archives(self.client.account_scope)[0]["state"], "canceled")
        self.assertEqual(self.store.bill_history(self.client.account_scope, quarter_days(2024, "Q1"))["total"], 0)
        await self.service.request(2024, "Q1", retry=True)
        await self.service.run_once()
        async def change(_):
            self.client.account_scope = "different-account"
            return zipped(csv_bytes())
        self.downloader.download.side_effect = change
        await self.service.run_once()
        self.assertEqual(self.store.bill_archives("fixture-scope")[0]["state"], "failed")
        self.assertEqual(self.store.bill_archives("different-account"), [])

    async def test_lease_takeover_rejects_old_writer_and_completion_without_import(self):
        job = await self.service.request(2024, "Q1")
        now = _now_ms()
        self.store.claim_bill_archive(self.client.account_scope, "old", now)
        other = StateStore(self.path)
        self.assertIsNone(other.claim_bill_archive(self.client.account_scope, "new", now + 1))
        other.claim_bill_archive(self.client.account_scope, "new", now + 120_001)
        with self.assertRaises(BillImportLeaseLost):
            self.store.touch_bill_archive(job["id"], self.client.account_scope, "old", now + 120_002)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            other.update_bill_archive(job["id"], self.client.account_scope, "new", now + 120_002, state="completed")

    async def test_http_auth_validation_idempotency_and_account_scope(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "archive-http-fixture"}), \
                patch.object(api_main, "state_store", self.store), \
                patch.object(api_main, "account_client", self.client), \
                patch.object(api_main, "quarterly_history", self.service):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="http://test") as api:
                route = "/api/v1/account/bills/archives"
                self.assertEqual((await api.get(route)).status_code, 401)
                api.headers["X-Admin-Token"] = "archive-http-fixture"
                self.assertEqual((await api.post(route, json={"year": True, "quarter": "Q1"})).status_code, 422)
                self.assertEqual((await api.post(route, json={"year": 2024, "quarter": "Q9"})).status_code, 422)
                response = await api.post(route, json={"year": 2024, "quarter": "Q1"})
                self.assertEqual(response.status_code, 202)
                job = response.json()["job"]
                self.assertEqual(self.client.applications, 0)
                duplicate = await api.post(route, json={"year": 2024, "quarter": "Q1"})
                self.assertEqual(duplicate.json()["job"]["id"], job["id"])
                self.client.account_scope = "other-account"
                self.assertEqual((await api.get(route)).json(), {"data": []})
                self.assertEqual((await api.post(f"{route}/{job['id']}/cancel")).status_code, 404)
                self.client.account_scope = "fixture-scope"
                self.assertEqual((await api.post(f"{route}/{job['id']}/cancel")).json()["data"][0]["state"], "canceled")
