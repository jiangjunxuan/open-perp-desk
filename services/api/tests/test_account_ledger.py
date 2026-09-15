import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import main as api_main
from app.account_ledger import AccountLedgerError, parse_daily_bills, value_daily_risk
from app.account_sync import AccountSynchronizer
from app.okx_account import OkxAccountClient, OkxAccountError
from app.state_store import StateStore


def bill(bill_id="1", **changes):
    return {
        "billId": bill_id, "instId": "BTC-USDT-SWAP", "instType": "SWAP",
        "type": "2", "subType": "5", "ccy": "USDT", "pnl": "-10",
        "fee": "-1", "balChg": "-11", "posBalChg": "0",
        "ts": "1789257600000",
        **changes,
    }


AS_OF = datetime(2026, 9, 13, 12, tzinfo=timezone.utc)


class AccountLedgerTests(unittest.TestCase):
    def test_trade_fees_and_funding_are_counted_once(self):
        rows = [
            bill(),
            bill("2", type="8", subType="173", balChg="-2", pnl="-2", fee="0"),
            bill("3", type="8", subType="174", balChg="0.5", pnl="0.5", fee="0"),
        ]
        parsed, summary = parse_daily_bills(rows + rows, AS_OF)
        self.assertEqual(len(parsed), 3)
        self.assertEqual(summary["by_currency"]["USDT"], {
            "realized_pnl": "-10", "fees": "-1", "funding": "-1.5",
            "adjustments": "0", "net_pnl": "-12.5",
        })

    def test_isolated_funding_uses_position_balance_without_double_counting(self):
        _, summary = parse_daily_bills([
            bill(type="8", subType="173", balChg="0", posBalChg="-0.125", pnl="-0.125", fee="0"),
        ], AS_OF)
        self.assertEqual(summary["by_currency"]["USDT"]["funding"], "-0.125")
        with self.assertRaisesRegex(AccountLedgerError, "ambiguous"):
            parse_daily_bills([bill(type="8", subType="173", balChg="-1", posBalChg="-2")], AS_OF)

    def test_transfers_do_not_create_profit(self):
        _, summary = parse_daily_bills([
            bill(type="1", subType="11", instType="", instId="", balChg="50000"),
            bill("2", type="6", subType="160", balChg="-100"),
        ], AS_OF)
        self.assertEqual(summary["excluded_transfers"], 2)
        self.assertEqual(summary["by_currency"], {})

    def test_liquidation_adl_and_deductions_are_included(self):
        _, summary = parse_daily_bills([
            bill(type="5"), bill("2", type="9"),
            bill("3", type="7", balChg="-2"), bill("4", type="10", balChg="-3"),
        ], AS_OF)
        self.assertEqual(summary["by_currency"]["USDT"]["net_pnl"], "-27")
        self.assertEqual(summary["by_currency"]["USDT"]["adjustments"], "-5")

    def test_currencies_stay_separate_and_usd_valuation_is_explicit(self):
        _, summary = parse_daily_bills([
            bill(), bill("2", ccy="BTC", pnl="-0.0002", fee="-0.00001"),
        ], AS_OF)
        result = value_daily_risk(summary, {"USDT": Decimal("0.99"), "BTC": Decimal(50000)}, Decimal(0))
        self.assertEqual(result["usd"]["net_pnl"], -21.39)
        self.assertEqual(set(result["by_currency"]), {"USDT", "BTC"})
        with self.assertRaisesRegex(AccountLedgerError, "currency_valuation"):
            value_daily_risk(summary, {"USDT": Decimal(1)}, Decimal(0))

    def test_positive_unrealized_pnl_does_not_mask_daily_loss(self):
        _, summary = parse_daily_bills([bill()], AS_OF)
        self.assertEqual(value_daily_risk(summary, {"USDT": Decimal(1)}, Decimal(100))["risk_pnl_usd"], -11)
        self.assertEqual(value_daily_risk(summary, {"USDT": Decimal(1)}, Decimal(-5))["risk_pnl_usd"], -16)

    def test_precision_and_more_than_500_bills_are_not_truncated(self):
        _, summary = parse_daily_bills([
            bill(str(index), pnl="0", fee="-0.00000000000000001") for index in range(701)
        ], AS_OF)
        self.assertEqual(summary["bills"], 701)
        self.assertEqual(Decimal(summary["by_currency"]["USDT"]["net_pnl"]), Decimal("-0.00000000000000701"))

    def test_wrong_day_future_timestamp_and_duplicate_conflict(self):
        yesterday = str(int((AS_OF - timedelta(days=1)).timestamp() * 1000))
        parsed, summary = parse_daily_bills([bill(ts=yesterday)], AS_OF)
        self.assertEqual(parsed, [])
        self.assertEqual(summary["bills"], 0)
        with self.assertRaisesRegex(AccountLedgerError, "timestamp"):
            parse_daily_bills([bill(ts=str(int((AS_OF + timedelta(seconds=1)).timestamp() * 1000)))], AS_OF)
        with self.assertRaisesRegex(AccountLedgerError, "conflict"):
            parse_daily_bills([bill(), bill(pnl="100")], AS_OF)
        with self.assertRaisesRegex(AccountLedgerError, "timezone"):
            parse_daily_bills([], AS_OF.replace(tzinfo=None))

    def test_malformed_or_unsupported_bills_fail_closed(self):
        for change in [
            {"pnl": "NaN"}, {"fee": None}, {"billId": ""}, {"ccy": ""},
            {"type": "999"}, {"instType": "SPOT"}, {"ts": "2.5"},
            {"type": "8", "subType": "unknown"},
            {"type": "8", "subType": "173", "balChg": "1", "fee": "0"},
        ]:
            with self.subTest(change=change), self.assertRaises(AccountLedgerError):
                parse_daily_bills([bill(**change)], AS_OF)
        for rows in [None, {}, [None]]:
            with self.subTest(rows=rows), self.assertRaises(AccountLedgerError):
                parse_daily_bills(rows, AS_OF)


class BillPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))

    def test_atomic_idempotent_snapshot_and_account_isolation(self):
        parsed, summary = parse_daily_bills([bill(), bill("2")], AS_OF)
        self.store.save_bill_snapshot("demo-a", parsed, summary)
        self.store.save_bill_snapshot("demo-a", parsed, summary)
        other, other_summary = parse_daily_bills([bill(pnl="100")], AS_OF)
        self.store.save_bill_snapshot("live-a", other, other_summary)
        self.assertEqual(len(self.store.bill_snapshot("demo-a")["data"]), 2)
        self.assertEqual(self.store.bill_snapshot("live-a")["data"][0]["realized_pnl"], "100")
        self.assertTrue(self.store.bill_snapshot("demo-a", 1)["has_more"])
        self.assertEqual(self.store.bill_snapshot("missing")["data"], [])

    def test_late_snapshot_cannot_replace_newer_accounting(self):
        parsed, summary = parse_daily_bills([bill(pnl="-50")], AS_OF)
        self.store.save_bill_snapshot("scope", parsed, summary)
        old, old_summary = parse_daily_bills([bill(pnl="100")], AS_OF - timedelta(minutes=1))
        self.store.save_bill_snapshot("scope", old, old_summary)
        result = self.store.bill_snapshot("scope")
        self.assertEqual(result["summary"]["captured_at"], AS_OF.isoformat())
        self.assertEqual(result["data"][0]["realized_pnl"], "-50")

    def test_snapshot_freshness_and_string_precision(self):
        now = datetime.now(timezone.utc)
        rows, summary = parse_daily_bills([
            bill(ts=str(int(now.timestamp() * 1000)), pnl="0.00000000000000001", fee="0"),
        ], now)
        self.store.save_bill_snapshot("scope", rows, summary)
        result = self.store.bill_snapshot("scope")
        self.assertTrue(result["fresh"])
        self.assertEqual(Decimal(result["data"][0]["realized_pnl"]), Decimal("0.00000000000000001"))
        with patch("app.state_store.datetime") as clock:
            clock.now.return_value = now + timedelta(seconds=46)
            clock.fromisoformat = datetime.fromisoformat
            self.assertFalse(self.store.bill_snapshot("scope")["fresh"])

    def test_legacy_performance_never_adds_different_currencies(self):
        base = {
            "trade_id": "1", "inst_id": "BTC-USDT-SWAP", "side": "sell", "pos_side": "net",
            "fill_price": 50000, "fill_size": 1, "fee": -1, "fee_ccy": "USDT",
            "realized_pnl": 10, "filled_at": AS_OF.isoformat(),
        }
        self.store.save_fill(base)
        self.assertEqual(self.store.performance_report()["currency"], "USDT")
        self.store.save_fill({**base, "trade_id": "2", "inst_id": "BTC-USD-SWAP", "fee_ccy": "BTC", "fee": -0.00001, "realized_pnl": -0.001})
        report = self.store.performance_report()
        self.assertEqual(report["valuation_status"], "mixed_currency")
        self.assertIsNone(report["net_pnl"])
        self.assertEqual(report["equity_curve"], [])
        self.assertIsNone(self.store.pnl_summary()["net_pnl"])

    def test_missing_fee_currency_does_not_produce_a_false_profit(self):
        self.store.save_fill({
            "trade_id": "1", "inst_id": "BTC-USDT-SWAP", "side": "sell", "pos_side": "net",
            "fill_price": 50000, "fill_size": 1, "fee": -1,
            "realized_pnl": 10, "filled_at": AS_OF.isoformat(),
        })
        self.assertEqual(self.store.performance_report()["valuation_status"], "unresolved_currency")
        self.assertIsNone(self.store.pnl_summary()["fees"])

    def test_performance_requires_finite_starting_equity(self):
        for value in [float("inf"), float("nan"), 0, -1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.performance_report(initial_equity=value)


class BillsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))

    async def test_signed_bills_request_paginates_over_a_fixed_utc_window(self):
        requests = []
        def handle(request):
            requests.append(request)
            start = 100 if request.url.params.get("after") else 0
            rows = [bill(str(index)) for index in range(start, 100 if start == 0 else 101)]
            return httpx.Response(200, json={"code": "0", "data": rows})
        with patch.dict(os.environ, {"OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass"}):
            client = OkxAccountClient(httpx.MockTransport(handle))
        rows = await client.bills_today(as_of=AS_OF)
        self.assertEqual(len(rows), 101)
        first, second = requests
        self.assertEqual(first.url.path, "/api/v5/account/bills")
        self.assertEqual(first.url.params["instType"], "SWAP")
        self.assertEqual(first.url.params["begin"], str(int(AS_OF.replace(hour=0).timestamp() * 1000)))
        self.assertEqual(first.url.params["end"], second.url.params["end"])
        self.assertEqual(second.url.params["after"], "99")
        self.assertEqual(first.headers["x-simulated-trading"], "1")
        self.assertEqual(first.headers["ok-access-sign"], client.signature(
            first.headers["ok-access-timestamp"], "GET", first.url.raw_path.decode(), secret_key="secret",
        ))

    async def test_incomplete_bill_pagination_never_returns_partial_success(self):
        client = OkxAccountClient()
        client._get = AsyncMock(side_effect=[[bill(str(index)) for index in range(100)], OkxAccountError("unavailable")])
        with self.assertRaises(OkxAccountError):
            await client.bills_today(as_of=AS_OF)
        client._get = AsyncMock(return_value=[bill("same") for _ in range(100)])
        with self.assertRaisesRegex(OkxAccountError, "repeated"):
            await client.bills_today(as_of=AS_OF)

    async def test_invalid_transport_payload_and_secret_url_are_not_exposed(self):
        for payload in [[], {"code": "0", "data": None}, {"code": "0", "data": [None]}]:
            with patch.dict(os.environ, {"OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass"}):
                client = OkxAccountClient(httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))
            with self.assertRaises(OkxAccountError):
                await client.balance()
        def fail(request):
            raise httpx.ConnectError("socks5://user:private-password@localhost:1080", request=request)
        client.transport = httpx.MockTransport(fail)
        with self.assertRaises(OkxAccountError) as caught:
            await client.balance()
        self.assertNotIn("private-password", str(caught.exception))

    async def test_rest_sync_persists_bills_and_keeps_prior_snapshot_on_failure(self):
        now = datetime.now(timezone.utc)
        account = SimpleNamespace(
            configured=True, account_scope="scope",
            positions=AsyncMock(return_value=[]), pending_orders=AsyncMock(return_value=[]),
            fills_history=AsyncMock(return_value=[]),
            bills_today=AsyncMock(return_value=[bill(ts=str(int(now.timestamp() * 1000)))]),
        )
        sync = AccountSynchronizer(self.store, account, SimpleNamespace(snapshot=lambda: {}))
        result = await sync.sync_rest()
        self.assertEqual(result["bills"], 1)
        before = self.store.bill_snapshot("scope")
        account.bills_today.side_effect = OkxAccountError("incomplete pages")
        result = await sync.sync_rest()
        self.assertIn("bills", result["errors"])
        self.assertEqual(self.store.bill_snapshot("scope")["summary"], before["summary"])

    async def test_ledger_route_requires_auth_and_uses_active_account_scope(self):
        rows, summary = parse_daily_bills([bill()], AS_OF)
        self.store.save_bill_snapshot("scope", rows, summary)
        account = SimpleNamespace(configured=True, account_scope="scope")
        with patch.object(api_main, "state_store", self.store), patch.object(api_main, "account_client", account), patch.dict(os.environ, {"ADMIN_API_TOKEN": "test-ledger-token"}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="http://test") as client:
                self.assertEqual((await client.get("/api/v1/account/bills")).status_code, 401)
                result = await client.get("/api/v1/account/bills", headers={"X-Admin-Token": "test-ledger-token"})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json()["data"][0]["net_pnl"], "-11")
                account.account_scope = "other"
                self.assertEqual((await client.get("/api/v1/account/bills", headers={"X-Admin-Token": "test-ledger-token"})).json()["data"], [])

    async def test_account_scope_changes_between_demo_and_live_without_exposing_key(self):
        client = OkxAccountClient()
        client.api_key = "private-test-key"
        original = client.account_scope
        self.assertNotIn(client.api_key, original)
        client.demo = not client.demo
        self.assertNotEqual(original, client.account_scope)
