import asyncio
import json
import os
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import certifi
from websockets.asyncio.client import connect


class OkxMarketStream:
    """Read-only OKX public market stream with fail-closed reconnects."""

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.url = os.getenv(
            "OKX_WS_PUBLIC_URL",
            "wss://ws.okx.com:8443/ws/v5/public",
        )
        self.proxy_url = os.getenv("OKX_PROXY_URL", "").strip() or None
        self.connected = False
        self.last_message_at: str | None = None
        self.last_error: str | None = None
        self.tickers: dict[str, dict[str, Any]] = {}
        self.candles: dict[str, dict[str, Any]] = {}
        self._last_message_epoch: float | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="okx-public-market-stream",
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self.connected = False

    @property
    def fresh(self) -> bool:
        return (
            self.connected
            and self._last_message_epoch is not None
            and time.monotonic() - self._last_message_epoch < 45
        )

    async def _run(self) -> None:
        delay = 1.0
        while True:
            try:
                async with connect(
                    self.url,
                    proxy=self.proxy_url,
                    ssl=ssl.create_default_context(cafile=certifi.where()),
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=2**20,
                ) as socket:
                    await socket.send(json.dumps(self.subscription_message()))
                    self.connected = True
                    self.last_error = None
                    delay = 1.0
                    async for raw in socket:
                        self.consume(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                # Never expose a proxy URL or credentials in the API status.
                self.last_error = type(exc).__name__
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    def subscription_message(self) -> dict[str, Any]:
        return {
            "op": "subscribe",
            "args": [
                *(
                    {"channel": "tickers", "instId": symbol}
                    for symbol in self.symbols
                ),
                *(
                    {"channel": "candle1m", "instId": symbol}
                    for symbol in self.symbols
                ),
            ],
        }

    def consume(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return

        if message.get("event") in {"subscribe", "channel-conn-count"}:
            return

        argument = message.get("arg") or {}
        data = message.get("data") or []
        channel = argument.get("channel")
        inst_id = argument.get("instId")
        if not channel or not inst_id or not data:
            return

        received_at = datetime.now(timezone.utc).isoformat()
        record = {
            "inst_id": inst_id,
            "channel": channel,
            "data": data[0],
            "received_at": received_at,
        }
        self.last_message_at = received_at
        self._last_message_epoch = time.monotonic()

        if channel == "tickers":
            self.tickers[inst_id] = record
        elif channel == "candle1m":
            self.candles[inst_id] = record

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "fresh": self.fresh,
            "proxy_configured": self.proxy_url is not None,
            "symbols": self.symbols,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
            "tickers": self.tickers,
            "candles": self.candles,
        }
