import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

import httpx


class OkxAccountError(RuntimeError):
    """Raised when a signed OKX account request cannot be completed."""


class OkxAccountClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("OKX_REST_BASE_URL", "https://www.okx.com").rstrip("/")
        self.api_key = os.getenv("OKX_API_KEY", "").strip()
        self.secret_key = os.getenv("OKX_SECRET_KEY", "").strip()
        self.passphrase = os.getenv("OKX_PASSPHRASE", "").strip()
        self.proxy_url = os.getenv("OKX_PROXY_URL", "").strip() or None
        self.demo = os.getenv("OKX_DEMO", "true").lower() == "true"

    @property
    def configured(self) -> bool:
        return all((self.api_key, self.secret_key, self.passphrase))

    @staticmethod
    def timestamp() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00",
            "Z",
        )

    @staticmethod
    def signature(
        timestamp: str,
        method: str,
        request_path: str,
        body: str = "",
        secret_key: str = "",
    ) -> str:
        message = f"{timestamp}{method.upper()}{request_path}{body}".encode()
        digest = hmac.new(secret_key.encode(), message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, timestamp: str, request_path: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self.signature(
                timestamp,
                "GET",
                request_path,
                secret_key=self.secret_key,
            ),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
        }
        if self.demo:
            headers["x-simulated-trading"] = "1"
        return headers

    async def _get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.configured:
            raise OkxAccountError("OKX read-only credentials are not configured")

        query = f"?{urlencode(params)}" if params else ""
        request_path = f"{path}{query}"
        timestamp = self.timestamp()
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy_url,
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers=self._headers(timestamp, request_path),
            ) as client:
                response = await client.get(f"{self.base_url}{path}", params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OkxAccountError(f"OKX account request failed: {exc}") from exc

        if payload.get("code") != "0":
            raise OkxAccountError(payload.get("msg") or "OKX returned an unknown error")
        return payload.get("data", [])

    async def balance(self) -> list[dict[str, Any]]:
        return await self._get("/api/v5/account/balance")

    async def positions(self) -> list[dict[str, Any]]:
        return await self._get("/api/v5/account/positions", {"instType": "SWAP"})

    async def config(self) -> list[dict[str, Any]]:
        return await self._get("/api/v5/account/config")

    async def pending_orders(self) -> list[dict[str, Any]]:
        return await self._get(
            "/api/v5/trade/orders-pending",
            {"instType": "SWAP"},
        )

    async def orders_history(
        self,
        inst_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = {
            "instType": "SWAP",
            "limit": str(max(1, min(limit, 100))),
        }
        if inst_id:
            params["instId"] = inst_id
        return await self._get(
            "/api/v5/trade/orders-history-archive",
            params,
        )

    async def fills_history(
        self,
        inst_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = {
            "instType": "SWAP",
            "limit": str(max(1, min(limit, 100))),
        }
        if inst_id:
            params["instId"] = inst_id
        return await self._get("/api/v5/trade/fills-history", params)

    async def pending_algo_orders(
        self,
        inst_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = {
            "instType": "SWAP",
            "limit": str(max(1, min(limit, 100))),
        }
        if inst_id:
            params["instId"] = inst_id
        return await self._get(
            "/api/v5/trade/orders-algo-pending",
            params,
        )

    async def algo_orders_history(
        self,
        inst_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = {
            "instType": "SWAP",
            "limit": str(max(1, min(limit, 100))),
        }
        if inst_id:
            params["instId"] = inst_id
        return await self._get(
            "/api/v5/trade/orders-algo-history",
            params,
        )
