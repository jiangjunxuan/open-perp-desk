import asyncio
import os
import traceback
import unittest
from unittest.mock import patch

import httpx

from app import main as api_main
from app.okx_market import OkxMarketClient, OkxMarketError


PROXY = "invalid://fixture-user:fixture-password@proxy.invalid:1080"


class MarketTransportSafetyTests(unittest.IsolatedAsyncioTestCase):
    def market(self):
        with patch.dict(os.environ, {"OKX_PROXY_URL": PROXY}, clear=True):
            return OkxMarketClient()

    async def test_invalid_proxy_does_not_expose_configuration(self):
        with self.assertRaises(OkxMarketError) as raised:
            await self.market().ticker("BTC-USDT-SWAP")
        self.assertEqual(str(raised.exception), "OKX market request failed: ValueError")

    async def test_transport_errors_are_redacted_without_retry_or_fallback(self):
        errors = (
            httpx.ProxyError(PROXY),
            httpx.ConnectError(PROXY),
            httpx.ReadTimeout(PROXY),
            httpx.HTTPStatusError(
                PROXY,
                request=httpx.Request("GET", "https://okx.invalid"),
                response=httpx.Response(503),
            ),
            ValueError(PROXY),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                market = self.market()
                with patch("app.okx_market.httpx.AsyncClient", side_effect=error) as client:
                    with self.assertRaises(OkxMarketError) as raised:
                        await market.ticker("BTC-USDT-SWAP")
                client.assert_called_once()
                self.assertEqual(client.call_args.kwargs["proxy"], PROXY)
                self.assertEqual(
                    str(raised.exception),
                    f"OKX market request failed: {type(error).__name__}",
                )
                formatted = "".join(traceback.format_exception(raised.exception))
                for private in ("fixture-user", "fixture-password", "proxy.invalid"):
                    self.assertNotIn(private, formatted)

    async def test_public_market_routes_do_not_return_proxy_details(self):
        transport = httpx.ASGITransport(app=api_main.app)
        with patch.object(api_main, "market_client", self.market()):
            async with httpx.AsyncClient(transport=transport, base_url="http://local.invalid") as client:
                for route in ("ticker", "candles", "instruments", "overview"):
                    with self.subTest(route=route):
                        response = await client.get(f"/api/v1/market/{route}")
                        self.assertEqual(response.status_code, 502)
                        self.assertEqual(
                            response.json(),
                            {"detail": "OKX market request failed: ValueError"},
                        )

    async def test_cancellation_is_not_converted_to_a_transport_error(self):
        with patch("app.okx_market.httpx.AsyncClient", side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await self.market().ticker("BTC-USDT-SWAP")
