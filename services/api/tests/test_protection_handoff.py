import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.account_sync import AccountSynchronizer
from app.execution_engine import ExecutionEngine
from app.okx_account import OkxAccountClient
from app.okx_market import OkxMarketClient
from app.okx_trade import OkxTradeClient, OkxTradeError, OrderRequest
from app.order_preflight import OrderPreflight
from app.protection_handoff import ProtectionHandoff
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
        )
        self.manager = ProtectionHandoff(self.store, self.sync, self.engine)

    async def entry(self, name="entry1", size=2, stop=90):
        order = OrderRequest(
            inst_id=SYMBOL, side="buy", sz=size, stop_loss=stop, take_profit=120, cl_ord_id=name,
        )
        with self.exchange.lock:
            raw = self.exchange._create_order(order.okx_payload())
            self.exchange._fill(raw["ordId"])
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


if __name__ == "__main__":
    unittest.main()
