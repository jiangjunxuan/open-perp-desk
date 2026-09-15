from unittest.mock import patch

from app.okx_trade import OkxTradeError
from app.protection_adjustment import ProtectionAdjustment
from tests.fixtures.exchange_server import SYMBOL
from tests.test_protection_handoff import ProtectionHandoffTests


class ProtectionAdjustmentTests(ProtectionHandoffTests):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.adjustment = ProtectionAdjustment(self.manager, self.market)

    async def reduce(self, size):
        with self.exchange.lock:
            raw = self.exchange._create_order({
                "instId": SYMBOL, "side": "sell", "posSide": "net",
                "tdMode": "isolated", "ordType": "market", "sz": str(size), "reduceOnly": True,
            })
            self.exchange._fill(raw["ordId"])
        await self.sync.sync_rest()

    async def test_manual_partial_close_amends_native_quantity_to_lot_remainder(self):
        await self.entry(size=4)
        await self.reduce(1)
        result = await self.adjustment.run(SYMBOL, 100, dry_run=False, market_data_fresh=True)
        self.assertTrue(result["accepted"], result)
        algo = next(iter(self.exchange.algos.values()))
        self.assertEqual(algo["sz"], "3")
        self.assertEqual([row["path"] for row in self.exchange.posts], ["/api/v5/trade/amend-algos"])
        self.assertEqual(self.store.protection_adjustments(self.account.account_scope), [])

    async def test_lost_amendment_response_is_resolved_by_query_without_resend(self):
        await self.entry(size=4)
        await self.reduce(1)
        amend = self.trade.amend_algo_size

        async def lose(*args, **kwargs):
            await amend(*args, **kwargs)
            raise OkxTradeError("fixture lost amendment response")

        with patch.object(self.trade, "amend_algo_size", side_effect=lose):
            result = await self.adjustment.run(SYMBOL, 100, dry_run=False, market_data_fresh=True)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(len([row for row in self.exchange.posts if row["path"] == "/api/v5/trade/amend-algos"]), 1)
        self.assertEqual(self.store.protection_adjustments(self.account.account_scope), [])

    async def test_unresolved_amendment_retries_same_target_after_persisted_backoff(self):
        await self.entry(size=4)
        await self.reduce(1)
        calls = 0

        async def accepted_but_old(*args, **kwargs):
            nonlocal calls
            calls += 1
            return {"data": [{"algoId": next(iter(self.exchange.algos)), "reqId": args[3], "sCode": "0"}]}

        with patch.object(self.trade, "amend_algo_size", side_effect=accepted_but_old):
            first = await self.adjustment.run(SYMBOL, 100, dry_run=False, market_data_fresh=True)
            self.assertEqual(first["action"], "protection_adjustment_pending")
            record = self.store.protection_adjustments(self.account.account_scope)[0]
            self.store.update_protection_adjustment(record, retry_after_ms=0)
            second = await self.adjustment.run(SYMBOL, 100, dry_run=False, market_data_fresh=True)
        self.assertEqual(second["action"], "protection_adjustment_pending")
        self.assertEqual(calls, 2)
        self.assertEqual(len([row for row in self.exchange.posts if row["path"] == "/api/v5/trade/amend-algos"]), 0)

    async def test_flat_position_cancels_orphaned_live_native_protection(self):
        await self.entry(size=4)
        await self.reduce(4)
        result = await self.adjustment.run(SYMBOL, 100, dry_run=False, market_data_fresh=True)
        self.assertTrue(result["accepted"], result)
        self.assertEqual(self.exchange.algos[next(iter(self.exchange.algos))]["state"], "canceled")
        self.assertEqual([row["path"] for row in self.exchange.posts], ["/api/v5/trade/cancel-algos"])

    async def test_native_quantity_above_verified_opening_is_not_amended(self):
        await self.entry(size=4)
        algo_id = next(iter(self.exchange.algos))
        self.exchange.algos[algo_id]["sz"] = "5"
        await self.sync.sync_rest()
        result = await self.adjustment.run(SYMBOL, 100, dry_run=False, market_data_fresh=True)
        self.assertIsNone(result)
        self.assertEqual(self.exchange.posts, [])

    async def test_triggered_price_wins_over_quantity_amendment(self):
        await self.entry(size=4)
        await self.reduce(1)
        result = await self.adjustment.run(SYMBOL, 89, dry_run=False, market_data_fresh=True)
        self.assertIsNone(result)
        self.assertEqual(self.exchange.posts, [])

    async def test_adjustment_state_survives_restart_without_replaying_request(self):
        await self.entry(size=4)
        await self.reduce(1)
        async def accepted_but_still_old(*args, **kwargs):
            return {"data": [{
                "algoId": next(iter(self.exchange.algos)), "reqId": args[3], "sCode": "0",
            }]}

        with patch.object(self.trade, "amend_algo_size", side_effect=accepted_but_still_old):
            result = await self.adjustment.run(SYMBOL, 100, dry_run=False, market_data_fresh=True)
        self.assertEqual(result["action"], "protection_adjustment_pending")
        self.assertEqual(len(self.exchange.posts), 0)
        self.reopen()
        self.adjustment = ProtectionAdjustment(self.manager, self.market)
        result = await self.adjustment.run(SYMBOL, 100, dry_run=False, market_data_fresh=True)
        self.assertEqual(result["action"], "protection_adjustment_pending")
        self.assertEqual(len(self.exchange.posts), 0)
        self.assertEqual(self.store.protection_adjustments(self.account.account_scope)[0]["status"], "accepted")


if __name__ == "__main__":
    import unittest

    unittest.main()
