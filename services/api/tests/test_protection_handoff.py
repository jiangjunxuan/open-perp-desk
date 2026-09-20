import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.account_sync import AccountSynchronizer
from app.execution_engine import ExecutionEngine
from app.okx_account import OkxAccountClient, OkxAccountError
from app.okx_market import OkxMarketClient
from app.okx_trade import OkxTradeClient, OkxTradeError, OrderRequest
from app.order_preflight import OrderPreflight
from app.position_lots import positions_with_lots
from app.protection_handoff import ProtectionHandoff, close_context
from app.risk_engine import RiskEngine
from app.state_store import StateStore
from app.trading_signal import TradeSignal
from tests.fixtures.exchange_server import ExchangeServer, SYMBOL


class ProtectionHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "handoff.sqlite3")
        self.exchange = ExchangeServer()
        self.addAsyncCleanup(self.exchange.close)
        await self.exchange.start()
        with patch.dict(os.environ, {
            **self.exchange.environment(), "EXECUTION_ENABLED": "true", "OKX_DEMO": "true",
            "TRADING_MODE": "demo", "LIVE_TRADING_ENABLED": "false", "OKX_PROXY_URL": "",
        }):
            self.account = OkxAccountClient()
            self.market = OkxMarketClient()
            self.trade = OkxTradeClient()
        self.reopen()

    def reopen(self):
        self.store = StateStore(self.path)
        self.sync = AccountSynchronizer(self.store, self.account, SimpleNamespace(snapshot=lambda: {}))
        self.engine = ExecutionEngine(
            self.store, RiskEngine(), self.trade, SimpleNamespace(configured=False),
            preflight=OrderPreflight(self.account, self.market, self.store),
            private_stream_ready=lambda: True,
        )
        self.manager = ProtectionHandoff(self.store, self.sync, self.engine)

    async def entry(self, name="entry1", size=2, stop=90, filled=None, side="buy", target=120):
        order = OrderRequest(
            inst_id=SYMBOL, side=side, sz=size, stop_loss=stop, take_profit=target, cl_ord_id=name,
        )
        with self.exchange.lock:
            raw = self.exchange._create_order(order.okx_payload())
            self.exchange._fill(raw["ordId"], quantity=filled)
        self.sync._save_regular_order(dict(raw), source="structured-technical")
        result = await self.sync.sync_rest()
        self.assertNotIn("errors", result, result)
        allocation = self.store.position_lots(self.store.list_positions()[0])
        self.assertEqual(allocation["status"], "verified", allocation)
        return raw

    async def run_handoff(self, **changes):
        return await self.manager.run(SYMBOL, 85, dry_run=False, market_data_fresh=True, **changes)

    def cancellations(self):
        return [row for row in self.exchange.posts if row["path"] == "/api/v5/trade/cancel-algos"]

    async def test_cancel_confirmation_precedes_exact_lot_close_and_restart_does_not_resend(self):
        await self.entry()
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        paths = [row["path"] for row in self.exchange.posts]
        self.assertEqual(paths, ["/api/v5/trade/cancel-algos", "/api/v5/trade/order"])
        self.assertEqual(result["order"]["size"], 2)
        self.assertTrue(result["order"]["reduce_only"])
        context = json.loads(result["order"]["protection_context_json"])
        self.assertEqual(context["kind"], "handoff")
        self.reopen()
        await self.sync.sync_rest()
        pending = await self.run_handoff()
        self.assertEqual(pending["reasons"], ["handoff_close_unconfirmed"])
        self.assertEqual(len(self.exchange.order_submissions), 1)
        self.exchange.fill(result["order"]["exchange_order_id"])
        await self.sync.sync_rest()
        completed = await self.run_handoff()
        self.assertEqual(completed["action"], "protection_handoff_complete")
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope), [])
        self.assertEqual(len(self.cancellations()), 1)
        self.assertEqual(self.exchange.errors, [])

    async def test_preview_does_not_create_handoff_cancel_or_submit(self):
        await self.entry()
        result = await self.manager.run(SYMBOL, 85, dry_run=True, market_data_fresh=True)
        self.assertTrue(result["accepted"], result)
        self.assertTrue(result["dry_run"])
        self.assertEqual(self.exchange.posts, [])
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope), [])

    async def test_cancellation_ack_is_not_terminal_and_new_entries_are_blocked(self):
        await self.entry()
        async def accepted_only(inst_id, algo_id):
            return {"code": "0", "data": [{"algoId": algo_id, "sCode": "0"}]}
        with patch.object(self.trade, "cancel_algo_order", side_effect=accepted_only):
            result = await self.run_handoff()
        self.assertEqual(result["action"], "protection_cancel_pending")
        self.assertEqual(self.exchange.order_submissions, [])
        signal = TradeSignal(
            inst_id=SYMBOL, action="open_long", confidence=.9, leverage=2, position_pct=5,
            entry_price=float(self.exchange.price), stop_loss=90, take_profit=120,
        )
        blocked = await self.engine.submit_signal(signal, account_equity=1000, daily_pnl_pct=0)
        self.assertEqual(blocked["reasons"], ["protection_handoff_pending"])
        self.assertEqual(self.exchange.order_submissions, [])

    async def test_lost_cancel_response_is_recovered_by_query_before_close(self):
        await self.entry()
        cancel = self.trade.cancel_algo_order
        async def lose(inst_id, algo_id):
            await cancel(inst_id, algo_id)
            raise OkxTradeError("fixture response lost")
        with patch.object(self.trade, "cancel_algo_order", side_effect=lose):
            result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(len(self.cancellations()), 1)
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_failed_cancel_retries_only_after_fresh_live_query_and_backoff(self):
        await self.entry()
        with patch.object(self.trade, "cancel_algo_order", side_effect=OkxTradeError("fixture 503")) as cancel:
            result = await self.run_handoff()
            self.assertEqual(result["action"], "protection_cancel_pending")
            await self.run_handoff()
            self.assertEqual(cancel.call_count, 1)
        handoff = self.store.protection_handoffs(self.account.account_scope)[0]
        self.store.update_protection_handoff(handoff, cancel_after_ms=0)
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_pending_cancel_survives_restart_but_waits_for_fresh_market_before_retry(self):
        await self.entry()
        async def accepted_only(inst_id, algo_id):
            return {"data": [{"algoId": algo_id, "sCode": "0"}]}
        with patch.object(self.trade, "cancel_algo_order", side_effect=accepted_only):
            await self.run_handoff()
        handoff = self.store.protection_handoffs(self.account.account_scope)[0]
        self.store.update_protection_handoff(handoff, cancel_after_ms=0)
        self.reopen()
        result = await self.manager.run(SYMBOL, 85, dry_run=False, market_data_fresh=False)
        self.assertEqual(result["reasons"], ["handoff_market_data_stale"])
        self.assertEqual(self.exchange.posts, [])
        self.assertEqual(next(iter(self.exchange.algos.values()))["state"], "live")
        pending = self.store.protection_handoffs(self.account.account_scope)[0]
        self.assertEqual(pending["cancel_attempts"], 1)
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(len(self.cancellations()), 1)
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_native_trigger_race_never_sends_a_second_close(self):
        await self.entry()
        async def native_wins(inst_id, algo_id):
            self.exchange.trigger_protection(algo_id)
            return {"code": "0", "data": [{"algoId": algo_id, "sCode": "51400"}]}
        with patch.object(self.trade, "cancel_algo_order", side_effect=native_wins):
            result = await self.run_handoff()
        self.assertEqual(result["action"], "protection_native_completed", result)
        self.assertEqual(self.exchange.order_submissions, [])
        self.assertEqual(self.store.list_positions(), [])

    async def test_merged_position_closes_only_triggered_lot_and_retries_partial_remainder(self):
        await self.entry("entry1", size=1, stop=80)
        await self.entry("entry2", size=3, stop=90)
        first = await self.run_handoff()
        self.assertTrue(first["accepted"], first)
        close_id = first["order"]["exchange_order_id"]
        self.assertEqual(first["order"]["size"], 3)
        self.exchange.fill(close_id, quantity=1)
        with self.exchange.lock:
            self.exchange.orders[close_id]["state"] = "canceled"
        await self.sync.sync_rest()
        self.reopen()
        await self.sync.sync_rest()
        second = await self.run_handoff()
        self.assertTrue(second["accepted"], second)
        self.assertEqual(second["order"]["size"], 2)
        self.assertNotEqual(first["order"]["client_order_id"], second["order"]["client_order_id"])
        self.exchange.fill(second["order"]["exchange_order_id"])
        await self.sync.sync_rest()
        await self.run_handoff()
        position = self.store.list_positions()[0]
        self.assertEqual(position["size"], 1)
        allocation = self.store.position_lots(position)
        self.assertEqual([lot["opening_order_id"] for lot in allocation["lots"]], ["entry1"])
        self.assertEqual(next(iter(self.exchange.algos.values()))["state"], "live")
        self.assertEqual(len(self.cancellations()), 1)
        self.assertEqual(len(self.exchange.order_submissions), 2)

    async def test_close_response_loss_keeps_one_order_until_reconciliation(self):
        await self.entry()
        place = self.trade.place_order
        async def lose(order):
            await place(order)
            raise OkxTradeError("fixture lost close acknowledgement")
        with patch.object(self.trade, "place_order", side_effect=lose):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_close_outcome_requires_reconciliation"])
        self.reopen()
        await self.sync.sync_rest()
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_close_unconfirmed"])
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_concurrent_managers_cannot_double_cancel_or_double_close(self):
        await self.entry()
        second = ProtectionHandoff(self.store, self.sync, self.engine)
        results = await asyncio.gather(
            self.run_handoff(), second.run(SYMBOL, 85, dry_run=False, market_data_fresh=True),
        )
        self.assertEqual(sum(bool(row.get("accepted")) and not row.get("idempotent") for row in results), 1, results)
        self.assertEqual(len(self.cancellations()), 1)
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_emergency_stop_and_account_mismatch_prevent_cancel(self):
        await self.entry()
        self.engine.safety.stop("fixture")
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_execution_locked"])
        self.assertEqual(self.exchange.posts, [])

    async def test_scope_change_during_native_query_does_not_cancel_in_new_account(self):
        await self.entry()
        query = self.account.algo_order_details
        async def change(*args, **kwargs):
            result = await query(*args, **kwargs)
            self.account.api_key = self.trade.api_key = "another-fixture-account"
            return result
        with patch.object(self.account, "algo_order_details", side_effect=change):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_account_changed"])
        self.assertEqual(self.exchange.posts, [])

    async def test_stale_market_or_disabled_execution_cannot_remove_native_protection(self):
        await self.entry()
        result = await self.manager.run(SYMBOL, 85, dry_run=False, market_data_fresh=False)
        self.assertEqual(result["reasons"], ["handoff_risk_rejected"])
        self.trade.execution_enabled = False
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_execution_locked"])
        self.assertEqual(self.exchange.posts, [])
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope), [])

    async def test_forged_ready_state_cannot_bypass_fresh_native_cancellation_check(self):
        await self.entry()
        async def accepted_only(inst_id, algo_id):
            return {"data": [{"algoId": algo_id, "sCode": "0"}]}
        with patch.object(self.trade, "cancel_algo_order", side_effect=accepted_only):
            await self.run_handoff()
        handoff = self.store.protection_handoffs(self.account.account_scope)[0]
        self.store.update_protection_handoff(handoff, status="ready")
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_changed"])
        self.assertEqual(self.exchange.order_submissions, [])

    async def test_position_change_before_atomic_close_claim_prevents_submission(self):
        await self.entry()
        claim = self.store.claim_order
        def changed(record, **kwargs):
            if record.get("protection_context", {}).get("kind") == "handoff":
                current = self.store.list_positions()[0]
                self.store.upsert_position({**current, "exchange_trade_id": "another-trade"})
            return claim(record, **kwargs)
        with patch.object(self.store, "claim_order", side_effect=changed):
            result = await self.run_handoff()
        self.assertFalse(result["accepted"], result)
        self.assertIn("execution_budget_snapshot_changed", result["reasons"])
        self.assertEqual(len(self.cancellations()), 1)
        self.assertEqual(self.exchange.order_submissions, [])

    async def test_manual_flat_position_does_not_complete_while_local_close_is_pending(self):
        await self.entry()
        first = await self.run_handoff()
        self.assertTrue(first["accepted"], first)
        with self.exchange.lock:
            manual = self.exchange._create_order({
                "instId": SYMBOL, "posSide": "net", "tdMode": "isolated", "side": "sell",
                "ordType": "market", "sz": "2", "reduceOnly": True,
            })
            self.exchange._fill(manual["ordId"])
        await self.sync.sync_rest()
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_close_unconfirmed"])
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope)[0]["status"], "closing")
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_external_amendment_after_intent_requires_review_without_close(self):
        await self.entry()
        async def amend(inst_id, algo_id):
            with self.exchange.lock:
                self.exchange.algos[algo_id].update(slTriggerPx="70", state="canceled")
            return {"code": "0", "data": [{"algoId": algo_id, "sCode": "0"}]}
        with patch.object(self.trade, "cancel_algo_order", side_effect=amend):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_cancellation_unverified"])
        self.assertEqual(self.exchange.order_submissions, [])
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope)[0]["status"], "review")

    async def test_partial_parent_cancel_precedes_close_of_confirmed_fill(self):
        await self.entry(size=3, filled=1)
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 1)
        self.assertEqual([row["path"] for row in self.exchange.posts], [
            "/api/v5/trade/cancel-order", "/api/v5/trade/order",
        ])
        handoff = self.store.protection_handoffs(self.account.account_scope)[0]
        self.assertEqual(json.loads(handoff["evidence_json"])["kind"], "attached")
        self.assertIsNone(handoff["native_evidence_json"])
        self.reopen()
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_close_unconfirmed"])
        self.exchange.fill(self.store.get_order(handoff["close_order_id"])["exchange_order_id"])
        await self.run_handoff()
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope), [])
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_partial_parent_cancel_ack_cannot_authorize_close_or_forged_ready_state(self):
        await self.entry(size=3, filled=1)
        async def accepted_only(inst_id, order_id):
            return {"data": [{"ordId": order_id, "sCode": "0"}]}
        with patch.object(self.trade, "cancel_order", side_effect=accepted_only):
            result = await self.run_handoff()
        self.assertEqual(result["action"], "protection_opening_cancel_pending")
        self.assertEqual(self.exchange.order_submissions, [])
        handoff = self.store.protection_handoffs(self.account.account_scope)[0]
        handoff = self.store.update_protection_handoff(handoff, status="ready")
        position = self.store.list_positions()[0]
        result = await self.engine.submit_signal(
            self.manager._signal(handoff), account_equity=0, daily_pnl_pct=0, size=1,
            expected_position_trade_id=position["exchange_trade_id"], expected_protection=close_context(handoff),
        )
        self.assertEqual(result["reasons"], ["close_handoff_parent_unverified"])
        self.assertEqual(self.exchange.order_submissions, [])

    async def test_partial_parent_lost_cancel_response_recovers_by_exact_query(self):
        await self.entry(size=3, filled=1)
        cancel = self.trade.cancel_order
        async def lose(inst_id, order_id):
            await cancel(inst_id, order_id)
            raise OkxTradeError("fixture lost parent cancellation response")
        with patch.object(self.trade, "cancel_order", side_effect=lose):
            result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(len(self.exchange.posts), 2)

    async def test_more_parent_fills_during_cancel_are_included_in_the_lot_close(self):
        await self.entry(size=3, filled=1)
        cancel = self.trade.cancel_order
        async def fill_then_cancel(inst_id, order_id):
            self.exchange.fill(order_id, quantity=1)
            return await cancel(inst_id, order_id)
        with patch.object(self.trade, "cancel_order", side_effect=fill_then_cancel):
            result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 2)
        self.assertEqual(len(self.cancellations()), 0)

    async def test_parent_full_fill_race_binds_then_cancels_generated_native_before_close(self):
        await self.entry(size=3, filled=1)
        cancel = self.trade.cancel_order
        async def fill_then_cancel(inst_id, order_id):
            self.exchange.fill(order_id)
            return await cancel(inst_id, order_id)
        with patch.object(self.trade, "cancel_order", side_effect=fill_then_cancel):
            result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 3)
        self.assertEqual([row["path"] for row in self.exchange.posts], [
            "/api/v5/trade/cancel-order", "/api/v5/trade/cancel-algos", "/api/v5/trade/order",
        ])
        handoff = self.store.protection_handoffs(self.account.account_scope)[0]
        self.assertEqual(json.loads(handoff["evidence_json"])["size"], 1)
        self.assertEqual(json.loads(handoff["native_evidence_json"])["size"], 3)
        self.assertIsNone(self.store.bind_handoff_native(handoff, json.loads(handoff["native_evidence_json"])))

    async def test_full_parent_waits_for_delayed_native_generation_across_restart(self):
        await self.entry(size=3, filled=1)
        saved = {}
        cancel = self.trade.cancel_order
        async def delay_native(inst_id, order_id):
            self.exchange.fill(order_id)
            with self.exchange.lock:
                saved.update(self.exchange.algos)
                self.exchange.algos.clear()
            return await cancel(inst_id, order_id)
        with patch.object(self.trade, "cancel_order", side_effect=delay_native):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_creation_pending"])
        self.assertEqual(self.exchange.order_submissions, [])
        self.reopen()
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_creation_pending"])
        with self.exchange.lock:
            self.exchange.algos.update(saved)
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 3)
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_generated_native_trigger_wins_parent_cancellation_race_without_second_close(self):
        await self.entry(size=3, filled=1)
        cancel = self.trade.cancel_order
        async def native_wins(inst_id, order_id):
            self.exchange.fill(order_id)
            self.exchange.trigger_protection(next(iter(self.exchange.algos)))
            return await cancel(inst_id, order_id)
        with patch.object(self.trade, "cancel_order", side_effect=native_wins):
            result = await self.run_handoff()
        self.assertEqual(result["action"], "protection_native_completed", result)
        self.assertEqual(self.exchange.order_submissions, [])
        self.assertEqual(len(self.cancellations()), 0)

    async def test_native_query_outage_after_parent_cancel_never_means_absence(self):
        await self.entry(size=3, filled=1)
        with patch.object(self.account, "algo_order_details", side_effect=OkxAccountError("fixture outage", code="50011")):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_query_unavailable"])
        self.assertEqual(self.exchange.order_submissions, [])
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(sum(row["path"] == "/api/v5/trade/cancel-order" for row in self.exchange.posts), 1)

    async def test_malformed_empty_native_lookup_does_not_authorize_partial_parent_close(self):
        await self.entry(size=3, filled=1)
        with patch.object(self.account, "algo_order_details", side_effect=OkxAccountError("Exact algo lookup did not return one order")):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_query_unavailable"])
        self.assertEqual(self.exchange.order_submissions, [])

    async def test_parent_amendment_during_cancel_requires_review(self):
        await self.entry(size=3, filled=1)
        cancel = self.trade.cancel_order
        async def amend(inst_id, order_id):
            self.exchange.orders[order_id]["attachAlgoOrds"][0]["slTriggerPx"] = "70"
            return await cancel(inst_id, order_id)
        with patch.object(self.trade, "cancel_order", side_effect=amend):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_opening_changed"])
        self.assertEqual(self.exchange.order_submissions, [])

    async def test_partial_parent_concurrent_managers_cancel_and_close_once(self):
        await self.entry(size=3, filled=1)
        results = await asyncio.gather(self.run_handoff(), self.run_handoff())
        self.assertEqual(sum(bool(row.get("accepted")) and not row.get("idempotent") for row in results), 1, results)
        self.assertEqual(sum(row["path"] == "/api/v5/trade/cancel-order" for row in self.exchange.posts), 1)
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_parent_cancel_503_retries_require_backoff_and_fresh_market(self):
        await self.entry(size=3, filled=1)
        with patch.object(self.trade, "cancel_order", side_effect=OkxTradeError("fixture 503")) as cancel:
            await self.run_handoff()
            await self.run_handoff()
            self.assertEqual(cancel.call_count, 1)
        handoff = self.store.protection_handoffs(self.account.account_scope)[0]
        self.store.update_protection_handoff(handoff, cancel_after_ms=0)
        result = await self.manager.run(SYMBOL, 85, dry_run=False, market_data_fresh=False)
        self.assertEqual(result["reasons"], ["handoff_market_data_stale"])
        self.assertEqual(self.exchange.posts, [])
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)

    async def test_already_canceled_partial_parent_is_recovered_without_second_cancel(self):
        raw = await self.entry(size=3, filled=1)
        await self.trade.cancel_order(SYMBOL, raw["ordId"])
        await self.sync.sync_rest()
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 1)
        self.assertEqual(sum(row["path"] == "/api/v5/trade/cancel-order" for row in self.exchange.posts), 1)

    async def test_partial_parent_preview_does_not_cancel_or_create_handoff(self):
        await self.entry(size=3, filled=1)
        result = await self.manager.run(SYMBOL, 85, dry_run=True, market_data_fresh=True)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(self.exchange.posts, [])
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope), [])

    async def test_parent_query_account_change_prevents_cancellation(self):
        await self.entry(size=3, filled=1)
        query = self.account.order_details
        calls = 0
        async def change(*args, **kwargs):
            nonlocal calls
            result = await query(*args, **kwargs)
            calls += 1
            if calls == 2:
                self.account.api_key = self.trade.api_key = "another-fixture-account"
            return result
        with patch.object(self.account, "order_details", side_effect=change):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_account_changed"])
        self.assertEqual(self.exchange.posts, [])

    async def test_native_appearing_during_close_preflight_blocks_unprotected_close(self):
        raw = await self.entry(size=3, filled=1)
        prepare = self.engine.preflight.prepare
        async def appear(*args, **kwargs):
            if kwargs.get("expected_protection", {}).get("kind") == "handoff":
                with self.exchange.lock:
                    self.exchange.algos["latealgo"] = {
                        **raw["attachAlgoOrds"][0], "algoId": "latealgo",
                        "algoClOrdId": raw["attachAlgoOrds"][0]["attachAlgoClOrdId"],
                        "instId": SYMBOL, "posSide": "net", "tdMode": "isolated", "side": "sell",
                        "sz": "1", "reduceOnly": True, "ordType": "oco", "state": "live",
                        "cTime": raw["cTime"], "uTime": raw["uTime"],
                    }
            return await prepare(*args, **kwargs)
        with patch.object(self.engine.preflight, "prepare", side_effect=appear):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["close_handoff_unverified"])
        self.assertEqual(self.exchange.order_submissions, [])
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(len(self.cancellations()), 1)

    async def test_legacy_handoff_schema_preserves_pending_intent_during_upgrade(self):
        await self.entry()
        async def accepted_only(inst_id, algo_id):
            return {"data": [{"algoId": algo_id, "sCode": "0"}]}
        with patch.object(self.trade, "cancel_algo_order", side_effect=accepted_only):
            await self.run_handoff()
        original = self.store.protection_handoffs(self.account.account_scope)[0]
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("ALTER TABLE protection_handoffs DROP COLUMN native_evidence_json")
            connection.execute("ALTER TABLE protection_handoffs DROP COLUMN native_settlement_json")
            connection.commit()
        self.reopen()
        restored = self.store.protection_handoffs(self.account.account_scope)[0]
        self.assertEqual(restored, original)
        result = await self.run_handoff()
        self.assertEqual(result["action"], "protection_cancel_pending")
        self.assertEqual(self.exchange.posts, [])
        self.store.update_protection_handoff(restored, cancel_after_ms=0)
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(len(self.cancellations()), 1)
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def native_remainder(self, *, state="effective", child_state="canceled", filled=1, side="buy"):
        await self.entry(size=4, side=side, stop=90 if side == "buy" else 120, target=120 if side == "buy" else 90)
        algo_id = list(self.exchange.algos)[-1]
        self.exchange.trigger_protection(algo_id, filled=filled, child_state=child_state, state=state)
        await self.sync.sync_rest()
        return algo_id, self.exchange.algos[algo_id]["ordIdList"][0]

    async def test_native_partial_fill_closes_only_ledger_remainder_after_price_recovers(self):
        await self.native_remainder()
        self.assertTrue(self.manager.has_work(SYMBOL))
        result = await self.manager.run(SYMBOL, 105, dry_run=False, market_data_fresh=True)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 3)
        context = json.loads(result["order"]["protection_context_json"])
        self.assertEqual(context["native_settlement"]["issued_size"], "4")
        self.assertEqual(context["native_settlement"]["filled_size"], "1")
        self.assertEqual(len(self.cancellations()), 0)
        self.reopen()
        pending = await self.run_handoff()
        self.assertEqual(pending["reasons"], ["handoff_close_unconfirmed"])
        self.exchange.fill(result["order"]["exchange_order_id"])
        await self.run_handoff()
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope), [])
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_pending_native_child_is_never_covered_by_a_second_close(self):
        _, child_id = await self.native_remainder(child_state="partially_filled")
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_child_pending"])
        self.assertEqual(self.exchange.order_submissions, [])
        with self.exchange.lock:
            self.exchange.orders[child_id]["state"] = "canceled"
        await self.sync.sync_rest()
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 3)

    async def test_partially_effective_native_is_canceled_before_remainder_close(self):
        algo_id, _ = await self.native_remainder(state="partially_effective")
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(self.exchange.algos[algo_id]["state"], "canceled")
        self.assertEqual(result["order"]["size"], 3)
        self.assertEqual([row["path"] for row in self.exchange.posts], [
            "/api/v5/trade/cancel-algos", "/api/v5/trade/order",
        ])
        settlement = json.loads(result["order"]["protection_context_json"])["native_settlement"]
        self.assertEqual(settlement["state"], "canceled")

    async def test_partial_native_cancel_ack_does_not_prove_terminal_state(self):
        await self.native_remainder(state="partially_effective")
        async def accepted_only(inst_id, algo_id):
            return {"data": [{"algoId": algo_id, "sCode": "0"}]}
        with patch.object(self.trade, "cancel_algo_order", side_effect=accepted_only) as cancel:
            result = await self.run_handoff()
            self.assertEqual(result["action"], "protection_native_cancel_pending")
            await self.run_handoff()
            self.assertEqual(cancel.call_count, 1)
        self.assertEqual(self.exchange.order_submissions, [])
        handoff = self.store.protection_handoffs(self.account.account_scope)[0]
        self.assertEqual(handoff["status"], "native_cancel_pending")
        self.assertIsNone(handoff["native_settlement_json"])

    async def test_lost_partial_native_cancel_response_is_resolved_by_query(self):
        await self.native_remainder(state="partially_effective")
        cancel = self.trade.cancel_algo_order
        async def lose(*args):
            await cancel(*args)
            raise OkxTradeError("fixture lost native cancellation response")
        with patch.object(self.trade, "cancel_algo_order", side_effect=lose):
            result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(len(self.cancellations()), 1)
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_native_child_lookup_outage_pauses_without_local_close(self):
        _, child_id = await self.native_remainder()
        query = self.account.order_details
        async def outage(*args, **kwargs):
            if kwargs.get("ord_id") == child_id:
                raise OkxAccountError("fixture 503")
            return await query(*args, **kwargs)
        with patch.object(self.account, "order_details", side_effect=outage):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_children_unavailable"])
        self.assertEqual(self.exchange.posts, [])

    async def test_native_child_side_and_reduce_only_must_match_the_owner(self):
        _, child_id = await self.native_remainder()
        query = self.account.order_details
        async def wrong_side(*args, **kwargs):
            raw = await query(*args, **kwargs)
            return {**raw, "side": "buy", "reduceOnly": False} if kwargs.get("ord_id") == child_id else raw
        with patch.object(self.account, "order_details", side_effect=wrong_side):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_child_identity_unverified"])
        self.assertEqual(self.exchange.posts, [])

    async def test_native_issued_quantity_is_not_treated_as_filled_quantity(self):
        algo_id, _ = await self.native_remainder()
        self.exchange.algos[algo_id]["actualSz"] = "3"
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_issued_quantity_unverified"])
        self.assertEqual(self.exchange.posts, [])

    async def test_changed_native_fill_after_settlement_requires_review(self):
        _, child_id = await self.native_remainder()
        first = await self.run_handoff()
        self.assertTrue(first["accepted"], first)
        self.exchange.orders[child_id]["accFillSz"] = "2"
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_settlement_changed"])
        self.assertEqual(len(self.exchange.order_submissions), 1)
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope)[0]["status"], "review")

    async def test_native_settlement_is_rechecked_before_atomic_local_close_claim(self):
        _, child_id = await self.native_remainder()
        prepare = self.engine.preflight.prepare
        async def change(*args, **kwargs):
            if kwargs.get("expected_protection", {}).get("kind") == "handoff":
                self.exchange.orders[child_id]["accFillSz"] = "2"
            return await prepare(*args, **kwargs)
        with patch.object(self.engine.preflight, "prepare", side_effect=change):
            result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["close_handoff_settlement_changed"])
        self.assertEqual(self.exchange.order_submissions, [])

    async def test_equivalent_native_quantity_formatting_does_not_invalidate_settlement(self):
        _, child_id = await self.native_remainder()
        first = await self.run_handoff()
        self.assertTrue(first["accepted"], first)
        self.exchange.orders[child_id].update(sz="4.000", accFillSz="1.00")
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_close_unconfirmed"])
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_native_then_local_partial_close_continues_only_remaining_lot(self):
        await self.native_remainder()
        first = await self.run_handoff()
        self.assertTrue(first["accepted"], first)
        self.exchange.fill(first["order"]["exchange_order_id"], quantity=1)
        self.exchange.orders[first["order"]["exchange_order_id"]]["state"] = "canceled"
        self.reopen()
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 2)
        self.assertNotEqual(result["order"]["client_order_id"], first["order"]["client_order_id"])
        self.assertEqual(len(self.exchange.order_submissions), 2)

    async def test_native_remainder_for_net_short_uses_buy_and_positive_quantity(self):
        await self.native_remainder(side="sell")
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["side"], "buy")
        self.assertEqual(result["order"]["size"], 3)

    async def test_zero_filled_terminal_native_child_allows_full_lot_remainder(self):
        await self.native_remainder(filled=0)
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 4)
        self.assertEqual(json.loads(result["order"]["protection_context_json"])["native_settlement"]["filled_size"], "0")

    async def test_native_remainder_cannot_consume_a_different_opening_lot(self):
        await self.entry(name="untouched", size=2, stop=80, target=140)
        await self.native_remainder()
        result = await self.run_handoff()
        self.assertTrue(result["accepted"], result)
        self.assertEqual(result["order"]["size"], 3)
        self.exchange.fill(result["order"]["exchange_order_id"])
        await self.run_handoff()
        lot = positions_with_lots(self.store)[0]["lot_allocation"]["lots"][0]
        self.assertEqual(lot["opening_order_id"], "untouched")
        self.assertEqual(float(lot["remaining_size"]), 2)
        self.assertEqual(lot["protection"]["state"], "native_matched")
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_disappearing_native_children_after_settlement_requires_review(self):
        algo_id, _ = await self.native_remainder()
        first = await self.run_handoff()
        self.assertTrue(first["accepted"], first)
        self.exchange.algos[algo_id].update(state="canceled", actualSz="0", ordIdList=[])
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_settlement_changed"])
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope)[0]["status"], "review")
        self.assertEqual(len(self.exchange.order_submissions), 1)

    async def test_pending_child_after_terminal_settlement_requires_review(self):
        _, child_id = await self.native_remainder()
        first = await self.run_handoff()
        self.assertTrue(first["accepted"], first)
        self.exchange.orders[child_id]["state"] = "partially_filled"
        result = await self.run_handoff()
        self.assertEqual(result["reasons"], ["handoff_native_child_pending"])
        self.assertEqual(self.store.protection_handoffs(self.account.account_scope)[0]["status"], "review")
        self.assertEqual(len(self.exchange.order_submissions), 1)


if __name__ == "__main__":
    unittest.main()
