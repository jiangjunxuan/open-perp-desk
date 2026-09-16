from contextlib import suppress

import anyio


def proxy_cleanup_trace():
    # httpcore 1.0.9 leaves the TCP stream unowned when a SOCKS handshake fails.
    # Use its request-local trace extension; never patch the shared connection pool.
    stream = None

    async def trace(event, info):
        nonlocal stream
        if event in {"socks.connect_tcp.complete", "socks.start_tls.complete"}:
            stream = info.get("return_value")
        elif event in {"socks.setup_socks5_connection.failed", "socks.start_tls.failed"}:
            connection, stream = stream, None
            if connection is not None:
                with anyio.CancelScope(shield=True), suppress(Exception):
                    await connection.aclose()
        elif event.startswith(("http11.", "http2.")):
            stream = None

    return trace
