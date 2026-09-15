import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import main as api_main
from app.pushplus import PushPlusClient, PushPlusError
from app.state_store import StateStore


SECRET = "local-notification-test-secret"
MESSAGE_ID = "a" * 32


def client_for(handler, *, token=SECRET):
    with patch.dict(os.environ, {
        "PUSHPLUS_TOKEN": token,
        "PUSHPLUS_BASE_URL": f"https://fixture-user:fixture-password@push.invalid/send?private={SECRET}",
    }, clear=True):
        return PushPlusClient(transport=httpx.MockTransport(handler))


class PushPlusResponseTests(unittest.IsolatedAsyncioTestCase):
    async def test_acceptance_is_not_delivery_and_raw_message_is_discarded(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"code": 200, "msg": SECRET, "data": MESSAGE_ID})
        result = await client_for(handler).send("title", "content")
        self.assertEqual(len(calls), 1)
        self.assertEqual(json.loads(calls[0].content)["token"], SECRET)
        self.assertTrue(result["accepted"])
        self.assertFalse(result["delivery_confirmed"])
        self.assertEqual(result["message_id"], MESSAGE_ID)
        self.assertNotIn(SECRET, str(result))
        self.assertEqual(result["msg"], "request_accepted")

    async def test_legacy_acceptance_without_id_is_explicitly_unconfirmed(self):
        result = await client_for(lambda _: httpx.Response(200, json={"code": "200"})).send("t", "c")
        self.assertTrue(result["accepted"])
        self.assertIsNone(result["message_id"])
        self.assertFalse(result["delivery_confirmed"])

    async def test_http_503_is_unknown_redacted_and_not_blindly_reposted(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503, text=SECRET)
        with self.assertRaises(PushPlusError) as raised:
            await client_for(handler).send("t", "c")
        self.assertEqual(len(calls), 1)
        self.assertTrue(raised.exception.acceptance_unknown)
        for private in (SECRET, "fixture-password", "push.invalid"):
            self.assertNotIn(private, str(raised.exception))

    async def test_timeout_and_connection_errors_do_not_expose_transport_details(self):
        for error in (httpx.ReadTimeout(SECRET), httpx.ConnectError(f"proxy://fixture-user:fixture-password@{SECRET}")):
            with self.subTest(error=type(error).__name__):
                def handler(_):
                    raise error
                with self.assertRaisesRegex(PushPlusError, "^pushplus_acceptance_unknown$"):
                    await client_for(handler).send("t", "c")

    async def test_explicit_rejection_discards_provider_message(self):
        with self.assertRaises(PushPlusError) as raised:
            await client_for(lambda _: httpx.Response(200, json={"code": 401, "msg": SECRET})).send("t", "c")
        self.assertEqual(str(raised.exception), "pushplus_rejected")
        self.assertFalse(raised.exception.acceptance_unknown)

    async def test_malformed_shapes_codes_and_identifiers_are_unknown(self):
        for payload in (None, [], "text", {"code": True}, {"code": "arbitrary"}, {"code": 200.0},
                        {"code": 200, "data": {"token": SECRET}}, {"code": 200, "data": SECRET}):
            with self.subTest(payload=payload), self.assertRaisesRegex(PushPlusError, "^pushplus_acceptance_unknown$"):
                await client_for(lambda _: httpx.Response(200, json=payload)).send("t", "c")

    async def test_reflected_token_cannot_be_returned_as_message_id(self):
        with self.assertRaisesRegex(PushPlusError, "^pushplus_acceptance_unknown$"):
            await client_for(lambda _: httpx.Response(200, json={"code": 200, "data": MESSAGE_ID.upper()}),
                             token=MESSAGE_ID).send("t", "c")

    async def test_invalid_json_and_oversized_body_are_bounded_unknown_responses(self):
        for body in (b"not json", b'{"code":200,"msg":"' + b"x" * 65_536 + b'"}'):
            with self.subTest(size=len(body)), self.assertRaisesRegex(PushPlusError, "^pushplus_acceptance_unknown$"):
                await client_for(lambda _: httpx.Response(200, content=body)).send("t", "c")

    async def test_unconfigured_client_never_sends(self):
        handler = AsyncMock()
        with self.assertRaises(PushPlusError) as raised:
            await client_for(handler, token="").send("t", "c")
        handler.assert_not_awaited()
        self.assertFalse(raised.exception.acceptance_unknown)
        self.assertEqual(str(raised.exception), "pushplus_unconfigured")


class NotificationApiSafetyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.admin = patch.dict(os.environ, {"ADMIN_API_TOKEN": "local-notification-admin"})
        self.admin.start()
        self.addCleanup(self.admin.stop)
        self.state_patch = patch.object(api_main, "state_store", self.store)
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),
                                      base_url="http://local.invalid",
                                      headers={"X-Admin-Token": "local-notification-admin"})
        self.addAsyncCleanup(self.http.aclose)

    async def request_with(self, handler, *, token=SECRET, authorized=True):
        client = client_for(handler, token=token)
        with patch.object(api_main.execution_engine, "notify", client.send):
            return await self.http.post("/api/v1/notifications/test", json={"title": "test", "content": "content"},
                                        headers={} if authorized else {"X-Admin-Token": "wrong"})

    async def test_api_preserves_acceptance_boundary_and_safe_audit(self):
        response = await self.request_with(lambda _: httpx.Response(200, json={"code": 200, "msg": SECRET, "data": MESSAGE_ID}))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["accepted"])
        self.assertFalse(response.json()["delivery_confirmed"])
        self.assertNotIn("sent", response.json())
        audit = self.store.list_audit()
        self.assertEqual(audit[0]["event_type"], "notification_accepted")
        evidence = json.loads(audit[0]["payload_json"])
        self.assertFalse(evidence["delivery_confirmed"])
        self.assertEqual(evidence["message_id"], MESSAGE_ID)
        self.assertNotIn(SECRET, response.text + str(audit))
        self.assertEqual(self.store.list_orders(), [])

    async def test_http_error_and_unknown_exception_never_leak_secrets_or_claim_sent(self):
        response = await self.request_with(lambda _: httpx.Response(503, text=SECRET))
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "pushplus_acceptance_unknown")
        with patch.object(api_main.execution_engine, "notify", AsyncMock(side_effect=RuntimeError(SECRET))):
            other = await self.http.post("/api/v1/notifications/test", json={})
        self.assertEqual(other.status_code, 502)
        self.assertNotIn(SECRET, other.text + response.text + str(self.store.list_audit()))
        self.assertTrue(all(row["event_type"] == "notification_test_unconfirmed" for row in self.store.list_audit()))

    async def test_rejection_and_missing_configuration_are_distinct(self):
        rejected = await self.request_with(lambda _: httpx.Response(200, json={"code": 401, "msg": SECRET}))
        self.assertEqual(rejected.status_code, 502)
        self.assertEqual(rejected.json()["detail"], "pushplus_rejected")
        handler = AsyncMock()
        missing = await self.request_with(handler, token="")
        self.assertEqual(missing.status_code, 503)
        self.assertEqual(missing.json()["detail"], "pushplus_unconfigured")
        handler.assert_not_awaited()

    async def test_unauthorized_test_is_read_only_and_does_not_contact_pushplus(self):
        handler = AsyncMock()
        denied = await self.request_with(handler, authorized=False)
        self.assertEqual(denied.status_code, 401)
        handler.assert_not_awaited()
        self.assertEqual(self.store.list_audit(), [])


if __name__ == "__main__":
    unittest.main()
