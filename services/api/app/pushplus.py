import json
import os
import re
from typing import Any

import httpx

from .http_transport import proxy_cleanup_trace


class PushPlusError(RuntimeError):
    """Safe failure metadata; upstream messages and URLs never leave this client."""

    CODES = {"pushplus_unconfigured", "pushplus_rejected", "pushplus_acceptance_unknown"}

    def __init__(self, code: str = "pushplus_acceptance_unknown") -> None:
        self.code = code if code in self.CODES else "pushplus_acceptance_unknown"
        self.acceptance_unknown = self.code == "pushplus_acceptance_unknown"
        super().__init__(self.code)


class PushPlusClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = os.getenv(
            "PUSHPLUS_BASE_URL",
            "https://www.pushplus.plus/send",
        )
        self.token = os.getenv("PUSHPLUS_TOKEN", "").strip()
        self.proxy_url = os.getenv("PUSHPLUS_PROXY_URL", "").strip() or None
        self.transport = transport

    @property
    def configured(self) -> bool:
        return bool(self.token)

    async def send(
        self,
        title: str,
        content: str,
        template: str = "markdown",
        topic: str | None = None,
    ) -> dict[str, Any]:
        if not self.configured:
            raise PushPlusError("pushplus_unconfigured")

        payload: dict[str, str] = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": template,
        }
        if topic:
            payload["topic"] = topic
        try:
            async with httpx.AsyncClient(
                proxy=self.proxy_url,
                transport=self.transport,
                timeout=httpx.Timeout(10.0, connect=5.0),
            ) as client:
                async with client.stream(
                    "POST", self.base_url, json=payload,
                    extensions={"trace": proxy_cleanup_trace()},
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes(8192):
                        body.extend(chunk)
                        if len(body) > 65_536:
                            raise PushPlusError("pushplus_acceptance_unknown")
                    result = json.loads(body)
        except (httpx.HTTPError, ValueError):
            # A failed POST response is not proof that the provider rejected it.
            raise PushPlusError("pushplus_acceptance_unknown") from None

        if not isinstance(result, dict) or type(result.get("code")) not in {int, str}:
            raise PushPlusError("pushplus_acceptance_unknown")
        if not re.fullmatch(r"[0-9]{3}", str(result["code"])):
            raise PushPlusError("pushplus_acceptance_unknown")
        if str(result["code"]) != "200":
            raise PushPlusError("pushplus_rejected")
        message_id = result.get("data")
        if message_id not in (None, "") and (
            not isinstance(message_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", message_id)
            or message_id.casefold() == self.token.casefold()
        ):
            raise PushPlusError("pushplus_acceptance_unknown")
        return {
            "code": result["code"], "msg": "request_accepted", "accepted": True,
            "delivery_confirmed": False, "message_id": message_id or None,
        }
