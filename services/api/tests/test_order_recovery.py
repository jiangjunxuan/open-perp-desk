import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app import main as api_main
from app.account_reconciler import AccountReconciler
from app.account_sync import AccountSynchronizer
from app.execution_engine import ExecutionEngine
from app.okx_account import OkxAccountClient, OkxAccountError
from app.okx_trade import OkxTradeClient
from app.order_preflight import PreparedExecution
from app.risk_engine import RiskEngine
from app.state_store import OrderSnapshotConflict, StateStore
from app.trading_signal import TradeSignal


ENV = {
    "OKX_API_KEY": "recovery-fixture-key",
    "OKX_SECRET_KEY": "recovery-fixture-secret",
    "OKX_PASSPHRASE": "recovery-fixture-pass",
    "OKX_PROXY_URL": "",
    "OKX_DEMO": "true",
    "TRADING_MODE": "demo",
    "EXECUTION_ENABLED": "true",
}


def exchange_order(**overrides):
    now = str(int(datetime.now(timezone.utc).timestamp() * 1000))
    return {
        "ordId": "exchange1", "clOrdId": "local1", "instId": "BTC-USDT-SWAP",
        "side": "buy", "posSide": "net", "ordType": "market", "tdMode": "isolated",
        "sz": "1", "state": "filled", "cTime": now, "uTime": now, **overrides,
    }


def snapshot_order(**overrides):
    return {
        "client_order_id": "local1", "exchange_order_id": "exchange1",
        "inst_id": "BTC-USDT-SWAP", "side": "buy", "pos_side": "net",
        "ord_type": "market", "td_mode": "isolated", "size": 1,
        "status": "live", "source": "okx-rest-orders", "account_scope": "account-a",
        "exchange_updated_ms": 1000, "raw": {"state": "live", "uTime": "1000"},
        **overrides,
    }


class AccountContractTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        with patch.dict(os.environ, ENV, clear=False):
            return OkxAccountClient(transport=httpx.MockTransport(handler))

    def assert_signed(self, request):
        expected = OkxAccountClient.signature(
            request.headers["OK-ACCESS-TIMESTAMP"], "GET",
            request.url.raw_path.decode(), secret_key=ENV["OKX_SECRET_KEY"],
        )
        self.assertEqual(request.headers["OK-ACCESS-SIGN"], expected)
        self.assertEqual(request.headers["x-simulated-trading"], "1")

    async def test_pending_protection_paginates_with_required_type_and_signed_query(self):
        calls = []

        async def handler(request):
            self.assert_signed(request)
            query = request.url.params
            self.assertEqual(query["ordType"], "conditional,oco")
            self.assertEqual(query["instType"], "SWAP")
            self.assertEqual(query["instId"], "BTC-USDT-SWAP")
            self.assertEqual(query["limit"], "2")
            calls.append(dict(query))
            rows = [{"algoId": "3"}, {"algoId": "2"}] if "after" not in query else [{"algoId": "1"}]
            return httpx.Response(200, json={"code": "0", "data": rows})

        rows = await self.client(handler).pending_algo_orders("BTC-USDT-SWAP", limit=2)
        self.assertEqual(len(rows), 3)
        self.assertEqual(calls[1]["after"], "2")

    async def test_history_queries_all_three_states_and_paginates_each(self):
        calls = []

        async def handler(request):
            self.assert_signed(request)
            query = request.url.params
            state = query["state"]
            self.assertIn(state, {"effective", "canceled", "order_failed"})
            self.assertEqual(query["ordType"], "conditional,oco")
            calls.append((state, query.get("after")))
            ids = [f"{state}-3", f"{state}-2"] if "after" not in query else [f"{state}-1"]
            return httpx.Response(200, json={"code": "0", "data": [{"algoId": value} for value in ids]})

        rows = await self.client(handler).algo_orders_history(limit=2)
        self.assertEqual(len(rows), 9)
        self.assertEqual(calls, [(state, after) for state in ("effective", "canceled", "order_failed")
                                for after in (None, f"{state}-2")])

    async def test_repeated_algo_page_fails_closed(self):
        async def handler(_request):
            return httpx.Response(200, json={"code": "0", "data": [{"algoId": "same"}]})

        with self.assertRaisesRegex(OkxAccountError, "repeated"):
            await self.client(handler).pending_algo_orders(limit=1)

    async def test_preflight_algo_inventory_queries_every_supported_category(self):
        types = []
        async def handler(request):
            self.assert_signed(request)
            types.append(request.url.params["ordType"])
            return httpx.Response(200, json={"code": "0", "data": []})
        self.assertEqual(await self.client(handler).active_algo_orders(), [])
        self.assertEqual(types, ["conditional,oco", "trigger", "move_order_stop", "iceberg", "twap", "chase", "smart_iceberg"])

    async def test_exact_lookup_uses_client_id_until_exchange_id_is_known(self):
        calls = []

        async def handler(request):
            self.assert_signed(request)
            self.assertEqual(request.url.path, "/api/v5/trade/order")
            calls.append(dict(request.url.params))
            return httpx.Response(200, json={"code": "0", "data": [exchange_order()]})

        client = self.client(handler)
        await client.order_details("BTC-USDT-SWAP", client_order_id="local1")
        await client.order_details("BTC-USDT-SWAP", ord_id="exchange1", client_order_id="local1")
        self.assertEqual(calls, [
            {"instId": "BTC-USDT-SWAP", "clOrdId": "local1"},
            {"instId": "BTC-USDT-SWAP", "ordId": "exchange1"},
        ])

    async def test_exact_lookup_rejects_empty_multiple_and_mismatched_results(self):
        for rows in ([], [exchange_order(), exchange_order()], [exchange_order(clOrdId="other")],
                     [exchange_order(instId="ETH-USDT-SWAP")], [exchange_order(ordId="other")]):
            async def handler(_request):
                return httpx.Response(200, json={"code": "0", "data": rows})
            with self.subTest(rows=rows), self.assertRaises(OkxAccountError):
                await self.client(handler).order_details("BTC-USDT-SWAP", ord_id="exchange1", client_order_id="local1")

    async def test_not_found_preserves_exchange_code(self):
        async def handler(_request):
            return httpx.Response(200, json={"code": "51603", "msg": "Order does not exist", "data": []})
        with self.assertRaises(OkxAccountError) as caught:
            await self.client(handler).order_details("BTC-USDT-SWAP", client_order_id="local1")
        self.assertEqual(caught.exception.code, "51603")

    async def test_algo_cancel_uses_array_payload_and_correct_signature(self):
        async def handler(request):
            self.assertEqual(request.url.path, "/api/v5/trade/cancel-algos")
            self.assertEqual(json.loads(request.content), [{"instId": "BTC-USDT-SWAP", "algoId": "algo1"}])
            expected = OkxTradeClient.signature(
                request.headers["OK-ACCESS-TIMESTAMP"], "POST", request.url.path,
                request.content.decode(), ENV["OKX_SECRET_KEY"],
            )
            self.assertEqual(request.headers["OK-ACCESS-SIGN"], expected)
            return httpx.Response(200, json={"code": "0", "data": [{"algoId": "algo1", "sCode": "0"}]})
        with patch.dict(os.environ, ENV, clear=False):
            client = OkxTradeClient(transport=httpx.MockTransport(handler))
        await client.cancel_algo_order("BTC-USDT-SWAP", "algo1")


class SnapshotOrderingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "state.sqlite3")
        self.store = StateStore(self.path)

    def test_old_rest_and_future_live_cannot_reopen_filled_order(self):
        self.store.save_exchange_order(snapshot_order(status="filled", exchange_updated_ms=2000))
        for timestamp in (1000, 2000, 3000, None):
            saved, applied = self.store.save_exchange_order(snapshot_order(exchange_updated_ms=timestamp))
            self.assertFalse(applied)
            self.assertEqual(saved["status"], "filled")
        reopened = StateStore(self.path)
        self.assertEqual(reopened.get_order("local1")["exchange_updated_ms"], 2000)
        self.assertFalse(reopened.has_active_order("BTC-USDT-SWAP"))

    def test_partial_fill_does_not_regress_on_later_live_snapshot(self):
        self.store.save_exchange_order(snapshot_order(status="partially_filled", exchange_updated_ms=2000))
        saved, applied = self.store.save_exchange_order(snapshot_order(exchange_updated_ms=3000))
        self.assertFalse(applied)
        self.assertEqual(saved["status"], "partially_filled")

    def test_equal_millisecond_terminal_can_advance_live_once(self):
        self.store.save_exchange_order(snapshot_order())
        order = snapshot_order(status="filled", raw={"state": "filled", "uTime": "1000"})
        _, applied = self.store.save_exchange_order(order)
        generation = self.store.execution_snapshot()[0]
        _, replayed = self.store.save_exchange_order(order)
        self.assertTrue(applied)
        self.assertFalse(replayed)
        self.assertEqual(generation, self.store.execution_snapshot()[0])

    def test_identity_and_account_mismatches_never_overwrite(self):
        self.store.save_exchange_order(snapshot_order())
        for field, value in (("exchange_order_id", "other"), ("inst_id", "ETH-USDT-SWAP"),
                             ("account_scope", "account-b"), ("order_kind", "algo")):
            with self.subTest(field=field), self.assertRaises(OrderSnapshotConflict):
                self.store.save_exchange_order(snapshot_order(**{field: value}))
        self.assertEqual(self.store.get_order("local1")["account_scope"], "account-a")

    def test_late_local_cancel_response_does_not_overwrite_filled(self):
        self.store.save_exchange_order(snapshot_order(status="filled"))
        saved = self.store.save_order(snapshot_order(status="canceling"))
        self.assertEqual(saved["status"], "filled")

    def test_migration_recovers_legacy_algo_kind_and_exchange_timestamp(self):
        path = str(Path(self.directory.name) / "legacy.sqlite3")
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript("""
                CREATE TABLE orders (
                    client_order_id TEXT PRIMARY KEY, exchange_order_id TEXT, status TEXT NOT NULL,
                    inst_id TEXT NOT NULL, side TEXT NOT NULL, pos_side TEXT NOT NULL,
                    ord_type TEXT NOT NULL, td_mode TEXT NOT NULL, size REAL NOT NULL, price REAL,
                    reduce_only INTEGER NOT NULL DEFAULT 0, stop_loss REAL, take_profit REAL,
                    source TEXT NOT NULL, raw_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                INSERT INTO orders (client_order_id, status, inst_id, side, pos_side, ord_type,
                    td_mode, size, source, raw_json, created_at, updated_at)
                VALUES ('algo1', 'live', 'BTC-USDT-SWAP', 'sell', 'net', 'conditional', 'cross',
                    1, 'okx-algo-stream', '{"uTime":"1767225600000"}', 'unknown', 'unknown');
            """)
        order = StateStore(path).get_order("algo1")
        self.assertEqual(order["order_kind"], "algo")
        self.assertEqual(order["exchange_updated_ms"], 1767225600000)
        self.assertIsNone(order["account_scope"])


