import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Path as RoutePath, Query
from pydantic import BaseModel, ConfigDict, Field
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse

from .okx_account import OkxAccountClient, OkxAccountError
from .okx_algo_stream import OkxAlgoOrderStream
from .okx_account_stream import OkxAccountStream
from .okx_market import OkxMarketClient, OkxMarketError
from .okx_market_stream import OkxMarketStream
from .okx_trade import OkxTradeClient, OkxTradeError, OrderRequest
from .pushplus import PushPlusClient, PushPlusError
from .risk_engine import RiskEngine
from .execution_engine import ExecutionEngine
from .order_preflight import OrderPreflight
from .position_lots import positions_with_lots
from .protection_handoff import handoff_summaries
from .protection_incident import incident_summaries
from .ai_analysis import AIAnalysisError, TradingAgentsAdapter
from .account_sync import AccountSynchronizer
from .account_history import AccountHistoryImporter
from .account_valuation import AccountValuationWorker
from .account_performance import AccountPerformanceWorker
from .quarterly_history import QuarterlyHistoryImporter
from .historical_ledger import archive_first_day, history_days
from .account_reconciler import AccountReconciler
from .equity_baseline import EquityBaselineSampler
from .automation_worker import AutomationWorker
from .backtest import BacktestEngine
from .state_store import BillImportBusy, StateStore
from .safety_control import SafetyController
from .strategy_engine import StrategyEngine
from .trading_signal import TradeSignal
from .realtime import control_events, private_events


def _symbols() -> list[str]:
    raw = os.getenv("MARKET_SYMBOLS", "BTC-USDT-SWAP,ETH-USDT-SWAP")
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


