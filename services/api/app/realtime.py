import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any


def event_frame(event: str, payload: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"


async def control_events(
    status: Callable[[], dict[str, Any]],
    analysis: Callable[[], dict[str, Any]],
) -> AsyncIterator[str]:
    previous = {}
    heartbeat_at = float("-inf")
    while True:
        payloads = await asyncio.to_thread(lambda: (("status", status()), ("analysis_status", analysis())))
        for name, payload in payloads:
            # Market timestamps have their own feed; don't redraw controls per tick.
            comparable = json.loads(json.dumps(payload))
            for section in ("market_stream", "account_stream", "algo_stream"):
                if section in comparable:
                    comparable[section].pop("last_message_at", None)
                    comparable[section].pop("candles_last_message_at", None)
            encoded = json.dumps(comparable, sort_keys=True)
            if encoded != previous.get(name):
                yield event_frame(name, payload)
                previous[name] = encoded
        if time.monotonic() - heartbeat_at >= 5:
            yield event_frame("heartbeat", {})
            heartbeat_at = time.monotonic()
        await asyncio.sleep(.25)


async def private_events(store, account, account_client, authorized) -> AsyncIterator[str]:
    revision = None
    previous = {}
    heartbeat_at = float("-inf")
    while authorized():
        current_revision = store.revision
        payloads = {
            "account": {
                "configured": account.configured,
                "connected": account.connected,
                "authenticated": account.authenticated,
                "balance": account.balance if account.connected and account.authenticated else [],
                "last_message_at": account.last_message_at,
            },
        }
        if current_revision != revision:
            # No exchange requests here. Reading the durable ledger cannot place
            # an order, trigger reconciliation, or multiply REST traffic per tab.
            def read_state():
                return {
                    "positions": {"data": store.list_positions()},
                    "orders": {"data": store.list_orders()},
                    "fills": {"data": store.list_fills()},
                    "activity": {"data": store.list_audit()},
                    "bills": {
                        "configured": account_client.configured,
                        **store.bill_snapshot(account_client.account_scope),
                    },
                    "bill_import": {"job": store.bill_import(account_client.account_scope)},
                    "bill_archives": {"data": store.bill_archives(account_client.account_scope)},
                    "bill_valuation": {"job": store.bill_valuation(account_client.account_scope)},
                    "strategies": {"data": store.list_strategies()},
                    "analyses": {"data": store.analysis_index(limit=1)},
                }
            payloads.update(await asyncio.to_thread(read_state))
            revision = current_revision
        if not authorized():
            break
        for name, payload in payloads.items():
            if not authorized():
                yield event_frame("locked", {})
                return
            comparable = {key: value for key, value in payload.items() if key != "last_message_at"}
            encoded = json.dumps(comparable, sort_keys=True)
            if encoded != previous.get(name):
                yield event_frame(name, payload)
                previous[name] = encoded
        if time.monotonic() - heartbeat_at >= 5:
            yield event_frame("heartbeat", {})
            heartbeat_at = time.monotonic()
        await asyncio.sleep(.25)
    yield event_frame("locked", {})
