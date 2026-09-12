import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, model_validator

from .live_safety import LiveSafetyGate


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
    stop_loss: float | None = Field(default=None, gt=0.0)
    take_profit: float | None = Field(default=None, gt=0.0)
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
        if self.stop_loss is not None or self.take_profit is not None:
            attached: dict[str, Any] = {
                "attachAlgoClOrdId": f"{self.cl_ord_id or 'opd'}-protect",
                "tpOrdKind": "condition",
            }
            if self.take_profit is not None:
                attached.update(
                    {
                        "tpTriggerPx": self._number(self.take_profit),
                        "tpTriggerPxType": "mark",
                        "tpOrdPx": "-1",
                    }
                )
            if self.stop_loss is not None:
                attached.update(
                    {
                        "slTriggerPx": self._number(self.stop_loss),
                        "slTriggerPxType": "mark",
                        "slOrdPx": "-1",
                    }
                )
            payload["attachAlgoOrds"] = [attached]
        if self.cl_ord_id:
            payload["clOrdId"] = self.cl_ord_id
        return payload

    @staticmethod
    def _number(value: float) -> str:
        return format(value, "f").rstrip("0").rstrip(".")


class OkxTradeClient:
    """Guarded OKX order client with a separate live safety gate."""

    def __init__(
        self,
        live_gate: LiveSafetyGate | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
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
        self.live_gate = live_gate or LiveSafetyGate()
        self.transport = transport

    @property
    def configured(self) -> bool:
        return all((self.api_key, self.secret_key, self.passphrase))

    @property
    def enabled(self) -> bool:
        demo_enabled = (
            self.execution_enabled
            and self.configured
            and self.demo
            and self.trading_mode == "demo"
        )
        live_enabled = (
            self.execution_enabled
            and self.configured
            and self.live_gate.allowed
        )
        return demo_enabled or live_enabled

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
                transport=self.transport,
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
        self._raise_for_order_row_error(payload)
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
                transport=self.transport,
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

    @staticmethod
    def _raise_for_order_row_error(payload: dict[str, Any]) -> None:
        """Reject a batch response when OKX rejected the individual order."""
        rows = payload.get("data") or []
        if not rows:
            raise OkxTradeError("OKX order response did not contain an order result")
        row = rows[0] or {}
        if str(row.get("sCode", "0")) != "0":
            raise OkxTradeError(
                row.get("sMsg")
                or row.get("msg")
                or f"OKX order rejected with sCode={row.get('sCode')}"
            )
        if not row.get("ordId"):
            raise OkxTradeError("OKX order response did not contain ordId")

    def _assert_enabled(self) -> None:
        if self.demo and self.trading_mode == "demo":
            if not self.execution_enabled:
                raise OkxTradeError("Execution is disabled by EXECUTION_ENABLED.")
            if not self.configured:
                raise OkxTradeError("OKX credentials are not configured.")
            return
        if not self.live_gate.allowed:
            raise OkxTradeError(
                "Live order execution is blocked by the independent safety gate."
            )
        if not self.configured:
            raise OkxTradeError("OKX credentials are not configured.")
