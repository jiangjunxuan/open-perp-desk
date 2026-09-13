import asyncio
import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from urllib.parse import urlencode

import httpx


class OkxAccountError(RuntimeError):
    """Raised when a signed OKX account request cannot be completed."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


class OkxAccountClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = os.getenv("OKX_REST_BASE_URL", "https://www.okx.com").rstrip("/")
        self.api_key = os.getenv("OKX_API_KEY", "").strip()
        self.secret_key = os.getenv("OKX_SECRET_KEY", "").strip()
        self.passphrase = os.getenv("OKX_PASSPHRASE", "").strip()
        self.proxy_url = os.getenv("OKX_PROXY_URL", "").strip() or None
        self.demo = os.getenv("OKX_DEMO", "true").lower() == "true"
        self.transport = transport
        self._bill_request_lock = asyncio.Lock()
        self._last_bill_request = 0.0
        self._archive_request_lock = asyncio.Lock()
        self._last_archive_request = 0.0
        self._order_request_lock = asyncio.Lock()
        self._last_order_request = 0.0

    @property
    def configured(self) -> bool:
        return all((self.api_key, self.secret_key, self.passphrase))

    @property
    def account_scope(self) -> str:
        identity = f"{self.base_url}\n{self.demo}\n{self.api_key}"
        return hashlib.sha256(identity.encode()).hexdigest()

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
        if path == "/api/v5/account/bills":
            async with self._bill_request_lock:
                loop = asyncio.get_running_loop()
                delay = 0.21 - (loop.time() - self._last_bill_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_bill_request = loop.time()
        elif path == "/api/v5/account/bills-archive":
            async with self._archive_request_lock:
                loop = asyncio.get_running_loop()
                delay = 0.41 - (loop.time() - self._last_archive_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_archive_request = loop.time()
        elif path.startswith("/api/v5/trade/"):
            async with self._order_request_lock:
                loop = asyncio.get_running_loop()
                delay = 0.11 - (loop.time() - self._last_order_request)
                if delay > 0:
                    await asyncio.sleep(delay)
                self._last_order_request = loop.time()
        timestamp = self.timestamp()
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy_url,
                transport=self.transport,
                timeout=httpx.Timeout(10.0, connect=5.0),
                headers=self._headers(timestamp, request_path),
            ) as client:
                response = await client.get(f"{self.base_url}{path}", params=params)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OkxAccountError(f"OKX account request failed: {type(exc).__name__}") from exc

        if not isinstance(payload, dict):
            raise OkxAccountError("Invalid OKX account response")
        if payload.get("code") != "0":
            raise OkxAccountError(
                payload.get("msg") or "OKX returned an unknown error",
                code=str(payload.get("code") or ""),
            )
        data = payload.get("data")
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise OkxAccountError("Invalid OKX account data")
        return data

    async def balance(self) -> list[dict[str, Any]]:
        return await self._get("/api/v5/account/balance")

    async def positions(self) -> list[dict[str, Any]]:
        return await self._get("/api/v5/account/positions", {"instType": "SWAP"})

    async def config(self) -> list[dict[str, Any]]:
        return await self._get("/api/v5/account/config")

    async def pending_orders(self) -> list[dict[str, Any]]:
        return await self._all_pages(
            "/api/v5/trade/orders-pending",
            {"instType": "SWAP"},
            cursor_field="ordId",
        )

    async def _all_pages(
        self,
        path: str,
        params: dict[str, str],
        *,
        cursor_field: str,
        page_size: int = 100,
        on_page: Callable[[], Awaitable[None]] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        page_size = max(1, min(page_size, 100))
        query = {**params, "limit": str(page_size)}
        for _ in range(100):
            page = await self._get(path, query)
            if on_page is not None:
                await on_page()
            if len(page) > page_size:
                raise OkxAccountError("Invalid account pagination page size")
            for row in page:
                cursor = str(row.get(cursor_field) or "")
                if not cursor or cursor in seen:
                    raise OkxAccountError("Incomplete or repeated account pagination")
                seen.add(cursor)
                rows.append(row)
            if len(page) < page_size:
                return rows
            query["after"] = str(page[-1][cursor_field])
        raise OkxAccountError("Account pagination safety limit reached")

    async def fills_today(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        begin = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._all_pages(
            "/api/v5/trade/fills-history",
            {
                "instType": "SWAP",
                "begin": str(int(begin.timestamp() * 1000)),
                "end": str(int(now.timestamp() * 1000)),
            },
            cursor_field="billId",
        )

    async def bills_today(self, *, as_of: datetime | None = None) -> list[dict[str, Any]]:
        now = as_of or datetime.now(timezone.utc)
        if now.tzinfo is None:
            raise OkxAccountError("Bill snapshot timezone is required")
        now = now.astimezone(timezone.utc)
        begin = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return await self._all_pages(
            "/api/v5/account/bills",
            {
                "instType": "SWAP",
                "begin": str(int(begin.timestamp() * 1000)),
                "end": str(int(now.timestamp() * 1000)),
            },
            cursor_field="billId",
        )

    async def bills_archive(
        self, begin_ms: int, end_ms: int, *,
        on_page: Callable[[], Awaitable[None]] | None = None,
    ) -> list[dict[str, Any]]:
        if begin_ms <= 0 or end_ms <= begin_ms:
            raise OkxAccountError("Invalid archive window")
        # Do not filter by instType: funding-account transfers have no instrument.
        rows = await self._all_pages(
            "/api/v5/account/bills-archive",
            {"begin": str(begin_ms - 1), "end": str(end_ms)},
            cursor_field="billId", on_page=on_page,
        )
        selected = []
        for row in rows:
            try:
                timestamp = int(str(row.get("ts")))
            except (ValueError, TypeError) as exc:
                raise OkxAccountError("Invalid archive timestamp") from exc
            if not begin_ms - 1 <= timestamp <= end_ms:
                raise OkxAccountError("Archive response outside requested window")
            # Pad remote filters by one millisecond, then enforce [begin, end)
            # locally so endpoint inclusivity cannot drop a boundary record.
            if begin_ms <= timestamp < end_ms:
                selected.append(row)
        return selected

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
        *,
        ord_type: str = "conditional,oco",
    ) -> list[dict[str, Any]]:
        params = {
            "instType": "SWAP",
            "ordType": ord_type,
        }
        if inst_id:
            params["instId"] = inst_id
        return await self._all_pages(
            "/api/v5/trade/orders-algo-pending",
            params,
            cursor_field="algoId",
            page_size=limit,
        )

    async def active_algo_orders(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for ord_type in ("conditional,oco", "trigger", "move_order_stop", "iceberg", "twap", "chase", "smart_iceberg"):
            rows.extend(await self.pending_algo_orders(ord_type=ord_type))
        return rows

    async def algo_orders_history(
        self,
        inst_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        params = {
            "instType": "SWAP",
            "ordType": "conditional,oco",
        }
        if inst_id:
            params["instId"] = inst_id
        rows: list[dict[str, Any]] = []
        for state in ("effective", "canceled", "order_failed"):
            rows.extend(await self._all_pages(
                "/api/v5/trade/orders-algo-history",
                {**params, "state": state},
                cursor_field="algoId",
                page_size=limit,
            ))
        return rows

    async def order_details(
        self,
        inst_id: str,
        *,
        ord_id: str | None = None,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if not inst_id or not (ord_id or client_order_id):
            raise OkxAccountError("Order instrument and identity are required")
        params = {"instId": inst_id}
        if ord_id:
            params["ordId"] = ord_id
        else:
            params["clOrdId"] = str(client_order_id)
        rows = await self._get("/api/v5/trade/order", params)
        if len(rows) != 1:
            raise OkxAccountError("Exact order lookup did not return one order")
        row = rows[0]
        if row.get("instId") != inst_id or not row.get("ordId"):
            raise OkxAccountError("Order lookup identity mismatch")
        if ord_id and str(row["ordId"]) != ord_id:
            raise OkxAccountError("Order lookup exchange identity mismatch")
        if client_order_id and row.get("clOrdId") != client_order_id:
            raise OkxAccountError("Order lookup client identity mismatch")
        return row
