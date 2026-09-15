"""Real TradingAgents graph + local model protocol fixture; never a live-model test."""

import argparse
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "services/api"))
from app.ai_analysis import SYSTEM_ENV, TradingAgentsAdapter


FIXTURES = {
    "ResearchPlan": {
        "recommendation": "Hold", "rationale": "Local protocol fixture, not investment research.",
        "strategic_actions": "Do not place orders.",
    },
    "TraderProposal": {
        "action": "Hold", "reasoning": "Local protocol fixture, no orders.",
        "entry_price": None, "stop_loss": None, "position_sizing": None,
    },
    "PortfolioDecision": {
        "rating": "Hold", "executive_summary": "Local protocol fixture, not a live model.",
        "investment_thesis": "Verify orchestration only. Do not place orders.",
        "price_target": None, "time_horizon": None,
    },
    "SentimentReport": {
        "overall_band": "Neutral", "overall_score": 5.0, "confidence": "low",
        "narrative": "Local protocol fixture. No social-data analysis was performed.",
    },
}


class ProviderFixture(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *_args):
        pass

    def do_POST(self):
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        schema_name = body.get("response_format", {}).get("json_schema", {}).get("name")
        forced_tool = body.get("tool_choice", {})
        tool_name = forced_tool.get("function", {}).get("name") if isinstance(forced_tool, dict) else None
        schema_tools = [
            tool.get("function", {}).get("name") for tool in body.get("tools", [])
            if tool.get("function", {}).get("name") in FIXTURES
        ]
        if len(schema_tools) == 1:
            tool_name = schema_tools[0]
        name = schema_name or tool_name
        content = json.dumps(FIXTURES[name]) if name in FIXTURES else "Local protocol fixture. Evidence is insufficient. **Rating**: Hold"
        message = {"role": "assistant", "content": content}
        if tool_name in FIXTURES:
            message = {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": f"call-{len(self.calls)}", "type": "function",
                                "function": {"name": tool_name, "arguments": content}}],
            }
        serialized_messages = json.dumps(body.get("messages", []))
        self.calls.append({
            "schema": name,
            "has_okx_evidence": "External OKX perpetual market evidence" in serialized_messages,
            "bounded_tokens": body.get("max_tokens", body.get("max_completion_tokens")) == 4096,
        })
        response = json.dumps({
            "id": f"chatcmpl-fixture-{len(self.calls)}", "object": "chat.completion",
            "created": int(time.time()), "model": "protocol-fixture",
            "choices": [{"index": 0, "message": message,
                         "finish_reason": "tool_calls" if tool_name in FIXTURES else "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)


def read_public(origin, route):
    with urlopen(origin.rstrip("/") + route, timeout=30) as response:
        return json.load(response)


async def verify(args, port, directory):
    environment = {name: value for name, value in os.environ.items() if name in SYSTEM_ENV}
    environment.update({
        "TRADINGAGENTS_ENABLED": "true", "TRADINGAGENTS_PATH": str(args.source.resolve()),
        "TRADINGAGENTS_PYTHON": os.path.abspath(args.python),
        "TRADINGAGENTS_LLM_PROVIDER": "openai_compatible",
        "TRADINGAGENTS_LLM_BACKEND_URL": f"http://127.0.0.1:{port}/v1",
        "TRADINGAGENTS_DEEP_THINK_LLM": "protocol-fixture",
        "TRADINGAGENTS_QUICK_THINK_LLM": "protocol-fixture",
        "TRADINGAGENTS_TIMEOUT_SECONDS": "120", "DATA_DIR": directory,
    })
    with patch.dict(os.environ, environment, clear=True):
        adapter = TradingAgentsAdapter()
    ready = await adapter.check_ready()
    stream = read_public(args.origin, "/api/v1/market/stream?symbol=BTC-USDT-SWAP")
    candles = read_public(args.origin, "/api/v1/market/candles?inst_id=BTC-USDT-SWAP&bar=15m&limit=100")
    overview = read_public(args.origin, "/api/v1/market/overview?inst_id=BTC-USDT-SWAP")
    ticker = stream["tickers"]["BTC-USDT-SWAP"]["data"]
    context = {
        "inst_id": "BTC-USDT-SWAP", "bar": "15m", "captured_at": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker, "candles": candles["data"], "candle_count": len(candles["data"]),
        "funding_rate": overview.get("funding_rate", {}),
        "open_interest": overview.get("open_interest", {}), "errors": [],
    }
    analysis = await adapter.analyze("BTC-USDT-SWAP", context)
    expected = {"market_report", "sentiment_report", "news_report", "investment_plan",
                "trader_investment_plan", "final_trade_decision"}
    present = {key for key in expected if analysis["state"].get(key)}
    assert present == expected, f"Missing reports: {expected - present}"
    assert analysis["decision"] == "Hold"
    assert analysis["signal"] == {} and analysis["execution_authorized"] is False
    assert len(ProviderFixture.calls) >= 10, "Graph did not traverse all decision stages"
    assert all(call["bounded_tokens"] for call in ProviderFixture.calls)
    assert sum(call["has_okx_evidence"] for call in ProviderFixture.calls) >= 6
    assert {"ResearchPlan", "TraderProposal", "PortfolioDecision"} <= {
        call["schema"] for call in ProviderFixture.calls
    }, "Structured decisions fell back to text; schema integration is unverified"
    return {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "scope": "real TradingAgents graph and SDK, local model protocol fixture, public OKX snapshot",
        "live_model_verified": False, "exchange_orders_submitted": False,
        "ready": ready, "model_calls": len(ProviderFixture.calls),
        "structured_schemas": sorted({call["schema"] for call in ProviderFixture.calls if call["schema"]}),
        "calls_with_okx_evidence": sum(call["has_okx_evidence"] for call in ProviderFixture.calls),
        "reports": sorted(present), "decision": analysis["decision"],
        "execution_authorized": analysis["execution_authorized"],
        "candle_count": context["candle_count"],
        "notes": "Upstream may perform a public Yahoo instrument identity lookup. No external model or trade API is called.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "work/upstream-tradingagents")
    parser.add_argument("--python", default=str(ROOT / "work/ai-venv/bin/python"))
    parser.add_argument("--origin", default="http://127.0.0.1:8099")
    args = parser.parse_args()
    ProviderFixture.calls = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderFixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="ai-smoke-", dir=ROOT / "work") as directory:
            report = asyncio.run(verify(args, server.server_port, directory))
        destination = ROOT / "outputs/ai-framework-verification.json"
        destination.parent.mkdir(exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
