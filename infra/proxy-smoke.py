"""Read-only OKX REST and WebSocket probe through the configured outbound proxy."""

import argparse
import asyncio
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services/api"))

from app.okx_market import OkxMarketClient
from app.okx_market_stream import OkxMarketStream


def write_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


async def rest_probe(client: OkxMarketClient, symbol: str) -> dict:
    ticker, candles = await asyncio.gather(
        client.ticker(symbol),
        client.candles(symbol, "15m", 10),
    )
    if ticker.get("instId") != symbol or not float(ticker.get("last", "0")) > 0:
        raise RuntimeError("ticker_payload_invalid")
    if len(candles) < 2 or any(len(row) < 6 for row in candles):
        raise RuntimeError("candle_payload_invalid")
    return {
        "instrument": symbol,
        "ticker_instrument_matches": True,
        "candles_received": len(candles),
    }


async def websocket_probe(stream: OkxMarketStream, timeout: float) -> dict:
    await stream.start()
    try:
        async with asyncio.timeout(timeout):
            while not (
                stream.fresh
                and stream.candles_fresh
                and all(
                    symbol in stream.tickers and symbol in stream.candles
                    for symbol in stream.symbols
                )
            ):
                await asyncio.sleep(0.1)
        before = {
            symbol: stream.tickers[symbol]["received_at"]
            for symbol in stream.symbols
        }
        await asyncio.sleep(2)
        if not stream.fresh or not stream.candles_fresh:
            raise RuntimeError("websocket_stream_stale")
        if any(
            stream.tickers[symbol]["received_at"] == before[symbol]
            for symbol in stream.symbols
        ):
            raise RuntimeError("ticker_stream_not_advancing")
        return {
            "quotes_connected": stream.connected,
            "quotes_fresh": stream.fresh,
            "candles_connected": stream.candles_connected,
            "candles_fresh": stream.candles_fresh,
            "symbols": stream.symbols,
            "candle_bars": list(stream.candle_bars),
            "quotes_advanced": True,
        }
    finally:
        await stream.stop()


async def run(timeout: float, symbols: list[str]) -> dict:
    proxy = os.getenv("OKX_PROXY_URL", "").strip()
    if not proxy:
        raise RuntimeError("proxy_not_configured")
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
        raise RuntimeError("proxy_url_invalid")
    market = OkxMarketClient()
    stream = OkxMarketStream(symbols)
    started = time.monotonic()
    rest = await asyncio.gather(*(rest_probe(market, symbol) for symbol in symbols))
    websocket = await websocket_probe(stream, timeout)
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "scope": "okx_public_read_only_through_outbound_proxy",
        "proxy_configured": True,
        "proxy_scheme": parsed.scheme,
        "rest": rest,
        "websocket": websocket,
        "private_account_verified": False,
        "trading_performed": False,
        "elapsed_seconds": round(time.monotonic() - started, 2),
    }


def load_environment() -> None:
    path = Path(os.getenv("OPENPERPDESK_ENV_FILE", ROOT / ".env"))
    if not path.is_file():
        return
    for name, value in dotenv_values(path, interpolate=False).items():
        if value is not None:
            os.environ.setdefault(name, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--symbols", default="BTC-USDT-SWAP,ETH-USDT-SWAP")
    args = parser.parse_args()
    if not 5 <= args.timeout <= 300:
        parser.error("--timeout must be between 5 and 300 seconds")
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols or any(not item.endswith("-SWAP") for item in symbols):
        parser.error("--symbols must contain OKX perpetual instruments")
    destination = ROOT / "outputs" / "proxy-verification.json"
    destination.unlink(missing_ok=True)
    try:
        load_environment()
        result = asyncio.run(run(args.timeout, symbols))
        write_private_json(destination, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, RuntimeError, TimeoutError, ValueError) as error:
        print(
            f"Proxy smoke failed ({type(error).__name__}); no report was published.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
