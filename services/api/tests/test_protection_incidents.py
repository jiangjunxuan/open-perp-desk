import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from pathlib import Path
from types import SimpleNamespace

from app.account_sync import AccountSynchronizer
from app import main as api_main
from fastapi import HTTPException
from app.state_store import ExposureSnapshotChanged, StateStore
from app.position_protection import attached_algo_client_id


class IncidentAccountClient:
    account_scope = "incident-fixture"


class ProtectionIncidentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.sync = AccountSynchronizer(
            self.store,
            IncidentAccountClient(),
            SimpleNamespace(snapshot=lambda: {}),
        )

    def opening_order(self):
        return {
            "client_order_id": "entry-incident-1",
            "exchange_order_id": "exchange-incident-1",
            "status": "filled",
            "inst_id": "BTC-USDT-SWAP",
            "side": "buy",
            "pos_side": "net",
            "ord_type": "market",
            "td_mode": "cross",
            "size": 1,
            "stop_loss": 49000,
            "take_profit": 52000,
            "source": "structured-technical",
            "account_scope": "incident-fixture",
            "created_at": "2026-01-01T00:00:00+00:00",
        }

    def failed_exchange_order(self):
        return {
            "ordId": "exchange-incident-1",
            "clOrdId": "entry-incident-1",
            "state": "filled",
            "instId": "BTC-USDT-SWAP",
            "side": "buy",
            "posSide": "net",
            "ordType": "market",
            "tdMode": "cross",
            "sz": "1",
            "cTime": "1767225600000",
            "uTime": "1767225600000",
            "attachAlgoOrds": [{
                "attachAlgoClOrdId": attached_algo_client_id("entry-incident-1"),
                "failCode": "51000",
                "failReason": "attached protection rejected",
            }],
        }

    def test_explicit_exchange_failure_is_persisted_and_deduplicated(self):
        self.store.save_order(self.opening_order())
        self.sync._save_regular_order(self.failed_exchange_order(), source="okx-order-history")
        self.sync._save_regular_order(self.failed_exchange_order(), source="okx-order-history")

        incidents = self.store.protection_incidents("incident-fixture")
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0]["failure_code"], "attached_protection_51000")
        self.assertEqual(incidents[0]["version"], 0)
        self.assertEqual(len(self.store.list_audit()), 1)
        self.assertTrue(self.store.has_active_protection_incident(
            "incident-fixture", "BTC-USDT-SWAP",
        ))

    def test_active_incident_blocks_new_opening_claim(self):
        self.store.record_protection_incident({
            "account_scope": "incident-fixture",
            "inst_id": "BTC-USDT-SWAP",
            "opening_order_id": "entry-incident-1",
            "failure_code": "attached_protection_missing",
        })
        with self.assertRaises(ExposureSnapshotChanged) as context:
            self.store.claim_order({
                "client_order_id": "new-entry",
                "status": "submitting",
                "inst_id": "BTC-USDT-SWAP",
                "side": "buy",
                "pos_side": "net",
                "ord_type": "market",
                "td_mode": "cross",
                "size": 1,
                "source": "structured-technical",
                "account_scope": "incident-fixture",
            })
        self.assertEqual(str(context.exception), "protection_incident_review_required")

    def test_matching_native_protection_supersedes_active_incident(self):
        self.store.save_order(self.opening_order())
        incident = self.store.record_protection_incident({
            "account_scope": "incident-fixture",
            "inst_id": "BTC-USDT-SWAP",
            "opening_order_id": "entry-incident-1",
            "failure_code": "attached_protection_missing",
        })
        self.sync._save_algo_order({
            "algoId": "native-incident-1",
            "algoClOrdId": attached_algo_client_id("entry-incident-1"),
            "instId": "BTC-USDT-SWAP",
            "side": "sell",
            "posSide": "net",
            "tdMode": "cross",
            "state": "live",
            "ordType": "oco",
            "sz": "1",
            "slTriggerPx": "49000",
            "tpTriggerPx": "52000",
            "cTime": "1767225600000",
            "uTime": "1767225600000",
        })

        current = self.store.protection_incident(incident["incident_id"])
        self.assertEqual(current["status"], "superseded")
        self.assertEqual(self.store.has_active_protection_incident(
            "incident-fixture", "BTC-USDT-SWAP",
        ), False)

    def test_resolution_is_compare_and_swap_and_requires_note(self):
        incident = self.store.record_protection_incident({
            "account_scope": "incident-fixture",
            "inst_id": "BTC-USDT-SWAP",
            "opening_order_id": "entry-incident-1",
            "failure_code": "attached_protection_missing",
        })
        with self.assertRaises(ValueError):
            self.store.resolve_protection_incident(
                incident, resolution="external_protection_verified", note=" ",
            )
        resolved = self.store.resolve_protection_incident(
            incident,
            resolution="external_protection_verified",
            note="已在 OKX 订单详情核实原生保护单。",
        )
        self.assertEqual(resolved["status"], "resolved")
        self.assertIsNone(self.store.resolve_protection_incident(
            incident,
            resolution="external_protection_verified",
            note="重复提交",
        ))
        self.assertEqual(len(self.store.list_audit()), 1)

    def test_historical_failure_replay_does_not_reopen_resolved_incident(self):
        self.store.save_order(self.opening_order())
        self.sync._save_regular_order(self.failed_exchange_order(), source="okx-order-history")
        incident = self.store.protection_incidents("incident-fixture")[0]
        self.store.resolve_protection_incident(
            incident, resolution="external_protection_verified", note="Reviewed external protection",
        )
        self.sync._save_regular_order(self.failed_exchange_order(), source="okx-order-history")
        self.assertEqual(self.store.protection_incidents("incident-fixture"), [])
        self.assertEqual(len(self.store.protection_incidents("incident-fixture", include_resolved=True)), 1)
        self.assertEqual(len(self.store.list_audit()), 2)
        changed = self.failed_exchange_order()
        changed["attachAlgoOrds"][0]["failCode"] = "51001"
        self.sync._save_regular_order(changed, source="okx-order-history")
        active = self.store.protection_incidents("incident-fixture")
        self.assertEqual(len(active), 1)
        self.assertNotEqual(active[0]["incident_id"], incident["incident_id"])
        self.assertEqual(active[0]["failure_code"], "attached_protection_51001")

    def test_changed_failure_invalidates_existing_review_version(self):
        self.store.save_order(self.opening_order())
        self.sync._save_regular_order(self.failed_exchange_order(), source="okx-order-history")
        old = self.store.protection_incidents("incident-fixture")[0]
        changed = self.failed_exchange_order()
        changed["attachAlgoOrds"][0]["failReason"] = "Additional exchange evidence"
        self.sync._save_regular_order(changed, source="okx-order-history")
        current = self.store.protection_incidents("incident-fixture")[0]
        self.assertEqual(current["version"], old["version"] + 1)
        self.assertEqual(current["status"], "review")
        self.assertIsNone(self.store.resolve_protection_incident(
            old, resolution="external_protection_verified", note="Obsolete review",
        ))

    def test_late_order_snapshot_cannot_replace_incident_evidence(self):
        self.store.save_order(self.opening_order())
        fresh = self.failed_exchange_order()
        fresh["uTime"] = "1767225601000"
        fresh["attachAlgoOrds"][0]["failReason"] = "Current evidence"
        self.sync._save_regular_order(fresh, source="okx-order-history")
        incident = self.store.protection_incidents("incident-fixture")[0]
        self.sync._save_regular_order(self.failed_exchange_order(), source="okx-order-history")
        current = self.store.protection_incidents("incident-fixture")[0]
        self.assertEqual(current["failure_detail"], "Current evidence")
        self.assertEqual(current["version"], incident["version"])
        self.assertEqual(len(self.store.list_audit()), 1)

    def test_closed_resolution_checks_current_scope_and_position_in_store(self):
        incident = self.store.record_protection_incident({
            "account_scope": "incident-fixture", "inst_id": "BTC-USDT-SWAP",
            "position_key": "BTC-USDT-SWAP:net:cross", "opening_order_id": "entry-incident-1",
            "failure_code": "attached_protection_missing",
        })
        for scope, status, size in (
            ("different-account", "closed", 0),
            ("incident-fixture", "open", 1),
            ("incident-fixture", "open", 0),
            ("incident-fixture", "closed", 1),
        ):
            with self.subTest(scope=scope, status=status, size=size):
                self.store.upsert_position({
                    "position_key": incident["position_key"], "inst_id": incident["inst_id"],
                    "pos_side": "net", "td_mode": "cross", "account_scope": scope,
                    "size": size, "entry_price": 50000, "status": status,
                })
                with self.assertRaisesRegex(ExposureSnapshotChanged, "position_not_closed"):
                    self.store.resolve_protection_incident(incident, resolution="position_closed", note="Close review")
                self.assertEqual(self.store.protection_incident(incident["incident_id"])["status"], "open")
        self.assertEqual(self.store.list_audit(), [])

    def test_closed_position_with_uncertain_order_cannot_resolve(self):
        self.store.save_order({**self.opening_order(), "status": "submission_unknown"})
        incident = self.store.record_protection_incident({
            "account_scope": "incident-fixture", "inst_id": "BTC-USDT-SWAP",
            "position_key": "BTC-USDT-SWAP:net:cross", "opening_order_id": "entry-incident-1",
            "failure_code": "attached_protection_missing",
        })
        self.store.upsert_position({
            "position_key": incident["position_key"], "inst_id": incident["inst_id"],
            "pos_side": "net", "td_mode": "cross", "account_scope": "incident-fixture",
            "size": 0, "entry_price": 50000, "status": "closed",
        })
        with self.assertRaisesRegex(ExposureSnapshotChanged, "orders_pending"):
            self.store.resolve_protection_incident(incident, resolution="position_closed", note="Close review")

    def test_resolution_audit_failure_rolls_back_release(self):
        incident = self.store.record_protection_incident({
            "account_scope": "incident-fixture", "inst_id": "BTC-USDT-SWAP",
            "opening_order_id": "entry-incident-1", "failure_code": "attached_protection_missing",
        })
        with self.store._connection() as connection:
            connection.execute("""
                CREATE TRIGGER reject_resolution_audit BEFORE INSERT ON audit_events
                BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END
            """)
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.resolve_protection_incident(
                incident, resolution="external_protection_verified", note="External review",
            )
        self.assertEqual(self.store.protection_incident(incident["incident_id"])["status"], "open")

    def test_position_closed_resolution_requires_durable_closed_position(self):
        incident = self.store.record_protection_incident({
            "account_scope": "incident-fixture",
            "inst_id": "BTC-USDT-SWAP",
            "position_key": "BTC-USDT-SWAP:net:cross",
            "opening_order_id": "entry-incident-1",
            "failure_code": "attached_protection_missing",
        })
        request = api_main.ProtectionIncidentResolutionRequest(
            resolution="position_closed",
            note="仓位已归零，依据本地对账记录。",
            expected_version=incident["version"],
        )
        notifier = AsyncMock()
        with patch.object(api_main, "state_store", self.store), \
                patch.object(api_main, "account_client", SimpleNamespace(account_scope="incident-fixture", configured=True)), \
                patch.object(api_main, "account_sync", SimpleNamespace(sync_rest=AsyncMock(return_value={}))), \
                patch.object(api_main, "execution_engine", SimpleNamespace(notify_event=notifier)):
            with self.assertRaises(HTTPException) as context:
                import asyncio
                asyncio.run(api_main.resolve_protection_incident(
                    request, incident["incident_id"], None,
                ))
        self.assertEqual(context.exception.status_code, 409)
        self.store.upsert_position({
            "position_key": "BTC-USDT-SWAP:net:cross",
            "inst_id": "BTC-USDT-SWAP",
            "pos_side": "net",
            "td_mode": "cross",
            "account_scope": "incident-fixture",
            "size": 0,
            "entry_price": 50000,
            "status": "closed",
        })
        with patch.object(api_main, "state_store", self.store), \
                patch.object(api_main, "account_client", SimpleNamespace(account_scope="incident-fixture", configured=True)), \
                patch.object(api_main, "account_sync", SimpleNamespace(sync_rest=AsyncMock(return_value={}))), \
                patch.object(api_main, "execution_engine", SimpleNamespace(notify_event=notifier)):
            result = __import__("asyncio").run(api_main.resolve_protection_incident(
                request, incident["incident_id"], None,
            ))
        self.assertTrue(result["accepted"])
        self.assertEqual(self.store.protection_incident(incident["incident_id"])["status"], "resolved")
        notifier.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
