import base64
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

from app.okx_account_stream import OkxAccountStream


class OkxAccountStreamTests(unittest.TestCase):
    def test_signature_matches_private_websocket_login_formula(self) -> None:
        timestamp = "1700000000"
        secret = "test-secret"
        expected = base64.b64encode(
            hmac.new(
                secret.encode(),
                f"{timestamp}GET/users/self/verify".encode(),
                hashlib.sha256,
            ).digest()
        ).decode()

        self.assertEqual(
            OkxAccountStream.signature(timestamp, secret),
            expected,
        )

    def test_login_and_subscriptions_are_read_only_channels(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
            },
            clear=False,
        ):
            stream = OkxAccountStream()

        login = stream.login_message("1700000000")
        self.assertEqual(login["op"], "login")
        self.assertEqual(login["args"][0]["apiKey"], "key")
        channels = {
            item["channel"] for item in stream.subscription_message()["args"]
        }
        self.assertEqual(channels, {"account", "positions", "orders"})

    def test_consume_updates_account_positions_and_orders(self) -> None:
        stream = OkxAccountStream()
        stream.consume(json.dumps({"event": "login", "code": "0"}))
        stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "account"},
                    "data": [{"ccy": "USDT", "cashBal": "1000"}],
                }
            )
        )
        stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "positions"},
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "mgnMode": "isolated",
                            "pos": "1",
                        }
                    ],
                }
            )
        )
        stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "orders"},
                    "data": [{"ordId": "123", "state": "live"}],
                }
            )
        )

        snapshot = stream.snapshot()
        self.assertTrue(snapshot["authenticated"])
        self.assertEqual(snapshot["balance"][0]["ccy"], "USDT")
        self.assertEqual(snapshot["positions"][0]["instId"], "BTC-USDT-SWAP")
        self.assertEqual(snapshot["orders"][0]["ordId"], "123")

    def test_incremental_events_preserve_other_balances_and_positions(self) -> None:
        stream = OkxAccountStream()
        stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "account"},
                    "data": [
                        {"ccy": "USDT", "cashBal": "1000"},
                        {"ccy": "BTC", "cashBal": "0.1"},
                    ],
                }
            )
        )
        stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "positions"},
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "mgnMode": "isolated",
                            "pos": "1",
                        },
                        {
                            "instId": "ETH-USDT-SWAP",
                            "posSide": "long",
                            "mgnMode": "isolated",
                            "pos": "2",
                        },
                    ],
                }
            )
        )
        stream.consume(
            json.dumps(
                {
                    "arg": {
                        "channel": "positions",
                        "instId": "BTC-USDT-SWAP",
                    },
                    "data": [
                        {
                            "instId": "BTC-USDT-SWAP",
                            "posSide": "long",
                            "mgnMode": "isolated",
                            "pos": "0",
                        }
                    ],
                }
            )
        )

        snapshot = stream.snapshot()
        self.assertEqual(
            {item["ccy"] for item in snapshot["balance"]},
            {"USDT", "BTC"},
        )
        self.assertEqual(len(snapshot["positions"]), 2)
        btc = next(
            item
            for item in snapshot["positions"]
            if item["instId"] == "BTC-USDT-SWAP"
        )
        self.assertEqual(btc["pos"], "0")

    def test_unconfigured_stream_does_not_start(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "",
                "OKX_SECRET_KEY": "",
                "OKX_PASSPHRASE": "",
            },
            clear=False,
        ):
            stream = OkxAccountStream()

        self.assertFalse(stream.configured)

    def test_demo_stream_defaults_to_demo_private_endpoint(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_DEMO": "true",
                "OKX_WS_PRIVATE_URL": "",
            },
            clear=False,
        ):
            stream = OkxAccountStream()

        self.assertEqual(stream.url, "wss://wspap.okx.com:8443/ws/v5/private")


if __name__ == "__main__":
    unittest.main()
