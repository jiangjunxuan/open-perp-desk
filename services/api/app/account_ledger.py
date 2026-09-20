from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class AccountLedgerError(ValueError):
    """An incomplete or ambiguous exchange ledger cannot approve new exposure."""


def amount(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AccountLedgerError(f"bill_{field}_invalid") from exc
    if not parsed.is_finite():
        raise AccountLedgerError(f"bill_{field}_invalid")
    return parsed


def _balance_change(row: dict[str, Any]) -> Decimal:
    account = amount(row.get("balChg"), "balance_change")
    position = amount(row.get("posBalChg") or "0", "position_balance_change")
    # Funding may debit the isolated position instead of the account wallet.
    # These are alternative representations, not two amounts to add together.
    if account and position and account != position:
        raise AccountLedgerError("bill_balance_change_ambiguous")
    return account if account else position


def parse_daily_bills(
    rows: list[dict[str, Any]],
    as_of: datetime,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if as_of.tzinfo is None:
        raise AccountLedgerError("bill_snapshot_timezone_required")
    as_of = as_of.astimezone(timezone.utc)
    end = int(as_of.timestamp() * 1000)
    begin = int(as_of.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    parsed: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    totals: dict[str, dict[str, Decimal]] = {}
    excluded = 0
    if not isinstance(rows, list):
        raise AccountLedgerError("bill_rows_invalid")
    for row in rows:
        if not isinstance(row, dict):
            raise AccountLedgerError("bill_row_invalid")
        bill_id = str(row.get("billId") or "")
        if not bill_id:
            raise AccountLedgerError("bill_id_missing")
        if bill_id in seen:
            if row != seen[bill_id]:
                raise AccountLedgerError("bill_id_conflict")
            continue
        seen[bill_id] = row
        timestamp = amount(row.get("ts"), "timestamp")
        if timestamp != timestamp.to_integral_value() or timestamp <= 0 or timestamp > end:
            raise AccountLedgerError("bill_timestamp_invalid")
        if timestamp < begin:
            continue
        currency = str(row.get("ccy") or "")
        if not currency:
            raise AccountLedgerError("bill_currency_missing")
        bill_type = str(row.get("type") or "")
        subtype = str(row.get("subType") or "")
        instrument = str(row.get("instId") or "")
        pnl = fees = funding = adjustments = Decimal(0)
        if bill_type in {"1", "6"}:
            kind = "transfer"
            excluded += 1
        else:
            if row.get("instType") != "SWAP" or not instrument.endswith("-SWAP"):
                raise AccountLedgerError("bill_instrument_mismatch")
            if bill_type in {"2", "5", "9"}:
                kind = {"2": "trade", "5": "liquidation", "9": "adl"}[bill_type]
                pnl = amount(row.get("pnl"), "pnl")
                fees = amount(row.get("fee"), "fee")
            elif bill_type == "8":
                kind = "funding"
                if subtype not in {"173", "174"}:
                    raise AccountLedgerError("bill_funding_subtype_unknown")
                funding = _balance_change(row)
                if (subtype == "173" and funding > 0) or (subtype == "174" and funding < 0):
                    raise AccountLedgerError("bill_funding_sign_invalid")
                if amount(row.get("fee") or "0", "fee"):
                    raise AccountLedgerError("bill_funding_fee_ambiguous")
            elif bill_type in {"7", "10"}:
                kind = "interest" if bill_type == "7" else "clawback"
                adjustments = _balance_change(row)
                if adjustments > 0:
                    raise AccountLedgerError("bill_deduction_sign_invalid")
            else:
                raise AccountLedgerError("bill_type_unsupported")
        net = pnl + fees + funding + adjustments
        values = {"realized_pnl": pnl, "fees": fees, "funding": funding, "adjustments": adjustments, "net_pnl": net}
        if kind != "transfer":
            bucket = totals.setdefault(currency, {name: Decimal(0) for name in values})
            for name, value in values.items():
                bucket[name] += value
        parsed.append({
            "bill_id": bill_id,
            "inst_id": instrument,
            "currency": currency,
            "bill_type": bill_type,
            "subtype": subtype,
            "kind": kind,
            "timestamp_ms": int(timestamp),
            **{name: str(value) for name, value in values.items()},
            "raw": row,
        })
    summary = {
        "scope": "SWAP",
        "day_utc": as_of.date().isoformat(),
        "captured_at": as_of.isoformat(),
        "bills": len(parsed),
        "excluded_transfers": excluded,
        "by_currency": {
            currency: {name: str(value) for name, value in bucket.items()}
            for currency, bucket in sorted(totals.items())
        },
    }
    return parsed, summary


def value_daily_risk(
    summary: dict[str, Any],
    rates: dict[str, Decimal],
    unrealized_usd: Decimal,
) -> dict[str, Any]:
    if not unrealized_usd.is_finite():
        raise AccountLedgerError("unrealized_pnl_invalid")
    totals = {name: Decimal(0) for name in ("realized_pnl", "fees", "funding", "adjustments", "net_pnl")}
    for currency, bucket in summary["by_currency"].items():
        values = {name: amount(bucket[name], name) for name in totals}
        if not any(values.values()):
            continue
        rate = rates.get(currency)
        if rate is None or not rate.is_finite() or rate <= 0:
            raise AccountLedgerError("bill_currency_valuation_unavailable")
        for name, value in values.items():
            totals[name] += value * rate
    # Do not let unrealized gains, including gains carried overnight, offset
    # today's realized losses. This is a risk measure, not midnight equity PnL.
    risk_pnl = totals["net_pnl"] + min(unrealized_usd, Decimal(0))
    return {
        **summary,
        "valuation": "current_account_currency_rates",
        "currency": "USD",
        "usd": {name: float(value) for name, value in totals.items()},
        "unrealized_pnl_usd": float(unrealized_usd),
        "risk_pnl_usd": float(risk_pnl),
        "risk_basis": "utc_realized_net_plus_current_unrealized_loss",
    }
