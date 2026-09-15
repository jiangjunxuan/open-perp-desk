import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app import main as api_main
from app.execution_engine import ExecutionEngine
from app.okx_account import OkxAccountClient, OkxAccountError
from app.okx_trade import OkxOrderRejected, OkxTradeClient, OkxTradeError, OrderRequest
from app.order_preflight import OrderPreflight, PreflightError
from app.position_protection import protection_evidence
from app.risk_engine import RiskEngine, RiskLimits
from app.state_store import ExposureSnapshotChanged, StateStore
from app.trading_signal import TradeSignal
from tests.test_order_recovery import exchange_order, protection_order, snapshot_order


def instrument(symbol="BTC-USDT-SWAP"):
    return {
        "instId": symbol, "state": "live", "ctType": "linear", "ctVal": "0.01",
        "ctMult": "1", "ctValCcy": symbol.split("-")[0], "settleCcy": "USDT",
        "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1", "maxMktSz": "10000",
    }


def position(side="net", size="0.2", notional="100", upl="0", margin="isolated"):
    return {
        "instId": "BTC-USDT-SWAP", "posSide": side, "pos": size, "availPos": size,
        "notionalUsd": notional, "upl": upl, "mgnMode": margin,
    }


class Account:
    configured = True
    demo = True
    account_scope = "test-preflight-account"

    def __init__(self):
        self.position_rows = []
        self.pending_rows = []
        self.algo_rows = []
        self.fill_rows = []
        self.bill_rows = []
        self.mode = "net_mode"
        self.equity = "1000"

    async def balance(self):
        return [{
            "totalEq": self.equity,
            "details": [{"ccy": "USDT", "eq": self.equity, "eqUsd": self.equity}],
        }]

    async def positions(self):
        return self.position_rows

    async def pending_orders(self):
        return self.pending_rows

    async def active_algo_orders(self):
        return self.algo_rows

    async def fills_today(self):
        return self.fill_rows

    async def bills_today(self, *, as_of=None):
        return self.bill_rows

    async def config(self):
        return [{"posMode": self.mode}]


class Market:
    def __init__(self):
        self.rows = [instrument(), instrument("ETH-USDT-SWAP")]
        self.age = 0

    async def instruments(self):
        return self.rows

    async def ticker(self, symbol):
        return {
            "instId": symbol, "last": "50000" if symbol.startswith("BTC-") else "2500",
            "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000) - self.age),
        }


class Trade:
    enabled = True

    def __init__(self):
        self.orders = []
        self.leverages = []

    async def set_leverage(self, *args):
        self.leverages.append(args)
        return {}

    async def place_order(self, order):
        self.orders.append(order)
        return {"code": "0", "data": [{"ordId": "exchange1"}]}


class OrderPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.account, self.market, self.trade = Account(), Market(), Trade()
        self.preflight = OrderPreflight(self.account, self.market, self.store)
        self.engine = ExecutionEngine(
            self.store, RiskEngine(), self.trade, SimpleNamespace(configured=False),
            preflight=self.preflight,
        )
        self.signal = TradeSignal(
            inst_id="BTC-USDT-SWAP", action="open_long", confidence=0.9,
            leverage=2, position_pct=5, entry_price=50000,
            stop_loss=49000.03, take_profit=52000.07,
        )

    async def submit(self, signal=None, size=0.2, **kwargs):
        return await self.engine.submit_signal(
            signal or self.signal,
            account_equity=1000000000, daily_pnl_pct=100, current_notional=0,
            size=size, **kwargs,
        )

    async def test_real_equity_and_contract_notional_override_caller_values(self):
        result = await self.submit(size=1)
        self.assertFalse(result["accepted"])
        self.assertIn("order_notional_above_signal_budget", result["reasons"])
        self.assertIn("total_exposure_above_limit", result["reasons"])
        self.assertEqual(self.trade.orders, [])
        self.assertEqual(self.trade.leverages, [])

    async def test_approved_order_uses_verified_mode_leverage_and_quantized_protection(self):
        result = await self.submit()
        self.assertTrue(result["accepted"])
        self.assertEqual(result["preflight"]["account_equity"], 1000)
        self.assertEqual(result["preflight"]["order_notional"], 100)
        self.assertEqual(self.trade.leverages, [("BTC-USDT-SWAP", 2, "isolated", "net")])
        order = self.trade.orders[0]
        self.assertEqual(order.stop_loss, 49000.1)
        self.assertEqual(order.take_profit, 52000)
        self.assertEqual(result["order"]["risk_notional"], 100)

    async def test_preview_with_credentials_runs_checks_but_never_sets_leverage(self):
        result = await self.submit(dry_run=True)
        self.assertTrue(result["accepted"])
        self.assertEqual(result["preflight"]["basis"], "exchange")
        self.assertEqual(self.trade.leverages, [])
        self.assertEqual(self.trade.orders, [])
        self.assertEqual(self.store.execution_snapshot()[0], 0)

    async def test_different_close_ids_do_not_bypass_an_unconfirmed_local_close(self):
        self.account.position_rows = [position()]
        signal = TradeSignal(inst_id="BTC-USDT-SWAP", action="close", confidence=1, leverage=1, position_pct=0)
        first = await self.submit(signal, idempotency_key="first-close")
        self.assertTrue(first["accepted"])
        second = await self.submit(signal, idempotency_key="new-close")
        self.assertFalse(second["accepted"])
        self.assertIn("pending_close_order_unconfirmed", second["reasons"])
        replay = await self.submit(signal, idempotency_key="first-close")
        self.assertTrue(replay["accepted"] and replay["idempotent"])
        self.assertEqual(len(self.trade.orders), 1)

    async def test_concurrent_close_ids_cannot_both_claim_execution(self):
        self.account.position_rows = [position()]
        signal = TradeSignal(inst_id="BTC-USDT-SWAP", action="close", confidence=1, leverage=1, position_pct=0)
        results = await asyncio.gather(
            self.submit(signal, idempotency_key="concurrent-close-one"),
            self.submit(signal, idempotency_key="concurrent-close-two"),
        )
        self.assertEqual(sum(result["accepted"] for result in results), 1, results)
        rejected = next(result for result in results if not result["accepted"])
        self.assertTrue(set(rejected["reasons"]) & {
            "pending_close_order_unconfirmed", "execution_budget_snapshot_changed",
        })
        self.assertEqual(len(self.trade.orders), 1)

    async def test_exchange_pending_close_blocks_net_and_hedged_close_requests(self):
        signal = TradeSignal(inst_id="BTC-USDT-SWAP", action="close", confidence=1, leverage=1, position_pct=0)
        for mode, side in (("net_mode", "net"), ("long_short_mode", "long")):
            with self.subTest(mode=mode):
                self.account.mode = mode
                self.account.position_rows = [position(side=side)]
                self.account.pending_rows = [{
                    "ordId": "external-close", "instId": signal.inst_id, "side": "sell",
                    "posSide": side, "reduceOnly": "true", "sz": ".2", "accFillSz": "0",
                }]
                result = await self.submit(signal)
                self.assertFalse(result["accepted"])
                self.assertIn("pending_close_order_unconfirmed", result["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_native_protection_does_not_block_a_standard_protective_close(self):
        self.account.position_rows = [position()]
        self.account.algo_rows = [{
            "algoId": "native-protection", "instId": "BTC-USDT-SWAP", "ordType": "oco",
            "posSide": "net", "side": "sell", "sz": ".2", "reduceOnly": "true",
        }]
        signal = TradeSignal(inst_id="BTC-USDT-SWAP", action="close", confidence=1, leverage=1, position_pct=0)
        self.assertTrue((await self.submit(signal))["accepted"])
        self.assertEqual(len(self.trade.orders), 1)

    async def test_protective_close_rechecks_position_trade_identity_before_submitting(self):
        self.account.position_rows = [{**position(), "tradeId": "current-trade"}]
        signal = TradeSignal(inst_id="BTC-USDT-SWAP", action="close", confidence=1, leverage=1, position_pct=0)
        rejected = await self.submit(signal, expected_position_trade_id="previous-trade")
        self.assertFalse(rejected["accepted"])
        self.assertIn("close_position_changed", rejected["reasons"])
        self.assertEqual(self.trade.orders, [])
        self.assertEqual(self.store.list_orders(), [])
        accepted = await self.submit(signal, expected_position_trade_id="current-trade")
        self.assertTrue(accepted["accepted"])
        self.assertEqual(len(self.trade.orders), 1)

    def native_close(self):
        opening = self.store.save_order(snapshot_order(
            status="filled", source="structured-technical", account_scope=self.account.account_scope,
            raw=exchange_order(), stop_loss=95, take_profit=110,
        ))
        self.account.position_rows = [{**position(size="1"), "tradeId": "entry-trade"}]
        self.account.algo_rows = [protection_order()]
        proof = protection_evidence(opening, self.account.algo_rows[0], native=True, position_size=1)
        signal = TradeSignal(
            inst_id="BTC-USDT-SWAP", action="close", confidence=1,
            leverage=1, position_pct=0, source="protective-stop_loss",
        )
        return signal, proof

    async def test_native_protective_close_requires_current_verified_parameters(self):
        signal, proof = self.native_close()
        result = await self.submit(
            signal, size=1, expected_position_trade_id="entry-trade", expected_protection=proof, dry_run=True,
        )
        self.assertTrue(result["accepted"], result)
        self.assertEqual(self.trade.orders, [])
        self.assertEqual(json.loads(result["order"]["raw_json"])["expected_protection"], proof)
        rejected = await self.submit(
            signal, size=1, expected_position_trade_id="entry-trade", expected_protection=proof,
        )
        self.assertEqual(rejected["reasons"], ["native_protection_handoff_required"])
        self.assertEqual(self.trade.orders, [])

    async def test_external_change_between_trigger_and_preflight_prevents_submission(self):
        signal, proof = self.native_close()
        for changes in (
            {"slTriggerPx": "90"}, {"tpTriggerPx": "120"}, {"slTriggerPxType": "last"},
            {"tpOrdPx": "110"}, {"state": "canceled"}, {"state": "effective"},
            {"sz": ".5"}, {"sz": "2"}, {"posSide": "short"}, {"tdMode": "cross"},
            {"side": "buy"}, {"algoId": "replacement"}, {"algoClOrdId": "different"},
        ):
            self.account.algo_rows = [protection_order(**changes)]
            result = await self.submit(
                signal, size=1, expected_position_trade_id="entry-trade", expected_protection=proof,
            )
            self.assertFalse(result["accepted"], changes)
            self.assertIn("close_protection_changed", result["reasons"], changes)
        self.assertEqual(self.trade.orders, [])

    async def test_missing_or_duplicate_native_snapshot_cannot_execute_cached_protection(self):
        signal, proof = self.native_close()
        for rows in ([], [protection_order(), protection_order()]):
            self.account.algo_rows = rows
            result = await self.submit(
                signal, size=1, expected_position_trade_id="entry-trade", expected_protection=proof,
            )
            self.assertIn("close_protection_changed", result["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_protective_execution_without_evidence_is_rejected(self):
        signal, _proof = self.native_close()
        result = await self.submit(signal, size=1, expected_position_trade_id="entry-trade")
        self.assertIn("close_protection_unverified", result["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_partial_fill_fallback_rechecks_current_parent_attached_terms(self):
        parent = exchange_order(state="partially_filled", accFillSz=".5", attachAlgoOrds=[{
            **protection_order(), "sz": "", "attachAlgoClOrdId": protection_order()["algoClOrdId"],
        }])
        opening = self.store.save_order(snapshot_order(
            status="partially_filled", source="structured-technical", account_scope=self.account.account_scope,
            raw=parent,
        ))
        self.account.position_rows = [{**position(size=".5"), "tradeId": "entry-trade"}]
        self.account.pending_rows = [parent]
        proof = protection_evidence(opening, parent, native=False, position_size=.5)
        self.assertIsNotNone(proof)
        signal = TradeSignal(
            inst_id="BTC-USDT-SWAP", action="close", confidence=1,
            leverage=1, position_pct=0, source="protective-stop_loss",
        )
        self.account.pending_rows = [{**parent, "attachAlgoOrds": []}]
        rejected = await self.submit(
            signal, size=.5, expected_position_trade_id="entry-trade", expected_protection=proof,
        )
        self.assertIn("close_protection_changed", rejected["reasons"])
        self.account.pending_rows = [parent]
        accepted = await self.submit(
            signal, size=.5, expected_position_trade_id="entry-trade", expected_protection=proof, dry_run=True,
        )
        self.assertTrue(accepted["accepted"], accepted)
        rejected = await self.submit(
            signal, size=.5, expected_position_trade_id="entry-trade", expected_protection=proof,
        )
        self.assertIn("native_protection_handoff_required", rejected["reasons"])

    async def test_pending_and_position_exposure_share_account_budget(self):
        self.account.position_rows = [position(notional="150")]
        self.account.pending_rows = [{
            "instId": "BTC-USDT-SWAP", "ordId": "pending1", "clOrdId": "pendingclient",
            "sz": "0.2", "accFillSz": "0", "side": "buy", "posSide": "net", "px": "",
        }]
        result = await self.submit()
        self.assertIn("total_exposure_above_limit", result["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_unknown_local_order_reserves_budget_and_matching_pending_is_not_counted_twice(self):
        self.store.save_order({
            "client_order_id": "uncertain1", "status": "submission_unknown",
            "inst_id": "BTC-USDT-SWAP", "side": "buy", "pos_side": "net",
            "ord_type": "market", "td_mode": "isolated", "size": 0.5,
            "risk_notional": 250,
        })
        prepared = await self.preflight.prepare(self.signal, 0.05)
        self.assertEqual(prepared.current_notional, 250)
        result = await self.submit()
        self.assertIn("total_exposure_above_limit", result["reasons"])
        self.account.pending_rows = [{
            "instId": "BTC-USDT-SWAP", "ordId": "pending1", "clOrdId": "uncertain1",
            "sz": "0.5", "accFillSz": "0", "side": "buy", "posSide": "net",
        }]
        prepared = await self.preflight.prepare(self.signal, 0.05)
        self.assertEqual(prepared.current_notional, 250)

    async def test_stale_quote_missing_metadata_and_invalid_steps_fail_closed(self):
        self.market.age = 31000
        self.assertIn("market_data_stale", (await self.submit())["reasons"])
        self.market.age = 0
        self.assertIn("order_size_outside_contract_steps", (await self.submit(size=0.015))["reasons"])
        self.market.rows = []
        self.assertIn("instrument_not_tradable", (await self.submit())["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_unknown_position_notional_cannot_be_treated_as_zero(self):
        self.account.position_rows = [position()]
        self.account.position_rows[0].pop("notionalUsd")
        result = await self.submit()
        self.assertIn("position_notional_invalid", result["reasons"])

    async def test_missing_unrealized_loss_cannot_be_treated_as_zero(self):
        for value in (None, "", "NaN"):
            self.account.position_rows = [position(upl=value)]
            self.assertIn("unrealized_pnl_invalid", (await self.submit())["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_non_reducing_algorithms_are_checked_before_submission(self):
        self.account.algo_rows = [{
            "algoId": "external-trigger", "instId": "ETH-USDT-SWAP", "ordType": "trigger",
            "posSide": "net", "side": "buy", "sz": "100", "reduceOnly": "false",
        }]
        self.assertIn("unvalued_algo_order_exposure", (await self.submit())["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_confirmed_reducing_algorithms_do_not_add_opening_exposure(self):
        self.account.algo_rows = [{
            "algoId": "protection", "instId": "BTC-USDT-SWAP", "ordType": "conditional",
            "posSide": "net", "side": "sell", "sz": "1", "reduceOnly": "true",
        }]
        self.assertTrue((await self.submit())["accepted"])

    async def test_foreign_account_reservation_cannot_be_reused(self):
        self.store.save_order({
            "client_order_id": "foreign", "status": "submission_unknown",
            "inst_id": "BTC-USDT-SWAP", "side": "buy", "pos_side": "net",
            "ord_type": "market", "td_mode": "isolated", "size": .05,
            "account_scope": "another-demo-account", "risk_notional": 25,
        })
        self.assertIn("active_order_account_scope_mismatch", (await self.submit())["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_daily_loss_uses_exchange_bills_and_not_user_supplied_gain(self):
        self.account.bill_rows = [{
            "instId": "BTC-USDT-SWAP", "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
            "billId": "1", "instType": "SWAP", "type": "2", "subType": "5",
            "pnl": "-29", "fee": "-2", "ccy": "USDT",
        }]
        result = await self.submit()
        self.assertIn("daily_loss_limit_reached", result["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_funding_loss_blocks_open_orders_even_with_unrealized_gain(self):
        self.account.position_rows = [position(notional="1", upl="100")]
        self.account.bill_rows = [{
            "billId": "funding1", "instId": "BTC-USDT-SWAP", "instType": "SWAP",
            "type": "8", "subType": "173", "ccy": "USDT", "balChg": "-31",
            "posBalChg": "0", "pnl": "-31", "fee": "0",
            "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        }]
        result = await self.submit()
        self.assertIn("daily_loss_limit_reached", result["reasons"])
        self.assertEqual(self.trade.orders, [])
        self.assertEqual(self.store.bill_snapshot(self.account.account_scope)["summary"]["bills"], 1)

    async def test_funding_and_trade_bills_do_not_double_count_fills(self):
        timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        self.account.fill_rows = [{"fillPnl": "10", "fee": "-1", "feeCcy": "USDT", "ts": timestamp}]
        self.account.bill_rows = [
            {"billId": "trade1", "instId": "BTC-USDT-SWAP", "instType": "SWAP", "type": "2", "subType": "5", "ccy": "USDT", "pnl": "10", "fee": "-1", "ts": timestamp},
            {"billId": "funding1", "instId": "BTC-USDT-SWAP", "instType": "SWAP", "type": "8", "subType": "173", "ccy": "USDT", "pnl": "-2", "fee": "0", "balChg": "0", "posBalChg": "-2", "ts": timestamp},
        ]
        result = await self.submit()
        self.assertTrue(result["accepted"])
        accounting = result["preflight"]["daily_accounting"]
        self.assertEqual(accounting["usd"]["net_pnl"], 7)
        self.assertEqual(accounting["usd"]["funding"], -2)
        self.assertEqual(result["preflight"]["daily_pnl_pct"], 0.7)

    async def test_unknown_bill_currency_or_failed_bill_fetch_blocks_execution(self):
        self.account.bill_rows = [{
            "billId": "unknown1", "instId": "BTC-USDT-SWAP", "instType": "SWAP",
            "type": "2", "subType": "5", "ccy": "UNKNOWN", "pnl": "-1", "fee": "0",
            "ts": str(int(datetime.now(timezone.utc).timestamp() * 1000)),
        }]
        self.assertIn("bill_currency_valuation_unavailable", (await self.submit())["reasons"])
        async def fail(**kwargs):
            raise OkxAccountError("Bill history unavailable")
        self.account.bills_today = fail
        self.assertIn("exchange_preflight_data_unavailable", (await self.submit())["reasons"])
        self.assertEqual(self.trade.orders, [])

    async def test_midnight_during_leverage_setup_requires_new_accounting(self):
        async def cross_midnight(*args):
            clock_patch = patch("app.execution_engine.datetime")
            clock = clock_patch.start()
            self.addCleanup(clock_patch.stop)
            clock.now.return_value = datetime.now(timezone.utc) + timedelta(days=1)
            return {}
        self.trade.set_leverage = cross_midnight
        with self.assertRaisesRegex(OkxOrderRejected, "accounting day"):
            await self.submit()
        self.assertEqual(self.trade.orders, [])
        self.assertEqual(self.store.list_orders()[0]["status"], "rejected")

    async def test_verified_short_close_reduces_exposure_even_after_daily_loss_limit(self):
        self.account.position_rows = [position(size="-0.2", upl="-100", margin="cross")]
        signal = TradeSignal(
            inst_id="BTC-USDT-SWAP", action="close", confidence=0.1,
            leverage=1, position_pct=0,
        )
        result = await self.submit(signal)
        self.assertTrue(result["accepted"])
        self.assertEqual(self.trade.orders[0].side, "buy")
        self.assertEqual(self.trade.orders[0].td_mode, "cross")
        self.assertTrue(self.trade.orders[0].reduce_only)
        self.assertEqual(self.trade.leverages, [])
        replay = await self.submit(signal)
        self.assertTrue(replay["idempotent"])

    async def test_hedged_close_uses_correct_side_without_net_reduce_only_flag(self):
        self.account.mode = "long_short_mode"
        self.account.position_rows = [position("long"), position("short")]
        signal = TradeSignal(
            inst_id="BTC-USDT-SWAP", action="close", confidence=1,
            leverage=1, position_pct=0,
        )
        ambiguous = await self.submit(signal)
        self.assertIn("close_position_missing_or_ambiguous", ambiguous["reasons"])
        result = await self.submit(signal, side_override="buy")
        self.assertTrue(result["accepted"])
        payload = self.trade.orders[0].okx_payload()
        self.assertEqual((payload["side"], payload["posSide"]), ("buy", "short"))
        self.assertNotIn("reduceOnly", payload)

    async def test_leverage_failure_or_emergency_stop_during_preparation_never_places_order(self):
        async def fail(*_args):
            raise OkxTradeError("leverage timeout")

        self.trade.set_leverage = fail
        with self.assertRaises(OkxTradeError):
            await self.submit()
        self.assertEqual(self.trade.orders, [])
        self.assertEqual(self.store.list_orders()[0]["status"], "rejected")
        async def stop(*_args):
            self.engine.safety.stop("test stop")

        self.trade.set_leverage = stop
        with self.assertRaises(OkxOrderRejected):
            await self.submit(self.signal.model_copy(update={"source": "different"}))
        self.assertEqual(self.trade.orders, [])

    async def test_two_distinct_requests_cannot_spend_same_snapshot_budget(self):
        ready = asyncio.Event()
        prepare = self.preflight.prepare
        count = 0

        async def concurrent_prepare(*args):
            nonlocal count
            result = await prepare(*args)
            count += 1
            if count == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 1)
            return result

        self.preflight.prepare = concurrent_prepare
        first_signal = self.signal.model_copy(update={"position_pct": 10, "source": "first"})
        second_signal = self.signal.model_copy(update={"position_pct": 10, "source": "second"})
        results = await asyncio.gather(
            self.submit(first_signal, size=0.4), self.submit(second_signal, size=0.4),
        )
        self.assertEqual(sum(result["accepted"] for result in results), 1)
        rejected = next(result for result in results if not result["accepted"])
        self.assertEqual(rejected["reasons"], ["execution_budget_snapshot_changed"])
        self.assertEqual(len(self.trade.orders), 1)

    async def test_actual_execution_requires_preflight_even_with_a_trade_client(self):
        self.engine.preflight = None
        self.assertEqual((await self.submit())["reasons"], ["exchange_preflight_required"])
        self.assertEqual(self.trade.orders, [])

    async def test_api_executes_shared_preflight_before_sending_any_order(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "preflight-test-token"}), patch.object(
            api_main, "execution_engine", self.engine,
        ), patch.object(api_main, "market_stream", SimpleNamespace(fresh=True)):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api_main.app), base_url="http://app.test",
            ) as client:
                response = await client.post(
                    "/api/v1/execution/signals",
                    headers={"X-Admin-Token": "preflight-test-token"},
                    json={
                        "signal": self.signal.model_dump(mode="json"),
                        "account_equity": 1000000000, "daily_pnl_pct": 100,
                        "current_notional": 0, "size": 100, "dry_run": False,
                    },
                )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["accepted"])
        self.assertEqual(self.trade.orders, [])


class ExchangePaginationTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_orders_fetches_all_pages(self):
        requests = []

        def handler(request):
            requests.append(request)
            count = 100 if len(requests) == 1 else 1
            start = 0 if len(requests) == 1 else 100
            return httpx.Response(200, json={
                "code": "0", "data": [{"ordId": str(i)} for i in range(start, start + count)],
            })

        with patch.dict(os.environ, {"OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass", "OKX_PROXY_URL": ""}):
            account = OkxAccountClient(transport=httpx.MockTransport(handler))
        rows = await account.pending_orders()
        self.assertEqual(len(rows), 101)
        self.assertEqual(requests[1].url.params["after"], "99")

    async def test_fill_pagination_uses_bill_id_and_utc_day_bounds(self):
        requests = []

        def handler(request):
            requests.append(request)
            rows = [{"billId": str(i)} for i in range(100)] if len(requests) == 1 else []
            return httpx.Response(200, json={"code": "0", "data": rows})

        with patch.dict(os.environ, {"OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass", "OKX_PROXY_URL": ""}):
            account = OkxAccountClient(transport=httpx.MockTransport(handler))
        rows = await account.fills_today()
        self.assertEqual(len(rows), 100)
        self.assertEqual(requests[1].url.params["after"], "99")
        begin = int(requests[0].url.params["begin"])
        self.assertEqual(begin % 86400000, 0)
        self.assertGreaterEqual(int(requests[0].url.params["end"]), begin)

    async def test_repeated_page_fails_closed(self):
        def handler(_request):
            return httpx.Response(200, json={
                "code": "0", "data": [{"ordId": str(i)} for i in range(100)],
            })

        with patch.dict(os.environ, {"OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass", "OKX_PROXY_URL": ""}):
            account = OkxAccountClient(transport=httpx.MockTransport(handler))
        with self.assertRaises(OkxAccountError):
            await account.pending_orders()


class LeverageRequestTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        with patch.dict(os.environ, {
            "OKX_API_KEY": "key", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass",
            "TRADING_MODE": "demo", "OKX_DEMO": "true", "EXECUTION_ENABLED": "true",
            "OKX_PROXY_URL": "",
        }):
            return OkxTradeClient(transport=httpx.MockTransport(handler))

    async def test_leverage_post_is_signed_and_hedge_side_is_scoped(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"code": "0", "data": [json.loads(request.content)]})

        client = self.client(handler)
        await client.set_leverage("BTC-USDT-SWAP", 2, "isolated", "short")
        await client.set_leverage("BTC-USDT-SWAP", 2, "cross", "net")
        self.assertEqual(requests[0].url.path, "/api/v5/account/set-leverage")
        self.assertEqual(json.loads(requests[0].content)["posSide"], "short")
        self.assertNotIn("posSide", json.loads(requests[1].content))
        self.assertEqual(requests[0].headers["x-simulated-trading"], "1")
        self.assertTrue(requests[0].headers["OK-ACCESS-SIGN"])

    async def test_mismatched_leverage_acknowledgement_is_rejected(self):
        def handler(_request):
            return httpx.Response(200, json={"code": "0", "data": [{
                "instId": "BTC-USDT-SWAP", "lever": "20", "mgnMode": "isolated",
            }]})

        with self.assertRaises(OkxTradeError):
            await self.client(handler).set_leverage("BTC-USDT-SWAP", 2, "isolated", "net")


class ReservationGenerationTests(unittest.TestCase):
    def test_second_store_cannot_claim_with_stale_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            first, second = StateStore(path), StateStore(path)
            before = second.execution_snapshot()[0]
            order = {
                "client_order_id": "first", "status": "preparing",
                "inst_id": "BTC-USDT-SWAP", "side": "buy", "pos_side": "net",
                "ord_type": "market", "td_mode": "isolated", "size": 1,
                "risk_notional": 200,
            }
            first.claim_order(order, expected_generation=before)
            with self.assertRaises(ExposureSnapshotChanged):
                second.claim_order({**order, "client_order_id": "second"}, expected_generation=before)
            self.assertEqual(len(first.list_orders()), 1)

    def test_invalid_risk_configuration_is_rejected_at_startup(self):
        for change in ({"max_leverage": 0}, {"max_daily_loss_pct": 0}, {"max_total_notional_pct": float("inf")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                RiskLimits(**change)
