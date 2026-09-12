import base64
import hashlib
import hmac
import os
import unittest
from unittest.mock import patch

from app.okx_trade import OkxTradeClient, OkxTradeError, OrderRequest


class OrderRequestTests(unittest.TestCase):
    def test_market_order_maps_to_okx_payload(self) -> None:
        order = OrderRequest(
            inst_id="BTC-USDT-SWAP",
            side="buy",
            sz=1,
            reduce_only=True,
            cl_ord_id="test-order-1",
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
                "clOrdId": "test-order-1",
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

    def test_non_swap_instrument_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            OrderRequest(inst_id="BTC-USDT", side="buy", sz=1)


class OkxTradeClientTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

