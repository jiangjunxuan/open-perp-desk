from typing import Any

from .state_store import StateStore


class SafetyController:
    """Persistent emergency-stop controller shared by every execution path."""

    FLAG = "emergency_stop"

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @property
    def emergency_stopped(self) -> bool:
        return self.store.get_control_flag(self.FLAG)

    @property
    def execution_allowed(self) -> bool:
        return not self.emergency_stopped

    def stop(self, reason: str = "manual emergency stop") -> None:
        self.store.set_control_flag(self.FLAG, True, reason)

    def resume(self, reason: str = "manual resume") -> None:
        self.store.set_control_flag(self.FLAG, False, reason)

    def snapshot(self) -> dict[str, Any]:
        flag = self.store.get_control_flags().get(
            self.FLAG,
            {"value": False, "reason": "", "updated_at": None},
        )
        return {
            "emergency_stopped": bool(flag["value"]),
            "execution_allowed": not bool(flag["value"]),
            "reason": flag["reason"],
            "updated_at": flag["updated_at"],
        }
