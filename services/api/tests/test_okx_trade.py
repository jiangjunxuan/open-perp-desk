import base64
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

import httpx
from app.live_safety import LiveSafetyGate
from app.okx_trade import OkxTradeClient, OkxTradeError, OrderRequest


class OrderRequestTests(unittest.TestCase):
    def test_market_order_maps_to_okx_payload(self) -> None:
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            sz=1,
            reduce_only=True,
            cl_ord_id="testorder1",
        )
        self.assertEqual(
            order.okx_payload(),
            {
                "instId": "BTC-USDT-SWAP",
                "tdMode": "isolated",
                "side": "buy",
                "posSide": "net",
                "ordType": "market",
                "sz": "1",
                "reduceOnly": True,
                "clOrdId": "testorder1",
            },
        )

    def test_limit_order_requires_price(self) -> None:
        with self.assertRaises(ValueError):
            OrderRequest(
                inst_id="BTC-USDT-SWAP",
                side="sell",
                ord_type="limit",
                sz=1,
            )

    def test_open_order_attaches_native_protection(self) -> None:
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            sz=1,
            stop_loss=68000,
            take_profit=74000,
            cl_ord_id="testorder2",
        )
        attached = order.okx_payload()["attachAlgoOrds"][0]
        self.assertEqual(attached["slTriggerPx"], "68000")
        self.assertEqual(attached["tpTriggerPx"], "74000")
        self.assertEqual(attached["slOrdPx"], "-1")
        self.assertEqual(attached["tpOrdPx"], "-1")

    def test_non_swap_instrument_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OrderRequest(inst_id="BTC-USDT", side="buy", sz=1)


class OkxTradeClientTests(unittest.TestCase):
    def test_demo_order_sends_signed_payload_and_demo_header(self) -> None:
        captured: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"code": "0", "data": [{"ordId": "okx-1"}]})

        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
                "OKX_DEMO": "true",
                "TRADING_MODE": "demo",
                "EXECUTION_ENABLED": "true",
                "OKX_REST_BASE_URL": "https://okx.test",
            },
            clear=False,
        ):
            client = OkxTradeClient(transport=httpx.MockTransport(handler))
            response = __import__("asyncio").run(
                client.place_order(
                    OrderRequest(
                        inst_id="BTC-USDT-SWAP",
                        side="buy",
                        sz=1,
                        stop_loss=68000,
                        take_profit=74000,
                        cl_ord_id="client1",
                    )
                )
            )

        request = captured["request"]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.path, "/api/v5/trade/order")
        self.assertEqual(request.headers["x-simulated-trading"], "1")
        self.assertTrue(request.headers["OK-ACCESS-SIGN"])
        self.assertEqual(response["data"][0]["ordId"], "okx-1")
        self.assertEqual(captured["payload"]["clOrdId"], "client1")
        self.assertEqual(captured["payload"]["attachAlgoOrds"][0]["slTriggerPx"], "68000")

    def test_demo_cancel_sends_exchange_order_id(self) -> None:
        captured: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            captured["payload"] = json.loads(request.content)
            return httpx.Response(200, json={"code": "0", "data": [{"sCode": "0"}]})

        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
                "OKX_DEMO": "true",
                "TRADING_MODE": "demo",
                "EXECUTION_ENABLED": "true",
                "OKX_REST_BASE_URL": "https://okx.test",
            },
            clear=False,
        ):
            client = OkxTradeClient(transport=httpx.MockTransport(handler))
            response = __import__("asyncio").run(
                client.cancel_order("BTC-USDT-SWAP", "okx-1")
            )

        request = captured["request"]
        self.assertEqual(request.url.path, "/api/v5/trade/cancel-order")
        self.assertEqual(captured["payload"], {"instId": "BTC-USDT-SWAP", "ordId": "okx-1"})
        self.assertEqual(response["code"], "0")

    def test_order_row_error_is_not_recorded_as_success(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "code": "0",
                    "data": [
                        {
                            "ordId": "",
                            "sCode": "51008",
                            "sMsg": "Insufficient margin",
                        }
                    ],
                },
            )

        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
                "OKX_DEMO": "true",
                "TRADING_MODE": "demo",
                "EXECUTION_ENABLED": "true",
                "OKX_REST_BASE_URL": "https://okx.test",
            },
            clear=False,
        ):
            client = OkxTradeClient(transport=httpx.MockTransport(handler))
            with self.assertRaises(OkxTradeError) as context:
                __import__("asyncio").run(
                    client.place_order(
                        OrderRequest(
                            inst_id="BTC-USDT-SWAP",
                            side="buy",
                            sz=1,
                        )
                    )
                )

        self.assertIn("Insufficient margin", str(context.exception))

    def test_signature_matches_okx_post_formula(self) -> None:
        timestamp = "2026-01-01T00:00:00.000Z"
        secret = "test-secret"
        request_path = "/api/v5/trade/order"
        body = '{"instId":"BTC-USDT-SWAP"}'
        expected = base64.b64encode(
            hmac.new(
                secret.encode(),
                f"{timestamp}POST{request_path}{body}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()
        self.assertEqual(
            OkxTradeClient.signature(
                timestamp,
                "POST",
                request_path,
                body,
                secret,
            ),
            expected,
        )

    def test_disabled_execution_fails_closed_without_request(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
                "OKX_DEMO": "true",
                "TRADING_MODE": "demo",
                "EXECUTION_ENABLED": "false",
            },
            clear=False,
        ):
            client = OkxTradeClient()

        self.assertFalse(client.enabled)
        with self.assertRaises(OkxTradeError):
            __import__("asyncio").run(
                client.place_order(
                    OrderRequest(
                        inst_id="BTC-USDT-SWAP",
                        side="buy",
                        sz=1,
                    )
                )
            )

    def test_live_mode_is_always_blocked(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
                "OKX_DEMO": "false",
                "TRADING_MODE": "live",
                "EXECUTION_ENABLED": "true",
            },
            clear=False,
        ):
            client = OkxTradeClient()

        self.assertFalse(client.enabled)
        with self.assertRaises(OkxTradeError):
            __import__("asyncio").run(
                client.place_order(
                    OrderRequest(
                        inst_id="BTC-USDT-SWAP",
                        side="buy",
                        sz=1,
                    )
                )
            )

    def test_live_mode_requires_explicit_process_local_unlock(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
                "OKX_DEMO": "false",
                "TRADING_MODE": "live",
                "EXECUTION_ENABLED": "true",
                "LIVE_TRADING_ENABLED": "true",
                "LIVE_UNLOCK_PHRASE": "approve-live-once",
            },
            clear=False,
        ):
            gate = LiveSafetyGate()
            client = OkxTradeClient(gate)
            self.assertFalse(client.enabled)
            self.assertTrue(gate.unlock("approve-live-once"))
            self.assertTrue(client.enabled)
            gate.lock()
            self.assertFalse(client.enabled)


if __name__ == "__main__":
    unittest.main()
