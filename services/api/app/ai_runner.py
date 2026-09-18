"""Private stdin/stdout protocol for a disposable TradingAgents process."""

import copy
import json
import math
import os
import re
import signal
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


_TRANSIENT_PROVIDER_STATUS_CODES = {408, 409, 425, 429}


def _status_code_from_exception(error: BaseException) -> int | None:
    """Find a provider HTTP status without serializing the provider error."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        for owner in (current, getattr(current, "response", None)):
            if owner is None:
                continue
            for name in ("status_code", "status"):
                value = getattr(owner, name, None)
                try:
                    code = int(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if 100 <= code <= 599:
                    return code
        current = current.__cause__ or current.__context__
    return None


def _is_transient_provider_error(error: BaseException) -> bool:
    status = _status_code_from_exception(error)
    return status in _TRANSIENT_PROVIDER_STATUS_CODES or (status is not None and 500 <= status <= 599)


def _transient_retry_count(config: dict[str, Any]) -> int:
    try:
        value = int(config.get("llm_max_retries", 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    return max(0, min(value, 3))


def _run_with_transient_retries(
    operation: Callable[[], Any], config: dict[str, Any], deadline: float,
    *, sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Retry only provider throttling/5xx failures within the process deadline."""
    retries = _transient_retry_count(config)
    attempt = 0
    while True:
        try:
            return operation()
        except Exception as error:
            if not _is_transient_provider_error(error) or attempt >= retries:
                raise
            attempt += 1
            delay = min(float(2 ** (attempt - 1)), 8.0)
            remaining = deadline - time.monotonic()
            if remaining <= delay:
                raise
            sleep(min(delay, max(0.0, remaining - 0.05)))


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


def _llm_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    """Mirror the upstream provider knobs without constructing the full graph."""
    provider = str(config.get("llm_provider") or "").lower()
    kwargs: dict[str, Any] = {}
    if provider == "google" and config.get("google_thinking_level"):
        kwargs["thinking_level"] = config["google_thinking_level"]
    elif provider == "openai" and config.get("openai_reasoning_effort"):
        kwargs["reasoning_effort"] = config["openai_reasoning_effort"]
    elif provider == "anthropic" and config.get("anthropic_effort"):
        kwargs["effort"] = config["anthropic_effort"]

    if config.get("temperature") not in (None, ""):
        kwargs["temperature"] = float(config["temperature"])
    if config.get("llm_max_retries") not in (None, ""):
        kwargs["max_retries"] = max(0, int(config["llm_max_retries"]))
    if config.get("max_tokens") not in (None, ""):
        kwargs["max_output_tokens" if provider == "google" else "max_tokens"] = int(
            config["max_tokens"]
        )
    return kwargs


def _response_text(response: Any) -> str:
    """Normalize LangChain content blocks to a bounded plain-text response."""
    content = getattr(response, "content", response)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                value = block.get("text") or block.get("content")
                if value is not None:
                    parts.append(str(value))
            else:
                value = getattr(block, "text", None) or getattr(block, "content", None)
                if value is not None:
                    parts.append(str(value))
        return "".join(parts)
    return str(content or "")


def _extract_json_object(text: str) -> dict[str, Any]:
    """Accept strict JSON plus the fenced/object forms models commonly emit."""
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL | re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start >= 0 and end > start:
            candidate = candidate[start:end + 1]
    try:
        value = json.loads(candidate)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _text(value: Any, maximum: int = 4000) -> str:
    if isinstance(value, str):
        return value.strip()[:maximum]
    if value is None:
        return ""
    return str(value).strip()[:maximum]


def _text_list(value: Any, maximum_items: int = 8) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [_text(item, 1000) for item in value if _text(item, 1000)][:maximum_items]
    item = _text(value, 1000)
    return [item] if item else []


