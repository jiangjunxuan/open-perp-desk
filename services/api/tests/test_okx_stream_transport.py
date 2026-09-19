import asyncio
import base64
import hashlib
import hmac
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, asynccontextmanager, contextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

import httpx
from httpcore._backends.auto import AutoBackend
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from app.okx_account import OkxAccountClient, OkxAccountError
from app.okx_account_stream import OkxAccountStream
from app.okx_algo_stream import OkxAlgoOrderStream
from app.okx_market import OkxMarketClient, OkxMarketError
from app.okx_market_stream import OkxMarketStream
from app.okx_trade import OkxTradeClient, OkxTradeError, OrderRequest
from app.okx_websocket import socket_messages, websocket_tls
from app.pushplus import PushPlusClient


async def wait_for(predicate, timeout=4):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class LocalExchange:
    def __init__(self):
        self.sockets = {}
        self.subscriptions = {}
        self.logins = []
        self.connection_count = {}
        self.errors = []
        self.emit = True
        self.reject_login = False
        self.reject_subscription = False

    async def handler(self, socket):
        path = socket.request.path
        self.sockets[path] = socket
        self.connection_count[path] = self.connection_count.get(path, 0) + 1
        try:
            if path in ("/private", "/algo"):
                login = json.loads(await socket.recv())
                self.logins.append(login)
                item = login["args"][0]
                expected = base64.b64encode(hmac.new(
                    b"fixture-secret", f"{item['timestamp']}GET/users/self/verify".encode(),
                    hashlib.sha256,
                ).digest()).decode()
                valid = item["apiKey"] == "fixture-key" and item["passphrase"] == "fixture-pass" and hmac.compare_digest(item["sign"], expected)
                await socket.send(json.dumps({"event": "login", "code": "0" if valid and not self.reject_login else "60009"}))
                if not valid or self.reject_login:
                    await socket.wait_closed()
                    return
            subscription = json.loads(await socket.recv())
            self.subscriptions[path] = subscription["args"]
            expected_channels = {
                "/public": {"tickers", "books5", "trades", "open-interest", "funding-rate"}, "/candles": {"candle1m", "candle15m", "candle1H", "candle4H"},
                "/private": {"account", "positions", "orders"}, "/algo": {"orders-algo"},
            }[path]
            actual = {item["channel"] for item in subscription["args"]}
            if actual != expected_channels or self.reject_subscription:
                await socket.send(json.dumps({"event": "error", "code": "60018"}))
                await socket.wait_closed()
                return
            for item in subscription["args"]:
                await socket.send(json.dumps({"event": "subscribe", "arg": item}))
                if not self.emit:
                    continue
                data = {
                    "tickers": [{"last": "100", "instId": "BTC-USDT-SWAP"}],
                    "books5": [{"asks": [["101", "1", "0", "1"]], "bids": [["99", "1", "0", "1"]], "ts": "1"}],
                    "trades": [{"tradeId": "trade-1", "px": "100", "sz": "1", "side": "buy", "ts": "1"}],
                    "open-interest": [{"oi": "100", "instId": "BTC-USDT-SWAP"}],
                    "funding-rate": [{"fundingRate": "0.0001", "instId": "BTC-USDT-SWAP"}],
                    "candle1m": [["1", "100", "101", "99", "100", "1"]],
                    "candle15m": [["1", "100", "101", "99", "100", "1"]],
                    "candle1H": [["1", "100", "101", "99", "100", "1"]],
                    "candle4H": [["1", "100", "101", "99", "100", "1"]],
                    "account": [{"ccy": "USDT", "cashBal": "1000"}],
                    "positions": [{"instId": "BTC-USDT-SWAP", "posSide": "net", "pos": "1"}],
                    "orders": [{"ordId": "order1", "state": "filled", "tradeId": "fill1", "fillSz": "1", "fillPx": "100"}],
                    "orders-algo": [{"algoId": "algo1", "state": "live", "slTriggerPx": "95", "tpTriggerPx": "110"}],
                }[item["channel"]]
                await socket.send(json.dumps({"arg": item, "data": data}))
            async for raw in socket:
                if raw == "ping":
                    await socket.send("pong")
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.errors.append(type(exc).__name__)

    @staticmethod
    def environment(port, proxy="", *, secure=False):
        base = f"{'wss' if secure else 'ws'}://127.0.0.1:{port}"
        return {
            "OKX_API_KEY": "fixture-key", "OKX_SECRET_KEY": "fixture-secret",
            "OKX_PASSPHRASE": "fixture-pass", "OKX_DEMO": "true",
            "OKX_WS_PUBLIC_URL": f"{base}/public", "OKX_WS_CANDLES_URL": f"{base}/candles",
            "OKX_WS_PRIVATE_URL": f"{base}/private", "OKX_WS_BUSINESS_URL": f"{base}/algo",
            "OKX_PROXY_URL": proxy,
        }


class WebSocketTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_sockets_login_subscribe_and_cache_four_streams(self):
        exchange = LocalExchange()
        async with serve(exchange.handler, "127.0.0.1", 0) as server:
            env = exchange.environment(server.sockets[0].getsockname()[1])
            with patch.dict(os.environ, env, clear=True):
                market, account, algo = OkxMarketStream(["BTC-USDT-SWAP"]), OkxAccountStream(), OkxAlgoOrderStream()
            try:
                await asyncio.gather(market.start(), account.start(), algo.start())
                await wait_for(lambda: market.fresh and market.candles_fresh and account.fills and algo.orders)
                self.assertEqual(len(exchange.subscriptions), 4)
                self.assertEqual(len(exchange.logins), 2)
                self.assertTrue(account.authenticated and algo.authenticated)
                self.assertEqual(account.snapshot()["fills"][0]["tradeId"], "fill1")
                self.assertEqual(algo.snapshot()["orders"][0]["slTriggerPx"], "95")
                self.assertEqual(exchange.errors, [])
            finally:
                await asyncio.gather(market.stop(), account.stop(), algo.stop())
            self.assertFalse(market.connected or market.candles_connected or account.authenticated or algo.authenticated)

    async def test_clean_close_marks_offline_and_reconnect_requires_new_data(self):
        exchange = LocalExchange()
        async with serve(exchange.handler, "127.0.0.1", 0) as server:
            with patch.dict(os.environ, exchange.environment(server.sockets[0].getsockname()[1]), clear=True):
                market, account, algo = OkxMarketStream(["BTC-USDT-SWAP"]), OkxAccountStream(), OkxAlgoOrderStream()
            try:
                await asyncio.gather(market.start(), account.start(), algo.start())
                await wait_for(lambda: market.fresh and market.candles_fresh and account.fills and algo.orders)
                self.assertTrue(account.account_ready)
                exchange.emit = False
                await asyncio.gather(*(socket.close(code=1000) for socket in list(exchange.sockets.values())))
                await wait_for(lambda: not any((market.connected, market.candles_connected, account.connected, algo.connected)))
                self.assertFalse(account.authenticated or algo.authenticated or market.fresh or market.candles_fresh)
                self.assertFalse(account.account_ready)
                self.assertEqual(account.balance, [])
                await wait_for(lambda: len(exchange.connection_count) == 4 and min(exchange.connection_count.values()) >= 2)
                await wait_for(lambda: market.connected and market.candles_connected and account.authenticated and algo.authenticated)
                self.assertFalse(market.fresh or market.candles_fresh)
                self.assertTrue(market.tickers and market.candles)
                self.assertFalse(account.account_ready)
                self.assertEqual(account.balance, [])
                await exchange.sockets["/private"].send(json.dumps({
                    "arg": {"channel": "account"}, "data": [{"totalEq": "2000"}],
                }))
                await wait_for(lambda: account.account_ready)
                self.assertEqual(account.balance, [{"totalEq": "2000"}])
            finally:
                await asyncio.gather(market.stop(), account.stop(), algo.stop())

    async def test_rejected_subscriptions_are_not_reported_as_online(self):
        exchange = LocalExchange()
        exchange.reject_subscription = True
        async with serve(exchange.handler, "127.0.0.1", 0) as server:
            with patch.dict(os.environ, exchange.environment(server.sockets[0].getsockname()[1]), clear=True):
                stream = OkxMarketStream(["BTC-USDT-SWAP"])
            try:
                await stream.start()
                await wait_for(lambda: stream.last_error and stream.candles_last_error)
                self.assertEqual(stream.last_error, "OkxSubscriptionError")
                self.assertFalse(stream.connected or stream.candles_connected or stream.fresh)
            finally:
                await stream.stop()

    async def test_rejected_login_never_subscribes_or_authenticates(self):
        exchange = LocalExchange()
        exchange.reject_login = True
        async with serve(exchange.handler, "127.0.0.1", 0) as server:
            with patch.dict(os.environ, exchange.environment(server.sockets[0].getsockname()[1]), clear=True):
                account, algo = OkxAccountStream(), OkxAlgoOrderStream()
            try:
                await asyncio.gather(account.start(), algo.start())
                await wait_for(lambda: all(
                    stream.last_error == "OkxAuthenticationError" and not stream.connected
                    for stream in (account, algo)
                ))
                self.assertEqual(account.last_error, "OkxAuthenticationError")
                self.assertEqual(algo.last_error, "OkxAuthenticationError")
                self.assertFalse(account.authenticated or algo.authenticated)
                self.assertEqual(exchange.subscriptions, {})
            finally:
                await asyncio.gather(account.stop(), algo.stop())

    async def test_application_heartbeat_and_pong_are_not_data(self):
        received = []

        async def handler(socket):
            received.append(await socket.recv())
            await socket.send("pong")
            received.append(await socket.recv())
            await socket.send('{"data":"after-heartbeat"}')
            await socket.close()

        async with serve(handler, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            async with connect(url, proxy=None) as socket:
                rows = [row async for row in socket_messages(socket, idle_seconds=.02, response_seconds=.05)]
        self.assertEqual(received, ["ping", "ping"])
        self.assertEqual(rows, ['{"data":"after-heartbeat"}'])

    async def test_missing_heartbeat_reply_times_out(self):
        async def handler(socket):
            await socket.recv()
            await socket.wait_closed()

        async with serve(handler, "127.0.0.1", 0) as server:
            async with connect(f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}", proxy=None) as socket:
                with self.assertRaises(TimeoutError):
                    await anext(socket_messages(socket, idle_seconds=.02, response_seconds=.02))

    async def test_tls_policy_never_allows_plaintext_remote_endpoints(self):
        context = websocket_tls("wss://ws.okx.com/ws/v5/public")
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertIsNone(websocket_tls("ws://127.0.0.1:1234"))
        with self.assertRaises(ValueError):
            websocket_tls("ws://127.0.0.1:1234", "http://example.com:8080")
        for url in ("ws://example.com", "ws://127.0.0.1.example.com", "http://localhost", "wssx://example.com"):
            with self.assertRaises(ValueError):
                websocket_tls(url)


@contextmanager
def local_certificate():
    executable = shutil.which("openssl")
    if executable is None:
        raise unittest.SkipTest("OpenSSL CLI is required for local TLS transport tests")
    with tempfile.TemporaryDirectory() as directory:
        certificate = str(Path(directory) / "fixture-cert.pem")
        key = str(Path(directory) / "fixture-key.pem")
        subprocess.run([
            executable, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", key, "-out", certificate, "-days", "1", "-subj", "/CN=localhost",
            "-addext", "subjectAltName=IP:127.0.0.1,DNS:localhost",
        ], check=True, capture_output=True, timeout=10)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        yield context, certificate


@asynccontextmanager
async def local_proxy(kind, allowed_ports):
    """Bounded fixture proxy, never an open relay; all destinations are loopback."""
    requests, tasks, writers = [], set(), set()
    authorization = "Basic " + base64.b64encode(b"fixture-user:fixture-password").decode()

    async def copy(reader, writer):
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()

    async def handle(reader, writer):
        tasks.add(asyncio.current_task())
        writers.add(writer)
        remote = None
        pumps = []
        try:
            initial = b""
            if kind == "http":
                header = await reader.readuntil(b"\r\n\r\n")
                lines = header.decode("latin1").split("\r\n")
                method, target, version = lines[0].split(" ")
                fields = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
                auth = next((value for key, value in fields.items() if key.lower() == "proxy-authorization"), "")
                if auth != authorization:
                    writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                    await writer.drain()
                    return
                if method == "CONNECT":
                    host, port = target.rsplit(":", 1)
                    port = int(port)
                else:
                    parsed = urlsplit(target)
                    host, port = parsed.hostname, parsed.port
                    origin_target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
                    lines[0] = f"{method} {origin_target} {version}"
                    lines = [line for line in lines if not line.lower().startswith("proxy-authorization:")]
                    initial = "\r\n".join(lines).encode("latin1")
            else:
                version, count = await reader.readexactly(2)
                methods = await reader.readexactly(count)
                if version != 5 or 2 not in methods:
                    raise ValueError("SOCKS auth method required")
                writer.write(b"\x05\x02")
                await writer.drain()
                auth_version, length = await reader.readexactly(2)
                username = await reader.readexactly(length)
                password = await reader.readexactly((await reader.readexactly(1))[0])
                valid = auth_version == 1 and username == b"fixture-user" and password == b"fixture-password"
                writer.write(b"\x01\x00" if valid else b"\x01\x01")
                await writer.drain()
                if not valid:
                    return
                version, method, reserved, address_type = await reader.readexactly(4)
                if version != 5 or method != 1 or reserved != 0:
                    raise ValueError("Invalid SOCKS request")
                if address_type == 1:
                    host = ".".join(str(value) for value in await reader.readexactly(4))
                elif address_type == 3:
                    host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
                else:
                    raise ValueError("Unsupported fixture address")
                port = int.from_bytes(await reader.readexactly(2), "big")
            if host not in {"127.0.0.1", "localhost"} or port not in allowed_ports:
                raise ValueError("Fixture refuses non-test destination")
            upstream, remote = await asyncio.open_connection("127.0.0.1", port)
            writers.add(remote)
            requests.append((host, port))
            if kind != "http":
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            elif method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                remote.write(initial)
                await remote.drain()
            await writer.drain()
            pumps = [asyncio.create_task(copy(reader, remote)), asyncio.create_task(copy(upstream, writer))]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for endpoint in (remote, writer):
                if endpoint is not None:
                    endpoint.close()
                    with suppress(Exception):
                        await endpoint.wait_closed()
                    writers.discard(endpoint)
            tasks.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    scheme = "http" if kind == "http" else "socks5h"
    try:
        yield f"{scheme}://fixture-user:fixture-password@127.0.0.1:{port}", requests
    finally:
        server.close()
        await server.wait_closed()
        for writer in list(writers):
            writer.close()
        remaining = list(tasks)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)


class ProxyTransportTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        certificate_context = local_certificate()
        cls.server_context, cls.certificate = certificate_context.__enter__()
        cls.addClassCleanup(certificate_context.__exit__, None, None, None)

    async def proxy_roundtrip(self, kind, *, secure=True):
        exchange = LocalExchange()
        rest_requests = []

        async def rest(reader, writer):
            try:
                raw = await reader.readuntil(b"\r\n\r\n")
                lines = raw.decode("latin1").split("\r\n")
                method, path, _ = lines[0].split(" ")
                headers = {key.lower(): value.strip() for key, value in (line.split(":", 1) for line in lines[1:] if ":" in line)}
                body = await reader.readexactly(int(headers.get("content-length", "0")))
                if path != "/send":
                    expected = base64.b64encode(hmac.new(b"fixture-secret",
                        f"{headers['ok-access-timestamp']}{method}{path}".encode() + body, hashlib.sha256).digest()).decode()
                    valid = hmac.compare_digest(headers["ok-access-sign"], expected) and headers["x-simulated-trading"] == "1"
                else:
                    valid = json.loads(body)["token"] == "fixture-push-token"
                rest_requests.append({"method": method, "path": path, "signed": valid, "body": json.loads(body) if body else {}})
                payload = {"code": "0", "data": [{"ccy": "USDT", "cashBal": "1000"}]} if method == "GET" else {"code": "0", "data": [{"ordId": "fixture-order", "sCode": "0"}]}
                if path == "/send":
                    payload = {"code": 200, "msg": "fixture accepted"}
                encoded = json.dumps(payload).encode()
                writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(encoded)}\r\nConnection: close\r\n\r\n".encode() + encoded)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        with ExitStack() as stack:
            context = None
            if secure:
                context = self.server_context
                stack.enter_context(patch("app.okx_websocket.certifi.where", return_value=self.certificate))
                stack.enter_context(patch.dict(os.environ, {"SSL_CERT_FILE": self.certificate}))
            rest_server = await asyncio.start_server(rest, "127.0.0.1", 0, ssl=context)
            async with rest_server, serve(exchange.handler, "127.0.0.1", 0, ssl=context) as ws_server:
                rest_port = rest_server.sockets[0].getsockname()[1]
                ws_port = ws_server.sockets[0].getsockname()[1]
                scheme = "https" if secure else "http"
                async with local_proxy(kind, {ws_port, rest_port}) as (url, requests):
                    env = {
                        **exchange.environment(ws_port, url, secure=secure),
                        "OKX_REST_BASE_URL": f"{scheme}://127.0.0.1:{rest_port}",
                        "EXECUTION_ENABLED": "true", "TRADING_MODE": "demo", "LIVE_TRADING_ENABLED": "false",
                        "PUSHPLUS_BASE_URL": f"{scheme}://127.0.0.1:{rest_port}/send",
                        "PUSHPLUS_TOKEN": "fixture-push-token", "PUSHPLUS_PROXY_URL": url,
                    }
                    with patch.dict(os.environ, env, clear=True):
                        market, account, algo = OkxMarketStream(["BTC-USDT-SWAP"]), OkxAccountStream(), OkxAlgoOrderStream()
                        account_rest, trade, push = OkxAccountClient(), OkxTradeClient(), PushPlusClient()
                    try:
                        await asyncio.gather(market.start(), account.start(), algo.start())
                        await wait_for(lambda: market.fresh and market.candles_fresh and account.fills and algo.orders)
                        self.assertEqual((await account_rest.balance())[0]["cashBal"], "1000")
                        result = await trade.place_order(OrderRequest(
                            inst_id="BTC-USDT-SWAP", side="buy", sz=1, stop_loss=95, take_profit=110,
                        ))
                        self.assertEqual(result["data"][0]["ordId"], "fixture-order")
                        self.assertEqual((await push.send("Fixture", "Local notification"))["code"], 200)
                        self.assertEqual(len(requests), 7)
                        self.assertTrue(all(item["signed"] for item in rest_requests))
                        order = next(item for item in rest_requests if item["path"] == "/api/v5/trade/order")
                        self.assertEqual(order["body"]["attachAlgoOrds"][0]["slTriggerPx"], "95")
                        self.assertEqual(order["body"]["attachAlgoOrds"][0]["tpTriggerPx"], "110")
                        self.assertFalse(trade.live_gate.allowed)
                        self.assertEqual(exchange.errors, [])
                    finally:
                        await asyncio.gather(market.stop(), account.stop(), algo.stop())

    async def test_authenticated_http_proxy_rest_websockets_and_notification(self):
        await self.proxy_roundtrip("http")

    async def test_authenticated_socks5_proxy_rest_websockets_and_notification(self):
        await self.proxy_roundtrip("socks5")

    async def test_plaintext_loopback_http_proxy_uses_origin_form_forwarding(self):
        await self.proxy_roundtrip("http", secure=False)

    async def test_untrusted_certificate_and_wrong_hostname_are_rejected(self):
        async def handler(socket):
            await socket.wait_closed()

        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        handshake_errors = []
        loop.set_exception_handler(lambda _loop, context: handshake_errors.append(context))
        try:
            async with serve(handler, "127.0.0.1", 0, ssl=self.server_context) as server:
                url = f"wss://127.0.0.1:{server.sockets[0].getsockname()[1]}"
                with self.assertRaises(ssl.SSLCertVerificationError):
                    async with connect(url, proxy=None, ssl=websocket_tls(url)):
                        self.fail("Untrusted certificate accepted")
                trusted = ssl.create_default_context(cafile=self.certificate)
                with self.assertRaises(ssl.SSLCertVerificationError):
                    async with connect(url, proxy=None, ssl=trusted, server_hostname="wrong.example"):
                        self.fail("Wrong hostname accepted")
                await asyncio.sleep(.02)
        finally:
            loop.set_exception_handler(previous_handler)
        self.assertTrue(all(
            item.get("message") == "Error on transport creation for incoming connection"
            and isinstance(item.get("exception"), (ConnectionResetError, ssl.SSLError))
            for item in handshake_errors
        ))

    async def test_proxy_authentication_failure_fails_closed(self):
        for kind in ("http", "socks5"):
            with self.subTest(proxy=kind):
                exchange = LocalExchange()
                direct_requests = []

                async def rest(reader, writer):
                    try:
                        raw = await reader.readuntil(b"\r\n\r\n")
                        direct_requests.append(raw.split(b"\r\n", 1)[0])
                        payload = b'{"code":"0","data":[{"instId":"BTC-USDT-SWAP","last":"100"}]}'
                        writer.write(
                            f"HTTP/1.1 200 OK\r\nContent-Length: {len(payload)}\r\n"
                            "Connection: close\r\n\r\n".encode() + payload
                        )
                        await writer.drain()
                    finally:
                        writer.close()
                        await writer.wait_closed()

                rest_server = await asyncio.start_server(rest, "127.0.0.1", 0)
                async with rest_server, serve(exchange.handler, "127.0.0.1", 0) as ws_server:
                    rest_port = rest_server.sockets[0].getsockname()[1]
                    ws_port = ws_server.sockets[0].getsockname()[1]
                    base = f"http://127.0.0.1:{rest_port}"
                    async with httpx.AsyncClient(trust_env=False) as direct:
                        self.assertEqual((await direct.get(base)).status_code, 200)
                    direct_requests.clear()
                    async with local_proxy(kind, {rest_port, ws_port}) as (url, requests):
                        env = {
                            **exchange.environment(ws_port, url.replace("fixture-password", "wrong")),
                            "OKX_REST_BASE_URL": base,
                            "EXECUTION_ENABLED": "true", "TRADING_MODE": "demo",
                            "LIVE_TRADING_ENABLED": "false",
                        }
                        with patch.dict(os.environ, env, clear=True):
                            stream = OkxMarketStream(["BTC-USDT-SWAP"])
                            account, algo = OkxAccountStream(), OkxAlgoOrderStream()
                            market_rest = OkxMarketClient()
                            account_rest, trade = OkxAccountClient(), OkxTradeClient()
                        opened = []
                        original_connect = AutoBackend.connect_tcp

                        async def track_connect(backend, *args, **kwargs):
                            connection = await original_connect(backend, *args, **kwargs)
                            opened.append((connection, connection.get_extra_info("socket")))
                            return connection

                        tracker = patch.object(AutoBackend, "connect_tcp", track_connect)
                        tracker.start()
                        try:
                            await asyncio.gather(stream.start(), account.start(), algo.start())
                            operations = (
                                (lambda: market_rest.ticker("BTC-USDT-SWAP"), OkxMarketError),
                                (account_rest.balance, OkxAccountError),
                                (lambda: trade.place_order(OrderRequest(
                                    inst_id="BTC-USDT-SWAP", side="buy", sz=1,
                                    stop_loss=95, take_profit=110,
                                )), OkxTradeError),
                            )
                            for operation, error_type in operations:
                                with self.assertRaises(error_type) as raised:
                                    await operation()
                                self.assertNotIn("fixture-user", str(raised.exception))
                                self.assertNotIn("wrong", str(raised.exception))
                            await wait_for(lambda: all((
                                stream.last_error, stream.candles_last_error,
                                account.last_error, algo.last_error,
                            )))
                            self.assertFalse(any((
                                stream.connected, stream.candles_connected, stream.fresh,
                                account.authenticated, algo.authenticated,
                            )))
                            self.assertEqual(requests, [])
                            self.assertEqual(direct_requests, [])
                            self.assertEqual(exchange.connection_count, {})
                            self.assertEqual(len(opened), 3)
                            self.assertTrue(all(sock.fileno() == -1 for _, sock in opened))
                            for client in (stream, account, algo):
                                self.assertNotIn("wrong", json.dumps(client.snapshot()))
                                self.assertNotIn("fixture-user", json.dumps(client.snapshot()))
                        finally:
                            tracker.stop()
                            await asyncio.gather(*(connection.aclose() for connection, _ in opened))
                            await asyncio.gather(stream.stop(), account.stop(), algo.stop())
