"""TradingView webhook normalization for the server-side execution boundary."""

import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .trading_signal import TradeSignal


class TradingViewWebhookError(ValueError):
    """A webhook cannot be converted into a safe internal signal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TradingViewPayload(BaseModel):
    """Accepted alert fields.

    TradingView alert messages are user-authored JSON, so unknown fields are
    retained by Pydantic but never forwarded to the exchange.
    """

    model_config = ConfigDict(extra="allow", allow_inf_nan=False)

    secret: str | None = Field(default=None, min_length=1, max_length=256)
    token: str | None = Field(default=None, min_length=1, max_length=256)
    alert_id: str | None = Field(default=None, min_length=1, max_length=128)
    id: str | None = Field(default=None, min_length=1, max_length=128)
    inst_id: str | None = Field(default=None, min_length=3, max_length=40)
    symbol: str | None = Field(default=None, min_length=1, max_length=100)
    ticker: str | None = Field(default=None, min_length=1, max_length=100)
    action: Literal["open_long", "open_short", "close", "hold"]
    side: Literal["buy", "sell"] | None = None
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    leverage: float = Field(default=2.0, ge=1.0, le=125.0)
    position_pct: float = Field(default=5.0, ge=0.0, le=100.0)
    entry_price: float | None = Field(default=None, gt=0.0)
    stop_loss: float | None = Field(default=None, gt=0.0)
    take_profit: float | None = Field(default=None, gt=0.0)
    size: float | None = Field(default=None, gt=0.0)
    dry_run: bool | None = None
    timestamp: datetime | None = None
    expires_at: datetime | None = None


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() == "true"


def enabled() -> bool:
    return _truthy("TRADINGVIEW_ENABLED")


def configured() -> bool:
    return enabled() and bool(os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "").strip())


def status() -> dict[str, object]:
    execution_enabled = _truthy("TRADINGVIEW_EXECUTION_ENABLED")
    configured_now = configured()
    return {
        "enabled": enabled(),
        "configured": configured_now,
        "execution_enabled": execution_enabled,
        "dry_run": not execution_enabled or _truthy("TRADINGVIEW_DRY_RUN", "true"),
    }


def parse_body(body: bytes) -> TradingViewPayload:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TradingViewWebhookError("invalid_json") from exc
    if not isinstance(decoded, dict):
        raise TradingViewWebhookError("json_object_required")
    try:
        return TradingViewPayload.model_validate(decoded)
    except ValueError as exc:
        raise TradingViewWebhookError("invalid_payload") from exc


def verify_secret(payload: TradingViewPayload, header_token: str | None) -> bool:
    expected = os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "").strip()
    provided = (header_token or payload.secret or payload.token or "").strip()
    return bool(expected and provided and secrets.compare_digest(provided, expected))


def _base_symbol(raw: str) -> str:
    value = raw.strip().upper().split(":")[-1]
    value = re.sub(r"(\.P|\.PERP|PERPETUAL)$", "", value)
    value = re.sub(r"[-_/](PERP|PERPETUAL)$", "", value)
    return value


def normalize_instrument(raw: str | None) -> str:
    if not raw:
        raise TradingViewWebhookError("symbol_required")
    value = _base_symbol(raw)
    if re.fullmatch(r"[A-Z0-9]+-[A-Z0-9]+-SWAP", value):
        return value
    for quote in ("USDT", "USDC", "USD"):
        if value.endswith(quote) and len(value) > len(quote):
            base = value[: -len(quote)]
            if re.fullmatch(r"[A-Z0-9]+", base):
                return f"{base}-{quote}-SWAP"
    raise TradingViewWebhookError("unsupported_symbol")


def _allowed_symbols() -> set[str]:
    raw = os.getenv("TRADINGVIEW_SYMBOLS", os.getenv("MARKET_SYMBOLS", ""))
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def _alert_id(payload: TradingViewPayload, body: bytes) -> str:
    value = (payload.alert_id or payload.id or "").strip()
    if value:
        return value
    return hashlib.sha256(body).hexdigest()[:32]


def _ttl_seconds() -> int:
    try:
        return max(15, min(3600, int(os.getenv("TRADINGVIEW_SIGNAL_TTL_SECONDS", "300"))))
    except ValueError:
        return 300


def _max_age_seconds() -> int:
    try:
        return max(15, min(3600, int(os.getenv("TRADINGVIEW_MAX_AGE_SECONDS", "300"))))
    except ValueError:
        return 300


def _default_size() -> float:
    try:
        value = float(os.getenv("TRADINGVIEW_DEFAULT_SIZE", "1"))
    except ValueError:
        value = 1.0
    return value if value > 0 else 1.0


def _check_timestamp(payload: TradingViewPayload, now: datetime) -> None:
    if payload.timestamp is None:
        return
    timestamp = payload.timestamp
    if timestamp.tzinfo is None:
        raise TradingViewWebhookError("timestamp_timezone_required")
    age = (now - timestamp.astimezone(timezone.utc)).total_seconds()
    if age < -30 or age > _max_age_seconds():
        raise TradingViewWebhookError("alert_too_old")


def to_signal(
    payload: TradingViewPayload,
    *,
    body: bytes,
    now: datetime | None = None,
) -> tuple[TradeSignal, float, str | None, str]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    _check_timestamp(payload, current)
    instrument = normalize_instrument(payload.inst_id or payload.symbol or payload.ticker)
    allowed = _allowed_symbols()
    if allowed and instrument not in allowed:
        raise TradingViewWebhookError("symbol_not_allowed")
    if payload.action == "close" and payload.side is None:
        raise TradingViewWebhookError("close_side_required")
    try:
        expires_at = payload.expires_at or current + timedelta(seconds=_ttl_seconds())
        signal = TradeSignal(
            inst_id=instrument,
            action=payload.action,
            confidence=payload.confidence,
            leverage=payload.leverage,
            position_pct=payload.position_pct,
            entry_price=payload.entry_price,
            stop_loss=payload.stop_loss,
            take_profit=payload.take_profit,
            source="tradingview",
            created_at=current,
            expires_at=expires_at,
        )
    except ValueError as exc:
        raise TradingViewWebhookError("invalid_signal") from exc
    size = payload.size if payload.size is not None else _default_size()
    return signal, size, payload.side, _alert_id(payload, body)


def execution_dry_run(payload: TradingViewPayload) -> bool:
    if not _truthy("TRADINGVIEW_EXECUTION_ENABLED"):
        return True
    if _truthy("TRADINGVIEW_DRY_RUN", "true"):
        return True
    return payload.dry_run is True


def numeric_context() -> dict[str, float]:
    def value(name: str, fallback: str) -> float:
        try:
            number = float(os.getenv(name, fallback))
        except ValueError:
            number = float(fallback)
        return number

    return {
        "account_equity": value(
            "TRADINGVIEW_ACCOUNT_EQUITY",
            os.getenv("AUTO_TRADING_ACCOUNT_EQUITY", "1000"),
        ),
        "daily_pnl_pct": value("TRADINGVIEW_DAILY_PNL_PCT", "0"),
        "current_notional": value("TRADINGVIEW_CURRENT_NOTIONAL", "0"),
    }
