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


def _install_data_guards(timeout_seconds: int) -> None:
    """Bound yfinance transport calls inside the disposable research process.

    TradingAgents uses yfinance for identity, price history, and news. Its
    public API defaults to a 30-second request timeout and may retry several
    endpoints, which can make a crypto research run spend minutes waiting on a
    vendor that is unavailable from the server. Keep the upstream package
    untouched and cap only this child process; callers still receive the
    upstream error text and can continue with the other evidence sources.
    """
    try:
        import yfinance as yf
        from yfinance.data import YfData
    except ImportError:
        return

    cap = max(1.0, float(timeout_seconds))

    def cap_timeout(value: Any) -> float:
        try:
            requested = float(value)
        except (TypeError, ValueError):
            requested = cap
        if requested <= 0:
            requested = cap
        return min(requested, getattr(YfData, "_openperpdesk_data_timeout", cap))

    original = getattr(YfData, "_openperpdesk_original_make_request", None)
    if original is None:
        original = YfData._make_request

        def bounded_make_request(
            self,
            url,
            request_method,
            body=None,
            params=None,
            timeout=30,
            data=None,
        ):
            return original(
                self,
                url,
                request_method,
                body=body,
                params=params,
                timeout=cap_timeout(timeout),
                data=data,
            )

        YfData._openperpdesk_original_make_request = original
        YfData._make_request = bounded_make_request

    def wrap_timeout_method(name: str, timeout_position: int = 0) -> None:
        """Cap yfinance helpers that call the session directly."""
        original_method = getattr(YfData, name, None)
        marker = f"_openperpdesk_original{name}"
        if original_method is None or getattr(YfData, marker, None) is not None:
            return

        def bounded_method(self, *args, **kwargs):
            positional = list(args)
            if "timeout" in kwargs:
                kwargs["timeout"] = cap_timeout(kwargs["timeout"])
            elif len(positional) > timeout_position:
                positional[timeout_position] = cap_timeout(positional[timeout_position])
            else:
                kwargs["timeout"] = cap
            return original_method(self, *positional, **kwargs)

        setattr(YfData, marker, original_method)
        setattr(YfData, name, bounded_method)

    # Cookie and crumb negotiation otherwise keeps its own 30-second default,
    # bypassing _make_request when Yahoo is unreachable.
    for method_name in (
        "_get_cookie_and_crumb",
        "_get_cookie_and_crumb_basic",
        "_get_cookie_basic",
        "_get_cookie_csrf",
        "_get_crumb_basic",
        "_get_crumb_csrf",
    ):
        wrap_timeout_method(method_name)
    wrap_timeout_method("_accept_consent_form", timeout_position=1)

    YfData._openperpdesk_data_timeout = cap
    # The adapter already bounds the whole child process. Avoid yfinance's
    # independent retry budget multiplying the per-request wait.
    try:
        yf.config.network.retries = 0
    except (AttributeError, TypeError):
        pass


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
    _install_data_guards(int(request.get("data_timeout_seconds", 8)))
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
