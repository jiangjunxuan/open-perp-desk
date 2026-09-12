import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class AIAnalysisError(RuntimeError):
    """Raised when the optional TradingAgents adapter cannot run."""


def _tradingagents_ticker(inst_id: str) -> str:
    base = inst_id.split("-", 1)[0].upper()
    return f"{base}-USD"


def _json_safe(value: Any) -> Any:
    """Convert LangGraph/LangChain values into durable API-safe JSON."""
    return json.loads(json.dumps(value, ensure_ascii=True, default=str))


class TradingAgentsAdapter:
    """Optional bridge to the vendored TradingAgents framework.

    The API service remains lightweight by default. Set
    TRADINGAGENTS_ENABLED=true and point TRADINGAGENTS_PATH at an installed
    TradingAgents source tree when LLM dependencies and credentials are ready.
    """

    def __init__(self) -> None:
        self.enabled = os.getenv("TRADINGAGENTS_ENABLED", "false").lower() == "true"
        self.path = os.getenv("TRADINGAGENTS_PATH", "").strip()

    @property
    def configured(self) -> bool:
        return self.enabled and bool(self.path) and Path(self.path).is_dir()

    def _run_sync(self, inst_id: str) -> dict[str, Any]:
        if not self.configured:
            raise AIAnalysisError(
                "TradingAgents is disabled; configure TRADINGAGENTS_ENABLED and "
                "TRADINGAGENTS_PATH first."
            )
        if self.path not in sys.path:
            sys.path.insert(0, self.path)
        try:
            from tradingagents.default_config import DEFAULT_CONFIG
            from tradingagents.graph.trading_graph import TradingAgentsGraph
        except ImportError as exc:
            raise AIAnalysisError(
                "TradingAgents dependencies are not installed in the API runtime."
            ) from exc

        config = DEFAULT_CONFIG.copy()
        env_config = {
            "TRADINGAGENTS_LLM_PROVIDER": "llm_provider",
            "TRADINGAGENTS_DEEP_THINK_LLM": "deep_think_llm",
            "TRADINGAGENTS_QUICK_THINK_LLM": "quick_think_llm",
            "TRADINGAGENTS_LLM_BACKEND_URL": "backend_url",
            "TRADINGAGENTS_RESULTS_DIR": "results_dir",
            "TRADINGAGENTS_CACHE_DIR": "data_cache_dir",
            "TRADINGAGENTS_MEMORY_LOG_PATH": "memory_log_path",
        }
        for env_name, config_name in env_config.items():
            value = os.getenv(env_name, "").strip()
            if value:
                config[config_name] = value
        config["output_language"] = os.getenv(
            "TRADINGAGENTS_OUTPUT_LANGUAGE",
            "Chinese",
        )
        graph = TradingAgentsGraph(
            selected_analysts=("market", "social", "news"),
            debug=False,
            config=config,
        )
        ticker = _tradingagents_ticker(inst_id)
        state, decision = graph.propagate(
            ticker,
            datetime.now(timezone.utc).date().isoformat(),
            asset_type="crypto",
        )
        return {
            "inst_id": inst_id,
            "source": "TradingAgents",
            "bias": "research",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "decision": _json_safe(decision),
            "state": _json_safe(state),
        }

    async def analyze(self, inst_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._run_sync, inst_id)
