from dataclasses import dataclass
from typing import Any

from .strategy_engine import StrategyEngine


def _close(row: list[str]) -> float | None:
    try:
        value = float(row[4])
    except (IndexError, TypeError, ValueError):
        return None
    return value if value > 0 else None


@dataclass
class _Position:
    direction: int
    entry: float
    stop: float
    target: float
    leverage: float
    position_pct: float
    entered_at: str


class BacktestEngine:
    """Small deterministic replay engine for the baseline strategy."""

    def __init__(self, strategy: StrategyEngine | None = None) -> None:
        self.strategy = strategy or StrategyEngine()

    def run(
        self,
        inst_id: str,
        rows: list[list[str]],
        *,
        initial_equity: float = 1000,
        fee_bps: float = 5,
        strategy_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if initial_equity <= 0:
            raise ValueError("initial_equity must be positive")
        chronological = list(reversed(rows))
        equity = initial_equity
        peak = equity
        max_drawdown_pct = 0.0
        position: _Position | None = None
        trades: list[dict[str, Any]] = []
        curve: list[dict[str, Any]] = []

        for index in range(len(chronological)):
            row = chronological[index]
            price = _close(row)
            if price is None:
                continue
            timestamp = str(row[0]) if row else ""
            if position:
                hit_stop = (
                    position.direction == 1 and price <= position.stop
                ) or (
                    position.direction == -1 and price >= position.stop
                )
                hit_target = (
                    position.direction == 1 and price >= position.target
                ) or (
                    position.direction == -1 and price <= position.target
                )
                if hit_stop or hit_target:
                    reason = "stop_loss" if hit_stop else "take_profit"
                    self._close_trade(
                        position,
                        price,
                        timestamp,
                        reason,
                        equity,
                        trades,
                        fee_bps,
                    )
                    equity += self._settlement_delta(
                        position,
                        price,
                        equity,
                        fee_bps,
                    )
                    position = None

            if position is None and index >= 25:
                try:
                    analysis = self.strategy.analyze(
                        inst_id,
                        list(reversed(chronological[max(0, index - 99): index + 1])),
                        config=strategy_config,
                    )
                except ValueError:
                    analysis = {}
                signal = analysis.get("signal", {})
                if signal.get("action") in {"open_long", "open_short"}:
                    direction = 1 if signal["action"] == "open_long" else -1
                    position = _Position(
                        direction=direction,
                        entry=price,
                        stop=float(signal["stop_loss"]),
                        target=float(signal["take_profit"]),
                        leverage=float(signal.get("leverage", 1)),
                        position_pct=float(signal.get("position_pct", 0)),
                        entered_at=timestamp,
                    )
            peak = max(peak, equity)
            drawdown = (peak - equity) / peak * 100 if peak else 0
            max_drawdown_pct = max(max_drawdown_pct, drawdown)
            curve.append({"timestamp": timestamp, "equity": round(equity, 8)})

        if position and curve:
            last_price = _close(chronological[-1]) or position.entry
            timestamp = str(chronological[-1][0])
            self._close_trade(
                position,
                last_price,
                timestamp,
                "end_of_sample",
                equity,
                trades,
                fee_bps,
            )
            equity += self._settlement_delta(
                position,
                last_price,
                equity,
                fee_bps,
            )
            curve.append(
                {
                    "timestamp": timestamp,
                    "equity": round(equity, 8),
                }
            )

        wins = sum(1 for trade in trades if trade["pnl_pct"] > 0)
        return {
            "inst_id": inst_id,
            "initial_equity": initial_equity,
            "final_equity": round(equity, 8),
            "return_pct": round((equity / initial_equity - 1) * 100, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "trades": len(trades),
            "win_rate_pct": round(wins / len(trades) * 100, 4) if trades else 0,
            "fee_bps": fee_bps,
            "trade_log": trades,
            "equity_curve": curve,
        }

    @staticmethod
    def _close_trade(
        position: _Position,
        exit_price: float,
        exited_at: str,
        reason: str,
        equity: float,
        trades: list[dict[str, Any]],
        fee_bps: float,
    ) -> None:
        pnl_pct = (exit_price - position.entry) / position.entry * position.direction
        notional = equity * position.position_pct / 100 * position.leverage
        fee = notional * fee_bps / 10000
        trades.append(
            {
                "direction": "long" if position.direction == 1 else "short",
                "entry": position.entry,
                "exit": exit_price,
                "pnl_pct": round(pnl_pct * 100, 4),
                "position_pct": position.position_pct,
                "leverage": position.leverage,
                "notional": round(notional, 8),
                "fee": round(fee, 8),
                "reason": reason,
                "entered_at": position.entered_at,
                "exited_at": exited_at,
                "equity_before": round(equity, 8),
                "fee_bps": fee_bps,
            }
        )

    @staticmethod
    def _settlement_delta(
        position: _Position,
        exit_price: float,
        equity: float,
        fee_bps: float,
    ) -> float:
        pnl_rate = (exit_price - position.entry) / position.entry * position.direction
        notional = equity * position.position_pct / 100 * position.leverage
        return notional * pnl_rate - notional * fee_bps / 10000
