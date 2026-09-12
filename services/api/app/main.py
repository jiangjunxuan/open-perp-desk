import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from .okx_account import OkxAccountClient, OkxAccountError
from .okx_account_stream import OkxAccountStream
from .okx_market import OkxMarketClient, OkxMarketError
from .okx_market_stream import OkxMarketStream
from .okx_trade import OkxTradeClient, OkxTradeError, OrderRequest
from .pushplus import PushPlusClient
from .risk_engine import RiskEngine
from .trading_signal import TradeSignal


def _symbols() -> list[str]:
    raw = os.getenv("MARKET_SYMBOLS", "BTC-USDT-SWAP,ETH-USDT-SWAP")
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


market_client = OkxMarketClient()
market_stream = OkxMarketStream(_symbols())
account_client = OkxAccountClient()
account_stream = OkxAccountStream()
pushplus_client = PushPlusClient()
risk_engine = RiskEngine()
trade_client = OkxTradeClient()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await market_stream.start()
    await account_stream.start()
    yield
    await account_stream.stop()
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


@app.get("/api/v1/system/status")
def system_status() -> dict[str, object]:
    trading_mode = os.getenv("TRADING_MODE", "demo").lower()
    return {
        "service": "OpenPerpDesk",
        "environment": os.getenv("APP_ENV", "development"),
        "trading_mode": trading_mode,
        "execution_enabled": trade_client.enabled,
        "market_data_connected": market_stream.fresh,
        "risk_engine_ready": False,
        "integrations": {
            "okx_credentials_configured": all(
                _is_configured(name)
                for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")
            ),
            "outbound_proxy_configured": market_stream.proxy_url is not None,
            "pushplus_configured": pushplus_client.configured,
            "account_readonly_configured": account_client.configured,
            "account_stream_configured": account_stream.configured,
        },
        "market_stream": {
            "connected": market_stream.connected,
            "fresh": market_stream.fresh,
            "last_message_at": market_stream.last_message_at,
            "last_error": market_stream.last_error,
        },
        "account_stream": {
            "connected": account_stream.connected,
            "authenticated": account_stream.authenticated,
            "last_message_at": account_stream.last_message_at,
            "last_error": account_stream.last_error,
        },
        "safety": {
            "live_orders_allowed": False,
            "reason": (
                "Demo execution is disabled until EXECUTION_ENABLED and guarded "
                "credentials are configured."
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
    try:
        balance, positions, config = await asyncio.gather(
            account_client.balance(),
            account_client.positions(),
            account_client.config(),
        )
        return {
            "configured": True,
            "demo": account_client.demo,
            "balance": balance,
            "positions": positions,
            "config": config,
        }
    except OkxAccountError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.get("/api/v1/account/stream")
def account_stream_snapshot() -> dict[str, object]:
    return account_stream.snapshot()


@app.get("/api/v1/execution/status")
def execution_status() -> dict[str, object]:
    return {
        "configured": trade_client.configured,
        "demo": trade_client.demo,
        "trading_mode": trade_client.trading_mode,
        "execution_enabled": trade_client.enabled,
        "live_execution_allowed": False,
        "proxy_configured": trade_client.proxy_url is not None,
    }


class RiskEvaluateRequest(BaseModel):
    signal: TradeSignal
    account_equity: float = Field(gt=0.0)
    daily_pnl_pct: float
    current_notional: float = Field(default=0.0, ge=0.0)


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
    )
    return {
        "approved": decision.approved,
        "reasons": list(decision.reasons),
        "checked_at": decision.checked_at,
        "execution_enabled": False,
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
    try:
        payload = await trade_client.place_order(order)
    except OkxTradeError as exc:
        raise HTTPException(status_code=423, detail=str(exc)) from exc
    return {
        "submitted": True,
        "demo": True,
        "live_execution_allowed": False,
        "data": payload.get("data", []),
    }


@app.get("/api/v1/notifications/status")
def notifications_status() -> dict[str, object]:
    return {
        "pushplus_configured": pushplus_client.configured,
        "send_enabled": pushplus_client.configured,
    }
