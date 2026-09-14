import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from typing import Any

from .okx_account import OkxAccountClient, OkxAccountError
from .account_ledger import AccountLedgerError, parse_daily_bills, value_daily_risk
from .okx_market import OkxMarketClient
from .state_store import StateStore
from .position_protection import attached_parent_evidence, protection_evidence
from .protection_handoff import HandoffError, close_context, effective_evidence, native_evidence, verified_lot
from .trading_signal import TradeSignal


class PreflightError(ValueError):
    """A public, non-secret reason why exchange-backed preflight failed."""


def number(value: Any, name: str, *, positive: bool = False) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PreflightError(f"{name}_invalid") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise PreflightError(f"{name}_invalid")
    return result


@dataclass(frozen=True)
class ContractSpec:
    inst_id: str
    contract_type: str
    contract_value: Decimal
    lot_size: Decimal
    min_size: Decimal
    tick_size: Decimal
    value_currency: str
    settlement_currency: str
    quote_currency: str
    max_market_size: Decimal | None

    @classmethod
    def parse(cls, row: dict[str, Any]) -> "ContractSpec":
        inst_id = str(row.get("instId") or "")
        parts = inst_id.split("-")
        if len(parts) != 3 or parts[-1] != "SWAP" or row.get("state") != "live":
            raise PreflightError("instrument_not_tradable")
        contract_type = row.get("ctType")
        if contract_type not in {"linear", "inverse"}:
            raise PreflightError("contract_type_unknown")
        value_currency = str(row.get("ctValCcy") or "")
        expected_currency = parts[0] if contract_type == "linear" else parts[1]
        if value_currency != expected_currency:
            raise PreflightError("contract_value_currency_mismatch")
        return cls(
            inst_id, contract_type,
            number(row.get("ctVal"), "contract_value", positive=True)
            * number(row.get("ctMult") or "1", "contract_multiplier", positive=True),
            number(row.get("lotSz"), "lot_size", positive=True),
            number(row.get("minSz"), "min_size", positive=True),
            number(row.get("tickSz"), "tick_size", positive=True),
            value_currency,
            str(row.get("settleCcy") or ""),
            parts[1],
            number(row["maxMktSz"], "max_market_size", positive=True)
            if row.get("maxMktSz") else None,
        )

    def validate_size(self, size: Decimal) -> None:
        if size <= 0 or size < self.min_size or size % self.lot_size != 0:
            raise PreflightError("order_size_outside_contract_steps")
        if self.max_market_size is not None and size > self.max_market_size:
            raise PreflightError("order_size_above_exchange_limit")

    def notional(self, size: Decimal, price: Decimal, rates: dict[str, Decimal]) -> Decimal:
        quote_amount = abs(size) * self.contract_value
        if self.contract_type == "linear":
            quote_amount *= price
        return quote_amount * currency_rate(self.quote_currency, rates)

    def protection(self, signal: TradeSignal, price: Decimal) -> TradeSignal:
        if signal.action not in {"open_long", "open_short"}:
            return signal
        long = signal.action == "open_long"
        stop = number(signal.stop_loss, "stop_loss", positive=True)
        target = number(signal.take_profit, "take_profit", positive=True)
        stop = (stop / self.tick_size).to_integral_value(
            rounding=ROUND_CEILING if long else ROUND_FLOOR,
        ) * self.tick_size
        target = (target / self.tick_size).to_integral_value(
            rounding=ROUND_FLOOR if long else ROUND_CEILING,
        ) * self.tick_size
        try:
            return TradeSignal.model_validate({
                **signal.model_dump(),
                "entry_price": float(price),
                "stop_loss": float(stop),
                "take_profit": float(target),
            })
        except ValueError as exc:
            raise PreflightError("protective_prices_invalid_at_current_market") from exc


def currency_rate(currency: str, rates: dict[str, Decimal]) -> Decimal:
    if currency not in rates:
        raise PreflightError("account_currency_valuation_unavailable")
    return rates[currency]


