import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .strategy_engine import DEFAULT_STRATEGY_CONFIG


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Small durable store for the control plane.

    SQLite keeps the first deployment self-contained. The database path is
    configurable so a Docker volume can persist state without putting secrets
    or runtime data in the repository.
    """

    def __init__(self, path: str | None = None) -> None:
        configured = path or os.getenv("STATE_DB_PATH", "")
        if configured:
            self.path = Path(configured)
        else:
            data_dir = Path(os.getenv("DATA_DIR", "./data"))
            self.path = data_dir / "openperpdesk.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    status TEXT NOT NULL,
                    inst_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    pos_side TEXT NOT NULL,
                    ord_type TEXT NOT NULL,
                    td_mode TEXT NOT NULL,
                    size REAL NOT NULL,
                    price REAL,
                    reduce_only INTEGER NOT NULL DEFAULT 0,
                    stop_loss REAL,
                    take_profit REAL,
                    source TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_created_at
                    ON orders(created_at DESC);
                CREATE TABLE IF NOT EXISTS fills (
                    trade_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    client_order_id TEXT,
                    inst_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    pos_side TEXT NOT NULL,
                    fill_price REAL NOT NULL,
                    fill_size REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    fee_ccy TEXT,
                    realized_pnl REAL NOT NULL DEFAULT 0,
                    filled_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_fills_filled_at
                    ON fills(filled_at DESC);
                CREATE TABLE IF NOT EXISTS positions (
                    position_key TEXT PRIMARY KEY,
                    inst_id TEXT NOT NULL,
                    pos_side TEXT NOT NULL,
                    size REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    mark_price REAL,
                    notional REAL NOT NULL DEFAULT 0,
                    stop_loss REAL,
                    take_profit REAL,
                    unrealized_pnl REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'open',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS analyses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    inst_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    bias TEXT NOT NULL,
                    signal_json TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS strategies (
                    strategy_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS control_flags (
                    name TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                """
            )
            position_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(positions)")
            }
            if "notional" not in position_columns:
                connection.execute(
                    "ALTER TABLE positions ADD COLUMN notional REAL NOT NULL DEFAULT 0"
                )
            connection.execute(
                """
                INSERT OR IGNORE INTO strategies
                    (strategy_id, name, enabled, config_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "structured-technical",
                    "结构化技术策略",
                    0,
                    json.dumps(DEFAULT_STRATEGY_CONFIG, ensure_ascii=True),
                    _utc_now(),
                ),
            )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row else None

    def get_order(self, client_order_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM orders WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
        return self._row(row)

    def save_order(self, order: dict[str, Any]) -> dict[str, Any]:
        now = order.get("updated_at") or _utc_now()
        created_at = order.get("created_at") or now
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO orders (
                    client_order_id, exchange_order_id, status, inst_id, side,
                    pos_side, ord_type, td_mode, size, price, reduce_only,
                    stop_loss, take_profit, source, raw_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_order_id) DO UPDATE SET
                    exchange_order_id = excluded.exchange_order_id,
                    status = excluded.status,
                    raw_json = excluded.raw_json,
                    updated_at = excluded.updated_at
                """,
                (
                    order["client_order_id"],
                    order.get("exchange_order_id"),
                    order["status"],
                    order["inst_id"],
                    order["side"],
                    order["pos_side"],
                    order["ord_type"],
                    order["td_mode"],
                    order["size"],
                    order.get("price"),
                    int(bool(order.get("reduce_only", False))),
                    order.get("stop_loss"),
                    order.get("take_profit"),
                    order.get("source", "manual"),
                    json.dumps(order.get("raw", {}), ensure_ascii=True),
                    created_at,
                    now,
                ),
            )
        return self.get_order(order["client_order_id"]) or order

    def list_orders(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_active_order(
        self,
        inst_id: str,
        *,
        reduce_only: bool = False,
    ) -> bool:
        """Check for an exchange order that has not reached a terminal state."""
        active_statuses = (
            "submitting",
            "submitted",
            "live",
            "partially_filled",
        )
        placeholders = ",".join("?" for _ in active_statuses)
        with self._connection() as connection:
            row = connection.execute(
                f"""
                SELECT 1
                FROM orders
                WHERE inst_id = ?
                  AND reduce_only = ?
                  AND status IN ({placeholders})
                LIMIT 1
                """,
                (inst_id, int(reduce_only), *active_statuses),
            ).fetchone()
        return row is not None

    def save_fill(self, fill: dict[str, Any]) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO fills (
                    trade_id, exchange_order_id, client_order_id, inst_id, side,
                    pos_side, fill_price, fill_size, fee, fee_ccy, realized_pnl,
                    filled_at, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_id) DO UPDATE SET
                    exchange_order_id = excluded.exchange_order_id,
                    client_order_id = excluded.client_order_id,
                    fill_price = excluded.fill_price,
                    fill_size = excluded.fill_size,
                    fee = excluded.fee,
                    fee_ccy = excluded.fee_ccy,
                    realized_pnl = excluded.realized_pnl,
                    filled_at = excluded.filled_at,
                    raw_json = excluded.raw_json
                """,
                (
                    fill["trade_id"],
                    fill.get("exchange_order_id"),
                    fill.get("client_order_id"),
                    fill["inst_id"],
                    fill["side"],
                    fill["pos_side"],
                    fill["fill_price"],
                    fill["fill_size"],
                    fill.get("fee", 0),
                    fill.get("fee_ccy"),
                    fill.get("realized_pnl", 0),
                    fill["filled_at"],
                    json.dumps(fill.get("raw", {}), ensure_ascii=True),
                ),
            )
        return self.get_fill(fill["trade_id"]) or fill

    def get_fill(self, trade_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM fills WHERE trade_id = ?",
                (trade_id,),
            ).fetchone()
        return self._row(row)

    def list_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM fills ORDER BY filled_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def pnl_summary(self, limit: int = 500) -> dict[str, Any]:
        fills = self.list_fills(limit)
        realized_pnl = sum(float(item.get("realized_pnl") or 0) for item in fills)
        fees = sum(float(item.get("fee") or 0) for item in fills)
        by_instrument: dict[str, dict[str, float | int]] = {}
        for item in fills:
            inst_id = str(item["inst_id"])
            bucket = by_instrument.setdefault(
                inst_id,
                {"fills": 0, "realized_pnl": 0.0, "fees": 0.0},
            )
            bucket["fills"] += 1
            bucket["realized_pnl"] += float(item.get("realized_pnl") or 0)
            bucket["fees"] += float(item.get("fee") or 0)
        return {
            "fills": len(fills),
            "realized_pnl": round(realized_pnl, 8),
            "fees": round(fees, 8),
            "net_pnl": round(realized_pnl + fees, 8),
            "by_instrument": by_instrument,
        }

    def performance_report(
        self,
        *,
        initial_equity: float = 1000.0,
        limit: int = 500,
    ) -> dict[str, Any]:
        """Build realized performance metrics from the durable fill ledger."""
        if initial_equity <= 0:
            raise ValueError("initial_equity must be greater than zero")
        fills = sorted(
            self.list_fills(limit),
            key=lambda item: str(item.get("filled_at") or ""),
        )
        orders = {
            str(item["client_order_id"]): item
            for item in self.list_orders(500)
            if item.get("client_order_id")
        }
        equity = float(initial_equity)
        peak = equity
        max_drawdown = 0.0
        daily: dict[str, dict[str, float | int]] = {}
        by_strategy: dict[str, dict[str, float | int]] = {}
        curve: list[dict[str, float | str]] = []

        for fill in fills:
            realized = float(fill.get("realized_pnl") or 0)
            fee = float(fill.get("fee") or 0)
            net = realized + fee
            equity += net
            peak = max(peak, equity)
            max_drawdown = max(max_drawdown, peak - equity)
            filled_at = str(fill.get("filled_at") or "unknown")
            day = filled_at[:10] if len(filled_at) >= 10 else "unknown"
            order = orders.get(str(fill.get("client_order_id") or ""))
            strategy = str(
                (order or {}).get("source")
                or (fill.get("raw") or {}).get("source")
                or "unknown"
            )
            for bucket in (
                daily.setdefault(
                    day,
                    {"fills": 0, "realized_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0},
                ),
                by_strategy.setdefault(
                    strategy,
                    {"fills": 0, "realized_pnl": 0.0, "fees": 0.0, "net_pnl": 0.0},
                ),
            ):
                bucket["fills"] += 1
                bucket["realized_pnl"] += realized
                bucket["fees"] += fee
                bucket["net_pnl"] += net
            curve.append(
                {
                    "filled_at": filled_at,
                    "equity": round(equity, 8),
                    "net_pnl": round(net, 8),
                }
            )

        net_pnl = equity - initial_equity
        def _round_buckets(
            values: dict[str, dict[str, float | int]],
        ) -> dict[str, dict[str, float | int]]:
            return {
                key: {
                    **bucket,
                    "realized_pnl": round(float(bucket["realized_pnl"]), 8),
                    "fees": round(float(bucket["fees"]), 8),
                    "net_pnl": round(float(bucket["net_pnl"]), 8),
                }
                for key, bucket in values.items()
            }

        return {
            "fills": len(fills),
            "initial_equity": round(initial_equity, 8),
            "ending_equity": round(equity, 8),
            "realized_pnl": round(
                sum(float(item.get("realized_pnl") or 0) for item in fills),
                8,
            ),
            "fees": round(sum(float(item.get("fee") or 0) for item in fills), 8),
            "net_pnl": round(net_pnl, 8),
            "return_pct": round(net_pnl / initial_equity * 100, 8),
            "max_drawdown": round(max_drawdown, 8),
            "max_drawdown_pct": round(max_drawdown / initial_equity * 100, 8),
            "daily": _round_buckets(daily),
            "by_strategy": _round_buckets(by_strategy),
            "equity_curve": curve,
        }

    def upsert_position(self, position: dict[str, Any]) -> dict[str, Any]:
        position_key = position.get(
            "position_key",
            f"{position['inst_id']}:{position['pos_side']}",
        )
        now = position.get("updated_at") or _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO positions (
                    position_key, inst_id, pos_side, size, entry_price, mark_price,
                    notional, stop_loss, take_profit, unrealized_pnl, status, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(position_key) DO UPDATE SET
                    size = excluded.size,
                    entry_price = excluded.entry_price,
                    mark_price = excluded.mark_price,
                    notional = excluded.notional,
                    stop_loss = excluded.stop_loss,
                    take_profit = excluded.take_profit,
                    unrealized_pnl = excluded.unrealized_pnl,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (
                    position_key,
                    position["inst_id"],
                    position["pos_side"],
                    position["size"],
                    position["entry_price"],
                    position.get("mark_price"),
                    position.get("notional", 0),
                    position.get("stop_loss"),
                    position.get("take_profit"),
                    position.get("unrealized_pnl", 0),
                    position.get("status", "open"),
                    now,
                ),
            )
        return self.get_position(position_key) or position

    def get_position(self, position_key: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM positions WHERE position_key = ?",
                (position_key,),
            ).fetchone()
        return self._row(row)

    def list_positions(self, open_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM positions"
        if open_only:
            query += " WHERE status = 'open'"
        query += " ORDER BY updated_at DESC"
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return [dict(row) for row in rows]

    def risk_context(self, account_equity: float) -> dict[str, float]:
        """Return conservative exposure and current-day PnL for risk checks."""
        if account_equity <= 0:
            return {
                "current_notional": 0.0,
                "daily_pnl": 0.0,
                "daily_pnl_pct": 0.0,
            }

        current_notional = 0.0
        positions = self.list_positions()
        for position in positions:
            stored_notional = abs(float(position.get("notional") or 0))
            if stored_notional:
                current_notional += stored_notional
                continue
            reference_price = float(
                position.get("mark_price")
                or position.get("entry_price")
                or 0
            )
            current_notional += abs(float(position.get("size") or 0)) * reference_price

        day_prefix = datetime.now(timezone.utc).date().isoformat()
        daily_pnl = 0.0
        for fill in self.list_fills(500):
            if str(fill.get("filled_at") or "").startswith(day_prefix):
                daily_pnl += float(fill.get("realized_pnl") or 0)
                daily_pnl += float(fill.get("fee") or 0)
        daily_pnl += sum(
            float(position.get("unrealized_pnl") or 0)
            for position in positions
        )
        return {
            "current_notional": round(current_notional, 8),
            "daily_pnl": round(daily_pnl, 8),
            "daily_pnl_pct": round(daily_pnl / account_equity * 100, 8),
        }

    def close_positions_not_seen(
        self,
        position_keys: set[str],
        *,
        updated_at: str | None = None,
    ) -> int:
        """Close open local positions absent from a successful exchange snapshot."""
        now = updated_at or _utc_now()
        with self._connection() as connection:
            if position_keys:
                placeholders = ",".join("?" for _ in position_keys)
                cursor = connection.execute(
                    f"""
                    UPDATE positions
                    SET size = 0, status = 'closed', updated_at = ?
                    WHERE status = 'open' AND position_key NOT IN ({placeholders})
                    """,
                    (now, *position_keys),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE positions
                    SET size = 0, status = 'closed', updated_at = ?
                    WHERE status = 'open'
                    """,
                    (now,),
                )
            return int(cursor.rowcount)

    def add_audit(
        self,
        event_type: str,
        message: str,
        *,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO audit_events
                    (event_type, severity, message, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    severity,
                    message,
                    json.dumps(payload or {}, ensure_ascii=True),
                    _utc_now(),
                ),
            )

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def save_analysis(self, analysis: dict[str, Any]) -> dict[str, Any]:
        created_at = analysis.get("created_at") or _utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO analyses
                    (inst_id, source, bias, signal_json, report_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    analysis["inst_id"],
                    analysis["source"],
                    analysis["bias"],
                    json.dumps(analysis["signal"], ensure_ascii=True),
                    json.dumps(analysis["report"], ensure_ascii=True),
                    created_at,
                ),
            )
            analysis["id"] = cursor.lastrowid
            analysis["created_at"] = created_at
        return analysis

    def list_analyses(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM analyses ORDER BY id DESC LIMIT ?",
                (max(1, min(limit, 100)),),
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["signal"] = json.loads(item.pop("signal_json"))
            item["report"] = json.loads(item.pop("report_json"))
            result.append(item)
        return result

    def save_strategy(
        self,
        strategy_id: str,
        name: str,
        *,
        enabled: bool,
        config: dict[str, Any],
    ) -> dict[str, Any]:
        now = _utc_now()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO strategies(strategy_id, name, enabled, config_json, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(strategy_id) DO UPDATE SET
                    name = excluded.name,
                    enabled = excluded.enabled,
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (
                    strategy_id,
                    name,
                    int(enabled),
                    json.dumps(config, ensure_ascii=True),
                    now,
                ),
            )
        return self.get_strategy(strategy_id) or {}

    def get_strategy(self, strategy_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM strategies WHERE strategy_id = ?",
                (strategy_id,),
            ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["enabled"] = bool(item["enabled"])
        item["config"] = json.loads(item.pop("config_json"))
        return item

    def list_strategies(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM strategies ORDER BY strategy_id",
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["enabled"] = bool(item["enabled"])
            item["config"] = json.loads(item.pop("config_json"))
            result.append(item)
        return result

    def get_control_flag(self, name: str, default: bool = False) -> bool:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM control_flags WHERE name = ?",
                (name,),
            ).fetchone()
        if not row:
            return default
        return str(row["value"]).lower() == "true"

    def set_control_flag(self, name: str, value: bool, reason: str = "") -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO control_flags(name, value, reason, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    value = excluded.value,
                    reason = excluded.reason,
                    updated_at = excluded.updated_at
                """,
                (name, str(bool(value)).lower(), reason, _utc_now()),
            )

    def get_control_flags(self) -> dict[str, dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT name, value, reason, updated_at FROM control_flags",
            ).fetchall()
        return {
            row["name"]: {
                "value": str(row["value"]).lower() == "true",
                "reason": row["reason"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        }
