"""TradingView webhook normalization for the server-side execution boundary."""

import hashlib
import json
import math
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .trading_signal import TradeSignal

MAX_BODY_BYTES = 16 * 1024


class TradingViewWebhookError(ValueError):
    """A webhook cannot be converted into a safe internal signal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TradingViewPayload(BaseModel):
    """Accepted alert fields.

    Unknown fields are ignored and never persisted or sent to the exchange.
    """

    model_config = ConfigDict(extra="ignore", allow_inf_nan=False)

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
        "symbols": sorted(_allowed_symbols()),
        "max_age_seconds": _max_age_seconds(),
        "signal_ttl_seconds": _ttl_seconds(),
    }


def parse_body(body: bytes) -> TradingViewPayload:
    if len(body) > MAX_BODY_BYTES:
        raise TradingViewWebhookError("payload_too_large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise TradingViewWebhookError("duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise TradingViewWebhookError("invalid_json")

    try:
        decoded = json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
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
    return bool(expected and provided and secrets.compare_digest(provided.encode(), expected.encode()))


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
    raw = os.getenv("TRADINGVIEW_SYMBOLS", os.getenv("MARKET_SYMBOLS", "BTC-USDT-SWAP,ETH-USDT-SWAP"))
    return {item.strip().upper() for item in raw.split(",") if item.strip()}


def _alert_id(payload: TradingViewPayload, body: bytes) -> str:
    value = (payload.alert_id or payload.id or "").strip()
    if value:
        return value
    raise TradingViewWebhookError("alert_id_required")


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
    except ValueError as exc:
        raise TradingViewWebhookError("invalid_default_size") from exc
    if not math.isfinite(value) or value <= 0:
        raise TradingViewWebhookError("invalid_default_size")
    return value


def _check_timestamp(payload: TradingViewPayload, now: datetime) -> None:
    if payload.timestamp is None:
        raise TradingViewWebhookError("timestamp_required")
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
    if not allowed:
        raise TradingViewWebhookError("symbol_allowlist_empty")
    if instrument not in allowed:
        raise TradingViewWebhookError("symbol_not_allowed")
    if payload.action == "close" and payload.side is None:
        raise TradingViewWebhookError("close_side_required")
    expected_side = {"open_long": "buy", "open_short": "sell"}.get(payload.action)
    if expected_side and payload.side is not None and payload.side != expected_side:
        raise TradingViewWebhookError("side_action_mismatch")
    try:
        expires_at = min(
            current + timedelta(seconds=_ttl_seconds()),
            payload.timestamp + timedelta(seconds=_max_age_seconds()),
        )
        if payload.expires_at is not None:
            if payload.expires_at.tzinfo is None:
                raise TradingViewWebhookError("timestamp_timezone_required")
            expires_at = min(expires_at, payload.expires_at)
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
    except TradingViewWebhookError:
        raise
    except ValueError as exc:
        raise TradingViewWebhookError("invalid_signal") from exc
    size = payload.size if payload.size is not None else _default_size()
    return signal, size, payload.side if payload.action == "close" else None, _alert_id(payload, body)


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
        except ValueError as exc:
            raise TradingViewWebhookError("invalid_simulation_context") from exc
        if not math.isfinite(number):
            raise TradingViewWebhookError("invalid_simulation_context")
        return number

    context = {
        "account_equity": value(
            "TRADINGVIEW_ACCOUNT_EQUITY",
            os.getenv("AUTO_TRADING_ACCOUNT_EQUITY", "1000"),
        ),
        "daily_pnl_pct": value("TRADINGVIEW_DAILY_PNL_PCT", "0"),
        "current_notional": value("TRADINGVIEW_CURRENT_NOTIONAL", "0"),
    }
    if context["account_equity"] <= 0 or context["current_notional"] < 0:
        raise TradingViewWebhookError("invalid_simulation_context")
    return context


def instruction_fingerprint(payload: TradingViewPayload, signal: TradeSignal, size: float, side: str | None) -> str:
    instruction = {
        **signal.model_dump(mode="json", exclude={"created_at", "expires_at"}),
        "size": size, "side": side, "dry_run": payload.dry_run,
        "timestamp": payload.timestamp.astimezone(timezone.utc).isoformat(),
        "expires_at": payload.expires_at.astimezone(timezone.utc).isoformat() if payload.expires_at else None,
    }
    return hashlib.sha256(json.dumps(instruction, sort_keys=True, allow_nan=False).encode()).hexdigest()
