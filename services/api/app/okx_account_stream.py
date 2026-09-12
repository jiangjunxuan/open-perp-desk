import asyncio
import base64
import hashlib
import hmac
import json
import os
import ssl
import time
from datetime import datetime, timezone
from typing import Any

import certifi
from websockets.asyncio.client import connect


class OkxAccountStream:
    """Authenticated, read-only OKX account stream."""

    def __init__(self) -> None:
        self.api_key = os.getenv("OKX_API_KEY", "").strip()
        self.secret_key = os.getenv("OKX_SECRET_KEY", "").strip()
        self.passphrase = os.getenv("OKX_PASSPHRASE", "").strip()
        self.demo = os.getenv("OKX_DEMO", "true").lower() == "true"
        default_url = (
            "wss://wspap.okx.com:8443/ws/v5/private"
            if self.demo
            else "wss://ws.okx.com:8443/ws/v5/private"
        )
        self.url = os.getenv("OKX_WS_PRIVATE_URL", "").strip() or default_url
        self.proxy_url = os.getenv("OKX_PROXY_URL", "").strip() or None
        self.connected = False
        self.authenticated = False
        self.last_message_at: str | None = None
        self.last_error: str | None = None
        self.balance: list[dict[str, Any]] = []
        self.positions: dict[str, dict[str, Any]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None

    @property
    def configured(self) -> bool:
        return all((self.api_key, self.secret_key, self.passphrase))

    async def start(self) -> None:
        if self._task is None and self.configured:
            self._task = asyncio.create_task(
                self._run(),
                name="okx-private-account-stream",
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self.connected = False
        self.authenticated = False

    @staticmethod
    def signature(timestamp: str, secret_key: str) -> str:
        message = f"{timestamp}GET/users/self/verify".encode()
        digest = hmac.new(secret_key.encode(), message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def login_message(self, timestamp: str | None = None) -> dict[str, Any]:
        current_timestamp = timestamp or str(int(time.time()))
        return {
            "op": "login",
            "args": [
                {
                    "apiKey": self.api_key,
                    "passphrase": self.passphrase,
                    "timestamp": current_timestamp,
                    "sign": self.signature(current_timestamp, self.secret_key),
                }
            ],
        }

    @staticmethod
    def subscription_message() -> dict[str, Any]:
        return {
            "op": "subscribe",
            "args": [
                {"channel": "account"},
                {"channel": "positions", "instType": "SWAP"},
                {"channel": "orders", "instType": "SWAP"},
            ],
        }

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
                    self.connected = True
                    self.authenticated = False
                    self.last_error = None
                    delay = 1.0
                    await socket.send(json.dumps(self.login_message()))
                    async for raw in socket:
                        self.consume(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.authenticated = False
                self.last_error = type(exc).__name__
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)

    def consume(self, raw: str | bytes) -> None:
        try:
            message = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return

        if message.get("event") == "login":
            if str(message.get("code", "")) == "0":
                self.authenticated = True
                self.last_error = None
            else:
                self.authenticated = False
                self.last_error = "login_failed"
            return

        if message.get("event") in {"subscribe", "channel-conn-count"}:
            return

        argument = message.get("arg") or {}
        data = message.get("data") or []
        channel = argument.get("channel")
        if not channel or not data:
            return

        self.last_message_at = datetime.now(timezone.utc).isoformat()
        if channel == "account":
            self.balance = data
        elif channel == "positions":
            self.positions = {
                self._position_key(item): item
                for item in data
            }
        elif channel == "orders":
            for item in data:
                order_id = str(item.get("ordId") or item.get("clOrdId") or "")
                if order_id:
                    self.orders[order_id] = item

    @staticmethod
    def _position_key(position: dict[str, Any]) -> str:
        return ":".join(
            (
                str(position.get("instId", "")),
                str(position.get("posSide", "")),
                str(position.get("mgnMode", "")),
            )
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "demo": self.demo,
            "connected": self.connected,
            "authenticated": self.authenticated,
            "proxy_configured": self.proxy_url is not None,
            "last_message_at": self.last_message_at,
            "last_error": self.last_error,
            "balance": list(self.balance),
            "positions": list(self.positions.values()),
            "orders": list(self.orders.values()),
        }
