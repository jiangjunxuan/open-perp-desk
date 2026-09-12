import os
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query

from .okx_market import OkxMarketClient, OkxMarketError


app = FastAPI(
    title="OpenPerpDesk API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
)
market_client = OkxMarketClient()


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
        "market_data_connected": False,
        "risk_engine_ready": False,
        "integrations": {
            "okx_credentials_configured": all(
                _is_configured(name)
                for name in ("OKX_API_KEY", "OKX_SECRET_KEY", "OKX_PASSPHRASE")
            ),
            "outbound_proxy_configured": _is_configured("OKX_PROXY_URL"),
            "pushplus_configured": _is_configured("PUSHPLUS_TOKEN"),
        },
        "safety": {
            "live_orders_allowed": trading_mode == "live" and False,
            "reason": "Execution worker is not connected in the initial skeleton.",
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
