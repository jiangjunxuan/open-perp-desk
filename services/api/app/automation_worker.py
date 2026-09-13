import asyncio
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any, Awaitable, Callable

from .account_sync import AccountSynchronizer
from .execution_engine import ExecutionEngine
from .okx_account import OkxAccountClient, OkxAccountError
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
        notifier: Callable[..., Awaitable[bool]] | None = None,
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
        self.notifier = notifier
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
        self._cycle_lock = asyncio.Lock()

    async def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(
                self._run(),
                name="openperpdesk-automation-worker",
            )
            await self._notify_event(
                "worker_started",
                "OpenPerpDesk Worker 已启动",
                f"自动 Worker 已启动，模式：{'Dry Run' if self.dry_run else 'Demo 执行'}。",
                payload={"dry_run": self.dry_run, "symbols": _symbols()},
            )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        await self._notify_event(
            "worker_stopped",
            "OpenPerpDesk Worker 已停止",
            "自动 Worker 已停止，不再生成新的策略执行请求。",
        )

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
        if self._cycle_lock.locked():
            return {"ran": False, "reason": "worker_cycle_in_progress"}
        async with self._cycle_lock:
            return await self._run_once()

    async def _run_once(self) -> dict[str, Any]:
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
                if rest_result.get("errors"):
                    raise OkxAccountError("Private account snapshot is incomplete")
            except Exception as exc:
                self.store.add_audit(
                    "worker_account_sync_failed",
                    "Worker skipped trading after account refresh failure",
                    severity="warning",
                    payload={"error": type(exc).__name__},
                )
                await self._notify_event(
                    "worker_account_sync_failed",
                    "OpenPerpDesk Worker 账户刷新失败",
                    f"Worker 未能刷新完整账户状态，已停止本轮交易：{type(exc).__name__}。",
                    payload={"error": type(exc).__name__},
                    severity="warning",
                )
                return {"ran": False, "reason": "account_snapshot_unavailable"}
        try:
            account_equity = await self._account_equity()
        except (OkxAccountError, ValueError, TypeError) as exc:
            self.store.add_audit(
                "worker_equity_unavailable",
                "Worker skipped trading because verified account equity is unavailable",
                severity="warning",
                payload={"error": type(exc).__name__},
            )
            return {"ran": False, "reason": "account_equity_unavailable"}
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
                    if (
                        close_result.get("accepted")
                        and not close_result.get("idempotent")
                    ):
                        await self.execution.notify_event(
                            "protective_exit_preview" if self.dry_run else "protective_exit_triggered",
                            "OpenPerpDesk 保护性平仓预览" if self.dry_run else "OpenPerpDesk 保护性平仓触发",
                            (
                                f"{symbol} {exit_event['reason']} 已触发，"
                                f"参考价格 {last_price}。"
                                + ("仅风控预览，不会向交易所发单。" if self.dry_run else "已提交平仓请求。")
                            ),
                            payload={
                                "inst_id": symbol,
                                "reason": exit_event["reason"],
                                "mark_price": last_price,
                                "size": abs(float(position["size"])),
                                "dry_run": self.dry_run,
                            },
                            severity="warning",
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
        if not self.account_client.configured:
            if not self.dry_run:
                raise OkxAccountError("Private account credentials are required")
            equity = float(os.getenv("AUTO_TRADING_ACCOUNT_EQUITY", "1000"))
        else:
            rows = await self.account_client.balance()
            if not rows:
                raise OkxAccountError("Account balance is empty")
            equity = float(rows[0].get("totalEq") or rows[0].get("adjEq") or 0)
        if not math.isfinite(equity) or equity <= 0:
            raise OkxAccountError("Account equity must be finite and positive")
        return equity

    async def _order_size(
        self,
        symbol: str,
        mark_price: float,
        account_equity: float,
        leverage: float,
        position_pct: float,
    ) -> float:
        """Convert risk budget to exchange contract size using instrument metadata."""
        if not all(math.isfinite(value) and value > 0 for value in (
            mark_price, account_equity, leverage, position_pct,
        )):
            return 0.0
        instruments_loader = getattr(self.market_client, "instruments", None)
        if instruments_loader is None:
            return 0.0
        rows = await instruments_loader(symbol)
        instrument = next((row for row in rows if row.get("instId") == symbol), {})
        try:
            ct_val = Decimal(str(instrument.get("ctVal") or "0"))
            ct_mult = Decimal(str(instrument.get("ctMult") or "1"))
            lot_size = Decimal(str(instrument.get("lotSz") or "0"))
            min_size = Decimal(str(instrument.get("minSz") or "0"))
        except InvalidOperation:
            return 0.0
        if not all(value.is_finite() and value > 0 for value in (
            ct_val, ct_mult, lot_size, min_size,
        )):
            return 0.0
        target_notional = (
            Decimal(str(account_equity))
            * Decimal(str(position_pct))
            / Decimal("100")
            * Decimal(str(leverage))
        )
        unit_notional = ct_val * ct_mult
        if instrument.get("ctType") != "inverse":
            unit_notional *= Decimal(str(mark_price))
        raw_size = target_notional / unit_notional
        sized = (raw_size / lot_size).to_integral_value(rounding=ROUND_DOWN) * lot_size
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
                await self._notify_event(
                    "worker_failed",
                    "OpenPerpDesk Worker 异常",
                    f"自动 Worker 周期失败：{type(exc).__name__}。",
                    payload={"error": type(exc).__name__},
                    severity="error",
                )
            await asyncio.sleep(self.interval_seconds)

    async def _notify_event(
        self,
        event_type: str,
        title: str,
        content: str,
        *,
        payload: dict[str, Any] | None = None,
        severity: str = "info",
    ) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(
                event_type,
                title,
                content,
                payload=payload,
                severity=severity,
            )
        except Exception as exc:
            self.store.add_audit(
                "notification_dispatch_failed",
                "Notification dispatcher raised unexpectedly",
                severity="warning",
                payload={"event_type": event_type, "error": type(exc).__name__},
            )
