import asyncio
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.okx_market_stream import OkxMarketStream
from app.realtime import control_events, private_events
from app.state_store import StateStore
from tests.fixtures.api_process import ApiProcess
from tests.fixtures.exchange_server import ExchangeServer, SYMBOL


async def next_event(lines, name, predicate=lambda payload: True):
    async with asyncio.timeout(4):
        event = ""
        async for line in lines:
            if line.startswith("event:"):
                event = line[6:].strip()
            if line.startswith("data:") and event == name:
                payload = json.loads(line[5:])
                if predicate(payload):
                    return payload
    raise AssertionError(f"Stream ended without {name}")


async def close_events(events):
    await events.aclose()


class RealtimeUnitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))

    async def test_revision_changes_only_after_successful_write(self):
        before = self.store.revision
        self.store.list_orders()
        self.assertEqual(self.store.revision, before)
        self.store.set_control_flag("test", True)
        self.assertGreater(self.store.revision, before)
        before = self.store.revision
        with self.assertRaises(RuntimeError):
            with self.store._connection() as connection:
                connection.execute("DELETE FROM control_flags")
                raise RuntimeError("rollback")
        self.assertEqual(self.store.revision, before)
        self.assertTrue(self.store.get_control_flag("test"))

    async def test_selected_candles_and_symbol_freshness_are_independent(self):
        stream = OkxMarketStream([SYMBOL, "ETH-USDT-SWAP"])
        stream.connected = stream.candles_connected = True
        for symbol in stream.symbols:
            stream.consume(json.dumps({"arg": {"channel": "tickers", "instId": symbol}, "data": [{"last": "100"}]}))
        stream._record_epochs[("tickers", SYMBOL)] = time.monotonic() - 60
        stream.consume(json.dumps({"arg": {"channel": "candle15m", "instId": SYMBOL}, "data": [["1", "2", "3", "1", "2", "4"]]}))
        payload = stream.browser_snapshot("15m")
        self.assertFalse(payload["tickers"][SYMBOL]["fresh"])
        self.assertTrue(payload["tickers"]["ETH-USDT-SWAP"]["fresh"])
        self.assertEqual(payload["candles"][SYMBOL]["channel"], "candle15m")
        self.assertEqual(stream.browser_snapshot("1H")["candles"], {})
        self.assertEqual(stream.candles, {})

    async def test_market_events_deliver_new_cache_without_polling_exchange(self):
        stream = OkxMarketStream([SYMBOL])
        events = stream.events("15m")
        self.addAsyncCleanup(close_events, events)
        self.assertIn("retry:", await anext(events))
        self.assertIn("event: market", await anext(events))
        stream.connected = True
        stream.consume(json.dumps({"arg": {"channel": "tickers", "instId": SYMBOL}, "data": [{"last": "777"}]}))
        frame = await asyncio.wait_for(anext(events), 1)
        self.assertIn('"last":"777"', frame)
        stream.connected = False
        self.assertIn('"fresh":false', await asyncio.wait_for(anext(events), 1))

    async def test_control_change_and_heartbeat(self):
        value = {"execution_enabled": False}
        events = control_events(lambda: value, lambda: {"configured": False})
        self.addAsyncCleanup(close_events, events)
        self.assertIn("event: status", await anext(events))
        self.assertIn("event: analysis_status", await anext(events))
        self.assertIn("event: heartbeat", await anext(events))
        value = {"execution_enabled": True}
        self.assertIn('"execution_enabled":true', await asyncio.wait_for(anext(events), 1))

    async def test_control_snapshot_does_not_block_socket_event_loop(self):
        caller = threading.get_ident()
        readers = []

        def status():
            readers.append(threading.get_ident())
            return {}

        events = control_events(status, lambda: {})
        self.addAsyncCleanup(close_events, events)
        await anext(events)
        self.assertEqual(len(readers), 1)
        self.assertNotEqual(readers[0], caller)

    async def test_revocation_interrupts_initial_private_snapshot(self):
        account = SimpleNamespace(
            configured=True, connected=True, authenticated=True,
            balance=[{"totalEq": "1000"}], last_message_at="now",
        )
        allowed = True
        events = private_events(
            self.store, account, SimpleNamespace(configured=True, account_scope="fixture"),
            lambda: allowed,
        )
        self.addAsyncCleanup(close_events, events)
        self.assertIn("event: account", await anext(events))
        allowed = False
        self.assertEqual(await anext(events), "event: locked\ndata: {}\n\n")
        with self.assertRaises(StopAsyncIteration):
            await anext(events)

    async def test_private_stream_revocation_and_committed_state(self):
        account = SimpleNamespace(
            configured=True, connected=True, authenticated=True,
            balance=[{"totalEq": "1000"}], last_message_at="now",
        )
        client = SimpleNamespace(configured=True, account_scope="fixture")
        allowed = True
        events = private_events(self.store, account, client, lambda: allowed)
        self.addAsyncCleanup(close_events, events)
        for _ in range(20):
            if "event: heartbeat" in await anext(events):
                break
        self.store.add_audit("push_fixture", "Immediate committed event")
        async with asyncio.timeout(2):
            while True:
                frame = await anext(events)
                if "event: activity" in frame:
                    self.assertIn("push_fixture", frame)
                    break
        allowed = False
        self.assertIn("event: locked", await anext(events))
        with self.assertRaises(StopAsyncIteration):
            await anext(events)

    async def test_private_stream_pushes_incident_review_version_and_resolution(self):
        account = SimpleNamespace(
            configured=True, connected=True, authenticated=True, balance=[], last_message_at="now",
        )
        record = {
            "account_scope": "fixture", "inst_id": SYMBOL, "opening_order_id": "incident-sse",
            "failure_code": "attached_protection_missing",
        }
        first = self.store.record_protection_incident(record)
        events = private_events(self.store, account, SimpleNamespace(configured=True, account_scope="fixture"), lambda: True)
        self.addAsyncCleanup(close_events, events)

        async def incident_frame():
            async with asyncio.timeout(2):
                while True:
                    frame = await anext(events)
                    if frame.startswith("event: protection_incidents\n"):
                        return json.loads(frame.split("data: ", 1)[1])

        self.assertEqual((await incident_frame())["data"][0]["version"], first["version"])
        updated = self.store.record_protection_incident({**record, "failure_detail": "new evidence"})
        payload = await incident_frame()
        self.assertEqual(payload["data"][0]["version"], updated["version"])
        self.assertEqual(payload["data"][0]["failure_detail"], "new evidence")
        self.store.resolve_protection_incident(
            updated, resolution="external_protection_verified", note="Reviewed external protection",
        )
        self.assertEqual((await incident_frame())["data"], [])


class RealtimeHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.exchange = ExchangeServer()
        self.addAsyncCleanup(self.exchange.close)
        await self.exchange.start()
        self.api = ApiProcess(self.directory.name, self.exchange)
        self.addAsyncCleanup(self.api.stop)
        await self.api.start()

    async def test_public_sse_and_private_auth_boundary(self):
        for route in ("/account/events", "/account/events?token=local-acceptance-admin-token", "/protection/handoffs"):
            response = await self.api.client.get("/api/v1" + route, headers={"X-Admin-Token": ""})
            self.assertEqual(response.status_code, 401)
        response = await self.api.client.get("/api/v1/market/events?bar=invalid")
        self.assertEqual(response.status_code, 422)
        async with self.api.client.stream("GET", "/api/v1/market/events?bar=4H") as response:
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/event-stream", response.headers["content-type"])
            self.assertEqual(response.headers["x-accel-buffering"], "no")
            payload = await next_event(response.aiter_lines(), "market", lambda row: SYMBOL in row["candles"])
            self.assertEqual(payload["candles"][SYMBOL]["channel"], "candle4H")
            self.assertNotIn("balance", payload)
            self.assertTrue(payload["tickers"][SYMBOL]["fresh"])

    async def test_emergency_stop_reaches_another_client_without_status_get(self):
        async with self.api.client.stream("GET", "/api/v1/system/events") as response:
            lines = response.aiter_lines()
            await next_event(lines, "status")
            await self.api.request("POST", "/safety/emergency-stop", {"reason": "fixture push acceptance"})
            payload = await next_event(lines, "status", lambda row: row["safety_control"]["emergency_stopped"])
            self.assertFalse(payload["safety_control"]["execution_allowed"])
            self.assertEqual(self.exchange.order_submissions, [])

    async def test_private_order_fill_and_position_push_without_manual_reconcile(self):
        self.exchange.private_paused = False
        async with self.api.client.stream("GET", "/api/v1/account/events") as response:
            lines = response.aiter_lines()
            await next_event(lines, "heartbeat")
            analysis = await self.api.request("POST", "/analysis", {"inst_id": SYMBOL, "bar": "15m", "limit": 100})
            submitted = await self.api.request("POST", "/execution/signals", {
                "signal": analysis["data"]["signal"], "account_equity": 1000,
                "daily_pnl_pct": 0, "current_notional": 0, "size": 1, "dry_run": False,
            })
            self.assertTrue(submitted["accepted"], submitted)
            order_id = submitted["order"]["exchange_order_id"]
            self.exchange.fill(order_id)
            payload = await next_event(lines, "fills", lambda row: len(row["data"]) == 1)
            self.assertEqual(payload["data"][0]["exchange_order_id"], order_id)
            # The fixture's REST reconcile interval is one hour.
            positions = await self.api.request("GET", "/positions")
            self.assertEqual(len(positions["data"]), 1)
            self.assertGreater(positions["data"][0]["stop_loss"], 0)
            self.assertEqual(len(self.exchange.order_submissions), 1)
