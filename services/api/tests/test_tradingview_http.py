import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.execution_engine import ExecutionEngine
from app.state_store import StateStore
from app.trading_signal import TradeSignal
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

    async def test_confirmed_exchange_rejection_is_not_reported_as_unknown(self):
        payload = await self.payload()
        original = self.exchange._post

        def reject_order(path, body):
            if path == "/api/v5/trade/order":
                return [{"sCode": "51008", "sMsg": "fixture rejection must not be published"}]
            return original(path, body)

        with patch.object(self.exchange, "_post", side_effect=reject_order):
            receipt = await self.post(payload)

            async def finished():
                rows = await self.api.request("GET", "/integrations/tradingview/alerts")
                return rows["data"][0]["status"] not in {"queued", "processing"}

            await eventually(finished)
            result = await self.post(payload)
        self.assertEqual(result["status"], "rejected")
        self.assertFalse(result["execution_accepted"])
        self.assertEqual(result["client_order_id"], receipt["client_order_id"])
        self.assertEqual(result["reasons"], ["previous_order_not_accepted"])
        self.assertNotIn("fixture rejection", json.dumps(result))
        self.assertTrue(result["idempotent"])
        self.assertEqual(len(self.exchange.order_submissions), 1)
        orders = await self.api.request("GET", "/orders")
        self.assertEqual(orders["data"][0]["status"], "rejected")
        self.assertEqual(self.exchange.errors, [])

    async def test_existing_submission_unknown_remains_unconfirmed_without_resend(self):
        payload = await self.payload()
        client_order_id = ExecutionEngine.client_order_id(
            TradeSignal.model_validate(payload), f"tradingview:{payload['alert_id']}",
        )
        store = StateStore(str(self.api.database))
        store.save_order({
            "client_order_id": client_order_id, "status": "submission_unknown",
            "inst_id": "BTC-USDT-SWAP", "side": "buy", "size": payload["size"],
            "pos_side": "net", "td_mode": "isolated", "ord_type": "market",
        })
        await self.post(payload)

        async def finished():
            rows = await self.api.request("GET", "/integrations/tradingview/alerts")
            return rows["data"][0]["status"] not in {"queued", "processing"}

        await eventually(finished)
        receipt = await self.post(payload)
        self.assertEqual(receipt["status"], "unconfirmed")
        self.assertIsNone(receipt["execution_accepted"])
        self.assertTrue(receipt["idempotent"])
        self.assertEqual(receipt["client_order_id"], client_order_id)
        self.assertEqual(receipt["reasons"], ["order_submission_unconfirmed"])
        self.assertEqual(self.exchange.order_submissions, [])
        self.assertEqual(store.get_order(client_order_id)["status"], "submission_unknown")
        self.assertEqual(self.exchange.errors, [])
