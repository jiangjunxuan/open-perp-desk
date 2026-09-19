import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .equity_performance import flow_rates, observation_bounds, prepare_interval, value_interval
from .historical_ledger import archive_first_day
from .historical_valuation import HistoricalValuationError
from .okx_account import OkxAccountError
from .okx_market import OkxMarketError
from .state_store import PerformanceLeaseLost


class AccountPerformanceWorker:
    """Read-only interval evidence, atomically published under a renewable lease."""

    def __init__(self, store, account, market, *, clock=None):
        self.store, self.account, self.market = store, account, market
        self.clock = clock or (lambda: int(time.time() * 1000))
        self._task = None
        self._wake = asyncio.Event()
        self._scheduled_at = 0
        self._scope = None

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="openperpdesk-account-performance")

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def notify(self):
        self._wake.set()

    async def _run(self):
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logging.getLogger(__name__).error("Performance worker failed: %s", type(exc).__name__)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 1)
            except TimeoutError:
                pass

    async def run_once(self):
        if not self.account.configured:
            return
        now_ms = self.clock()
        scope, market_scope = self.account.account_scope, self.market.rate_scope
        now = datetime.fromtimestamp(now_ms / 1000, timezone.utc)
        if now_ms - self._scheduled_at >= 30_000 or self._scope != (scope, market_scope):
            first = archive_first_day(now)
            days = [first + timedelta(days=offset) for offset in range((now.date() - first).days)]
            await asyncio.to_thread(self.store.schedule_performance, scope, market_scope, days, now_ms)
            self._scheduled_at, self._scope = now_ms, (scope, market_scope)
        owner = uuid4().hex
        job = await asyncio.to_thread(self.store.claim_performance, scope, market_scope, owner, now_ms)
        if job is None:
            return

        async def heartbeat():
            if not self.account.configured or scope != self.account.account_scope or market_scope != self.market.rate_scope:
                raise PerformanceLeaseLost("performance_account_or_market_changed")
            await asyncio.to_thread(self.store.touch_performance, job["id"], scope, owner, self.clock())

        try:
            if len(job["baselines"]) != 2:
                raise ValueError("performance_missing_baselines")
            start, end = job["baselines"]
            begin, finish = observation_bounds(start, end)
            if datetime.fromtimestamp(begin / 1000, timezone.utc).date() < archive_first_day(now):
                raise ValueError("performance_outside_retention")
            await heartbeat()
            raw = await self.account.bills_archive(begin, finish, on_page=heartbeat)
            await heartbeat()
            ledger_received_at = self.clock()
            received = datetime.fromtimestamp(ledger_received_at / 1000, timezone.utc)
            if datetime.fromtimestamp(begin / 1000, timezone.utc).date() < archive_first_day(received):
                raise ValueError("performance_outside_retention")
            records = await asyncio.to_thread(prepare_interval, raw, start, end)
            rates = {}
            for currency, candle_ms in sorted(flow_rates(records)):
                await heartbeat()
                price = await asyncio.to_thread(self.store.historical_rate, market_scope, currency, candle_ms)
                if price is None:
                    try:
                        price = await self.market.historical_index_rate(currency, candle_ms)
                    except HistoricalValuationError as exc:
                        if str(exc) != "historical_rate_unavailable":
                            raise
                        continue
                    await heartbeat()
                    # Immutable quotes remain useful if this interval is canceled.
                    await asyncio.to_thread(self.store.save_historical_rate, market_scope, currency, candle_ms, price)
                rates[(currency, candle_ms)] = price
            report = await asyncio.to_thread(value_interval, start, end, records, rates)
            report["ledger_received_at_ms"] = ledger_received_at
            await heartbeat()
            await asyncio.to_thread(
                self.store.complete_performance, job["id"], scope, owner, self.clock(), report,
                {"market_scope": market_scope, "records": records,
                 "rates": [{"currency": key[0], "candle_ms": key[1], "rate": value} for key, value in rates.items()]},
            )
        except asyncio.CancelledError:
            await asyncio.to_thread(
                self.store.defer_performance, job["id"], scope, owner, self.clock(), retry=True,
            )
            raise
        except PerformanceLeaseLost:
            return
        except Exception as exc:
            transient = isinstance(exc, (OkxAccountError, OkxMarketError, TimeoutError, OSError))
            known = {
                "performance_missing_baselines", "performance_outside_retention", "performance_interval_too_large",
                "performance_baselines_changed", "performance_bill_outside_interval", "performance_bill_identity_invalid",
            }
            error = "upstream_read_failed" if transient else str(exc) if str(exc) in known else "performance_evidence_invalid"
            await asyncio.to_thread(
                self.store.defer_performance, job["id"], scope, owner, self.clock(), retry=transient, error=error,
            )