def _normalize_fast_result(raw_text: str) -> dict[str, Any]:
    parsed = _extract_json_object(raw_text)
    aliases = {
        "buy": "Buy", "long": "Buy", "bullish": "Buy", "买入": "Buy", "偏多": "Buy",
        "sell": "Sell", "short": "Sell", "bearish": "Sell", "卖出": "Sell", "偏空": "Sell",
        "hold": "Hold", "neutral": "Hold", "观望": "Hold", "中性": "Hold",
    }
    decision = aliases.get(str(parsed.get("decision", "")).strip().lower(), "Hold")
    try:
        confidence = float(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = confidence if math.isfinite(confidence) else 0.0
    confidence = max(0.0, min(1.0, confidence))
    summary = _text(parsed.get("summary"))
    if not summary:
        summary = _text(raw_text, 4000) or "模型未返回可用研究摘要。"
    limitations = _text_list(parsed.get("limitations"))
    if not parsed:
        limitations.insert(0, "模型未按约定 JSON 格式返回，方向已降级为观望。")
    return {
        "decision": decision,
        "confidence": confidence,
        "summary": summary,
        "trend": _text(parsed.get("trend")),
        "evidence": _text_list(parsed.get("evidence")),
        "risks": _text_list(parsed.get("risks")),
        "invalidations": _text_list(parsed.get("invalidations")),
        "time_horizon": _text(parsed.get("time_horizon"), 200),
        "limitations": limitations,
        "raw_response": _text(raw_text, 12000),
    }


def _fast_market_evidence(context: dict[str, Any]) -> str:
    evidence = {
        key: context.get(key) for key in (
            "inst_id", "bar", "candle_count", "captured_at", "stream",
            "ticker", "funding_rate", "open_interest", "errors",
        )
    }
    candles = context.get("candles")
    evidence["recent_candles"] = candles[-60:] if isinstance(candles, list) else []
    return json.dumps(_json_safe(evidence), ensure_ascii=True)


def _run_fast_research(
    request: dict[str, Any], config: dict[str, Any], context: dict[str, Any],
    deadline: float,
) -> dict[str, Any]:
    """Run one bounded OKX-evidence call instead of the multi-agent debate graph."""
    source = Path(request["source_path"]).resolve()
    sys.path.insert(0, str(source))
    from tradingagents.llm_clients import create_llm_client

    model = str(config.get("quick_think_llm") or config.get("deep_think_llm") or "").strip()
    if not model:
        raise ValueError("quick_think_llm is not configured")
    client = create_llm_client(
        provider=str(config.get("llm_provider") or ""),
        model=model,
        base_url=config.get("backend_url"),
        **_llm_kwargs(config),
    )
    prompt = (
        "You are the fast research analyst for an OKX perpetual-contract dashboard.\n"
        "Use only the supplied OKX snapshot. Do not browse, call tools, infer missing prices, "
        "or claim news/sentiment that is not present. This is research only: it cannot place "
        "orders, set leverage, or authorize execution.\n\n"
        "Return ONLY one JSON object with these keys: decision (Buy, Sell, or Hold), "
        "confidence (0 to 1), summary, trend, evidence (array), risks (array), "
        "invalidations (array), time_horizon, limitations (array). Prefer Hold when data is "
        "stale, incomplete, contradictory, or insufficient. Write values in the requested "
        f"language: {config.get('output_language', 'Chinese')}.\n\n"
        "OKX snapshot:\n" + _fast_market_evidence(context)
    )
    llm = client.get_llm()
    response = _run_with_transient_retries(
        lambda: llm.invoke(prompt), config, deadline,
    )
    normalized = _normalize_fast_result(_response_text(response))
    decision = normalized["decision"]
    summary = normalized["summary"]
    limitations = normalized["limitations"] or [
        "研究结果仅供参考，不构成委托或投资建议。",
        "新闻、社交和宏观数据未在本次 OKX 快速研究中接入。",
    ]
    state = {
        "market_report": summary,
        "sentiment_report": "快速模式未接入社交情绪数据，不能据此推断市场情绪。",
        "news_report": "快速模式未接入新闻、宏观或基本面数据。",
        "investment_plan": {
            "decision": decision,
            "confidence": normalized["confidence"],
            "trend": normalized["trend"],
            "evidence": normalized["evidence"],
            "risks": normalized["risks"],
            "invalidations": normalized["invalidations"],
            "time_horizon": normalized["time_horizon"],
        },
        "trader_investment_plan": (
            "快速研究结果不可执行；请使用结构化策略、风控和人工闸门完成任何后续操作。"
        ),
        "final_trade_decision": f"研究结论（不可执行）：{decision}\n\n{summary}",
        "fast_research": normalized,
        "limitations": limitations,
    }
    return {
        "inst_id": request["inst_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "mode": "fast",
        "market_context": _json_safe(context),
        "decision": decision,
        "state": _json_safe(state),
    }


def run_request(request: dict[str, Any]) -> dict[str, Any]:
    source = Path(request["source_path"]).resolve()
    _install_data_guards(int(request.get("data_timeout_seconds", 8)))
    # The runner is launched as an isolated script, so make its sibling bridge
    # importable without inheriting the parent API process environment.
    sys.path.insert(0, str(Path(__file__).parent))
    sys.path.insert(0, str(source))
    from tradingagents.default_config import DEFAULT_CONFIG

    config = copy.deepcopy(DEFAULT_CONFIG)
    config.update(request["config"])
    config["checkpoint_enabled"] = False
    context = request.get("market_context") or {}
    run_mode = str(request.get("run_mode") or "full").strip().lower()
    if run_mode not in {"fast", "full"}:
        raise ValueError("unsupported TradingAgents run mode")
    deadline = time.monotonic() + max(1.0, float(request.get("timeout_seconds", 300)))
    if run_mode == "fast" and request["action"] == "analyze":
        return _run_fast_research(request, config, context, deadline)
    from tradingagents_okx_bridge import install_okx_bridge
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    install_okx_bridge(context, config)
    for key in ("results_dir", "data_cache_dir"):
        Path(config[key]).mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(config["memory_log_path"]).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if request["action"] == "probe":
        graph = TradingAgentsGraph(
            selected_analysts=("market", "social", "news"), debug=False, config=config,
        )
        return {
            "runtime_state": "ready",
            "provider": config["llm_provider"],
            "deep_model": config["deep_think_llm"],
            "quick_model": config["quick_think_llm"],
            "run_mode": run_mode,
            "provider_connection_verified": False,
            "execution_authorized": False,
        }
    inst_id = request["inst_id"]
    ticker = _tradingagents_ticker(inst_id)
    evidence = _market_context_prompt(context)

    def propagate_once() -> tuple[Any, Any]:
        # Build a fresh graph after a transient provider error so a partially
        # advanced LangGraph state is never reused for a retry.
        graph = TradingAgentsGraph(
            selected_analysts=("market", "social", "news"), debug=False, config=config,
        )
        resolver = graph.resolve_instrument_context

        def resolve_with_okx_context(symbol: str, asset_type: str = "stock") -> str:
            return f"{resolver(symbol, asset_type)}{evidence}"

        graph.resolve_instrument_context = resolve_with_okx_context
        return graph.propagate(
            ticker, datetime.now(timezone.utc).date().isoformat(), asset_type="crypto",
        )

    state, decision = _run_with_transient_retries(propagate_once, config, deadline)
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
    except Exception as error:
        # Provider exceptions may contain credentials, URLs or full request bodies.
        result = {
            "ok": False,
            "code": "provider_unavailable" if _is_transient_provider_error(error) else "runtime_failed",
        }
    sys.stdout.write(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
