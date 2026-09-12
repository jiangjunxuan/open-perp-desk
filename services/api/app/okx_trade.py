import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, model_validator


class OkxTradeError(RuntimeError):
    """Raised when a guarded OKX demo order cannot be completed."""


class OrderRequest(BaseModel):
    """A deliberately narrow SWAP order shape for the first executor."""

    inst_id: str = Field(min_length=9, max_length=40, pattern=r"^[A-Z0-9-]+$")
    side: Literal["buy", "sell"]
    td_mode: Literal["isolated", "cross"] = "isolated"
    pos_side: Literal["net", "long", "short"] = "net"
    ord_type: Literal["market", "limit"] = "market"
    sz: float = Field(gt=0.0)
    px: float | None = Field(default=None, gt=0.0)
    reduce_only: bool = False
    cl_ord_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=32,
        pattern=r"^[A-Za-z0-9_-]+$",
    )

    @model_validator(mode="after")
    def validate_contract_order(self) -> "OrderRequest":
        if not self.inst_id.endswith("-SWAP"):
            raise ValueError("only SWAP instruments are enabled")
        if self.ord_type == "limit" and self.px is None:
            raise ValueError("limit orders require px")
        if self.ord_type == "market" and self.px is not None:
            raise ValueError("market orders must not include px")
        if self.reduce_only and self.pos_side == "net":
            return self
        return self

    def okx_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "instId": self.inst_id,
            "tdMode": self.td_mode,
            "side": self.side,
            "posSide": self.pos_side,
            "ordType": self.ord_type,
            "sz": self._number(self.sz),
        }
        if self.px is not None:
            payload["px"] = self._number(self.px)
        if self.reduce_only:
            payload["reduceOnly"] = True
        if self.cl_ord_id:
            payload["clOrdId"] = self.cl_ord_id
        return payload

    @staticmethod
    def _number(value: float) -> str:
        return format(value, "f").rstrip("0").rstrip(".")


class OkxTradeClient:
    """Guarded OKX demo order client. Live order execution is intentionally blocked."""

    def __init__(self) -> None:
        self.base_url = os.getenv("OKX_REST_BASE_URL", "https://www.okx.com").rstrip("/")
        self.api_key = os.getenv("OKX_API_KEY", "").strip()
        self.secret_key = os.getenv("OKX_SECRET_KEY", "").strip()
        self.passphrase = os.getenv("OKX_PASSPHRASE", "").strip()
        self.proxy_url = os.getenv("OKX_PROXY_URL", "").strip() or None
        self.demo = os.getenv("OKX_DEMO", "true").lower() == "true"
        self.trading_mode = os.getenv("TRADING_MODE", "demo").lower()
        self.execution_enabled = (
            os.getenv("EXECUTION_ENABLED", "false").lower() == "true"
        )

    @property
    def configured(self) -> bool:
        return all((self.api_key, self.secret_key, self.passphrase))

    @property
    def enabled(self) -> bool:
        return (
            self.execution_enabled
            and self.configured
            and self.demo
            and self.trading_mode == "demo"
        )

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
        body: str,
        secret_key: str,
    ) -> str:
        message = f"{timestamp}{method.upper()}{request_path}{body}".encode()
        digest = hmac.new(secret_key.encode(), message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _headers(self, timestamp: str, request_path: str, body: str) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self.signature(
                timestamp,
                "POST",
                request_path,
                body,
                self.secret_key,
            ),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
        }
        if self.demo:
            headers["x-simulated-trading"] = "1"
        return headers

    async def place_order(self, order: OrderRequest) -> dict[str, Any]:
        self._assert_enabled()
        path = "/api/v5/trade/order"
        body = json.dumps(order.okx_payload(), separators=(",", ":"))
        timestamp = self.timestamp()
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy_url,
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers=self._headers(timestamp, path, body),
            ) as client:
                response = await client.post(f"{self.base_url}{path}", content=body)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OkxTradeError(f"OKX order request failed: {exc}") from exc

        if payload.get("code") != "0":
            raise OkxTradeError(payload.get("msg") or "OKX returned an unknown error")
        return payload

    async def cancel_order(self, inst_id: str, ord_id: str) -> dict[str, Any]:
        self._assert_enabled()
        path = "/api/v5/trade/cancel-order"
        body = json.dumps(
            {"instId": inst_id, "ordId": ord_id},
            separators=(",", ":"),
        )
        timestamp = self.timestamp()
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy_url,
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers=self._headers(timestamp, path, body),
            ) as client:
                response = await client.post(f"{self.base_url}{path}", content=body)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OkxTradeError(f"OKX cancel request failed: {exc}") from exc

        if payload.get("code") != "0":
            raise OkxTradeError(payload.get("msg") or "OKX returned an unknown error")
        return payload

    def _assert_enabled(self) -> None:
        if not self.demo or self.trading_mode != "demo":
            raise OkxTradeError(
                "Live order execution is blocked; only OKX demo mode is supported."
            )
        if not self.execution_enabled:
            raise OkxTradeError("Execution is disabled by EXECUTION_ENABLED.")
        if not self.configured:
            raise OkxTradeError("OKX credentials are not configured.")

