import base64
import hashlib
import hmac
import os
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

from app import main as api_main
from app.main import require_admin_token
from app.okx_account import OkxAccountClient, OkxAccountError
from app.okx_account_stream import OkxAccountStream
from app.okx_market import OkxMarketClient
from app.okx_market_stream import OkxMarketStream
from app.okx_trade import OkxTradeClient
from app.pushplus import PushPlusClient, PushPlusError


class OkxAccountTests(unittest.TestCase):
    def test_signature_matches_hmac_sha256_base64(self) -> None:
        timestamp = "2026-01-01T00:00:00.000Z"
        method = "GET"
        request_path = "/api/v5/account/positions?instType=SWAP"
        secret = "test-secret"
        expected = base64.b64encode(
            hmac.new(
                secret.encode(),
                f"{timestamp}{method}{request_path}".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        self.assertEqual(
            OkxAccountClient.signature(
                timestamp,
                method,
                request_path,
                secret_key=secret,
            ),
            expected,
        )

    def test_unconfigured_account_never_attempts_request(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "",
                "OKX_SECRET_KEY": "",
                "OKX_PASSPHRASE": "",
            },
            clear=False,
        ):
            client = OkxAccountClient()

        self.assertFalse(client.configured)
        with self.assertRaises(OkxAccountError):
            __import__("asyncio").run(client.balance())

    def test_pending_orders_uses_swap_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
            },
            clear=False,
        ):
            client = OkxAccountClient()
        with patch.object(
            client,
            "_get",
            new=__import__("unittest").mock.AsyncMock(return_value=[]),
        ) as mocked:
            __import__("asyncio").run(client.pending_orders())
        mocked.assert_awaited_once_with(
            "/api/v5/trade/orders-pending",
            {"instType": "SWAP", "limit": "100"},
        )

    def test_fills_history_uses_swap_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
            },
            clear=False,
        ):
            client = OkxAccountClient()
        with patch.object(
            client,
            "_get",
            new=__import__("unittest").mock.AsyncMock(return_value=[]),
        ) as mocked:
            __import__("asyncio").run(client.fills_history(limit=25))
        mocked.assert_awaited_once_with(
            "/api/v5/trade/fills-history",
            {"instType": "SWAP", "limit": "25"},
        )

    def test_orders_history_uses_archive_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
            },
            clear=False,
        ):
            client = OkxAccountClient()
        with patch.object(
            client,
            "_get",
            new=__import__("unittest").mock.AsyncMock(return_value=[]),
        ) as mocked:
            __import__("asyncio").run(
                client.orders_history("BTC-USDT-SWAP", limit=25),
            )
        mocked.assert_awaited_once_with(
            "/api/v5/trade/orders-history-archive",
            {
                "instType": "SWAP",
                "limit": "25",
                "instId": "BTC-USDT-SWAP",
            },
        )

    def test_pending_algo_orders_uses_swap_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
            },
            clear=False,
        ):
            client = OkxAccountClient()
        with patch.object(
            client,
            "_get",
            new=__import__("unittest").mock.AsyncMock(return_value=[]),
        ) as mocked:
            __import__("asyncio").run(client.pending_algo_orders(limit=25))
        mocked.assert_awaited_once_with(
            "/api/v5/trade/orders-algo-pending",
            {"instType": "SWAP", "ordType": "conditional,oco", "limit": "25"},
        )

    def test_algo_order_history_uses_swap_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
            },
            clear=False,
        ):
            client = OkxAccountClient()
        with patch.object(
            client,
            "_get",
            new=__import__("unittest").mock.AsyncMock(return_value=[]),
        ) as mocked:
            __import__("asyncio").run(client.algo_orders_history(limit=25))
        self.assertEqual(mocked.await_count, 3)
        self.assertEqual(
            [call.args for call in mocked.await_args_list],
            [("/api/v5/trade/orders-algo-history", {
                "instType": "SWAP", "ordType": "conditional,oco", "state": state, "limit": "25",
            }) for state in ("effective", "canceled", "order_failed")],
        )


