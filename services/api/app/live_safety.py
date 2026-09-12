import os
import secrets
from datetime import datetime, timezone
from typing import Any


class LiveSafetyGate:
    """Fail-closed, process-local gate for live order submission."""

    def __init__(self) -> None:
        self._unlocked = False
        self._unlocked_at: str | None = None

    @property
    def configuration_enabled(self) -> bool:
        return os.getenv("LIVE_TRADING_ENABLED", "false").lower() == "true"

    @property
    def mode_is_live(self) -> bool:
        return (
            os.getenv("TRADING_MODE", "demo").lower() == "live"
            and os.getenv("OKX_DEMO", "true").lower() == "false"
        )

    @property
    def allowed(self) -> bool:
        return (
            self.configuration_enabled
            and self.mode_is_live
            and os.getenv("EXECUTION_ENABLED", "false").lower() == "true"
            and self._unlocked
        )

    def unlock(self, phrase: str) -> bool:
        expected = os.getenv("LIVE_UNLOCK_PHRASE", "").strip()
        if not expected or not phrase or not secrets.compare_digest(phrase, expected):
            self.lock()
            return False
        self._unlocked = True
        self._unlocked_at = datetime.now(timezone.utc).isoformat()
        return True

    def lock(self) -> None:
        self._unlocked = False
        self._unlocked_at = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "configuration_enabled": self.configuration_enabled,
            "mode_is_live": self.mode_is_live,
            "unlocked": self._unlocked,
            "allowed": self.allowed,
            "unlocked_at": self._unlocked_at,
            "reason": (
                "Live orders require LIVE_TRADING_ENABLED, live OKX mode, "
                "EXECUTION_ENABLED, and an in-memory manual unlock."
            ),
        }
