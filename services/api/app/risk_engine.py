import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .trading_signal import TradeSignal


@dataclass(frozen=True)
class RiskLimits:
    max_leverage: float = 3.0
    max_position_pct: float = 10.0
    min_confidence: float = 0.65
    max_daily_loss_pct: float = 3.0
    max_stop_distance_pct: float = 5.0

    @classmethod
    def from_env(cls) -> "RiskLimits":
        return cls(
            max_leverage=float(os.getenv("RISK_MAX_LEVERAGE", "3")),
            max_position_pct=float(os.getenv("RISK_MAX_POSITION_PCT", "10")),
            min_confidence=float(os.getenv("RISK_MIN_CONFIDENCE", "0.65")),
            max_daily_loss_pct=float(os.getenv("RISK_MAX_DAILY_LOSS_PCT", "3")),
            max_stop_distance_pct=float(
                os.getenv("RISK_MAX_STOP_DISTANCE_PCT", "5")
            ),
        )


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reasons: tuple[str, ...]
    checked_at: str
    signal: dict[str, Any]


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits.from_env()

    def evaluate(
        self,
        signal: TradeSignal,
        *,
        account_equity: float,
        daily_pnl_pct: float,
        current_notional: float = 0.0,
    ) -> RiskDecision:
        reasons: list[str] = []
        now = datetime.now(timezone.utc)
        if signal.action == "hold":
            reasons.append("hold_signal")
        if signal.expires_at and signal.expires_at <= now:
            reasons.append("signal_expired")
        if account_equity <= 0:
            reasons.append("account_equity_invalid")
        if daily_pnl_pct <= -self.limits.max_daily_loss_pct:
            reasons.append("daily_loss_limit_reached")
        if signal.confidence < self.limits.min_confidence:
            reasons.append("confidence_below_threshold")
        if signal.leverage > self.limits.max_leverage:
            reasons.append("leverage_above_limit")
        if signal.action in {"open_long", "open_short"}:
            if signal.position_pct > self.limits.max_position_pct:
                reasons.append("position_size_above_limit")
            if signal.entry_price and signal.stop_loss:
                distance_pct = (
                    abs(signal.entry_price - signal.stop_loss)
                    / signal.entry_price
                    * 100
                )
                if distance_pct > self.limits.max_stop_distance_pct:
                    reasons.append("stop_distance_above_limit")
        if current_notional < 0:
            reasons.append("current_notional_invalid")
        return RiskDecision(
            approved=not reasons,
            reasons=tuple(reasons),
            checked_at=datetime.now(timezone.utc).isoformat(),
            signal=signal.model_dump(mode="json"),
        )
