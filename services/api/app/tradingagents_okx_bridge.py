"""OKX-first data bridge for the isolated TradingAgents research process.

TradingAgents is designed around stock-market vendors. OpenPerpDesk already
has an authoritative OKX snapshot, so crypto research must use that snapshot
instead of making a second Yahoo/social-data request with different freshness
and instrument semantics.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import datetime, timezone
from typing import Any


VENDOR_NAME = "openperpdesk_okx"
_CONTEXT: dict[str, Any] = {}


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _fmt(value: float | None, digits: int = 8) -> str:
    if value is None or not math.isfinite(value):
        return "N/A"
    return f"{value:.{digits}f}".rstrip("0").rstrip(".")


def _timestamp(value: Any) -> datetime | None:
    raw = _number(value)
    if raw is None:
        return None
    if raw > 100_000_000_000:
        raw /= 1000
    try:
        return datetime.fromtimestamp(raw, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _rows() -> list[dict[str, Any]]:
    raw_rows = _CONTEXT.get("candles")
    if not isinstance(raw_rows, list):
        return []
    parsed: list[dict[str, Any]] = []
    for raw in raw_rows:
        if not isinstance(raw, (list, tuple)) or len(raw) < 6:
            continue
        dt = _timestamp(raw[0])
        values = [_number(item) for item in raw[1:6]]
        if dt is None or any(item is None for item in values):
            continue
        parsed.append({
            "ts": int(_number(raw[0]) or 0),
            "dt": dt,
            "open": values[0],
            "high": values[1],
            "low": values[2],
            "close": values[3],
            "volume": values[4],
            "confirm": str(raw[8]) if len(raw) > 8 else "",
        })
    parsed.sort(key=lambda item: item["ts"])
    return parsed


def _symbol_matches(symbol: str) -> bool:
    expected = str(_CONTEXT.get("inst_id") or "").upper()
    candidate = str(symbol or "").upper()
    if not expected:
        return False
    base = expected.split("-", 1)[0]
    return candidate in {expected, f"{base}-USD", f"{base}-USDT", base}


def _unavailable(kind: str) -> str:
    return (
        f"DATA_UNAVAILABLE: {kind} is not connected for this crypto run. "
        "Use the verified OKX perpetual snapshot and do not fabricate values."
    )


def _sma(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0:
        return result
    total = 0.0
    for index, value in enumerate(values):
        total += value
        if index >= period:
            total -= values[index - period]
        if index + 1 >= period:
            result[index] = total / period
    return result


def _ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if not values or period <= 0:
        return result
    alpha = 2.0 / (period + 1)
    current = values[0]
    result[0] = current
    for index in range(1, len(values)):
        current = alpha * values[index] + (1 - alpha) * current
        result[index] = current
    return result


def _rsi(values: list[float], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains: list[float] = []
    losses: list[float] = []
    for index in range(1, len(values)):
        change = values[index] - values[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value() -> float:
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    result[period] = value()
    for index in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[index - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index - 1]) / period
        result[index] = value()
    return result


def _bollinger(values: list[float], period: int = 20, width: float = 2.0) -> tuple[
    list[float | None], list[float | None], list[float | None]
]:
    middle = _sma(values, period)
    upper: list[float | None] = [None] * len(values)
    lower: list[float | None] = [None] * len(values)
    for index in range(period - 1, len(values)):
        window = values[index - period + 1:index + 1]
        mean = middle[index]
        if mean is None:
            continue
        deviation = math.sqrt(sum((item - mean) ** 2 for item in window) / period)
        upper[index] = mean + width * deviation
        lower[index] = mean - width * deviation
    return middle, upper, lower


def _atr(rows: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    if not rows:
        return []
    true_ranges: list[float] = []
    for index, row in enumerate(rows):
        previous_close = rows[index - 1]["close"] if index else row["close"]
        true_ranges.append(max(
            row["high"] - row["low"],
            abs(row["high"] - previous_close),
            abs(row["low"] - previous_close),
        ))
    return _sma(true_ranges, period)


def _mfi(rows: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    result: list[float | None] = [None] * len(rows)
    typical = [
        (row["high"] + row["low"] + row["close"]) / 3 for row in rows
    ]
    flows = [typical[index] * rows[index]["volume"] for index in range(len(rows))]
    for index in range(period, len(rows)):
        positive = 0.0
        negative = 0.0
        for cursor in range(index - period + 1, index + 1):
            if cursor == 0 or typical[cursor] >= typical[cursor - 1]:
                positive += flows[cursor]
            else:
                negative += flows[cursor]
        result[index] = 100.0 if negative == 0 else 100.0 - 100.0 / (1 + positive / negative)
    return result


def _indicator_series(rows: list[dict[str, Any]]) -> dict[str, list[float | None]]:
    closes = [row["close"] for row in rows]
    volumes = [row["volume"] for row in rows]
    ema10 = _ema(closes, 10)
    sma50 = _sma(closes, 50)
    sma200 = _sma(closes, 200)
    rsi = _rsi(closes)
    boll, boll_ub, boll_lb = _bollinger(closes)
    macd_fast = _ema(closes, 12)
    macd_slow = _ema(closes, 26)
    macd = [
        (fast - slow) if fast is not None and slow is not None else None
        for fast, slow in zip(macd_fast, macd_slow)
    ]
    macd_values = [value or 0.0 for value in macd]
    macds = _ema(macd_values, 9)
    macdh = [
        (value - signal) if value is not None and signal is not None else None
        for value, signal in zip(macd, macds)
    ]
    atr = _atr(rows)
    vwma20: list[float | None] = [None] * len(rows)
    for index in range(19, len(rows)):
        price_volume = sum(
            closes[cursor] * volumes[cursor]
            for cursor in range(index - 19, index + 1)
        )
        volume = sum(volumes[index - 19:index + 1])
        vwma20[index] = price_volume / volume if volume else None
    return {
        "close_10_ema": ema10,
        "close_50_sma": sma50,
        "close_200_sma": sma200,
        "rsi": rsi,
        "boll": boll,
        "boll_ub": boll_ub,
        "boll_lb": boll_lb,
        "macd": macd,
        "macds": macds,
        "macdh": macdh,
        "atr": atr,
        "vwma": vwma20,
        "mfi": _mfi(rows),
    }


def _latest_value(series: list[float | None]) -> float | None:
    for value in reversed(series):
        if value is not None:
            return value
    return None


def _stock_data(symbol: str, start_date: str, end_date: str) -> str:
    if not _symbol_matches(symbol):
        return _unavailable(f"requested instrument {symbol}")
    rows = _rows()
    if not rows:
        return _unavailable("OKX candles")
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([f"# OKX perpetual data for { _CONTEXT.get('inst_id', symbol) }"])
    writer.writerow([f"# Requested window: {start_date}..{end_date}"])
    writer.writerow(["Date", "Open", "High", "Low", "Close", "Volume"])
    for row in rows[-300:]:
        writer.writerow([
            row["dt"].isoformat(),
            _fmt(row["open"]), _fmt(row["high"]), _fmt(row["low"]),
            _fmt(row["close"]), _fmt(row["volume"]),
        ])
    return output.getvalue()


def _indicators(symbol: str, indicator: str, curr_date: str, look_back_days: int = 30) -> str:
    if not _symbol_matches(symbol):
        return _unavailable(f"requested instrument {symbol}")
    rows = _rows()
    if not rows:
        return _unavailable("OKX candles")
    name = str(indicator or "").strip().lower()
    series = _indicator_series(rows).get(name)
    if series is None:
        return f"Indicator {name} is not supported by the OKX bridge."
    count = max(1, min(int(look_back_days or 30), len(rows), 60))
    lines = [f"## OKX {name} values for {curr_date} (latest {count} candles)", ""]
    for row, value in zip(rows[-count:], series[-count:]):
        lines.append(f"{row['dt'].isoformat()}: {_fmt(value)}")
    lines.append("")
    lines.append("Values are calculated from the captured OKX perpetual candles.")
    return "\n".join(lines)


def _verified_snapshot(symbol: str, curr_date: str, look_back_days: int = 30) -> str:
    if not _symbol_matches(symbol):
        return _unavailable(f"requested instrument {symbol}")
    rows = _rows()
    if not rows:
        return _unavailable("OKX candles")
    latest = rows[-1]
    indicators = _indicator_series(rows)
    lines = [
        f"## Verified OKX market snapshot for {_CONTEXT.get('inst_id', symbol)}",
        "",
        f"- Captured at: {_CONTEXT.get('captured_at', 'unknown')}",
        f"- Bar: {_CONTEXT.get('bar', 'unknown')}",
        f"- Requested analysis date: {curr_date}",
        f"- Candle rows: {len(rows)}",
        f"- Last candle confirmed: {latest.get('confirm') or 'unknown'}",
        "",
        "### Latest OHLCV",
        "",
        "| Field | Value |",
        "|---|---:|",
        f"| Timestamp | {latest['dt'].isoformat()} |",
        f"| Open | {_fmt(latest['open'])} |",
        f"| High | {_fmt(latest['high'])} |",
        f"| Low | {_fmt(latest['low'])} |",
        f"| Close | {_fmt(latest['close'])} |",
        f"| Volume | {_fmt(latest['volume'])} |",
        "",
        "### Technical indicators",
        "",
        "| Indicator | Value |",
        "|---|---:|",
    ]
    for name in (
        "close_10_ema", "close_50_sma", "close_200_sma", "rsi",
        "boll", "boll_ub", "boll_lb", "macd", "macds", "macdh", "atr",
    ):
        lines.append(f"| {name} | {_fmt(_latest_value(indicators[name]))} |")
    funding = _CONTEXT.get("funding_rate")
    open_interest = _CONTEXT.get("open_interest")
    if isinstance(funding, dict):
        lines += ["", "### OKX derivatives context", ""]
        lines.append(f"- Funding rate: {funding.get('fundingRate', 'N/A')}")
        lines.append(f"- Funding timestamp: {funding.get('fundingTime', 'N/A')}")
    if isinstance(open_interest, dict):
        lines.append(f"- Open interest: {open_interest.get('oi', 'N/A')}")
        lines.append(f"- Open interest timestamp: {open_interest.get('ts', 'N/A')}")
    lines += [
        "",
        "This snapshot is the source of truth for exact OKX price and indicator claims. "
        "It is research-only and cannot authorize or submit an order.",
    ]
    return "\n".join(lines)


def _install_vendor(interface_module: Any, method: str, function: Any) -> None:
    methods = interface_module.VENDOR_METHODS.setdefault(method, {})
    methods[VENDOR_NAME] = function


def install_okx_bridge(context: dict[str, Any], config: dict[str, Any]) -> None:
    """Install an OKX-only vendor set before constructing the graph."""
    global _CONTEXT
    _CONTEXT = context if isinstance(context, dict) else {}

    try:
        from tradingagents.dataflows import interface
        from tradingagents.agents.utils import agent_utils
        from tradingagents.agents.utils import market_data_validation_tools
        from tradingagents.agents.analysts import market_analyst, sentiment_analyst
        from tradingagents.graph import trading_graph
    except ImportError:
        # The repository's protocol fixture intentionally contains only the
        # graph constructor. Keep that fixture usable without weakening the
        # real-image bridge.
        return

    _install_vendor(interface, "get_stock_data", _stock_data)
    _install_vendor(interface, "get_indicators", _indicators)
    for method in (
        "get_news", "get_global_news", "get_insider_transactions",
        "get_macro_indicators", "get_prediction_markets",
        "get_fundamentals", "get_balance_sheet", "get_cashflow",
        "get_income_statement",
    ):
        _install_vendor(interface, method, lambda *args, _method=method, **kwargs: _unavailable(_method))

    config["data_vendors"] = {
        "core_stock_apis": VENDOR_NAME,
        "technical_indicators": VENDOR_NAME,
        "fundamental_data": VENDOR_NAME,
        "news_data": VENDOR_NAME,
        "macro_data": VENDOR_NAME,
        "prediction_markets": VENDOR_NAME,
    }

    def identity(ticker: str) -> dict[str, str]:
        base = str(ticker).split("-", 1)[0].upper()
        return {
            "company_name": f"OKX {base} perpetual contract",
            "exchange": "OKX",
            "quote_type": "crypto",
        }

    agent_utils.resolve_instrument_identity = identity
    trading_graph.resolve_instrument_identity = identity
    market_analyst.get_verified_market_snapshot.func = _verified_snapshot
    agent_utils.get_verified_market_snapshot.func = _verified_snapshot
    market_data_validation_tools.get_verified_market_snapshot.func = _verified_snapshot
    trading_graph.get_verified_market_snapshot.func = _verified_snapshot

    # Public social feeds may block or sleep on rate-limit backoff. Report the
    # missing source explicitly and keep the research run bounded.
    def unavailable_social(*args: Any, **kwargs: Any) -> str:
        return _unavailable("social feeds")

    sentiment_analyst.fetch_stocktwits_messages = unavailable_social
    sentiment_analyst.fetch_reddit_posts = unavailable_social


__all__ = ["install_okx_bridge"]