@dataclass(frozen=True)
class PreparedExecution:
    signal: TradeSignal
    account_equity: float
    daily_pnl_pct: float
    current_notional: float
    order_notional: float
    side: str
    pos_side: str
    td_mode: str
    generation: int
    captured_at: str
    market_timestamp: float
    verified_close: bool = False
    daily_accounting: dict[str, Any] = field(default_factory=dict)
    account_scope: str | None = None

    def summary(self) -> dict[str, Any]:
        return {
            "basis": "exchange",
            "account_equity": self.account_equity,
            "daily_pnl_pct": self.daily_pnl_pct,
            "current_notional": self.current_notional,
            "order_notional": self.order_notional,
            "side": self.side,
            "pos_side": self.pos_side,
            "td_mode": self.td_mode,
            "generation": self.generation,
            "captured_at": self.captured_at,
            "market_timestamp": self.market_timestamp,
            "daily_accounting": self.daily_accounting,
            "account_scope": self.account_scope,
        }


class OrderPreflight:
    """Read-only exchange checks shared by the web executor and strategy worker."""

    def __init__(
        self,
        account: OkxAccountClient,
        market: OkxMarketClient,
        store: StateStore,
    ) -> None:
        self.account = account
        self.market = market
        self.store = store

    @property
    def configured(self) -> bool:
        return self.account.configured

    async def prepare(
        self,
        signal: TradeSignal,
        size: float,
        side_override: str | None = None,
        *,
        expected_position_trade_id: str | None = None,
        expected_protection: dict[str, Any] | None = None,
    ) -> PreparedExecution:
        if not self.configured:
            raise PreflightError("private_account_not_configured")
        captured_at = datetime.now(timezone.utc)
        generation, local_orders = self.store.execution_snapshot()
        try:
            balance, positions, pending, bills, config, instruments, ticker, algos = await asyncio.gather(
                self.account.balance(), self.account.positions(),
                self.account.pending_orders(), self.account.bills_today(as_of=captured_at),
                self.account.config(), self.market.instruments(),
                self.market.ticker(signal.inst_id),
                self.account.active_algo_orders(),
            )
        except Exception as exc:
            raise PreflightError("exchange_preflight_data_unavailable") from exc
        if not balance or not config:
            raise PreflightError("account_snapshot_empty")
        equity = number(balance[0].get("totalEq"), "account_equity", positive=True)
        position_mode = config[0].get("posMode")
        if position_mode not in {"net_mode", "long_short_mode"}:
            raise PreflightError("account_position_mode_unknown")
        protective_algo_ids: set[str] = set()
        for algo in algos:
            hedge_close = position_mode == "long_short_mode" and (
                algo.get("posSide"), algo.get("side")
            ) in {("long", "sell"), ("short", "buy")}
            if not (str(algo.get("reduceOnly")).lower() == "true" or hedge_close):
                raise PreflightError("unvalued_algo_order_exposure")
            if not algo.get("algoId"):
                raise PreflightError("algo_order_identity_unavailable")
            protective_algo_ids.add(str(algo["algoId"]))

        rates = {"USD": Decimal(1)}
        for detail in balance[0].get("details") or []:
            amount = number(detail.get("eq") or "0", "currency_equity")
            usd = number(detail.get("eqUsd") or "0", "currency_usd_equity")
            if amount and usd / amount > 0:
                rates[str(detail.get("ccy"))] = usd / amount
        metadata = {str(row.get("instId")): row for row in instruments}
        specs: dict[str, ContractSpec] = {}

        def contract(inst_id: str) -> ContractSpec:
            if inst_id not in specs:
                specs[inst_id] = ContractSpec.parse(metadata.get(inst_id) or {})
            return specs[inst_id]

        def quote(row: dict[str, Any], inst_id: str) -> tuple[Decimal, float]:
            if row.get("instId") != inst_id:
                raise PreflightError("ticker_instrument_mismatch")
            timestamp = float(number(row.get("ts"), "ticker_timestamp", positive=True)) / 1000
            age = datetime.now(timezone.utc).timestamp() - timestamp
            if age > 30 or age < -5:
                raise PreflightError("market_data_stale")
            return number(row.get("last"), "market_price", positive=True), timestamp

        price, market_timestamp = quote(ticker, signal.inst_id)
        spec = contract(signal.inst_id)
        quantity = number(size, "order_size", positive=True)
        spec.validate_size(quantity)
        open_signal = signal.action in {"open_long", "open_short"}
        if open_signal:
            if self.store.protection_handoffs(self.account.account_scope, signal.inst_id):
                raise PreflightError("protection_handoff_pending")
            reference = number(signal.entry_price, "signal_price", positive=True)
            if abs(price - reference) / reference > Decimal("0.01"):
                raise PreflightError("signal_price_deviation")
        validated_signal = spec.protection(signal, price)
        side = "buy" if signal.action == "open_long" else "sell"
        pos_side = "net" if position_mode == "net_mode" else ("long" if side == "buy" else "short")
        td_mode = "isolated"
        verified_close = False
        if signal.action == "close":
            candidates = []
            for row in positions:
                if row.get("instId") != signal.inst_id:
                    continue
                position_size = number(row.get("pos") or "0", "position_size")
                if not position_size:
                    continue
                long = row.get("posSide") == "long" or (row.get("posSide") == "net" and position_size > 0)
                close_side = "sell" if long else "buy"
                if side_override is None or close_side == side_override:
                    candidates.append((row, close_side, abs(position_size)))
            if len(candidates) != 1:
                raise PreflightError("close_position_missing_or_ambiguous")
            position, side, position_size = candidates[0]
            if expected_position_trade_id is not None and position.get("tradeId") != expected_position_trade_id:
                raise PreflightError("close_position_changed")
            if quantity > position_size:
                raise PreflightError("close_size_above_position")
            if position.get("availPos") not in (None, ""):
                if quantity > abs(number(position["availPos"], "available_position")):
                    raise PreflightError("close_size_above_available_position")
            pos_side, td_mode = position.get("posSide"), position.get("mgnMode")
            if pos_side not in {"net", "long", "short"} or td_mode not in {"isolated", "cross"}:
                raise PreflightError("close_position_mode_unknown")
            if (position_mode == "net_mode") != (pos_side == "net"):
                raise PreflightError("position_mode_mismatch")
            if signal.source.startswith("protective-"):
                if expected_protection is None or not expected_position_trade_id:
                    raise PreflightError("close_protection_unverified")
                opening = self.store.get_order(expected_protection.get("opening_order_id", ""))
                if (
                    not opening or opening["account_scope"] != self.account.account_scope
                    or opening["inst_id"] != signal.inst_id or opening["pos_side"] != pos_side
                    or opening["td_mode"] != td_mode
                ):
                    raise PreflightError("close_protection_unverified")
                protected_size = position_size
                if expected_protection.get("lot_id"):
                    local = self.store.get_position(f"{signal.inst_id}:{pos_side}:{td_mode}")
                    if (
                        not local or local["account_scope"] != self.account.account_scope
                        or local["exchange_trade_id"] != position.get("tradeId")
                        or abs(number(local["size"], "local_position_size")) != position_size
                    ):
                        raise PreflightError("close_lot_position_changed")
                    try:
                        lot = verified_lot(self.store, local, expected_protection["lot_id"])
                    except HandoffError as exc:
                        raise PreflightError(str(exc)) from exc
                    if (
                        not lot or lot["opening_order_id"] != opening["client_order_id"]
                        or lot["opening_exchange_id"] != opening["exchange_order_id"]
                        or number(lot["remaining_size"], "lot_remaining_size", positive=True) != quantity
                    ):
                        raise PreflightError("close_lot_quantity_changed")
                    protected_size = quantity
                if expected_protection.get("kind") == "handoff":
                    handoff = self.store.protection_handoff(expected_protection.get("handoff_id", ""))
                    if (
                        not handoff or handoff["status"] != "ready"
                        or handoff["account_scope"] != self.account.account_scope
                        or handoff["inst_id"] != signal.inst_id
                        or close_context(handoff) != expected_protection
                        or not expected_protection.get("lot_id")
                        or any(
                            row.get("algoClOrdId") == expected_protection["algo_client_id"]
                            or expected_protection["algo_id"] is not None and row.get("algoId") == expected_protection["algo_id"]
                            for row in algos
                        )
                    ):
                        raise PreflightError("close_handoff_unverified")
                    original, proof = json.loads(handoff["evidence_json"]), effective_evidence(handoff)
                    parent = None
                    if original["kind"] == "attached":
                        try:
                            parent = await self.account.order_details(
                                signal.inst_id, ord_id=original["opening_exchange_id"],
                                client_order_id=original["opening_order_id"],
                            )
                        except Exception as exc:
                            raise PreflightError("close_handoff_parent_unavailable") from exc
                        if (
                            parent.get("state") not in {"filled", "canceled", "mmp_canceled"}
                            or any(row.get("ordId") == original["opening_exchange_id"] for row in pending)
                            or not attached_parent_evidence(opening, parent, position_size=float(quantity), expected=original)
                        ):
                            raise PreflightError("close_handoff_parent_unverified")
                    try:
                        native_row = await self.account.algo_order_details(
                            signal.inst_id, algo_id=expected_protection["algo_id"],
                            client_order_id=expected_protection["algo_client_id"],
                        )
                    except OkxAccountError as exc:
                        if proof["kind"] != "attached" or exc.code != "51603":
                            raise PreflightError("close_handoff_native_unavailable") from exc
                        native_row = None
                    except Exception as exc:
                        raise PreflightError("close_handoff_native_unavailable") from exc
                    if opening["account_scope"] != self.account.account_scope:
                        raise PreflightError("close_handoff_account_changed")
                    if proof["kind"] == "attached":
                        if (
                            native_row is not None or parent["state"] not in {"canceled", "mmp_canceled"}
                            or number(parent["accFillSz"], "parent_filled_size") >= number(parent["sz"], "parent_size")
                            or self.store.get_order(proof["algo_client_id"])
                        ):
                            raise PreflightError("close_handoff_absence_unverified")
                    elif not native_evidence(opening, native_row, proof, canceled=True):
                        raise PreflightError("close_handoff_cancellation_unverified")
                    current = expected_protection
                elif expected_protection.get("kind") == "attached" and expected_protection.get("lot_id"):
                    try:
                        parent = await self.account.order_details(
                            signal.inst_id, ord_id=opening["exchange_order_id"],
                            client_order_id=opening["client_order_id"],
                        )
                    except Exception as exc:
                        raise PreflightError("close_parent_unavailable") from exc
                    current = attached_parent_evidence(opening, parent, position_size=float(protected_size))
                    if current is not None:
                        current["lot_id"] = expected_protection["lot_id"]
                else:
                    native = expected_protection.get("kind") == "native"
                    matches = [
                        row for row in (algos if native else pending)
                        if row.get("algoClOrdId" if native else "clOrdId")
                        == expected_protection.get("algo_client_id" if native else "opening_order_id")
                    ]
                    current = protection_evidence(
                        opening, matches[0], native=native, position_size=float(protected_size),
                    ) if len(matches) == 1 else None
                    if current is not None and expected_protection.get("lot_id"):
                        current["lot_id"] = expected_protection["lot_id"]
                if current is None or current != expected_protection:
                    raise PreflightError("close_protection_changed")
            if any(
                row.get("instId") == signal.inst_id and row.get("side") == side
                and row.get("posSide") in {pos_side, None, ""}
                for row in pending
            ) or any(
                row.get("inst_id") == signal.inst_id and row.get("side") == side
                and row.get("reduce_only") and row.get("order_kind") != "algo"
                for row in local_orders
            ):
                raise PreflightError("pending_close_order_unconfirmed")
            verified_close = True
        elif side_override is not None and side_override != side:
            raise PreflightError("signal_side_mismatch")

        exposure = Decimal(0)
        unrealized_pnl = Decimal(0)
        for row in positions:
            if not number(row.get("pos") or "0", "position_size"):
                continue
            exposure += abs(number(row.get("notionalUsd"), "position_notional"))
            currency = contract(str(row.get("instId"))).settlement_currency
            unrealized_pnl += number(row.get("upl"), "unrealized_pnl") * currency_rate(currency, rates)
        now = datetime.now(timezone.utc)
        if now.date() != captured_at.date():
            raise PreflightError("account_day_changed")
        try:
            parsed_bills, bill_summary = parse_daily_bills(bills, captured_at)
            daily_accounting = value_daily_risk(bill_summary, rates, unrealized_pnl)
        except AccountLedgerError as exc:
            raise PreflightError(str(exc)) from exc
        self.store.save_bill_snapshot(self.account.account_scope, parsed_bills, bill_summary)
        daily_pnl = number(daily_accounting["risk_pnl_usd"], "daily_risk_pnl")

        tickers = {signal.inst_id: ticker}
        pending_symbols = {str(row.get("instId")) for row in pending} - tickers.keys()
        try:
            fetched = await asyncio.gather(*(self.market.ticker(symbol) for symbol in sorted(pending_symbols)))
        except Exception as exc:
            raise PreflightError("pending_order_valuation_unavailable") from exc
        tickers.update(zip(sorted(pending_symbols), fetched))
        known_ids: set[str] = set()
        known_exchange_ids: set[str] = set()
        for row in pending:
            inst_id = str(row.get("instId"))
            known_ids.add(str(row.get("clOrdId") or ""))
            known_exchange_ids.add(str(row.get("ordId") or ""))
            hedge_close = (
                (row.get("posSide"), row.get("side")) in {("long", "sell"), ("short", "buy")}
            )
            if str(row.get("reduceOnly")).lower() == "true" or hedge_close:
                continue
            remaining = number(row.get("sz"), "pending_size") - number(row.get("accFillSz") or "0", "filled_size")
            if remaining < 0:
                raise PreflightError("pending_size_invalid")
            pending_price, _ = quote(tickers[inst_id], inst_id)
            if row.get("px") not in (None, "", "0"):
                pending_price = max(pending_price, number(row["px"], "pending_price", positive=True))
            exposure += contract(inst_id).notional(remaining, pending_price, rates)
        for row in local_orders:
            if row.get("account_scope") and row["account_scope"] != self.account.account_scope:
                raise PreflightError("active_order_account_scope_mismatch")
            if row.get("order_kind") == "algo" and row.get("exchange_order_id") in protective_algo_ids:
                continue
            if row.get("reduce_only") or row["client_order_id"] in known_ids or (
                row.get("exchange_order_id") and row["exchange_order_id"] in known_exchange_ids
            ):
                continue
            reserved = row.get("risk_notional")
            if reserved is None:
                raise PreflightError("unreconciled_order_exposure")
            exposure += number(reserved, "reserved_notional", positive=True)
        if datetime.now(timezone.utc).date() != captured_at.date():
            raise PreflightError("account_day_changed")
        return PreparedExecution(
            validated_signal, float(equity), float(daily_pnl / equity * 100),
            float(exposure), float(spec.notional(quantity, price, rates)),
            side, pos_side, td_mode, generation, now.isoformat(), market_timestamp,
            verified_close, daily_accounting, self.account.account_scope,
        )
