import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from app.account_sync import AccountSynchronizer
from app.state_store import StateStore


SYMBOL = "BTC-USDT-SWAP"


class ProtectionBindingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.sqlite3"
        self.store = StateStore(str(self.path))
        self.account = SimpleNamespace(account_scope="account-a", configured=True)
        self.stream = SimpleNamespace(snapshot=lambda: {"positions": [], "orders": []})
        self.sync = AccountSynchronizer(self.store, self.account, self.stream)

    def entry(self, name="entry-1", trade="trade-1", *, raw=None, **changes):
        record = {
            "client_order_id": name, "exchange_order_id": f"exchange-{name}",
            "inst_id": SYMBOL, "side": "buy", "pos_side": "net", "td_mode": "isolated",
            "ord_type": "market", "status": "filled", "size": 1, "reduce_only": False,
            "stop_loss": 95, "take_profit": 110, "account_scope": "account-a",
            "source": "structured-technical", "created_at": "2026-01-01T00:00:00+00:00",
            **changes,
        }
        record["raw"] = {
            "instId": record["inst_id"], "side": record["side"], "posSide": record["pos_side"],
            "tdMode": record["td_mode"], "ordId": record["exchange_order_id"], "clOrdId": name,
            "tradeId": trade, "accFillSz": "1", **(raw or {}),
        }
        return self.store.save_order(record)

    def position(self, **changes):
        row = {
            "instId": SYMBOL, "posSide": "net", "mgnMode": "isolated", "pos": "1",
            "avgPx": "100", "markPx": "100", "tradeId": "trade-1", "uTime": "1000",
            **changes,
        }
        self.sync._save_position(row)
        return self.store.get_position(f"{row['instId']}:{row['posSide']}:{row['mgnMode']}")

    def assert_unlinked(self, row):
        self.assertIsNone(row["stop_loss"])
        self.assertIsNone(row["take_profit"])
        self.assertIsNone(row["protection_order_id"])

    def test_confirmed_trade_links_exact_account_mode_and_opening_order(self):
        self.entry()
        row = self.position()
        self.assertEqual((row["stop_loss"], row["take_profit"]), (95, 110))
        self.assertEqual(row["protection_order_id"], "entry-1")
        self.assertEqual(row["exchange_trade_id"], "trade-1")
        self.assertEqual(row["account_scope"], "account-a")
        self.assertEqual(row["td_mode"], "isolated")

    def test_partial_fill_links_only_the_confirmed_position_quantity(self):
        self.entry(status="partially_filled", raw={"accFillSz": ".5"})
        self.assertEqual(self.position(pos=".5")["protection_order_id"], "entry-1")
        self.assert_unlinked(self.position(pos=".6"))

    def test_wrong_account_or_missing_account_evidence_never_borrows_levels(self):
        for index, scope in enumerate(("account-b", None)):
            trade = f"trade-{index}"
            self.entry(f"entry-{index}", trade, account_scope=scope)
            self.assert_unlinked(self.position(tradeId=trade))

    def test_hedged_and_net_modes_and_margin_modes_are_not_interchangeable(self):
        for index, change in enumerate((
            {"pos_side": "long"}, {"td_mode": "cross"}, {"side": "sell"},
        )):
            trade = f"trade-{index}"
            self.entry(f"entry-{index}", trade, **change)
            self.assert_unlinked(self.position(tradeId=trade))

    def test_net_short_and_hedged_short_link_their_own_opening_fills(self):
        for index, side in enumerate(("net", "short")):
            trade = f"short-{index}"
            self.entry(f"short-entry-{index}", trade, pos_side=side, side="sell", stop_loss=105, take_profit=90)
            row = self.position(posSide=side, pos="-1" if side == "net" else "1", tradeId=trade)
            self.assertEqual((row["stop_loss"], row["take_profit"]), (105, 90))

    def test_unrelated_latest_trade_clears_old_levels_and_warns_only_once(self):
        self.entry()
        self.position()
        self.assert_unlinked(self.position(tradeId="manual-trade"))
        self.assert_unlinked(self.position(tradeId="manual-trade"))
        events = [row for row in self.store.list_audit() if row["event_type"] == "position_protection_unverified"]
        self.assertEqual(len(events), 1)

    def test_new_verified_entry_without_intermediate_zero_rotates_protection_identity(self):
        self.entry()
        first = self.position()
        self.entry("entry-2", "trade-2", stop_loss=90, take_profit=120)
        second = self.position(tradeId="trade-2")
        self.assertEqual(second["protection_order_id"], "entry-2")
        self.assertEqual(second["lifecycle_generation"], first["lifecycle_generation"] + 1)
        self.assertEqual(self.position(tradeId="trade-2")["lifecycle_generation"], second["lifecycle_generation"])

    def test_funding_and_mark_updates_do_not_rotate_a_verified_link(self):
        self.entry()
        first = self.position()
        for stamp in range(1001, 1005):
            row = self.position(markPx="101", uTime=str(stamp))
            self.assertEqual(row["lifecycle_generation"], first["lifecycle_generation"])
            self.assertEqual(row["protection_order_id"], "entry-1")

    def test_missing_or_malformed_trade_identity_does_not_pick_a_historical_order(self):
        self.entry()
        for value in (None, "", True, [], {}, "unsafe\nidentifier"):
            with self.subTest(value=value):
                self.assert_unlinked(self.position(tradeId=value))

    def test_unconfirmed_reducing_and_algo_orders_are_not_opening_evidence(self):
        for index, changes in enumerate((
            {"status": "submitted"}, {"status": "preview"}, {"reduce_only": True},
            {"order_kind": "algo"}, {"status": "submission_unknown"},
        )):
            trade = f"trade-{index}"
            self.entry(f"entry-{index}", trade, **changes)
            self.assert_unlinked(self.position(tradeId=trade))

    def test_invalid_filled_quantity_and_exchange_identity_fail_closed(self):
        for index, raw in enumerate((
            {"accFillSz": None}, {"accFillSz": "NaN"}, {"accFillSz": "Infinity"},
            {"accFillSz": "0"}, {"accFillSz": "2"}, {"accFillSz": True},
            {"ordId": "another-order"}, {"clOrdId": "another-client"},
            {"instId": "ETH-USDT-SWAP"}, {"tdMode": "cross"},
        )):
            trade = f"trade-{index}"
            self.entry(f"entry-{index}", trade, raw=raw)
            self.assert_unlinked(self.position(tradeId=trade))

    def test_duplicate_trade_evidence_is_ambiguous_not_latest_wins(self):
        self.entry()
        self.entry("duplicate-client", "trade-1")
        self.assert_unlinked(self.position())

    def test_binding_is_not_limited_to_the_latest_500_orders(self):
        self.entry()
        for index in range(501):
            self.entry(f"later-{index}", f"later-trade-{index}", created_at="2026-01-02T00:00:00+00:00")
        self.assertNotIn("entry-1", [row["client_order_id"] for row in self.store.list_orders(500)])
        self.assertEqual(self.position()["protection_order_id"], "entry-1")

    def test_link_evidence_survives_restart_and_cached_refresh(self):
        self.entry()
        row = self.position()
        self.store = StateStore(str(self.path))
        self.sync = AccountSynchronizer(self.store, self.account, self.stream)
        self.sync._refresh_position_protection(row["position_key"])
        refreshed = self.store.get_position(row["position_key"])
        self.assertEqual(refreshed["protection_order_id"], "entry-1")
        self.assertEqual(refreshed["lifecycle_generation"], row["lifecycle_generation"])
        self.account.account_scope = "account-b"
        self.sync._refresh_position_protection(row["position_key"])
        self.assert_unlinked(self.store.get_position(row["position_key"]))

    def test_legacy_unproven_levels_are_not_implicitly_trusted_after_migration(self):
        self.entry()
        row = self.position()
        with closing(sqlite3.connect(self.path)) as connection, connection:
            for column in ("account_scope", "td_mode", "exchange_trade_id", "protection_order_id"):
                connection.execute(f"ALTER TABLE positions DROP COLUMN {column}")
        self.store = StateStore(str(self.path))
        self.sync = AccountSynchronizer(self.store, self.account, self.stream)
        self.sync._refresh_position_protection(row["position_key"])
        self.assert_unlinked(self.store.get_position(row["position_key"]))
        self.assertEqual(self.position()["protection_order_id"], "entry-1")


if __name__ == "__main__":
    unittest.main()
