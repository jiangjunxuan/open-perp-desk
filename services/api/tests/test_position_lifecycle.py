import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from app.state_store import ACTIVE_ORDER_STATUSES, StateStore


SYMBOL = "BTC-USDT-SWAP"


def position(**overrides):
    return {
        "position_key": f"{SYMBOL}:net:isolated", "inst_id": SYMBOL, "pos_side": "net",
        "size": 1, "entry_price": 100, "stop_loss": 95, "take_profit": 110, **overrides,
    }


def order(**overrides):
    return {
        "client_order_id": "fixture-close", "status": "submission_unknown", "inst_id": SYMBOL,
        "side": "sell", "pos_side": "net", "td_mode": "isolated", "ord_type": "market",
        "size": 1, "reduce_only": True, "order_kind": "standard", **overrides,
    }


class PositionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = StateStore(str(self.path))

    def test_first_lifecycle_preserves_legacy_identity_and_updates_do_not_rotate_it(self):
        for change in ({}, {"size": 2}, {"size": .5}, {"mark_price": 104}, {"stop_loss": 96}):
            saved = self.store.upsert_position(position(**change))
            self.assertEqual(saved["lifecycle_generation"], 0)
        saved = self.store.upsert_position(position(lifecycle_generation=500))
        self.assertEqual(saved["lifecycle_generation"], 0, "caller cannot choose the durable generation")

    def test_observed_close_and_reopen_rotate_once_and_survive_restart(self):
        self.store.upsert_position(position())
        self.store.upsert_position(position(size=0, status="closed"))
        self.store.upsert_position(position(size=0, status="closed"))
        reopened = self.store.upsert_position(position())
        self.assertEqual(reopened["lifecycle_generation"], 1)
        self.store = StateStore(str(self.path))
        self.assertEqual(self.store.upsert_position(position())["lifecycle_generation"], 1)
        self.store.upsert_position(position(size=0, status="closed"))
        self.assertEqual(self.store.upsert_position(position())["lifecycle_generation"], 2)

    def test_missing_rest_position_and_zero_size_both_end_the_observed_lifecycle(self):
        self.store.upsert_position(position())
        self.store.close_positions_not_seen(set())
        self.assertEqual(self.store.upsert_position(position())["lifecycle_generation"], 1)
        self.store.upsert_position(position(size=0))
        self.assertEqual(self.store.upsert_position(position())["lifecycle_generation"], 2)

    def test_net_direction_reversal_rotates_without_needing_an_intermediate_zero(self):
        self.store.upsert_position(position())
        self.assertEqual(self.store.upsert_position(position(size=-2))["lifecycle_generation"], 1)
        self.assertEqual(self.store.upsert_position(position(size=-1))["lifecycle_generation"], 1)
        self.assertEqual(self.store.upsert_position(position(size=1))["lifecycle_generation"], 2)

    def test_competing_reopen_snapshots_allocate_only_one_generation(self):
        self.store.upsert_position(position(size=0, status="closed"))
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: self.store.upsert_position(position()), range(8)))
        self.assertEqual({row["lifecycle_generation"] for row in rows}, {1})

    def test_legacy_database_migration_does_not_rotate_open_positions_or_orders(self):
        self.store.upsert_position(position())
        self.store.save_order(order())
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("ALTER TABLE positions DROP COLUMN lifecycle_generation")
        migrated = StateStore(str(self.path))
        self.assertEqual(migrated.get_position(position()["position_key"])["lifecycle_generation"], 0)
        self.assertEqual(migrated.get_order("fixture-close")["status"], "submission_unknown")
        self.assertEqual(migrated.upsert_position(position())["lifecycle_generation"], 0)

    def test_pending_standard_close_guard_includes_unknown_and_canceling(self):
        for status in ACTIVE_ORDER_STATUSES:
            with self.subTest(status=status):
                self.store.save_order(order(status=status))
                self.assertTrue(self.store.has_active_standard_close(SYMBOL, "sell"))
                self.assertFalse(self.store.has_active_standard_close(SYMBOL, "buy"))
                self.assertFalse(self.store.has_active_standard_close("ETH-USDT-SWAP", "sell"))
        self.store.save_order(order(status="filled"))
        self.assertFalse(self.store.has_active_standard_close(SYMBOL, "sell"))

    def test_native_protection_open_orders_and_previews_do_not_count_as_pending_standard_closes(self):
        for index, change in enumerate((
            {"order_kind": "algo"}, {"reduce_only": False}, {"status": "preview"},
            {"status": "canceled"}, {"status": "rejected"},
        )):
            self.store.save_order(order(client_order_id=f"other-{index}", **change))
        self.assertFalse(self.store.has_active_standard_close(SYMBOL, "sell"))


if __name__ == "__main__":
    unittest.main()
