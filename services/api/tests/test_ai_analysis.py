import asyncio
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.ai_analysis import AIAnalysisError, TradingAgentsAdapter
from app import main as api_main
from app import tradingagents_okx_bridge


class TradingAgentsConfigTests(unittest.TestCase):
    def test_adapter_applies_runtime_provider_and_storage_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(
                os.environ,
                {
                    "TRADINGAGENTS_ENABLED": "true",
                    "TRADINGAGENTS_PATH": str(Path(__file__).parent / "fixtures/ai"),
                    "DATA_DIR": directory,
                    "TRADINGAGENTS_LLM_PROVIDER": "openai_compatible",
                    "TRADINGAGENTS_DEEP_THINK_LLM": "deep-model",
                    "TRADINGAGENTS_QUICK_THINK_LLM": "quick-model",
                    "TRADINGAGENTS_LLM_BACKEND_URL": "https://llm.example/v1",
                    "TRADINGAGENTS_RESULTS_DIR": f"{directory}/results",
                    "TRADINGAGENTS_CACHE_DIR": f"{directory}/cache",
                    "TRADINGAGENTS_MEMORY_LOG_PATH": f"{directory}/memory.md",
                    "TRADINGAGENTS_OUTPUT_LANGUAGE": "Chinese",
                    "TRADINGAGENTS_DATA_TIMEOUT_SECONDS": "12",
                    "OKX_SECRET_KEY": "must-not-reach-child",
                    "ADMIN_API_TOKEN": "must-not-reach-child",
                    "LIVE_UNLOCK_PHRASE": "must-not-reach-child",
                    "PUSHPLUS_TOKEN": "must-not-reach-child",
                    "PYTHONPATH": "/must/not/be/inherited",
                    "OPENAI_COMPATIBLE_API_KEY": "fixture-provider-key",
                },
                clear=True,
            ):
                adapter = TradingAgentsAdapter()
                original_path = list(sys.path)
                result = asyncio.run(adapter.analyze("BTC-USDT-SWAP", {
                    "inst_id": "BTC-USDT-SWAP", "bar": "15m", "candle_count": 100,
                    "candles": [[str(i)] for i in range(100)], "captured_at": "fixture-time",
                }))
            captured = result["state"]
            config = captured["config"]
            self.assertEqual(adapter.data_timeout_seconds, 12)
            self.assertEqual(config["llm_provider"], "openai_compatible")
            self.assertEqual(config["deep_think_llm"], "deep-model")
            self.assertEqual(config["quick_think_llm"], "quick-model")
            self.assertEqual(config["backend_url"], "https://llm.example/v1")
            self.assertEqual(config["results_dir"], f"{directory}/results")
            self.assertEqual(config["data_cache_dir"], f"{directory}/cache")
            self.assertEqual(config["memory_log_path"], f"{directory}/memory.md")
            self.assertEqual(config["output_language"], "Chinese")
            self.assertEqual(config["max_tokens"], 4096)
            self.assertEqual(config["llm_max_retries"], 1)
            self.assertFalse(config["checkpoint_enabled"])
            self.assertEqual(result["decision"], "Hold")
            self.assertEqual(result["market_context"]["bar"], "15m")
            self.assertEqual(captured["asset_type"], "crypto")
            self.assertEqual(captured["ticker"], "BTC-USD")
            self.assertIn("External OKX perpetual market evidence", captured["context"])
            self.assertIn('"bar": "15m"', captured["context"])
            self.assertNotIn('["20"]', captured["context"])
            self.assertNotEqual(captured["pid"], os.getpid())
            self.assertEqual(sys.path, original_path)
            self.assertNotIn("must-not-reach-child", str(captured))
            self.assertNotIn("PYTHONPATH", captured["environment"])
            self.assertEqual(captured["environment"]["OPENAI_COMPATIBLE_API_KEY"], "fixture-provider-key")
            self.assertFalse(Path(captured["home"]).exists())
            self.assertTrue(str(captured["home"]).startswith(directory))
            self.assertEqual(result["signal"], {})
            self.assertFalse(result["execution_authorized"])
            self.assertEqual(adapter.runtime_state, "ready")

    def test_adapter_rejects_unknown_run_mode(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TRADINGAGENTS_ENABLED": "true",
                "TRADINGAGENTS_PATH": str(Path(__file__).parent / "fixtures/ai"),
                "TRADINGAGENTS_RUN_MODE": "debate-lite",
            },
            clear=True,
        ):
            adapter = TradingAgentsAdapter()
        self.assertEqual(adapter.configuration_error, "invalid_config")
        self.assertEqual(adapter.run_mode, "full")


class AIProcessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = {
            "TRADINGAGENTS_ENABLED": "true",
            "TRADINGAGENTS_PATH": str(Path(__file__).parent / "fixtures/ai"),
            "DATA_DIR": self.directory.name,
        }

    def adapter(self, **extra) -> TradingAgentsAdapter:
        with patch.dict(os.environ, {**self.env, **extra}, clear=True):
            return TradingAgentsAdapter()

    async def wait_for_pid(self, adapter) -> int:
        path = Path(adapter.config["results_dir"]) / "pid"
        for _ in range(200):
            if path.exists():
                return int(path.read_text())
            await asyncio.sleep(.01)
        self.fail("Test process did not start")

    def assert_dead(self, pid) -> None:
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    async def test_disabled_and_incomplete_source_do_not_spawn(self) -> None:
        for env, code in (
            ({"TRADINGAGENTS_ENABLED": "false"}, "disabled"),
            ({"TRADINGAGENTS_PATH": self.directory.name}, "source_missing"),
            ({"TRADINGAGENTS_PYTHON": "/missing/python"}, "runtime_missing"),
            ({"TRADINGAGENTS_TIMEOUT_SECONDS": "nan"}, "invalid_config"),
            ({"TRADINGAGENTS_MAX_TOKENS": "0"}, "invalid_config"),
            ({"TRADINGAGENTS_DATA_TIMEOUT_SECONDS": "0"}, "invalid_config"),
        ):
            adapter = self.adapter(**env)
            with self.assertRaises(AIAnalysisError) as raised:
                await adapter.check_ready()
            self.assertEqual(raised.exception.code, code)
            self.assertFalse(adapter.configured)

    async def test_probe_checks_graph_without_running_research(self) -> None:
        adapter = self.adapter(TRADINGAGENTS_DEEP_THINK_LLM="sleep")
        result = await adapter.check_ready()
        self.assertFalse(result["provider_connection_verified"])
        self.assertEqual(adapter.runtime_state, "ready")
        self.assertFalse((Path(adapter.config["results_dir"]) / "pid").exists())

    async def test_fast_mode_uses_one_okx_evidence_call_without_graph_execution(self) -> None:
        adapter = self.adapter(
            TRADINGAGENTS_RUN_MODE="fast",
            TRADINGAGENTS_LLM_PROVIDER="openai_compatible",
            TRADINGAGENTS_QUICK_THINK_LLM="fixture-fast",
            TRADINGAGENTS_DEEP_THINK_LLM="fixture-deep",
        )
        result = await adapter.analyze("BTC-USDT-SWAP", {
            "inst_id": "BTC-USDT-SWAP",
            "bar": "15m",
            "candle_count": 2,
            "captured_at": "fixture-time",
            "candles": [
                ["1", "99", "101", "98", "100", "12"],
                ["2", "100", "102", "99", "101", "14"],
            ],
        })
        self.assertEqual(result["mode"], "fast")
        self.assertEqual(result["decision"], "Hold")
        self.assertEqual(result["state"]["fast_research"]["summary"], "fixture summary")
        self.assertEqual(result["signal"], {})
        self.assertFalse(result["execution_authorized"])
        self.assertFalse((Path(adapter.config["results_dir"]) / "pid").exists())

    async def test_timeout_terminates_term_ignoring_child_and_releases_lock(self) -> None:
        adapter = self.adapter(TRADINGAGENTS_DEEP_THINK_LLM="sleep", TRADINGAGENTS_TIMEOUT_SECONDS="1")
        task = asyncio.create_task(adapter.analyze("BTC-USDT-SWAP"))
        pid = await self.wait_for_pid(adapter)
        with self.assertRaises(AIAnalysisError) as raised:
            await task
        self.assertEqual(raised.exception.code, "timeout")
        self.assert_dead(pid)
        self.assertFalse(adapter.status()["busy"])
        self.assertFalse(list(adapter.root.glob("run-*")))
        self.assertEqual((await self.adapter().check_ready())["runtime_state"], "ready")

    async def test_cancel_and_shutdown_reap_child(self) -> None:
        for stop in ("cancel", "shutdown"):
            adapter = self.adapter(TRADINGAGENTS_DEEP_THINK_LLM="sleep")
            path = Path(adapter.config["results_dir"]) / "pid"
            path.unlink(missing_ok=True)
            task = asyncio.create_task(adapter.analyze("BTC-USDT-SWAP"))
            pid = await self.wait_for_pid(adapter)
            if stop == "cancel":
                task.cancel()
            else:
                await adapter.close()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assert_dead(pid)
            self.assertEqual(adapter.runtime_state, "canceled")
            self.assertFalse(adapter.status()["busy"])

    async def test_shared_storage_lock_rejects_parallel_adapters(self) -> None:
        first = self.adapter(TRADINGAGENTS_DEEP_THINK_LLM="sleep")
        task = asyncio.create_task(first.analyze("BTC-USDT-SWAP"))
        await self.wait_for_pid(first)
        try:
            with self.assertRaises(AIAnalysisError) as raised:
                await self.adapter().check_ready()
            self.assertEqual(raised.exception.code, "busy")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_crash_and_provider_error_are_redacted(self) -> None:
        for mode in ("crash", "error"):
            adapter = self.adapter(TRADINGAGENTS_DEEP_THINK_LLM=mode)
            with self.assertRaises(AIAnalysisError) as raised:
                await adapter.analyze("BTC-USDT-SWAP")
            self.assertEqual(raised.exception.code, "runtime_failed")
            self.assertNotIn("sensitive", str(raised.exception))
            self.assertFalse(list(adapter.root.glob("run-*")))

    async def test_output_and_input_are_bounded(self) -> None:
        adapter = self.adapter(TRADINGAGENTS_DEEP_THINK_LLM="large", TRADINGAGENTS_MAX_OUTPUT_BYTES="1024")
        with self.assertRaises(AIAnalysisError) as raised:
            await adapter.analyze("BTC-USDT-SWAP")
        self.assertEqual(raised.exception.code, "output_limit")
        with self.assertRaises(AIAnalysisError) as raised:
            await adapter.analyze("BTC-USDT-SWAP", {"huge": "x" * (300 * 1024)})
        self.assertEqual(raised.exception.code, "request_limit")

    async def test_invalid_instrument_is_rejected(self) -> None:
        for instrument in ("BTC", "../../BTC-USDT-SWAP", "BTC-USDT", "BTC-USD-SWAP\n"):
            with self.assertRaises(AIAnalysisError) as raised:
                await self.adapter().analyze(instrument)
            self.assertEqual(raised.exception.code, "invalid_instrument")

    async def test_wrong_instrument_result_is_rejected(self) -> None:
        adapter = self.adapter()
        with patch.object(adapter, "_invoke", AsyncMock(return_value={
            "inst_id": "ETH-USDT-SWAP", "state": {}, "decision": "Hold",
        })):
            with self.assertRaises(AIAnalysisError) as raised:
                await adapter.analyze("BTC-USDT-SWAP")
        self.assertEqual(raised.exception.code, "invalid_result")

    async def test_model_output_cannot_authorize_execution(self) -> None:
        adapter = self.adapter()
        with patch.object(adapter, "_invoke", AsyncMock(return_value={
            "inst_id": "BTC-USDT-SWAP", "state": {}, "decision": "Buy",
            "execution_authorized": True, "signal": {"action": "open_long"}, "source": "structured",
        })):
            result = await adapter.analyze("BTC-USDT-SWAP")
        self.assertEqual(result["signal"], {})
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["source"], "TradingAgents")

    async def wait_for_exit(self, pid) -> None:
        for _ in range(200):
            try:
                status = subprocess.run(
                    ["ps", "-p", str(pid), "-o", "stat="],
                    capture_output=True, text=True, check=False,
                ).stdout.strip()
                if not status or status.startswith("Z"):
                    return
            except ProcessLookupError:
                return
            await asyncio.sleep(.01)
        self.fail(f"Research process {pid} survived termination")

    async def test_timeout_kills_tool_descendants(self) -> None:
        adapter = self.adapter(TRADINGAGENTS_DEEP_THINK_LLM="descendant", TRADINGAGENTS_TIMEOUT_SECONDS="1")
        task = asyncio.create_task(adapter.analyze("BTC-USDT-SWAP"))
        pid = await self.wait_for_pid(adapter)
        with self.assertRaises(AIAnalysisError):
            await task
        self.assert_dead(pid)
        descendant = int((Path(adapter.config["results_dir"]) / "descendant").read_text())
        await self.wait_for_exit(descendant)

    async def test_parent_process_death_does_not_leave_research_running(self) -> None:
        adapter = self.adapter()
        parent = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import asyncio; from app.ai_analysis import TradingAgentsAdapter; "
            "asyncio.run(TradingAgentsAdapter().analyze('BTC-USDT-SWAP'))",
            env={
                **self.env,
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "TRADINGAGENTS_DEEP_THINK_LLM": "sleep",
            },
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        pid = None
        try:
            pid = await self.wait_for_pid(adapter)
            parent.kill()
            await parent.wait()
            await self.wait_for_exit(pid)
        finally:
            if parent.returncode is None:
                parent.kill()
                await parent.wait()
            if pid:
                try:
                    os.killpg(pid, 9)
                except ProcessLookupError:
                    pass


class AIEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_requires_admin_and_readiness_is_not_connection_proof(self) -> None:
        adapter = Mock()
        adapter.check_ready = AsyncMock(return_value={"provider_connection_verified": False})
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "fixture-token"}), patch.object(api_main, "tradingagents", adapter):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="http://test") as client:
                self.assertEqual((await client.post("/api/v1/analysis/ai/check")).status_code, 401)
                adapter.check_ready.assert_not_awaited()
                response = await client.post("/api/v1/analysis/ai/check", headers={"X-Admin-Token": "fixture-token"})
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.json()["data"]["provider_connection_verified"])

    async def test_failed_analysis_has_safe_audit_and_no_report(self) -> None:
        adapter = Mock(configuration_error=None)
        adapter.analyze = AsyncMock(side_effect=AIAnalysisError("timeout"))
        store = Mock()
        with patch.object(api_main, "tradingagents", adapter), patch.object(api_main, "state_store", store), patch.object(api_main, "_ai_market_context", AsyncMock(return_value={})):
            with self.assertRaises(api_main.HTTPException) as raised:
                await api_main.run_ai_analysis(api_main.AnalysisRequest(inst_id="BTC-USDT-SWAP"))
        self.assertEqual(raised.exception.status_code, 504)
        store.save_analysis.assert_not_called()
        self.assertEqual(store.add_audit.call_args.kwargs["payload"]["code"], "timeout")

    async def test_research_is_stored_without_execution_signal(self) -> None:
        adapter = Mock(configuration_error=None)
        adapter.analyze = AsyncMock(return_value={"decision": "Buy", "state": {}})
        store = Mock()
        store.save_analysis.return_value = {"id": 12, "created_at": "2026-09-13T00:00:00+00:00"}
        with patch.object(api_main, "tradingagents", adapter), patch.object(api_main, "state_store", store), patch.object(api_main, "_ai_market_context", AsyncMock(return_value={})), patch.object(api_main, "execution_engine", Mock()) as engine:
            result = await api_main.run_ai_analysis(api_main.AnalysisRequest(inst_id="BTC-USDT-SWAP"))
        self.assertEqual(store.save_analysis.call_args.args[0]["signal"], {})
        self.assertEqual(engine.mock_calls, [])
        self.assertEqual(result["data"]["id"], 12)


