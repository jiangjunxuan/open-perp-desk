import os
from typing import Any

import httpx


class PushPlusError(RuntimeError):
    """Raised when a PushPlus notification cannot be delivered."""


class PushPlusClient:
    def __init__(self) -> None:
        self.base_url = os.getenv(
            "PUSHPLUS_BASE_URL",
            "https://www.pushplus.plus/send",
        )
        self.token = os.getenv("PUSHPLUS_TOKEN", "").strip()
        self.proxy_url = os.getenv("PUSHPLUS_PROXY_URL", "").strip() or None

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
            raise PushPlusError("PushPlus token is not configured")

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
                timeout=httpx.Timeout(10.0, connect=5.0),
            ) as client:
                response = await client.post(self.base_url, json=payload)
                response.raise_for_status()
                result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise PushPlusError(f"PushPlus request failed: {exc}") from exc

        if str(result.get("code")) != "200":
            raise PushPlusError(result.get("msg") or "PushPlus returned an unknown error")
        return {"code": result.get("code"), "msg": result.get("msg", "")}

