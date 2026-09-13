"""Local protocol fixture, never a live OKX connection or matching engine."""

import asyncio
import base64
import copy
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

SYMBOL = "BTC-USDT-SWAP"
API_KEY = "local-fixture-key"
SECRET = "local-fixture-secret"
PASSPHRASE = "local-fixture-passphrase"
PUSH_TOKEN = "local-fixture-push"


def milliseconds():
    return str(int(time.time() * 1000))


class ExchangeServer:
    def __init__(self):
        self.lock = threading.RLock()
        self.orders = {}
        self.algos = {}
        self.fills = []
        self.bills = []
        self.archive_bills = []
        self.archive_fail_after = False
        self.index_rates = {"USDT-USD": "0.98", "BTC-USD": "50000", "ETH-USD": "2000"}
        self.position = None
        self.posts = []
        self.gets = []
        self.notifications = []
        self.errors = []
        self.fail_paths = set()
        self.subscriptions = {}
        self.private_paused = True
        self.hide_order_history = False
        self.hide_algo_history = False
        self.autofill = False
        self.hold_order_response = False
        self.release_order_response = threading.Event()
        self.blocked_order_received = threading.Event()
        self.revision = 0
        self.http = None
        self.ws = None
        self.thread = None
        latest = int(time.time() // 900) * 900000
        self.candles = []
        for index in range(100):
            price = round(100 + index * .08 + (.15 if index % 2 == 0 else -.15), 2)
            self.candles.append([
                str(latest - (99 - index) * 900000), str(price),
                str(round(price + .3, 2)), str(round(price - .3, 2)),
                str(price), "10", ".1", str(price * .1), "1",
            ])
        self.candles.reverse()
        self.price = self.candles[0][4]
        self.mark_price = None
        self.mark_timestamp = None

    def instrument(self):
        return {
            "instId": SYMBOL, "instType": "SWAP", "state": "live",
            "ctType": "linear", "ctVal": ".01", "ctMult": "1",
            "ctValCcy": "BTC", "settleCcy": "USDT", "lotSz": "1",
            "minSz": "1", "tickSz": ".01", "maxMktSz": "10000",
        }

    def balance(self):
        return {"totalEq": "1000", "details": [{"ccy": "USDT", "eq": "1000", "eqUsd": "1000"}]}

    def ticker(self):
        return {
            "instId": SYMBOL, "last": self.price, "ts": milliseconds(),
            "bidPx": self.price, "askPx": str(float(self.price) + .01),
            "open24h": "100", "high24h": "110", "low24h": "99",
        }

    def _private_valid(self, method, path, headers, body):
        timestamp = headers.get("OK-ACCESS-TIMESTAMP", "")
        expected = base64.b64encode(hmac.new(
            SECRET.encode(), f"{timestamp}{method}{path}".encode() + body,
            hashlib.sha256,
        ).digest()).decode()
        return (
            headers.get("OK-ACCESS-KEY") == API_KEY
            and headers.get("OK-ACCESS-PASSPHRASE") == PASSPHRASE
            and hmac.compare_digest(headers.get("OK-ACCESS-SIGN", ""), expected)
            and headers.get("x-simulated-trading") == "1"
        )

    def request(self, method, target, headers, raw):
        parsed = urlsplit(target)
        path, query = parsed.path, parse_qs(parsed.query)
        body = json.loads(raw) if raw else {}
        public = path.startswith(("/api/v5/public/", "/api/v5/market/"))
        with self.lock:
            if path == "/send":
                if body.get("token") != PUSH_TOKEN:
                    self.errors.append("push_token_invalid")
                    return {"code": 401, "msg": "fixture token rejected"}
                self.notifications.append(body)
                return {"code": 200, "msg": "local sink accepted"}
            if not public and not self._private_valid(method, target, headers, raw):
                self.errors.append("signature_or_demo_header_invalid")
                return {"code": "50113", "msg": "fixture signature rejected", "data": []}
            if public and headers.get("x-simulated-trading") != "1":
                self.errors.append("public_demo_header_missing")
            if method == "GET":
                self.gets.append({"path": path, "query": query})
                if path in self.fail_paths:
                    return {"code": "50011", "msg": "intentional fixture outage", "data": []}
                if path == "/api/v5/account/bills-archive" and self.archive_fail_after and query.get("after"):
                    return {"code": "50011", "msg": "intentional archive page outage", "data": []}
                data = self._get(path, query)
                if data is None:
                    return {"code": "51603", "msg": "fixture order missing", "data": []}
            else:
                self.posts.append({"path": path, "body": body})
                data = self._post(path, body)
            return {"code": "0", "data": copy.deepcopy(data)}

    def _get(self, path, query):
        if path == "/api/v5/public/instruments":
            return [self.instrument()]
        if path == "/api/v5/market/ticker":
            return [self.ticker()]
        if path == "/api/v5/public/mark-price":
            if query != {"instType": ["SWAP"], "instId": [SYMBOL]}:
                self.errors.append("mark_price_query_invalid")
                return []
            return [{
                "instId": SYMBOL, "instType": "SWAP",
                "markPx": self.mark_price if self.mark_price is not None else self.price,
                "ts": self.mark_timestamp if self.mark_timestamp is not None else milliseconds(),
            }]
        if path == "/api/v5/market/candles":
            return self.candles[:int(query.get("limit", ["100"])[0])]
        if path == "/api/v5/market/history-index-candles":
            rate = self.index_rates.get(query.get("instId", [""])[0])
            timestamp = int(query["after"][0]) - 60000
            return [[str(timestamp), rate, rate, rate, rate, "1"]] if rate else []
        if path == "/api/v5/public/funding-rate":
            return [{"instId": SYMBOL, "fundingRate": ".0001"}]
        if path == "/api/v5/public/open-interest":
            return [{"instId": SYMBOL, "oi": "1000", "oiCcy": "10"}]
        if path == "/api/v5/account/balance":
            return [self.balance()]
        if path == "/api/v5/account/config":
            return [{"posMode": "net_mode", "acctLv": "2"}]
        if path == "/api/v5/account/positions":
            return [self.position] if self.position and float(self.position["pos"]) else []
        if path == "/api/v5/account/bills":
            end = int(query.get("end", [milliseconds()])[0])
            return [bill for bill in reversed(self.bills) if int(bill["ts"]) <= end]
        if path == "/api/v5/account/bills-archive":
            begin, end = int(query["begin"][0]), int(query["end"][0])
            rows = sorted([bill for bill in self.archive_bills if begin <= int(bill["ts"]) <= end],
                          key=lambda bill: int(bill["billId"]), reverse=True)
            after = query.get("after", [None])[0]
            if after is not None:
                rows = [bill for bill in rows if int(bill["billId"]) < int(after)]
            return rows[:int(query.get("limit", ["100"])[0])]
        if path == "/api/v5/trade/orders-pending":
            return [order for order in self.orders.values() if order["state"] in {"live", "partially_filled"}]
        if path == "/api/v5/trade/orders-history-archive":
            return [] if self.hide_order_history else [
                order for order in self.orders.values() if order["state"] in {"filled", "canceled"}
            ]
        if path == "/api/v5/trade/fills-history":
            after = query.get("after", [None])[0]
            instrument = query.get("instId", [None])[0]
            rows = [row for row in reversed(self.fills) if (
                (after is None or int(row["billId"]) < int(after))
                and (instrument is None or row["instId"] == instrument)
            )]
            return rows[:int(query.get("limit", ["100"])[0])]
        if path == "/api/v5/trade/orders-algo-pending":
            types = query.get("ordType", [""])[0].split(",")
            return [algo for algo in self.algos.values() if algo["state"] == "live" and algo["ordType"] in types]
        if path == "/api/v5/trade/orders-algo-history":
            return [] if self.hide_algo_history else [
                algo for algo in self.algos.values() if algo["state"] == query.get("state", [""])[0]
            ]
        if path == "/api/v5/trade/order-algo":
            algo_id = query.get("algoId", [None])[0]
            client_id = query.get("algoClOrdId", [None])[0]
            algo = next((row for row in self.algos.values() if
                (algo_id and row["algoId"] == algo_id) or (client_id and row["algoClOrdId"] == client_id)), None)
            return [algo] if algo else None
        if path == "/api/v5/trade/order":
            order_id = query.get("ordId", [None])[0]
            client_id = query.get("clOrdId", [None])[0]
            order = next((row for row in self.orders.values() if
                (order_id and row["ordId"] == order_id) or (client_id and row["clOrdId"] == client_id)), None)
            return [order] if order else None
        raise AssertionError(f"Unexpected fixture GET {path}")

    def _post(self, path, body):
        if path == "/api/v5/account/set-leverage":
            return [body]
        if path == "/api/v5/trade/order":
            existing = next((row for row in self.orders.values() if row["clOrdId"] == body.get("clOrdId")), None)
            if existing:
                self.errors.append("duplicate_exchange_submission")
                return [{"sCode": "51000", "sMsg": "duplicate fixture order"}]
            order = self._create_order(body)
            if self.autofill:
                self._fill(order["ordId"])
            self.blocked_order_received.set()
            return [{"ordId": order["ordId"], "clOrdId": order["clOrdId"], "sCode": "0"}]
        if path == "/api/v5/trade/cancel-order":
            order = self.orders[body["ordId"]]
            order.update(state="canceled", uTime=milliseconds())
            self.revision += 1
            return [{"ordId": order["ordId"], "sCode": "0"}]
        if path == "/api/v5/trade/cancel-algos":
            algo = self.algos[body[0]["algoId"]]
            algo.update(state="canceled", uTime=milliseconds())
            self.revision += 1
            return [{"algoId": algo["algoId"], "sCode": "0"}]
        raise AssertionError(f"Unexpected fixture POST {path}")

    def _create_order(self, body):
        order_id = str(9000 + len(self.orders))
        now = milliseconds()
        order = {
            **body, "ordId": order_id, "clOrdId": body.get("clOrdId", ""),
            "state": "live", "cTime": now, "uTime": now, "instType": "SWAP",
            "px": body.get("px", ""), "accFillSz": "0", "reduceOnly": body.get("reduceOnly", False),
        }
        self.orders[order_id] = order
        self.revision += 1
        return order

    def _fill(self, order_id, quantity=None):
        order = self.orders[order_id]
        if order["state"] == "filled":
            return
        requested, filled = float(order["sz"]), float(order["accFillSz"])
        now, quantity = milliseconds(), float(quantity) if quantity is not None else requested - filled
        if not 0 < quantity <= requested - filled:
            raise AssertionError("Fixture fill exceeds remaining order quantity")
        cumulative = filled + quantity
        previous = float(self.position["pos"]) if self.position else 0
        change = quantity if order["side"] == "buy" else -quantity
        if order["reduceOnly"] and (previous == 0 or abs(change) > abs(previous) or change * previous >= 0):
            raise AssertionError("Fixture refused a non-reducing close")
        size = previous + change
        order.update(
            state="filled" if cumulative == requested else "partially_filled",
            accFillSz=str(cumulative), fillSz=str(quantity),
            avgPx=self.price, fillPx=self.price, tradeId=str(8000 + len(self.fills)),
            fillTime=now, uTime=now, fillFee="-.001", fillFeeCcy="USDT", fillPnl="0",
        )
        fill = {**order, "billId": str(6000 + len(self.fills)), "fee": "-.001", "feeCcy": "USDT", "ts": now}
        fill["subType"] = ("5" if order["side"] == "sell" else "6") if order["reduceOnly"] else (
            "3" if order["side"] == "buy" else "4"
        )
        self.fills.append(fill)
        self.bills.append({
            "billId": fill["billId"], "type": "2", "subType": "1",
            "instId": SYMBOL, "instType": "SWAP", "ccy": "USDT", "pnl": "0", "fee": "-.001", "ts": now,
        })
        self.position = {
            "instId": SYMBOL, "posSide": "net", "mgnMode": order["tdMode"],
            "pos": str(size), "availPos": str(abs(size)), "avgPx": self.price,
            "markPx": self.price, "notionalUsd": str(abs(size) * .01 * float(self.price)),
            "upl": "0", "uTime": now,
            "tradeId": order["tradeId"],
        }
        for attached in order.get("attachAlgoOrds", []) if order["state"] == "filled" else []:
            algo_id = str(7000 + len(self.algos))
            self.algos[algo_id] = {
                **attached, "algoId": algo_id, "algoClOrdId": attached["attachAlgoClOrdId"],
                "instId": SYMBOL, "ordType": "oco", "state": "live",
                "posSide": "net", "tdMode": order["tdMode"], "side": "sell" if size > 0 else "buy",
                "sz": order["sz"], "reduceOnly": True, "cTime": now, "uTime": now,
            }
        self.revision += 1

    def fill(self, order_id, quantity=None):
        with self.lock:
            self._fill(order_id, quantity=quantity)

    def trigger_protection(self, algo_id):
        with self.lock:
            algo = self.algos[algo_id]
            self.price = algo["slTriggerPx"]
            close = self._create_order({
                "instId": SYMBOL, "side": algo["side"], "posSide": "net",
                "tdMode": algo["tdMode"], "ordType": "market", "sz": algo["sz"], "reduceOnly": True,
            })
            self._fill(close["ordId"])
            algo.update(
                state="effective", actualSz=algo["sz"], actualPx=self.price,
                uTime=milliseconds(), ordIdList=[close["ordId"]],
            )
            self.revision += 1

    @property
    def order_submissions(self):
        with self.lock:
            return copy.deepcopy([item["body"] for item in self.posts if item["path"] == "/api/v5/trade/order"])

    async def socket_handler(self, socket):
        path = socket.request.path
        publisher = None
        try:
            if path in {"/private", "/algo"}:
                login = json.loads(await socket.recv())["args"][0]
                expected = base64.b64encode(hmac.new(
                    SECRET.encode(), f"{login['timestamp']}GET/users/self/verify".encode(), hashlib.sha256,
                ).digest()).decode()
                if not (login["apiKey"] == API_KEY and login["passphrase"] == PASSPHRASE and
                        hmac.compare_digest(login["sign"], expected)):
                    raise AssertionError("Invalid fixture websocket login")
                await socket.send(json.dumps({"event": "login", "code": "0"}))
            args = json.loads(await socket.recv())["args"]
            self.subscriptions[path] = {item["channel"] for item in args}
            expected = {
                "/public": {"tickers", "books5", "trades", "open-interest", "funding-rate"}, "/candles": {"candle1m", "candle15m", "candle1H", "candle4H"},
                "/private": {"account", "orders", "positions"}, "/algo": {"orders-algo"},
            }[path]
            if self.subscriptions[path] != expected:
                raise AssertionError(f"Unexpected subscriptions {path}")
            for arg in args:
                await socket.send(json.dumps({"event": "subscribe", "arg": arg}))

            async def publish():
                revision = -1
                while True:
                    with self.lock:
                        changed = revision != self.revision
                        revision = self.revision
                        messages = []
                        for arg in args:
                            channel = arg["channel"]
                            if channel == "tickers":
                                data = [self.ticker()]
                            elif channel == "books5":
                                data = [{
                                    "asks": [["101.01", "1", "0", "1"], ["101.02", "2", "0", "1"]],
                                    "bids": [["100.99", "1.5", "0", "1"], ["100.98", "2.5", "0", "1"]],
                                    "ts": milliseconds(),
                                }]
                            elif channel == "trades":
                                data = [{
                                    "tradeId": str(self.revision),
                                    "px": self.price,
                                    "sz": "1",
                                    "side": "buy" if self.revision % 2 == 0 else "sell",
                                    "ts": milliseconds(),
                                }]
                            elif channel == "open-interest":
                                data = [{"instId": SYMBOL, "oi": "100"}]
                            elif channel == "funding-rate":
                                data = [{"instId": SYMBOL, "fundingRate": "0.0001"}]
                            elif channel in {"candle1m", "candle15m", "candle1H", "candle4H"}:
                                data = self.candles[:1]
                            elif channel == "account":
                                data = [self.balance()]
                            elif self.private_paused or not changed:
                                continue
                            elif channel == "positions":
                                data = [self.position] if self.position else []
                            elif channel == "orders":
                                data = list(self.orders.values())
                            else:
                                data = list(self.algos.values())
                            messages.append(json.dumps({"arg": arg, "data": data}))
                    for message in messages:
                        await socket.send(message)
                    await asyncio.sleep(.1)

            publisher = asyncio.create_task(publish())
            async for raw in socket:
                if raw == "ping":
                    await socket.send("pong")
        except ConnectionClosed:
            pass
        except Exception as exc:
            self.errors.append(f"ws:{type(exc).__name__}:{exc}")
        finally:
            if publisher:
                publisher.cancel()
                await asyncio.gather(publisher, return_exceptions=True)

    async def start(self):
        exchange = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_):
                pass

            def handle_request(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length > 1048576:
                        raise ValueError("Fixture request too large")
                    raw = self.rfile.read(length)
                    result = exchange.request(self.command, self.path, self.headers, raw)
                    if self.command == "POST" and self.path == "/api/v5/trade/order" and exchange.hold_order_response:
                        exchange.release_order_response.wait(20)
                    encoded = json.dumps(result).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(encoded)
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    exchange.errors.append(f"http:{type(exc).__name__}:{exc}")
                    self.send_error(500)
                finally:
                    self.close_connection = True

            do_GET = handle_request
            do_POST = handle_request

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.ws = await serve(self.socket_handler, "127.0.0.1", 0)
        return self

    def environment(self):
        rest = f"http://127.0.0.1:{self.http.server_port}"
        websocket = f"ws://127.0.0.1:{self.ws.sockets[0].getsockname()[1]}"
        return {
            "OKX_REST_BASE_URL": rest, "OKX_WS_PUBLIC_URL": f"{websocket}/public",
            "OKX_WS_CANDLES_URL": f"{websocket}/candles", "OKX_WS_PRIVATE_URL": f"{websocket}/private",
            "OKX_WS_BUSINESS_URL": f"{websocket}/algo", "OKX_API_KEY": API_KEY,
            "OKX_SECRET_KEY": SECRET, "OKX_PASSPHRASE": PASSPHRASE,
            "PUSHPLUS_BASE_URL": f"{rest}/send", "PUSHPLUS_TOKEN": PUSH_TOKEN,
        }

    async def close(self):
        self.release_order_response.set()
        if self.ws:
            self.ws.close()
            await self.ws.wait_closed()
        if self.http:
            await asyncio.to_thread(self.http.shutdown)
            self.http.server_close()
            await asyncio.to_thread(self.thread.join, 5)
