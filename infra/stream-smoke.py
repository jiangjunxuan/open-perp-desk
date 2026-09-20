"""Read-only public OKX WebSocket acceptance probe. Never imports the trading API."""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))

from app.okx_market_stream import OkxMarketStream


async def main() -> None:
    symbols = ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]
    stream = OkxMarketStream(symbols)
    destination = ROOT / "outputs" / "public-stream-verification.json"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # A failed probe must not leave a previous successful report that looks current.
    destination.unlink(missing_ok=True)
    try:
        await stream.start()
        async with asyncio.timeout(30):
            while not (
                stream.fresh and stream.candles_fresh
                and all(symbol in stream.tickers and symbol in stream.candles for symbol in symbols)
            ):
                await asyncio.sleep(.1)
        snapshot = stream.snapshot()
        result = {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "scope": "public_websocket_read_only",
            "configured_endpoints": "environment_or_okx_defaults",
            "proxy_configured": stream.proxy_url is not None,
            "ticker_channel": "tickers",
            "candle_channel": "candle1m",
            "symbols": symbols,
            **{key: snapshot[key] for key in (
                "connected", "fresh", "last_message_at", "last_error",
                "candles_connected", "candles_fresh", "candles_last_message_at", "candles_last_error",
            )},
            "received": [
                {
                    "inst_id": symbol,
                    "ticker_instrument_matches": stream.tickers[symbol]["data"].get("instId") == symbol,
                    "candle_fields": len(stream.candles[symbol]["data"]),
                }
                for symbol in symbols
            ],
            "private_account_verified": False,
            "trading_performed": False,
        }
        if not all(row["ticker_instrument_matches"] and row["candle_fields"] >= 6 for row in result["received"]):
            raise RuntimeError("Market evidence is incomplete")
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await stream.stop()


if __name__ == "__main__":
    asyncio.run(main())
