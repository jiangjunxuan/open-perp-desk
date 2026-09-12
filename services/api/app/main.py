import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from .okx_market import OkxMarketClient, OkxMarketError
from .okx_market_stream import OkxMarketStream


def _symbols() -> list[str]:
    raw = os.getenv("MARKET_SYMBOLS", "BTC-USDT-SWAP,ETH-USDT-SWAP")
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


market_client = OkxMarketClient()
market_stream = OkxMarketStream(_symbols())


@asynccontextmanager
async def lifespan(_: FastAPI):
    await market_stream.start()
    yield
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
        "execution_enabled": False,
        "market_data_connected": market_stream.fresh,
        "risk_engine_ready": False,
        "integrations": {
            "okx_credentials_configured": all(
                _is_configured(name)
                for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")
            ),
            "outbound_proxy_configured": market_stream.proxy_url is not None,
            "pushplus_configured": _is_configured("PUSHPLUS_TOKEN"),
        },
        "market_stream": {
            "connected": market_stream.connected,
            "fresh": market_stream.fresh,
            "last_message_at": market_stream.last_message_at,
            "last_error": market_stream.last_error,
        },
        "safety": {
            "live_orders_allowed": False,
            "reason": "Execution worker is not connected in the initial read-only phase.",
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
