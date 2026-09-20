import asyncio
import json
import ssl
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import certifi
from websockets.exceptions import ConnectionClosedOK


class OkxSubscriptionError(ConnectionError):
    pass


class OkxAuthenticationError(ConnectionError):
    pass


def websocket_tls(url: str, proxy: str | None = None) -> ssl.SSLContext | None:
    endpoint = urlsplit(url)
    if endpoint.scheme == "wss":
        return ssl.create_default_context(cafile=certifi.where())
    loopback = {"127.0.0.1", "::1", "localhost"}
    if (
        endpoint.scheme == "ws" and endpoint.hostname in loopback
        and (proxy is None or urlsplit(proxy).hostname in loopback)
    ):
        return None
    raise ValueError("Non-local OKX WebSocket endpoints must use TLS")


def decode_message(raw: str | bytes) -> dict[str, Any]:
    try:
        message = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return message if isinstance(message, dict) else {}


async def socket_messages(
    socket: Any,
    *,
    idle_seconds: float = 20,
    response_seconds: float = 10,
) -> AsyncIterator[str | bytes]:
    """Use OKX's text heartbeat; WebSocket control pings aren't a substitute."""
    while True:
        try:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=idle_seconds)
            except TimeoutError:
                await socket.send("ping")
                raw = await asyncio.wait_for(socket.recv(), timeout=response_seconds)
        except ConnectionClosedOK:
            return
        if raw not in ("pong", b"pong"):
            yield raw
