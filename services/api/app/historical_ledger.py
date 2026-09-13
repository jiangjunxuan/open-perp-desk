import calendar
import json
from datetime import date, datetime, time, timedelta, timezone
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow, localcontext
from typing import Any

from .account_ledger import AccountLedgerError, amount, parse_daily_bills


DAY_MS = 86_400_000
PNL_FIELDS = ("realized_pnl", "fees", "funding", "adjustments", "net_pnl")
HISTORY_PRECISION = 80


def day_ms(day: date) -> int:
    return int(datetime.combine(day, time(), timezone.utc).timestamp() * 1000)


def archive_first_day(now: datetime) -> date:
    today = now.astimezone(timezone.utc).date()
    month = today.year * 12 + today.month - 1 - 3
    year, month = divmod(month, 12)
    boundary = date(year, month + 1, min(today.day, calendar.monthrange(year, month + 1)[1]))
    # Omit the partially retained boundary day, rather than claim it is complete.
    return boundary + timedelta(days=1)


def history_days(start: date, end: date, *, now: datetime, importing: bool = False) -> list[date]:
    if now.tzinfo is None:
        raise ValueError("history_timezone_required")
    count = (end - start).days + 1
    if count < 1 or count > (93 if importing else 366):
        raise ValueError("history_date_range_invalid")
    if end >= now.astimezone(timezone.utc).date():
        raise ValueError("history_requires_closed_utc_days")
    if importing and start < archive_first_day(now):
        raise ValueError("history_outside_archive_retention")
    return [start + timedelta(days=offset) for offset in range(count)]


def prepare_history_window(
    rows: list[dict[str, Any]], day: date,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    begin, end = day_ms(day), day_ms(day) + DAY_MS
    as_of = datetime.fromtimestamp((end - 1) / 1000, timezone.utc)
    prepared: list[dict[str, Any]] = []
    seen: set[str] = set()
    pnl: dict[str, dict[str, Decimal]] = {}
    flows: dict[str, Decimal] = {}
    # History sums must never silently inherit the caller's rounding precision.
    arithmetic = Context(prec=HISTORY_PRECISION, traps=[Inexact, InvalidOperation, Overflow])
    counts = {"unclassified": 0, "non_swap": 0, "account_transfer": 0, "internal_transfer": 0, "swap": 0}
    if not isinstance(rows, list):
        raise ValueError("history_rows_invalid")
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("history_row_invalid")
        bill_id = str(raw.get("billId") or "")
        if not bill_id or len(bill_id) > 128 or bill_id in seen:
            raise ValueError("history_bill_identity_invalid")
        seen.add(bill_id)
        timestamp = amount(raw.get("ts"), "timestamp")
        if timestamp != timestamp.to_integral_value() or not begin <= timestamp < end:
            raise ValueError("history_bill_outside_window")
        currency = raw.get("ccy")
        if not isinstance(currency, str) or not currency.strip() or len(currency) > 40:
            raise ValueError("history_currency_invalid")
        json.dumps(raw, allow_nan=False)
        record = {
            "bill_id": bill_id, "timestamp_ms": int(timestamp), "currency": currency,
            "inst_id": str(raw.get("instId") or ""), "inst_type": str(raw.get("instType") or ""),
            "bill_type": str(raw.get("type") or ""), "subtype": str(raw.get("subType") or ""),
            "kind": "unclassified", "cash_flow": None,
            **{field: None for field in PNL_FIELDS}, "raw": raw,
        }
        try:
            if record["bill_type"] == "1":
                cash = amount(raw.get("balChg"), "balance_change")
                position = amount(raw.get("posBalChg") or "0", "position_balance_change")
                if position:
                    raise AccountLedgerError("history_account_transfer_ambiguous")
                record.update(kind="account_transfer", cash_flow=str(cash))
                flows[currency] = arithmetic.add(flows.get(currency, Decimal(0)), cash)
            elif record["bill_type"] == "6":
                record["kind"] = "internal_transfer"
            elif record["inst_type"] == "SWAP":
                with localcontext(arithmetic):
                    parsed, _ = parse_daily_bills([raw], as_of)
                record.update(parsed[0])
                bucket = pnl.setdefault(currency, {field: Decimal(0) for field in PNL_FIELDS})
                for field in PNL_FIELDS:
                    bucket[field] = arithmetic.add(bucket[field], Decimal(record[field]))
            elif record["inst_type"] in {"SPOT", "MARGIN", "FUTURES", "OPTION", "EVENTS"}:
                record["kind"] = "non_swap"
            else:
                raise AccountLedgerError("history_bill_type_unclassified")
        except AccountLedgerError as exc:
            record["interpretation_error"] = str(exc)
        kind = record["kind"]
        counts[kind if kind in counts else "swap"] += 1
        prepared.append(record)
    return prepared, {
        "day_utc": day.isoformat(), "rows": len(prepared), "counts": counts,
        "swap_pnl_by_currency": {
            currency: {field: str(value) for field, value in bucket.items()}
            for currency, bucket in sorted(pnl.items())
        },
        "trading_account_transfers": {currency: str(value) for currency, value in sorted(flows.items())},
        "valuation_status": "not_valued",
    }


def combine_history_windows(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    pnl: dict[str, dict[str, Decimal]] = {}
    flows: dict[str, Decimal] = {}
    counts: dict[str, int] = {}
    arithmetic = Context(prec=HISTORY_PRECISION, traps=[Inexact, InvalidOperation, Overflow])
    for summary in summaries:
        for name, count in summary["counts"].items():
            counts[name] = counts.get(name, 0) + count
        for currency, fields in summary["swap_pnl_by_currency"].items():
            bucket = pnl.setdefault(currency, {field: Decimal(0) for field in PNL_FIELDS})
            for field, value in fields.items():
                bucket[field] = arithmetic.add(bucket[field], Decimal(value))
        for currency, value in summary["trading_account_transfers"].items():
            flows[currency] = arithmetic.add(flows.get(currency, Decimal(0)), Decimal(value))
    return {
        "scope": "trading_account", "basis": "covered_days_only",
        "rows": sum(summary["rows"] for summary in summaries), "counts": counts,
        "interpretation_status": "partial" if counts.get("unclassified") else "swap_and_transfers_only",
        "swap_pnl_by_currency": {
            currency: {field: str(value) for field, value in bucket.items()}
            for currency, bucket in sorted(pnl.items())
        },
        "trading_account_transfers": {currency: str(value) for currency, value in sorted(flows.items())},
        "valuation_status": "not_valued", "net_return": None,
    }
