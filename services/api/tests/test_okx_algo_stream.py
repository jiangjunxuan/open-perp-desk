import base64
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.account_sync import AccountSynchronizer
from app.okx_algo_stream import OkxAlgoOrderStream
from app.state_store import StateStore


class OkxAlgoOrderStreamTests(unittest.TestCase):
    def test_signature_matches_private_login_formula(self) -> None:
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
            OkxAlgoOrderStream.signature(timestamp, secret),
            expected,
        )

    def test_business_stream_subscribes_only_to_algo_orders(self) -> None:
        with patch.dict(
            os.environ,
            {
                "OKX_API_KEY": "key",
                "OKX_SECRET_KEY": "secret",
                "OKX_PASSPHRASE": "pass",
                "OKX_DEMO": "true",
                "OKX_WS_BUSINESS_URL": "",
            },
            clear=False,
        ):
            stream = OkxAlgoOrderStream()

        self.assertEqual(stream.url, "wss://wspap.okx.com:8443/ws/v5/business")
        self.assertEqual(
            stream.subscription_message(),
            {
                "op": "subscribe",
                "args": [{"channel": "orders-algo", "instType": "SWAP"}],
            },
        )

    def test_consume_keeps_algo_order_state(self) -> None:
        stream = OkxAlgoOrderStream()
        stream.consume(json.dumps({"event": "login", "code": "0"}))
        stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "orders-algo"},
                    "data": [
                        {
                            "algoId": "algo-1",
                            "instId": "BTC-USDT-SWAP",
                            "state": "live",
                            "slTriggerPx": "68000",
                            "tpTriggerPx": "74000",
                        }
                    ],
                }
            )
        )
        snapshot = stream.snapshot()
        self.assertEqual(snapshot["orders"][0]["algoId"], "algo-1")
        self.assertEqual(snapshot["orders"][0]["state"], "live")


class AlgoOrderSyncTests(unittest.TestCase):
    def test_stream_sync_persists_native_protection_order(self) -> None:
        class PrivateStream:
            def snapshot(self):
                return {
                    "configured": True,
                    "connected": True,
                    "authenticated": True,
                    "positions": [],
                    "orders": [],
                }

        class AlgoStream:
            def snapshot(self):
                return {
                    "connected": True,
                    "authenticated": True,
                    "orders": [
                        {
                            "algoId": "algo-1",
                            "algoClOrdId": "protect-1",
                            "instId": "BTC-USDT-SWAP",
                            "state": "live",
                            "side": "sell",
                            "posSide": "net",
                            "ordType": "conditional",
                            "tdMode": "isolated",
                            "sz": "1",
                            "slTriggerPx": "68000",
                            "tpTriggerPx": "74000",
                        }
                    ],
                }

        class AccountClient:
            configured = False

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(str(Path(directory) / "state.sqlite3"))
            synchronizer = AccountSynchronizer(
                store,
                AccountClient(),
                PrivateStream(),
                AlgoStream(),
            )
            result = synchronizer.sync_stream()
            order = store.get_order("protect-1")

        self.assertEqual(result["orders"], 1)
        self.assertEqual(order["exchange_order_id"], "algo-1")
        self.assertEqual(order["stop_loss"], 68000)
        self.assertEqual(order["take_profit"], 74000)
        self.assertEqual(order["source"], "okx-algo-stream")


if __name__ == "__main__":
    unittest.main()
