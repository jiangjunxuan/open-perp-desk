from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .trading_signal import TradeSignal


DEFAULT_STRATEGY_CONFIG: dict[str, Any] = {
    "fast_period": 9,
    "slow_period": 21,
    "rsi_period": 14,
    "long_rsi_min": 45.0,
    "long_rsi_max": 72.0,
    "short_rsi_min": 28.0,
    "short_rsi_max": 55.0,
    "stop_atr_multiplier": 1.5,
    "target_atr_multiplier": 2.5,
    "leverage": 2.0,
    "position_pct": 5.0,
    "signal_ttl_minutes": 5,
}


class StrategyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fast_period: int = Field(default=9, ge=2, le=100)
    slow_period: int = Field(default=21, ge=3, le=200)
    rsi_period: int = Field(default=14, ge=2, le=100)
    long_rsi_min: float = Field(default=45.0, ge=0.0, le=100.0)
    long_rsi_max: float = Field(default=72.0, ge=0.0, le=100.0)
    short_rsi_min: float = Field(default=28.0, ge=0.0, le=100.0)
    short_rsi_max: float = Field(default=55.0, ge=0.0, le=100.0)
    stop_atr_multiplier: float = Field(default=1.5, gt=0.0, le=20.0)
    target_atr_multiplier: float = Field(default=2.5, gt=0.0, le=50.0)
    leverage: float = Field(default=2.0, ge=1.0, le=125.0)
    position_pct: float = Field(default=5.0, ge=0.0, le=100.0)
    signal_ttl_minutes: int = Field(default=5, ge=1, le=1440)

    @model_validator(mode="after")
    def validate_periods_and_ranges(self) -> "StrategyConfig":
        if self.fast_period >= self.slow_period:
            raise ValueError("fast_period must be less than slow_period")
        if self.long_rsi_min >= self.long_rsi_max:
            raise ValueError("long RSI range is invalid")
        if self.short_rsi_min >= self.short_rsi_max:
            raise ValueError("short RSI range is invalid")
        return self


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _sma(values: list[float], period: int) -> float:
    return _mean(values[-period:]) if values else 0.0


def _rsi(values: list[float], period: int = 14) -> float:
    changes = [values[index] - values[index - 1] for index in range(1, len(values))]
    window = changes[-period:]
    gains = [max(change, 0.0) for change in window]
    losses = [abs(min(change, 0.0)) for change in window]
    average_gain = _mean(gains)
    average_loss = _mean(losses)
    if average_loss == 0:
        return 100.0 if average_gain else 50.0
    return 100 - (100 / (1 + average_gain / average_loss))


def _atr(rows: list[list[str]], period: int = 14) -> float:
    true_ranges: list[float] = []
    previous_close: float | None = None
    for row in rows:
        try:
            high, low, close = float(row[2]), float(row[3]), float(row[4])
        except (IndexError, TypeError, ValueError):
            continue
        ranges = [high - low]
        if previous_close is not None:
            ranges.extend([abs(high - previous_close), abs(low - previous_close)])
        true_ranges.append(max(ranges))
        previous_close = close
    return _mean(true_ranges[-period:])


class StrategyEngine:
    """Deterministic baseline strategy used when no LLM is available.

    It is intentionally explainable and emits the same structured signal shape
    consumed by the risk and execution layers.
    """

    @staticmethod
    def normalize_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
        merged = dict(DEFAULT_STRATEGY_CONFIG)
        if config:
            merged.update(config)
        return StrategyConfig.model_validate(merged).model_dump(mode="json")

    def analyze(
        self,
        inst_id: str,
        rows: list[list[str]],
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        settings = self.normalize_config(config)
        fast_period = settings["fast_period"]
        slow_period = settings["slow_period"]
        rsi_period = settings["rsi_period"]
        chronological = list(reversed(rows))
        closes: list[float] = []
        for row in chronological:
            try:
                closes.append(float(row[4]))
            except (IndexError, TypeError, ValueError):
                continue
        if len(closes) < max(20, slow_period):
            raise ValueError(
                f"at least {max(20, slow_period)} valid candles are required"
            )

        last = closes[-1]
        fast = _sma(closes, fast_period)
        slow = _sma(closes, slow_period)
        rsi = _rsi(closes, rsi_period)
        atr = _atr(chronological, rsi_period) or last * 0.005
        trend_strength = abs(fast - slow) / last if last else 0.0

        if fast > slow and settings["long_rsi_min"] <= rsi <= settings["long_rsi_max"]:
            action = "open_long"
            bias = "bullish"
        elif fast < slow and settings["short_rsi_min"] <= rsi <= settings["short_rsi_max"]:
            action = "open_short"
            bias = "bearish"
        else:
            action = "hold"
            bias = "neutral"

        confidence = min(0.95, 0.55 + trend_strength * 8 + min(abs(rsi - 50), 25) / 100)
        stop_distance = max(
            atr * settings["stop_atr_multiplier"],
            last * 0.003,
        )
        target_distance = max(
            atr * settings["target_atr_multiplier"],
            last * 0.006,
        )
        if action == "open_long":
            stop_loss, take_profit = last - stop_distance, last + target_distance
        elif action == "open_short":
            stop_loss, take_profit = last + stop_distance, last - target_distance
        else:
            stop_loss = take_profit = None

        created_at = datetime.now(timezone.utc)
        expires_at = created_at + timedelta(minutes=settings["signal_ttl_minutes"])
        signal = TradeSignal(
            inst_id=inst_id,
            action=action,
            confidence=round(confidence, 4),
            leverage=settings["leverage"],
            position_pct=settings["position_pct"],
            entry_price=last if action != "hold" else None,
            stop_loss=stop_loss,
            take_profit=take_profit,
            source="structured-technical",
            created_at=created_at,
            expires_at=expires_at,
        )
        return {
            "inst_id": inst_id,
            "source": "structured-technical",
            "bias": bias,
            "generated_at": created_at.isoformat(),
            "config": settings,
            "indicators": {
                "last": round(last, 8),
                "sma_9": round(fast, 8),
                "sma_21": round(slow, 8),
                "rsi_14": round(rsi, 4),
                "atr_14": round(atr, 8),
                "sma_fast": round(fast, 8),
                "sma_slow": round(slow, 8),
                "rsi": round(rsi, 4),
                "atr": round(atr, 8),
                "trend_strength": round(trend_strength, 8),
            },
            "signal": signal.model_dump(mode="json"),
            "report": {
                "summary": (
                    f"{inst_id} 当前为{bias}结构，9/21均线与 RSI 共同决定基础方向。"
                ),
                "risk_note": "该信号只用于研究和风控预检，不能替代人工审查。",
            },
        }
