import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone

from app.state_store import StateStore
from tests.fixtures.api_process import ApiProcess, eventually
from tests.fixtures.exchange_server import ExchangeServer


class TradingViewHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.exchange = ExchangeServer()
        self.addAsyncCleanup(self.exchange.close)
        await self.exchange.start()
        self.api = ApiProcess(self.directory.name, self.exchange)
        self.api.environment.update({
            "TRADINGVIEW_ENABLED": "true", "TRADINGVIEW_WEBHOOK_SECRET": "loopback-test",
            "TRADINGVIEW_EXECUTION_ENABLED": "true", "TRADINGVIEW_DRY_RUN": "false",
        })
        self.addAsyncCleanup(self.api.stop)
        await self.api.start()

    async def payload(self):
        analysis = await self.api.request("POST", "/analysis", {})
        return {
            **analysis["data"]["signal"], "alert_id": "loopback-alert",
            "secret": "loopback-test", "timestamp": datetime.now(timezone.utc).isoformat(), "size": 1,
        }

    async def post(self, payload):
        return await self.api.request("POST", "/integrations/tradingview/webhook", payload, status=202)

    async def test_ack_precedes_slow_exchange_and_completion_is_pushed(self):
        payload = await self.payload()
        self.exchange.hold_order_response = True
        async with self.api.client.stream("GET", "/api/v1/account/events") as response:
            receipt = await asyncio.wait_for(self.post(payload), 1)
            self.assertEqual(receipt["status"], "queued")
            self.assertIsNone(receipt["execution_accepted"])
            received = await asyncio.to_thread(self.exchange.blocked_order_received.wait, 5)
            self.assertTrue(received)
            repeated = await asyncio.wait_for(self.post(payload), 1)
            self.assertTrue(repeated["idempotent"])
            self.assertEqual(len(self.exchange.order_submissions), 1)
            self.exchange.release_order_response.set()
            seen = None
            async with asyncio.timeout(6):
                event = ""
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event = line[6:].strip()
                    elif line.startswith("data:") and event == "tradingview_alerts":
                        rows = json.loads(line[5:])["data"]
                        if rows and rows[0]["status"] == "submitted":
                            seen = rows[0]
                            break
            self.assertIsNotNone(seen)
            self.assertTrue(seen["execution_accepted"])
            self.assertTrue(seen["client_order_id"].startswith("opd"))
            self.assertNotIn("loopback-test", json.dumps(seen))
        self.assertEqual(len(self.exchange.order_submissions), 1)
        self.assertEqual(self.exchange.errors, [])

    async def test_crash_after_exchange_receipt_does_not_resubmit(self):
        payload = await self.payload()
        self.exchange.hold_order_response = True
        await self.post(payload)
        self.assertTrue(await asyncio.to_thread(self.exchange.blocked_order_received.wait, 5))
        await self.api.stop(crash=True)
        self.exchange.release_order_response.set()
        store = StateStore(str(self.api.database))
        with store._connection() as connection:
            connection.execute("UPDATE tradingview_alerts SET deadline_ms = 0")
        await self.api.start()

        async def interrupted():
            rows = await self.api.request("GET", "/integrations/tradingview/alerts")
            return rows["data"][0]["status"] == "interrupted"
        await eventually(interrupted)
        duplicate = await self.post(payload)
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(duplicate["status"], "interrupted")
        self.assertTrue(duplicate["client_order_id"].startswith("opd"))
        self.assertEqual(len(self.exchange.order_submissions), 1)
        orders = await self.api.request("GET", "/orders")
        self.assertTrue(any(row["exchange_order_id"] for row in orders["data"]))
        self.assertEqual(self.exchange.errors, [])
