import json
import time
import unittest

from app.okx_market_stream import OkxMarketStream


class OkxMarketStreamTests(unittest.TestCase):
    def setUp(self) -> None:
        self.stream = OkxMarketStream(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])

    def test_subscriptions_separate_public_tickers_and_business_candles(self) -> None:
        message = self.stream.subscription_message()

        self.assertEqual(message["op"], "subscribe")
        channels = {
            (item["channel"], item["instId"]) for item in message["args"]
        }
        self.assertIn(("tickers", "BTC-USDT-SWAP"), channels)
        self.assertIn(("books5", "BTC-USDT-SWAP"), channels)
        self.assertIn(("trades", "BTC-USDT-SWAP"), channels)
        self.assertNotIn(("candle1m", "ETH-USDT-SWAP"), channels)
        candles = self.stream.subscription_message(candles=True)
        self.assertEqual(
            {item["channel"] for item in candles["args"]},
            {"candle1m", "candle15m", "candle1H", "candle4H"},
        )
        self.assertTrue(self.stream.candles_url.endswith("/business"))

    def test_consume_caches_ticker_and_candle(self) -> None:
        self.stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
                    "data": [{"last": "62000", "sodUtc8": "61500"}],
                }
            )
        )
        self.stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "candle1m", "instId": "BTC-USDT-SWAP"},
                    "data": [["1710000000000", "1", "2", "0.5", "1.5", "10"]],
                }
            )
        )

        self.assertEqual(
            self.stream.tickers["BTC-USDT-SWAP"]["data"]["last"],
            "62000",
        )
        self.assertEqual(
            self.stream.candles["BTC-USDT-SWAP"]["data"][4],
            "1.5",
        )
        self.assertIsNotNone(self.stream.last_message_at)
        self.assertIsNotNone(self.stream.candles_last_message_at)

    def test_consume_caches_orderbook(self) -> None:
        self.stream.connected = True
        self.stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "books5", "instId": "BTC-USDT-SWAP"},
                    "data": [{
                        "asks": [["101", "2", "0", "1"]],
                        "bids": [["99", "3", "0", "1"]],
                        "ts": "1710000000000",
                    }],
                }
            )
        )
        snapshot = self.stream.browser_snapshot("15m")
        self.assertEqual(snapshot["order_books"]["BTC-USDT-SWAP"]["data"]["asks"][0][0], "101")
        self.assertTrue(snapshot["order_books"]["BTC-USDT-SWAP"]["fresh"])

    def test_consume_merges_recent_trades_without_duplicates(self) -> None:
        self.stream.connected = True
        for payload in (
            [{"tradeId": "2", "px": "101", "sz": "2", "side": "sell", "ts": "2"}],
            [
                {"tradeId": "2", "px": "101", "sz": "2", "side": "sell", "ts": "2"},
                {"tradeId": "1", "px": "100", "sz": "1", "side": "buy", "ts": "1"},
            ],
        ):
            self.stream.consume(json.dumps({
                "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
                "data": payload,
            }))

        snapshot = self.stream.browser_snapshot("15m")
        trades = snapshot["trades"]["BTC-USDT-SWAP"]
        self.assertEqual([item["tradeId"] for item in trades["data"]], ["2", "1"])
        self.assertTrue(trades["fresh"])

    def test_invalid_messages_do_not_change_state(self) -> None:
        self.stream.consume("not-json")
        self.stream.consume(json.dumps({"event": "subscribe"}))
        self.stream.consume(json.dumps({"arg": {"channel": "tickers"}, "data": []}))
        for raw in ("[]", "null", '{"arg":[],"data":[1]}'):
            self.stream.consume(raw)

        self.assertEqual(self.stream.tickers, {})
        self.assertEqual(self.stream.candles, {})
        self.assertFalse(self.stream.fresh)

    def test_disconnect_invalidates_freshness_even_after_recent_tick(self) -> None:
        self.stream.consume(
            json.dumps(
                {
                    "arg": {"channel": "tickers", "instId": "BTC-USDT-SWAP"},
                    "data": [{"last": "62000"}],
                }
            )
        )
        self.stream.connected = True
        self.stream._last_message_epoch = time.monotonic()
        self.assertTrue(self.stream.fresh)

        self.stream.connected = False
        self.assertFalse(self.stream.fresh)

    def test_candle_updates_cannot_make_stale_ticker_fresh(self) -> None:
        self.stream.connected = True
        self.stream.candles_connected = True
        self.stream.consume(json.dumps({
            "arg": {"channel": "candle1m", "instId": "BTC-USDT-SWAP"},
            "data": [["1", "2", "3", "1", "2", "1"]],
        }))
        self.assertTrue(self.stream.candles_fresh)
        self.assertFalse(self.stream.fresh)


if __name__ == "__main__":
    unittest.main()
