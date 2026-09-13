import asyncio
import logging
import time
from datetime import date, timedelta
from uuid import uuid4

from .historical_valuation import HistoricalValuationError, required_rates
from .okx_market import OkxMarketError
from .state_store import ValuationLeaseLost


def _now_ms() -> int:
    return int(time.time() * 1000)


class AccountValuationWorker:
    """Leased read-only historical quotes; cached prices survive interruptions."""

    def __init__(self, store, account_client, market_client):
        self.store = store
        self.account = account_client
        self.market = market_client
        self.owner = uuid4().hex
        self._task = None
        self._wake = asyncio.Event()

    async def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="openperpdesk-history-valuation")

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
                logging.getLogger(__name__).error("Valuation worker failed: %s", type(exc).__name__)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), 1)
            except TimeoutError:
                pass

    async def run_once(self):
        scope, market_scope = self.account.account_scope, self.market.rate_scope
        job = await asyncio.to_thread(
            self.store.claim_bill_valuation, scope, market_scope, self.owner, _now_ms(),
        )
        if job is None:
            return

        async def heartbeat():
            if scope != self.account.account_scope or market_scope != self.market.rate_scope:
                raise ValuationLeaseLost("valuation_account_or_market_changed")
            await asyncio.to_thread(self.store.touch_bill_valuation, job["id"], scope, self.owner, _now_ms())

        try:
            for offset in range(job["completed_days"], job["total_days"]):
                await heartbeat()
                day = date.fromisoformat(job["start_day"]) + timedelta(days=offset)
                records, captured_at = await asyncio.to_thread(self.store.valuation_day, scope, day)
                keys = await asyncio.to_thread(required_rates, records)
                loaded = missing = 0
                for currency, candle_ms in sorted(keys):
                    await heartbeat()
                    cached = await asyncio.to_thread(self.store.historical_rate, market_scope, currency, candle_ms)
                    if cached is not None:
                        continue
                    try:
                        rate = await self.market.historical_index_rate(currency, candle_ms)
                    except HistoricalValuationError as exc:
                        if str(exc) != "historical_rate_unavailable":
                            raise
                        missing += 1
                        continue
                    await heartbeat()
                    await asyncio.to_thread(
                        self.store.save_historical_rate, market_scope, currency, candle_ms, rate,
                        lease=(job["id"], scope, self.owner, _now_ms()),
                    )
                    loaded += 1
                await heartbeat()
                await asyncio.to_thread(
                    self.store.complete_valuation_day, job["id"], scope, self.owner, day, captured_at, loaded, missing, _now_ms(),
                )
        except asyncio.CancelledError:
            await asyncio.to_thread(self.store.finish_bill_valuation, job["id"], scope, self.owner, "queued")
            raise
        except ValuationLeaseLost:
            return
        except Exception as exc:
            known = {
                "valuation_history_incomplete", "valuation_history_changed", "valuation_day_too_large",
                "historical_rate_response_invalid", "historical_rate_duplicate", "historical_currency_invalid",
                "historical_timestamp_invalid",
            }
            error = str(exc) if str(exc) in known else "market_request_failed" if isinstance(exc, OkxMarketError) else type(exc).__name__
            await asyncio.to_thread(self.store.finish_bill_valuation, job["id"], scope, self.owner, "failed", error)
