import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import main as api_main
from app.live_safety import LiveSafetyGate
from app.okx_trade import OkxTradeClient
from app.safety_control import SafetyController
from app.state_store import StateStore


class LiveEmergencySafetyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        environment = patch.dict(os.environ, {
            "ADMIN_API_TOKEN": "local-live-gate-test",
            "LIVE_UNLOCK_PHRASE": "local-independent-phrase",
            "TRADING_MODE": "live",
            "OKX_DEMO": "false",
            "EXECUTION_ENABLED": "true",
            "LIVE_TRADING_ENABLED": "true",
            "OKX_API_KEY": "fixture-key",
            "OKX_SECRET_KEY": "fixture-secret",
            "OKX_PASSPHRASE": "fixture-passphrase",
        }, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.store = StateStore(str(Path(directory.name) / "state.sqlite3"))
        self.safety = SafetyController(self.store)
        self.gate = LiveSafetyGate()
        self.trade = OkxTradeClient(
            self.gate, transport=httpx.MockTransport(self.reject_exchange_request),
        )
        self.notify = AsyncMock(return_value=True)
        for name, value in {
            "state_store": self.store,
            "safety_controller": self.safety,
            "trade_client": self.trade,
            "execution_engine": SimpleNamespace(notify_event=self.notify),
        }.items():
            replacement = patch.object(api_main, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_main.app), base_url="http://test",
            headers={"X-Admin-Token": "local-live-gate-test"},
        )
        self.addAsyncCleanup(self.client.aclose)

    def reject_exchange_request(self, request):
        self.fail(f"Safety routes must not contact the exchange: {request.method}")

    async def post(self, path, payload=None, **kwargs):
        return await self.client.post(f"/api/v1/safety/{path}", json=payload, **kwargs)

    async def test_emergency_stop_revokes_unlock_before_notification_and_resume(self):
        self.assertTrue(self.gate.unlock("local-independent-phrase"))
        self.assertTrue(self.trade.enabled)
        observed = []

        async def notification(event_type, *_args, **_kwargs):
            observed.append((event_type, self.gate.allowed, self.safety.emergency_stopped))
            return True

        self.notify.side_effect = notification
        stopped = await self.post("emergency-stop", {"reason": "test emergency"})
        self.assertEqual(stopped.status_code, 200)
        self.assertTrue(stopped.json()["emergency_stopped"])
        self.assertFalse(self.gate.snapshot()["unlocked"])
        self.assertIsNone(self.gate.snapshot()["unlocked_at"])
        self.assertFalse(self.trade.enabled)
        resumed = await self.post("resume", {"reason": "test resume"})
        self.assertEqual(resumed.status_code, 200)
        self.assertFalse(resumed.json()["emergency_stopped"])
        self.assertFalse(self.gate.allowed or self.trade.enabled)
        self.assertEqual(observed, [
            ("emergency_stop", False, True), ("emergency_resume", False, False),
        ])

        unlocked = await self.post("live/unlock", {"phrase": "local-independent-phrase"})
        self.assertEqual(unlocked.status_code, 200)
        self.assertTrue(unlocked.json()["live_safety"]["allowed"])
        self.assertTrue(self.trade.enabled)
        self.assertNotIn("local-independent-phrase", json.dumps(self.store.list_audit()))

    async def test_unlock_during_emergency_is_rejected_and_cannot_be_prearmed(self):
        self.safety.stop("persistent stop")
        self.assertTrue(self.gate.unlock("local-independent-phrase"))
        result = await self.post("live/unlock", {"phrase": "local-independent-phrase"})
        self.assertEqual(result.status_code, 423)
        self.assertFalse(self.gate.snapshot()["unlocked"])
        self.notify.assert_not_awaited()
        self.assertEqual(self.store.list_audit(), [])
        await self.post("resume", {"reason": "review complete"})
        self.assertFalse(self.trade.enabled)
        wrong = await self.post("live/unlock", {"phrase": "wrong"})
        self.assertEqual(wrong.status_code, 403)
        self.assertFalse(self.gate.allowed)

    async def test_unauthorized_controls_cannot_change_either_gate(self):
        self.assertTrue(self.gate.unlock("local-independent-phrase"))
        for token in ("", "wrong"):
            for path, payload in (
                ("emergency-stop", {"reason": "unauthorized"}),
                ("resume", {"reason": "unauthorized"}),
                ("live/unlock", {"phrase": "local-independent-phrase"}),
                ("live/lock", None),
            ):
                with self.subTest(token=token, path=path):
                    result = await self.post(path, payload, headers={"X-Admin-Token": token})
                    self.assertEqual(result.status_code, 401)
                    self.assertTrue(self.gate.allowed)
                    self.assertFalse(self.safety.emergency_stopped)
        self.assertEqual(self.store.list_audit(), [])
        self.notify.assert_not_awaited()

    async def test_repeated_emergency_and_new_process_gate_stay_locked(self):
        self.assertTrue(self.gate.unlock("local-independent-phrase"))
        for _ in range(2):
            result = await self.post("emergency-stop", {"reason": "repeat stop"})
            self.assertEqual(result.status_code, 200)
            self.assertFalse(self.gate.allowed)
        reopened = SafetyController(StateStore(str(self.store.path)))
        self.assertTrue(reopened.emergency_stopped)
        self.assertFalse(LiveSafetyGate().allowed)

    async def test_delayed_unlock_response_reflects_newer_emergency_stop(self):
        notifying = asyncio.Event()
        release = asyncio.Event()

        async def notification(event_type, *_args, **_kwargs):
            if event_type == "live_safety_unlocked":
                notifying.set()
                await release.wait()
            return True

        self.notify.side_effect = notification
        pending = asyncio.create_task(
            self.post("live/unlock", {"phrase": "local-independent-phrase"}),
        )
        try:
            await asyncio.wait_for(notifying.wait(), 1)
            self.assertTrue(self.gate.allowed)
            stopped = await self.post("emergency-stop", {"reason": "newer emergency"})
            self.assertEqual(stopped.status_code, 200)
            self.assertFalse(self.trade.enabled)
            release.set()
            result = await asyncio.wait_for(pending, 1)
            self.assertEqual(result.status_code, 200)
            self.assertFalse(result.json()["unlocked"])
            self.assertFalse(result.json()["live_safety"]["allowed"])
        finally:
            release.set()
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
