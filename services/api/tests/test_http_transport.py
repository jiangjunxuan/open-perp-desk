import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio

from app.http_transport import proxy_cleanup_trace


class ProxyCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_handshake_closes_only_its_own_stream_once(self):
        first, second = proxy_cleanup_trace(), proxy_cleanup_trace()
        stream = SimpleNamespace(aclose=AsyncMock())
        other = SimpleNamespace(aclose=AsyncMock())
        await first("socks.connect_tcp.complete", {"return_value": stream})
        await second("socks.connect_tcp.complete", {"return_value": other})
        await first("socks.setup_socks5_connection.failed", {})
        await first("socks.setup_socks5_connection.failed", {})
        stream.aclose.assert_awaited_once()
        other.aclose.assert_not_awaited()

    async def test_tls_failure_closes_the_current_stream(self):
        trace = proxy_cleanup_trace()
        initial = SimpleNamespace(aclose=AsyncMock())
        current = SimpleNamespace(aclose=AsyncMock())
        await trace("socks.connect_tcp.complete", {"return_value": initial})
        await trace("socks.start_tls.complete", {"return_value": current})
        await trace("socks.start_tls.failed", {})
        initial.aclose.assert_not_awaited()
        current.aclose.assert_awaited_once()

    async def test_http_connection_owns_successful_stream_cleanup(self):
        for protocol in ("http11", "http2"):
            with self.subTest(protocol=protocol):
                trace = proxy_cleanup_trace()
                stream = SimpleNamespace(aclose=AsyncMock())
                await trace("socks.connect_tcp.complete", {"return_value": stream})
                await trace(f"{protocol}.send_request_headers.started", {})
                await trace("socks.start_tls.failed", {})
                stream.aclose.assert_not_awaited()

    async def test_direct_and_http_proxy_connections_are_not_intercepted(self):
        trace = proxy_cleanup_trace()
        stream = SimpleNamespace(aclose=AsyncMock())
        await trace("connection.connect_tcp.complete", {"return_value": stream})
        await trace("connection.start_tls.failed", {})
        await trace("socks.setup_socks5_connection.failed", {})
        stream.aclose.assert_not_awaited()

    async def test_cleanup_error_does_not_replace_the_transport_error(self):
        trace = proxy_cleanup_trace()
        stream = SimpleNamespace(aclose=AsyncMock(side_effect=OSError("fixture close failed")))
        await trace("socks.connect_tcp.complete", {"return_value": stream})
        await trace("socks.setup_socks5_connection.failed", {})
        stream.aclose.assert_awaited_once()

    async def test_cleanup_survives_cancellation(self):
        trace = proxy_cleanup_trace()
        closed = []

        async def close():
            await anyio.lowlevel.checkpoint()
            closed.append(True)

        await trace("socks.connect_tcp.complete", {"return_value": SimpleNamespace(aclose=close)})
        with anyio.CancelScope() as scope:
            scope.cancel()
            await trace("socks.setup_socks5_connection.failed", {})
        self.assertEqual(closed, [True])
