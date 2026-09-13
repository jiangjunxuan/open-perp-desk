import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from app.automation_worker import AutomationWorker
from app.execution_engine import ExecutionEngine
from app.okx_market import OkxMarketClient, OkxMarketError
from app.risk_engine import RiskEngine, RiskLimits
from app.state_store import StateStore
from app.trading_signal import TradeSignal
from tests.test_position_lifecycle import order


SYMBOL = "BTC-USDT-SWAP"
NOW = 1_800_000_000


def quote(**overrides):
    return {"instId": SYMBOL, "instType": "SWAP", "markPx": "100", "ts": str(NOW * 1000), **overrides}


class MarkPriceClientTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, rows):
        client = OkxMarketClient()
        with patch.object(client, "get", AsyncMock(return_value=rows)), patch("app.okx_market.time.time", return_value=NOW):
            return await client.mark_price(SYMBOL)

    async def test_exact_public_endpoint_and_demo_header(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"code": "0", "data": [quote(markPx="101.25")]})

        real_client = httpx.AsyncClient
        with patch.dict(os.environ, {"OKX_DEMO": "true", "OKX_PROXY_URL": ""}, clear=True):
            client = OkxMarketClient()
        with patch("app.okx_market.httpx.AsyncClient", side_effect=lambda **kwargs: real_client(
            **kwargs, transport=httpx.MockTransport(handler),
        )), patch("app.okx_market.time.time", return_value=NOW):
            self.assertEqual(await client.mark_price(SYMBOL), 101.25)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].method, "GET")
        self.assertEqual(requests[0].url.path, "/api/v5/public/mark-price")
        self.assertEqual(dict(requests[0].url.params), {"instType": "SWAP", "instId": SYMBOL})
        self.assertEqual(requests[0].headers["x-simulated-trading"], "1")
        self.assertNotIn("OK-ACCESS-KEY", requests[0].headers)

    async def test_empty_malformed_and_ambiguous_rows_are_rejected(self):
        for rows in (None, {}, [], ["bad"], [quote(), quote()]):
            with self.subTest(rows=rows), self.assertRaisesRegex(OkxMarketError, "mark_price_payload_invalid"):
                await self.read(rows)

    async def test_instrument_and_contract_type_must_match(self):
        for row in (quote(instId="ETH-USDT-SWAP"), quote(instId=""), quote(instType="FUTURES"), quote(instType=None)):
            with self.subTest(row=row), self.assertRaisesRegex(OkxMarketError, "mark_price_instrument_mismatch"):
                await self.read([row])

    async def test_price_must_be_finite_positive_and_numeric_not_boolean(self):
        for value in ("0", "-1", "NaN", "Infinity", "-Infinity", "", None, True, False, {}, [], "1e999", "1e-999"):
            with self.subTest(value=value), self.assertRaisesRegex(OkxMarketError, "^mark_price_invalid$"):
                await self.read([quote(markPx=value)])

    async def test_timestamp_must_be_finite_positive_and_numeric(self):
        for value in ("0", "-1", "NaN", "Infinity", "", None, True, {}, "not-a-timestamp"):
            with self.subTest(value=value), self.assertRaisesRegex(OkxMarketError, "mark_price_timestamp_invalid"):
                await self.read([quote(ts=value)])

    async def test_timestamp_limits_are_checked_after_receiving_response(self):
        for age in (-5, 0, 30):
            self.assertEqual(await self.read([quote(ts=str((NOW - age) * 1000))]), 100)
        for age in (-5.001, 30.001):
            with self.subTest(age=age), self.assertRaisesRegex(OkxMarketError, "mark_price_stale"):
                await self.read([quote(ts=str((NOW - age) * 1000))])
        client = OkxMarketClient()
        clock = [NOW]

        async def delayed_response(*_args):
            clock[0] += 31
            return [quote()]

        with patch.object(client, "get", delayed_response), patch("app.okx_market.time.time", side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(OkxMarketError, "mark_price_stale"):
                await client.mark_price(SYMBOL)

    async def test_cancellation_is_not_transformed_into_a_price(self):
        client = OkxMarketClient()
        with patch.object(client, "get", AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await client.mark_price(SYMBOL)


class ProtectiveMarkWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.store.save_strategy("structured-technical", "test", enabled=True, config={})
        self.environment = patch.dict(os.environ, {
            "AUTO_TRADING_SYMBOLS": SYMBOL, "AUTO_TRADING_ACCOUNT_EQUITY": "1000",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = OkxMarketClient()
        self.client.get = AsyncMock(return_value=[quote()])
        self.market = SimpleNamespace(
            mark_price=self.client.mark_price,
            ticker=AsyncMock(return_value={"instId": SYMBOL, "last": "94"}),
            candles=AsyncMock(return_value=[]),
        )
        signal = TradeSignal(
            inst_id=SYMBOL, action="open_long", confidence=.9, leverage=1,
            position_pct=5, entry_price=100, stop_loss=95, take_profit=110,
        )
        self.strategy = SimpleNamespace(analyze=Mock(return_value={
            "inst_id": SYMBOL, "source": "test", "bias": "bullish",
            "signal": signal.model_dump(mode="json"), "report": {},
        }))
        self.engine = ExecutionEngine(
            self.store, RiskEngine(RiskLimits()), SimpleNamespace(enabled=False),
            SimpleNamespace(configured=False),
        )
        self.engine.submit_signal = AsyncMock(return_value={"accepted": True, "idempotent": False})
        self.engine.notify_event = AsyncMock(return_value=True)
        self.notifier = AsyncMock(return_value=True)
        self.worker = AutomationWorker(
            self.market, SimpleNamespace(configured=False), SimpleNamespace(sync_stream=lambda: {}),
            self.strategy, self.engine, RiskEngine(RiskLimits()), self.store, notifier=self.notifier,
        )
        self.worker.enabled = self.worker.dry_run = True
        self.position()

    def position(self, **overrides):
        for existing in self.store.list_positions():
            self.store.upsert_position({**existing, "status": "closed", "size": 0})
        self.store.upsert_position({
            "inst_id": SYMBOL, "pos_side": "long",
            "size": 1, "entry_price": 100, "stop_loss": 95, "take_profit": 110, **overrides,
        })

    async def cycle(self):
        with patch("app.okx_market.time.time", return_value=NOW):
            return await self.worker.run_once()

    async def test_last_trade_crossing_stop_does_not_trigger_mark_protection(self):
        result = await self.cycle()
        self.assertEqual(result["results"][0]["action"], "skip_open_existing_exposure")
        self.engine.submit_signal.assert_not_awaited()
        self.engine.notify_event.assert_not_awaited()

    async def test_fresh_mark_can_trigger_even_when_ticker_is_unavailable(self):
        self.client.get.return_value = [quote(markPx="94")]
        self.market.ticker.side_effect = OkxMarketError("ticker unavailable")
        result = await self.cycle()
        self.assertEqual(result["results"][0]["action"], "stop_loss")
        self.assertEqual(self.engine.submit_signal.await_args.kwargs["side_override"], "sell")
        self.assertEqual(self.engine.submit_signal.await_args.kwargs["size"], 1)
        self.assertTrue(self.engine.submit_signal.await_args.kwargs["dry_run"])
        self.market.ticker.assert_not_awaited()
        self.strategy.analyze.assert_not_called()
        evidence = self.engine.notify_event.await_args.kwargs["payload"]
        self.assertEqual(evidence["mark_price"], 94)
        self.assertEqual(evidence["price_source"], "mark")

    async def test_missing_invalid_or_stale_mark_blocks_symbol_and_recovers_on_next_cycle(self):
        for rows in ([], [quote(markPx="NaN")], [quote(ts=str((NOW - 31) * 1000))], [quote(instId="ETH-USDT-SWAP")]):
            self.client.get.return_value = rows
            result = await self.cycle()
            self.assertEqual(result["results"][0]["action"], "skip_protection_price_unavailable")
            self.assertFalse(result["results"][0]["accepted"])
        self.client.get.side_effect = OkxMarketError("secret-proxy-url")
        await self.cycle()
        self.assertNotIn("secret-proxy-url", str(self.store.list_audit()))
        self.engine.submit_signal.assert_not_awaited()
        self.market.ticker.assert_not_awaited()
        self.strategy.analyze.assert_not_called()
        self.assertEqual(self.notifier.await_count, 5)
        self.client.get.side_effect = None
        self.client.get.return_value = [quote(markPx="94")]
        self.assertEqual((await self.cycle())["results"][0]["action"], "stop_loss")
        self.engine.submit_signal.assert_awaited_once()

    async def test_short_and_net_short_closes_use_buy_and_absolute_size(self):
        for pos_side, size in (("short", 2), ("net", -2)):
            self.position(pos_side=pos_side, size=size, stop_loss=105, take_profit=90)
            self.client.get.return_value = [quote(markPx="106")]
            result = await self.cycle()
            self.assertEqual(result["results"][0]["action"], "stop_loss")
            self.assertEqual(self.engine.submit_signal.await_args.kwargs["side_override"], "buy")
            self.assertEqual(self.engine.submit_signal.await_args.kwargs["size"], 2)

    async def test_worker_cancellation_does_not_emit_unavailable_alert_or_submit(self):
        self.client.get.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.cycle()
        self.engine.submit_signal.assert_not_awaited()
        self.notifier.assert_not_awaited()

    async def test_execution_rejects_invalid_prices_before_comparing_thresholds(self):
        for price in (True, False, 0, -1, float("nan"), float("inf"), float("-inf"), "94", None):
            with self.subTest(price=price), self.assertRaisesRegex(ValueError, "mark_price_invalid"):
                self.engine.protective_exit(inst_id=SYMBOL, mark_price=price)
        self.engine.submit_signal.assert_not_awaited()

    async def test_reopened_position_gets_new_preview_but_repeated_snapshot_does_not(self):
        self.client.get.return_value = [quote(markPx="94")]
        self.engine.submit_signal = ExecutionEngine.submit_signal.__get__(self.engine)
        await self.cycle()
        first_id = self.store.list_orders()[0]["client_order_id"]
        await self.cycle()
        self.assertEqual(len(self.store.list_orders()), 1)
        self.position()
        await self.cycle()
        self.assertEqual(len(self.store.list_orders()), 2)
        self.assertNotEqual(self.store.list_orders()[0]["client_order_id"], first_id)
        await self.cycle()
        self.assertEqual(len(self.store.list_orders()), 2)

    async def test_unconfirmed_old_close_blocks_new_lifecycle_until_reconciled(self):
        self.worker.dry_run = False
        self.engine.trade_client.enabled = True
        self.worker._account_equity = AsyncMock(return_value=1000)
        self.client.get.return_value = [quote(markPx="94")]
        self.store.save_order(order())
        self.position()
        result = await self.cycle()
        self.assertEqual(result["results"][0]["action"], "skip_pending_protective_close")
        self.engine.submit_signal.assert_not_awaited()
        self.store.save_order(order(status="filled"))
        result = await self.cycle()
        self.assertEqual(result["results"][0]["action"], "stop_loss")
        self.engine.submit_signal.assert_awaited_once()
        self.assertTrue(self.engine.submit_signal.await_args.kwargs["idempotency_key"].endswith(":lifecycle:1"))


if __name__ == "__main__":
    unittest.main()