class SnapshotAccount:
    configured = True
    account_scope = "account-a"

    def __init__(self):
        self.lookups = []
        self.result = None
        self.pending = []
        self.history = []
        self.algos = []
        self.position_rows = []

    async def positions(self):
        return self.position_rows

    async def pending_orders(self):
        return self.pending

    async def orders_history(self, **_kwargs):
        return self.history

    async def fills_history(self):
        return []

    async def pending_algo_orders(self, **_kwargs):
        return self.algos

    async def algo_orders_history(self, **_kwargs):
        return []

    async def order_details(self, inst_id, **kwargs):
        self.lookups.append((inst_id, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "state.sqlite3")
        self.store = StateStore(self.path)
        self.account = SnapshotAccount()
        self.stream = SimpleNamespace(snapshot=lambda: {"positions": [], "orders": []})
        self.sync = AccountSynchronizer(self.store, self.account, self.stream)

    def intent(self, **overrides):
        record = snapshot_order(
            exchange_order_id=None, source="structured-technical", status="submission_unknown",
            exchange_updated_ms=None, risk_notional=100, created_at=datetime.now(timezone.utc).isoformat(),
            raw={"preflight": {"account_scope": "account-a"}}, **overrides,
        )
        self.store.claim_order(record)
        return record

    async def test_exact_recovery_survives_real_sqlite_reopen(self):
        self.intent()
        self.account.result = exchange_order()
        reopened = StateStore(self.path)
        sync = AccountSynchronizer(reopened, self.account, self.stream)
        result = await sync.sync_rest()
        self.assertEqual(result["recovery"], {"checked": 1, "recovered": 1, "unresolved": 0})
        self.assertNotIn("errors", result)
        self.assertEqual(reopened.get_order("local1")["status"], "filled")
        self.assertEqual(reopened.get_order("local1")["risk_notional"], 100)
        self.assertFalse(reopened.has_active_order("BTC-USDT-SWAP"))
        self.assertEqual(self.account.lookups[0][1], {"ord_id": None, "client_order_id": "local1"})

    async def test_not_found_and_timeout_keep_reservation_after_restart(self):
        self.intent()
        for error in (OkxAccountError("not found", code="51603"), TimeoutError("timeout")):
            self.account.result = error
            result = await self.sync.sync_rest()
            self.assertIn("order_recovery", result["errors"])
            reopened = StateStore(self.path)
            self.assertEqual(reopened.get_order("local1")["status"], "submission_unknown")
            self.assertTrue(reopened.has_active_order("BTC-USDT-SWAP"))

    async def test_mismatched_and_reused_client_ids_are_not_recovered(self):
        self.intent()
        for fields in ({"side": "sell"}, {"sz": "2"}, {"clOrdId": "other"},
                       {"instId": "ETH-USDT-SWAP"}, {"posSide": "long"},
                       {"tdMode": "cross"}, {"cTime": "1767225600000"}, {"uTime": ""},
                       {"state": "unknown"}):
            self.account.result = exchange_order(**fields)
            result = await self.sync.sync_rest()
            self.assertEqual(result["recovery"]["unresolved"], 1, fields)
            self.assertEqual(len(self.store.list_orders()), 1)
            self.assertEqual(self.store.get_order("local1")["status"], "submission_unknown")

    async def test_scope_change_never_queries_other_account(self):
        self.intent()
        self.account.account_scope = "account-b"
        result = await self.sync.sync_rest()
        self.assertEqual(result["recovery"]["unresolved"], 1)
        self.assertEqual(self.account.lookups, [])

    async def test_preparing_intent_is_not_released_when_absent(self):
        self.store.claim_order(snapshot_order(
            source="structured-technical", status="preparing", exchange_order_id=None,
            exchange_updated_ms=None, risk_notional=100,
        ))
        self.account.result = OkxAccountError("not found", code="51603")
        await self.sync.sync_rest()
        self.assertEqual(self.store.get_order("local1")["status"], "preparing")

    async def test_pending_and_newer_history_are_both_applied(self):
        now = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        self.account.pending = [exchange_order(state="live", uTime=str(int(now) - 1))]
        self.account.history = [exchange_order(uTime=now)]
        result = await self.sync.sync_rest()
        self.assertEqual(result["orders"], 1)
        self.assertEqual(self.store.get_order("local1")["status"], "filled")
        self.assertEqual(self.account.lookups, [])

    async def test_rest_attaches_protection_on_first_filled_snapshot(self):
        self.intent(stop_loss=95, take_profit=110)
        self.account.history = [exchange_order()]
        self.account.position_rows = [{
            "instId": "BTC-USDT-SWAP", "posSide": "net", "pos": "1",
            "mgnMode": "isolated", "avgPx": "100", "markPx": "100",
        }]
        result = await self.sync.sync_rest()
        self.assertNotIn("errors", result)
        position = self.store.list_positions()[0]
        self.assertEqual((position["stop_loss"], position["take_profit"]), (95, 110))

    async def test_stream_attaches_protection_on_first_partial_fill(self):
        self.intent(stop_loss=95, take_profit=110)
        snapshot = {
            "configured": True, "connected": True, "authenticated": True,
            "orders": [exchange_order(state="partially_filled")],
            "positions": [{
                "instId": "BTC-USDT-SWAP", "posSide": "net", "pos": ".5",
                "mgnMode": "isolated", "avgPx": "100", "markPx": "100",
            }],
        }
        self.stream.snapshot = lambda: snapshot
        self.assertEqual(self.sync.sync_stream()["orders"], 1)
        position = self.store.list_positions()[0]
        self.assertEqual(position["size"], .5)
        self.assertEqual((position["stop_loss"], position["take_profit"]), (95, 110))

    async def test_protection_side_and_pending_size_use_order_fields(self):
        self.account.algos = [{
            "algoId": "algo1", "algoClOrdId": "protect1", "instId": "BTC-USDT-SWAP",
            "state": "live", "side": "sell", "actualSide": "tp", "posSide": "long",
            "sz": "2", "actualSz": "0", "reduceOnly": "false",
            "ordType": "conditional", "cTime": "1000", "uTime": "1000",
        }]
        await self.sync.sync_rest()
        order = self.store.get_order("protect1")
        self.assertEqual(order["side"], "sell")
        self.assertEqual(order["size"], 2)
        self.assertEqual(order["order_kind"], "algo")
        self.assertTrue(order["reduce_only"])
        self.assertFalse(self.store.has_active_order("BTC-USDT-SWAP"))

    async def test_non_reducing_algo_is_not_exempted_from_exposure(self):
        self.account.algos = [{
            "algoId": "algo1", "instId": "BTC-USDT-SWAP", "state": "live",
            "side": "buy", "posSide": "net", "sz": "1", "reduceOnly": "false",
        }]
        await self.sync.sync_rest()
        self.assertTrue(self.store.has_active_order("BTC-USDT-SWAP"))
        self.assertEqual(self.account.lookups, [])

    async def test_partial_reconcile_does_not_report_success(self):
        self.intent()
        self.account.result = OkxAccountError("not found", code="51603")
        reconciler = AccountReconciler(self.sync, self.store)
        await reconciler.run_once()
        self.assertIsNone(reconciler.last_sync_at)
        self.assertIn("order_recovery", reconciler.last_error)
        self.assertIsNotNone(reconciler.last_attempt_at)
        self.account.result = exchange_order()
        await reconciler.run_once()
        success_time = reconciler.last_sync_at
        self.assertIsNotNone(success_time)
        self.assertIsNone(reconciler.last_error)

        async def failed_positions():
            raise OkxAccountError("unavailable")
        self.account.positions = failed_positions
        await reconciler.run_once()
        self.assertEqual(reconciler.last_sync_at, success_time)
        self.assertIn("positions", reconciler.last_error)

    async def test_lost_submit_response_recovers_without_second_send(self):
        sent = []
        store = self.store

        class Preflight:
            configured = True

            async def prepare(self, signal, size, side_override):
                now = datetime.now(timezone.utc)
                return PreparedExecution(
                    signal, 1000, 0, 0, 100, "buy", "net", "isolated",
                    store.execution_snapshot()[0], now.isoformat(), now.timestamp(),
                    account_scope="account-a",
                )

        class Trade:
            enabled = True

            async def set_leverage(self, *_args):
                return {}

            async def place_order(self, order):
                sent.append(order)
                raise TimeoutError("accepted at exchange; response lost")

        signal = TradeSignal(
            inst_id="BTC-USDT-SWAP", action="open_long", confidence=.9,
            leverage=2, position_pct=5, entry_price=100, stop_loss=95, take_profit=110,
        )
        engine = ExecutionEngine(self.store, RiskEngine(), Trade(), SimpleNamespace(configured=False), preflight=Preflight())
        with self.assertRaises(TimeoutError):
            await engine.submit_signal(signal, account_equity=1000, daily_pnl_pct=0)
        local_id = self.store.list_orders()[0]["client_order_id"]
        reopened = StateStore(self.path)
        self.account.result = exchange_order(clOrdId=local_id)
        await AccountSynchronizer(reopened, self.account, self.stream).sync_rest()
        restarted = ExecutionEngine(reopened, RiskEngine(), Trade(), SimpleNamespace(configured=False), preflight=Preflight())
        replay = await restarted.submit_signal(signal, account_equity=1000, daily_pnl_pct=0)
        self.assertTrue(replay["accepted"])
        self.assertTrue(replay["idempotent"])
        self.assertEqual(len(sent), 1)

    async def test_algo_cancel_routes_to_algorithm_endpoint_and_waits_for_confirmation(self):
        self.store.save_exchange_order(snapshot_order(order_kind="algo", source="okx-algo-rest"))
        calls = []

        class Trade:
            async def cancel_algo_order(self, inst_id, algo_id):
                calls.append((inst_id, algo_id))
                return {"data": [{"sCode": "0", "algoId": algo_id}]}

        with patch.object(api_main, "state_store", self.store), patch.object(api_main, "trade_client", Trade()):
            result = await api_main.cancel_stored_order("local1")
        self.assertTrue(result["accepted"])
        self.assertEqual(result["order"]["status"], "canceling")
        self.assertEqual(calls, [("BTC-USDT-SWAP", "exchange1")])

    async def test_process_exit_mid_submission_recovers_without_resending(self):
        signal = TradeSignal(
            inst_id="BTC-USDT-SWAP", action="open_long", confidence=.9,
            leverage=2, position_pct=5, entry_price=100, stop_loss=95, take_profit=110,
        )
        script = """
import asyncio, os, sqlite3, sys
from datetime import datetime, timezone
from types import SimpleNamespace
from app.execution_engine import ExecutionEngine
from app.order_preflight import PreparedExecution
from app.risk_engine import RiskEngine
from app.state_store import StateStore
from app.trading_signal import TradeSignal

store = StateStore(sys.argv[1])
signal = TradeSignal.model_validate_json(sys.argv[2])
class Preflight:
    configured = True
    async def prepare(self, signal, size, side_override):
        now = datetime.now(timezone.utc)
        return PreparedExecution(signal, 1000, 0, 0, 100, "buy", "net", "isolated",
            store.execution_snapshot()[0], now.isoformat(), now.timestamp(),
            account_scope="account-a")
class Trade:
    enabled = True
    async def set_leverage(self, *_args):
        return {}
    async def place_order(self, order):
        with sqlite3.connect(store.path) as connection:
            connection.execute("CREATE TABLE crash_drill_sends (client_id TEXT)")
            connection.execute("INSERT INTO crash_drill_sends VALUES (?)", (order.cl_ord_id,))
        os._exit(23)
engine = ExecutionEngine(store, RiskEngine(), Trade(), SimpleNamespace(configured=False), preflight=Preflight())
asyncio.run(engine.submit_signal(signal, account_equity=1000, daily_pnl_pct=0))
"""
        process = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-c", script, self.path, signal.model_dump_json()],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(process.returncode, 23, process.stderr)
        restarted_store = StateStore(self.path)
        order = restarted_store.list_orders()[0]
        self.assertEqual(order["status"], "submitting")
        self.assertTrue(restarted_store.has_active_order(signal.inst_id))
        self.account.result = exchange_order(clOrdId=order["client_order_id"])
        sync = AccountSynchronizer(restarted_store, self.account, self.stream)
        result = await sync.sync_rest()
        self.assertNotIn("errors", result)
        # No trade implementation is available in the restarted executor.
        restarted = ExecutionEngine(restarted_store, RiskEngine(), object(), SimpleNamespace(configured=False))
        replay = await restarted.submit_signal(signal, account_equity=1000, daily_pnl_pct=0)
        self.assertTrue(replay["idempotent"])
        self.assertTrue(replay["accepted"])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM crash_drill_sends").fetchone()[0], 1)