class AIMarketContextTests(unittest.TestCase):
    def test_context_keeps_requested_window_and_names_partial_failures(self) -> None:
        class FakeMarket:
            async def ticker(self, _inst_id):
                return {"last": "100"}

            async def candles(self, _inst_id, bar, limit):
                self.bar = bar
                self.limit = limit
                return [["1", "99", "101", "98", "100"]]

            async def funding_rate(self, _inst_id):
                raise RuntimeError("funding unavailable")

            async def open_interest(self, _inst_id):
                return {"oi": "12"}

        class FakeStream:
            connected = True
            fresh = True
            last_message_at = "2026-09-12T00:00:00+00:00"

        market = FakeMarket()
        with patch.object(api_main, "market_client", market), patch.object(
            api_main,
            "market_stream",
            FakeStream(),
        ):
            context = asyncio.run(
                api_main._ai_market_context("BTC-USDT-SWAP", "15m", 80)
            )

        self.assertEqual(market.bar, "15m")
        self.assertEqual(market.limit, 80)
        self.assertEqual(context["bar"], "15m")
        self.assertEqual(context["requested_limit"], 80)
        self.assertEqual(context["candle_count"], 1)
        self.assertEqual(context["errors"], ["funding_rate"])
        self.assertEqual(context["open_interest"]["oi"], "12")


class OkxBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.previous_context = tradingagents_okx_bridge._CONTEXT
        base = datetime(2026, 9, 17, tzinfo=timezone.utc)
        rows = []
        for index in range(220):
            timestamp = int((base + timedelta(minutes=15 * index)).timestamp() * 1000)
            close = 100 + index / 10
            rows.append([
                str(timestamp), str(close), str(close + 1), str(close - 1),
                str(close + 0.5), "10", "0", "0", "1",
            ])
        tradingagents_okx_bridge._CONTEXT = {
            "inst_id": "BTC-USDT-SWAP",
            "bar": "15m",
            "captured_at": "2026-09-17T00:00:00+00:00",
            "candles": list(reversed(rows)),
            "funding_rate": {"fundingRate": "0.001"},
            "open_interest": {"oi": "12"},
        }

    def tearDown(self) -> None:
        tradingagents_okx_bridge._CONTEXT = self.previous_context

    def test_okx_rows_feed_csv_indicators_and_verified_snapshot(self) -> None:
        csv_text = tradingagents_okx_bridge._stock_data(
            "BTC-USD", "2026-09-17", "2026-09-17"
        )
        self.assertIn("# OKX perpetual data for BTC-USDT-SWAP", csv_text)
        self.assertLess(csv_text.index("2026-09-17T00:00:00+00:00"), csv_text.index("2026-09-17T00:15:00+00:00"))

        indicator_text = tradingagents_okx_bridge._indicators(
            "BTC-USD", "rsi", "2026-09-17", 5
        )
        self.assertIn("OKX rsi values", indicator_text)
        self.assertIn("100", indicator_text)

        snapshot = tradingagents_okx_bridge._verified_snapshot("BTC-USD", "2026-09-17")
        self.assertIn("Verified OKX market snapshot", snapshot)
        self.assertIn("close_200_sma", snapshot)
        self.assertIn("Funding rate: 0.001", snapshot)

    def test_okx_bridge_rejects_a_different_instrument(self) -> None:
        result = tradingagents_okx_bridge._stock_data(
            "ETH-USD", "2026-09-17", "2026-09-17"
        )
        self.assertIn("DATA_UNAVAILABLE", result)


if __name__ == "__main__":
    unittest.main()
