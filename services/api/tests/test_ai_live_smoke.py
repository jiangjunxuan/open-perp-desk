import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra/ai-live-smoke.py"
spec = importlib.util.spec_from_file_location("ai_live_smoke_probe", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class _Adapter:
    configured = True
    configuration_error = None
    run_mode = "full"

    def __init__(self):
        self.captured = {
            "run_mode": os.getenv("TRADINGAGENTS_RUN_MODE"),
            "timeout": os.getenv("TRADINGAGENTS_TIMEOUT_SECONDS"),
        }

    async def analyze(self, _inst_id, market_context=None):
        return {
            "inst_id": _inst_id,
            "source": "TradingAgents",
            "state": {"final_trade_decision": "Hold"},
            "signal": {},
            "execution_authorized": False,
            "decision": "Hold",
            "mode": "full",
        }


class AiLiveSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_aligns_and_restores_adapter_deadline(self):
        context = {
            "inst_id": "BTC-USDT-SWAP",
            "bar": "15m",
            "candles": [["1", "99", "101", "98", "100", "1"]] * 30,
        }
        created = []

        def make_adapter():
            adapter = _Adapter()
            created.append(adapter)
            return adapter

        with patch.dict(
            os.environ,
            {
                "TRADINGAGENTS_RUN_MODE": "fast",
                "TRADINGAGENTS_TIMEOUT_SECONDS": "77",
            },
            clear=True,
        ), patch.object(probe, "TradingAgentsAdapter", side_effect=make_adapter), patch.object(
            probe, "public_context", new=AsyncMock(return_value=context)
        ), patch.object(probe, "OkxMarketClient"):
            result = await probe.run_probe(
                timeout=1800,
                inst_id="BTC-USDT-SWAP",
                bar="15m",
                limit=100,
                run_mode="full",
            )
            restored = {
                "run_mode": os.environ.get("TRADINGAGENTS_RUN_MODE"),
                "timeout": os.environ.get("TRADINGAGENTS_TIMEOUT_SECONDS"),
            }

        self.assertEqual(len(created), 1)
        adapter = created[0]
        self.assertEqual(result["mode"], "full")
        self.assertEqual(adapter.captured, {"run_mode": "full", "timeout": "1800"})
        self.assertEqual(restored, {"run_mode": "fast", "timeout": "77"})


if __name__ == "__main__":
    unittest.main()
