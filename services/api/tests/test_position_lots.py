import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from app.account_sync import AccountSynchronizer
from app.okx_account import OkxAccountClient, OkxAccountError
from app.position_lots import PositionLotReconciler, positions_with_lots
from app.position_protection import attached_algo_client_id
from app.realtime import private_events
from app.state_store import StateStore


SYMBOL = "BTC-USDT-SWAP"


class FillAccount:
    configured = True
    account_scope = "lots-account"

    def __init__(self):
        self.rows = []
        self.orders = {}
        self.pages = 0
        self.lookups = []
        self.before_page = None
        self.failure_after = None

    async def position_fill_pages(self, inst_id):
        rows = list(reversed(self.rows))
        for offset in range(0, len(rows), 2):
            self.pages += 1
            if self.before_page:
                await self.before_page()
            if self.failure_after is not None and offset >= self.failure_after:
                raise TimeoutError("private-transport-secret")
            yield rows[offset:offset + 2]

    async def order_details(self, inst_id, *, ord_id):
        self.lookups.append(ord_id)
        return self.orders[ord_id]


class PositionLotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = str(Path(self.directory.name) / "lots.sqlite3")
        self.store = StateStore(self.path)
        self.account = FillAccount()
        self.sync = AccountSynchronizer(self.store, self.account, SimpleNamespace(snapshot=lambda: {}))
        self.reconciler = PositionLotReconciler(self.store, self.account, self.sync._save_regular_order)

    def fill(
        self, owner, quantity, *, side="buy", requested=None, price="100", mode="isolated",
        pos_side="net", source="structured-technical", reduce=False, context=None, save=True, **changes,
    ):
        quantity = Decimal(str(quantity))
        exchange_id = str(100 + int(owner.removeprefix("client")))
        previous = self.account.orders.get(exchange_id)
        cumulative = quantity + (Decimal(previous["accFillSz"]) if previous else 0)
        requested = str(requested or cumulative)
        index = len(self.account.rows) + 1
        subtype = ("5" if side == "sell" else "6") if reduce else ("3" if side == "buy" else "4")
        raw = {
            "instId": SYMBOL, "instType": "SWAP", "clOrdId": owner, "ordId": exchange_id,
            "side": side, "posSide": pos_side, "tdMode": mode, "sz": requested,
            "state": "filled" if Decimal(requested) == cumulative else "partially_filled",
            "accFillSz": str(cumulative), "tradeId": str(2000 + index), "reduceOnly": reduce,
            "cTime": "1767225600000", "uTime": str(1767225600000 + index),
        }
        self.account.orders[exchange_id] = raw
        row = {
            **raw, "billId": str(3000 + index), "fillTime": raw["uTime"], "ts": raw["uTime"],
            "fillPx": price, "fillSz": str(quantity), "subType": subtype, **changes,
        }
        self.account.rows.append(row)
        if save:
            self.store.save_order({
                "client_order_id": owner, "exchange_order_id": exchange_id, "inst_id": SYMBOL,
                "side": side, "pos_side": pos_side, "td_mode": mode, "ord_type": "market",
                "size": float(requested), "status": raw["state"], "source": source,
                "account_scope": self.account.account_scope, "reduce_only": reduce,
                "raw": {**raw, "ordType": "market"}, "protection_context": context,
            })
            self.account.orders[exchange_id]["ordType"] = "market"
        else:
            self.account.orders[exchange_id]["ordType"] = "market"
        return row

    def position(self, size, *, side="net", mode="isolated", trade=None, scope=None):
        return self.store.upsert_position({
            "position_key": f"{SYMBOL}:{side}:{mode}", "inst_id": SYMBOL, "pos_side": side,
            "td_mode": mode, "account_scope": scope or self.account.account_scope, "size": size,
            "exchange_trade_id": trade or self.account.rows[-1]["tradeId"], "entry_price": 100,
        })

    async def reconcile(self, size, **kwargs):
        return await self.reconciler.reconcile(self.position(size, **kwargs))

    def assert_lots(self, result, sizes):
        self.assertEqual(result["status"], "verified", result)
        self.assertEqual(
            {lot["opening_order_id"]: Decimal(lot["remaining_size"]) for lot in result["lots"]},
            {key: Decimal(str(value)) for key, value in sizes.items()},
        )
        self.assertFalse(result["execution_ready"])

    async def test_multiple_entries_reconstruct_from_fresh_flat_boundary_across_pages(self):
        self.fill("client1", 1)
        self.fill("client2", 2)
        self.fill("client3", 3)
        result = await self.reconcile(6)
        self.assert_lots(result, {"client1": 1, "client2": 2, "client3": 3})
        self.assertEqual(result["fill_count"], 3)
        self.assertEqual(self.account.pages, 2)

    async def test_partial_fills_of_one_order_share_a_lot_with_weighted_entry_price(self):
        self.fill("client1", ".4", requested="1", price="100")
        self.fill("client1", ".6", requested="1", price="110")
        result = await self.reconcile(1)
        self.assert_lots(result, {"client1": 1})
        self.assertEqual(Decimal(result["lots"][0]["entry_price"]), Decimal("106"))

    async def test_external_manual_reduction_uses_explicit_fifo_policy(self):
        self.fill("client1", 1)
        self.fill("client2", 2)
        self.fill("client3", ".5", side="sell", reduce=True, source="okx-rest-orders")
        result = await self.reconcile(2.5)
        self.assert_lots(result, {"client1": ".5", "client2": 2})
        self.assertEqual(result["policy"], "fifo_with_protective_links")
        self.assertEqual(result["fifo_closes"], 1)

    async def test_local_protective_close_keeps_owner_through_exchange_raw_replacement(self):
        self.fill("client1", 1)
        self.fill("client2", 2)
        close = self.fill("client3", ".5", side="sell", reduce=True, source="protective-stop_loss", context={
            "opening_order_id": "client2", "opening_exchange_id": "102",
        })
        self.sync._save_regular_order(self.account.orders[close["ordId"]], source="okx-rest-orders")
        result = await self.reconcile(2.5)
        self.assert_lots(result, {"client1": 1, "client2": "1.5"})
        self.assertEqual(result["attributed_closes"], 1)

    async def test_legacy_protective_close_without_owner_is_not_treated_as_manual_fifo(self):
        self.fill("client1", 1)
        self.fill("client2", 2)
        self.fill("client3", ".5", side="sell", reduce=True, source="protective-stop_loss")
        result = await self.reconcile(2.5)
        self.assertEqual(result["reason"], "lot_close_context_unverified")
        self.assertEqual(result["lots"], [])

    async def test_old_verified_policy_snapshot_is_rebuilt_after_upgrade(self):
        self.fill("client1", 1)
        target = self.position(1)
        result = await self.reconciler.reconcile(target)
        old = {key: value for key, value in result.items() if key != "policy_version"}
        self.assertTrue(self.store.save_position_lots(
            target, old, expected_generation=self.store.execution_snapshot()[0],
        ))
        self.assertIsNone(self.store.position_lots(target))
        pages = self.account.pages
        rebuilt = await self.reconciler.reconcile(target)
        self.assertGreater(self.account.pages, pages)
        self.assert_lots(rebuilt, {"client1": 1})

    def native(self, owner, *, child=None, size="1", state="live"):
        self.sync._save_algo_order({
            "algoId": f"9{owner.removeprefix('client')}", "algoClOrdId": attached_algo_client_id(owner),
            "instId": SYMBOL, "side": "sell", "posSide": "net", "tdMode": "isolated",
            "ordType": "oco", "state": state, "sz": size, "reduceOnly": True,
            "slTriggerPx": "95", "slTriggerPxType": "mark", "slOrdPx": "-1",
            "tpTriggerPx": "110", "tpTriggerPxType": "mark", "tpOrdPx": "-1",
            "ordIdList": [child] if child else [], "uTime": "1767225600100",
        })

    async def test_native_child_order_closes_its_parent_entry_not_fifo(self):
        self.fill("client1", 1)
        self.fill("client2", 2)
        close = self.fill("client3", ".5", side="sell", reduce=True, source="okx-rest-orders")
        self.native("client2", child=close["ordId"], size="2", state="effective")
        result = await self.reconcile(2.5)
        self.assert_lots(result, {"client1": 1, "client2": "1.5"})
        self.assertEqual(result["attributed_closes"], 1)

    async def test_explicit_close_cannot_spill_into_another_entry(self):
        self.fill("client1", 2)
        self.fill("client2", 1)
        self.fill("client3", "1.5", side="sell", reduce=True, source="protective-stop_loss", context={
            "opening_order_id": "client2", "opening_exchange_id": "102",
        })
        result = await self.reconcile(1.5)
        self.assertEqual(result["reason"], "lot_close_quantity_exceeds_owner")
        self.assertEqual(result["lots"], [])

    async def test_short_hedged_and_net_short_entries_use_positive_lot_quantities(self):
        for index, side in enumerate(("short", "net")):
            self.account.rows = []
            self.account.orders = {}
            first, second = f"client{index * 10 + 1}", f"client{index * 10 + 2}"
            self.fill(first, 1, side="sell", pos_side=side)
            self.fill(second, 2, side="sell", pos_side=side)
            result = await self.reconcile(-3 if side == "net" else 3, side=side)
            self.assert_lots(result, {first: 1, second: 2})

    async def test_other_margin_modes_are_excluded_using_verified_order_details(self):
        self.fill("client1", 1)
        self.fill("client2", 5, mode="cross")
        self.fill("client3", 2)
        result = await self.reconcile(3)
        self.assert_lots(result, {"client1": 1, "client3": 2})

    async def test_missing_order_details_are_recovered_read_only(self):
        self.fill("client1", 1, save=False)
        self.fill("client2", 2)
        result = await self.reconcile(3)
        self.assert_lots(result, {"client1": 1, "client2": 2})
        self.assertFalse(result["lots"][0]["managed"])
        self.assertEqual(self.account.lookups, ["101"])

    async def test_missing_flat_boundary_and_missing_last_trade_are_not_guessed(self):
        self.fill("client1", 1)
        self.assertEqual((await self.reconcile(2))["reason"], "lot_flat_boundary_missing")
        self.assertEqual((await self.reconcile(1, trade="unknown"))["reason"], "lot_latest_trade_missing")

    async def test_missing_legacy_identity_never_writes_null_snapshot_or_queries_exchange(self):
        self.fill("client1", 1)
        target = self.position(1)
        for missing in ({"account_scope": None}, {"exchange_trade_id": None}, {"exchange_trade_id": ""}):
            current = self.store.upsert_position({**target, **missing})
            result = await self.reconciler.reconcile(current)
            self.assertEqual(result["reason"], "lot_position_identity_unverified")
            self.assertEqual(result["lots"], [])
            display = positions_with_lots(self.store)[0]["lot_allocation"]
            self.assertEqual(display["reason"], "lot_position_identity_unverified")
            self.assertFalse(self.store.save_position_lots(
                current, result, expected_generation=self.store.execution_snapshot()[0],
            ))
        self.assertEqual(self.account.pages, 0)
        self.assertEqual(self.account.lookups, [])

    async def test_history_before_current_flat_boundary_does_not_leak_into_reopening(self):
        self.fill("client1", 1)
        first = await self.reconcile(1)
        self.fill("client2", 1, side="sell", reduce=True)
        self.fill("client3", 2)
        result = await self.reconcile(2)
        self.assert_lots(result, {"client3": 2})
        self.assertEqual(result["fill_count"], 1)
        self.assertNotEqual(first["lots"][0]["lot_id"], result["lots"][0]["lot_id"])

    async def test_net_reversal_within_one_fill_requires_explicit_review(self):
        self.fill("client1", 3)
        result = await self.reconcile(1)
        self.assertEqual(result["reason"], "lot_reversal_requires_review")

    async def test_duplicate_trade_and_incomplete_pagination_fail_without_partial_lots(self):
        self.fill("client1", 1)
        first = self.fill("client2", 1)
        self.fill("client3", 1, tradeId=first["tradeId"])
        self.assertEqual((await self.reconcile(3))["reason"], "lot_trade_identity_ambiguous")
        self.account.rows[-1]["tradeId"] = "2003"
        self.account.failure_after = 2
        result = await self.reconcile(3)
        self.assertEqual(result["reason"], "lot_history_unavailable")
        self.assertNotIn("private-transport-secret", str(self.store.list_audit()))

    async def test_position_change_during_history_does_not_publish_stale_allocation(self):
        self.fill("client1", 1)
        target = self.position(1)

        async def change():
            self.store.upsert_position({**target, "exchange_trade_id": "newtrade", "size": 2})
        self.account.before_page = change
        result = await self.reconciler.reconcile(target)
        self.assertEqual(result["reason"], "lot_snapshot_changed")
        self.assertIsNone(self.store.position_lots(self.store.get_position(target["position_key"])))

    async def test_order_change_between_history_pages_does_not_verify_old_order_evidence(self):
        self.fill("client1", 1)
        self.fill("client2", 1)
        self.fill("client3", 1)
        target = self.position(3)

        async def change():
            if self.account.pages == 2:
                raw = {**self.account.orders["103"], "accFillSz": ".5", "uTime": "1767225600200"}
                self.sync._save_regular_order(raw, source="okx-rest-orders")
        self.account.before_page = change
        result = await self.reconciler.reconcile(target)
        self.assertEqual(result["reason"], "lot_order_snapshot_changed")
        self.assertEqual(result["lots"], [])

    async def test_generation_change_during_allocation_fails_snapshot_compare_and_swap(self):
        self.fill("client1", 1)
        allocate = self.reconciler._allocate

        def change(position, frames):
            result = allocate(position, frames)
            self.native("client1")
            return result
        with patch.object(self.reconciler, "_allocate", side_effect=change):
            result = await self.reconcile(1)
        self.assertEqual(result["reason"], "lot_snapshot_changed")
        self.assertEqual(result["lots"], [])

    async def test_account_change_during_history_is_detected(self):
        self.fill("client1", 1)
        target = self.position(1)

        async def change():
            self.account.account_scope = "another-account"
        self.account.before_page = change
        result = await self.reconciler.reconcile(target)
        self.assertEqual(result["reason"], "lot_account_changed")

    async def test_snapshot_survives_restart_and_unchanged_position_avoids_history_calls(self):
        self.fill("client1", 1)
        self.fill("client2", 2)
        target = self.position(3)
        first = await self.reconciler.reconcile(target)
        pages = self.account.pages
        reopened = StateStore(self.path)
        reconciler = PositionLotReconciler(reopened, self.account, self.sync._save_regular_order)
        self.assertEqual(await reconciler.reconcile(target), first)
        self.assertEqual(self.account.pages, pages)
        self.assertIsNone(reopened.position_lots({**target, "account_scope": "other-account"}))

    async def test_cached_allocation_is_not_reused_after_account_configuration_changes(self):
        self.fill("client1", 1)
        target = self.position(1)
        await self.reconciler.reconcile(target)
        self.account.account_scope = "another-account"
        display = positions_with_lots(self.store, account_scope=self.account.account_scope)[0]["lot_allocation"]
        self.assertEqual(display["reason"], "lot_account_changed")
        self.assertEqual(display["lots"], [])
        result = await self.reconciler.reconcile(target)
        self.assertEqual(result["reason"], "lot_position_identity_unverified")
        self.assertEqual(result["lots"], [])

    async def test_closed_and_reopened_positions_reject_old_snapshot_and_stale_caller(self):
        self.fill("client1", 1)
        target = self.position(1)
        await self.reconciler.reconcile(target)
        self.store.upsert_position({**target, "status": "closed", "size": 0})
        self.assertIsNone(self.store.position_lots(target))
        reopened = self.store.upsert_position(target)
        self.assertGreater(reopened["lifecycle_generation"], target["lifecycle_generation"])
        self.assertIsNone(self.store.position_lots(target))
        self.assertIsNone(self.store.position_lots(reopened))
        self.assert_lots(await self.reconciler.reconcile(reopened), {"client1": 1})

    async def test_native_amendment_invalidates_cached_owner_attribution_and_refreshes_display(self):
        self.fill("client1", 1)
        self.fill("client2", 2)
        target = self.position(3)
        await self.reconciler.reconcile(target)
        self.native("client1")
        self.assertIsNone(self.store.position_lots(target))
        await self.reconciler.reconcile(target)
        display = positions_with_lots(self.store)[0]["lot_allocation"]
        self.assertEqual(display["lots"][0]["protection"]["state"], "native_matched")
        self.assertEqual(display["lots"][0]["protection"]["stop_loss"], 95)
        self.assertFalse(display["execution_ready"])

    async def test_native_quantity_after_manual_partial_close_is_marked_mismatched(self):
        self.fill("client1", 1)
        self.fill("client2", 2)
        self.fill("client3", ".5", side="sell", reduce=True, source="okx-rest-orders")
        self.native("client1")
        await self.reconcile(2.5)
        display = positions_with_lots(self.store)[0]["lot_allocation"]
        self.assertEqual(display["lots"][0]["protection"]["state"], "native_size_mismatch")

    async def test_native_quantity_smaller_than_remaining_lot_is_marked_mismatched(self):
        self.fill("client1", 1)
        self.native("client1", size=".5")
        await self.reconcile(1)
        display = positions_with_lots(self.store)[0]["lot_allocation"]
        self.assertEqual(display["lots"][0]["protection"]["state"], "native_size_mismatch")
        self.assertEqual(display["lots"][0]["protection"]["size"], .5)
        self.assertEqual(display["lots"][0]["protection"]["stop_loss"], 95)
        self.assertFalse(display["execution_ready"])

    async def test_terminal_native_state_without_quantity_is_displayed_without_live_prices(self):
        self.fill("client1", 1)
        self.native("client1", size="", state="canceled")
        await self.reconcile(1)
        protection = positions_with_lots(self.store)[0]["lot_allocation"]["lots"][0]["protection"]
        self.assertEqual(protection, {
            "state": "canceled", "size": None, "triggered": False, "stop_loss": None, "take_profit": None,
        })

    async def test_private_stream_pushes_lot_updates_without_exchange_requests(self):
        self.fill("client1", 1)
        self.native("client1")
        target = self.position(1)
        await self.reconciler.reconcile(target)
        account_stream = SimpleNamespace(
            configured=True, connected=True, authenticated=True, balance=[], last_message_at=None,
        )
        events = private_events(self.store, account_stream, self.account, lambda: True)

        async def next_positions():
            async def read():
                async for frame in events:
                    if frame.startswith("event: positions\n"):
                        return json.loads(frame.split("\ndata: ", 1)[1])["data"]
            return await asyncio.wait_for(read(), 2)

        try:
            first = (await next_positions())[0]["lot_allocation"]
            self.assertEqual(first["status"], "verified")
            calls = (self.account.pages, list(self.account.lookups))
            self.native("client1", size=".5")
            pending = (await next_positions())[0]["lot_allocation"]
            self.assertEqual(pending["status"], "unverified")
            self.assertEqual(pending["lots"], [])
            self.assertEqual((self.account.pages, self.account.lookups), calls)
            await self.reconciler.reconcile(target)
            calls = (self.account.pages, list(self.account.lookups))
            updated = (await next_positions())[0]["lot_allocation"]
            self.assertEqual(updated["lots"][0]["protection"]["state"], "native_size_mismatch")
            self.assertEqual(updated["lots"][0]["protection"]["size"], .5)
            self.assertEqual((self.account.pages, self.account.lookups), calls)
        finally:
            await events.aclose()

    async def test_fill_subtype_must_match_side_and_position_direction(self):
        self.fill("client1", 1, subType="4")
        result = await self.reconcile(1)
        self.assertEqual(result["reason"], "lot_fill_direction_mismatch")
        self.assertEqual(result["lots"], [])

    async def test_old_database_upgrade_preserves_orders_without_guessing_missing_close_context(self):
        self.fill("client1", 1)
        original = self.store.get_order("client1")
        with sqlite3.connect(self.path) as connection:
            connection.execute("DROP TABLE position_lot_snapshots")
            connection.execute("ALTER TABLE orders DROP COLUMN protection_context_json")
        reopened = StateStore(self.path)
        upgraded = reopened.get_order("client1")
        self.assertEqual(upgraded, original)
        self.assertEqual(upgraded["protection_context_json"], "{}")
        with sqlite3.connect(self.path) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM position_lot_snapshots").fetchone()[0], 0)
        reconciler = PositionLotReconciler(reopened, self.account, self.sync._save_regular_order)
        self.assert_lots(await reconciler.reconcile(self.position(1)), {"client1": 1})

    async def test_cancellation_propagates_without_writing_failure_snapshot(self):
        self.fill("client1", 1)
        target = self.position(1)

        async def cancel():
            raise asyncio.CancelledError()
        self.account.before_page = cancel
        with self.assertRaises(asyncio.CancelledError):
            await self.reconciler.reconcile(target)
        self.assertIsNone(self.store.position_lots(target))


