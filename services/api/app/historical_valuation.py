import re
from datetime import date, datetime, timezone
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow
from typing import Any

from .account_ledger import amount
from .historical_ledger import HISTORY_PRECISION, PNL_FIELDS


MINUTE_MS = 60_000
RATE_POLICY = "previous_confirmed_1m_index_close"
VALUE_FIELDS = (*PNL_FIELDS, "cash_flow")
PNL_KINDS = {"trade", "liquidation", "adl", "funding", "interest", "clawback"}


class HistoricalValuationError(ValueError):
    """Historical evidence is missing or cannot be interpreted exactly."""


def rate_key(currency: str, timestamp_ms: int) -> tuple[str, int]:
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z0-9]{1,40}", currency):
        raise HistoricalValuationError("historical_currency_invalid")
    if type(timestamp_ms) is not int or timestamp_ms < MINUTE_MS:
        raise HistoricalValuationError("historical_timestamp_invalid")
    return currency, timestamp_ms // MINUTE_MS * MINUTE_MS - MINUTE_MS


def record_values(record: dict[str, Any]) -> dict[str, Decimal] | None:
    values = {field: Decimal(0) for field in VALUE_FIELDS}
    if record["kind"] in PNL_KINDS:
        values.update({field: amount(record[field], field) for field in PNL_FIELDS})
    elif record["kind"] == "account_transfer":
        values["cash_flow"] = amount(record["cash_flow"], "cash_flow")
    elif record["kind"] != "internal_transfer":
        return None
    return values


def required_rates(records: list[dict[str, Any]]) -> set[tuple[str, int]]:
    required = set()
    for record in records:
        values = record_values(record)
        if values and any(values.values()) and record["currency"] != "USD":
            required.add(rate_key(record["currency"], record["timestamp_ms"]))
    return required


def parse_index_rate(rows: Any, candle_ms: int) -> str:
    if not isinstance(rows, list) or len(rows) > 100:
        raise HistoricalValuationError("historical_rate_response_invalid")
    matches = []
    for row in rows:
        if not isinstance(row, list) or len(row) != 6:
            raise HistoricalValuationError("historical_rate_response_invalid")
        timestamp = amount(row[0], "index_timestamp")
        if timestamp != timestamp.to_integral_value() or timestamp <= 0 or int(timestamp) % MINUTE_MS:
            raise HistoricalValuationError("historical_rate_response_invalid")
        prices = [amount(value, "index_price") for value in row[1:5]]
        opening, high, low, close = prices
        if min(prices) <= 0 or not low <= min(opening, close) <= max(opening, close) <= high:
            raise HistoricalValuationError("historical_rate_response_invalid")
        if not isinstance(row[5], str) or row[5] not in {"0", "1"}:
            raise HistoricalValuationError("historical_rate_response_invalid")
        if int(timestamp) == candle_ms and row[5] == "1":
            matches.append(str(close))
    if len(matches) > 1:
        raise HistoricalValuationError("historical_rate_duplicate")
    if not matches:
        raise HistoricalValuationError("historical_rate_unavailable")
    return matches[0]


def value_history(
    records: list[dict[str, Any]], days: list[date], covered: set[str],
    rates: dict[tuple[str, int], str],
) -> dict[str, Any]:
    arithmetic = Context(prec=HISTORY_PRECISION, traps=[Inexact, InvalidOperation, Overflow])
    daily = {
        day.isoformat(): {
            "day_utc": day.isoformat(), "covered": day.isoformat() in covered,
            "rows": 0, "valued_rows": 0, "unclassified_rows": 0, "missing_rate_rows": 0,
            "subtotal": {field: Decimal(0) for field in VALUE_FIELDS},
        } for day in days
    }
    missing: set[tuple[str, int]] = set()
    row_values = {}
    for record in records:
        day = datetime.fromtimestamp(record["timestamp_ms"] / 1000, timezone.utc).date().isoformat()
        if day not in daily:
            raise HistoricalValuationError("historical_record_outside_range")
        bucket = daily[day]
        bucket["rows"] += 1
        values = record_values(record)
        if values is None:
            bucket["unclassified_rows"] += 1
            continue
        key = rate_key(record["currency"], record["timestamp_ms"])
        if not any(values.values()):
            rate = Decimal(1)
            basis = "zero_amount"
        elif record["currency"] == "USD":
            rate = Decimal(1)
            basis = "same_currency"
        elif key in rates:
            rate = amount(rates[key], "historical_rate")
            basis = RATE_POLICY
            if rate <= 0:
                raise HistoricalValuationError("historical_rate_invalid")
        else:
            missing.add(key)
            bucket["missing_rate_rows"] += 1
            continue
        usd = {field: arithmetic.multiply(value, rate) for field, value in values.items()}
        for field, value in usd.items():
            bucket["subtotal"][field] = arithmetic.add(bucket["subtotal"][field], value)
        bucket["valued_rows"] += 1
        row_values[record["bill_id"]] = {
            "usd": {field: str(value) for field, value in usd.items()},
            "rate": str(rate) if basis != "zero_amount" else None, "basis": basis,
            "index": f"{key[0]}-USD" if basis == RATE_POLICY else None,
            "candle_ms": key[1] if basis == RATE_POLICY else None,
        }
    totals = {field: Decimal(0) for field in VALUE_FIELDS}
    complete = True
    for bucket in daily.values():
        valid = bucket["covered"] and not bucket["unclassified_rows"] and not bucket["missing_rate_rows"]
        complete = complete and valid
        for field, value in bucket["subtotal"].items():
            totals[field] = arithmetic.add(totals[field], value)
        bucket["status"] = "valued" if valid else "incomplete"
        bucket["valued_subtotal_usd"] = {field: str(value) for field, value in bucket.pop("subtotal").items()}
        bucket["usd"] = bucket["valued_subtotal_usd"] if valid else None
    return {
        "currency": "USD", "policy": RATE_POLICY,
        "scope": "recognized_swap_pnl_and_trading_account_transfers",
        "status": "valued" if complete else "incomplete",
        "usd": {field: str(value) for field, value in totals.items()} if complete else None,
        "valued_subtotal_usd": {field: str(value) for field, value in totals.items()},
        "missing_rate_count": len(missing),
        "missing_day_count": sum(not bucket["covered"] for bucket in daily.values()),
        "missing_rates": [
            {"currency": currency, "index": f"{currency}-USD", "candle_ms": timestamp}
            for currency, timestamp in sorted(missing)[:100]
        ],
        "unclassified_rows": sum(bucket["unclassified_rows"] for bucket in daily.values()),
        "daily": list(daily.values()), "rows": row_values,
        "net_return": None, "return_status": "equity_baselines_unavailable",
    }
