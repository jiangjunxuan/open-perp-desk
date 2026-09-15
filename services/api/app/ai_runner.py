"""Private stdin/stdout protocol for a disposable TradingAgents process."""

import copy
import json
import os
import signal
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _tradingagents_ticker(inst_id: str) -> str:
    return f"{inst_id.split('-', 1)[0].upper()}-USD"


def _guard_parent(parent_pid: int, timeout: int) -> None:
    deadline = time.monotonic() + timeout + 3
    while True:
        if os.getppid() != parent_pid or time.monotonic() > deadline:
            if os.getpgrp() == os.getpid():
                os.killpg(os.getpid(), signal.SIGKILL)
            os._exit(124)
        time.sleep(.5)


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, default=str, allow_nan=False))


def _market_context_prompt(context: dict[str, Any]) -> str:
    evidence = {
        key: context.get(key) for key in (
            "inst_id", "bar", "candle_count", "captured_at", "stream",
            "ticker", "funding_rate", "open_interest", "errors",
        )
    }
    evidence["recent_candles"] = (context.get("candles") or [])[:20]
    return (
        "\n\nExternal OKX perpetual market evidence (public snapshot):\n"
        + json.dumps(_json_safe(evidence), ensure_ascii=True)
        + "\nThis research concerns the named OKX perpetual contract, not a spot order. "
        "Respect the snapshot timestamp and interval; report missing or stale evidence. "
        "Any upstream USD spot/daily data is supplemental, not an OKX execution price. "
        "Reports are research only and cannot authorize or submit an order."
    )


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    source = Path(request["source_path"]).resolve()
    sys.path.insert(0, str(source))
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(request["config"])
    config["checkpoint_enabled"] = False
    for key in ("results_dir", "data_cache_dir"):
        Path(config[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(config["memory_log_path"]).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    graph = TradingAgentsGraph(
        selected_analysts=("market", "social", "news"), debug=False, config=config,
    )
    if request["action"] == "probe":
        return {
            "runtime_state": "ready",
            "provider": config["llm_provider"],
            "deep_model": config["deep_think_llm"],
            "quick_model": config["quick_think_llm"],
            "provider_connection_verified": False,
            "execution_authorized": False,
        }
    inst_id = request["inst_id"]
    ticker = _tradingagents_ticker(inst_id)
    context = request.get("market_context") or {}
    evidence = _market_context_prompt(context)
    resolver = graph.resolve_instrument_context

    def resolve_with_okx_context(symbol: str, asset_type: str = "stock") -> str:
        return f"{resolver(symbol, asset_type)}{evidence}"

    graph.resolve_instrument_context = resolve_with_okx_context
    state, decision = graph.propagate(
        ticker, datetime.now(timezone.utc).date().isoformat(), asset_type="crypto",
    )
    return {
        "inst_id": inst_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "market_context": _json_safe(context),
        "decision": _json_safe(decision),
        "state": _json_safe(state),
    }


def main() -> None:
    os.umask(0o077)
    try:
        request = json.loads(sys.stdin.buffer.read(256 * 1024 + 1))
        threading.Thread(
            target=_guard_parent,
            args=(request["parent_pid"], request["timeout_seconds"]),
            daemon=True,
        ).start()
        # Third-party prints must never corrupt the result protocol.
        with redirect_stdout(sys.stderr):
            data = run_request(request)
        result = {"ok": True, "data": data}
    except ImportError:
        result = {"ok": False, "code": "dependencies_missing"}
    except (ValueError, KeyError, TypeError):
        result = {"ok": False, "code": "provider_configuration"}
    except Exception:
        # Provider exceptions may contain credentials, URLs or full request bodies.
        result = {"ok": False, "code": "runtime_failed"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
