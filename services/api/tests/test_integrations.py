import base64
import hashlib
import hmac
import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.main import require_admin_token
from app.okx_account import OkxAccountClient, OkxAccountError
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


class PushPlusTests(unittest.TestCase):
    def test_unconfigured_pushplus_fails_closed(self) -> None:
        with patch.dict(os.environ, {"PUSHPLUS_TOKEN": ""}, clear=False):
            client = PushPlusClient()

        self.assertFalse(client.configured)
        with self.assertRaises(PushPlusError):
            __import__("asyncio").run(client.send("test", "test"))


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


if __name__ == "__main__":
    unittest.main()
