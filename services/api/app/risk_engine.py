import os
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .trading_signal import TradeSignal


@dataclass(frozen=True)
class RiskLimits:
    max_leverage: float = 3.0
    max_position_pct: float = 10.0
    max_total_notional_pct: float = 30.0
    min_confidence: float = 0.65
    max_daily_loss_pct: float = 3.0
    max_stop_distance_pct: float = 5.0

    def __post_init__(self) -> None:
        values = (
            self.max_leverage, self.max_position_pct, self.max_total_notional_pct,
            self.min_confidence, self.max_daily_loss_pct, self.max_stop_distance_pct,
        )
        if not all(math.isfinite(value) for value in values):
            raise ValueError("Risk limits must be finite")
        if not all(value > 0 for value in (
            self.max_leverage, self.max_position_pct, self.max_total_notional_pct,
            self.max_daily_loss_pct, self.max_stop_distance_pct,
        )):
            raise ValueError("Risk limits must be positive")
        if not 0 <= self.min_confidence <= 1 or not 1 <= self.max_leverage <= 125:
            raise ValueError("Risk confidence or leverage is outside the valid range")

    @classmethod
    def from_env(cls) -> "RiskLimits":
        return cls(
            max_leverage=float(os.getenv("RISK_MAX_LEVERAGE", "3")),
            max_position_pct=float(os.getenv("RISK_MAX_POSITION_PCT", "10")),
            max_total_notional_pct=float(
                os.getenv("RISK_MAX_TOTAL_NOTIONAL_PCT", "30")
            ),
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
        market_data_fresh: bool = True,
        order_notional: float | None = None,
        verified_close: bool = False,
    ) -> RiskDecision:
        reasons: list[str] = []
        now = datetime.now(timezone.utc)
        if not all(math.isfinite(value) for value in (
            account_equity, daily_pnl_pct, current_notional,
        )):
            reasons.append("risk_context_not_finite")
        if not market_data_fresh:
            reasons.append("market_data_stale")
        if signal.action == "hold":
            reasons.append("hold_signal")
        if signal.expires_at and signal.expires_at <= now:
            reasons.append("signal_expired")
        if account_equity <= 0:
            reasons.append("account_equity_invalid")
        reducing = signal.action == "close" and verified_close
        if daily_pnl_pct <= -self.limits.max_daily_loss_pct and not reducing:
            reasons.append("daily_loss_limit_reached")
        if signal.confidence < self.limits.min_confidence and not reducing:
            reasons.append("confidence_below_threshold")
        if signal.leverage > self.limits.max_leverage and not reducing:
            reasons.append("leverage_above_limit")
        if signal.action in {"open_long", "open_short"}:
            if signal.position_pct > self.limits.max_position_pct:
                reasons.append("position_size_above_limit")
            proposed_notional = (
                account_equity * signal.position_pct / 100 * signal.leverage
            )
            if order_notional is not None:
                if not math.isfinite(order_notional) or order_notional <= 0:
                    reasons.append("order_notional_invalid")
                elif order_notional > proposed_notional + 1e-8:
                    reasons.append("order_notional_above_signal_budget")
                proposed_notional = order_notional
            max_total_notional = (
                account_equity * self.limits.max_total_notional_pct / 100
            )
            if current_notional + proposed_notional > max_total_notional:
                reasons.append("total_exposure_above_limit")
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
