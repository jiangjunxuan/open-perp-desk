import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app import main as api_main
from app.state_store import StateStore


class ChartAnnotationApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.account = SimpleNamespace(configured=True, account_scope="account-a")
        self.patches = [
            patch.object(api_main, "state_store", self.store),
            patch.object(api_main, "account_client", self.account),
            patch.dict(os.environ, {"ADMIN_API_TOKEN": "chart-test-token"}),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_main.app),
            base_url="http://test",
        )
        self.addAsyncCleanup(self.client.aclose)

    async def test_requires_admin_and_is_scoped_to_active_account(self):
        route = "/api/v1/market/annotations?inst_id=BTC-USDT-SWAP&bar=15m"
        self.assertEqual((await self.client.get(route)).status_code, 401)

        body = {
            "annotations": [{
                "id": "horizontal-1",
                "type": "horizontal",
                "price": 100,
            }],
        }
        headers = {"X-Admin-Token": "chart-test-token"}
        response = await self.client.put(route, headers=headers, json=body)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["inst_id"], "BTC-USDT-SWAP")
        self.assertEqual((await self.client.get(route, headers=headers)).json()["data"], response.json()["data"])

        self.account.account_scope = "account-b"
        self.assertEqual((await self.client.get(route, headers=headers)).json()["data"], [])

    async def test_rejects_extra_fields_and_invalid_instrument(self):
        headers = {"X-Admin-Token": "chart-test-token"}
        extra = await self.client.put(
            "/api/v1/market/annotations",
            headers=headers,
            json={"annotations": [{
                "id": "horizontal-1",
                "type": "horizontal",
                "price": 100,
                "text": "unexpected",
            }]},
        )
        self.assertEqual(extra.status_code, 422)

        invalid = await self.client.get(
            "/api/v1/market/annotations?inst_id=BTC-USDT&bar=15m",
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 422)