class PushPlusTests(unittest.TestCase):
    def test_unconfigured_pushplus_fails_closed(self) -> None:
        with patch.dict(os.environ, {"PUSHPLUS_TOKEN": ""}, clear=False):
            client = PushPlusClient()

        self.assertFalse(client.configured)
        with self.assertRaises(PushPlusError):
            __import__("asyncio").run(client.send("test", "test"))

    def test_configured_pushplus_sends_json_payload(self) -> None:
        captured: dict[str, object] = {}

        async def handler(request: httpx.Request) -> httpx.Response:
            captured["payload"] = __import__("json").loads(request.content)
            return httpx.Response(200, json={"code": "200", "msg": "success"})

        with patch.dict(
            os.environ,
            {
                "PUSHPLUS_TOKEN": "test-token",
                "PUSHPLUS_BASE_URL": "https://pushplus.test/send",
            },
            clear=False,
        ):
            client = PushPlusClient(transport=httpx.MockTransport(handler))
            result = __import__("asyncio").run(
                client.send("title", "content", topic="ops"),
            )

        self.assertEqual(result["code"], "200")
        self.assertEqual(captured["payload"]["token"], "test-token")
        self.assertEqual(captured["payload"]["topic"], "ops")

    def test_pushplus_non_success_code_fails_closed(self) -> None:
        async def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"code": "999", "msg": "bad token"})

        with patch.dict(os.environ, {"PUSHPLUS_TOKEN": "test-token"}, clear=False):
            client = PushPlusClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(PushPlusError):
            __import__("asyncio").run(client.send("title", "content"))


class AdminTokenTests(unittest.TestCase):
    def test_private_api_stays_locked_without_admin_token(self) -> None:
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": ""}, clear=False):
            with self.assertRaises(HTTPException) as context:
                require_admin_token(None)

        self.assertEqual(context.exception.status_code, 503)

    def test_admin_token_uses_constant_time_comparison_path(self) -> None:
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "local-test-token"}, clear=False):
            require_admin_token("local-test-token")
            with self.assertRaises(HTTPException) as context:
                require_admin_token("wrong-token")

        self.assertEqual(context.exception.status_code, 401)


class HealthEndpointTests(unittest.TestCase):
    class ReadyMarket:
        connected = True
        fresh = True
        last_message_at = "2026-09-12T00:00:00+00:00"
        last_error = None

    class StaleMarket:
        connected = False
        fresh = False
        last_message_at = "2026-09-12T00:00:00+00:00"
        last_error = "ConnectionClosed"

    def test_readiness_is_ready_when_market_and_store_are_available(self) -> None:
        with patch.object(api_main, "market_stream", self.ReadyMarket()):
            payload = api_main.readiness()

        self.assertTrue(payload["ready"])
        self.assertEqual(payload["status"], "ready")
        self.assertTrue(payload["checks"]["market_stream"]["fresh"])

    def test_readiness_returns_503_when_market_stream_is_stale(self) -> None:
        with patch.object(api_main, "market_stream", self.StaleMarket()):
            with self.assertRaises(HTTPException) as context:
                api_main.readiness()

        self.assertEqual(context.exception.status_code, 503)
        self.assertFalse(context.exception.detail["ready"])
        self.assertEqual(
            context.exception.detail["checks"]["market_stream"]["last_error"],
            "ConnectionClosed",
        )

    def test_metrics_do_not_include_proxy_urls_or_credentials(self) -> None:
        class ProxiedMarket(self.ReadyMarket):
            proxy_url = "socks5h://user:secret@proxy.example:1080"

        with patch.object(api_main, "market_stream", ProxiedMarket()):
            payload = api_main.health_metrics()

        serialized = __import__("json").dumps(payload)
        self.assertNotIn("proxy.example", serialized)
        self.assertNotIn("secret", serialized)

    def test_system_status_exposes_algo_configuration_and_store_state(self) -> None:
        class ConfiguredAlgoStream:
            configured = True
            connected = False
            authenticated = False
            last_message_at = None
            last_error = "ConnectionClosed"

        with patch.object(api_main, "algo_stream", ConfiguredAlgoStream()):
            payload = api_main.system_status()

        self.assertTrue(payload["state_store_ready"])
        self.assertTrue(payload["algo_stream"]["configured"])
        self.assertFalse(payload["algo_stream"]["connected"])

    def test_system_status_exposes_private_account_stream_configuration(self) -> None:
        class ConfiguredAccountStream:
            configured = True
            connected = True
            authenticated = True
            last_message_at = "2026-09-12T00:00:00+00:00"
            last_error = None

        with patch.object(api_main, "account_stream", ConfiguredAccountStream()):
            payload = api_main.system_status()

        self.assertTrue(payload["account_stream"]["configured"])
        self.assertTrue(payload["account_stream"]["authenticated"])


class ProxyConfigurationTests(unittest.TestCase):
    def test_proxy_is_loaded_by_rest_and_websocket_clients(self) -> None:
        proxy = "socks5h://user:secret@proxy.example:1080"
        with patch.dict(
            os.environ,
            {
                "OKX_PROXY_URL": proxy,
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
            },
            clear=False,
        ):
            clients = (
                OkxMarketClient(),
                OkxAccountClient(),
                OkxTradeClient(),
                OkxMarketStream(["BTC-USDT-SWAP"]),
                OkxAccountStream(),
            )

        self.assertTrue(all(client.proxy_url == proxy for client in clients))


if __name__ == "__main__":
    unittest.main()