class PositionFillPaginationTests(unittest.IsolatedAsyncioTestCase):
    def client(self, handler):
        with patch.dict(os.environ, {
            "OKX_API_KEY": "test", "OKX_SECRET_KEY": "secret", "OKX_PASSPHRASE": "pass",
            "OKX_PROXY_URL": "", "OKX_DEMO": "true",
        }):
            return OkxAccountClient(transport=httpx.MockTransport(handler))

    async def test_signed_pages_use_bill_id_not_trade_id_and_can_stop_at_boundary(self):
        queries = []

        async def handler(request):
            queries.append(dict(request.url.params))
            self.assertEqual(request.url.path, "/api/v5/trade/fills-history")
            expected = OkxAccountClient.signature(
                request.headers["OK-ACCESS-TIMESTAMP"], "GET", request.url.raw_path.decode(), secret_key="secret",
            )
            self.assertEqual(request.headers["OK-ACCESS-SIGN"], expected)
            ids = ["4", "3"] if len(queries) == 1 else ["2", "1"]
            return httpx.Response(200, json={"code": "0", "data": [
                {"instId": SYMBOL, "instType": "SWAP", "billId": value} for value in ids
            ]})
        pages = self.client(handler).position_fill_pages(SYMBOL, page_size=2)
        async for page in pages:
            if page[-1]["billId"] == "1":
                break
        await pages.aclose()
        self.assertEqual(queries, [
            {"instType": "SWAP", "instId": SYMBOL, "limit": "2"},
            {"instType": "SWAP", "instId": SYMBOL, "limit": "2", "after": "3"},
        ])

    async def test_wrong_instrument_duplicate_and_out_of_order_pages_are_rejected(self):
        for change in ({"instId": "ETH-USDT-SWAP"}, {"instType": "FUTURES"}, {"billId": "bad"}, {}):
            async def handler(_request):
                row = {"instId": SYMBOL, "instType": "SWAP", "billId": "3", **change}
                return httpx.Response(200, json={"code": "0", "data": [row, row]})
            with self.subTest(change=change), self.assertRaises(OkxAccountError):
                async for _ in self.client(handler).position_fill_pages(SYMBOL, page_size=2):
                    pass


if __name__ == "__main__":
    unittest.main()
