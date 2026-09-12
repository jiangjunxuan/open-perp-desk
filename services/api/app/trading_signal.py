from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, model_validator


SignalAction = Literal["open_long", "open_short", "close", "hold"]


class TradeSignal(BaseModel):
    """The only signal shape accepted by the future execution layer."""

    inst_id: str = Field(min_length=3, max_length=40, pattern=r"^[A-Z0-9-]+$")
    action: SignalAction
    confidence: float = Field(ge=0.0, le=1.0)
    leverage: float = Field(ge=1.0, le=125.0)
    position_pct: float = Field(ge=0.0, le=100.0)
    entry_price: float | None = Field(default=None, gt=0.0)
    stop_loss: float | None = Field(default=None, gt=0.0)
    take_profit: float | None = Field(default=None, gt=0.0)
    source: str = Field(default="manual", min_length=1, max_length=40)
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_price_direction(self) -> "TradeSignal":
        if self.expires_at and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        if self.action in {"open_long", "open_short"}:
            if self.entry_price is None or self.stop_loss is None or self.take_profit is None:
                raise ValueError(
                    "open signals require entry_price, stop_loss, and take_profit"
                )
            if self.action == "open_long" and not (
                self.stop_loss < self.entry_price < self.take_profit
            ):
                raise ValueError("long prices must satisfy stop_loss < entry < take_profit")
            if self.action == "open_short" and not (
                self.take_profit < self.entry_price < self.stop_loss
            ):
                raise ValueError("short prices must satisfy take_profit < entry < stop_loss")
        return self

