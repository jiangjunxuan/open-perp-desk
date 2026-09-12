import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from app.ai_analysis import TradingAgentsAdapter
from app import main as api_main


class TradingAgentsConfigTests(unittest.TestCase):
    def test_adapter_applies_runtime_provider_and_storage_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "tradingagents"
            source_path.mkdir()
            with patch.dict(
                os.environ,
                {
                    "TRADINGAGENTS_ENABLED": "true",
                    "TRADINGAGENTS_PATH": str(source_path),
                    "TRADINGAGENTS_LLM_PROVIDER": "openai_compatible",
                    "TRADINGAGENTS_DEEP_THINK_LLM": "deep-model",
                    "TRADINGAGENTS_QUICK_THINK_LLM": "quick-model",
                    "TRADINGAGENTS_LLM_BACKEND_URL": "https://llm.example/v1",
                    "TRADINGAGENTS_RESULTS_DIR": "/data/results",
                    "TRADINGAGENTS_CACHE_DIR": "/data/cache",
                    "TRADINGAGENTS_MEMORY_LOG_PATH": "/data/memory.md",
                    "TRADINGAGENTS_OUTPUT_LANGUAGE": "Chinese",
                },
                clear=False,
            ):
                adapter = TradingAgentsAdapter()
                captured: dict[str, object] = {}

                class FakeGraph:
                    def __init__(self, **kwargs):
                        captured["config"] = kwargs["config"]

                    def propagate(self, *_args, **_kwargs):
                        return {"ok": True}, "Hold"

                default_module = ModuleType("tradingagents.default_config")
                default_module.DEFAULT_CONFIG = {
                    "llm_provider": "openai",
                    "deep_think_llm": "default-deep",
                    "quick_think_llm": "default-quick",
                    "backend_url": None,
                    "results_dir": "/tmp/default-results",
                    "data_cache_dir": "/tmp/default-cache",
                    "memory_log_path": "/tmp/default-memory.md",
                }
                graph_module = ModuleType("tradingagents.graph.trading_graph")
                graph_module.TradingAgentsGraph = FakeGraph
                package_module = ModuleType("tradingagents")
                package_module.__path__ = [str(source_path)]
                graph_package = ModuleType("tradingagents.graph")
                graph_package.__path__ = []

                with patch.dict(
                    sys.modules,
                    {
                        "tradingagents": package_module,
                        "tradingagents.graph": graph_package,
                        "tradingagents.default_config": default_module,
                        "tradingagents.graph.trading_graph": graph_module,
                    },
                ):
                    result = adapter._run_sync(
                        "BTC-USDT-SWAP",
                        {
                            "inst_id": "BTC-USDT-SWAP",
                            "bar": "15m",
                            "candle_count": 100,
                        },
                    )

        config = captured["config"]
        self.assertEqual(config["llm_provider"], "openai_compatible")
        self.assertEqual(config["deep_think_llm"], "deep-model")
        self.assertEqual(config["quick_think_llm"], "quick-model")
        self.assertEqual(config["backend_url"], "https://llm.example/v1")
        self.assertEqual(config["results_dir"], "/data/results")
        self.assertEqual(config["data_cache_dir"], "/data/cache")
        self.assertEqual(config["memory_log_path"], "/data/memory.md")
        self.assertEqual(config["output_language"], "Chinese")
        self.assertEqual(result["decision"], "Hold")
        self.assertEqual(result["market_context"]["bar"], "15m")
        self.assertEqual(result["market_context"]["candle_count"], 100)


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


if __name__ == "__main__":
    unittest.main()
