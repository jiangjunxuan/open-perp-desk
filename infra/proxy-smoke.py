"""Read-only OKX REST and WebSocket probe through the configured outbound proxy."""

import argparse
import asyncio
import json
import math
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path.cwd() if __file__ in {"<stdin>", "-"} else Path(__file__).resolve().parents[1]
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


def websocket_records(stream: OkxMarketStream) -> dict | None:
    snapshots = {bar: stream.browser_snapshot(bar) for bar in stream.candle_bars}
    if not snapshots or not stream.fresh or not stream.candles_fresh:
        return None
    quotes = next(iter(snapshots.values()))["tickers"]
    records = {}
    for symbol in stream.symbols:
        quote = quotes.get(symbol)
        candles = {bar: snapshot["candles"].get(symbol) for bar, snapshot in snapshots.items()}
        if not quote or not quote["fresh"] or any(
            not candle or not candle["fresh"] for candle in candles.values()
        ):
            return None
        if quote["data"].get("instId") != symbol:
            raise RuntimeError("ticker_instrument_mismatch")
        price = float(quote["data"].get("last", "0"))
        if not math.isfinite(price) or price <= 0:
            raise RuntimeError("ticker_payload_invalid")
        for bar, candle in candles.items():
            row = candle["data"]
            if candle["inst_id"] != symbol or candle["channel"] != f"candle{bar}":
                raise RuntimeError("candle_subscription_mismatch")
            if not isinstance(row, list) or len(row) < 6:
                raise RuntimeError("candle_payload_invalid")
            values = [float(value) for value in row[:6]]
            if any(not math.isfinite(value) for value in values) or any(
                value <= 0 for value in values[:5]
            ) or values[5] < 0:
                raise RuntimeError("candle_payload_invalid")
        records[symbol] = {"quote": quote, "candles": candles}
    return records


async def websocket_probe(stream: OkxMarketStream, timeout: float) -> dict:
    try:
        await stream.start()
        async with asyncio.timeout(timeout):
            while (before := websocket_records(stream)) is None:
                await asyncio.sleep(0.1)
        await asyncio.sleep(2)
        after = websocket_records(stream)
        if after is None:
            raise RuntimeError("websocket_stream_stale")
        if any(
            after[symbol]["quote"]["received_at"] == before[symbol]["quote"]["received_at"]
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
            "received": [
                {
                    "instrument": symbol,
                    "quote_fresh": record["quote"]["fresh"],
                    "fresh_candle_bars": list(record["candles"]),
                }
                for symbol, record in after.items()
            ],
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
    for name, value in read_env_file(path).items():
        os.environ.setdefault(name, value)


def read_env_file(path: Path) -> dict[str, str]:
    """Read the small, non-interpolating .env subset used by deployment probes."""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "a").isalnum() or name[0].isdigit():
            continue
        value = value.strip()
        if value.startswith(("'", '"')) and len(value) >= 2 and value[-1] == value[0]:
            quote = value[0]
            value = value[1:-1]
            if quote == '"':
                value = (
                    value.replace("\\n", "\n")
                    .replace("\\r", "\r")
                    .replace("\\t", "\t")
                    .replace('\\"', '"')
                    .replace("\\\\", "\\")
                )
        else:
            value = value.split(" #", 1)[0].rstrip()
        values[name] = value
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--symbols", default="BTC-USDT-SWAP,ETH-USDT-SWAP")
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "proxy-verification.json")
    args = parser.parse_args()
    if not 5 <= args.timeout <= 300:
        parser.error("--timeout must be between 5 and 300 seconds")
    symbols = [item.strip().upper() for item in args.symbols.split(",") if item.strip()]
    if not symbols or any(not item.endswith("-SWAP") for item in symbols):
        parser.error("--symbols must contain OKX perpetual instruments")
    destination = None if args.output == Path("-") else args.output
    if destination is not None:
        destination.unlink(missing_ok=True)
    try:
        load_environment()
        result = asyncio.run(run(args.timeout, symbols))
        if destination is not None:
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
