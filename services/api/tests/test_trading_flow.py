import asyncio
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, suppress

import httpx

from tests.fixtures.api_process import ApiProcess, eventually
from tests.fixtures.exchange_server import ExchangeServer, SYMBOL


@unittest.skipIf(os.name == "nt", "The inherited Uvicorn socket requires a POSIX host")
class TradingFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.exchange = ExchangeServer()
        self.addAsyncCleanup(self.exchange.close)
        await self.exchange.start()
        self.api = ApiProcess(self.directory.name, self.exchange)
        self.addAsyncCleanup(self.api.stop)
        await self.api.start()

    async def signal_payload(self):
        response = await self.api.request("POST", "/analysis", {"inst_id": SYMBOL, "bar": "15m", "limit": 100})
        signal = response["data"]["signal"]
        self.assertEqual(signal["action"], "open_long")
        return {
            "signal": signal, "account_equity": 1000000000,
            "daily_pnl_pct": 999, "current_notional": 0, "size": 1, "dry_run": False,
        }

    async def synchronize(self):
        result = await self.api.request("POST", "/account/sync")
        self.assertNotIn("errors", result["rest"], result)
        return result

    async def test_analysis_order_native_protection_and_restart(self):
        response = await self.api.client.get("/api/v1/orders", headers={"X-Admin-Token": ""})
        self.assertEqual(response.status_code, 401)
        await self.api.request("POST", "/execution/orders", {"inst_id": SYMBOL, "side": "buy", "sz": 1}, status=410)
        await self.api.request("POST", "/safety/live/unlock", {"phrase": "not-a-live-approval"}, status=403)
        self.assertEqual(len(self.exchange.subscriptions), 4)
        payload = await self.signal_payload()
        preview = await self.api.request("POST", "/execution/signals", {**payload, "dry_run": True})
        self.assertTrue(preview["accepted"], preview)
        self.assertEqual(preview["preflight"]["basis"], "exchange")
        self.assertEqual(preview["preflight"]["account_equity"], 1000)
        self.assertEqual(preview["preflight"]["daily_pnl_pct"], 0)
        self.assertEqual(self.exchange.order_submissions, [])
        submitted = await self.api.request("POST", "/execution/signals", payload)
        self.assertTrue(submitted["accepted"], submitted)
        order_id = submitted["order"]["exchange_order_id"]
        client_id = submitted["order"]["client_order_id"]
        replay = await self.api.request("POST", "/execution/signals", payload)
        self.assertTrue(replay["accepted"] and replay["idempotent"], replay)
        self.assertEqual(len(self.exchange.order_submissions), 1)
        attached = self.exchange.order_submissions[0]["attachAlgoOrds"][0]
        self.assertEqual(attached["slTriggerPxType"], "mark")
        self.assertEqual(attached["tpTriggerPxType"], "mark")
        self.assertEqual(attached["slOrdPx"], "-1")
        self.assertEqual(attached["tpOrdPx"], "-1")

        # No private push can hide a first-REST-sync ordering defect.
        self.exchange.fill(order_id)
        await self.synchronize()
        positions = (await self.api.request("GET", "/positions"))["data"]
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["stop_loss"], float(attached["slTriggerPx"]))
        self.assertEqual(positions[0]["take_profit"], float(attached["tpTriggerPx"]))
        orders = (await self.api.request("GET", "/orders"))["data"]
        self.assertEqual(next(order for order in orders if order["client_order_id"] == client_id)["status"], "filled")
        self.assertEqual(len([order for order in orders if order["order_kind"] == "algo"]), 1)
        await self.synchronize()
        self.assertEqual(len((await self.api.request("GET", "/fills"))["data"]), 1)
        self.assertEqual(sum("收到成交回报" in row["title"] for row in self.exchange.notifications), 1)

        self.exchange.private_paused = False
        self.exchange.trigger_protection(next(iter(self.exchange.algos)))
        async def private_fill_arrived():
            snapshot = await self.api.request("GET", "/account/stream")
            return len(snapshot.get("fills", [])) == 2
        await eventually(private_fill_arrived)
        await self.synchronize()
        self.assertEqual((await self.api.request("GET", "/positions"))["data"], [])
        self.assertEqual(len((await self.api.request("GET", "/fills"))["data"]), 2)
        self.assertEqual(len(self.exchange.order_submissions), 1)
        protection = next(row for row in (await self.api.request("GET", "/orders"))["data"] if row["order_kind"] == "algo")
        self.assertEqual(protection["status"], "effective")

        await self.api.request("POST", "/safety/emergency-stop", {"reason": "local restart drill"})
        await self.api.stop()
        await self.api.start()
        self.assertTrue((await self.api.request("GET", "/safety/status"))["emergency_stopped"])
        self.assertFalse((await self.api.request("GET", "/execution/status"))["live_execution_allowed"])
        self.assertEqual(len((await self.api.request("GET", "/fills"))["data"]), 2)
        self.assertEqual(sum("收到成交回报" in row["title"] for row in self.exchange.notifications), 2)
        rejected = await self.api.request("POST", "/execution/signals", payload)
        self.assertFalse(rejected["accepted"])
        self.assertIn("emergency_stop_active", rejected["reasons"])
        self.assertEqual(len(self.exchange.order_submissions), 1)
        self.assertEqual(self.exchange.errors, [])

    async def test_submission_crash_exact_lookup_and_no_resend(self):
        payload = await self.signal_payload()
        self.exchange.hold_order_response = True
        self.exchange.hide_order_history = True
        pending = asyncio.create_task(self.api.request("POST", "/execution/signals", payload))
        try:
            self.assertTrue(await asyncio.to_thread(self.exchange.blocked_order_received.wait, 10))
            with closing(sqlite3.connect(self.api.database)) as connection:
                status = connection.execute("SELECT status FROM orders").fetchone()[0]
            self.assertEqual(status, "submitting")
            await self.api.stop(crash=True)
            with suppress(httpx.HTTPError):
                await pending
        finally:
            self.exchange.release_order_response.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        order_id = next(iter(self.exchange.orders))
        self.exchange.fill(order_id)
        await self.api.start()
        orders = (await self.api.request("GET", "/orders"))["data"]
        recovered = next(row for row in orders if row["order_kind"] == "standard")
        self.assertEqual(recovered["status"], "filled")
        self.assertEqual(recovered["exchange_order_id"], order_id)
        positions = (await self.api.request("GET", "/positions"))["data"]
        self.assertEqual(positions[0]["stop_loss"], float(self.exchange.order_submissions[0]["attachAlgoOrds"][0]["slTriggerPx"]))
        self.assertTrue(any(row["path"] == "/api/v5/trade/order" and "clOrdId" in row["query"] for row in self.exchange.gets))
        replay = await self.api.request("POST", "/execution/signals", payload)
        self.assertTrue(replay["accepted"] and replay["idempotent"], replay)
        self.assertEqual(len(self.exchange.order_submissions), 1)
        self.assertEqual(self.exchange.errors, [])

    async def test_preflight_outage_and_regular_and_algo_cancellation(self):
        payload = await self.signal_payload()
        self.exchange.fail_paths.add("/api/v5/account/balance")
        rejected = await self.api.request("POST", "/execution/signals", payload)
        self.assertFalse(rejected["accepted"], rejected)
        self.assertIn("exchange_preflight_data_unavailable", rejected["reasons"])
        self.exchange.fail_paths.clear()
        oversized = await self.api.request("POST", "/execution/signals", {**payload, "size": 10000})
        self.assertFalse(oversized["accepted"], oversized)
        self.assertEqual(self.exchange.order_submissions, [])
        self.assertFalse(any(item["path"] == "/api/v5/account/set-leverage" for item in self.exchange.posts))

        submitted = await self.api.request("POST", "/execution/signals", payload)
        self.assertTrue(submitted["accepted"], submitted)
        client_id = submitted["order"]["client_order_id"]
        canceled = await self.api.request("POST", f"/execution/orders/{client_id}/cancel")
        self.assertTrue(canceled["accepted"])
        self.assertEqual(canceled["order"]["status"], "canceling")
        await self.synchronize()
        orders = (await self.api.request("GET", "/orders"))["data"]
        self.assertEqual(next(row for row in orders if row["client_order_id"] == client_id)["status"], "canceled")
        replay = await self.api.request("POST", f"/execution/orders/{client_id}/cancel")
        self.assertFalse(replay["accepted"])
        self.assertTrue(replay["idempotent"])

        submitted = await self.api.request("POST", "/execution/signals", await self.signal_payload())
        self.assertTrue(submitted["accepted"], submitted)
        self.exchange.fill(submitted["order"]["exchange_order_id"])
        await self.synchronize()
        orders = (await self.api.request("GET", "/orders"))["data"]
        protection = next(row for row in orders if row["order_kind"] == "algo")
        canceled = await self.api.request("POST", f"/execution/orders/{protection['client_order_id']}/cancel")
        self.assertTrue(canceled["accepted"])
        self.assertEqual(canceled["order"]["status"], "canceling")
        await self.synchronize()
        orders = (await self.api.request("GET", "/orders"))["data"]
        self.assertEqual(next(row for row in orders if row["order_kind"] == "algo")["status"], "canceled")
        self.assertEqual(len((await self.api.request("GET", "/positions"))["data"]), 1)
        self.assertEqual(len(self.exchange.order_submissions), 2)
        paths = [item["path"] for item in self.exchange.posts]
        self.assertEqual(paths.count("/api/v5/trade/cancel-order"), 1)
        self.assertEqual(paths.count("/api/v5/trade/cancel-algos"), 1)
        self.assertEqual(self.exchange.errors, [])

    async def test_worker_dry_run_execution_and_protective_close(self):
        await self.api.request("PUT", "/strategies/structured-technical", {
            "name": "Local acceptance strategy", "enabled": True, "config": {},
        })

        async def run_cycle(*, dry_run, expected_count):
            await self.api.request("POST", "/worker/control", {"enabled": True, "dry_run": dry_run})
            async def completed():
                return (await self.api.request("GET", "/worker/status"))["run_count"] >= expected_count
            await eventually(completed)
            await self.api.request("POST", "/worker/control", {"enabled": False})

        await run_cycle(dry_run=True, expected_count=1)
        self.assertEqual(self.exchange.order_submissions, [])
        self.assertEqual((await self.api.request("GET", "/orders"))["data"][0]["status"], "preview")
        self.exchange.autofill = True
        await run_cycle(dry_run=False, expected_count=2)
        self.assertEqual(len(self.exchange.order_submissions), 1)
        self.assertEqual(float(self.exchange.order_submissions[0]["sz"]), 92)
        await self.synchronize()
        await run_cycle(dry_run=False, expected_count=3)
        self.assertEqual(len(self.exchange.order_submissions), 1)
        activities = (await self.api.request("GET", "/activity"))["data"]
        cycle = next(item for item in activities if item["event_type"] == "worker_run")
        self.assertIn("skip_open_existing_exposure", cycle["payload_json"])

        attached = self.exchange.order_submissions[0]["attachAlgoOrds"][0]
        self.exchange.mark_price = self.exchange.price
        self.exchange.price = str(float(attached["slTriggerPx"]) - .1)
        await run_cycle(dry_run=False, expected_count=4)
        self.assertEqual(len(self.exchange.order_submissions), 1, "last price must not trigger a mark-price stop")
        self.exchange.fail_paths.add("/api/v5/public/mark-price")
        await run_cycle(dry_run=False, expected_count=5)
        self.exchange.fail_paths.clear()
        self.assertEqual(len(self.exchange.order_submissions), 1, "unavailable mark price must not fall back to last")
        activities = (await self.api.request("GET", "/activity"))["data"]
        self.assertTrue(any(item["event_type"] == "worker_protection_price_unavailable" for item in activities))
        self.exchange.mark_price = self.exchange.price
        self.exchange.mark_timestamp = "1700000000000"
        await run_cycle(dry_run=False, expected_count=6)
        self.assertEqual(len(self.exchange.order_submissions), 1, "stale mark price must not trigger a stop")
        self.exchange.mark_timestamp = None
        self.exchange.price = self.exchange.candles[0][4]
        await run_cycle(dry_run=False, expected_count=7)
        self.assertEqual(len(self.exchange.order_submissions), 2)
        close = self.exchange.order_submissions[1]
        self.assertTrue(close["reduceOnly"])
        self.assertEqual(close["side"], "sell")
        self.assertNotIn("attachAlgoOrds", close)
        await self.synchronize()
        self.assertEqual((await self.api.request("GET", "/positions"))["data"], [])
        self.assertTrue(any("保护性平仓触发" in row["title"] for row in self.exchange.notifications))
        self.assertTrue(any(
            f"标记价格 {self.exchange.mark_price}" in row["content"]
            for row in self.exchange.notifications if "保护性平仓触发" in row["title"]
        ))
        self.assertFalse((await self.api.request("GET", "/worker/status"))["running"])
        self.assertEqual(self.exchange.errors, [])
