from datetime import date, datetime, timezone
from decimal import Context, Decimal, Inexact, InvalidOperation, Overflow, ROUND_HALF_EVEN
from typing import Any

from .account_ledger import amount
from .equity_baseline import baseline_window
from .historical_ledger import DAY_MS, HISTORY_PRECISION, prepare_history_window
from .historical_valuation import RATE_POLICY, rate_key


PERFORMANCE_POLICY = "observed_equity_cash_adjusted_modified_dietz_v1"
RATIO_POLICY = "34_significant_digits_half_even_midpoint_milliseconds"
MAX_INTERVAL_ROWS = 100_000


def money_context() -> Context:
    return Context(prec=HISTORY_PRECISION, traps=[Inexact, InvalidOperation, Overflow])


def ratio_context() -> Context:
    return Context(prec=34, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, Overflow])


def observation_bounds(start: dict, end: dict) -> tuple[int, int]:
    for observation in (start, end):
        baseline_window(observation["target_ms"], observation["request_started_ms"], observation["received_at_ms"])
        amount(observation["equity_usd"], "equity")
    if end["target_ms"] - start["target_ms"] != DAY_MS:
        raise ValueError("performance_nonadjacent_baselines")
    # Both inclusive timing-uncertainty windows must be covered by the ledger.
    return start["request_started_ms"], end["received_at_ms"] + 1


def prepare_interval(rows: list[dict], start: dict, end: dict) -> list[dict]:
    begin, finish = observation_bounds(start, end)
    if not isinstance(rows, list) or len(rows) > MAX_INTERVAL_ROWS:
        raise ValueError("performance_interval_too_large")
    grouped: dict[date, list[dict]] = {}
    seen = set()
    for raw in rows:
        if not isinstance(raw, dict):
            raise ValueError("performance_row_invalid")
        identity = str(raw.get("billId") or "")
        timestamp = amount(raw.get("ts"), "timestamp")
        if not identity or identity in seen:
            raise ValueError("performance_bill_identity_invalid")
        seen.add(identity)
        if timestamp != timestamp.to_integral_value() or not begin <= timestamp < finish:
            raise ValueError("performance_bill_outside_interval")
        day = datetime.fromtimestamp(int(timestamp) / 1000, timezone.utc).date()
        grouped.setdefault(day, []).append(raw)
    records = []
    for day, group in grouped.items():
        prepared, _ = prepare_history_window(group, day)
        for record in prepared:
            record["performance_kind"] = "unknown"
            if record["kind"] == "account_transfer":
                cash = amount(record["cash_flow"], "cash_flow")
                subtype = record["subtype"]
                source, destination = record["raw"].get("from"), record["raw"].get("to")
                expected = ("6", "18") if subtype == "11" else ("18", "6")
                direction_valid = all(value in (None, "", expected[i]) for i, value in enumerate((source, destination)))
                if subtype in {"11", "12"} and direction_valid and (
                    (subtype == "11" and cash >= 0) or (subtype == "12" and cash <= 0)
                ):
                    record["performance_kind"] = "external_flow"
            elif record["kind"] in {"trade", "liquidation", "adl", "funding", "interest", "clawback", "internal_transfer"}:
                record["performance_kind"] = "internal_effect"
            elif record["kind"] == "non_swap" and record["bill_type"] in {"2", "5", "9"}:
                # Account-wide equity already includes spot/other trading PnL.
                record["performance_kind"] = "internal_effect"
            records.append(record)
    return sorted(records, key=lambda record: (record["timestamp_ms"], record["bill_id"]))


def flow_rates(records: list[dict]) -> set[tuple[str, int]]:
    return {
        rate_key(record["currency"], record["timestamp_ms"])
        for record in records
        if record["performance_kind"] == "external_flow"
        and amount(record["cash_flow"], "cash_flow") and record["currency"] != "USD"
    }


