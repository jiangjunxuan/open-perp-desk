import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import main as api_main
from app.state_store import StateStore


class ProtectionIncidentApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = StateStore(str(Path(directory.name) / "state.sqlite3"))
        self.record = {
            "account_scope": "review-account", "inst_id": "BTC-USDT-SWAP",
            "position_key": "BTC-USDT-SWAP:net:cross", "opening_order_id": "review-opening",
            "failure_code": "attached_protection_missing",
        }
        self.incident = self.store.record_protection_incident(self.record)
        self.account = SimpleNamespace(account_scope="review-account", configured=True)
        self.sync = AsyncMock(return_value={})
        self.notify = AsyncMock(return_value=True)
        for replacement in (
            patch.dict(os.environ, {"ADMIN_API_TOKEN": "incident-api-fixture"}),
            patch.object(api_main, "state_store", self.store),
            patch.object(api_main, "account_client", self.account),
            patch.object(api_main, "account_sync", SimpleNamespace(sync_rest=self.sync)),
            patch.object(api_main, "execution_engine", SimpleNamespace(notify_event=self.notify)),
        ):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_main.app), base_url="http://test",
            headers={"X-Admin-Token": "incident-api-fixture"},
        )
        self.addAsyncCleanup(self.client.aclose)

    async def resolve(self, **changes):
        return await self.client.post(
            f"/api/v1/protection/incidents/{self.incident['incident_id']}/resolve",
            json={
                "resolution": "external_protection_verified", "note": "External protection reviewed",
                "expected_version": self.incident["version"], **changes,
            },
        )

    def closed_position(self, **changes):
        self.store.upsert_position({
            "position_key": self.record["position_key"], "inst_id": self.record["inst_id"],
            "account_scope": self.account.account_scope, "pos_side": "net",
            "td_mode": "cross", "size": 0, "status": "closed", "entry_price": 50000, **changes,
        })

    def assert_locked(self):
        self.assertIn(self.store.protection_incident(self.incident["incident_id"])["status"], {"open", "review"})
        self.notify.assert_not_awaited()

    async def test_snapshot_includes_client_review_version(self):
        result = await self.client.get("/api/v1/protection/incidents")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["data"][0]["version"], self.incident["version"])
        self.assertNotIn("account_scope", result.json()["data"][0])

    async def test_stale_review_cannot_acknowledge_new_evidence(self):
        self.store.record_protection_incident({**self.record, "failure_detail": "new evidence"})
        response = await self.resolve()
        self.assertEqual(response.status_code, 409)
        self.sync.assert_not_awaited()
        self.assert_locked()

    async def test_validation_requires_strict_version_and_meaningful_note(self):
        for value in (None, True, "0", -1, 0.5):
            with self.subTest(version=value):
                response = await self.resolve(expected_version=value)
                self.assertEqual(response.status_code, 422)
        for note in ("   ", " a ", "x" * 501):
            with self.subTest(note=note[:10]):
                self.assertEqual((await self.resolve(note=note)).status_code, 422)
        response = await self.client.post(
            f"/api/v1/protection/incidents/{self.incident['incident_id']}/resolve",
            json={"resolution": "position_closed", "note": "Missing reviewed version"},
        )
        self.assertEqual(response.status_code, 422)
        self.assert_locked()

    async def test_authentication_and_account_scope_are_required(self):
        for token in ("", "wrong"):
            response = await self.client.post(
                f"/api/v1/protection/incidents/{self.incident['incident_id']}/resolve",
                headers={"X-Admin-Token": token},
                json={"resolution": "position_closed", "note": "Unauthorized", "expected_version": 0},
            )
            self.assertEqual(response.status_code, 401)
        self.account.account_scope = "another-account"
        self.assertEqual((await self.resolve()).status_code, 404)
        self.sync.assert_not_awaited()
        self.assert_locked()

    async def test_closed_resolution_requires_successful_fresh_reconciliation(self):
        self.closed_position()
        self.account.configured = False
        self.assertEqual((await self.resolve(resolution="position_closed")).status_code, 503)
        self.sync.assert_not_awaited()
        self.account.configured = True
        self.sync.side_effect = RuntimeError("sensitive provider detail")
        response = await self.resolve(resolution="position_closed")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("sensitive", response.text)
        self.sync.side_effect = None
        self.sync.return_value = {"errors": ["positions"]}
        self.assertEqual((await self.resolve(resolution="position_closed")).status_code, 409)
        self.assert_locked()
        self.sync.return_value = {}
        self.assertEqual((await self.resolve(resolution="position_closed")).status_code, 200)
        self.assertEqual(self.store.protection_incident(self.incident["incident_id"])["status"], "resolved")
        self.notify.assert_awaited_once()

    async def test_account_switch_during_reconciliation_cannot_resolve(self):
        self.closed_position()

        async def switched():
            self.account.account_scope = "another-account"
            return {}

        self.sync.side_effect = switched
        self.assertEqual((await self.resolve(resolution="position_closed")).status_code, 409)
        self.assert_locked()

    async def test_reopened_position_and_new_evidence_during_sync_stay_locked(self):
        self.closed_position()

        async def reopened():
            self.closed_position(status="open", size=2)
            return {}

        self.sync.side_effect = reopened
        self.assertEqual((await self.resolve(resolution="position_closed")).status_code, 409)
        self.closed_position()

        async def changed():
            self.store.record_protection_incident({**self.record, "failure_detail": "changed during sync"})
            return {}

        self.sync.side_effect = changed
        self.assertEqual((await self.resolve(resolution="position_closed")).status_code, 409)
        self.assert_locked()

    async def test_duplicate_submissions_have_one_resolution_audit_and_notification(self):
        self.closed_position()
        arrived = 0
        release = asyncio.Event()

        async def synchronized():
            nonlocal arrived
            arrived += 1
            if arrived == 2:
                release.set()
            await asyncio.wait_for(release.wait(), 1)
            return {}

        self.sync.side_effect = synchronized
        responses = await asyncio.gather(
            self.resolve(resolution="position_closed"), self.resolve(resolution="position_closed"),
        )
        self.assertEqual(sorted(result.status_code for result in responses), [200, 409])
        self.assertEqual(len(self.store.list_audit()), 1)
        self.notify.assert_awaited_once()

    async def test_delayed_notification_response_keeps_new_incident(self):
        async def changed(*_args, **_kwargs):
            self.store.record_protection_incident({**self.record, "failure_code": "new_failure"})
            return True

        self.notify.side_effect = changed
        response = await self.resolve()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["failure_code"], "new_failure")
        self.assertNotEqual(response.json()["data"][0]["incident_id"], self.incident["incident_id"])
        self.sync.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
