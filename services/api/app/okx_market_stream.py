import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any

from websockets.asyncio.client import connect

from .okx_websocket import OkxSubscriptionError, decode_message, socket_messages, websocket_tls


class OkxMarketStream:
    """Read-only OKX public market stream with fail-closed reconnects."""

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols
        self.url = os.getenv("OKX_WS_PUBLIC_URL", "").strip() or "wss://ws.okx.com:8443/ws/v5/public"
        self.candles_url = os.getenv("OKX_WS_CANDLES_URL", "").strip() or "wss://ws.okx.com:8443/ws/v5/business"
        self.proxy_url = os.getenv("OKX_PROXY_URL", "").strip() or None
        self.connected = False
        self.last_message_at: str | None = None
        self.last_error: str | None = None
        self.candles_connected = False
        self.candles_last_message_at: str | None = None
        self.candles_last_error: str | None = None
        self.tickers: dict[str, dict[str, Any]] = {}
        self.candles: dict[str, dict[str, Any]] = {}
        self._last_message_epoch: float | None = None
        self._last_candle_epoch: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._candle_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="okx-public-market-stream",
            )
        if self._candle_task is None:
            self._candle_task = asyncio.create_task(
                self._run(candles=True),
                name="okx-public-candle-stream",
            )

    async def stop(self) -> None:
        tasks = [task for task in (self._task, self._candle_task) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._task = None
        self._candle_task = None
        self.connected = False
        self.candles_connected = False

    @property
    def fresh(self) -> bool:
        return (
            self.connected
            and self._last_message_epoch is not None
            and time.monotonic() - self._last_message_epoch < 45
        )

    @property
    def candles_fresh(self) -> bool:
        return (
            self.candles_connected
            and self._last_candle_epoch is not None
            and time.monotonic() - self._last_candle_epoch < 45
        )

    async def _run(self, *, candles: bool = False) -> None:
        delay = 1.0
        url = self.candles_url if candles else self.url
        connection_field = "candles_connected" if candles else "connected"
        error_field = "candles_last_error" if candles else "last_error"
        epoch_field = "_last_candle_epoch" if candles else "_last_message_epoch"
        while True:
            try:
                setattr(self, epoch_field, None)
                async with connect(
                    url,
                    proxy=self.proxy_url,
                    ssl=websocket_tls(url, self.proxy_url),
                    ping_interval=20,
                    ping_timeout=20,
                    close_timeout=5,
                    max_size=2**20,
                ) as socket:
                    await socket.send(json.dumps(self.subscription_message(candles=candles)))
                    setattr(self, connection_field, True)
                    setattr(self, error_field, None)
                    delay = 1.0
                    async for raw in socket_messages(socket):
                        if decode_message(raw).get("event") == "error":
                            raise OkxSubscriptionError()
                        self.consume(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Never expose a proxy URL or credentials in the API status.
                setattr(self, error_field, type(exc).__name__)
            finally:
                setattr(self, connection_field, False)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    def subscription_message(self, *, candles: bool = False) -> dict[str, Any]:
        return {
            "op": "subscribe",
            "args": [
                {"channel": "candle1m" if candles else "tickers", "instId": symbol}
                for symbol in self.symbols
            ],
        }

    def consume(self, raw: str | bytes) -> None:
        message = decode_message(raw)

        if message.get("event") in {"subscribe", "channel-conn-count"}:
            return

        argument = message.get("arg")
        data = message.get("data")
        if not isinstance(argument, dict) or not isinstance(data, list):
            return
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
        if channel == "tickers" and isinstance(data[0], dict):
            self.last_message_at = received_at
            self._last_message_epoch = time.monotonic()
            self.tickers[inst_id] = record
        elif channel == "candle1m" and isinstance(data[0], list):
            self.candles_last_message_at = received_at
            self._last_candle_epoch = time.monotonic()
            self.candles[inst_id] = record

    def snapshot(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "fresh": self.fresh,
            "proxy_configured": self.proxy_url is not None,
            "symbols": self.symbols,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
            "candles_connected": self.candles_connected,
            "candles_fresh": self.candles_fresh,
            "candles_last_message_at": self.candles_last_message_at,
            "candles_last_error": self.candles_last_error,
            "tickers": self.tickers,
            "candles": self.candles,
        }
