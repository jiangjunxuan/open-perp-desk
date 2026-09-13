import asyncio
import hashlib
import os
import time
from typing import Any

import httpx


class OkxMarketError(RuntimeError):
    """Raised when an OKX public market request cannot be completed."""


class OkxMarketClient:
    def __init__(self) -> None:
        self.base_url = os.getenv("OKX_REST_BASE_URL", "https://www.okx.com").rstrip("/")
        self.proxy_url = os.getenv("OKX_PROXY_URL", "").strip() or None
        self.demo = os.getenv("OKX_DEMO", "true").lower() == "true"
        self._index_lock = asyncio.Lock()
        self._index_requested_at = float("-inf")

    @property
    def rate_scope(self) -> str:
        return hashlib.sha256(f"{self.base_url}|{self.demo}".encode()).hexdigest()

    async def historical_index_rate(self, currency: str, candle_ms: int) -> str:
        from .historical_valuation import MINUTE_MS, parse_index_rate, rate_key

        rate_key(currency, candle_ms + MINUTE_MS)
        if candle_ms % MINUTE_MS:
            raise ValueError("historical_candle_alignment_invalid")
        async with self._index_lock:
            wait = .21 - (time.monotonic() - self._index_requested_at)
            if wait > 0:
                await asyncio.sleep(wait)
            self._index_requested_at = time.monotonic()
            rows = await self.get("/api/v5/market/history-index-candles", {
                "instId": f"{currency}-USD", "bar": "1m",
                "after": str(candle_ms + MINUTE_MS), "before": str(candle_ms - 1), "limit": "2",
            })
        return parse_index_rate(rows, candle_ms)

    async def get(self, path: str, params: dict[str, str]) -> list[dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if self.demo:
            headers["x-simulated-trading"] = "1"

        try:
            async with httpx.AsyncClient(
                proxy=self.proxy_url,
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers=headers,
            ) as client:
                response = await client.get(f"{self.base_url}{path}", params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OkxMarketError(f"OKX market request failed: {exc}") from exc

        if payload.get("code") != "0":
            raise OkxMarketError(payload.get("msg") or "OKX returned an unknown error")
        return payload.get("data", [])

    async def instruments(self, inst_id: str | None = None) -> list[dict[str, Any]]:
        params = {"instType": "SWAP"}
        if inst_id:
            params["instId"] = inst_id
        return await self.get("/api/v5/public/instruments", params)

    async def ticker(self, inst_id: str) -> dict[str, Any]:
        rows = await self.get("/api/v5/market/ticker", {"instId": inst_id})
        return rows[0] if rows else {}

    async def candles(self, inst_id: str, bar: str, limit: int) -> list[list[str]]:
        return await self.get(
            "/api/v5/market/candles",
            {"instId": inst_id, "bar": bar, "limit": str(limit)},
        )

    async def funding_rate(self, inst_id: str) -> dict[str, Any]:
        rows = await self.get("/api/v5/public/funding-rate", {"instId": inst_id})
        return rows[0] if rows else {}

    async def open_interest(self, inst_id: str) -> dict[str, Any]:
        rows = await self.get(
            "/api/v5/public/open-interest",
            {"instType": "SWAP", "instId": inst_id},
        )
        return rows[0] if rows else {}
