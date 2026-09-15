import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app import main as api_main
from app.state_store import StateStore


class AnalysisHistoryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = StateStore(str(Path(directory.name) / "history.sqlite3"))

    def save(self, index: int, **overrides) -> dict:
        return self.store.save_analysis({
            "inst_id": "BTC-USDT-SWAP",
            "source": "TradingAgents",
            "bias": "research",
            "signal": {},
            "report": {"decision": "Hold", "state": {"market_report": "x" * 10000}},
            "created_at": f"2026-09-13T00:{index:02d}:00+00:00",
            **overrides,
        })

    def test_empty_history(self) -> None:
        self.assertEqual(self.store.analysis_index(), {"data": [], "next_before_id": None})
        self.assertIsNone(self.store.get_analysis(1))

    def test_index_excludes_reports_and_signals(self) -> None:
        saved = self.save(1)
        page = self.store.analysis_index()
        self.assertEqual(set(page["data"][0]), {"id", "inst_id", "source", "bias", "created_at"})
        self.assertEqual(page["data"][0]["id"], saved["id"])
        self.assertIsNone(page["next_before_id"])
        self.assertEqual(self.store.get_analysis(saved["id"]), saved)

    def test_cursor_pages_remain_stable_across_new_records(self) -> None:
        saved = [self.save(index) for index in range(5)]
        first = self.store.analysis_index(2)
        self.save(6)
        second = self.store.analysis_index(2, before_id=first["next_before_id"])
        third = self.store.analysis_index(2, before_id=second["next_before_id"])
        ids = [row["id"] for page in (first, second, third) for row in page["data"]]
        self.assertEqual(ids, [row["id"] for row in reversed(saved)])
        self.assertIsNone(third["next_before_id"])

    def test_filters_compose_with_cursor(self) -> None:
        self.save(1, inst_id="ETH-USDT-SWAP")
        technical = self.save(2, source="structured-technical", bias="neutral")
        ai = self.save(3)
        page = self.store.analysis_index(inst_id="BTC-USDT-SWAP", source="TradingAgents")
        self.assertEqual([row["id"] for row in page["data"]], [ai["id"]])
        page = self.store.analysis_index(before_id=ai["id"], inst_id="BTC-USDT-SWAP")
        self.assertEqual([row["id"] for row in page["data"]], [technical["id"]])

    def test_filter_values_cannot_inject_sql(self) -> None:
        self.save(1)
        self.assertEqual(self.store.analysis_index(source="' OR 1=1 --")["data"], [])
        self.assertEqual(len(self.store.analysis_index()["data"]), 1)

    def test_limits_are_bounded(self) -> None:
        for index in range(55):
            self.save(index)
        self.assertEqual(len(self.store.analysis_index(1000)["data"]), 50)
        self.assertEqual(len(self.store.analysis_index(0)["data"]), 1)

    def test_legacy_list_keeps_complete_shape(self) -> None:
        saved = self.save(1, source="structured-technical", signal={"action": "hold"})
        self.assertEqual(self.store.list_analyses(), [saved])


class AnalysisHistoryAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = StateStore(str(Path(directory.name) / "history.sqlite3"))
        self.addCleanup(patch.stopall)
        patch.object(api_main, "state_store", self.store).start()
        patch.dict(os.environ, {"ADMIN_API_TOKEN": "history-fixture-token"}).start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=api_main.app), base_url="http://test"
        )
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"X-Admin-Token": "history-fixture-token"}

    async def test_history_and_detail_require_admin(self) -> None:
        for route in ("/api/v1/analysis/history", "/api/v1/analysis/1"):
            self.assertEqual((await self.client.get(route)).status_code, 401)
            self.assertEqual((await self.client.get(route, headers={"X-Admin-Token": "wrong"})).status_code, 401)

    async def test_missing_and_invalid_ids(self) -> None:
        self.assertEqual((await self.client.get("/api/v1/analysis/123", headers=self.headers)).status_code, 404)
        self.assertEqual((await self.client.get("/api/v1/analysis/nope", headers=self.headers)).status_code, 422)

    async def test_query_bounds(self) -> None:
        for query in ("limit=0", "limit=51", "before_id=0", "source="):
            response = await self.client.get(f"/api/v1/analysis/history?{query}", headers=self.headers)
            self.assertEqual(response.status_code, 422)

    async def test_history_routes_and_detail_roundtrip(self) -> None:
        saved = self.store.save_analysis({
            "inst_id": "BTC-USDT-SWAP", "source": "TradingAgents", "bias": "research",
            "signal": {}, "report": {"state": {"market_report": "完整研究报告"}},
        })
        response = await self.client.get("/api/v1/analysis/history?source=TradingAgents", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"][0]["id"], saved["id"])
        self.assertNotIn("report", response.json()["data"][0])
        detail = await self.client.get(f"/api/v1/analysis/{saved['id']}", headers=self.headers)
        self.assertEqual(detail.json()["data"], saved)
        self.assertEqual((await self.client.get("/api/v1/analysis/status")).status_code, 200)
