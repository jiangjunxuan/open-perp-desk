import os
from datetime import datetime, timezone

from fastapi import FastAPI


app = FastAPI(
    title="OpenPerpDesk API",
    version="0.1.0",
    docs_url="/api/docs",
    openapi_url="/api/openapi.json",
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

