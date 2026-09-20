import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.okx_market_stream import OkxMarketStream


ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("public_stream_probe", ROOT / "infra/stream-smoke.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class PublicStreamProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.destination = self.root / "outputs/public-stream-verification.json"
        self.destination.parent.mkdir()
        self.destination.write_text('{"connected":true,"checked_at":"old"}')
        self.stream = OkxMarketStream(["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
        self.stream.start = AsyncMock()
        self.stream.stop = AsyncMock()
        for name, value in (
            ("ROOT", self.root),
            ("OkxMarketStream", lambda _: self.stream),
        ):
            replacement = patch.object(probe, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)

    def populate(self, *, mismatch=False):
        self.stream.connected = self.stream.candles_connected = True
        for symbol in self.stream.symbols:
            self.stream.consume(json.dumps({
                "arg": {"channel": "tickers", "instId": symbol},
                "data": [{"instId": "invalid" if mismatch else symbol, "last": "100"}],
            }))
            self.stream.consume(json.dumps({
                "arg": {"channel": "candle1m", "instId": symbol},
                "data": [["1", "100", "101", "99", "100", "10", "10", "1000", "1"]],
            }))

    async def test_failed_probe_removes_previous_success_and_stops_stream(self):
        self.stream.start.side_effect = ConnectionError("local fixture failure")
        with self.assertRaises(ConnectionError):
            await probe.main()
        self.assertFalse(self.destination.exists())
        self.stream.stop.assert_awaited_once()

    async def test_invalid_evidence_cannot_leave_previous_success(self):
        self.populate(mismatch=True)
        with self.assertRaisesRegex(RuntimeError, "evidence is incomplete"):
            await probe.main()
        self.assertFalse(self.destination.exists())
        self.stream.stop.assert_awaited_once()

    async def test_success_publishes_current_readonly_evidence(self):
        self.populate()
        with redirect_stdout(io.StringIO()):
            await probe.main()
        report = json.loads(self.destination.read_text())
        self.assertNotEqual(report["checked_at"], "old")
        self.assertTrue(report["fresh"] and report["candles_fresh"])
        self.assertEqual(len(report["received"]), 2)
        self.assertFalse(report["trading_performed"] or report["private_account_verified"])
        self.stream.stop.assert_awaited_once()