market_client = OkxMarketClient()
market_stream = OkxMarketStream(_symbols())
account_client = OkxAccountClient()
account_stream = OkxAccountStream()
algo_stream = OkxAlgoOrderStream()
pushplus_client = PushPlusClient()
risk_engine = RiskEngine()
trade_client = OkxTradeClient()
state_store = StateStore()
account_history = AccountHistoryImporter(state_store, account_client)
quarterly_history = QuarterlyHistoryImporter(state_store, account_client)
account_valuation = AccountValuationWorker(state_store, account_client, market_client)
equity_baseline = EquityBaselineSampler(state_store, account_client)
account_performance = AccountPerformanceWorker(state_store, account_client, market_client)
safety_controller = SafetyController(state_store)
strategy_engine = StrategyEngine()
backtest_engine = BacktestEngine(strategy_engine)
tradingagents = TradingAgentsAdapter()
execution_engine = ExecutionEngine(
    state_store,
    risk_engine,
    trade_client,
    pushplus_client,
    safety_controller,
    preflight=OrderPreflight(account_client, market_client, state_store),
)
account_sync = AccountSynchronizer(
    state_store,
    account_client,
    account_stream,
    algo_stream,
    notifier=execution_engine.notify_event,
)
account_reconciler = AccountReconciler(
    account_sync,
    state_store,
    notifier=execution_engine.notify_event,
)
automation_worker = AutomationWorker(
    market_client,
    account_client,
    account_sync,
    strategy_engine,
    execution_engine,
    risk_engine,
    state_store,
    safety_controller,
    market_data_fresh=lambda: market_stream.fresh,
    notifier=execution_engine.notify_event,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    account_stream.on_update = account_reconciler.notify_stream
    algo_stream.on_update = account_reconciler.notify_stream
    await market_stream.start()
    await account_stream.start()
    await algo_stream.start()
    await account_reconciler.start()
    await equity_baseline.start()
    await quarterly_history.start()
    await account_valuation.start()
    await account_performance.start()
    await automation_worker.start()
    try:
        yield
    finally:
        await equity_baseline.stop()
        await account_performance.close()
        await account_valuation.close()
        await quarterly_history.close()
        await account_history.close()
        await tradingagents.close()
        await automation_worker.stop()
        await account_reconciler.stop()
        await account_stream.stop()
        await algo_stream.stop()
        await market_stream.stop()


app = FastAPI(
    title="OpenPerpDesk API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)


def _is_configured(name: str) -> bool:
    return bool(os.getenv(name, "").strip())


def require_admin_token(x_admin_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("ADMIN_API_TOKEN", "").strip()
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Private account API is locked until ADMIN_API_TOKEN is configured.",
        )
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(status_code=401, detail="Invalid admin token.")


@app.get("/api/v1/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "api",
        "time": datetime.now(timezone.utc).isoformat(),
    }


def _readiness_payload() -> dict[str, object]:
    market_fresh = market_stream.fresh
    state_store_ready = state_store.path.exists()
    checks = {
        "state_store": {
            "ok": state_store_ready,
        },
        "market_stream": {
            "ok": market_fresh,
            "connected": market_stream.connected,
            "fresh": market_fresh,
            "last_message_at": market_stream.last_message_at,
            "last_error": market_stream.last_error,
        },
        "risk_engine": {"ok": True},
    }
    ready = all(bool(check["ok"]) for check in checks.values())
    return {
        "status": "ready" if ready else "degraded",
        "ready": ready,
        "service": "api",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


@app.get("/api/v1/health/readiness")
def readiness() -> dict[str, object]:
    payload = _readiness_payload()
    if not payload["ready"]:
        raise HTTPException(status_code=503, detail=payload)
    return payload


@app.get("/api/v1/health/metrics")
def health_metrics() -> dict[str, object]:
    """Return non-secret operational gauges for external monitoring."""
    return {
        "service": "OpenPerpDesk",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "market_stream": {
            "connected": market_stream.connected,
            "fresh": market_stream.fresh,
            "last_message_at": market_stream.last_message_at,
            "last_error": market_stream.last_error,
        },
        "account_stream": {
            "configured": account_stream.configured,
            "connected": account_stream.connected,
            "authenticated": account_stream.authenticated,
            "last_message_at": account_stream.last_message_at,
            "last_error": account_stream.last_error,
        },
        "algo_stream": {
            "configured": algo_stream.configured,
            "connected": algo_stream.connected,
            "authenticated": algo_stream.authenticated,
            "last_message_at": algo_stream.last_message_at,
            "last_error": algo_stream.last_error,
        },
        "automation_worker": automation_worker.snapshot(),
        "account_reconciler": account_reconciler.snapshot(),
        "equity_baseline": equity_baseline.snapshot(),
        "quarterly_history": quarterly_history.snapshot(),
        "execution": {
            "enabled": trade_client.enabled,
            "live_allowed": trade_client.live_gate.allowed,
        },
        "safety": safety_controller.snapshot(),
    }


@app.get("/api/v1/system/status")
def system_status() -> dict[str, object]:
    trading_mode = os.getenv("TRADING_MODE", "demo").lower()
    return {
        "service": "OpenPerpDesk",
        "environment": os.getenv("APP_ENV", "development"),
        "trading_mode": trading_mode,
        "execution_enabled": trade_client.enabled,
        "market_data_connected": market_stream.fresh,
        "risk_engine_ready": True,
        "risk_limits": {
            "max_leverage": risk_engine.limits.max_leverage,
            "max_position_pct": risk_engine.limits.max_position_pct,
            "max_total_notional_pct": risk_engine.limits.max_total_notional_pct,
            "min_confidence": risk_engine.limits.min_confidence,
            "max_daily_loss_pct": risk_engine.limits.max_daily_loss_pct,
            "max_stop_distance_pct": risk_engine.limits.max_stop_distance_pct,
        },
        "state_store_ready": state_store.path.exists(),
        "integrations": {
            "okx_credentials_configured": all(
                _is_configured(name)
                for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")
            ),
            "outbound_proxy_configured": market_stream.proxy_url is not None,
            "pushplus_configured": pushplus_client.configured,
            "account_readonly_configured": account_client.configured,
            "account_stream_configured": account_stream.configured,
            "tradingagents_configured": tradingagents.configured,
        },
        "market_stream": {
            "connected": market_stream.connected,
            "fresh": market_stream.fresh,
            "last_message_at": market_stream.last_message_at,
            "last_error": market_stream.last_error,
            "candles_connected": market_stream.candles_connected,
            "candles_fresh": market_stream.candles_fresh,
            "candles_last_message_at": market_stream.candles_last_message_at,
            "candles_last_error": market_stream.candles_last_error,
        },
        "account_stream": {
            "configured": account_stream.configured,
            "connected": account_stream.connected,
            "authenticated": account_stream.authenticated,
            "last_message_at": account_stream.last_message_at,
            "last_error": account_stream.last_error,
        },
        "algo_stream": {
            "configured": algo_stream.configured,
            "connected": algo_stream.connected,
            "authenticated": algo_stream.authenticated,
            "last_message_at": algo_stream.last_message_at,
            "last_error": algo_stream.last_error,
        },
        "state_store": {"ok": state_store.path.exists()},
        "automation_worker": automation_worker.snapshot(),
        "account_reconciler": account_reconciler.snapshot(),
        "live_safety": trade_client.live_gate.snapshot(),
        "quarterly_history": quarterly_history.snapshot(),
        "safety_control": safety_controller.snapshot(),
        "safety": {
            "live_orders_allowed": trade_client.live_gate.allowed,
            "reason": (
                "Live execution requires the independent safety gate; it is "
                "locked by default."
            ),
        },
    }


def _market_error(exc: OkxMarketError) -> HTTPException:
    return HTTPException(status_code=502, detail=str(exc))


@app.get("/api/v1/market/instruments")
async def market_instruments(inst_id: str | None = None) -> dict[str, object]:
    try:
        return {"data": await market_client.instruments(inst_id)}
    except OkxMarketError as exc:
        raise _market_error(exc) from exc


@app.get("/api/v1/market/ticker")
async def market_ticker(
    inst_id: str = Query(default="BTC-USDT-SWAP", min_length=3, max_length=40),
) -> dict[str, object]:
    try:
        return {"data": await market_client.ticker(inst_id)}
    except OkxMarketError as exc:
        raise _market_error(exc) from exc


@app.get("/api/v1/market/candles")
async def market_candles(
    inst_id: str = Query(default="BTC-USDT-SWAP", min_length=3, max_length=40),
    bar: str = Query(default="15m", pattern=r"^[0-9]+[mHhDWMw]$"),
    limit: int = Query(default=100, ge=1, le=300),
) -> dict[str, object]:
    try:
        return {"data": await market_client.candles(inst_id, bar, limit)}
    except OkxMarketError as exc:
        raise _market_error(exc) from exc


@app.get("/api/v1/market/overview")
async def market_overview(
    inst_id: str = Query(default="BTC-USDT-SWAP", min_length=3, max_length=40),
) -> dict[str, object]:
    try:
        return {
            "instrument": inst_id,
            "ticker": await market_client.ticker(inst_id),
            "funding_rate": await market_client.funding_rate(inst_id),
            "open_interest": await market_client.open_interest(inst_id),
        }
    except OkxMarketError as exc:
        raise _market_error(exc) from exc


@app.get("/api/v1/market/stream")
def market_stream_snapshot() -> dict[str, Any]:
    return market_stream.snapshot()


@app.get("/api/v1/market/events")
async def market_events(
    bar: Literal["1m", "15m", "1H", "4H"] = "15m",
) -> StreamingResponse:
    return StreamingResponse(
        market_stream.events(bar),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/system/events")
async def system_events() -> StreamingResponse:
    return StreamingResponse(
        control_events(system_status, analysis_status),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/account/events")
async def account_events(
    x_admin_token: str | None = Header(default=None),
    _: None = Depends(require_admin_token),
) -> StreamingResponse:
    def authorized() -> bool:
        expected = os.getenv("ADMIN_API_TOKEN", "").strip()
        return bool(expected and x_admin_token and secrets.compare_digest(x_admin_token, expected))

    return StreamingResponse(
        private_events(state_store, account_stream, account_client, authorized, lambda: market_client.rate_scope),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@app.get("/api/v1/account/overview")
async def account_overview(
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    if not account_client.configured:
        return {
            "configured": False,
            "demo": account_client.demo,
            "balance": [],
            "positions": [],
            "config": [],
            "reason": "OKX read-only credentials are not configured.",
        }
    results = await asyncio.gather(
        account_client.balance(),
        account_client.positions(),
        account_client.config(),
        return_exceptions=True,
    )
    names = ("balance", "positions", "config")
    response: dict[str, object] = {
        "configured": True,
        "demo": account_client.demo,
    }
    errors: dict[str, str] = {}
    for name, result in zip(names, results):
        if isinstance(result, Exception):
            response[name] = []
            errors[name] = type(result).__name__
        else:
            response[name] = result
    if len(errors) == len(names):
        raise HTTPException(
            status_code=502,
            detail="All OKX account endpoints failed.",
        )
    if errors:
        response["errors"] = errors
    return response


@app.get("/api/v1/account/stream")
def account_stream_snapshot(
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return account_stream.snapshot()


@app.post("/api/v1/account/sync")
async def sync_account(
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    try:
        stream_result = account_sync.sync_stream()
        rest_result = await account_sync.sync_rest()
    except OkxAccountError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    state_store.add_audit(
        "account_sync",
        "OKX account state synchronized",
        payload={"stream": stream_result, "rest": rest_result},
    )
    return {"stream": stream_result, "rest": rest_result}


@app.get("/api/v1/worker/status")
def worker_status() -> dict[str, object]:
    return automation_worker.snapshot()


class WorkerControlRequest(BaseModel):
    enabled: bool
    dry_run: bool | None = None


@app.post("/api/v1/worker/control")
async def control_worker(
    request: WorkerControlRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    if not request.enabled:
        automation_worker.enabled = False
        await automation_worker.stop()
        state_store.add_audit(
            "worker_disabled",
            "Automation worker disabled from the web control plane",
        )
        return automation_worker.snapshot()

    if safety_controller.emergency_stopped:
        raise HTTPException(
            status_code=423,
            detail="Automation worker cannot start while emergency stop is active.",
        )

    requested_dry_run = (
        automation_worker.dry_run
        if request.dry_run is None
        else request.dry_run
    )
    if not requested_dry_run:
        if (
            trade_client.trading_mode != "demo"
            or not trade_client.demo
            or not trade_client.enabled
        ):
            raise HTTPException(
                status_code=423,
                detail=(
                    "Non-dry-run Worker requires an enabled OKX Demo execution "
                    "client; live execution is not available from this control."
                ),
            )

    automation_worker.dry_run = requested_dry_run
    automation_worker.enabled = True
    await automation_worker.start()
    state_store.add_audit(
        "worker_enabled",
        "Automation worker enabled from the web control plane",
        payload={"dry_run": requested_dry_run},
    )
    return automation_worker.snapshot()


@app.post("/api/v1/account/reconcile")
async def reconcile_account(
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    if not account_reconciler.enabled:
        return {"ran": False, "reason": "OKX private credentials are not configured"}
    try:
        return await account_reconciler.run_once()
    except OkxAccountError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/api/v1/worker/run")
async def run_worker_once(
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    try:
        return await automation_worker.run_once()
    except Exception as exc:
        raise HTTPException(status_code=502, detail=type(exc).__name__) from exc


@app.get("/api/v1/positions")
def stored_positions(
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return {"data": positions_with_lots(state_store, account_scope=account_client.account_scope)}


@app.get("/api/v1/orders")
def stored_orders(
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return {"data": state_store.list_orders(limit)}


@app.get("/api/v1/protection/handoffs")
def stored_protection_handoffs(_: None = Depends(require_admin_token)) -> dict[str, object]:
    return {"data": handoff_summaries(state_store, account_client.account_scope)}


@app.get("/api/v1/protection/adjustments")
def stored_protection_adjustments(_: None = Depends(require_admin_token)) -> dict[str, object]:
    from .protection_adjustment import adjustment_summaries
    return {"data": adjustment_summaries(state_store, account_client.account_scope)}


@app.get("/api/v1/protection/incidents")
def stored_protection_incidents(_: None = Depends(require_admin_token)) -> dict[str, object]:
    return {"data": incident_summaries(state_store, account_client.account_scope)}


class ProtectionIncidentResolutionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolution: Literal["external_protection_verified", "position_closed"]
    note: str = Field(min_length=3, max_length=500)


@app.post("/api/v1/protection/incidents/{incident_id}/resolve")
async def resolve_protection_incident(
    request: ProtectionIncidentResolutionRequest,
    incident_id: str = RoutePath(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    incident = state_store.protection_incident(incident_id)
    if not incident or incident["account_scope"] != account_client.account_scope:
        raise HTTPException(status_code=404, detail="Protection incident not found.")
    if request.resolution == "position_closed":
        position = state_store.get_position(incident["position_key"]) if incident.get("position_key") else None
        if position and (position["status"] != "closed" or float(position["size"]) != 0):
            raise HTTPException(status_code=409, detail="Position is still open; cannot resolve as closed.")
    try:
        resolved = state_store.resolve_protection_incident(
            incident, resolution=request.resolution, note=request.note,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(status_code=409, detail="Protection incident changed; refresh before resolving.")
    state_store.add_audit(
        "protection_incident_resolved",
        "Protection incident was manually resolved",
        payload={
            "incident_id": incident_id, "inst_id": resolved["inst_id"],
            "resolution": request.resolution,
        },
    )
    await execution_engine.notify_event(
        "protection_incident_resolved",
        "OpenPerpDesk 保护事故已解除",
        f"{resolved['inst_id']} 的附带保护事故已由管理员复核解除：{request.resolution}。",
        payload={"incident_id": incident_id, "resolution": request.resolution},
        severity="warning",
    )
    return {"accepted": True, "data": incident_summaries(state_store, account_client.account_scope)}


@app.get("/api/v1/fills")
def stored_fills(
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return {"data": state_store.list_fills(limit)}


@app.get("/api/v1/account/bills")
def account_bills(
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    return {
        "configured": account_client.configured,
        **state_store.bill_snapshot(account_client.account_scope, limit),
    }


@app.get("/api/v1/performance/pnl")
def performance_pnl(
    limit: int = Query(default=500, ge=1, le=500),
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return {"data": state_store.pnl_summary(limit)}


class BillImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_day: date
    end_day: date


class BillArchiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int = Field(ge=2021, strict=True)
    quarter: Literal["Q1", "Q2", "Q3", "Q4"]
    retry: bool = False


@app.post("/api/v1/account/bills/archives", status_code=202)
async def request_bill_archive(
    request: BillArchiveRequest, _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    try:
        return {"job": await quarterly_history.request(request.year, request.quarter, retry=request.retry)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OkxAccountError as exc:
        raise HTTPException(status_code=503, detail="OKX read-only credentials are not configured") from exc


@app.get("/api/v1/account/bills/archives")
def bill_archives(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    return {"data": state_store.bill_archives(account_client.account_scope)}


@app.post("/api/v1/account/bills/archives/{job_id}/cancel")
def cancel_bill_archive(
    job_id: str = RoutePath(min_length=32, max_length=32, pattern=r"^[a-f0-9]+$"),
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    if not state_store.cancel_bill_archive(job_id, account_client.account_scope):
        raise HTTPException(status_code=404, detail="Bill archive not found")
    return {"data": state_store.bill_archives(account_client.account_scope)}


@app.post("/api/v1/account/bills/imports", status_code=202)
async def import_account_bills(
    request: BillImportRequest, _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    try:
        return {"job": await account_history.start(request.start_day, request.end_day)}
    except BillImportBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OkxAccountError as exc:
        raise HTTPException(status_code=503, detail="OKX read-only credentials are not configured") from exc


@app.get("/api/v1/account/bills/imports")
def latest_bill_import(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    return {"job": state_store.bill_import(account_client.account_scope)}


@app.get("/api/v1/account/bills/imports/{job_id}")
def bill_import_status(
    job_id: str = RoutePath(min_length=32, max_length=32, pattern=r"^[a-f0-9]+$"),
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    job = state_store.bill_import(account_client.account_scope, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Bill import not found")
    return {"job": job}


@app.get("/api/v1/account/bills/history")
def historical_account_bills(
    start_day: date, end_day: date,
    limit: int = Query(default=100, ge=1, le=500),
    before_ts: int | None = Query(default=None, gt=0),
    before_id: str | None = Query(default=None, min_length=1, max_length=128),
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    try:
        days = history_days(start_day, end_day, now=now)
        report = state_store.bill_history(
            account_client.account_scope, days, limit=limit, before_ts=before_ts, before_id=before_id,
            rate_scope=market_client.rate_scope,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"configured": account_client.configured, "archive_first_day": archive_first_day(now).isoformat(), **report}


@app.post("/api/v1/account/bills/valuation", status_code=202)
async def request_bill_valuation(
    request: BillImportRequest, _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    try:
        days = history_days(request.start_day, request.end_day, now=datetime.now(timezone.utc))
        job = await asyncio.to_thread(
            state_store.create_bill_valuation, account_client.account_scope, market_client.rate_scope, days,
        )
    except BillImportBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    account_valuation.notify()
    return {"job": job}


@app.post("/api/v1/account/performance/collect", status_code=202)
async def collect_account_performance(
    request: BillImportRequest, _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    if not account_client.configured:
        raise HTTPException(status_code=503, detail="OKX read-only credentials are not configured")
    now = datetime.now(timezone.utc)
    try:
        days = history_days(request.start_day, request.end_day, now=now, importing=True)
        result = await asyncio.to_thread(
            state_store.schedule_performance, account_client.account_scope, market_client.rate_scope,
            days, int(now.timestamp() * 1000), retry=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    account_performance.notify()
    return result


@app.post("/api/v1/account/performance/cancel")
async def cancel_account_performance(
    request: BillImportRequest, _: None = Depends(require_admin_token),
) -> dict[str, int]:
    try:
        days = history_days(request.start_day, request.end_day, now=datetime.now(timezone.utc))
        count = await asyncio.to_thread(
            state_store.cancel_performance, account_client.account_scope, market_client.rate_scope, days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"canceled": count}


@app.get("/api/v1/account/bills/valuation")
def latest_bill_valuation(_: None = Depends(require_admin_token)) -> dict[str, Any]:
    return {"job": state_store.bill_valuation(account_client.account_scope)}


@app.post("/api/v1/account/bills/valuation/{job_id}/cancel")
def cancel_bill_valuation(
    job_id: str = RoutePath(min_length=32, max_length=32, pattern=r"^[a-f0-9]+$"),
    _: None = Depends(require_admin_token),
) -> dict[str, Any]:
    if not state_store.cancel_bill_valuation(job_id, account_client.account_scope):
        raise HTTPException(status_code=404, detail="Valuation not found")
    return {"job": state_store.bill_valuation(account_client.account_scope)}


@app.get("/api/v1/performance/report")
def performance_report(
    initial_equity: float = Query(default=1000.0, gt=0),
    limit: int = Query(default=500, ge=1, le=500),
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    try:
        return {
            "data": state_store.performance_report(
                initial_equity=initial_equity,
                limit=limit,
            )
        }
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/v1/activity")
def activity(
    limit: int = Query(default=100, ge=1, le=500),
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return {"data": state_store.list_audit(limit)}


@app.get("/api/v1/execution/status")
def execution_status() -> dict[str, object]:
    return {
        "configured": trade_client.configured,
        "demo": trade_client.demo,
        "trading_mode": trade_client.trading_mode,
        "execution_enabled": trade_client.enabled,
        "live_execution_allowed": trade_client.live_gate.allowed,
        "proxy_configured": trade_client.proxy_url is not None,
        "live_safety": trade_client.live_gate.snapshot(),
    }


class LiveUnlockRequest(BaseModel):
    phrase: str = Field(min_length=1, max_length=200)


class SafetyReasonRequest(BaseModel):
    reason: str = Field(default="manual operator action", min_length=1, max_length=200)


@app.get("/api/v1/safety/status")
def safety_status() -> dict[str, object]:
    return safety_controller.snapshot()


@app.post("/api/v1/safety/emergency-stop")
async def emergency_stop(
    request: SafetyReasonRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    safety_controller.stop(request.reason)
    state_store.add_audit(
        "emergency_stop",
        "Emergency stop activated",
        severity="warning",
        payload={"reason": request.reason},
    )
    await execution_engine.notify_event(
        "emergency_stop",
        "OpenPerpDesk 已触发急停",
        f"新订单已停止放行：{request.reason}。",
        payload={"reason": request.reason},
        severity="warning",
    )
    return safety_controller.snapshot()


@app.post("/api/v1/safety/resume")
async def resume_trading(
    request: SafetyReasonRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    safety_controller.resume(request.reason)
    state_store.add_audit(
        "emergency_resume",
        "Emergency stop released",
        payload={"reason": request.reason},
    )
    await execution_engine.notify_event(
        "emergency_resume",
        "OpenPerpDesk 已恢复执行",
        f"急停闸门已解除：{request.reason}。",
        payload={"reason": request.reason},
    )
    return safety_controller.snapshot()


@app.post("/api/v1/safety/live/unlock")
async def unlock_live(
    request: LiveUnlockRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    unlocked = trade_client.live_gate.unlock(request.phrase)
    if not unlocked:
        raise HTTPException(status_code=403, detail="Live safety unlock rejected.")
    state_store.add_audit(
        "live_safety_unlocked",
        "Live safety gate unlocked in process memory",
        severity="warning",
    )
    # The gate remains process-local and is still fail-closed after restart.
    await execution_engine.notify_event(
        "live_safety_unlocked",
        "OpenPerpDesk 实盘闸门已解锁",
        "实盘安全闸门已在当前进程内人工解锁。",
        severity="warning",
    )
    return {"unlocked": True, "live_safety": trade_client.live_gate.snapshot()}


@app.post("/api/v1/safety/live/lock")
async def lock_live(
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    trade_client.live_gate.lock()
    state_store.add_audit("live_safety_locked", "Live safety gate locked")
    await execution_engine.notify_event(
        "live_safety_locked",
        "OpenPerpDesk 实盘闸门已锁定",
        "实盘安全闸门已锁定，后续实盘订单将被阻止。",
        severity="warning",
    )
    return {"unlocked": False, "live_safety": trade_client.live_gate.snapshot()}


class RiskEvaluateRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    signal: TradeSignal
    account_equity: float = Field(gt=0.0)
    daily_pnl_pct: float
    current_notional: float = Field(default=0.0, ge=0.0)


class AnalysisRequest(BaseModel):
    inst_id: str = Field(default="BTC-USDT-SWAP", min_length=9, max_length=40)
    bar: str = Field(default="15m", pattern=r"^[0-9]+[mHhDWMw]$")
    limit: int = Field(default=100, ge=20, le=300)
    strategy_id: str = Field(
        default="structured-technical",
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


class BacktestRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    inst_id: str = Field(default="BTC-USDT-SWAP", min_length=9, max_length=40)
    bar: str = Field(default="15m", pattern=r"^[0-9]+[mHhDWMw]$")
    limit: int = Field(default=300, ge=30, le=300)
    initial_equity: float = Field(default=1000, gt=0)
    fee_bps: float = Field(default=5, ge=0, le=100)
    strategy_id: str = Field(
        default="structured-technical",
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9_-]+$",
    )


def _strategy_config(strategy_id: str) -> dict[str, object]:
    record = state_store.get_strategy(strategy_id)
    if record:
        return record["config"]
    if strategy_id == "structured-technical":
        return strategy_engine.normalize_config()
    raise HTTPException(status_code=404, detail="Strategy not found.")


@app.get("/api/v1/analysis/status")
def analysis_status() -> dict[str, object]:
    return {
        "structured_strategy": {"available": True, "source": "structured-technical"},
        "tradingagents": tradingagents.status(),
    }


@app.post("/api/v1/analysis/ai/check")
async def check_ai_runtime(_: None = Depends(require_admin_token)) -> dict[str, object]:
    try:
        return {"data": await tradingagents.check_ready()}
    except AIAnalysisError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


async def _ai_market_context(
    inst_id: str,
    bar: str,
    limit: int,
) -> dict[str, object]:
    """Capture public OKX evidence alongside an optional AI research run."""
    results = await asyncio.gather(
        market_client.ticker(inst_id),
        market_client.candles(inst_id, bar, limit),
        market_client.funding_rate(inst_id),
        market_client.open_interest(inst_id),
        return_exceptions=True,
    )
    names = ("ticker", "candles", "funding_rate", "open_interest")
    context_errors: list[str] = []
    context: dict[str, object] = {
        "inst_id": inst_id,
        "bar": bar,
        "requested_limit": limit,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "stream": {
            "connected": market_stream.connected,
            "fresh": market_stream.fresh,
            "last_message_at": market_stream.last_message_at,
        },
        "errors": context_errors,
    }
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            context_errors.append(name)
            continue
        context[name] = result
    context["candle_count"] = len(context.get("candles") or [])
    return context


@app.post("/api/v1/backtest")
async def run_backtest(
    request: BacktestRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    try:
        candles = await market_client.candles(
            request.inst_id,
            request.bar,
            request.limit,
        )
        result = backtest_engine.run(
            request.inst_id,
            candles,
            initial_equity=request.initial_equity,
            fee_bps=request.fee_bps,
            strategy_config=_strategy_config(request.strategy_id),
        )
    except (OkxMarketError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    state_store.add_audit(
        "backtest_completed",
        "Historical strategy replay completed",
        payload={
            "inst_id": request.inst_id,
            "trades": result["trades"],
            "return_pct": result["return_pct"],
        },
    )
    return {"data": result}


@app.post("/api/v1/analysis")
async def run_analysis(
    request: AnalysisRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    try:
        candles = await market_client.candles(
            request.inst_id,
            request.bar,
            request.limit,
        )
        analysis = strategy_engine.analyze(
            request.inst_id,
            candles,
            config=_strategy_config(request.strategy_id),
        )
        analysis["strategy_id"] = request.strategy_id
    except (OkxMarketError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    state_store.save_analysis(analysis)
    state_store.add_audit(
        "analysis_completed",
        "Structured market analysis completed",
        payload={"inst_id": request.inst_id, "source": analysis["source"]},
    )
    return {"data": analysis}


@app.post("/api/v1/analysis/ai")
async def run_ai_analysis(
    request: AnalysisRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    try:
        if tradingagents.configuration_error:
            raise AIAnalysisError(tradingagents.configuration_error)
        market_context = await _ai_market_context(
            request.inst_id,
            request.bar,
            request.limit,
        )
        analysis = await tradingagents.analyze(
            request.inst_id,
            market_context=market_context,
        )
    except AIAnalysisError as exc:
        state_store.add_audit(
            "ai_analysis_failed",
            "TradingAgents research did not complete",
            payload={"inst_id": request.inst_id, "code": exc.code},
        )
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    saved = state_store.save_analysis(
        {
            "inst_id": request.inst_id,
            "source": "TradingAgents",
            "bias": analysis.get("bias", "research"),
            "signal": {},
            "report": analysis,
        }
    )
    state_store.add_audit(
        "ai_analysis_completed",
        "TradingAgents analysis completed",
        payload={"inst_id": request.inst_id},
    )
    return {"data": {**analysis, "id": saved["id"], "created_at": saved["created_at"]}}


@app.get("/api/v1/analysis")
def analysis_history(
    limit: int = Query(default=30, ge=1, le=100),
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return {"data": state_store.list_analyses(limit)}


@app.get("/api/v1/analysis/history")
def analysis_index(
    limit: int = Query(default=20, ge=1, le=50),
    before_id: int | None = Query(default=None, ge=1),
    inst_id: str | None = Query(default=None, min_length=1, max_length=64),
    source: str | None = Query(default=None, min_length=1, max_length=64),
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return state_store.analysis_index(
        limit, before_id=before_id, inst_id=inst_id, source=source
    )


@app.get("/api/v1/analysis/{analysis_id}")
def analysis_detail(
    analysis_id: int,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    record = state_store.get_analysis(analysis_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return {"data": record}


class SignalExecutionRequest(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    signal: TradeSignal
    account_equity: float = Field(gt=0.0)
    daily_pnl_pct: float
    current_notional: float = Field(default=0.0, ge=0.0)
    size: float = Field(default=1.0, gt=0.0)
    dry_run: bool = False


@app.post("/api/v1/execution/signals")
async def execute_signal(
    request: SignalExecutionRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    try:
        return await execution_engine.submit_signal(
            request.signal,
            account_equity=request.account_equity,
            daily_pnl_pct=request.daily_pnl_pct,
            current_notional=request.current_notional,
            size=request.size,
            dry_run=request.dry_run,
            market_data_fresh=market_stream.fresh,
        )
    except OkxTradeError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc


@app.post("/api/v1/risk/evaluate")
def evaluate_risk(
    request: RiskEvaluateRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    decision = risk_engine.evaluate(
        request.signal,
        account_equity=request.account_equity,
        daily_pnl_pct=request.daily_pnl_pct,
        current_notional=request.current_notional,
        market_data_fresh=market_stream.fresh,
    )
    return {
        "approved": decision.approved,
        "reasons": list(decision.reasons),
        "checked_at": decision.checked_at,
        "execution_enabled": False,
        "market_data_fresh": market_stream.fresh,
        "note": "Risk evaluation only; no order was submitted.",
        "signal": decision.signal,
    }


@app.post("/api/v1/execution/orders/preview")
def preview_order(
    order: OrderRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return {
        "accepted": True,
        "execution_enabled": trade_client.enabled,
        "live_execution_allowed": False,
        "order": order.okx_payload(),
        "note": "Preview only; no order was submitted.",
    }


@app.post("/api/v1/execution/orders")
async def place_demo_order(
    order: OrderRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    raise HTTPException(
        status_code=410,
        detail=(
            "Direct orders are disabled. Submit a TradeSignal to "
            "/api/v1/execution/signals so risk checks and idempotency run first."
        ),
    )


@app.post("/api/v1/execution/orders/{client_order_id}/cancel")
async def cancel_stored_order(
    client_order_id: str = RoutePath(
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_-]+$",
    ),
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    order = state_store.get_order(client_order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found.")
    exchange_order_id = str(order.get("exchange_order_id") or "")
    if not exchange_order_id:
        raise HTTPException(
            status_code=409,
            detail="Order has no exchange order ID and cannot be canceled.",
        )
    if order.get("status") in {"canceled", "filled", "failed", "rejected", "effective", "triggered", "expired", "order_failed", "mmp_canceled"}:
        return {"accepted": False, "idempotent": True, "order": order}
    is_algo = order.get("order_kind") == "algo"
    try:
        cancel = trade_client.cancel_algo_order if is_algo else trade_client.cancel_order
        response = await cancel(
            str(order["inst_id"]),
            exchange_order_id,
        )
    except OkxTradeError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    response_data = response.get("data", [])
    response_row = response_data[0] if response_data else {}
    cancel_succeeded = (
        bool(response_row)
        and str(response_row.get("sCode", "0")) == "0"
        and str(response_row.get("algoId" if is_algo else "ordId")) == exchange_order_id
    )
    updated = {
        **order,
        "status": "canceling" if cancel_succeeded else "cancel_failed",
        "raw": response,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    saved = state_store.save_order(updated)
    state_store.add_audit(
        "order_cancel_requested" if cancel_succeeded else "order_cancel_failed",
        "Stored order cancellation requested",
        severity="info" if cancel_succeeded else "warning",
        payload={"client_order_id": client_order_id},
    )
    await execution_engine.notify_event(
        "order_cancel_requested" if cancel_succeeded else "order_cancel_failed",
        "OpenPerpDesk 撤单结果",
        (
            f"{order['inst_id']} 客户端订单 {client_order_id} "
            f"{'撤单请求已受理，最终状态以交易所回报为准' if cancel_succeeded else '撤单请求未确认'}。"
        ),
        payload={
            "client_order_id": client_order_id,
            "status": saved["status"],
        },
        severity="info" if cancel_succeeded else "warning",
    )
    return {
        "accepted": cancel_succeeded,
        "idempotent": False,
        "order": saved,
        "exchange": response,
    }


@app.get("/api/v1/strategies")
def strategies(
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    return {"data": state_store.list_strategies()}


class StrategyConfigRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    enabled: bool = False
    config: dict[str, object] = Field(default_factory=dict)


@app.put("/api/v1/strategies/{strategy_id}")
def save_strategy(
    strategy_id: str,
    request: StrategyConfigRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    if not strategy_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=422, detail="Invalid strategy id.")
    try:
        normalized_config = strategy_engine.normalize_config(request.config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "data": state_store.save_strategy(
            strategy_id,
            request.name,
            enabled=request.enabled,
            config=normalized_config,
        )
    }


@app.get("/api/v1/notifications/status")
def notifications_status() -> dict[str, object]:
    return {
        "pushplus_configured": pushplus_client.configured,
        "send_enabled": pushplus_client.configured,
    }


class NotificationTestRequest(BaseModel):
    title: str = Field(default="OpenPerpDesk 测试通知", min_length=1, max_length=80)
    content: str = Field(default="来自 OpenPerpDesk 的通知链路测试，请核对微信是否收到。", min_length=1, max_length=2000)


@app.post("/api/v1/notifications/test")
async def notification_test(
    request: NotificationTestRequest,
    _: None = Depends(require_admin_token),
) -> dict[str, object]:
    try:
        result = await execution_engine.notify(request.title, request.content)
        if result.get("accepted") is not True or result.get("delivery_confirmed") is not False:
            raise PushPlusError("pushplus_acceptance_unknown")
    except PushPlusError as exc:
        state_store.add_audit(
            "notification_test_unconfirmed" if exc.acceptance_unknown else "notification_test_rejected",
            "PushPlus test request was not confirmed" if exc.acceptance_unknown else "PushPlus test request was rejected",
            severity="warning", payload={"error": exc.code, "acceptance_unknown": exc.acceptance_unknown},
        )
        raise HTTPException(status_code=503 if exc.code == "pushplus_unconfigured" else 502, detail=exc.code) from None
    except Exception as exc:
        state_store.add_audit(
            "notification_test_unconfirmed", "PushPlus test request was not confirmed",
            severity="warning", payload={"error": type(exc).__name__, "acceptance_unknown": True},
        )
        raise HTTPException(status_code=502, detail="pushplus_acceptance_unknown") from None
    state_store.add_audit(
        "notification_accepted",
        "PushPlus accepted the test request; WeChat delivery is unverified",
        payload={"message_id": result.get("message_id"), "delivery_confirmed": False},
    )
    return {"accepted": True, "delivery_confirmed": False, "result": result}


_module_path = Path(__file__).resolve()
_web_directory = (
    _module_path.parents[3] / "apps" / "web"
    if len(_module_path.parents) > 3
    else Path("/nonexistent/openperpdesk-web")
)
if _web_directory.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=_web_directory, html=True),
        name="web",
    )
