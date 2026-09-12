import asyncio
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Callable

from .account_sync import AccountSynchronizer
from .execution_engine import ExecutionEngine
from .okx_account import OkxAccountClient
from .okx_market import OkxMarketClient
from .risk_engine import RiskEngine
from .state_store import StateStore
from .safety_control import SafetyController
from .strategy_engine import StrategyEngine
from .trading_signal import TradeSignal


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _symbols() -> list[str]:
    raw = os.getenv("AUTO_TRADING_SYMBOLS", os.getenv("MARKET_SYMBOLS", "BTC-USDT-SWAP"))
    return [item.strip().upper() for item in raw.split(",") if item.strip()]


class AutomationWorker:
    """Periodic Demo strategy runner with explicit opt-in configuration."""

    def __init__(
        self,
        market_client: OkxMarketClient,
        account_client: OkxAccountClient,
        account_sync: AccountSynchronizer,
        strategy: StrategyEngine,
        execution: ExecutionEngine,
        risk: RiskEngine,
        store: StateStore,
        safety: SafetyController | None = None,
        market_data_fresh: Callable[[], bool] | None = None,
    ) -> None:
        self.market_client = market_client
        self.account_client = account_client
        self.account_sync = account_sync
        self.strategy = strategy
        self.execution = execution
        self.risk = risk
        self.store = store
        self.safety = safety or SafetyController(store)
        self.market_data_fresh = market_data_fresh or (lambda: True)
        self.enabled = os.getenv("AUTO_TRADING_ENABLED", "false").lower() == "true"
        self.dry_run = os.getenv("AUTO_TRADING_DRY_RUN", "true").lower() == "true"
        self.strategy_id = os.getenv(
            "AUTO_TRADING_STRATEGY_ID",
            "structured-technical",
        ).strip()
        self.interval_seconds = max(
            15,
            int(os.getenv("AUTO_TRADING_INTERVAL_SECONDS", "60")),
        )
        self.bar = os.getenv("AUTO_TRADING_BAR", "15m")
        self.candle_limit = max(
            30,
            min(300, int(os.getenv("AUTO_TRADING_CANDLE_LIMIT", "100"))),
        )
        self.last_run_at: str | None = None
        self.last_error: str | None = None
        self.run_count = 0
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="openperpdesk-automation-worker",
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "running": self._task is not None,
            "interval_seconds": self.interval_seconds,
            "bar": self.bar,
            "symbols": _symbols(),
            "strategy_id": self.strategy_id,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "run_count": self.run_count,
        }

    async def run_once(self) -> dict[str, Any]:
        if not self.enabled:
            return {"ran": False, "reason": "AUTO_TRADING_ENABLED is false"}
        if not self.safety.execution_allowed:
            return {"ran": False, "reason": "emergency stop is active"}
        if not self.dry_run and not self.execution.trade_client.enabled:
            return {"ran": False, "reason": "Demo execution is disabled"}
        strategy_record = self.store.get_strategy(self.strategy_id)
        if not strategy_record:
            return {
                "ran": False,
                "reason": f"strategy_not_found:{self.strategy_id}",
            }
        if not strategy_record["enabled"]:
            return {
                "ran": False,
                "reason": f"strategy_disabled:{self.strategy_id}",
            }
        strategy_config = strategy_record["config"]

        stream_result = self.account_sync.sync_stream()
        rest_result: dict[str, Any] | None = None
        if self.account_client.configured:
            try:
                # Refresh private state immediately before sizing/risk checks.
                # The background reconciler remains useful between cycles.
                rest_result = await self.account_sync.sync_rest()
            except Exception as exc:
                self.store.add_audit(
                    "worker_account_sync_failed",
                    "Worker kept the last private state after account refresh failure",
                    severity="warning",
                    payload={"error": type(exc).__name__},
                )
        account_equity = await self._account_equity()
        risk_context = self.store.risk_context(account_equity)
        current_notional = risk_context["current_notional"]
        results: list[dict[str, Any]] = []
        for symbol in _symbols():
            ticker = await self.market_client.ticker(symbol)
            last_price = float(ticker.get("last") or 0)
            if last_price > 0:
                exit_event = self.execution.protective_exit(
                    inst_id=symbol,
                    mark_price=last_price,
                )
                if exit_event:
                    position = exit_event["position"]
                    is_long = (
                        position["pos_side"] in {"long", "net"}
                        and position["size"] > 0
                    )
                    close_signal = TradeSignal(
                        inst_id=symbol,
                        action="close",
                        confidence=1,
                        leverage=1,
                        position_pct=0,
                        source=f"protective-{exit_event['reason']}",
                    )
                    close_result = await self.execution.submit_signal(
                        close_signal,
                        account_equity=account_equity,
                        daily_pnl_pct=risk_context["daily_pnl_pct"],
                        current_notional=current_notional,
                        size=abs(float(position["size"])),
                        dry_run=self.dry_run,
                        side_override="sell" if is_long else "buy",
                        idempotency_key=(
                            f"{symbol}:{position['position_key']}:{exit_event['reason']}"
                        ),
                        market_data_fresh=self.market_data_fresh(),
                    )
                    results.append(
                        {
                            "inst_id": symbol,
                            "action": exit_event["reason"],
                            "accepted": close_result.get("accepted", False),
                        }
                    )
                    continue
            candles = await self.market_client.candles(
                symbol,
                self.bar,
                self.candle_limit,
            )
            analysis = self.strategy.analyze(
                symbol,
                candles,
                config=strategy_config,
            )
            self.store.save_analysis(analysis)
            signal = TradeSignal.model_validate(analysis["signal"])
            positions = [
                item for item in self.store.list_positions()
                if item["inst_id"] == symbol
            ]
            if (
                signal.action in {"open_long", "open_short"}
                and (
                    positions
                    or self.store.has_active_order(symbol, reduce_only=False)
                )
            ):
                results.append({"inst_id": symbol, "action": "skip_open_existing_exposure"})
                continue
            size = await self._order_size(
                symbol,
                last_price,
                account_equity,
                signal.leverage,
                signal.position_pct,
            )
            if size <= 0:
                results.append(
                    {
                        "inst_id": symbol,
                        "action": "skip_size_below_minimum",
                        "price": last_price,
                    }
                )
                continue
            result = await self.execution.submit_signal(
                signal,
                account_equity=account_equity,
                daily_pnl_pct=risk_context["daily_pnl_pct"],
                current_notional=current_notional,
                size=size,
                dry_run=self.dry_run,
                market_data_fresh=self.market_data_fresh(),
                idempotency_key=self._signal_idempotency_key(
                    symbol,
                    signal.action,
                    candles,
                ),
            )
            if result.get("accepted") and signal.action in {"open_long", "open_short"}:
                current_notional += (
                    account_equity
                    * signal.position_pct
                    / 100
                    * signal.leverage
                )
            results.append(
                {
                    "inst_id": symbol,
                    "action": signal.action,
                    "size": size,
                    "accepted": result.get("accepted", False),
                    "idempotent": result.get("idempotent", False),
                    "reasons": result.get("reasons", []),
                }
            )
        self.last_run_at = _now()
        self.last_error = None
        self.run_count += 1
        self.store.add_audit(
            "worker_run",
            "Automation worker cycle completed",
            payload={
                "stream": stream_result,
                "rest": rest_result,
                "risk_context": risk_context,
                "ending_notional": current_notional,
                "results": results,
            },
        )
        return {
            "ran": True,
            "stream": stream_result,
            "rest": rest_result,
            "risk_context": {
                **risk_context,
                "ending_notional": round(current_notional, 8),
            },
            "results": results,
        }

    async def _account_equity(self) -> float:
        fallback = float(os.getenv("AUTO_TRADING_ACCOUNT_EQUITY", "1000"))
        if not self.account_client.configured:
            return fallback
        try:
            rows = await self.account_client.balance()
            if rows:
                return float(rows[0].get("totalEq") or rows[0].get("adjEq") or fallback)
        except Exception:
            return fallback
        return fallback

    async def _order_size(
        self,
        symbol: str,
        mark_price: float,
        account_equity: float,
        leverage: float,
        position_pct: float,
    ) -> float:
        """Convert risk budget to exchange contract size using instrument metadata."""
        if mark_price <= 0 or account_equity <= 0 or position_pct <= 0:
            return 0.0
        instruments_loader = getattr(self.market_client, "instruments", None)
        if instruments_loader is None:
            return 1.0
        rows = await instruments_loader(symbol)
        instrument = rows[0] if rows else {}
        ct_val = Decimal(str(instrument.get("ctVal") or "1"))
        lot_size = Decimal(str(instrument.get("lotSz") or "1"))
        min_size = Decimal(str(instrument.get("minSz") or lot_size))
        if ct_val <= 0 or lot_size <= 0 or min_size <= 0:
            return 0.0
        target_notional = (
            Decimal(str(account_equity))
            * Decimal(str(position_pct))
            / Decimal("100")
            * Decimal(str(leverage))
        )
        raw_size = target_notional / (Decimal(str(mark_price)) * ct_val)
        sized = raw_size.quantize(lot_size, rounding=ROUND_DOWN)
        if sized < min_size:
            return 0.0
        return float(sized)

    def _signal_idempotency_key(
        self,
        symbol: str,
        action: str,
        candles: list[list[str]],
    ) -> str:
        """Keep one decision per strategy candle while allowing the next candle."""
        latest_timestamp = str(candles[0][0]) if candles and candles[0] else "unknown"
        return f"{symbol}:{self.strategy_id}:{self.bar}:{latest_timestamp}:{action}"

    async def _run(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.store.add_audit(
                    "worker_failed",
                    "Automation worker cycle failed",
                    severity="error",
                    payload={"error": type(exc).__name__},
                )
            await asyncio.sleep(self.interval_seconds)
