import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

from .position_lots import positions_with_lots
from .protection_handoff import handoff_summaries
from .protection_adjustment import adjustment_summaries
from .protection_incident import incident_summaries


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


async def private_events(
    store, account, account_client, authorized, rate_scope=lambda: None, connection_check=None,
) -> AsyncIterator[str]:
    def account_state() -> tuple[bool, bool, bool]:
        connected = bool(account.connected)
        authenticated = bool(account.authenticated)
        ready = bool(getattr(account, "account_ready", connected and authenticated))
        return connected, authenticated, ready

    revision = None
    previous = {}
    heartbeat_at = float("-inf")
    while authorized():
        scope, market_scope = account_client.account_scope, rate_scope()
        stream_state = account_state()
        current_revision = (store.revision, scope, market_scope, stream_state)
        if revision is not None and revision[3] != stream_state:
            # Browsers clear live account values on disconnect. Re-send the
            # ledger after recovery even when its stored content is unchanged.
            previous = {}
        payloads = {
            "account": {
                "configured": account.configured,
                "connected": account.connected,
                "authenticated": account.authenticated,
                "ready": all(stream_state),
                "balance": account.balance if all(stream_state) else [],
                "last_message_at": account.last_message_at,
            },
        }
        if connection_check is not None:
            payloads["connection_check"] = {"data": connection_check()}
        if current_revision != revision:
            # No exchange requests here. Reading the durable ledger cannot place
            # an order, trigger reconciliation, or multiply REST traffic per tab.
            def read_state():
                return {
                    "positions": {"data": positions_with_lots(store, account_scope=scope)},
                    "protection_handoffs": {"data": handoff_summaries(store, scope)},
                    "protection_adjustments": {"data": adjustment_summaries(store, scope)},
                    "protection_incidents": {"data": incident_summaries(store, scope)},
                    "orders": {"data": store.list_orders()},
                    "fills": {"data": store.list_fills()},
                    "activity": {"data": store.list_audit()},
                    "tradingview_alerts": {"data": store.list_tradingview_alerts()},
                    "chart_annotations": {"data": store.list_chart_annotations(scope)},
                    "bills": {
                        "configured": account_client.configured,
                        **store.bill_snapshot(account_client.account_scope),
                    },
                    "bill_import": {"job": store.bill_import(account_client.account_scope)},
                    "bill_archives": {"data": store.bill_archives(account_client.account_scope)},
                    "bill_valuation": {"job": store.bill_valuation(account_client.account_scope)},
                    "equity_baseline": {"latest": store.equity_baseline(account_client.account_scope)},
                    "account_performance": store.performance_updates(scope, market_scope),
                    "strategies": {"data": store.list_strategies()},
                    "analyses": {"data": store.analysis_index(limit=1)},
                }
            payloads.update(await asyncio.to_thread(read_state))
            if (scope != account_client.account_scope or market_scope != rate_scope()
                    or stream_state != account_state()):
                continue
            revision = current_revision
        if not authorized():
            break
        for name, payload in payloads.items():
            if not authorized():
                yield event_frame("locked", {})
                return
            if stream_state != account_state():
                revision = None
                previous = {}
                break
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
