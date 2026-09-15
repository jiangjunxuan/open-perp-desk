import asyncio
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app import main as api_main
from app.tradingview import (
    TradingViewWebhookError,
    normalize_instrument,
    parse_body,
    to_signal,
)


class TradingViewNormalizationTests(unittest.TestCase):
    def payload(self, **overrides):
        data = {
            "secret": "test-secret",
            "alert_id": "alert-1",
            "symbol": "BINANCE:BTCUSDT.P",
            "action": "open_long",
            "confidence": 0.9,
            "leverage": 2,
            "position_pct": 5,
            "entry_price": 50000,
            "stop_loss": 49000,
            "take_profit": 52000,
            "size": 0.01,
        }
        data.update(overrides)
        return parse_body(json.dumps(data).encode())

    def test_normalizes_common_tradingview_symbols(self):
        self.assertEqual(normalize_instrument("BINANCE:BTCUSDT.P"), "BTC-USDT-SWAP")
        self.assertEqual(normalize_instrument("OKX:ETH-USDT-SWAP"), "ETH-USDT-SWAP")

    def test_signal_is_structured_and_has_server_expiry(self):
        payload = self.payload()
        signal, size, side, alert_id = to_signal(
            payload,
            body=b'{"alert_id":"alert-1"}',
        )
        self.assertEqual(signal.source, "tradingview")
        self.assertEqual(signal.inst_id, "BTC-USDT-SWAP")
        self.assertEqual(size, 0.01)
        self.assertIsNone(side)
        self.assertEqual(alert_id, "alert-1")
        self.assertGreater(signal.expires_at, signal.created_at)

    def test_close_requires_direction_for_safe_reduce_only_order(self):
        with self.assertRaises(TradingViewWebhookError) as context:
            to_signal(
                self.payload(
                    action="close",
                    entry_price=None,
                    stop_loss=None,
                    take_profit=None,
                ),
                body=b"close",
            )
        self.assertEqual(context.exception.code, "close_side_required")

    def test_old_alert_is_rejected(self):
        with self.assertRaises(TradingViewWebhookError) as context:
            to_signal(
                self.payload(timestamp="2020-01-01T00:00:00Z"),
                body=b"old",
            )
        self.assertEqual(context.exception.code, "alert_too_old")


class TradingViewEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_passes_only_structured_signal_to_executor(self):
        payload = {
            "secret": "test-secret",
            "alert_id": "alert-42",
            "symbol": "BINANCE:BTCUSDT.P",
            "action": "open_long",
            "confidence": 0.95,
            "leverage": 2,
            "position_pct": 4,
            "entry_price": 50000,
            "stop_loss": 49000,
            "take_profit": 52000,
            "size": 0.01,
        }
        result = {
            "accepted": True,
            "idempotent": False,
            "dry_run": True,
            "order": {"status": "preview"},
        }
        with patch.dict(
            os.environ,
            {
                "TRADINGVIEW_ENABLED": "true",
                "TRADINGVIEW_WEBHOOK_SECRET": "test-secret",
                "TRADINGVIEW_EXECUTION_ENABLED": "false",
                "TRADINGVIEW_DRY_RUN": "true",
                "TRADINGVIEW_SYMBOLS": "BTC-USDT-SWAP",
            },
            clear=False,
        ), patch.object(api_main, "market_stream", SimpleNamespace(fresh=True)), patch.object(
            api_main.execution_engine,
            "submit_signal",
            new=AsyncMock(return_value=result),
        ) as submit, patch.object(api_main.state_store, "add_audit"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api_main.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/integrations/tradingview/webhook",
                    json=payload,
                )
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertEqual(body["inst_id"], "BTC-USDT-SWAP")
        kwargs = submit.await_args.kwargs
        signal = submit.await_args.args[0]
        self.assertTrue(kwargs["dry_run"])
        self.assertEqual(kwargs["idempotency_key"], "tradingview:alert-42")
        self.assertEqual(signal.source, "tradingview")
        self.assertEqual(signal.inst_id, "BTC-USDT-SWAP")

    async def test_webhook_rejects_wrong_secret_without_execution(self):
        payload = {
            "secret": "wrong",
            "alert_id": "alert-43",
            "symbol": "BTCUSDT",
            "action": "hold",
        }
        with patch.dict(
            os.environ,
            {
                "TRADINGVIEW_ENABLED": "true",
                "TRADINGVIEW_WEBHOOK_SECRET": "test-secret",
            },
            clear=False,
        ), patch.object(api_main.execution_engine, "submit_signal", new=AsyncMock()) as submit:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=api_main.app),
                base_url="http://test",
            ) as client:
                response = await client.post(
                    "/api/v1/integrations/tradingview/webhook",
                    json=payload,
                )
        self.assertEqual(response.status_code, 401)
        submit.assert_not_awaited()
