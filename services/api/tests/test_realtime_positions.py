import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.account_sync import AccountSynchronizer
from app.okx_account_stream import OkxAccountStream
from app.state_store import StateStore


def position(size="1", updated="100"):
    return {
        "instId": "BTC-USDT-SWAP", "posSide": "net", "mgnMode": "isolated",
        "pos": size, "avgPx": "100", "markPx": "101", "notionalUsd": "101",
        "upl": "1", "uTime": updated,
    }


class Account:
    configured = True
    account_scope = "realtime-position-fixture"

    def __init__(self):
        self.rows = []
        self.started = asyncio.Event()
        self.release = None

    async def positions(self):
        rows = list(self.rows)
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        return rows

    async def pending_orders(self):
        return []

    async def fills_history(self):
        return []


class RealtimePositionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(str(Path(self.directory.name) / "state.sqlite3"))
        self.account = Account()
        self.stream = OkxAccountStream()
        self.stream.api_key = self.stream.secret_key = self.stream.passphrase = "fixture-only"
        self.stream.connected = self.stream.authenticated = True
        self.sync = AccountSynchronizer(self.store, self.account, self.stream)

    def push(self, row):
        self.stream.consume(json.dumps({"arg": {"channel": "positions"}, "data": [row]}))
        return self.sync.sync_stream()

    async def begin_slow_rest(self):
        self.account.release = asyncio.Event()
        task = asyncio.create_task(self.sync.sync_rest())
        await asyncio.wait_for(self.account.started.wait(), 1)
        return task

    async def test_unchanged_stream_cache_cannot_reopen_rest_closed_position(self):
        self.push(position())
        await self.sync.sync_rest()
        self.assertEqual(self.store.list_positions(), [])
        self.sync.sync_stream()
        self.assertEqual(self.store.list_positions(), [])

    async def test_new_push_is_not_replaced_by_slow_rest_response(self):
        self.push(position())
        self.account.rows = [position()]
        task = await self.begin_slow_rest()
        self.push(position("2", "200"))
        self.account.release.set()
        await task
        self.assertEqual(self.store.list_positions()[0]["size"], 2)

    async def test_position_opened_during_rest_is_not_closed_as_missing(self):
        task = await self.begin_slow_rest()
        self.push(position())
        self.account.release.set()
        await task
        self.assertEqual(len(self.store.list_positions()), 1)

    async def test_newer_exchange_version_in_rest_can_supersede_inflight_push(self):
        self.push(position())
        self.account.rows = [position("3", "300")]
        task = await self.begin_slow_rest()
        self.push(position("2", "200"))
        self.account.release.set()
        await task
        self.assertEqual(self.store.list_positions()[0]["size"], 3)

    async def test_position_closed_during_rest_is_not_reopened(self):
        self.push(position())
        self.account.rows = [position()]
        task = await self.begin_slow_rest()
        self.push(position("0", "200"))
        self.account.release.set()
        await task
        self.assertEqual(self.store.list_positions(), [])

    async def test_older_exchange_version_is_not_applied_after_newer_rest(self):
        self.push(position())
        self.account.rows = [position("2", "200")]
        await self.sync.sync_rest()
        self.push(position("1", "100"))
        self.assertEqual(self.store.list_positions()[0]["size"], 2)

    async def test_new_identical_push_is_not_confused_with_replayed_cache(self):
        self.push(position())
        await self.sync.sync_rest()
        self.assertEqual(self.store.list_positions(), [])
        self.sync.sync_stream()
        self.assertEqual(self.store.list_positions(), [])
        self.push(position())
        self.assertEqual(len(self.store.list_positions()), 1)

    async def test_protection_refresh_uses_current_position_not_old_stream_size(self):
        self.push(position())
        self.account.rows = [position("2", "200")]
        await self.sync.sync_rest()
        with patch.object(self.sync, "_local_protection", return_value=(90, 110, "fixture-entry")):
            self.sync.sync_stream()
        current = self.store.list_positions()[0]
        self.assertEqual(current["size"], 2)
        self.assertEqual((current["stop_loss"], current["take_profit"]), (90, 110))
