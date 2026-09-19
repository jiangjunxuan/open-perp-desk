"""Read-only public OKX index checks; no account credentials or ledger writes."""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))

from app.historical_ledger import day_ms
from app.historical_valuation import MINUTE_MS
from app.okx_market import OkxMarketClient


async def main():
    client = OkxMarketClient()
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    candle_ms = day_ms(yesterday) - MINUTE_MS
    rates = {}
    for currency in ("USDT", "BTC", "ETH"):
        rates[f"{currency}-USD"] = await client.historical_index_rate(currency, candle_ms)
    print(json.dumps({
        "read_only": True, "source": "configured OKX historical index endpoint",
        "official_host": urlsplit(client.base_url).hostname in {"www.okx.com", "eea.okx.com", "us.okx.com"},
        "candle_open_ms": candle_ms, "candle_close_ms": candle_ms + MINUTE_MS,
        "confirmed": True, "rates": rates, "account_accessed": False,
    }, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        raise SystemExit(f"Historical quote verification failed: {type(exc).__name__}") from None
