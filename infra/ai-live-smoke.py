"""Read-only TradingAgents provider probe using a live public OKX snapshot."""

import argparse
import asyncio
import json
import math
import os
import re
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path.cwd() if __file__ in {"<stdin>", "-"} else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))

from app.ai_analysis import AIAnalysisError, TradingAgentsAdapter  # noqa: E402
from app.okx_market import OkxMarketClient, OkxMarketError  # noqa: E402


def write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


async def public_context(client: OkxMarketClient, inst_id: str, bar: str, limit: int) -> dict:
    values = await asyncio.gather(
        client.ticker(inst_id),
        client.candles(inst_id, bar, limit),
        client.funding_rate(inst_id),
        client.open_interest(inst_id),
    )
    ticker, candles, funding_rate, open_interest = values
    if ticker.get("instId") != inst_id:
        raise RuntimeError("ticker_instrument_mismatch")
    try:
        price = float(ticker.get("last", "0"))
    except (TypeError, ValueError, OverflowError):
        raise RuntimeError("ticker_price_invalid") from None
    if not math.isfinite(price) or price <= 0:
        raise RuntimeError("ticker_price_invalid")
    if not isinstance(candles, list) or len(candles) < 30 or any(
        not isinstance(row, list) or len(row) < 6 for row in candles
    ):
        raise RuntimeError("candle_evidence_incomplete")
    if not isinstance(funding_rate, dict) or not isinstance(open_interest, dict):
        raise RuntimeError("public_context_incomplete")
    return {
        "inst_id": inst_id,
        "bar": bar,
        "requested_limit": limit,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "candles": candles,
        "candle_count": len(candles),
        "funding_rate": funding_rate,
        "open_interest": open_interest,
        "errors": [],
    }


async def run_probe(
    *, timeout: float, inst_id: str, bar: str, limit: int,
    run_mode: str | None = None,
) -> dict:
    started = time.monotonic()
    previous_run_mode = os.environ.get("TRADINGAGENTS_RUN_MODE")
    if run_mode is not None:
        os.environ["TRADINGAGENTS_RUN_MODE"] = run_mode
    try:
        adapter = TradingAgentsAdapter()
    finally:
        if run_mode is not None:
            if previous_run_mode is None:
                os.environ.pop("TRADINGAGENTS_RUN_MODE", None)
            else:
                os.environ["TRADINGAGENTS_RUN_MODE"] = previous_run_mode
    client = OkxMarketClient()
    if not adapter.configured:
        raise RuntimeError(adapter.configuration_error or "tradingagents_not_configured")
    async with asyncio.timeout(timeout):
        context = await public_context(client, inst_id, bar, limit)
        result = await adapter.analyze(inst_id, market_context=context)
    if (
        result.get("inst_id") != inst_id
        or result.get("source") != "TradingAgents"
        or not isinstance(result.get("state"), dict)
        or not result.get("state")
        or result.get("signal") != {}
        or result.get("execution_authorized") is not False
    ):
        raise RuntimeError("analysis_result_invalid_or_executable")
    decision = result.get("decision")
    if decision is None or (isinstance(decision, str) and not decision.strip()):
        raise RuntimeError("analysis_decision_empty")
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "tradingagents_real_model_read_only",
        "instrument": inst_id,
        "bar": bar,
        "mode": result.get("mode", adapter.run_mode),
        "provider": os.getenv("TRADINGAGENTS_LLM_PROVIDER", ""),
        "market_evidence": {
            "ticker_received": True,
            "candles_received": len(context["candles"]),
            "funding_rate_received": True,
            "open_interest_received": True,
            "errors": [],
        },
        "analysis": {
            "decision_type": type(decision).__name__,
            "decision_nonempty": True,
            "state_keys": sorted(result["state"].keys()),
        },
        "provider_connection_verified": True,
        "execution_authorized": False,
        "private_account_verified": False,
        "trading_performed": False,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--inst-id", default="BTC-USDT-SWAP")
    parser.add_argument("--bar", default="15m")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--run-mode", choices=("fast", "full"), default=None)
    parser.add_argument("--output", type=Path, default=ROOT / "outputs/ai-verification.json")
    args = parser.parse_args()
    if not 30 <= args.timeout <= 1800:
        parser.error("--timeout must be between 30 and 1800 seconds")
    if not re.fullmatch(r"[A-Z0-9]{2,20}-(?:USDT|USDC|USD)-SWAP", args.inst_id):
        parser.error("--inst-id must be an OKX perpetual instrument")
    if not re.fullmatch(r"[0-9]+[mHhDWMw]", args.bar):
        parser.error("--bar must be an OKX candle period")
    if not 30 <= args.limit <= 300:
        parser.error("--limit must be between 30 and 300")
    destination = None if args.output == Path("-") else args.output
    if destination is not None:
        destination.unlink(missing_ok=True)
    try:
        result = asyncio.run(run_probe(
            timeout=args.timeout, inst_id=args.inst_id, bar=args.bar, limit=args.limit,
            run_mode=args.run_mode,
        ))
        if destination is not None:
            write_private_json(destination, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (AIAnalysisError, OkxMarketError, OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(f"AI live smoke failed: {type(error).__name__}.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
