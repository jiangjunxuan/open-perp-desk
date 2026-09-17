import importlib.util
import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.okx_market_stream import OkxMarketStream


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("proxy_probe", ROOT / "infra/proxy-smoke.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProxyWebsocketProbeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stream = OkxMarketStream(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        self.stream.start = AsyncMock()
        self.stream.stop = AsyncMock()
        self.stream.connected = self.stream.candles_connected = True
        for symbol in self.stream.symbols:
            self.quote(symbol)
            for bar in self.stream.candle_bars:
                self.candle(symbol, bar)

    def quote(self, symbol, *, identity=None, price="100"):
        self.stream.consume(json.dumps({
            "arg": {"channel": "tickers", "instId": symbol},
            "data": [{"instId": identity or symbol, "last": price}],
        }))

    def candle(self, symbol, bar, *, row=None):
        self.stream.consume(json.dumps({
            "arg": {"channel": f"candle{bar}", "instId": symbol},
            "data": [row if row is not None else ["1710000000000", "100", "101", "99", "100", "10"]],
        }))

    async def test_success_proves_every_symbol_and_period(self):
        async def advance(_):
            for symbol in self.stream.symbols:
                self.quote(symbol)

        with patch.object(probe.asyncio, "sleep", side_effect=advance):
            result = await probe.websocket_probe(self.stream, .1)
        self.assertTrue(result["quotes_advanced"])
        self.assertEqual(result["received"], [
            {"instrument": symbol, "quote_fresh": True,
             "fresh_candle_bars": ["1m", "15m", "1H", "4H"]}
            for symbol in self.stream.symbols
        ])
        self.stream.stop.assert_awaited_once()

    async def test_missing_one_symbol_period_cannot_pass_on_global_freshness(self):
        del self.stream._candles_by_bar["4H"]["ETH-USDT-SWAP"]
        self.assertTrue(self.stream.fresh and self.stream.candles_fresh)
        with self.assertRaises(TimeoutError):
            await probe.websocket_probe(self.stream, .01)
        self.stream.stop.assert_awaited_once()

    async def test_stale_individual_quote_or_candle_cannot_pass(self):
        for channel in ("tickers", "candle1m", "candle15m", "candle1H", "candle4H"):
            with self.subTest(channel=channel):
                self.setUp()
                self.stream._record_epochs[(channel, "ETH-USDT-SWAP")] = time.monotonic() - 60
                self.assertTrue(self.stream.fresh and self.stream.candles_fresh)
                with self.assertRaises(TimeoutError):
                    await probe.websocket_probe(self.stream, .01)
                self.stream.stop.assert_awaited_once()

    async def test_invalid_identity_or_prices_cannot_publish_success(self):
        for identity, price in (("OTHER-USDT-SWAP", "100"), (None, "NaN"), (None, "Infinity"), (None, "0")):
            with self.subTest(identity=identity, price=price):
                self.setUp()
                self.quote("ETH-USDT-SWAP", identity=identity, price=price)
                with self.assertRaises(RuntimeError):
                    await probe.websocket_probe(self.stream, .1)
                self.stream.stop.assert_awaited_once()

    async def test_malformed_candles_cannot_publish_success(self):
        for row in ([], ["1"], ["1", "100", "101", "99", "NaN", "10"],
                    ["1", "100", "101", "99", "100", "-1"],
                    ["0", "100", "101", "99", "100", "10"]):
            with self.subTest(row=row):
                self.setUp()
                self.candle("ETH-USDT-SWAP", "4H", row=row)
                with self.assertRaisesRegex(RuntimeError, "candle_payload_invalid"):
                    await probe.websocket_probe(self.stream, .1)
                self.stream.stop.assert_awaited_once()

    async def test_one_frozen_quote_cannot_pass_when_other_symbol_advances(self):
        async def advance(_):
            self.quote("BTC-USDT-SWAP")

        with patch.object(probe.asyncio, "sleep", side_effect=advance):
            with self.assertRaisesRegex(RuntimeError, "ticker_stream_not_advancing"):
                await probe.websocket_probe(self.stream, .1)
        self.stream.stop.assert_awaited_once()

    async def test_disconnection_during_observation_cannot_pass(self):
        async def disconnect(_):
            self.stream.candles_connected = False

        with patch.object(probe.asyncio, "sleep", side_effect=disconnect):
            with self.assertRaisesRegex(RuntimeError, "websocket_stream_stale"):
                await probe.websocket_probe(self.stream, .1)
        self.stream.stop.assert_awaited_once()

    async def test_partial_start_failure_stops_stream(self):
        self.stream.start.side_effect = RuntimeError("fixture_start_failed")
        with self.assertRaisesRegex(RuntimeError, "fixture_start_failed"):
            await probe.websocket_probe(self.stream, .1)
        self.stream.stop.assert_awaited_once()
