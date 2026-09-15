import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import main as api_main
from app.protection_adjustment import ProtectionAdjustment
from app.protection_review import ProtectionReviewer, ProtectionReviewError
from app.position_protection import protection_evidence
from app.okx_account import OkxAccountClient
from app.okx_trade import OrderRequest
from app.state_store import ExposureSnapshotChanged, StateStore
from tests import test_protection_handoff as handoff_fixture
from tests.fixtures.exchange_server import ExchangeServer, SYMBOL, milliseconds
from tests.fixtures.api_process import ApiProcess
from tests.test_realtime import next_event


class ProtectionReviewTests(unittest.IsolatedAsyncioTestCase):
    reopen = handoff_fixture.ProtectionHandoffTests.reopen
    entry = handoff_fixture.ProtectionHandoffTests.entry

    async def asyncSetUp(self):
        await handoff_fixture.ProtectionHandoffTests.asyncSetUp(self)
        self.reviewer = ProtectionReviewer(self.store, self.sync)

    async def handoff(self, **entry):
        await self.entry(**entry)
        position, lot, proof, reason, _ = self.manager._candidate(SYMBOL, 85)
        record = self.store.create_protection_handoff({
            "handoff_id": "review-handoff", "lot_id": lot["lot_id"], "evidence": proof,
            "reason": reason, "trigger_price": 85,
        }, position, expected_generation=self.store.execution_snapshot()[0])
        return self.store.update_protection_handoff(
            record, status="review", last_error="handoff_native_changed", cancel_attempts=3,
            cancel_after_ms=9999999999999,
        )

    async def reduce(self, size):
        with self.exchange.lock:
            raw = self.exchange._create_order({
                "instId": SYMBOL, "side": "sell", "posSide": "net",
                "tdMode": "isolated", "ordType": "market", "sz": str(size), "reduceOnly": True,
            })
            self.exchange._fill(raw["ordId"])
        await self.sync.sync_rest()

    async def adjustment(self, *, attempts=0):
        await self.entry(size=4)
        await self.reduce(1)
        manager = ProtectionAdjustment(self.manager, self.market)
        position, opening, lot, proof, target = manager._candidate(SYMBOL, 100)
        record = self.store.claim_protection_adjustment({
            "adjustment_id": "review-adjustment", "opening_order_id": opening["client_order_id"],
            "lot_id": lot["lot_id"], "evidence": proof, "target_size": str(target), "now_ms": 0,
        }, position, expected_generation=self.store.execution_snapshot()[0])
        return self.store.update_protection_adjustment(
            record, status="review", last_error="adjustment_native_changed",
            attempts=attempts, retry_after_ms=9999999999999,
        )

    async def review(self, record, *, resolution="resume", expected_version=None):
        return await self.reviewer.review(
            "handoff" if "handoff_id" in record else "adjustment", record,
            expected_version=record["version"] if expected_version is None else expected_version,
            resolution=resolution, note="Operator reviewed exchange evidence",
        )

    def native(self):
        return next(iter(self.exchange.algos.values()))

    async def test_resume_preserves_intent_backoff_and_sends_no_mutation(self):
        record = await self.handoff()
        result = await self.review(record)
        self.assertEqual(result["status"], "cancel_pending")
        for field in ("evidence_json", "cancel_attempts", "cancel_after_ms", "close_sequence"):
            self.assertEqual(result[field], record[field])
        self.assertEqual(self.exchange.posts, [])
        self.assertEqual(len(self.store.list_audit()), 1)
        self.assertEqual(json.loads(self.store.list_audit()[0]["payload_json"])["reviewed_version"], record["version"])
        self.store.update_protection_handoff(result, cancel_after_ms=0)
        resumed = await self.manager.run(SYMBOL, 85, dry_run=False, market_data_fresh=True)
        self.assertTrue(resumed["accepted"], resumed)
        self.assertEqual([row["path"] for row in self.exchange.posts], ["/api/v5/trade/cancel-algos", "/api/v5/trade/order"])

    async def test_changed_original_protection_stays_in_review(self):
        record = await self.handoff()
        self.native().update(slTriggerPx="95", uTime=milliseconds())
        with self.assertRaisesRegex(ProtectionReviewError, "review_native_changed"):
            await self.review(record)
        self.assertEqual(self.store.protection_handoff(record["handoff_id"])["status"], "review")
        self.assertEqual(self.exchange.posts, [])

    async def test_partial_opening_resumes_cancellation_without_creating_native_proof(self):
        record = await self.handoff(size=4, filled=2)
        result = await self.review(record)
        self.assertEqual(result["status"], "opening_cancel_pending")
        self.assertIsNone(result["native_evidence_json"])
        self.assertEqual(self.exchange.posts, [])

    async def test_closed_archive_requires_terminal_native_even_if_parameters_changed(self):
        record = await self.handoff()
        await self.reduce(2)
        with self.assertRaisesRegex(ProtectionReviewError, "review_native_not_terminal"):
            await self.review(record, resolution="position_closed")
        self.native().update(state="canceled", slTriggerPx="95", uTime=milliseconds())
        result = await self.review(record, resolution="position_closed")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["evidence_json"], record["evidence_json"])
        self.assertEqual(self.exchange.posts, [])

    async def test_open_position_cannot_be_archived(self):
        record = await self.handoff()
        with self.assertRaisesRegex(ProtectionReviewError, "review_position_not_closed"):
            await self.review(record, resolution="position_closed")

    async def test_unknown_close_record_cannot_be_archived(self):
        record = await self.handoff()
        await self.reduce(2)
        self.native().update(state="canceled", uTime=milliseconds())
        with self.store._connection() as connection:
            connection.execute(
                "UPDATE protection_handoffs SET close_order_id = 'missing', close_sequence = 1 WHERE handoff_id = ?",
                (record["handoff_id"],),
            )
        record = self.store.protection_handoff(record["handoff_id"])
        with self.assertRaisesRegex(ProtectionReviewError, "review_close_unconfirmed"):
            await self.review(record, resolution="position_closed")

    async def test_native_children_are_checked_before_resuming(self):
        record = await self.handoff()
        self.exchange.trigger_protection(self.native()["algoId"], filled=1, child_state="partially_filled")
        with self.assertRaisesRegex(ValueError, "handoff_native_child_pending"):
            await self.review(record)
        child = self.exchange.orders[self.native()["ordIdList"][0]]
        child.update(state="canceled", uTime=milliseconds())
        result = await self.review(record)
        self.assertEqual(result["status"], "native_executing")
        self.assertIsNone(result["native_settlement_json"])
        self.assertEqual(self.exchange.posts, [])

    async def test_stale_version_is_rejected_before_querying(self):
        record = await self.handoff()
        with patch.object(self.sync, "sync_rest", new_callable=AsyncMock) as sync:
            with self.assertRaisesRegex(ProtectionReviewError, "review_record_changed"):
                await self.review(record, expected_version=record["version"] + 1)
            sync.assert_not_awaited()

    async def test_incomplete_reconciliation_does_not_release_review(self):
        record = await self.handoff()
        with patch.object(self.sync, "sync_rest", new=AsyncMock(return_value={"errors": ["positions"]})):
            with self.assertRaisesRegex(ProtectionReviewError, "snapshot_incomplete"):
                await self.review(record)
        self.assertEqual(self.exchange.posts, [])

    async def test_position_change_during_query_is_caught_at_atomic_commit(self):
        record = await self.handoff()
        query = self.account.algo_order_details

        async def changed(*args, **kwargs):
            result = await query(*args, **kwargs)
            position = self.store.get_position(record["position_key"])
            self.store.upsert_position({**position, "size": 3})
            return result

        with patch.object(self.account, "algo_order_details", side_effect=changed):
            with self.assertRaisesRegex(ExposureSnapshotChanged, "review_snapshot_changed"):
                await self.review(record)
        self.assertEqual(self.store.protection_handoff(record["handoff_id"])["status"], "review")

    async def test_order_change_during_query_invalidates_generation(self):
        record = await self.handoff()
        query = self.account.algo_order_details

        async def changed(*args, **kwargs):
            result = await query(*args, **kwargs)
            with self.store._connection() as connection:
                connection.execute("UPDATE execution_generation SET generation = generation + 1")
            return result

        with patch.object(self.account, "algo_order_details", side_effect=changed):
            with self.assertRaisesRegex(ExposureSnapshotChanged, "review_snapshot_changed"):
                await self.review(record)

    async def test_duplicate_reviews_only_commit_once(self):
        record = await self.handoff()
        results = await asyncio.gather(self.review(record), self.review(record), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1, results)
        self.assertEqual(sum(isinstance(result, ExposureSnapshotChanged) for result in results), 1, results)
        self.assertEqual(len(self.store.list_audit()), 1)
        self.assertEqual(self.exchange.posts, [])

    async def test_unattempted_adjustment_can_resume_without_modifying_target(self):
        record = await self.adjustment()
        result = await self.review(record)
        self.assertEqual(result["status"], "prepared")
        for field in ("evidence_json", "target_size", "attempts", "retry_after_ms"):
            self.assertEqual(result[field], record[field])
        self.assertEqual(self.exchange.posts, [])

    async def test_uncertain_adjustment_keeps_attempt_and_backoff(self):
        record = await self.adjustment(attempts=1)
        result = await self.review(record)
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["retry_after_ms"], record["retry_after_ms"])
        self.assertEqual(result["attempts"], 1)

    async def test_confirmed_target_is_completed_without_resending(self):
        record = await self.adjustment(attempts=1)
        self.native().update(sz="3", uTime=milliseconds())
        result = await self.review(record)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.exchange.posts, [])

    async def test_changed_lot_cannot_discard_uncertain_old_amendment(self):
        record = await self.adjustment(attempts=1)
        await self.reduce(1)
        with self.assertRaisesRegex(ProtectionReviewError, "outcome_unconfirmed"):
            await self.review(record)
        self.assertEqual(self.store.protection_adjustment(record["adjustment_id"])["status"], "review")

    async def test_obsolete_unattempted_target_is_retired_and_worker_uses_new_remainder(self):
        record = await self.adjustment()
        await self.reduce(1)
        result = await self.review(record)
        self.assertEqual(result["status"], "superseded")
        self.assertEqual(result["target_size"], record["target_size"])
        replacements = self.store.protection_adjustments(self.account.account_scope)
        self.assertEqual(len(replacements), 1)
        self.assertEqual(Decimal(replacements[0]["target_size"]), Decimal(2))
        self.assertEqual(replacements[0]["status"], "prepared")
        with self.assertRaisesRegex(ExposureSnapshotChanged, "protection_adjustment_pending"):
            self.store.claim_order({
                "client_order_id": "blocked-new-entry", "inst_id": SYMBOL, "status": "submitting",
                "side": "buy", "pos_side": "net", "ord_type": "market", "td_mode": "isolated",
                "size": 1, "source": "structured-technical", "account_scope": self.account.account_scope,
            })
        manager = ProtectionAdjustment(self.manager, self.market)
        result = await manager.run(SYMBOL, 100, dry_run=False, market_data_fresh=True)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(Decimal(self.native()["sz"]), Decimal(2))
        self.assertEqual(len(self.exchange.posts), 1)
        self.assertEqual(self.exchange.posts[0]["path"], "/api/v5/trade/amend-algos")

    async def test_replacement_and_audit_roll_back_together(self):
        record = await self.adjustment()
        await self.reduce(1)
        with self.store._connection() as connection:
            connection.execute("""
                CREATE TRIGGER reject_review_audit BEFORE INSERT ON audit_events
                WHEN NEW.event_type = 'protection_review_resolved'
                BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            await self.review(record)
        records = self.store.protection_adjustments(self.account.account_scope)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["adjustment_id"], record["adjustment_id"])
        self.assertEqual(records[0]["status"], "review")
        self.assertEqual(self.exchange.posts, [])

    async def test_review_does_not_release_emergency_stop(self):
        record = await self.handoff()
        self.engine.safety.stop("fixture emergency")
        result = await self.review(record)
        self.assertEqual(result["status"], "cancel_pending")
        self.assertTrue(self.engine.safety.emergency_stopped)
        outcome = await self.manager.run(SYMBOL, 85, dry_run=False, market_data_fresh=True)
        self.assertEqual(outcome["reasons"], ["handoff_execution_locked"])
        self.assertEqual(self.exchange.posts, [])

    async def test_account_switch_after_query_cannot_release_review(self):
        record = await self.handoff()
        query = self.account.algo_order_details

        async def switched(*args, **kwargs):
            result = await query(*args, **kwargs)
            self.account.api_key = "another-fixture-account"
            return result

        with patch.object(self.account, "algo_order_details", side_effect=switched):
            with self.assertRaisesRegex(ProtectionReviewError, "review_account_unavailable"):
                await self.review(record)
        self.assertEqual(self.store.protection_handoff(record["handoff_id"])["status"], "review")

    async def test_persisted_review_can_resume_after_process_objects_reopen(self):
        record = await self.handoff()
        self.reopen()
        self.reviewer = ProtectionReviewer(self.store, self.sync)
        result = await self.review(self.store.protection_handoff(record["handoff_id"]))
        self.assertEqual(result["status"], "cancel_pending")
        self.assertEqual(result["evidence_json"], record["evidence_json"])
        self.assertEqual(self.exchange.posts, [])

    async def test_review_endpoint_requires_admin_and_returns_versioned_summary(self):
        record = await self.handoff()
        notifier = AsyncMock()
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "review-api-fixture"}), \
                patch.object(api_main, "state_store", self.store), \
                patch.object(api_main, "account_client", self.account), \
                patch.object(api_main, "account_sync", self.sync), \
                patch.object(api_main, "execution_engine", SimpleNamespace(notify_event=notifier)):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="http://test") as client:
                path = f"/api/v1/protection/handoffs/{record['handoff_id']}/review"
                payload = {"expected_version": record["version"], "resolution": "resume", "note": "Reviewed evidence"}
                self.assertEqual((await client.post(path, json=payload)).status_code, 401)
                headers = {"X-Admin-Token": "review-api-fixture"}
                self.assertEqual((await client.post(path, json={**payload, "expected_version": True}, headers=headers)).status_code, 422)
                response = await client.post(path, json=payload, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()["trading_performed"])
                self.assertEqual(response.json()["data"][0]["version"], record["version"] + 1)
                self.assertEqual((await client.post(path, json=payload, headers=headers)).status_code, 409)
        notifier.assert_awaited_once()
        self.assertEqual(self.exchange.posts, [])


class ProtectionReviewProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_api_review_pushes_status_without_exchange_mutation_or_unlock(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        exchange = ExchangeServer()
        self.addAsyncCleanup(exchange.close)
        await exchange.start()
        api = ApiProcess(directory.name, exchange)
        api.environment["EXECUTION_ENABLED"] = "false"
        self.addAsyncCleanup(api.stop)
        await api.start()
        with patch.dict(os.environ, exchange.environment()):
            account = OkxAccountClient()
        store = StateStore(str(api.database))
        order = OrderRequest(
            inst_id=SYMBOL, side="buy", sz=2, stop_loss=90, take_profit=120, cl_ord_id="reviewprocess",
        )
        with exchange.lock:
            raw = exchange._create_order(order.okx_payload())
            exchange._fill(raw["ordId"])
        store.save_order({
            "client_order_id": raw["clOrdId"], "exchange_order_id": raw["ordId"], "status": "filled",
            "inst_id": SYMBOL, "side": "buy", "pos_side": "net", "ord_type": "market",
            "td_mode": "isolated", "size": 2, "stop_loss": 90, "take_profit": 120,
            "source": "structured-technical", "account_scope": account.account_scope, "raw": raw,
        })
        await api.request("POST", "/account/sync")
        position = store.list_positions()[0]
        allocation = store.position_lots(position)
        self.assertEqual(allocation["status"], "verified", allocation)
        proof = protection_evidence(
            store.get_order(raw["clOrdId"]), next(iter(exchange.algos.values())), native=True, position_size=2,
        )
        record = store.create_protection_handoff({
            "handoff_id": "process-review", "lot_id": allocation["lots"][0]["lot_id"],
            "evidence": proof, "reason": "stop_loss", "trigger_price": 85,
        }, position, expected_generation=store.execution_snapshot()[0])
        record = store.update_protection_handoff(record, status="review", last_error="handoff_native_changed")
        await api.request("POST", "/safety/emergency-stop", {"reason": "review acceptance fixture"})
        async with api.client.stream("GET", "/api/v1/account/events") as stream:
            lines = stream.aiter_lines()
            before = await next_event(lines, "protection_handoffs")
            self.assertEqual(before["data"][0]["version"], record["version"])
            response = await api.request("POST", "/protection/handoffs/process-review/review", {
                "expected_version": record["version"], "resolution": "resume", "note": "Reviewed fixture evidence",
            })
            self.assertFalse(response["trading_performed"])
            pushed = await next_event(lines, "protection_handoffs", lambda value: value["data"][0]["status"] == "cancel_pending")
            self.assertEqual(pushed["data"][0]["version"], record["version"] + 1)
        status = await api.request("GET", "/system/status")
        self.assertTrue(status["safety_control"]["emergency_stopped"])
        self.assertFalse(status["execution_enabled"])
        self.assertFalse(status["automation_worker"]["enabled"])
        self.assertFalse(status["live_safety"]["allowed"])
        self.assertEqual(exchange.posts, [])
        self.assertEqual(exchange.errors, [])


if __name__ == "__main__":
    unittest.main()
