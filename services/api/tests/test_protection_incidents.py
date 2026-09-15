import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.account_sync import AccountSynchronizer
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


if __name__ == "__main__":
    unittest.main()