def value_interval(start: dict, end: dict, records: list[dict], rates: dict) -> dict[str, Any]:
    begin, finish = observation_bounds(start, end)
    arithmetic, ratios = money_context(), ratio_context()
    start_mid = (start["request_started_ms"] + start["received_at_ms"]) // 2
    end_mid = (end["request_started_ms"] + end["received_at_ms"]) // 2
    duration = Decimal(end_mid - start_mid)
    equity_start, equity_end = amount(start["equity_usd"], "equity"), amount(end["equity_usd"], "equity")
    cash = weighted = Decimal(0)
    flows, flow_count, unknown, ambiguous, missing = [], 0, 0, 0, 0
    for record in records:
        kind = record["performance_kind"]
        if kind == "unknown":
            unknown += 1
            continue
        if kind != "external_flow":
            continue
        flow_count += 1
        timestamp = record["timestamp_ms"]
        native = amount(record["cash_flow"], "cash_flow")
        uncertain = bool(native) and (timestamp <= start["received_at_ms"] or timestamp >= end["request_started_ms"])
        ambiguous += int(uncertain)
        key = rate_key(record["currency"], timestamp)
        price = "1" if record["currency"] == "USD" or not native else rates.get(key)
        flow = {
            "bill_id": record["bill_id"], "timestamp_ms": timestamp, "currency": record["currency"],
            "amount": str(native), "boundary_uncertain": uncertain, "usd": None,
            "rate": price, "candle_ms": key[1] if native and record["currency"] != "USD" else None,
        }
        if price is None:
            missing += 1
        else:
            rate = amount(price, "historical_rate")
            if rate <= 0:
                raise ValueError("historical_rate_invalid")
            usd = arithmetic.multiply(native, rate)
            flow["usd"] = str(usd)
            cash = arithmetic.add(cash, usd)
            weighted = arithmetic.add(weighted, arithmetic.multiply(usd, Decimal(end_mid - timestamp)))
        if len(flows) < 100:
            flows.append(flow)
    status = "unclassified_bills" if unknown else "boundary_uncertain" if ambiguous else "missing_rates" if missing else "estimated"
    result = {
        "status": status, "policy": PERFORMANCE_POLICY, "rate_policy": RATE_POLICY, "ratio_policy": RATIO_POLICY,
        "begin_ms": begin, "end_ms": finish, "start": start, "end": end,
        "start_midpoint_ms": start_mid, "end_midpoint_ms": end_mid, "rows": len(records),
        "unclassified_rows": unknown, "boundary_flow_rows": ambiguous, "missing_rate_rows": missing,
        "cash_flow_usd": None, "pnl_usd": None, "weighted_capital_usd": None,
        "return_pct": None, "return_status": status, "flows": flows, "flow_count": flow_count,
    }
    if status != "estimated":
        return result
    pnl = arithmetic.subtract(arithmetic.subtract(equity_end, equity_start), cash)
    capital_numerator = arithmetic.add(arithmetic.multiply(equity_start, duration), weighted)
    result.update(cash_flow_usd=str(cash), pnl_usd=str(pnl),
                  weighted_capital_usd=str(ratios.divide(capital_numerator, duration)))
    if equity_start <= 0 or equity_end < 0 or capital_numerator <= 0:
        result["return_status"] = "nonpositive_capital"
        return result
    # Defer division until the final ratio so cash amounts are never rounded.
    rate = ratios.divide(arithmetic.multiply(pnl, duration), capital_numerator)
    if rate <= -1:
        result["return_status"] = "nonpositive_link_factor"
    else:
        result["return_pct"] = str(ratios.multiply(rate, Decimal(100)))
    return result


def summarize_performance(daily: list[dict]) -> dict[str, Any]:
    complete = bool(daily) and all(row["status"] == "estimated" for row in daily)
    ratios, arithmetic = ratio_context(), money_context()
    pnl = flows = Decimal(0)
    link = peak = Decimal(1)
    drawdown = Decimal(0)
    can_link = complete and all(row.get("return_pct") is not None for row in daily)
    for row in daily:
        row["nav_index"] = None
        if complete:
            pnl = arithmetic.add(pnl, amount(row["pnl_usd"], "pnl"))
            flows = arithmetic.add(flows, amount(row["cash_flow_usd"], "cash_flow"))
        if can_link:
            factor = ratios.add(Decimal(1), ratios.divide(amount(row["return_pct"], "return"), Decimal(100)))
            link = ratios.multiply(link, factor)
            peak = max(peak, link)
            drawdown = min(drawdown, ratios.subtract(ratios.divide(link, peak), Decimal(1)))
            row["nav_index"] = str(link)
    return {
        "currency": "USD", "scope": "trading_account_total_equity",
        "policy": PERFORMANCE_POLICY, "ratio_policy": RATIO_POLICY,
        "status": "estimated" if complete else "incomplete",
        "complete_intervals": sum(row["status"] == "estimated" for row in daily),
        "total_intervals": len(daily), "daily": daily,
        "pnl_usd": str(pnl) if complete else None, "cash_flow_usd": str(flows) if complete else None,
        "linked_return_pct": str(ratios.multiply(ratios.subtract(link, Decimal(1)), Decimal(100))) if can_link else None,
        "observed_max_drawdown_pct": str(ratios.multiply(drawdown, Decimal(100))) if can_link else None,
    }
